# -*- coding: utf-8 -*-
"""投毒：-t '内容' 每块同一段；预设 / 编辑 / 模板；--set；四种格式。"""
import json
import sys

import numpy as np
import pytest
from PIL import Image
from test_roundtrip import nai_png, random_rgba

from nai_meta.core import NAI_TEXT_KEYS, STEALTH_KEYS, summarize
from nai_meta.nai_inspect import choose_meta, inspect_file
from nai_meta.nai_strip import main as strip_main
from nai_meta.nai_strip import webp_chunks


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'cfg'))
    monkeypatch.setenv('APPDATA', str(tmp_path / 'cfg'))
    return tmp_path / 'cfg' / 'nai-meta'


def test_text_fills_every_chunk_and_stealth(tmp_path, cfg):
    src = tmp_path / 'a.png'
    nai_png(src)
    assert strip_main([str(src), '-t', '杂鱼']) == 0
    dst = tmp_path / 'a_poison.png'
    rec = inspect_file(dst)
    assert rec['text_chunks'] == {k: '杂鱼' for k in NAI_TEXT_KEYS}
    st = rec['stealth']['meta']
    assert {k: st[k] for k in STEALTH_KEYS} == {k: '杂鱼' for k in STEALTH_KEYS}
    assert np.array_equal(np.asarray(Image.open(dst))[..., :3], np.asarray(Image.open(src))[..., :3])
    assert 'ABCD1234' not in dst.read_bytes().decode('latin-1')          # 原来的模型哈希等一点不剩


def test_text_with_set_makes_comment_json(tmp_path, cfg):
    src = tmp_path / 'b.png'
    Image.fromarray(random_rgba()[:, :, :3]).save(src)                   # RGB、无元数据
    assert strip_main([str(src), '-t', 'x', '--set', 'seed=7', '--set', 'Title=t']) == 0
    dst = tmp_path / 'b_poison.png'
    rec = inspect_file(dst)
    assert rec['mode'] == 'RGBA' and rec['text_chunks']['Title'] == 't' and rec['text_chunks']['Software'] == 'x'
    meta, _ = choose_meta(rec, 'auto')
    s = summarize(meta)
    assert s['prompt'] == 'x' and s['uc'] == 'x' and s['seed'] == 7
    assert rec['consistent'] is True


def test_set_alone_keeps_original_and_overrides(tmp_path, cfg):
    src = tmp_path / 'c.png'
    nai_png(src)
    assert strip_main([str(src), '--set', 'seed=9', '--set', 'uc=bad hands']) == 0
    rec = inspect_file(tmp_path / 'c_poison.png')
    s = summarize(choose_meta(rec, 'auto')[0])
    assert s['prompt'] == '1girl, solo' and s['seed'] == 9 and s['uc'] == 'bad hands'
    assert s['signed_hash'] is None and rec['consistent'] is True


def test_preset_edit_list_use_and_template(tmp_path, cfg, monkeypatch):
    ed = tmp_path / 'ed.py'                                              # 假编辑器：改 prompt，seed 置空
    ed.write_text('import json,sys\np=sys.argv[1]\nd=json.load(open(p,encoding="utf-8"))\n'
                  'd["Comment"]["prompt"]="preset junk"\nd["Comment"]["seed"]=None\n'
                  'json.dump(d,open(p,"w",encoding="utf-8"),ensure_ascii=False)\n')
    monkeypatch.setenv('EDITOR', f'{sys.executable} {ed}')
    assert strip_main(['-t', 'edit:1']) == 0                              # 不带图片：只建预设
    preset = cfg / 'presets' / '1.json'
    assert json.loads(preset.read_text('utf-8'))['Comment']['prompt'] == 'preset junk'
    src = tmp_path / 'd.png'
    nai_png(src)
    assert strip_main([str(src), '-t', '1']) == 0
    rec = inspect_file(tmp_path / 'd_poison.png')
    s = summarize(choose_meta(rec, 'auto')[0])
    assert s['prompt'] == 'preset junk' and s['seed'] not in (None, 42) and (s['width'], s['height']) == (160, 120)
    assert rec['consistent'] is True
    assert strip_main([str(src), '-t', '9']) == 1                         # 没有的预设
    assert strip_main(['-t', 'list']) == 0
    assert strip_main([str(src), '-t', f'@{preset}', '-o', str(tmp_path / 'e.png')]) == 0
    assert summarize(choose_meta(inspect_file(tmp_path / 'e.png'), 'auto')[0])['prompt'] == 'preset junk'


def test_poison_webp_and_jpeg(tmp_path, cfg):
    arr = random_rgba()
    w = tmp_path / 'a.webp'                                              # 无损 WebP：EXIF + 隐写
    Image.fromarray(arr).save(w, lossless=True)
    assert strip_main([str(w), '-t', 'x']) == 0
    rec = inspect_file(tmp_path / 'a_poison.webp')
    assert rec['exif_meta']['Description'] == 'x' and rec['stealth']['meta']['Description'] == 'x'
    j = tmp_path / 'a.jpg'                                               # JPEG：只有 EXIF，扫描数据不动
    Image.fromarray(arr[:, :, :3]).save(j, quality=90)
    assert strip_main([str(j), '-t', 'y']) == 0
    out = tmp_path / 'a_poison.jpg'
    assert inspect_file(out)['exif_meta']['Description'] == 'y'
    raw, new = j.read_bytes(), out.read_bytes()
    assert new[new.index(b'\xff\xda'):] == raw[raw.index(b'\xff\xda'):]
    lw = tmp_path / 'l.webp'                                             # 有损 WebP：容器层写 EXIF，VP8 数据不动
    Image.fromarray(arr).save(lw, quality=80)
    assert strip_main([str(lw), '-t', 'z']) == 0
    out = tmp_path / 'l_poison.webp'
    rec = inspect_file(out)
    assert rec['exif_meta']['Description'] == 'z' and rec['stealth'] is None
    assert [t for t, _ in webp_chunks(out.read_bytes())] == [b'VP8X', b'VP8 ', b'EXIF']
    assert dict(webp_chunks(out.read_bytes()))[b'VP8 '] == dict(webp_chunks(lw.read_bytes()))[b'VP8 ']
