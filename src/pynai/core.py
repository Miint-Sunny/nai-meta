# -*- coding: utf-8 -*-
"""pynai 共用逻辑：PNG 块扫描、LSB 隐写读/擦、NAI 元数据整理。

NAI 出图时把同一份元数据写了两遍：

1. PNG 文本块（tEXt）：Title / Description / Software / Source / Generation time / Comment。
   Comment 是 JSON 字符串，装着全部生成参数（prompt、uc、seed、sampler、v4_prompt …）。
   这一层 exiftool 能看到，也最容易被 QQ/微信转发剥掉。
2. LSB 隐写（stealth pnginfo）：把 {Description, Software, Source, Generation time, Comment}
   这份 JSON gzip 后，按**列优先**顺序写进 alpha 通道每个像素的最低位。
   novelai.net/inspect 读的就是它；只要图没被重编码（转 JPEG、缩放、二压）就还在。

   比特流布局：[magic 15 字节 ASCII][32 位大端 = 数据比特数][数据]
   magic：stealth_pnginfo / stealth_pngcomp（alpha 通道，后者 gzip）
         stealth_rgbinfo / stealth_rgbcomp（无 alpha 时写 RGB 三通道，A1111 插件用）
   NAI 自己只写 stealth_pngcomp。

读取思路来自 nai5-prompting/反推/stealth_decode.py，这里补上了「擦除」这一半。
"""
from __future__ import annotations

import glob
import gzip
import json
import os
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
# 这些块会被 nai-strip 去掉：文本、EXIF、修改时间
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
    exif: bytes | None = None                      # eXIf 块原始内容
    chunks: Counter = field(default_factory=Counter)

    @property
    def meta_chunk_names(self) -> list[str]:
        return [t for t in self.chunks if t.encode('latin-1') in META_TYPES]


def _txt(b: bytes) -> str:
    """PNG 规范说 tEXt 是 Latin-1，但 NAI 和大多数工具实际写的是 UTF-8。"""
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
        if typ == b'zTXt':                       # 1 字节压缩方法 + zlib 流
            return key, _txt(zlib.decompress(rest[1:]))
        flag, rest = rest[0], rest[2:]           # iTXt: 压缩标志、压缩方法、语言、翻译关键字、正文
        _lang, _, rest = rest.partition(b'\x00')
        _trans, _, text = rest.partition(b'\x00')
        if flag == 1:
            text = zlib.decompress(text)
        return key, _txt(text)
    except Exception as e:                       # 坏块不该让整张图读不了
        return key, f'<无法解码: {e}>'


def scan_png(path) -> PngScan | None:
    """逐块扫描 PNG，只读文本 / eXIf / IHDR，IDAT 直接跳过。非 PNG 返回 None。"""
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
                if k in scan.texts:              # 同名块重复时加序号，不覆盖
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
    """Comment 字段本身是 JSON 字符串，展开成 dict 方便取值。就地修改并返回。"""
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
    if not meta:
        return False
    if 'NovelAI' in str(meta.get('Software', '')) or 'NovelAI' in str(meta.get('Source', '')):
        return True
    c = meta.get('Comment')
    return isinstance(c, dict) and 'prompt' in c


def meta_from_text(s: str) -> dict | None:
    """一段字符串（隐写正文 / EXIF UserComment / JPEG 注释）里解出 NAI 元数据。"""
    try:
        d = json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return parse_a1111(s) if isinstance(s, str) else None
    if not isinstance(d, dict):
        return None
    if 'Comment' in d:
        return expand_comment(d)
    if 'prompt' in d:                            # 有人只把 Comment 那层塞进来
        return {'Comment': d}
    return None


# 采样器 id → NAI 界面上的名字
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
# 模型哈希 → 名字。Source 缺失或写成枚举名（见过 "DiffusionModelMetaName.NAIv4next"）时兜底；
# 除 V3 外都是从本机 900 多张图里统计出来的
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
    """Stable Diffusion WebUI（A1111 / Forge，很多工具沿用）的 parameters 文本 → 伪 NAI 元数据，
    好让摘要和版式复用。格式：
        正向提示词
        Negative prompt: 负面
        Steps: 28, Sampler: Euler a, CFG scale: 7, Seed: 1, Size: 512x768, Model hash: abc, Model: xyz"""
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
    return f'{v:g}' if isinstance(v, float) else str(v)


def _nums(xs) -> str:
    return ', '.join(num(x) for x in xs)


def _chars(cp: dict) -> list[dict]:
    """角色 caption 列表。空的跳过但保留原序号：负面区块的「角色 2」得对应正向的「角色 2」。"""
    out = []
    for i, cc in enumerate(cp.get('char_captions') or [], 1):
        t = (cc.get('char_caption') or '').strip()
        if t:
            out.append({'index': i, 'caption': t,
                        'centers': [(x.get('x'), x.get('y')) for x in cc.get('centers') or []]})
    return out


def summarize(meta: dict) -> dict:
    """摊平成一眼能看的参数表：模型 / 生图类型 / 附加功能 / 采样 / 引导 / 各区块提示词 / 开关。"""
    c = meta.get('Comment') if isinstance(meta.get('Comment'), dict) else {}
    v4 = c.get('v4_prompt') or {}
    cap = v4.get('caption') or {}
    ncap = (c.get('v4_negative_prompt') or {}).get('caption') or {}

    # 模型：Source 形如 "NovelAI Diffusion V5 0ADF9AB7"，末尾 8 位十六进制是模型哈希
    source = str(meta.get('Source') or '')
    m = _HASH_RE.search(source)
    mhash = c.get('model_hash') or (m.group(1) if m else None)
    mname = c.get('model_name') or (source[:m.start()] if m else source) or None
    if mhash and (not mname or mname.startswith('DiffusionModel')):
        mname = KNOWN_MODEL_HASHES.get(mhash.upper(), mname)
    model = {'name': mname, 'hash': mhash, 'source': source or None, 'software': meta.get('Software')}

    # 生图类型：request_type 分文生图 / i2i / inpaint；Enhance 是带 upscaled_enhance 的 i2i；
    # Director Tools（emotion / lineart / colorize …）走 req_type + defry
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
        sub = c.get('img2img') if isinstance(c.get('img2img'), dict) else {}   # V4.5 inpaint 把这些塞在子字典里
        for k in ('strength', 'noise'):
            v = c[k] if c.get(k) is not None else sub.get(k)
            if v is not None:
                gtype[k] = v

    # 附加功能：Vibe Transfer / 角色参考 / ControlNet，任何类型都可能叠加
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

    # 开关：只列打开的
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
    """两份 NAI 元数据哪些字段不一样。只比公共字段（文本块多一个 Title，隐写里没有）。"""
    out = []
    for k in ('Description', 'Software', 'Source', 'Generation time'):
        if k in a and k in b and a[k] != b[k]:
            out.append(k)
    ca, cb = a.get('Comment'), b.get('Comment')
    if isinstance(ca, dict) and isinstance(cb, dict):
        for k in sorted(set(ca) | set(cb)):
            va, vb = ca.get(k), cb.get(k)
            # 两层各自签名，signed_hash 本来就不同；隐写层不存参考图这类大字段（写成 None），一边缺失不算冲突
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
SIG_BITS = 15 * 8           # 四种 magic 等长
LEN_BITS = 32
HEADER_BITS = SIG_BITS + LEN_BITS


@dataclass
class Stealth:
    channel: str            # 'alpha' | 'rgb'
    compressed: bool
    magic: str
    used_bits: int          # 头 + 数据 (+ FEC 段) 一共占了多少个最低位（擦除时用）
    nbytes: int             # 解压后的字节数
    text: str
    fec_bytes: int = 0      # 官方格式载荷后可选的纠错码，NAI 目前不写

    @property
    def meta(self) -> dict | None:
        return meta_from_text(self.text)

    def describe(self) -> str:
        s = f"{self.channel}{'+gzip' if self.compressed else ''} {self.nbytes} B"
        return s + (f' + FEC {self.fec_bytes} B' if self.fec_bytes else '')


def _lsb_stream(arr: np.ndarray, channel: str) -> np.ndarray:
    """把最低位抽成 0/1 比特流。列优先：先走完一列的 y，再下一列 x，与写入顺序一致。"""
    if channel == 'alpha':
        return (arr[:, :, 3] & 1).T.reshape(-1)
    # 每个像素贡献 r,g,b 三位，像素本身按列优先
    return (arr[:, :, :3] & 1).transpose(1, 0, 2).reshape(-1)


def _to_rgb_or_rgba(im: Image.Image) -> Image.Image:
    if im.mode in ('RGB', 'RGBA'):
        return im
    has_alpha = 'A' in im.mode or 'transparency' in im.info
    return im.convert('RGBA' if has_alpha else 'RGB')


def find_stealth(im: Image.Image) -> Stealth | None:
    """在图里找隐写。RGBA 先查 alpha 通道再查 RGB；RGB 只查 RGB。找不到返回 None。"""
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
    # 长度得字节对齐、为正、装得下，否则判为噪声误命中
    if n_bits <= 0 or n_bits % 8 or HEADER_BITS + n_bits > bits.size:
        return None
    payload = np.packbits(bits[HEADER_BITS:HEADER_BITS + n_bits]).tobytes()
    if compressed:
        try:
            payload = gzip.decompress(payload)
        except Exception:                        # gzip 头对上了但内容坏了
            return None
    used, fec_bytes = HEADER_BITS + n_bits, 0
    # 官方格式（alpha 通道）载荷后还跟一段可选 FEC 纠错码：32 位长度（比特数），0xffffffff = 没有。
    # NAI 目前只写这个标记；用官方 nai_add_fec.py 加过 FEC 的图这里也一并算进擦除范围。
    if channel == 'alpha' and used + LEN_BITS <= bits.size:
        fec_len = int.from_bytes(np.packbits(bits[used:used + LEN_BITS]).tobytes(), 'big')
        if fec_len == 0xFFFFFFFF:
            used += LEN_BITS
        elif fec_len > 0 and fec_len % 8 == 0 and used + LEN_BITS + fec_len <= bits.size:
            used += LEN_BITS + fec_len
            fec_bytes = fec_len // 8
    return Stealth(channel, compressed, magic, used, len(payload), payload.decode('utf-8', 'replace'), fec_bytes)


def wipe_stealth(arr: np.ndarray, channel: str, used_bits: int) -> None:
    """就地把隐写占用的那些最低位清零。只动头 + 数据覆盖到的像素，其余一位不碰。"""
    h = arr.shape[0]
    idx = np.arange(used_bits)
    if channel == 'alpha':
        arr[idx % h, idx // h, 3] &= 0xFE
    else:
        pix = idx // 3
        arr[pix % h, pix // h, idx % 3] &= 0xFE


# ---------------------------------------------------------------- 跨平台
def setup_console() -> None:
    """Windows 上 stdout 重定向到文件 / 管道时默认是 GBK，✔ ▸ 这类符号会直接报错；统一成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass


def _plain_symbols() -> bool:
    """老式 cmd / PowerShell 窗口的字体常缺 ✔ ▸ ⚠，用 GBK 里有的 √ × > ! 代替。
    Windows Terminal（有 WT_SESSION 环境变量）不用降级。PYNAI_ASCII=1/0 可强制。"""
    flag = os.environ.get('PYNAI_ASCII')
    if flag is not None:
        return flag not in ('0', '')
    return os.name == 'nt' and not os.environ.get('WT_SESSION')


SYM = ({'ok': '√', 'bad': '×', 'yes': '√', 'no': '×', 'arrow': '>', 'warn': '!', 'bar': '==', 'rule': '-'}
       if _plain_symbols() else
       {'ok': '✔', 'bad': '✗', 'yes': '✓', 'no': '✗', 'arrow': '▸', 'warn': '⚠', 'bar': '━━', 'rule': '─'})

GLOB_CHARS = ('*', '?', '[')


def confirm(prompt: str) -> bool:
    """命令行里的 y/N。回车、n、Ctrl-C、Ctrl-D 都算否。"""
    try:
        return input(prompt).strip().lower() in ('y', 'yes')
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# ---------------------------------------------------------------- 杂项
def iter_images(paths, recursive: bool = False) -> Iterator[tuple[Path, Path]]:
    """产出 (文件, 相对路径)。目录输入时相对路径保留层级，供 --outdir 用；单文件就是文件名。
    带 * ? [ 的参数自己展开：Windows 的 cmd / PowerShell 不替外部程序展开通配符。"""
    for p in paths:
        p = Path(p)
        if any(ch in str(p) for ch in GLOB_CHARS) and not p.exists():
            matches = sorted(glob.glob(str(p), recursive=True))
            if not matches:
                print(f'没有匹配: {p}', file=sys.stderr)
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
            print(f'找不到: {p}', file=sys.stderr)


def fmt_size(n: int) -> str:
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if n < 1024 or unit == 'GiB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.2f} {unit}'
        n /= 1024
    return f'{n:.2f} GiB'
