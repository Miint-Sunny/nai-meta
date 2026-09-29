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
        arr[-10:, -10:, 3] = 0            # 位于右下角，避开隐写数据占用的前几列
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
        assert find_stealth(im)                   # 有损 WebP 的 alpha 为无损压缩，隐写数据保留
    assert strip_main([str(src)]) == 0
    dst = tmp_path / 'a_clean.webp'
    assert_clean(dst)
    chunks_out = webp_chunks(dst.read_bytes())
    assert [t for t, _ in chunks_out] == [b'VP8 ']                 # 改为简单格式，仅保留图像数据
    assert dict(chunks_out)[b'VP8 '] == dict(webp_chunks(raw))[b'VP8 ']   # RGB 数据逐字节不变
    with Image.open(dst) as im:
        assert im.mode == 'RGB'


def test_webp_lossy_transparent_reencodes_with_note(tmp_path, capsys):
    src = tmp_path / 'a.webp'
    Image.fromarray(_stealth_rgba(transparent=True)).save(src, quality=80, exif=_exif())
    assert strip_main([str(src)]) == 0
    assert '含透明像素' in capsys.readouterr().out
    dst = tmp_path / 'a_clean.webp'
    assert_clean(dst)
    out = np.asarray(Image.open(dst).convert('RGBA'))
    assert (out[-10:, -10:, 3] == 0).all()


def _official_layout(arr, payload, fec=None):
    """按官方格式写入：magic、32 位数据长度（比特）、数据、32 位 FEC 长度（0xffffffff 表示无）、FEC。"""
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
    arr[-10:, -10:, 3] = 0                    # 加入透明像素：图像并非完全不透明时，隐写区域同样须被清除
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
    assert (col_major[:n] == 0xFF).all()      # 头部、数据与 FEC 段中的 254、255 均恢复为 255
    assert (out[-10:, -10:, 3] == 0).all()    # 透明像素保持原值


def test_stealth_area_back_to_255_despite_odd_edge_pixels(tmp_path):
    """隐写区域的 alpha 恢复为 255，不受区域外非不透明像素的影响。

    NovelAI 的 WebP 边缘常有少量 alpha 为 239、251 的像素，整幅图像因此不属于完全不透明。
    隐写区域须恢复为 255，否则会留下一段 alpha 为 254 的区域，表明隐写数据曾被清除；区域外的像素保持原值。
    """
    arr = random_rgba()
    arr[..., 3] = 255
    embed(arr, 'alpha', 'stealth_pngcomp', gzip.compress(json.dumps(META).encode()))
    h, w = arr.shape[:2]
    arr[h - 1, w - 1, 3], arr[h - 2, w // 2, 3], arr[1, 0, 3] = 239, 251, 239   # 最后一个像素位于隐写区域内
    src = tmp_path / 'a.webp'
    Image.fromarray(arr).save(src, lossless=True)
    assert strip_main([str(src)]) == 0
    out = np.asarray(Image.open(tmp_path / 'a_clean.webp').convert('RGBA'))
    assert np.array_equal(out[..., :3], arr[..., :3])
    a = out[..., 3].copy()
    assert a[h - 1, w - 1] == 239 and a[h - 2, w // 2] == 251 and a[1, 0] == 238   # 隐写区域内 alpha < 254 的像素只清除最低位
    a[h - 1, w - 1] = a[h - 2, w // 2] = a[1, 0] = 255
    assert (a == 255).all()                   # 其余像素均为 255


def _nai_style_exif():
    """构造 NovelAI WebP 下载的 EXIF：Software 为模型名与哈希，DocumentName 为 Title，ImageDescription 为提示词，UserComment 为 JSON。"""
    ex = Image.Exif()
    ex[0x0131] = META['Source']                 # NovelAI 将模型名与哈希写入 Software
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
    assert rec['consistent'] is True                                # 比对 EXIF 与隐写层
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


def test_nai_webp_description_leading_nuls(tmp_path):
    """Description 开头的 NUL 字节不计为两层差异。

    提示词含中文时，NovelAI 的 WebP 在 ImageDescription 与 UserComment 内 Description 的开头写入 4 个 NUL，
    隐写层中没有。读取时须去掉 NUL，既不报告两层不一致，也不出现在 -p 的输出中。
    """
    from nai_meta.nai_inspect import inspect_file
    src = tmp_path / 'nai.webp'
    nul = '\x00\x00\x00\x00' + META['Description']
    ex = Image.Exif()
    ex[0x0131] = META['Source']
    ex[0x010d] = 'NovelAI generated image'
    ex[0x010e] = nul
    ex.get_ifd(0x8769)[0x9286] = b'ASCII\x00\x00\x00' + json.dumps({**META, 'Description': nul}).encode()
    Image.fromarray(_stealth_rgba()).save(src, lossless=True, exif=ex.tobytes())
    assert b'\x00\x00\x00\x00' + META['Description'].encode() in src.read_bytes()   # 确认测试数据包含 NUL
    rec = inspect_file(src)
    assert rec['exif_meta']['Description'] == META['Description'] and rec['consistent'] is True
