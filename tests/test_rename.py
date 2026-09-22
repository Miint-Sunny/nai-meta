# -*- coding: utf-8 -*-
"""-N：输出改名成 日期-编号；同目录接着已有的号；-i -N 原地改名；NAI 默认文件名提醒；TUI /n。"""
import json
import shlex

import pytest
from PIL import Image
from test_roundtrip import nai_png, random_rgba
from test_tui import drive as drive_tui

import nai_meta.nai_strip as ns
from nai_meta.nai_inspect import inspect_file
from nai_meta.nai_strip import main as strip_main

DAY = '20260923'
NAI_STYLE = '1girl, full body, from side, giant hand s-1684195033'


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'cfg'))
    monkeypatch.setenv('APPDATA', str(tmp_path / 'cfg'))
    monkeypatch.setattr(ns, 'today', lambda: DAY)
    return tmp_path / 'cfg' / 'nai-meta'


def clean(p):
    rec = inspect_file(p)
    return not rec['text_chunks'] and rec['stealth'] is None


def test_rename_next_to_original_continues_numbering(tmp_path, cfg, capsys):
    d = tmp_path / 'pics'
    d.mkdir()
    nai_png(d / f'{NAI_STYLE}.png')
    nai_png(d / 'b.png')
    (d / f'{DAY}-0003.webp').write_bytes(b'')                           # 同日已有 0003（别的扩展名也算）
    (d / '20260922-0009.png').write_bytes(b'')                          # 别的日子不算
    assert strip_main([str(d / f'{NAI_STYLE}.png'), str(d / 'b.png'), '-N']) == 0
    assert clean(d / f'{DAY}-0004.png') and clean(d / f'{DAY}-0005.png')
    assert (d / f'{NAI_STYLE}.png').exists()                            # 旁边模式不动原图
    assert 'NAI 默认命名' not in capsys.readouterr().out                 # 已经改名就不提醒
    assert strip_main([str(d / 'b.png'), '-N']) == 0                    # 再跑一次接着排
    assert (d / f'{DAY}-0006.png').exists()


def test_rename_outdir_recursive_and_dry_run(tmp_path, cfg, capsys):
    src = tmp_path / 'src'
    (src / 'sub').mkdir(parents=True)
    nai_png(src / 'a.png')
    nai_png(src / 'sub' / 'b.png')
    nai_png(src / 'sub' / 'c.png')
    out = tmp_path / 'out'
    assert strip_main([str(src), '-r', '-d', str(out), '-N', '-n']) == 0      # dry-run：报名字不写
    got = capsys.readouterr().out
    assert f'{DAY}-0001.png' in got and f'{DAY}-0002.png' in got and not out.exists()
    assert strip_main([str(src), '-r', '-d', str(out), '-N', '-y']) == 0
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob('*.png')) == [
        f'{DAY}-0001.png', f'sub/{DAY}-0001.png', f'sub/{DAY}-0002.png']  # 每个目录各自从 0001 起


def test_rename_in_place_removes_original(tmp_path, cfg):
    a, b = tmp_path / f'{NAI_STYLE}.png', tmp_path / f'{DAY}-0001.png'
    nai_png(a)
    nai_png(b)                                                           # 已经是编号名：原地剥，不换号
    plain = tmp_path / 'plain.png'
    Image.fromarray(random_rgba()[:, :, :3]).save(plain)                # 没元数据：只改名
    assert strip_main([str(a), str(b), str(plain), '-i', '-N']) == 0
    names = sorted(p.name for p in tmp_path.glob('*.png'))
    assert names == [f'{DAY}-0001.png', f'{DAY}-0002.png', f'{DAY}-0003.png']
    assert all(clean(tmp_path / n) for n in names)


def test_rename_conflicts_and_hint(tmp_path, cfg, capsys):
    src = tmp_path / f'{NAI_STYLE}.png'
    nai_png(src)
    assert strip_main([str(src), '-o', str(tmp_path / 'x.png'), '-N']) == 1
    assert strip_main([str(src)]) == 0
    out = capsys.readouterr().out
    assert 'NAI 默认命名' in out and '-N' in out                         # 没改名：提醒一句
    assert (tmp_path / f'{NAI_STYLE}_clean.png').exists()


def test_tui_rename_toggle_is_remembered(tmp_path, cfg):
    d = tmp_path / 'pics'
    d.mkdir()
    nai_png(d / 'a.png')
    nai_png(d / 'b.png')
    q = shlex.quote
    assert drive_tui(f'/n\n{q(str(d / "a.png"))}\n/q\n') == 0
    assert (d / f'{DAY}-0001.png').exists()
    assert json.loads((cfg / 'tui.json').read_text('utf-8'))['rename'] is True
    assert drive_tui(f'{q(str(d / "b.png"))}\n/q\n') == 0             # 重进仍开着，接着排号
    assert (d / f'{DAY}-0002.png').exists()
