# -*- coding: utf-8 -*-
"""nai-strip：剥掉 NovelAI 图片的元数据，像素不动。

PNG：去掉全部文本块（tEXt/iTXt/zTXt）、eXIf、tIME，擦掉 LSB 隐写，然后重新编码
     （PNG 无损，重编码不掉画质）。alpha 通道如果本来就是全不透明（NAI 出图都是），
     顺手把被隐写改成 254 的像素归回 255。
JPEG：按段剥掉 APP1(EXIF/XMP)、APP13(Photoshop/IPTC)、COM 等，不重新编码，画质不变。
WebP：无损的走像素路线无损重存；有损且 alpha 全不透明的（NAI 的 WebP 下载）在容器层丢掉
     ALPH / EXIF / XMP 块，RGB 数据一字节不动；有损又带真透明的只能有损重编码，会提示。
其他格式：走 Pillow 重编码，会有画质损失，会提示。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shlex
import struct
import subprocess
import sys
from argparse import Namespace
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

from PIL.PngImagePlugin import PngInfo

from .core import (GLOB_CHARS, SYM, confirm, config_dir, embed_stealth, expand_comment, fill_meta, find_stealth,
                   fmt_size, is_nai, iter_images, make_meta, meta_to_exif, meta_to_text, parse_set, scan_png,
                   setup_console, stealth_payload, wipe_stealth)


# ---------------------------------------------------------------- PNG / 通用（像素路线）
def clean_pixels(im: Image.Image, opts) -> tuple[Image.Image, list[str], list[str]]:
    """返回 (干净的新图, 做了什么, 提示)。新图不带任何 info，元数据得显式传给 save。"""
    done, notes = [], []
    if im.mode not in ('RGB', 'RGBA'):
        notes.append(f'{im.mode} 模式，未查隐写')
        clean = im.copy()
        for k in ('exif', 'xmp', 'comment', 'dpi'):
            clean.info.pop(k, None)
        return clean, done, notes

    arr = np.array(im)                           # 拷贝，可写
    st = find_stealth(im)
    if st:
        wipe_stealth(arr, st.channel, st.used_bits)
        done.append(f'隐写 {st.describe()}')
    if opts.scrub_all:
        arr &= 0xFE
        done.append('全通道 LSB 清零')
    if im.mode == 'RGBA':
        a = arr[:, :, 3]
        if a.min() >= 254 and (a != 255).any():  # 本来全不透明，只被隐写动过最低位
            a[:] = 255
            done.append('alpha→255')
        if opts.drop_alpha:
            if (a == 255).all():
                arr = arr[:, :, :3]
                done.append('去 alpha')
            else:
                notes.append('alpha 不是全不透明，保留')
    return Image.fromarray(arr), done, notes


def save_pixels(clean: Image.Image, im: Image.Image, dst: Path, fmt: str, opts,
                lossless: bool = False, meta: dict | None = None) -> None:
    kw = {'icc_profile': None if opts.strip_icc else im.info.get('icc_profile')}
    if clean.mode == im.mode and 'transparency' in im.info:
        kw['transparency'] = im.info['transparency']
    if fmt == 'WEBP':
        if lossless:
            kw['lossless'] = True
        else:                                    # alpha 无损、透明像素下的 RGB 也保留
            kw.update(quality=95, alpha_quality=100, exact=True)
    if meta:                                     # 投毒：PNG 写文本块，WebP 写 EXIF；隐写已在像素里
        if fmt == 'PNG':
            info = PngInfo()
            for k, v in meta_to_text(meta).items():
                info.add_text(k, v)
            kw['pnginfo'] = info
        elif fmt == 'WEBP':
            kw['exif'] = meta_to_exif(meta)
    clean.save(dst, format=fmt, **kw)


def poison_pixels(clean: Image.Image, meta: dict) -> Image.Image:
    """把隐写写进 alpha 最低位。没有 alpha 的图补一层全 255 的（NAI 出图本来就是 RGBA）。"""
    if clean.mode != 'RGBA':
        clean = clean.convert('RGBA')
    arr = np.array(clean)
    embed_stealth(arr, stealth_payload(meta))
    return Image.fromarray(arr)


# ---------------------------------------------------------------- JPEG（按段，无损）
APP_NAMES = {0xE0: 'APP0/JFIF', 0xE1: 'APP1/EXIF-XMP', 0xE2: 'APP2/ICC', 0xEC: 'APP12',
             0xED: 'APP13/Photoshop', 0xEE: 'APP14/Adobe', 0xFE: 'COM'}


def strip_jpeg(data: bytes, keep_icc: bool) -> tuple[bytes, list[str]]:
    """保留 APP0(JFIF)、APP14(Adobe 色彩变换标记，去了会偏色)、可选 APP2(ICC)，其余 APPn 与 COM 全丢。
    从 SOS 起原样拷贝，扫描数据一个字节不动。"""
    if data[:2] != b'\xff\xd8':
        raise ValueError('不是 JPEG')
    out = bytearray(b'\xff\xd8')
    removed = []
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            raise ValueError(f'JPEG 结构异常 @ {pos}')
        m = data[pos + 1]
        if m == 0xFF:                            # 填充字节
            pos += 1
            continue
        if m == 0xDA or m == 0xD9:               # SOS：剩下全是扫描数据；EOI
            out += data[pos:]
            break
        if m == 0xD8 or 0xD0 <= m <= 0xD7 or m == 0x01:   # 无长度的独立标记
            out += data[pos:pos + 2]
            pos += 2
            continue
        ln = struct.unpack('>H', data[pos + 2:pos + 4])[0]
        seg = data[pos:pos + 2 + ln]
        drop = False
        if 0xE1 <= m <= 0xEF and m != 0xEE:
            drop = not (m == 0xE2 and keep_icc and seg[4:16] == b'ICC_PROFILE\x00')
        elif m == 0xFE:
            drop = True
        if drop:
            removed.append(f'{APP_NAMES.get(m, f"APP{m - 0xE0}")} {ln} B')
        else:
            out += seg
        pos += 2 + ln
    return bytes(out), removed


def jpeg_with_exif(data: bytes, exif: bytes) -> bytes:
    """在 SOI 后插一段 APP1 EXIF，扫描数据不动。exif 带 Exif\\0\\0 头。"""
    return data[:2] + b'\xff\xe1' + struct.pack('>H', len(exif) + 2) + exif + data[2:]


# ---------------------------------------------------------------- WebP（容器层）
WEBP_META = {b'EXIF': 'EXIF', b'XMP ': 'XMP', b'ICCP': 'ICC', b'ALPH': 'ALPH'}


def webp_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    if data[:4] != b'RIFF' or data[8:12] != b'WEBP':
        raise ValueError('不是 WebP')
    out, pos = [], 12
    while pos + 8 <= len(data):
        tag, ln = data[pos:pos + 4], struct.unpack('<I', data[pos + 4:pos + 8])[0]
        out.append((tag, data[pos + 8:pos + 8 + ln]))
        pos += 8 + ln + (ln & 1)                 # 奇数长度补一个字节
    return out


def build_webp(chunks) -> bytes:
    body = b''.join(tag + struct.pack('<I', len(p)) + p + (b'\0' if len(p) & 1 else b'')
                    for tag, p in chunks)
    return b'RIFF' + struct.pack('<I', 4 + len(body)) + b'WEBP' + body


def strip_webp_container(data: bytes, keep_icc: bool, drop_alpha: bool) -> tuple[bytes, list[str]]:
    """丢 EXIF / XMP（可选 ICCP / ALPH）块，改 VP8X 标志位，图像数据一字节不动。"""
    kept, removed = [], []
    for tag, p in webp_chunks(data):
        if tag in (b'EXIF', b'XMP ') or (tag == b'ICCP' and not keep_icc) or (tag == b'ALPH' and drop_alpha):
            removed.append(f'{WEBP_META[tag]} {len(p)} B')
        else:
            kept.append((tag, p))
    tags = {t for t, _ in kept}
    out = []
    for tag, p in kept:
        if tag == b'VP8X':                       # 标志位：ICC 0x20 · Alpha 0x10 · EXIF 0x08 · XMP 0x04 · Anim 0x02
            flags = p[0] & ~(0x08 | 0x04)
            if b'ICCP' not in tags:
                flags &= ~0x20
            if b'ALPH' not in tags and b'ANIM' not in tags:
                flags &= ~0x10
            if flags == 0:                       # 没有扩展特性了，退回简单格式
                continue
            p = bytes([flags]) + p[1:]
        out.append((tag, p))
    return build_webp(out), removed


def webp_with_exif(data: bytes, exif: bytes, size: tuple[int, int]) -> bytes:
    """给 WebP 容器加 EXIF 块（去掉 Exif\\0\\0 头），没有 VP8X 就补一个。"""
    tiff = exif[6:] if exif.startswith(b'Exif\x00\x00') else exif
    chunks = [(t, p) for t, p in webp_chunks(data) if t != b'EXIF']
    tags = {t for t, _ in chunks}
    if b'VP8X' in tags:
        chunks = [(t, bytes([p[0] | 0x08]) + p[1:]) if t == b'VP8X' else (t, p) for t, p in chunks]
    else:
        flags = 0x08 | (0x10 if b'ALPH' in tags else 0)
        vp8x = bytes([flags, 0, 0, 0]) + (size[0] - 1).to_bytes(3, 'little') + (size[1] - 1).to_bytes(3, 'little')
        chunks.insert(0, (b'VP8X', vp8x))
    return build_webp(chunks + [(b'EXIF', tiff)])


def plan_webp(src: Path, im: Image.Image, opts, poison: dict | None = None):
    """WebP 分三种。VP8L 无损：像素路线，无损重存。VP8 有损 + alpha 全不透明（NAI 出图就是这样，
    隐写藏在无损压缩的 ALPH 块里）：容器层丢掉 ALPH / EXIF / XMP，RGB 数据一字节不动。
    VP8 有损 + 真透明：只能有损重编码。返回 (写文件函数, 做了什么, 提示)。"""
    raw = src.read_bytes()
    tags = {t for t, _ in webp_chunks(raw)}
    done, notes = [], []

    def container(new: bytes, removed: list[str]):
        if poison:                               # 有损 WebP 没有可写隐写的 alpha，只写 EXIF
            new = webp_with_exif(new, meta_to_exif(poison), im.size)
            removed = removed + ['写入投毒（EXIF）']
        return (lambda p: p.write_bytes(new)), removed

    def pixels(lossless: bool):
        clean, d, n = clean_pixels(im, opts)
        if poison:
            clean = poison_pixels(clean, poison)
            d.append('写入投毒（EXIF+隐写）')
        return (lambda p: save_pixels(clean, im, p, 'WEBP', opts, lossless=lossless, meta=poison)), d, n

    if b'ANIM' in tags:
        new, removed = strip_webp_container(raw, keep_icc=not opts.strip_icc, drop_alpha=False)
        notes.append('动图：只去容器层元数据，未查隐写')
        writer, removed = container(new, removed)
        return writer, removed, notes
    if b'VP8L' in tags:
        return pixels(lossless=True)
    has_alpha = b'ALPH' in tags
    if has_alpha:
        alpha = np.asarray(im.convert('RGBA'))[:, :, 3]
        if alpha.min() < 254:
            notes.append('有损 WebP 带真透明，只能重新编码（有损）')
            writer, d, n = pixels(lossless=False)
            return writer, d, notes + n
        st = find_stealth(im)
        if st:
            done.append(f'隐写 {st.describe()}')
        done.append('alpha 通道整个去掉（本来全不透明）')
    if opts.scrub_all:
        notes.append('有损 WebP 不做像素处理')
    new, removed = strip_webp_container(raw, keep_icc=not opts.strip_icc, drop_alpha=has_alpha)
    writer, removed = container(new, removed)
    return writer, done + removed, notes


# ---------------------------------------------------------------- 主流程
DEFAULTS = dict(paths=[], recursive=False, output=None, outdir=None, in_place=False, suffix=None,
                drop_alpha=False, scrub_all=False, strip_icc=False, overwrite=False, no_verify=False,
                dry_run=False, yes=False, poison=None, sets={}, poison_meta=None)


def make_opts(**overrides) -> Namespace:
    """给 TUI / 测试用：和命令行解析出来同构的选项对象。"""
    return Namespace(**{**DEFAULTS, **overrides})


def describe_plan(items, opts, label: str = '') -> str:
    """确认提示用的一句话：多少张、什么格式、写到哪。"""
    ext = Counter(f.suffix.lower().lstrip('.') for f, _ in items)
    kinds = ' · '.join(f'{k} {n}' for k, n in ext.most_common())
    if opts.in_place:
        dest = f'{SYM["warn"]} 原地覆盖，不留备份'
    elif opts.output:
        dest = f'→ {opts.output}'
    elif opts.outdir:
        dest = f'→ 目录 {opts.outdir}'
    else:
        dest = f'→ 原图旁边 +{suffix_of(opts)}'
    return f'{label + "：" if label else ""}{len(items)} 张（{kinds}）{dest}'


def output_path(src: Path, rel: Path, opts) -> Path:
    if opts.in_place:
        return src
    if opts.output:
        return Path(opts.output)
    if opts.outdir:
        return Path(opts.outdir) / rel
    return src.with_name(src.stem + suffix_of(opts) + src.suffix)


def suffix_of(opts) -> str:
    if opts.suffix:
        return opts.suffix
    return '_poison' if (opts.poison or opts.sets) else '_clean'


# ---------------------------------------------------------------- 投毒：预设与编辑
EDIT_HELP = [
    '这就是要写进图里的全部内容，保存退出即生效；清空文件或 JSON 不合法则取消。',
    'PNG 文本块 / WebP 与 JPEG 的 EXIF：下面每个键各一块，Comment 会转成 JSON 字符串。',
    '隐写层：Description、Software、Source、Generation time、Comment 五项打包 gzip 写进 alpha 最低位。',
    'Comment.prompt、Description、v4_prompt.caption.base_caption 要一致（novelai.net/inspect 读 Comment.prompt）；',
    '写入时 width / height 按每张图实际尺寸覆盖，seed 为 null 则每张随机。_ 开头的键不写入。',
]


def preset_dir() -> Path:
    return config_dir() / 'presets'


def load_meta_file(path: Path) -> dict:
    d = json.loads(path.read_text('utf-8'))
    if not isinstance(d, dict):
        raise ValueError('预设得是一个 JSON 对象')
    return expand_comment({k: v for k, v in d.items() if not k.startswith('_')})


def save_preset(name: str, meta: dict) -> Path:
    preset_dir().mkdir(parents=True, exist_ok=True)
    p = preset_dir() / f'{name}.json'
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), 'utf-8')
    return p


def list_presets() -> str:
    files = sorted(preset_dir().glob('*.json'), key=lambda x: (len(x.stem), x.stem))
    if not files:
        return f'没有预设（用 nais -t edit:1 建一个，存在 {preset_dir()}）'
    lines = [f'预设（{preset_dir()}）：']
    for f in files:
        try:
            m = load_meta_file(f)
            c = m.get('Comment') if isinstance(m.get('Comment'), dict) else {}
            prompt = (c.get('prompt') or m.get('Description') or '').replace('\n', ' ')
            lines.append(f'  {f.stem:>4}  {prompt[:70]}{"…" if len(prompt) > 70 else ""}')
        except Exception as e:
            lines.append(f'  {f.stem:>4}  <坏了: {e}>')
    return '\n'.join(lines)


def edit_meta(meta: dict) -> dict | None:
    """用 $VISUAL / $EDITOR 打开一份 JSON 让用户改，读回来。取消返回 None。"""
    doc = {'_说明': EDIT_HELP, **meta}
    config_dir().mkdir(parents=True, exist_ok=True)
    path = config_dir() / 'edit.json'
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), 'utf-8')
    editor = os.environ.get('VISUAL') or os.environ.get('EDITOR')
    if not editor:
        editor = 'notepad' if os.name == 'nt' else ('nano' if any(
            os.access(os.path.join(d, 'nano'), os.X_OK) for d in os.environ.get('PATH', '').split(os.pathsep)) else 'vi')
    print(f'用 {editor} 打开 {path}（EDITOR 环境变量可换编辑器）', file=sys.stderr)
    try:
        subprocess.call([*shlex.split(editor, posix=os.name != 'nt'), str(path)])
    except OSError as e:
        print(f'打不开编辑器 {editor}: {e}', file=sys.stderr)
        return None
    try:
        text = path.read_text('utf-8')
        if not text.strip():
            return None
        return load_meta_file(path)
    except (ValueError, json.JSONDecodeError) as e:
        print(f'JSON 不合法，已取消：{e}', file=sys.stderr)
        return None


def resolve_poison(opts, first: Path | None = None) -> int | None:
    """把 -t 的写法解析好：文本 → 每张图按原图元数据换提示词（留 None，逐张处理）；
    数字 → 预设；@文件 → 模板；edit / edit:N → 编辑器；list → 列预设。返回非 None 表示要直接退出。"""
    opts.sets = dict(parse_set(x) for x in (opts.sets or [])) if isinstance(opts.sets, list) else (opts.sets or {})
    opts.poison_meta = None
    spec = opts.poison
    if spec is None:
        return None
    if spec == 'list':
        print(list_presets())
        return 0
    if spec.startswith('edit'):
        name = spec.partition(':')[2] or None
        base = None
        if name and (preset_dir() / f'{name}.json').exists():
            base = load_meta_file(preset_dir() / f'{name}.json')
        elif first is not None:
            base = _orig_meta(first)
        size = Image.open(first).size if first is not None else (0, 0)
        meta = edit_meta(make_meta(base, sets=opts.sets, size=size))
        if meta is None:
            print('已取消')
            return 1
        if name is None:
            try:
                name = input('存为预设编号 / 名字（留空不存）: ').strip() or None
            except (EOFError, KeyboardInterrupt):
                name = None
        if name:
            print(f'预设 {name} 已存到 {save_preset(name, meta)}')
        opts.poison_meta = make_meta(meta)
        return None
    if spec.startswith('@'):
        opts.poison_meta = make_meta(load_meta_file(Path(spec[1:]).expanduser()), sets=opts.sets)
        return None
    if spec.isdigit() or (preset_dir() / f'{spec}.json').exists():
        p = preset_dir() / f'{spec}.json'
        if not p.exists():
            print(f'没有预设 {spec}\n{list_presets()}', file=sys.stderr)
            return 1
        opts.poison_meta = make_meta(load_meta_file(p), sets=opts.sets)
    return None


def _orig_meta(src: Path, im: Image.Image | None = None, scan=None) -> dict | None:
    """原图自带的 NAI 元数据：文本块 → 隐写 → EXIF。投毒时拿它当底，只换提示词。"""
    from .nai_inspect import exif_meta, read_exif
    scan = scan if scan is not None else scan_png(src)
    if scan and scan.texts:
        m = expand_comment(dict(scan.texts))
        if is_nai(m):
            return m
    own = im is None
    if own:
        im = Image.open(src)
        im.load()
    try:
        st = find_stealth(im)
        if st and is_nai(st.meta):
            return st.meta
        m = exif_meta(read_exif(im))
        return m if is_nai(m) else None
    finally:
        if own:
            im.close()


def poison_for(src: Path, im: Image.Image, scan, opts) -> dict | None:
    """这张图要写入的元数据；不投毒返回 None。预设 / 模板逐张覆盖尺寸，seed 为空则随机。"""
    if opts.poison_meta is not None:
        meta = copy.deepcopy(opts.poison_meta)
        c = meta['Comment']
        c['width'], c['height'] = im.size
        if c.get('seed') is None:
            import random
            c['seed'] = random.randrange(1, 2 ** 32)
        return meta
    if opts.poison:                              # -t '内容'：每个分块都塞这段
        return fill_meta(opts.poison, opts.sets)
    if opts.sets:                                # 只 --set：以原图元数据为底改字段
        return make_meta(_orig_meta(src, im, scan), sets=opts.sets, size=im.size)
    return None


def verify_poison(dst: Path, meta: dict) -> list[str]:
    """回读，确认写进去的提示词能被读出来（文本块 / EXIF 与隐写各自）。返回问题清单。"""
    from .nai_inspect import inspect_file
    rec = inspect_file(dst)
    want = str(meta.get('Description', ''))
    problems = []
    outer = rec.get('text_meta') or rec.get('exif_meta') or {}
    if str(outer.get('Description', '')) != want:
        problems.append('明文层')
    if dst.suffix.lower() not in ('.jpg', '.jpeg') and rec.get('mode') == 'RGBA':
        stm = (rec.get('stealth') or {}).get('meta') or {}
        if str(stm.get('Description', '')) != want:
            problems.append('隐写')
    return problems


def verify(dst: Path) -> list[str]:
    """重新打开输出，确认三层都没了。返回残留清单（空 = 干净）。"""
    left = []
    scan = scan_png(dst)
    if scan:
        if scan.texts:
            left.append('文本块 ' + ', '.join(scan.texts))
        if scan.exif:
            left.append('eXIf')
        if 'tIME' in scan.chunks:
            left.append('tIME')
    with Image.open(dst) as im:
        im.load()
        if im.getexif():
            left.append('EXIF')
        for k in ('xmp', 'comment', 'photoshop'):
            if im.info.get(k):
                left.append(k)
        if im.format != 'JPEG' and find_stealth(im):
            left.append('隐写')
    return left


def strip_one(src: Path, rel: Path, opts) -> tuple[bool, str]:
    """返回 (成功?, 报告行)。"""
    dst = output_path(src, rel, opts)
    tag = f'{src.name} → {dst if opts.output or opts.outdir else dst.name}' if dst != src else f'{src.name} (原地)'
    if dst.exists() and dst != src and not opts.overwrite:
        return False, f'{SYM["bad"]} {tag}   输出已存在，跳过（--overwrite 可覆盖）'

    try:
        found, done, notes = [], [], []
        scan = scan_png(src)
        with Image.open(src) as im:
            im.load()
            fmt = im.format or 'PNG'
            if scan:
                if scan.texts:
                    found.append(f"文本块 {len(scan.texts)} ({', '.join(scan.texts)})")
                for t in ('eXIf', 'tIME'):
                    if t in scan.chunks:
                        found.append(t)
                if scan.bit_depth == 16:
                    notes.append('16 位 PNG 会被降到 8 位')
            elif im.getexif():
                found.append('EXIF')
            for k in ('xmp', 'comment', 'photoshop'):
                if im.info.get(k):
                    found.append(k)

            poison = poison_for(src, im, scan, opts)
            if fmt == 'JPEG':
                if opts.scrub_all:
                    notes.append('JPEG 不做像素处理')
                new_bytes, removed = strip_jpeg(src.read_bytes(), keep_icc=not opts.strip_icc)
                done += removed
                if poison:
                    new_bytes = jpeg_with_exif(new_bytes, meta_to_exif(poison))
                    done.append('写入投毒（EXIF）')
                writer = lambda p: p.write_bytes(new_bytes)  # noqa: E731
            elif fmt == 'WEBP':
                writer, done_w, notes_w = plan_webp(src, im, opts, poison)
                done += done_w
                notes += notes_w
            else:
                if fmt != 'PNG':
                    notes.append(f'{fmt} 会被重新编码（有损）')
                clean, done_px, notes_px = clean_pixels(im, opts)
                done += done_px
                notes += notes_px
                if poison and fmt == 'PNG':
                    clean = poison_pixels(clean, poison)
                    done.append('写入投毒（文本块+隐写）')
                elif poison:
                    notes.append(f'{fmt} 不写投毒')
                    poison = None
                writer = lambda p: save_pixels(clean, im, p, fmt, opts, meta=poison)  # noqa: E731
            found += [d for d in done if d.startswith('隐写')]

        if not found and not done:
            if opts.in_place:
                return True, f'· {tag}   没发现元数据，不动'
            notes.append('没发现元数据，照样写了一份干净副本')

        if opts.dry_run:
            return True, f'· {tag}   [dry-run] 发现: {" · ".join(found) or "无"}' + (f'   ({"; ".join(notes)})' if notes else '')

        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + '.tmp~')
        writer(tmp)
        os.replace(tmp, dst)

        if opts.no_verify:
            left = []
        elif poison:
            left = [f'投毒回读失败: {", ".join(x)}' for x in [verify_poison(dst, poison)] if x]
        else:
            left = verify(dst)
        size = f'{fmt_size(src.stat().st_size)} → {fmt_size(dst.stat().st_size)}' if dst != src else fmt_size(dst.stat().st_size)
        # JPEG 按段报告（哪些段、多大）就够了；PNG 报发现的块 + 像素层动作
        removed = done if fmt in ('JPEG', 'WEBP') else found + [d for d in done if not d.startswith('隐写')]
        line = f'{SYM["ok"]} {tag}   去掉: {" · ".join(removed) or "无"}   {size}'
        if left:
            line = f'{SYM["bad"]} {tag}   仍有残留: {", ".join(left)}   {size}'
        if notes:
            line += f'   ({"; ".join(notes)})'
        return not left, line
    except Exception as e:
        return False, f'{SYM["bad"]} {tag}   失败: {e}'


def main(argv=None) -> int:
    setup_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'tui':                 # nais tui：交互模式，拖图进来就处理
        from .tui import run_tui
        return run_tui(argv[1:])
    ap = argparse.ArgumentParser(
        prog='nai-strip',
        description='剥掉 NovelAI 图片的元数据：PNG 文本块 / EXIF / LSB 隐写，像素内容不变。',
        epilog='示例：nais a.png（→ a_clean.png）  |  nais -i *.png（原地）  |  nais -r ./图 -d ./干净  |  nais tui（交互模式，拖图进来）')
    ap.add_argument('paths', nargs='*', help='图片文件或目录')
    ap.add_argument('-r', '--recursive', action='store_true', help='目录递归')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('-o', '--output', metavar='FILE', help='输出文件（只能配一个输入文件）')
    g.add_argument('-d', '--outdir', metavar='DIR', help='输出目录，保持原文件名；目录输入时保留相对层级')
    g.add_argument('-i', '--in-place', action='store_true', help='原地覆盖原文件（不留备份）')
    ap.add_argument('--suffix', default=None, help='不指定 -o/-d/-i 时写在原图旁边，文件名加此后缀（默认 _clean，投毒时 _poison）')
    ap.add_argument('-t', '--poison', metavar='内容', help=(
        "剥完再写入假元数据（投毒，文本块/EXIF 与隐写两层都写）：-t '内容' 每个分块都塞这段；"
        '-t 1 用预设 1；-t edit 打开编辑器逐字段确定，-t edit:1 编辑并存为预设 1；-t @文件.json 用现成模板；-t list 列预设'))
    ap.add_argument('--set', action='append', default=[], metavar='键=值',
                    help='改单个字段，可重复：--set seed=7 --set uc=lowres；单独用时以原图元数据为底')
    ap.add_argument('--drop-alpha', action='store_true', help='alpha 全不透明时去掉 alpha 通道存成 RGB，文件更小')
    ap.add_argument('--scrub-all', action='store_true', help='清掉所有通道所有像素的最低位（应付未知隐写变种；颜色最多变 1/255）')
    ap.add_argument('--strip-icc', action='store_true', help='连 ICC 色彩配置也去掉（默认保留，它不含生成信息）')
    ap.add_argument('--overwrite', action='store_true', help='输出文件已存在时覆盖（默认跳过）')
    ap.add_argument('--no-verify', action='store_true', help='写完不回读验证')
    ap.add_argument('-n', '--dry-run', action='store_true', help='只报告会做什么，不写文件')
    ap.add_argument('-y', '--yes', action='store_true', help='处理文件夹 / 通配符时不问 y/N（脚本里用）')
    a = ap.parse_args(argv)
    a.sets = a.set

    items = list(iter_images(a.paths, a.recursive))
    try:
        rc = resolve_poison(a, items[0][0] if items else None)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f'-t 参数有问题: {e}', file=sys.stderr)
        return 1
    if rc is not None:
        return rc
    if not items:
        if a.poison_meta is not None:
            return 0                              # 只是编辑预设
        print('没有找到图片', file=sys.stderr)
        return 1
    if a.output and len(items) > 1:
        print('-o 只能配一个输入文件；多个文件请用 -d 输出目录', file=sys.stderr)
        return 1
    # 文件夹 / 通配符是批量操作，先报数量再问一句；逐个点名的文件不问
    batch = [p for p in a.paths if Path(p).is_dir() or any(ch in p for ch in GLOB_CHARS)]
    if batch and not a.yes and not a.dry_run:
        print(describe_plan(items, a, ', '.join(batch)))
        if not confirm('继续？[y/N]（-y 可跳过确认）'):
            print('已取消')
            return 1

    ok = fail = 0
    for src, rel in items:
        good, line = strip_one(src, rel, a)
        print(line)
        ok += good
        fail += not good
    if len(items) > 1:
        print(f'—— 共 {len(items)} 张：成功 {ok}，失败/跳过 {fail}')
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
