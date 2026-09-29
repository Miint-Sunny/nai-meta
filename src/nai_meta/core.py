# -*- coding: utf-8 -*-
"""nai-meta 的共用逻辑：PNG 块扫描、LSB 隐写的读取/清除/写入、NovelAI 元数据整理。

NovelAI 生成的图片包含两层相同的元数据：

1. 明文层。PNG 使用文本块（tEXt）：Title、Description、Software、Source、Generation time、Comment；
   WebP 使用 EXIF。Comment 为 JSON 字符串，包含全部生成参数（prompt、uc、seed、sampler、v4_prompt 等）。
   该层可被 exiftool 等通用工具读取，也最容易被转发平台移除。
2. LSB 隐写层（stealth pnginfo）。将 {Description, Software, Source, Generation time, Comment}
   序列化为 JSON 并以 gzip 压缩，按列优先顺序写入 alpha 通道各像素的最低位。
   novelai.net/inspect 读取的是该层；图像未经重新编码（转换格式、缩放、有损压缩）时该层保持完整。

   比特流布局：[magic，15 字节 ASCII][数据长度，32 位大端，单位为比特][数据][FEC 长度，32 位]
   magic：stealth_pnginfo / stealth_pngcomp（alpha 通道，后者经 gzip 压缩）；
          stealth_rgbinfo / stealth_rgbcomp（RGB 三通道，A1111 插件在无 alpha 时使用）。
   NovelAI 仅写入 stealth_pngcomp。

格式定义见 https://github.com/NovelAI/novelai-image-metadata 。
"""
from __future__ import annotations

import copy
import glob
import gzip
import json
import os
import random
import re
import struct
import sys
import zlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

IMG_EXTS = {'.png', '.jpg', '.jpeg', '.webp'}

# ---------------------------------------------------------------- PNG 块
PNG_SIG = b'\x89PNG\r\n\x1a\n'
TEXT_TYPES = (b'tEXt', b'iTXt', b'zTXt')
# nai-strip 移除的 PNG 块：文本、EXIF、修改时间
META_TYPES = TEXT_TYPES + (b'eXIf', b'tIME')
COLOR_TYPES = {0: 'Gray', 2: 'RGB', 3: 'Palette', 4: 'Gray+Alpha', 6: 'RGBA'}


@dataclass
class PngScan:
    width: int = 0
    height: int = 0
    bit_depth: int = 0
    color_type: int = 0
    interlace: int = 0
    texts: dict = field(default_factory=dict)      # 关键字 → 文本
    exif: bytes | None = None                      # eXIf 块的原始字节
    chunks: Counter = field(default_factory=Counter)

    @property
    def meta_chunk_names(self) -> list[str]:
        return [t for t in self.chunks if t.encode('latin-1') in META_TYPES]


def _txt(b: bytes) -> str:
    """解码文本块内容。PNG 规范规定 tEXt 为 Latin-1，NovelAI 与多数工具实际写入 UTF-8，故优先按 UTF-8 解码。"""
    try:
        return b.decode('utf-8')
    except UnicodeDecodeError:
        return b.decode('latin-1')


def _decode_text_chunk(typ: bytes, data: bytes) -> tuple[str, str]:
    kw, _, rest = data.partition(b'\x00')
    key = kw.decode('latin-1', 'replace')
    try:
        if typ == b'tEXt':
            return key, _txt(rest)
        if typ == b'zTXt':                       # 1 字节压缩方法，其后为 zlib 数据流
            return key, _txt(zlib.decompress(rest[1:]))
        flag, rest = rest[0], rest[2:]           # iTXt：压缩标志、压缩方法、语言标签、翻译后的关键字、正文
        _lang, _, rest = rest.partition(b'\x00')
        _trans, _, text = rest.partition(b'\x00')
        if flag == 1:
            text = zlib.decompress(text)
        return key, _txt(text)
    except Exception as e:                       # 单个损坏的块不影响读取其余内容
        return key, f'<无法解码：{e}>'


def scan_png(path) -> PngScan | None:
    """逐块扫描 PNG 文件，读取 IHDR、文本块和 eXIf，跳过图像数据。

    Returns:
        扫描结果；文件不是 PNG 时返回 None。
    """
    with open(path, 'rb') as fh:
        if fh.read(8) != PNG_SIG:
            return None
        scan = PngScan()
        while True:
            hdr = fh.read(8)
            if len(hdr) < 8:
                break
            ln, typ = struct.unpack('>I4s', hdr)
            name = typ.decode('latin-1', 'replace')
            scan.chunks[name] += 1
            if typ == b'IHDR':
                d = fh.read(ln)
                (scan.width, scan.height, scan.bit_depth, scan.color_type,
                 _c, _f, scan.interlace) = struct.unpack('>IIBBBBB', d[:13])
            elif typ in TEXT_TYPES:
                k, v = _decode_text_chunk(typ, fh.read(ln))
                if k in scan.texts:              # 关键字重复时追加序号，保留全部内容
                    k = f'{k}#{scan.chunks[name]}'
                scan.texts[k] = v
            elif typ == b'eXIf':
                scan.exif = fh.read(ln)
            elif typ == b'IEND':
                break
            else:
                fh.seek(ln, 1)
            fh.seek(4, 1)                        # CRC
        return scan


# ---------------------------------------------------------------- 元数据整理
def expand_comment(meta: dict) -> dict:
    """将 Comment 字段从 JSON 字符串解析为 dict。就地修改 meta 并返回；无法解析时保持原样。"""
    c = meta.get('Comment')
    if isinstance(c, str):
        try:
            d = json.loads(c)
            if isinstance(d, dict):
                meta['Comment'] = d
        except json.JSONDecodeError:
            pass
    return meta


def is_nai(meta: dict | None) -> bool:
    """判断 meta 是否为 NovelAI 元数据。"""
    if not meta:
        return False
    if 'NovelAI' in str(meta.get('Software', '')) or 'NovelAI' in str(meta.get('Source', '')):
        return True
    c = meta.get('Comment')
    return isinstance(c, dict) and 'prompt' in c


def meta_from_text(s: str) -> dict | None:
    """从字符串（隐写正文、EXIF UserComment、JPEG 注释）中解析 NovelAI 元数据。

    也接受 A1111 格式的 parameters 文本。无法解析时返回 None。
    """
    try:
        d = json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return parse_a1111(s) if isinstance(s, str) else None
    if not isinstance(d, dict):
        return None
    if 'Comment' in d:
        return expand_comment(d)
    if 'prompt' in d:                            # 仅包含 Comment 内层对象的情况
        return {'Comment': d}
    return None


# 采样器标识 → NovelAI 界面显示名
SAMPLERS = {
    'k_euler': 'Euler', 'k_euler_ancestral': 'Euler Ancestral',
    'k_dpmpp_2s_ancestral': 'DPM++ 2S Ancestral', 'k_dpmpp_2m': 'DPM++ 2M',
    'k_dpmpp_2m_sde': 'DPM++ 2M SDE', 'k_dpmpp_sde': 'DPM++ SDE',
    'k_dpm_2': 'DPM2', 'k_dpm_2_ancestral': 'DPM2 Ancestral', 'k_dpm_fast': 'DPM Fast',
    'k_dpm_adaptive': 'DPM Adaptive', 'k_lms': 'LMS', 'k_heun': 'Heun',
    'ddim': 'DDIM', 'ddim_v3': 'DDIM', 'plms': 'PLMS', 'nai_smea': 'SMEA', 'nai_smea_dyn': 'SMEA DYN',
}
REQUEST_TYPES = {
    'PromptGenerateRequest': ('txt2img', '文生图'),
    'Img2ImgRequest': ('img2img', '图生图 i2i'),
    'NativeInfillingRequest': ('inpaint', '局部重绘 inpaint'),
    'A1111': ('a1111', 'WebUI 文生图（A1111 格式）'),
    'A1111-img2img': ('a1111_img2img', 'WebUI 图生图（A1111 格式）'),
}
STRENGTH_KINDS = ('img2img', 'inpaint', 'enhance', 'a1111_img2img')
# 模型哈希 → 模型名。用于 Source 缺失或为内部枚举名（如 "DiffusionModelMetaName.NAIv4next"）的情况。
# V3 以外的条目统计自实际生成的图片。
KNOWN_MODEL_HASHES = {
    'C1E1DE52': 'NovelAI Diffusion V3',
    'F6E18726': 'NovelAI Diffusion V4', '79F47848': 'NovelAI Diffusion V4', '4F49EC75': 'NovelAI Diffusion V4',
    'C1CCBA86': 'NovelAI Diffusion V4', '37442FCA': 'NovelAI Diffusion V4',
    '4BDE2A90': 'NovelAI Diffusion V4.5', '1229B44F': 'NovelAI Diffusion V4.5',
    'C02D4F98': 'NovelAI Diffusion V4.5', '5BB76870': 'NovelAI Diffusion V4.5',
    '0ADF9AB7': 'NovelAI Diffusion V5', '657484A5': 'NovelAI Diffusion V5', 'DB276663': 'NovelAI Diffusion V5',
}
_HASH_RE = re.compile(r'\s+([0-9A-Fa-f]{8})$')
_A1111_KV = re.compile(r'\s*([A-Za-z][\w ]*?):\s*("(?:[^"\\]|\\.)*"|[^,]*)(?:,|$)')


def parse_a1111(text: str) -> dict | None:
    """将 Stable Diffusion WebUI（A1111、Forge 等）的 parameters 文本转换为 NovelAI 元数据结构。

    转换后可复用同一套摘要与显示逻辑。输入格式::

        正向提示词
        Negative prompt: 负面提示词
        Steps: 28, Sampler: Euler a, CFG scale: 7, Seed: 1, Size: 512x768, Model hash: abc, Model: xyz

    Returns:
        元数据 dict；文本不是该格式时返回 None。
    """
    if not text or 'Steps:' not in text:
        return None
    lines = text.strip().split('\n')
    idx = max(i for i, ln in enumerate(lines) if 'Steps:' in ln)
    head, param_line = lines[:idx], lines[idx]
    neg = next((i for i, ln in enumerate(head) if ln.startswith('Negative prompt:')), None)
    if neg is None:
        prompt, uc = '\n'.join(head).strip(), ''
    else:
        prompt = '\n'.join(head[:neg]).strip()
        uc = '\n'.join([head[neg][len('Negative prompt:'):]] + head[neg + 1:]).strip()
    kv = {k.strip(): v.strip().strip('"') for k, v in _A1111_KV.findall(param_line)}

    def f(key, cast):
        v = kv.get(key)
        try:
            return cast(v) if v not in (None, '') else None
        except ValueError:
            return v

    size = kv.get('Size', '').lower()
    w, h = (size.split('x', 1) + [''])[:2] if 'x' in size else ('', '')
    c = {'prompt': prompt, 'uc': uc, 'steps': f('Steps', int), 'sampler': kv.get('Sampler'),
         'noise_schedule': kv.get('Schedule type'), 'scale': f('CFG scale', float), 'seed': f('Seed', int),
         'width': int(w) if w.isdigit() else None, 'height': int(h) if h.isdigit() else None,
         'model_name': kv.get('Model'), 'model_hash': kv.get('Model hash'), 'request_type': 'A1111',
         'a1111_params': kv}
    if kv.get('Denoising strength'):
        c['strength'] = f('Denoising strength', float)
        c['request_type'] = 'A1111-img2img'
    return {'Software': 'Stable Diffusion WebUI', 'Comment': c}


def num(v) -> str:
    """格式化数值：浮点数去掉多余的零。"""
    return f'{v:g}' if isinstance(v, float) else str(v)


def _nums(xs) -> str:
    return ', '.join(num(x) for x in xs)


def _chars(cp: dict) -> list[dict]:
    """提取角色提示词列表。

    跳过空的角色提示词，但保留原始序号，使负面提示词中的“角色 2”与正向提示词中的“角色 2”对应。
    """
    out = []
    for i, cc in enumerate(cp.get('char_captions') or [], 1):
        t = (cc.get('char_caption') or '').strip()
        if t:
            out.append({'index': i, 'caption': t,
                        'centers': [(x.get('x'), x.get('y')) for x in cc.get('centers') or []]})
    return out


def summarize(meta: dict) -> dict:
    """将元数据整理为扁平的参数摘要。

    包括模型、生成类型、附加功能、采样、引导、各部分提示词和已启用的开关。
    """
    c = meta.get('Comment') if isinstance(meta.get('Comment'), dict) else {}
    v4 = c.get('v4_prompt') or {}
    cap = v4.get('caption') or {}
    ncap = (c.get('v4_negative_prompt') or {}).get('caption') or {}

    # 模型：Source 形如 "NovelAI Diffusion V5 0ADF9AB7"，末尾 8 位十六进制数为模型哈希
    source = str(meta.get('Source') or '')
    m = _HASH_RE.search(source)
    mhash = c.get('model_hash') or (m.group(1) if m else None)
    mname = c.get('model_name') or (source[:m.start()] if m else source) or None
    if mhash and (not mname or mname.startswith('DiffusionModel')):
        mname = KNOWN_MODEL_HASHES.get(mhash.upper(), mname)
    model = {'name': mname, 'hash': mhash, 'source': source or None, 'software': meta.get('Software')}

    # 生成类型：request_type 区分文生图、图生图和局部重绘；Enhance 是带 upscaled_enhance 的图生图；
    # 导演工具（emotion、lineart、colorize 等）由 req_type 与 defry 标识
    rt = c.get('request_type')
    kind, label = REQUEST_TYPES.get(rt, (rt or 'unknown', rt or '未知'))
    gtype = {'kind': kind, 'label': label, 'request_type': rt}
    if c.get('req_type'):
        gtype.update(kind='director_tool', label=f"导演工具 {c['req_type']}", req_type=c['req_type'])
        if c.get('defry') is not None:
            gtype['defry'] = c['defry']
    elif c.get('upscaled_enhance'):
        gtype.update(kind='enhance', label='增强 Enhance')
    if gtype['kind'] in STRENGTH_KINDS:
        sub = c.get('img2img') if isinstance(c.get('img2img'), dict) else {}   # V4.5 局部重绘将这些字段放在子对象中
        for k in ('strength', 'noise'):
            v = c[k] if c.get(k) is not None else sub.get(k)
            if v is not None:
                gtype[k] = v

    # 附加功能：Vibe Transfer、角色参考、ControlNet，可与任意生成类型组合
    addons = []
    refs = c.get('reference_strength_multiple') or (
        [c['reference_strength']] if c.get('reference_strength') is not None else [])
    if refs:
        info = c.get('reference_information_extracted_multiple') or []
        addons.append({'kind': 'vibe', 'label': f'Vibe Transfer ×{len(refs)}',
                       'detail': f'强度 {_nums(refs)}' + (f' · 信息提取 {_nums(info)}' if info else '')})
    drs = c.get('director_reference_strengths') or []
    dds = c.get('director_reference_descriptions') or []
    if drs or dds:
        sec = c.get('director_reference_secondary_strengths') or []
        addons.append({'kind': 'character_reference', 'label': f'角色参考 ×{len(drs) or len(dds)}',
                       'detail': (f'强度 {_nums(drs)}' if drs else '') + (f' · 次强度 {_nums(sec)}' if sec else '')})
    if c.get('controlnet_model'):
        addons.append({'kind': 'controlnet', 'label': f"ControlNet {c['controlnet_model']}",
                       'detail': f"强度 {num(c['controlnet_strength'])}" if c.get('controlnet_strength') is not None else ''})

    # 开关：仅列出已启用的项
    toggles: dict = {}
    if c.get('skip_cfg_above_sigma') not in (None, 0, False):
        toggles['Variety+'] = True
    if c.get('dynamic_thresholding'):
        toggles['Decrisper'] = True
    if c.get('sm'):
        toggles['SMEA DYN' if c.get('sm_dyn') else 'SMEA'] = True
    if c.get('tag_hint_qt'):
        toggles['质量标签'] = True
    if c.get('tag_hint_uc_preset') is not None:
        toggles['UC 预设'] = f"#{c['tag_hint_uc_preset']}"
    if c.get('tag_hint_transparent_background'):
        toggles['透明背景'] = True
    if c.get('upscale'):
        toggles['Upscale'] = True if c['upscale'] is True else num(c['upscale'])
    us = c.get('uncond_scale')
    if isinstance(us, (int, float)) and 0 < us < 1:
        toggles['UC 强度'] = num(us)
    if c.get('legacy_v3_extend'):
        toggles['Legacy V3 extend'] = True
    if v4.get('use_coords'):
        toggles['角色坐标'] = True

    sampler = c.get('sampler')
    return {
        'model': model,
        'type': gtype,
        'addons': addons,
        'width': c.get('width'), 'height': c.get('height'),
        'steps': c.get('steps'), 'scale': c.get('scale'), 'cfg_rescale': c.get('cfg_rescale'),
        'sampler': sampler, 'sampler_name': SAMPLERS.get(sampler), 'noise_schedule': c.get('noise_schedule'),
        'seed': c.get('seed'),
        'generation_time': meta.get('Generation time'),
        'prompt': c.get('prompt') or cap.get('base_caption') or meta.get('Description') or '',
        'char_prompts': _chars(cap),
        'use_coords': bool(v4.get('use_coords')),
        'uc': c.get('uc') or ncap.get('base_caption') or '',
        'char_uc': _chars(ncap),
        'toggles': toggles,
        'signed_hash': c.get('signed_hash'),
        'version': c.get('version'),
    }


def diff_meta(a: dict, b: dict) -> list[str]:
    """比较两份 NovelAI 元数据，返回取值不同的字段名。

    仅比较两层共有的字段（明文层的 Title 在隐写层中不存在）。
    """
    out = []
    for k in ('Description', 'Software', 'Source', 'Generation time'):
        if k in a and k in b and a[k] != b[k]:
            out.append(k)
    ca, cb = a.get('Comment'), b.get('Comment')
    if isinstance(ca, dict) and isinstance(cb, dict):
        for k in sorted(set(ca) | set(cb)):
            va, vb = ca.get(k), cb.get(k)
            # 两层分别签名，signed_hash 必然不同；隐写层不保存参考图等大字段（值为 None），单侧缺失不视为差异
            if k == 'signed_hash' or va is None or vb is None:
                continue
            if va != vb:
                out.append(f'Comment.{k}')
    elif ca != cb:
        out.append('Comment')
    return out


# ---------------------------------------------------------------- LSB 隐写
MAGICS = {
    'stealth_pnginfo': ('alpha', False),
    'stealth_pngcomp': ('alpha', True),
    'stealth_rgbinfo': ('rgb', False),
    'stealth_rgbcomp': ('rgb', True),
}
SIG_BITS = 15 * 8           # 四种 magic 长度相同
LEN_BITS = 32
HEADER_BITS = SIG_BITS + LEN_BITS


@dataclass
class Stealth:
    channel: str            # 'alpha' | 'rgb'
    compressed: bool
    magic: str
    used_bits: int          # 头部、数据与 FEC 段共占用的最低位数量，清除时使用
    nbytes: int             # 解压后的字节数
    text: str
    fec_bytes: int = 0      # 官方格式数据之后可选的纠错码长度；NovelAI 目前不写入

    @property
    def meta(self) -> dict | None:
        return meta_from_text(self.text)

    def describe(self) -> str:
        """返回简短描述，如 ``alpha+gzip 4726 B``。"""
        s = f"{self.channel}{'+gzip' if self.compressed else ''} {self.nbytes} B"
        return s + (f' + FEC {self.fec_bytes} B' if self.fec_bytes else '')


def _lsb_stream(arr: np.ndarray, channel: str) -> np.ndarray:
    """提取最低位比特流。按列优先顺序（先遍历一列的所有行，再到下一列），与写入顺序一致。"""
    if channel == 'alpha':
        return (arr[:, :, 3] & 1).T.reshape(-1)
    # RGB 模式下每个像素依次提供 R、G、B 三个比特，像素按列优先排列
    return (arr[:, :, :3] & 1).transpose(1, 0, 2).reshape(-1)


def _to_rgb_or_rgba(im: Image.Image) -> Image.Image:
    if im.mode in ('RGB', 'RGBA'):
        return im
    has_alpha = 'A' in im.mode or 'transparency' in im.info
    return im.convert('RGBA' if has_alpha else 'RGB')


def find_stealth(im: Image.Image) -> Stealth | None:
    """检测图像中的 LSB 隐写数据。

    RGBA 图像依次检查 alpha 通道与 RGB 通道，RGB 图像仅检查 RGB 通道。

    Returns:
        检测结果；未检测到时返回 None。
    """
    im = _to_rgb_or_rgba(im)
    arr = np.asarray(im)
    channels = ('alpha', 'rgb') if im.mode == 'RGBA' else ('rgb',)
    for channel in channels:
        st = _decode_channel(arr, channel)
        if st:
            return st
    return None


def _decode_channel(arr: np.ndarray, channel: str) -> Stealth | None:
    bits = _lsb_stream(arr, channel)
    if bits.size < HEADER_BITS:
        return None
    magic = np.packbits(bits[:SIG_BITS]).tobytes().decode('ascii', 'replace')
    if magic not in MAGICS or MAGICS[magic][0] != channel:
        return None
    compressed = MAGICS[magic][1]
    n_bits = int.from_bytes(np.packbits(bits[SIG_BITS:HEADER_BITS]).tobytes(), 'big')
    # 长度须为正数、按字节对齐且不超出图像容量，否则视为噪声误匹配
    if n_bits <= 0 or n_bits % 8 or HEADER_BITS + n_bits > bits.size:
        return None
    payload = np.packbits(bits[HEADER_BITS:HEADER_BITS + n_bits]).tobytes()
    if compressed:
        try:
            payload = gzip.decompress(payload)
        except Exception:                        # magic 匹配但数据已损坏
            return None
    used, fec_bytes = HEADER_BITS + n_bits, 0
    # 官方格式（alpha 通道）在数据之后有一段可选的 FEC 纠错码：先是 32 位长度（单位为比特），
    # 0xffffffff 表示无纠错码。NovelAI 目前只写入该标记；经官方 nai_add_fec.py 添加的纠错码同样计入清除范围。
    if channel == 'alpha' and used + LEN_BITS <= bits.size:
        fec_len = int.from_bytes(np.packbits(bits[used:used + LEN_BITS]).tobytes(), 'big')
        if fec_len == 0xFFFFFFFF:
            used += LEN_BITS
        elif fec_len > 0 and fec_len % 8 == 0 and used + LEN_BITS + fec_len <= bits.size:
            used += LEN_BITS + fec_len
            fec_bytes = fec_len // 8
    return Stealth(channel, compressed, magic, used, len(payload), payload.decode('utf-8', 'replace'), fec_bytes)


def wipe_stealth(arr: np.ndarray, channel: str, used_bits: int) -> int:
    """就地清除隐写数据占用的最低位，不修改占用区域以外的像素。

    对于 alpha 通道隐写，占用区域内 alpha ≥ 254 的像素设为 255，其余像素清除最低位。
    NovelAI 在不透明的 alpha 上写入隐写，因此该区域内的 254 均由写入产生。
    该规则不依赖整幅图像完全不透明（NovelAI 的 WebP 边缘常有少量 alpha < 254 的像素）。

    Returns:
        alpha 由 254 恢复为 255 的像素数。
    """
    h = arr.shape[0]
    idx = np.arange(used_bits)
    if channel == 'alpha':
        rows, cols = idx % h, idx // h
        a = arr[rows, cols, 3]
        arr[rows, cols, 3] = np.where(a >= 254, 255, a & 0xFE)
        return int(np.count_nonzero(a == 254))
    pix = idx // 3
    arr[pix % h, pix // h, idx % 3] &= 0xFE
    return 0


# ---------------------------------------------------------------- 写入（投毒 / 自定义元数据）
NAI_TEXT_KEYS = ('Title', 'Description', 'Software', 'Source', 'Generation time', 'Comment')   # PNG 文本块顺序
STEALTH_KEYS = ('Description', 'Software', 'Source', 'Generation time', 'Comment')            # 隐写层不含 Title
DEFAULT_UC = ('nsfw, lowres, artistic error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, '
              'very displeasing, chromatic aberration, dithering, halftone, screentone, multiple views, logo, '
              'too many watermarks, negative space, blank page')


def default_comment(width: int = 0, height: int = 0) -> dict:
    """生成 NovelAI Diffusion V5 格式的默认 Comment。

    字段及顺序与实际生成的图片一致，可被 novelai.net/inspect 识别。
    """
    return {
        'prompt': '', 'steps': 28, 'height': height, 'width': width, 'scale': 5.0, 'uncond_scale': 0.0,
        'cfg_rescale': 0.0, 'seed': random.randrange(1, 2 ** 32), 'n_samples': 1, 'noise_schedule': 'karras',
        'legacy_v3_extend': False, 'reference_information_extracted_multiple': [], 'reference_strength_multiple': [],
        'v4_prompt': {'caption': {'base_caption': '', 'char_captions': []},
                      'use_coords': False, 'use_order': True, 'legacy_uc': False},
        'v4_negative_prompt': {'caption': {'base_caption': DEFAULT_UC, 'char_captions': []},
                               'use_coords': False, 'use_order': False, 'legacy_uc': False},
        'director_reference_strengths': None, 'director_reference_descriptions': None,
        'upscale': None, 'straight_alpha': True, 'quality_boost': False, 'sampler': 'k_euler_ancestral',
        'controlnet_strength': 1.0, 'controlnet_model': None, 'dynamic_thresholding': False,
        'dynamic_thresholding_percentile': 0.999, 'dynamic_thresholding_mimic_scale': 10.0,
        'sm': False, 'sm_dyn': False, 'skip_cfg_above_sigma': None, 'skip_cfg_below_sigma': 0.0,
        'deliberate_euler_ancestral_bug': False, 'prefer_brownian': True, 'cfg_sched_eligibility': 'enable_for_post_summer_samplers',
        'uncond_per_vibe': True, 'wonky_vibe_correlation': True, 'stream': 'msgpack',
        'tag_hint_transparent_background': None, 'tag_hint_uc_preset': 2, 'tag_hint_qt': 1,
        'legacy': False, 'color_correct': False, 'version': 1, 'uc': DEFAULT_UC,
        'request_type': 'PromptGenerateRequest', 'model_name': 'NovelAI Diffusion V5', 'model_hash': '0ADF9AB7',
    }


def set_prompt(c: dict, text: str) -> None:
    """设置正向提示词。

    同时写入 Comment.prompt 与 v4_prompt.caption.base_caption（V4 及以后版本实际使用该字段）。
    外层的 Description 由调用方负责同步。
    """
    c['prompt'] = text
    cap = c.setdefault('v4_prompt', {}).setdefault('caption', {})
    cap['base_caption'] = text
    cap.setdefault('char_captions', [])


def set_uc(c: dict, text: str) -> None:
    """设置负面提示词，同时写入 Comment.uc 与 v4_negative_prompt.caption.base_caption。"""
    c['uc'] = text
    cap = c.setdefault('v4_negative_prompt', {}).setdefault('caption', {})
    cap['base_caption'] = text
    cap.setdefault('char_captions', [])


def parse_set(item: str) -> tuple[str, object]:
    """解析 ``KEY=VALUE`` 形式的字段设置，如 ``seed=7``、``uc=lowres``、``sm=true``。

    值优先按 JSON 解析，解析失败时作为字符串。

    Raises:
        ValueError: 缺少 ``=``。
    """
    k, sep, v = item.partition('=')
    if not sep:
        raise ValueError(f'--set 的格式应为 KEY=VALUE：{item}')
    try:
        return k.strip(), json.loads(v)
    except json.JSONDecodeError:
        return k.strip(), v


def make_meta(base: dict | None = None, prompt: str | None = None, uc: str | None = None,
              sets: dict | None = None, size: tuple[int, int] = (0, 0)) -> dict:
    """构造待写入图片的元数据。

    Args:
        base: 基础元数据，通常取自原图，以保留 seed、模型等字段；为 None 时使用 :func:`default_comment`。
        prompt: 正向提示词，同时写入 Description。
        uc: 负面提示词。
        sets: 逐字段覆盖，键为顶层字段名或 Comment 内的字段名。
        size: 使用默认 Comment 时写入的宽和高。

    Returns:
        按 PNG 文本块顺序排列的元数据。修改内容后原签名失效，因此总会移除 signed_hash。
    """
    meta = copy.deepcopy(base) if base else {}
    if isinstance(meta.get('Comment'), str):     # 由 fill_meta 生成：Comment 为纯文本而非 JSON，保持原样
        filled = fill_meta(meta['Comment'], sets)
        for k in NAI_TEXT_KEYS:
            if k != 'Comment' and k in meta and k not in (sets or {}):
                filled[k] = meta[k]
        return filled
    c = meta.get('Comment') if isinstance(meta.get('Comment'), dict) else None
    if c is None:
        c = default_comment(*size)
    meta.setdefault('Title', 'NovelAI generated image')
    meta.setdefault('Software', 'NovelAI')
    meta.setdefault('Source', f"{c.get('model_name') or 'NovelAI Diffusion V5'} {c.get('model_hash') or '0ADF9AB7'}")
    meta.setdefault('Generation time', f'{random.uniform(2, 9):.13f}')
    if prompt is not None:
        set_prompt(c, prompt)
        meta['Description'] = prompt
    if uc is not None:
        set_uc(c, uc)
    for k, v in (sets or {}).items():
        if k in NAI_TEXT_KEYS:
            meta[k] = v
        elif k == 'prompt':
            set_prompt(c, str(v))
            meta['Description'] = str(v)
        elif k == 'uc':
            set_uc(c, str(v))
        else:
            c[k] = v
    meta.setdefault('Description', c.get('prompt', ''))
    c.pop('signed_hash', None)
    meta['Comment'] = c
    ordered = {k: meta[k] for k in NAI_TEXT_KEYS if k in meta}
    ordered.update({k: v for k, v in meta.items() if k not in ordered and not k.startswith('_')})
    return ordered


def compile_rules(rules: dict) -> list[tuple[str, str, "re.Pattern"]]:
    """编译词语替换规则。

    默认按子串匹配且不区分大小写；OLD 写作 ``/正则表达式/`` 时按正则表达式匹配。

    Returns:
        (OLD, NEW, 编译后的模式) 列表。
    """
    out = []
    for old, new in rules.items():
        if len(old) > 2 and old.startswith('/') and old.endswith('/'):
            pat = re.compile(old[1:-1], re.I)
        else:
            pat = re.compile(re.escape(old), re.I)
        out.append((old, str(new), pat))
    return out


def substitute_strings(obj, rules: dict, counts: Counter | None = None):
    """按规则递归替换 obj 中的所有字符串。

    遍历 dict 与 list，其他类型保持不变。

    Returns:
        (替换后的对象, 各规则的命中次数)。
    """
    counts = Counter() if counts is None else counts
    compiled = rules if isinstance(rules, list) else compile_rules(rules)
    if isinstance(obj, str):
        for old, new, pat in compiled:
            obj, n = pat.subn(new, obj)
            if n:
                counts[f'{old}→{new}'] += n
        return obj, counts
    if isinstance(obj, dict):
        return {k: substitute_strings(v, compiled, counts)[0] for k, v in obj.items()}, counts
    if isinstance(obj, list):
        return [substitute_strings(v, compiled, counts)[0] for v in obj], counts
    return obj, counts


def fill_meta(text: str, sets: dict | None = None) -> dict:
    """构造所有字段均为同一文本的元数据（对应 ``-t TEXT``）。

    Comment 也写入该文本本身。sets 中包含 Comment 内部字段（如 seed、uc）时，
    Comment 改为 ``{"prompt": text, "uc": text, ...}`` 形式的对象。
    """
    meta = {k: text for k in NAI_TEXT_KEYS}
    inner = {}
    for k, v in (sets or {}).items():
        if k in NAI_TEXT_KEYS:
            meta[k] = v
        else:
            inner[k] = v
    if inner:
        meta['Comment'] = {'prompt': text, 'uc': text, **inner}
    return meta


def _comment_str(meta: dict) -> str:
    c = meta.get('Comment')
    return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)


def meta_to_text(meta: dict) -> dict:
    """转换为 PNG 文本块内容。Comment 序列化为 JSON 字符串。"""
    out = {}
    for k in NAI_TEXT_KEYS:
        if k in meta and meta[k] is not None:
            out[k] = _comment_str(meta) if k == 'Comment' else str(meta[k])
    return out


def stealth_payload(meta: dict) -> bytes:
    """生成与 NovelAI 格式一致的隐写数据。

    布局：magic ``stealth_pngcomp``、32 位数据长度（比特）、gzip 压缩的 JSON、FEC 长度 ``0xffffffff``（无纠错码）。
    """
    d = {k: (_comment_str(meta) if k == 'Comment' else meta[k]) for k in STEALTH_KEYS if k in meta}
    data = gzip.compress(json.dumps(d, ensure_ascii=False).encode('utf-8'))
    return b'stealth_pngcomp' + (len(data) * 8).to_bytes(4, 'big') + data + b'\xff\xff\xff\xff'


def embed_stealth(arr: np.ndarray, payload: bytes) -> None:
    """按列优先顺序将数据写入 alpha 通道的最低位。

    Args:
        arr: RGBA 像素数组，就地修改。
        payload: :func:`stealth_payload` 的返回值。

    Raises:
        ValueError: 图像像素数少于数据比特数。
    """
    bits = np.unpackbits(np.frombuffer(payload, dtype=np.uint8))
    h, w = arr.shape[:2]
    if bits.size > h * w:
        raise ValueError(f'图像尺寸不足，无法写入隐写数据：需要 {bits.size} 个像素，实际为 {h * w} 个')
    idx = np.arange(bits.size)
    arr[idx % h, idx // h, 3] = (arr[idx % h, idx // h, 3] & 0xFE) | bits


def meta_to_exif(meta: dict) -> bytes:
    """生成 WebP 与 JPEG 使用的 EXIF 数据，字段布局与 NovelAI 的 WebP 下载一致。

    Software = Source（模型名与哈希），DocumentName = Title，ImageDescription = Description，
    UserComment = 完整元数据的 JSON。

    Returns:
        以 ``Exif\\0\\0`` 开头的字节串。
    """
    ex = Image.Exif()
    if meta.get('Source'):
        ex[0x0131] = str(meta['Source'])
    if meta.get('Title'):
        ex[0x010D] = str(meta['Title'])
    if meta.get('Description'):
        ex[0x010E] = str(meta['Description'])
    full = {k: (_comment_str(meta) if k == 'Comment' else v) for k, v in meta.items() if k in NAI_TEXT_KEYS}
    ex.get_ifd(0x8769)[0x9286] = b'ASCII\x00\x00\x00' + json.dumps(full, ensure_ascii=False).encode('utf-8')
    return ex.tobytes()


def load_meta_json(path: Path) -> dict:
    """读取预设或模板文件。

    忽略以 ``_`` 开头的键，并将 Comment 字符串解析为对象。

    Raises:
        ValueError: 顶层不是 JSON 对象。
    """
    d = json.loads(Path(path).read_text('utf-8'))
    if not isinstance(d, dict):
        raise ValueError(f'{path}：预设文件的顶层必须是 JSON 对象')
    return expand_comment({k: v for k, v in d.items() if not k.startswith('_')})


def config_dir() -> Path:
    """返回配置目录：Windows 为 %APPDATA%\\nai-meta，其他系统为 $XDG_CONFIG_HOME/nai-meta（默认 ~/.config/nai-meta）。"""
    if os.name == 'nt':
        base = Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming')
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return base / 'nai-meta'


# ---------------------------------------------------------------- 跨平台
def setup_console() -> None:
    """将标准输出与标准错误设为 UTF-8。

    Windows 上重定向到文件或管道时默认编码为 GBK，输出 ✔ 等符号会引发编码错误。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass


def _plain_symbols() -> bool:
    """判断是否使用 GBK 字符集内的替代符号（√ × > !）。

    传统 cmd 与 PowerShell 窗口的默认字体缺少 ✔ ▸ ⚠ 等字形；Windows Terminal（设置了 WT_SESSION）不受影响。
    环境变量 NAI_META_ASCII 设为 1 或 0 可强制开启或关闭。
    """
    flag = os.environ.get('NAI_META_ASCII')
    if flag is not None:
        return flag not in ('0', '')
    return os.name == 'nt' and not os.environ.get('WT_SESSION')


SYM = ({'ok': '√', 'bad': '×', 'yes': '√', 'no': '×', 'arrow': '>', 'warn': '!', 'bar': '==', 'rule': '-'}
       if _plain_symbols() else
       {'ok': '✔', 'bad': '✗', 'yes': '✓', 'no': '✗', 'arrow': '▸', 'warn': '⚠', 'bar': '━━', 'rule': '─'})

GLOB_CHARS = ('*', '?', '[')


def confirm(prompt: str) -> bool:
    """在终端中请求确认。仅输入 y 或 yes 视为同意；回车、其他输入、Ctrl-C、Ctrl-D 均视为拒绝。"""
    try:
        return input(prompt).strip().lower() in ('y', 'yes')
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# ---------------------------------------------------------------- 输出与文件遍历
def error(message: str, hint: str | None = None) -> None:
    """向标准错误输出错误信息，可附带一行提示。"""
    sys.stdout.flush()                           # 先输出已缓冲的标准输出，保持前后顺序
    print(f'错误：{message}', file=sys.stderr)
    if hint:
        print(f'提示：{hint}', file=sys.stderr)


def warn(message: str, hint: str | None = None) -> None:
    """向标准错误输出警告信息，可附带一行提示。"""
    sys.stdout.flush()                           # 先输出已缓冲的标准输出，保持前后顺序
    print(f'警告：{message}', file=sys.stderr)
    if hint:
        print(f'提示：{hint}', file=sys.stderr)

def iter_images(paths, recursive: bool = False) -> Iterator[tuple[Path, Path]]:
    """遍历输入路径，产出 (图片文件, 相对路径)。

    输入为目录时，相对路径保留目录层级，供 ``--outdir`` 使用；输入为文件时，相对路径为文件名。
    含 ``* ? [`` 的参数由本函数展开，因为 Windows 的 cmd 与 PowerShell 不会为外部程序展开通配符。
    """
    for p in paths:
        p = Path(p)
        if any(ch in str(p) for ch in GLOB_CHARS) and not p.exists():
            matches = sorted(glob.glob(str(p), recursive=True))
            if not matches:
                warn(f'没有与 {p} 匹配的文件')
                continue
            yield from iter_images(matches, recursive)
            continue
        if p.is_dir():
            it = p.rglob('*') if recursive else p.glob('*')
            for f in sorted(it):
                if f.is_file() and f.suffix.lower() in IMG_EXTS and not f.name.startswith('.'):
                    yield f, f.relative_to(p)
        elif p.is_file():
            yield p, Path(p.name)
        else:
            warn(f'{p} 不存在')


def fmt_size(n: int) -> str:
    """格式化文件大小，使用二进制单位（KiB、MiB）。"""
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if n < 1024 or unit == 'GiB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.2f} {unit}'
        n /= 1024
    return f'{n:.2f} GiB'
