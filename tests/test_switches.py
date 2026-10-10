# -*- coding: utf-8 -*-
"""开关与原文件处理：settings.json 的读写与迁移、nais config、命令行与开关的优先级、
帮助中的开关状态、移到废纸篓（--trash）与永久删除（--delete）、同一路径保护、交互模式的保存行为。"""
import json
import os
import re

import pytest
from test_roundtrip import nai_png
from test_tui import drive as drive_tui

import nai_meta.nai_strip as ns
import nai_meta.settings as st
import nai_meta.tui as tui
from nai_meta.nai_inspect import inspect_file
from nai_meta.nai_strip import main as strip_main

DAY = '20261011'


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'cfg'))
    monkeypatch.setenv('APPDATA', str(tmp_path / 'cfg'))
    monkeypatch.setattr(ns, 'today', lambda: DAY)
    return tmp_path / 'cfg' / 'nai-meta'


@pytest.fixture
def trash(tmp_path, monkeypatch):
    """以临时目录代替系统废纸篓，记录被移入的文件。"""
    bin_dir = tmp_path / 'Trash'
    bin_dir.mkdir()
    moved = []

    def move(path):
        target = bin_dir / os.path.basename(path)
        os.replace(path, target)
        moved.append(target)
        return str(target)

    for mod in (ns, st, tui):
        monkeypatch.setattr(mod, 'trash_supported', lambda: True)
    monkeypatch.setattr(ns, 'move_to_trash', move)
    return moved


@pytest.fixture
def pics(tmp_path):
    d = tmp_path / 'pics'
    d.mkdir()
    return d


def clean(p):
    rec = inspect_file(p)
    return not rec['text_chunks'] and rec['stealth'] is None


def help_text(capsys, argv=('-h',)) -> str:
    with pytest.raises(SystemExit) as e:
        strip_main(list(argv))
    assert e.value.code == 0
    return re.sub(r'\s+', '', capsys.readouterr().out)        # 去掉折行，便于查找


# ---------------------------------------------------------------- 设置文件与 nais config
def test_config_command(cfg, capsys, trash):
    assert strip_main(['config']) == 0
    out = capsys.readouterr().out
    assert 'trash' in out and 'outdir' in out and '未设置' in out
    assert strip_main(['config', 'trash', 'on']) == 0
    assert strip_main(['config', 'outdir', '~/clean']) == 0
    assert strip_main(['config', 'rename', 'yes']) == 0
    saved = json.loads((cfg / 'settings.json').read_text('utf-8'))
    assert saved['trash'] is True and saved['outdir'] == '~/clean' and saved['rename'] is True
    assert st.load()['outdir'] == os.path.expanduser('~/clean')          # 读取时展开 ~
    capsys.readouterr()
    assert strip_main(['config', 'trash']) == 0
    assert capsys.readouterr().out.strip() == 'trash：开'
    assert strip_main(['config', 'outdir', '-']) == 0                    # - 恢复默认值
    assert json.loads((cfg / 'settings.json').read_text('utf-8'))['outdir'] is None
    assert strip_main(['config', 'trash', 'maybe']) == 2                 # 无效取值
    with pytest.raises(SystemExit) as e:                                 # 无效名称：argparse 报错
        strip_main(['config', 'overwrite', 'on'])
    assert e.value.code == 2
    assert strip_main(['config', '--reset']) == 0
    assert st.load() == {sw.attr: sw.default for sw in st.SWITCHES}


def test_legacy_tui_settings_are_migrated(cfg):
    cfg.mkdir(parents=True)
    (cfg / 'tui.json').write_text(json.dumps(
        {'outdir': '/x/out', 'suffix': '_clean', 'rename': True, 'overwrite': True, 'drop_alpha': False}), 'utf-8')
    values = st.load()
    assert values['outdir'] == '/x/out' and values['rename'] is True
    assert values['suffix'] is None                                     # 旧版的 _clean 即默认值，视为未设置
    saved = json.loads((cfg / 'settings.json').read_text('utf-8'))
    assert 'overwrite' not in saved and saved['rename'] is True         # overwrite 不再是开关


def test_help_shows_switch_state(cfg, capsys, trash):
    text = help_text(capsys)
    assert 'macOS（开关，当前：关）' in text and '递归处理子目录（开关，当前：关）' in text
    assert '只能逐次指定，不能设为开关' in text and 'naisconfigKEYVALUE' in text
    assert strip_main(['config', 'trash', 'on']) == 0
    assert strip_main(['config', 'outdir', '/tmp/x']) == 0
    capsys.readouterr()
    text = help_text(capsys)
    assert 'macOS（开关，当前：开）' in text and '（开关，当前：/tmp/x）' in text


# ---------------------------------------------------------------- 开关与命令行的优先级
def test_switches_apply_and_explicit_options_win(cfg, capsys, pics):
    nai_png(pics / 'a.png')
    nai_png(pics / 'b.png')
    assert strip_main(['config', 'rename', 'on']) == 0
    capsys.readouterr()
    assert strip_main([str(pics / 'a.png')]) == 0
    assert clean(pics / f'{DAY}-0001.png')
    assert '已启用的开关：按“日期-序号”重命名' in capsys.readouterr().err   # 运行前列出取自设置文件的开关
    assert strip_main([str(pics / 'b.png'), '--no-rename']) == 0         # 本次关闭
    assert (pics / 'b_clean.png').exists()
    assert '已启用的开关' not in capsys.readouterr().err
    assert strip_main([str(pics / 'b.png'), '-o', str(pics / 'o.png')]) == 0   # -o 指定了文件名，不使用重命名开关
    assert (pics / 'o.png').exists()


def test_outdir_and_suffix_switches(cfg, pics, tmp_path):
    nai_png(pics / 'a.png')
    out = tmp_path / 'out'
    assert strip_main(['config', 'outdir', str(out)]) == 0
    assert strip_main(['config', 'suffix', '_c']) == 0
    assert strip_main([str(pics / 'a.png')]) == 0
    assert (out / 'a.png').exists()                                      # 输出目录模式保留原文件名
    assert strip_main([str(pics / 'a.png'), '-d', '-']) == 0             # -d -：本次输出到源文件所在目录
    assert (pics / 'a_c.png').exists()
    assert strip_main([str(pics / 'a.png'), '-d', '-', '--suffix', '-']) == 0   # --suffix -：本次使用默认后缀
    assert (pics / 'a_clean.png').exists()


# ---------------------------------------------------------------- 原文件：废纸篓与永久删除
def test_trash_after_success(cfg, pics, trash, capsys):
    src = pics / 'a.png'
    nai_png(src)
    assert strip_main([str(src), '--trash', '-s']) == 0                  # 试运行不移动
    assert src.exists() and '原文件将移到废纸篓' in capsys.readouterr().out
    assert strip_main([str(src), '--trash']) == 0
    assert not src.exists() and [p.name for p in trash] == ['a.png']
    assert clean(pics / 'a_clean.png')
    assert '原文件已移到废纸篓' in capsys.readouterr().out
    assert not list(pics.glob('.*'))                                     # 没有残留的临时文件


def test_trash_switch_with_in_place(cfg, pics, trash):
    a, b = pics / 'a.png', pics / 'b.png'
    nai_png(a)
    nai_png(b)
    assert strip_main(['config', 'trash', 'on']) == 0
    assert strip_main([str(a), '-i']) == 0                               # 原文件先进废纸篓，结果写回原路径
    assert clean(a) and not clean(trash[0]) and trash[0].name == 'a.png'
    assert strip_main([str(b), '-i', '-n']) == 0                         # 原地重命名：原文件进废纸篓
    assert not b.exists() and clean(pics / f'{DAY}-0001.png') and trash[1].name == 'b.png'
    c = pics / 'c.png'
    nai_png(c)
    assert strip_main([str(c), '-i', '--no-trash']) == 0                 # 本次关闭：直接覆盖
    assert clean(c) and len(trash) == 2


def test_delete_is_explicit_and_wins(cfg, pics, trash, capsys):
    a = pics / 'a.png'
    nai_png(a)
    assert strip_main(['config', 'trash', 'on']) == 0
    assert strip_main([str(a), '--delete']) == 0                         # 明确指定 --delete：永久删除，不进废纸篓
    assert not a.exists() and not trash and clean(pics / 'a_clean.png')
    assert '原文件已删除' in capsys.readouterr().out
    nai_png(a)
    assert strip_main([str(a), '--trash', '--delete']) == 1              # 两者同时明确指定：报错
    assert a.exists()


def test_original_kept_when_verification_or_trash_fails(cfg, pics, trash, monkeypatch, capsys):
    a = pics / 'a.png'
    nai_png(a)
    raw = a.read_bytes()
    monkeypatch.setattr(ns, 'verify', lambda p: ['输出文件仍含 测试'])
    assert strip_main([str(a), '--trash']) == 1                          # 验证失败：原文件保留
    assert a.exists() and not trash
    assert strip_main([str(a), '-i', '--trash']) == 1                    # 原地修改时验证失败：源文件不变
    assert a.read_bytes() == raw and '源文件未修改' in capsys.readouterr().out
    monkeypatch.undo()

    def fail(path):
        raise OSError('测试：无法移动')
    for mod in (ns, st, tui):
        monkeypatch.setattr(mod, 'trash_supported', lambda: True)
    monkeypatch.setattr(ns, 'move_to_trash', fail)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(cfg.parent))
    assert strip_main([str(a), '-i', '--trash']) == 1                    # 移到废纸篓失败：源文件不变
    assert a.read_bytes() == raw
    assert strip_main([str(a), '--trash', '--overwrite']) == 1           # 输出已写出，但原文件保留
    assert a.exists() and (pics / 'a_clean.png').exists()
    assert not list(pics.glob('.*'))


def test_output_path_equal_to_source_is_refused(cfg, pics):
    a = pics / 'a.png'
    nai_png(a)
    raw = a.read_bytes()
    assert strip_main([str(a), '-d', str(pics)]) == 0                    # 输出目录即源目录：跳过，不覆盖
    assert a.read_bytes() == raw
    assert strip_main([str(a), '-o', str(pics / '.' / 'a.png'), '--overwrite']) == 0
    assert a.read_bytes() == raw


def test_trash_unsupported_platform(cfg, pics, monkeypatch, capsys):
    for mod in (ns, st, tui):
        monkeypatch.setattr(mod, 'trash_supported', lambda: False)
    a = pics / 'a.png'
    nai_png(a)
    assert strip_main([str(a), '--trash']) == 1
    assert '不支持' in capsys.readouterr().err and a.exists() and not (pics / 'a_clean.png').exists()
    assert strip_main(['config', 'trash', 'on']) == 1


# ---------------------------------------------------------------- 交互模式
def test_tui_switches_are_shared_and_session_settings_are_not(cfg, pics, trash):
    nai_png(pics / 'a.png')
    q = str(pics / 'a.png')
    assert drive_tui('/trash\n/ow\n/dry\n/q\n') == 0
    saved = json.loads((cfg / 'settings.json').read_text('utf-8'))
    assert saved['trash'] is True and 'overwrite' not in saved and 'dry_run' not in saved
    assert drive_tui(f'{q}\n/q\n') == 0                                  # 重新启动后废纸篓开关仍开启
    assert not (pics / 'a.png').exists() and trash[0].name == 'a.png'
    nai_png(pics / 'b.png')
    assert strip_main([str(pics / 'b.png')]) == 0                        # 命令行使用同一开关
    assert trash[1].name == 'b.png'


# ---------------------------------------------------------------- 系统废纸篓（需手动开启）
@pytest.mark.skipif(os.environ.get('NAI_META_TEST_REAL_TRASH') != '1',
                    reason='会向系统废纸篓移入一个测试文件；设置 NAI_META_TEST_REAL_TRASH=1 后运行')
def test_real_system_trash(cfg, pics):
    from nai_meta.trash import move_to_trash, supported
    if not supported():
        pytest.skip('当前系统不支持移到废纸篓')
    f = pics / 'nai-meta-trash-test.png'
    nai_png(f)
    result = move_to_trash(f)
    assert not f.exists()
    assert result is None or os.path.basename(result).startswith('nai-meta-trash-test')
