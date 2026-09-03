# -*- coding: utf-8 -*-
"""WebP 三种情况，以及官方格式载荷后的 FEC 段。"""
import gzip
import json

import numpy as np
from PIL import Image
from test_roundtrip import META, assert_clean, embed, random_rgba

from nai_meta.core import find_stealth, summarize
from nai_meta.nai_strip import main as strip_main
from nai_meta.nai_strip import webp_chunks


def _stealth_rgba(transparent=False):
    arr = random_rgba()
    if transparent:
        arr[-10:, -10:, 3] = 0            # 右下角，避开隐写占用的前几列
    embed(arr, 'alpha', 'stealth_pngcomp', gzip.compress(json.dumps(META).encode()))
    return arr


def _exif():
    ex = Image.Exif()
    ex[0x0131] = 'NovelAI'
    return ex.tobytes()


def test_webp_lossless_pixel_exact(tmp_path):
    src = tmp_path / 'a.webp'
    arr = _stealth_rgba()
    Image.fromarray(arr).save(src, lossless=True, exif=_exif(), xmp=b'<x/>')
    with Image.open(src) as im:
        assert find_stealth(im)
    assert strip_main([str(src)]) == 0
    dst = tmp_path / 'a_clean.webp'
    assert_clean(dst)
    out = np.asarray(Image.open(dst).convert('RGBA'))
    assert np.array_equal(out[..., :3], arr[..., :3]) and (out[..., 3] == 255).all()


def test_webp_lossy_opaque_alpha_container_strip(tmp_path):
    src = tmp_path / 'a.webp'
    Image.fromarray(_stealth_rgba()).save(src, quality=80, exif=_exif(), xmp=b'<x/>')
    raw = src.read_bytes()
    tags_in = [t for t, _ in webp_chunks(raw)]
    assert b'ALPH' in tags_in and b'EXIF' in tags_in
    with Image.open(src) as im:
        assert find_stealth(im)                   # 有损 WebP 的 alpha 是无损压缩的，隐写还在
    assert strip_main([str(src)]) == 0
    dst = tmp_path / 'a_clean.webp'
    assert_clean(dst)
    chunks_out = webp_chunks(dst.read_bytes())
    assert [t for t, _ in chunks_out] == [b'VP8 ']                 # 退回简单格式，只剩图像数据
    assert dict(chunks_out)[b'VP8 '] == dict(webp_chunks(raw))[b'VP8 ']   # RGB 数据一字节不动
    with Image.open(dst) as im:
        assert im.mode == 'RGB'


def test_webp_lossy_transparent_reencodes_with_note(tmp_path, capsys):
    src = tmp_path / 'a.webp'
    Image.fromarray(_stealth_rgba(transparent=True)).save(src, quality=80, exif=_exif())
    assert strip_main([str(src)]) == 0
    assert '真透明' in capsys.readouterr().out
    dst = tmp_path / 'a_clean.webp'
    assert_clean(dst)
    out = np.asarray(Image.open(dst).convert('RGBA'))
    assert (out[-10:, -10:, 3] == 0).all()


def _official_layout(arr, payload, fec=None):
    """官方格式：magic + 32 位载荷比特数 + 载荷 + 32 位 FEC 比特数（0xffffffff = 无）+ FEC。"""
    data = b'stealth_pngcomp' + (len(payload) * 8).to_bytes(4, 'big') + payload
    data += (len(fec) * 8).to_bytes(4, 'big') + fec if fec else b'\xff\xff\xff\xff'
    bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8))
    h = arr.shape[0]
    idx = np.arange(bits.size)
    arr[idx % h, idx // h, 3] = 0xFE | bits
    return bits.size


def test_fec_sentinel_counted_into_used_bits(tmp_path):
    arr = random_rgba()
    n = _official_layout(arr, gzip.compress(json.dumps(META).encode()))
    Image.fromarray(arr).save(tmp_path / 'a.png')
    with Image.open(tmp_path / 'a.png') as im:
        st = find_stealth(im)
    assert st.fec_bytes == 0 and st.used_bits == n


def test_fec_data_detected_and_wiped(tmp_path):
    arr = random_rgba()
    fec = b'\xab' * 40
    n = _official_layout(arr, gzip.compress(json.dumps(META).encode()), fec)
    arr[-10:, -10:, 3] = 0                    # 加点真透明，逼 strip 走「只擦占用位」而不是 alpha→255
    src = tmp_path / 'a.png'
    Image.fromarray(arr).save(src)
    with Image.open(src) as im:
        st = find_stealth(im)
    assert st.fec_bytes == 40 and st.used_bits == n
    assert strip_main([str(src)]) == 0
    dst = tmp_path / 'a_clean.png'
    assert_clean(dst)
    out = np.asarray(Image.open(dst))
    col_major = out[..., 3].T.reshape(-1)
    assert (col_major[:n] == 0xFE).all()      # 头 + 载荷 + FEC 段全清零
    assert (out[-10:, -10:, 3] == 0).all()


def _nai_style_exif():
    """NAI 的 WebP 下载：EXIF Software = 模型名+哈希，DocumentName = Title，ImageDescription = 提示词，UserComment = {"Comment": ...}"""
    ex = Image.Exif()
    ex[0x0131] = META['Source']                 # NAI 把模型名+哈希写在 Software 里
    ex[0x010d] = 'NovelAI generated image'
    ex[0x010e] = META['Description']
    ex.get_ifd(0x8769)[0x9286] = b'ASCII\x00\x00\x00' + json.dumps({'Comment': META['Comment']}).encode()
    return ex.tobytes()


def test_nai_webp_download_layout(tmp_path):
    from nai_meta.nai_inspect import inspect_file, choose_meta
    src = tmp_path / 'nai.webp'
    arr = _stealth_rgba()
    Image.fromarray(arr).save(src, lossless=True, exif=_nai_style_exif())
    rec = inspect_file(src)
    assert rec['text_meta'] is None and rec['stealth'] and rec['outer_layer'] == 'EXIF'
    assert rec['consistent'] is True                                # EXIF 层 vs 隐写层
    meta, src_name = choose_meta(rec, 'auto')
    assert src_name == 'EXIF'
    s = summarize(meta)
    assert s['model'] == {'name': 'NovelAI Diffusion V5', 'hash': 'ABCD1234', 'source': META['Source'], 'software': 'NovelAI'}
    assert s['seed'] == 42 and s['prompt'] == '1girl, solo'
    assert strip_main([str(src)]) == 0
    dst = tmp_path / 'nai_clean.webp'
    assert_clean(dst)
    out = np.asarray(Image.open(dst).convert('RGBA'))
    assert np.array_equal(out[..., :3], arr[..., :3]) and (out[..., 3] == 255).all()
