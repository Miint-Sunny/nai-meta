# -*- coding: utf-8 -*-
"""合成带隐写 / 文本块 / EXIF 的小图，nai-inspect 得读出来，nai-strip 得剥干净且像素不变。"""
import gzip
import json

import numpy as np
import pytest
from PIL import Image, PngImagePlugin

from nai_meta.core import find_stealth, scan_png
from nai_meta.nai_inspect import inspect_file
from nai_meta.nai_strip import main as strip_main

COMMENT = {'prompt': '1girl, solo', 'uc': 'lowres', 'seed': 42, 'steps': 28, 'scale': 5.0,
           'sampler': 'k_euler_ancestral', 'noise_schedule': 'karras', 'width': 64, 'height': 48,
           'v4_prompt': {'caption': {'base_caption': '1girl, solo',
                                     'char_captions': [{'char_caption': 'red hair', 'centers': [{'x': 0.5, 'y': 0.5}]}]},
                         'use_coords': False},
           'request_type': 'PromptGenerateRequest', 'skip_cfg_above_sigma': 19.0}
META = {'Description': '1girl, solo', 'Software': 'NovelAI', 'Source': 'NovelAI Diffusion V5 ABCD1234',
        'Generation time': '1.5', 'Comment': json.dumps(COMMENT)}


def embed(arr, channel, magic, data):
    """把 magic + 32 位长度 + data 按列优先写进最低位（与 NAI 写入顺序一致）。"""
    head = magic.encode() + (len(data) * 8).to_bytes(4, 'big') + data
    bits = np.unpackbits(np.frombuffer(head, dtype=np.uint8))
    h = arr.shape[0]
    idx = np.arange(bits.size)
    if channel == 'alpha':
        arr[idx % h, idx // h, 3] = (arr[idx % h, idx // h, 3] & 0xFE) | bits
    else:
        pix = idx // 3
        arr[pix % h, pix // h, idx % 3] = (arr[pix % h, pix // h, idx % 3] & 0xFE) | bits


def random_rgba(w=160, h=120, alpha=255):
    rng = np.random.default_rng(0)
    arr = rng.integers(0, 256, (h, w, 4), dtype=np.uint8)
    arr[:, :, 3] = alpha
    return arr


def nai_png(path, channel='alpha', compressed=True, with_text=True, transparent_corner=False):
    arr = random_rgba()
    if transparent_corner:
        arr[:10, :10, 3] = 0
    payload = json.dumps(META).encode()
    if channel == 'alpha':
        embed(arr, 'alpha', 'stealth_pngcomp' if compressed else 'stealth_pnginfo',
              gzip.compress(payload) if compressed else payload)
        im = Image.fromarray(arr)
    else:
        arr = np.ascontiguousarray(arr[:, :, :3])
        embed(arr, 'rgb', 'stealth_rgbcomp', gzip.compress(payload))
        im = Image.fromarray(arr)
    info = PngImagePlugin.PngInfo()
    if with_text:
        for k, v in META.items():
            info.add_text(k, v)
        info.add_text('Title', 'NovelAI generated image')
    im.save(path, pnginfo=info)
    return np.asarray(im).copy()


def assert_clean(path):
    scan = scan_png(path)
    if scan:
        assert not scan.texts and scan.exif is None
    with Image.open(path) as im:
        im.load()
        assert not im.getexif()
        assert not im.info.get('xmp') and not im.info.get('comment')
        if im.format != 'JPEG':
            assert find_stealth(im) is None


def test_inspect_reads_both_layers(tmp_path):
    p = tmp_path / 'a.png'
    nai_png(p)
    rec = inspect_file(p)
    assert set(rec['text_chunks']) == {'Description', 'Software', 'Source', 'Generation time', 'Comment', 'Title'}
    assert rec['stealth']['channel'] == 'alpha' and rec['stealth']['compressed']
    assert rec['consistent'] is True
    from nai_meta.core import summarize
    s = summarize(rec['text_meta'])
    assert s['seed'] == 42 and s['prompt'] == '1girl, solo'
    assert s['char_prompts'][0]['caption'] == 'red hair'
    assert s['type']['kind'] == 'txt2img' and s['toggles'] == {'Variety+': True}
    assert s['model'] == {'name': 'NovelAI Diffusion V5', 'hash': 'ABCD1234', 'source': 'NovelAI Diffusion V5 ABCD1234', 'software': 'NovelAI'}
    assert s['sampler_name'] == 'Euler Ancestral'


def test_inspect_flags_inconsistent_layers(tmp_path):
    p = tmp_path / 'a.png'
    arr = random_rgba()
    embed(arr, 'alpha', 'stealth_pngcomp', gzip.compress(json.dumps(META).encode()))
    info = PngImagePlugin.PngInfo()
    poisoned = dict(META, Comment=json.dumps(dict(COMMENT, prompt='杂鱼, 杂鱼, 杂鱼', seed=1)))
    for k, v in poisoned.items():
        info.add_text(k, v)
    Image.fromarray(arr).save(p, pnginfo=info)
    rec = inspect_file(p)
    assert rec['consistent'] is False
    assert set(rec['diff_keys']) == {'Comment.prompt', 'Comment.seed'}


@pytest.mark.parametrize('channel,compressed,transparent', [
    ('alpha', True, False), ('alpha', False, True), ('rgb', True, False)])
def test_strip_removes_everything_keeps_pixels(tmp_path, channel, compressed, transparent):
    src = tmp_path / 'a.png'
    before = nai_png(src, channel, compressed, transparent_corner=transparent)
    assert strip_main([str(src)]) == 0
    dst = tmp_path / 'a_clean.png'
    assert_clean(dst)
    after = np.asarray(Image.open(dst))
    if channel == 'alpha':
        assert np.array_equal(before[..., :3], after[..., :3])      # RGB 一位不差
        if transparent:
            assert (after[:10, :10, 3] == 0).all()                    # 真透明保留
        else:
            assert (after[..., 3] == 255).all()                       # 归回全不透明
    else:
        assert np.abs(before.astype(int) - after.astype(int)).max() <= 1   # 隐写就在 RGB 最低位，只能差 1


def test_strip_rgb_stealth_only_touches_lsb(tmp_path):
    src = tmp_path / 'a.png'
    before = nai_png(src, 'rgb', True, with_text=False)
    assert strip_main([str(src), '-o', str(tmp_path / 'b.png')]) == 0
    after = np.asarray(Image.open(tmp_path / 'b.png'))
    assert_clean(tmp_path / 'b.png')
    assert np.abs(before.astype(int) - after.astype(int)).max() <= 1


def test_strip_jpeg_lossless(tmp_path):
    src = tmp_path / 'a.jpg'
    ex = Image.Exif()
    ex[0x0131] = 'NovelAI'
    ex.get_ifd(0x8769)[0x9286] = b'ASCII\x00\x00\x00' + json.dumps(COMMENT).encode()
    Image.fromarray(random_rgba()[:, :, :3]).save(src, quality=90, exif=ex.tobytes(), comment=b'hi')
    rec = inspect_file(src)
    assert rec['exif']['Software'] == 'NovelAI'
    raw = src.read_bytes()
    assert strip_main([str(src), '-i']) == 0
    out = src.read_bytes()
    assert_clean(src)
    assert out[out.index(b'\xff\xda'):] == raw[raw.index(b'\xff\xda'):]   # 扫描数据原样


def test_strip_in_place_skips_clean_file(tmp_path, capsys):
    p = tmp_path / 'a.png'
    Image.fromarray(random_rgba()[:, :, :3]).save(p)
    raw = p.read_bytes()
    assert strip_main([str(p), '-i']) == 0
    assert p.read_bytes() == raw
    assert '不动' in capsys.readouterr().out


def test_no_overwrite_by_default(tmp_path):
    src = tmp_path / 'a.png'
    nai_png(src)
    assert strip_main([str(src)]) == 0
    assert strip_main([str(src)]) == 1            # a_clean.png 已存在 → 跳过并返回 1
    assert strip_main([str(src), '--overwrite']) == 0
