# -*- coding: utf-8 -*-
"""按日期重命名（-N）：序号接续目录中已有的编号；-i -N 重命名源文件；NovelAI 默认文件名的警告；交互模式的 /n。"""
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
    (d / f'{DAY}-0003.webp').write_bytes(b'')                           # 当天已有 0003（不区分扩展名）
    (d / '20260922-0009.png').write_bytes(b'')                          # 其他日期的编号不计入
    assert strip_main([str(d / f'{NAI_STYLE}.png'), str(d / 'b.png'), '-N']) == 0
    assert clean(d / f'{DAY}-0004.png') and clean(d / f'{DAY}-0005.png')
    assert (d / f'{NAI_STYLE}.png').exists()                            # 输出到源文件所在目录时，源文件保留
    assert 'NovelAI 默认文件名' not in capsys.readouterr().err          # 已使用 -N 时不提示
    assert strip_main([str(d / 'b.png'), '-N']) == 0                    # 再次运行时序号继续递增
    assert (d / f'{DAY}-0006.png').exists()


def test_rename_outdir_recursive_and_dry_run(tmp_path, cfg, capsys):
    src = tmp_path / 'src'
    (src / 'sub').mkdir(parents=True)
    nai_png(src / 'a.png')
    nai_png(src / 'sub' / 'b.png')
    nai_png(src / 'sub' / 'c.png')
    out = tmp_path / 'out'
    assert strip_main([str(src), '-r', '-d', str(out), '-N', '-n']) == 0      # 试运行：报告文件名，不写入文件
    got = capsys.readouterr().out
    assert f'{DAY}-0001.png' in got and f'{DAY}-0002.png' in got and not out.exists()
    assert strip_main([str(src), '-r', '-d', str(out), '-N', '-y']) == 0
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob('*.png')) == [
        f'{DAY}-0001.png', f'sub/{DAY}-0001.png', f'sub/{DAY}-0002.png']  # 各目录分别从 0001 开始编号


def test_rename_in_place_removes_original(tmp_path, cfg):
    a, b = tmp_path / f'{NAI_STYLE}.png', tmp_path / f'{DAY}-0001.png'
    nai_png(a)
    nai_png(b)                                                           # 文件名已是“日期-序号”格式：原地处理，保留原名
    plain = tmp_path / 'plain.png'
    Image.fromarray(random_rgba()[:, :, :3]).save(plain)                # 不含元数据：只重命名
    assert strip_main([str(a), str(b), str(plain), '-i', '-N']) == 0
    names = sorted(p.name for p in tmp_path.glob('*.png'))
    assert names == [f'{DAY}-0001.png', f'{DAY}-0002.png', f'{DAY}-0003.png']
    assert all(clean(tmp_path / n) for n in names)


def test_rename_conflicts_and_hint(tmp_path, cfg, capsys):
    src = tmp_path / f'{NAI_STYLE}.png'
    nai_png(src)
    assert strip_main([str(src), '-o', str(tmp_path / 'x.png'), '-N']) == 1
    assert strip_main([str(src)]) == 0
    err = capsys.readouterr().err
    assert 'NovelAI 默认文件名' in err and '-N' in err                   # 未使用 -N：在标准错误输出警告与提示
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
    assert drive_tui(f'{q(str(d / "b.png"))}\n/q\n') == 0             # 重新启动后设置仍为开启，序号继续递增
    assert (d / f'{DAY}-0002.png').exists()
