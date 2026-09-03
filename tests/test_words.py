# -*- coding: utf-8 -*-
"""-w 改词：不剥，只换命中的词，两层 + 文件名；词表内置 / 编辑 / 列表；和 -t 互斥。"""
import json

import numpy as np
import pytest
from PIL import Image, PngImagePlugin
from test_roundtrip import META, embed, random_rgba

from nai_meta.core import summarize
from nai_meta.nai_inspect import choose_meta, inspect_file
from nai_meta.nai_strip import main as strip_main
from nai_meta.tui import run_tui


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'cfg'))
    monkeypatch.setenv('APPDATA', str(tmp_path / 'cfg'))
    return tmp_path / 'cfg' / 'nai-meta'


def loli_png(path):
    """提示词、角色、负面、Description 里都带 loli 的 NAI 图（文本块 + 隐写）。"""
    import gzip
    c = json.loads(META['Comment'])
    c['prompt'] = '1girl, loli, solo, Lolita fashion'
    c['uc'] = 'lowres, loli'
    c['v4_prompt']['caption']['base_caption'] = c['prompt']
    c['v4_prompt']['caption']['char_captions'][0]['char_caption'] = 'loli, red hair'
    c['signed_hash'] = 'AAAA'
    meta = dict(META, Description=c['prompt'], Comment=json.dumps(c))
    arr = random_rgba()
    embed(arr, 'alpha', 'stealth_pngcomp', gzip.compress(json.dumps(meta).encode()))
    info = PngImagePlugin.PngInfo()
    for k, v in meta.items():
        info.add_text(k, v)
    Image.fromarray(arr).save(path, pnginfo=info)
    return arr


def drive(argv, text):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text(text)
            return strip_main(argv)


def test_replace_word_in_both_layers_and_filename(tmp_path, cfg):
    src = tmp_path / '1girl, loli, solo s-42.png'
    arr = loli_png(src)
    assert strip_main([str(src), '-w', 'loli=1011']) == 0
    dst = tmp_path / '1girl, 1011, solo s-42_w.png'                 # 文件名里的词也换了
    assert dst.exists()
    rec = inspect_file(dst)
    assert rec['consistent'] is True
    s = summarize(choose_meta(rec, 'auto')[0])
    assert s['prompt'] == '1girl, 1011, solo, 1011ta fashion'         # 子串、不分大小写
    assert s['uc'] == 'lowres, 1011' and s['char_prompts'][0]['caption'] == '1011, red hair'
    assert s['seed'] == 42 and s['model']['hash'] == 'ABCD1234'        # 其余原样
    assert s['signed_hash'] is None                                    # 改过内容签名作废
    assert 'loli' not in rec['stealth']['raw'].lower()
    assert 'loli' not in json.dumps(rec['text_chunks']).lower()
    assert np.array_equal(np.asarray(Image.open(dst))[..., :3], arr[..., :3])


def test_builtin_preset_suffix_and_skips(tmp_path, cfg, capsys):
    src = tmp_path / 'a.png'
    loli_png(src)
    assert strip_main([str(src), '-w', 'discord']) == 0
    assert (tmp_path / 'a_discord.png').exists()                       # 一个预设 → 后缀用预设名
    assert 'loli' not in json.dumps(inspect_file(tmp_path / 'a_discord.png')['text_chunks']).lower()
    plain = tmp_path / 'p.png'                                         # 没有 NAI 元数据 → 不动
    Image.fromarray(random_rgba()).save(plain)
    assert strip_main([str(plain), '-w', 'discord']) == 0
    assert '没有 NAI 元数据' in capsys.readouterr().out and not (tmp_path / 'p_discord.png').exists()
    assert strip_main([str(src), '-w', 'zzz=1']) == 0                  # 没命中 → 不动
    assert '没命中' in capsys.readouterr().out
    assert strip_main([str(src), '-w', 'nope']) == 1                   # 没有的词表
    assert strip_main(['-w', 'list']) == 0
    assert 'discord' in capsys.readouterr().out
    assert strip_main([str(src), '-w', 'discord', '-t', 'x']) == 1     # 互斥


def test_edit_words_and_regex(tmp_path, cfg):
    assert drive(['-w', 'edit', 'mine'], 'loli=1011\n/lo+li/=L\n-loli\n:w\n') == 0
    txt = (cfg / 'words' / 'mine.txt').read_text('utf-8')
    assert '/lo+li/=L' in txt and 'loli=1011' not in txt
    src = tmp_path / 'a.png'
    loli_png(src)
    assert strip_main([str(src), '-w', 'mine']) == 0
    s = summarize(choose_meta(inspect_file(tmp_path / 'a_mine.png'), 'auto')[0])
    assert s['prompt'] == '1girl, L, solo, Lta fashion'


def test_tui_w_command(tmp_path, cfg):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    src = tmp_path / 'a.png'
    loli_png(src)
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text(f'/w discord\n{src}\n/q\n')
            assert run_tui([]) == 0
    assert (tmp_path / 'a_discord.png').exists()
