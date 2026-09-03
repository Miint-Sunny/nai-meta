# -*- coding: utf-8 -*-
"""TUI：拖进来的路径解析、文件夹 y/N、设置持久化；命令行文件夹确认。"""
import json
import shlex

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from test_roundtrip import nai_png

from pynai.nai_strip import main as strip_main
from pynai.tui import parse_paths, run_tui


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'cfg'))
    monkeypatch.setenv('APPDATA', str(tmp_path / 'cfg'))
    return tmp_path / 'cfg' / 'pynai'


def drive(text: str) -> int:
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text(text)
            return run_tui([])


def test_parse_dragged_paths(tmp_path):
    d = tmp_path / 'my pics'
    d.mkdir()
    (d / 'a b.png').write_bytes(b'')
    # macOS 拖拽：反斜杠转义空格，多个文件空格隔开
    got = parse_paths(f'{tmp_path}/my\\ pics/a\\ b.png {tmp_path}/my\\ pics')
    assert got == [d / 'a b.png', d]
    # 手敲的带空格路径，没转义
    assert parse_paths(str(d / 'a b.png')) == [d / 'a b.png']
    assert parse_paths(f'"{d}"') == [d]


def test_tui_drag_file_and_folder_with_confirm(tmp_path, cfg):
    d = tmp_path / 'my pics'
    d.mkdir()
    nai_png(d / 'a.png')
    nai_png(d / 'b.png')
    out = tmp_path / 'out'
    q = shlex.quote
    # 设输出目录 → 拖单文件 → 拖文件夹并拒绝 → 拖文件夹并同意 → 退出
    assert drive(f'/out {q(str(out))}\n{q(str(d / "a.png"))}\n{q(str(d))}\nn\n{q(str(d))}\ny\n/q\n') == 0
    assert (out / 'a.png').exists() and (out / 'b.png').exists()
    saved = json.loads((cfg / 'tui.json').read_text('utf-8'))
    assert saved['outdir'] == str(out) and saved['suffix'] == '_clean'
    # 再进来时记住了输出目录；/out - 恢复默认后写在原图旁边
    nai_png(d / 'c.png')
    assert drive(f'{q(str(d / "c.png"))}\n/out -\n{q(str(d / "c.png"))}\n/q\n') == 0
    assert (out / 'c.png').exists() and (d / 'c_clean.png').exists()


def test_tui_folder_declined_writes_nothing(tmp_path, cfg):
    d = tmp_path / 'pics'
    d.mkdir()
    nai_png(d / 'a.png')
    assert drive(f'{shlex.quote(str(d))}\n\n/q\n') == 0        # 直接回车 = 否
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
    # -y 不问；逐个点名的文件也不问
    nai_png(d / 'b.png')
    monkeypatch.setattr('builtins.input', lambda _prompt: pytest.fail('不该问'))
    assert strip_main([str(d), '-y', '--overwrite']) == 0
    assert strip_main([str(d / 'b.png'), '--overwrite']) == 0
