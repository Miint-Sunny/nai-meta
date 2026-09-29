# -*- coding: utf-8 -*-
"""交互模式：拖入路径的解析、目录确认、设置保存；命令行处理目录时的确认。"""
import json
import shlex

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from test_roundtrip import nai_png

from nai_meta.nai_strip import main as strip_main
from nai_meta.tui import parse_paths, run_tui


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'cfg'))
    monkeypatch.setenv('APPDATA', str(tmp_path / 'cfg'))
    return tmp_path / 'cfg' / 'nai-meta'


def drive(text: str) -> int:
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text(text)
            return run_tui([])


def test_parse_dragged_paths(tmp_path):
    d = tmp_path / 'my pics'
    d.mkdir()
    (d / 'a b.png').write_bytes(b'')
    # macOS 拖入：空格以反斜杠转义，多个路径以空格分隔
    got = parse_paths(f'{tmp_path}/my\\ pics/a\\ b.png {tmp_path}/my\\ pics')
    assert got == [d / 'a b.png', d]
    # 手动输入、未转义的含空格路径
    assert parse_paths(str(d / 'a b.png')) == [d / 'a b.png']
    assert parse_paths(f'"{d}"') == [d]


def test_tui_drag_file_and_folder_with_confirm(tmp_path, cfg):
    d = tmp_path / 'my pics'
    d.mkdir()
    nai_png(d / 'a.png')
    nai_png(d / 'b.png')
    out = tmp_path / 'out'
    q = shlex.quote
    # 设置输出目录，拖入单个文件，拖入目录并拒绝，再次拖入目录并确认，然后退出
    assert drive(f'/out {q(str(out))}\n{q(str(d / "a.png"))}\n{q(str(d))}\nn\n{q(str(d))}\ny\n/q\n') == 0
    assert (out / 'a.png').exists() and (out / 'b.png').exists()
    saved = json.loads((cfg / 'tui.json').read_text('utf-8'))
    assert saved['outdir'] == str(out) and saved['suffix'] is None      # 未设置后缀时保存为 None，由是否写入伪造元数据决定默认后缀
    # 重新启动后保留输出目录；/out - 恢复为输出到源文件所在目录
    nai_png(d / 'c.png')
    assert drive(f'{q(str(d / "c.png"))}\n/out -\n{q(str(d / "c.png"))}\n/q\n') == 0
    assert (out / 'c.png').exists() and (d / 'c_clean.png').exists()


def test_tui_folder_declined_writes_nothing(tmp_path, cfg):
    d = tmp_path / 'pics'
    d.mkdir()
    nai_png(d / 'a.png')
    assert drive(f'{shlex.quote(str(d))}\n\n/q\n') == 0        # 直接按回车视为拒绝
    assert not (d / 'a_clean.png').exists()


def test_cli_folder_asks_and_respects_answer(tmp_path, monkeypatch, capsys):
    d = tmp_path / 'pics'
    d.mkdir()
    nai_png(d / 'a.png')
    monkeypatch.setattr('builtins.input', lambda _prompt: 'n')
    assert strip_main([str(d)]) == 1
    assert '已取消' in capsys.readouterr().out
    assert not (d / 'a_clean.png').exists()
    monkeypatch.setattr('builtins.input', lambda _prompt: 'y')
    assert strip_main([str(d)]) == 0
    assert (d / 'a_clean.png').exists()
    # 使用 -y 或逐个指定文件时不请求确认
    nai_png(d / 'b.png')
    monkeypatch.setattr('builtins.input', lambda _prompt: pytest.fail('不该问'))
    assert strip_main([str(d), '-y', '--overwrite']) == 0
    assert strip_main([str(d / 'b.png'), '--overwrite']) == 0


def test_absolute_path_without_spaces_is_not_a_command(tmp_path, cfg):
    """以 / 开头的绝对路径（如 macOS 的 /Users/...）不能被识别为命令。"""
    src = tmp_path / 'a.png'
    nai_png(src)
    assert ' ' not in str(src)
    assert drive(f'{src}\n/q\n') == 0
    assert (tmp_path / 'a_clean.png').exists()
