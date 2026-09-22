# -*- coding: utf-8 -*-
"""nai-strip：剥掉 NovelAI 图片的元数据，像素不动。

PNG：去掉全部文本块（tEXt/iTXt/zTXt）、eXIf、tIME，擦掉 LSB 隐写，然后重新编码
     （PNG 无损，重编码不掉画质）。隐写占用区里被改成 254 的 alpha 归回 255，
     别处的 alpha 一位不碰（NAI 的 WebP 边缘常有几个不到 254 的像素，照样保留）。
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
import re
import struct
import sys
import time
from argparse import Namespace
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

from PIL.PngImagePlugin import PngInfo

from .core import (GLOB_CHARS, SYM, confirm, config_dir, embed_stealth, expand_comment, fill_meta, find_stealth,
                   fmt_size, is_nai, iter_images, load_meta_json, make_meta, meta_to_exif, meta_to_text, parse_set,
                   scan_png, setup_console, stealth_payload, substitute_strings, wipe_stealth)


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
    restored = 0
    if st:
        restored = wipe_stealth(arr, st.channel, st.used_bits)
        done.append(f'隐写 {st.describe()}')
    if opts.scrub_all:
        arr[:, :, :3] &= 0xFE
        if im.mode == 'RGBA':                    # alpha 同隐写区的规矩：≥254 归 255，其余清最低位
            a = arr[:, :, 3]
            restored += int(np.count_nonzero(a == 254))
            arr[:, :, 3] = np.where(a >= 254, 255, a & 0xFE)
        done.append('全通道 LSB 清零')
    if im.mode == 'RGBA':
        a = arr[:, :, 3]
        if a.min() >= 254 and (a != 255).any():  # 本来全不透明，被没认出来的东西动过最低位
            restored += int(np.count_nonzero(a == 254))
            a[:] = 255
        if restored:
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


def plan_webp(src: Path, im: Image.Image, opts, poison: dict | None = None, label: str = '写入投毒'):
    """WebP 分三种。VP8L 无损：像素路线，无损重存。VP8 有损 + alpha 全不透明（NAI 出图就是这样，
    隐写藏在无损压缩的 ALPH 块里）：容器层丢掉 ALPH / EXIF / XMP，RGB 数据一字节不动。
    VP8 有损 + 真透明：只能有损重编码。返回 (写文件函数, 做了什么, 提示)。"""
    raw = src.read_bytes()
    tags = {t for t, _ in webp_chunks(raw)}
    done, notes = [], []

    def container(new: bytes, removed: list[str]):
        if poison:                               # 有损 WebP 没有可写隐写的 alpha，只写 EXIF
            new = webp_with_exif(new, meta_to_exif(poison), im.size)
            removed = removed + [f'{label}（EXIF）']
        return (lambda p: p.write_bytes(new)), removed

    def pixels(lossless: bool):
        clean, d, n = clean_pixels(im, opts)
        if poison:
            clean = poison_pixels(clean, poison)
            d.append(f'{label}（EXIF+隐写）')
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
                dry_run=False, yes=False, poison=None, sets={}, poison_meta=None,
                words=[], word_rules={}, word_presets=[], rename=False)


def make_opts(**overrides) -> Namespace:
    """给 TUI / 测试用：和命令行解析出来同构的选项对象。"""
    return Namespace(**{**DEFAULTS, **overrides})


def describe_plan(items, opts, label: str = '') -> str:
    """确认提示用的一句话：多少张、什么格式、写到哪。"""
    ext = Counter(f.suffix.lower().lstrip('.') for f, _ in items)
    kinds = ' · '.join(f'{k} {n}' for k, n in ext.most_common())
    named = f'改名 {today()}-NNNN' if opts.rename else ''
    if opts.in_place:
        dest = f'{SYM["warn"]} 原地覆盖，不留备份' + (f'，{named}' if named else '')
    elif opts.output:
        dest = f'→ {opts.output}'
    elif opts.outdir:
        dest = f'→ 目录 {opts.outdir}' + (f'，{named}' if named else '')
    else:
        dest = f'→ 原图旁边 {named or "+" + suffix_of(opts)}'
    return f'{label + "：" if label else ""}{len(items)} 张（{kinds}）{dest}'


def today() -> str:
    return time.strftime('%Y%m%d')


DATE_NAME = re.compile(r'\d{8}-\d{4,}')          # -N 起的名字：20260923-0001
NAI_NAME = re.compile(r' s-\d+$')                # NAI 下载的默认文件名：提示词开头 + s-种子


def next_name(opts, folder: Path, ext: str) -> Path:
    """-N：同一目录按 今天日期-0001 往后接号。已有的同日编号不论扩展名都跳过，
    免得 0001.png 和 0001.webp 并存；dry-run 也占号，报出来的名字和真跑时一致。"""
    date = today()
    taken = vars(opts).setdefault('_numbers', {})
    key = (str(folder.resolve()), date)
    if key not in taken:
        pat = re.compile(rf'{date}-(\d{{4,}})')
        taken[key] = max((int(m.group(1)) for f in (folder.iterdir() if folder.is_dir() else ())
                          if (m := pat.fullmatch(f.stem))), default=0)
    taken[key] += 1
    return folder / f'{date}-{taken[key]:04d}{ext.lower()}'


def output_path(src: Path, rel: Path, opts) -> Path:
    if opts.in_place:
        if opts.rename and not DATE_NAME.fullmatch(src.stem):     # 已经是编号名的就不再换号
            return next_name(opts, src.parent, src.suffix)
        return src
    if opts.output:
        return Path(opts.output)
    if opts.rename:
        return next_name(opts, Path(opts.outdir) / rel.parent if opts.outdir else src.parent, src.suffix)
    if opts.outdir:
        return Path(opts.outdir) / rel
    return src.with_name(Path(rel.name).stem + suffix_of(opts) + src.suffix)   # rel 可能被 -w 改过词


def suffix_of(opts) -> str:
    if opts.suffix:
        return opts.suffix
    if opts.word_rules:                          # 只用了一个预设就拿预设名当后缀：a_discord.png
        return f'_{opts.word_presets[0]}' if len(opts.word_presets) == 1 and len(opts.words) == 1 else '_w'
    return '_poison' if (opts.poison or opts.sets) else '_clean'


# ---------------------------------------------------------------- 改词（-w）：词表
BUILTIN_WORDS = {'discord': {'loli': '1011'}}


def words_dir() -> Path:
    return config_dir() / 'words'


def words_path(name: str) -> Path:
    return words_dir() / f'{name}.txt'


def parse_words_text(text: str) -> dict:
    rules = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith('#') or '=' not in ln:
            continue
        k, _, v = ln.partition('=')
        rules[k.strip()] = v.strip()
    return rules


def load_words(name: str) -> dict | None:
    """用户文件优先，其次内置；都没有返回 None。"""
    if words_path(name).exists():
        return parse_words_text(words_path(name).read_text('utf-8'))
    return dict(BUILTIN_WORDS[name]) if name in BUILTIN_WORDS else None


def save_words(name: str, rules: dict) -> Path:
    words_dir().mkdir(parents=True, exist_ok=True)
    p = words_path(name)
    p.write_text('# 一行一条：旧=新。不分大小写、按子串匹配；旧写成 /正则/ 按正则\n'
                 + ''.join(f'{k}={v}\n' for k, v in rules.items()), 'utf-8')
    return p


def list_words() -> str:
    names = sorted({*BUILTIN_WORDS, *(p.stem for p in words_dir().glob('*.txt'))} if words_dir().exists() else set(BUILTIN_WORDS))
    lines = [f'改词表（{words_dir()}；没有文件的是内置）：']
    for n in names:
        rules = load_words(n) or {}
        src = '' if words_path(n).exists() else '（内置）'
        lines.append(f'  {n:>10}{src}  ' + ' · '.join(f'{k}→{v}' for k, v in rules.items()))
    return '\n'.join(lines)


def resolve_words(opts) -> int | None:
    """-w 的写法：旧=新 单条规则；预设名；list；edit 名字 / edit:名字。多个 -w 叠加。返回非 None 表示要直接退出。"""
    opts.word_rules, opts.word_presets = {}, []
    for spec in opts.words or []:
        spec = spec.strip()
        if spec == 'list':
            print(list_words())
            return 0
        if spec == 'edit' or spec.startswith(('edit:', 'edit ')):
            from .edit import edit_words_interactive
            name = spec[4:].lstrip(': ').strip()
            if not name:
                print('要写成 -w edit 名字', file=sys.stderr)
                return 1
            rules = edit_words_interactive(name, load_words(name) or {})
            if rules is None:
                print('已取消')
                return 1
            print(f'改词表 {name} 已存到 {save_words(name, rules)}')
            opts.word_rules.update(rules)
            opts.word_presets.append(name)
            continue
        if '=' in spec:
            k, _, v = spec.partition('=')
            opts.word_rules[k.strip()] = v.strip()
            continue
        rules = load_words(spec)
        if rules is None:
            print(f'没有改词表 {spec}\n{list_words()}', file=sys.stderr)
            return 1
        opts.word_rules.update(rules)
        opts.word_presets.append(spec)
    return None


# ---------------------------------------------------------------- 投毒：预设
def preset_dir() -> Path:
    return config_dir() / 'presets'


def preset_path(name: str) -> Path:
    return preset_dir() / f'{name}.json'


# 内置预设：都是「每块塞同一段」。自己存一个同编号的（nais -t edit 1）就盖过内置的
BUILTIN_PRESETS = {
    '1': ('空格', ' ' * 512),
    '2': ('杂鱼', ', '.join(['杂鱼~♥'] * 64)),
}


def load_preset(name: str) -> dict | None:
    """预设的内容：先找自己存的，没有再看内置；都没有返回 None。"""
    p = preset_path(name)
    if p.exists():
        return load_meta_json(p)
    if name in BUILTIN_PRESETS:
        return fill_meta(BUILTIN_PRESETS[name][1])
    return None


def _builtin_desc(name: str) -> str:
    label, text = BUILTIN_PRESETS[name]
    unit = text.split(', ')[0]
    what = f' {len(text)} 个半角空格' if not text.strip() else f'「{unit}」×{text.count(unit)}，逗号隔开'
    return f'内置 · {label}：每块塞{what}'


def save_preset(name: str, meta: dict) -> Path:
    preset_dir().mkdir(parents=True, exist_ok=True)
    p = preset_path(name)
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), 'utf-8')
    return p


def list_presets() -> str:
    files = {f.stem: f for f in preset_dir().glob('*.json')} if preset_dir().exists() else {}
    names = sorted({*BUILTIN_PRESETS, *files}, key=lambda x: (not x.isdigit(), len(x), x))
    lines = [f'预设（-t 编号；自己的存在 {preset_dir()}，nais -t edit 编号 新建或改）：']
    for n in names:
        if n not in files:
            lines.append(f'  {n:>4}  {_builtin_desc(n)}')
            continue
        try:
            m = load_meta_json(files[n])
            c = m.get('Comment') if isinstance(m.get('Comment'), dict) else {}
            prompt = str(c.get('prompt') or m.get('Description') or '').replace('\n', ' ')
            if not prompt.strip():
                prompt = f'（{len(prompt)} 个空白字符）' if prompt else '（空）'
            over = f'[盖过内置 {BUILTIN_PRESETS[n][0]}] ' if n in BUILTIN_PRESETS else ''
            lines.append(f'  {n:>4}  {over}{prompt[:70]}{"…" if len(prompt) > 70 else ""}')
        except Exception as e:
            lines.append(f'  {n:>4}  <坏了: {e}>')
    return '\n'.join(lines)


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
    if spec == 'edit' or spec.startswith(('edit:', 'edit ')):   # edit / edit:1 / edit 1
        from .edit import ask, edit_interactive
        name = spec[4:].lstrip(': ').strip() or None
        base = load_preset(name) if name else None
        if base is None and first is not None:
            base = _orig_meta(first)
        size = Image.open(first).size if first is not None else (0, 0)
        meta = edit_interactive(make_meta(base, sets=opts.sets, size=size), name)
        if meta is None:
            print('已取消')
            return 1
        if name is None:
            name = ask('存为预设编号 / 名字（留空不存）: ') or None
        if name:
            print(f'预设 {name} 已存到 {save_preset(name, meta)}')
        opts.poison_meta = make_meta(meta)
        return None
    if spec.startswith('@'):
        opts.poison_meta = make_meta(load_meta_json(Path(spec[1:]).expanduser()), sets=opts.sets)
        return None
    if spec.isdigit() or preset_path(spec).exists():     # 数字一律当预设；名字和现有预设撞上也当预设
        base = load_preset(spec)
        if base is None:
            print(f'没有预设 {spec}\n{list_presets()}', file=sys.stderr)
            return 1
        opts.poison_meta = make_meta(base, sets=opts.sets)
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
        if isinstance(c, dict):                  # 整段填充的预设 Comment 是原文，没有这些字段
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


def verify_words(dst: Path, rules: dict) -> list[str]:
    """回读所有文本层，确认旧词一个不剩（正则规则不查）。"""
    from .nai_inspect import inspect_file
    from .core import compile_rules
    rec = inspect_file(dst)
    texts = list((rec.get('text_chunks') or {}).values())
    if rec.get('stealth'):
        texts.append(rec['stealth']['raw'])
    for v in (rec.get('exif') or {}).values():
        if isinstance(v, str):
            texts.append(v)
    left = []
    for old, _new, pat in compile_rules(rules):
        if old.startswith('/'):
            continue
        if any(pat.search(t) for t in texts):
            left.append(f'仍有 {old}')
    return left


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
    try:
        found, done, notes = [], [], []
        scan = scan_png(src)
        with Image.open(src) as im:
            im.load()
            fmt = im.format or 'PNG'
            words_meta, hits = None, None
            if opts.word_rules:                  # -w：不剥，读出原元数据只换词，文件名里的词也换
                orig = _orig_meta(src, im, scan)
                if not is_nai(orig):
                    return True, f'· {src.name}   没有 NAI 元数据，改词没对象，不动'
                words_meta, hits = substitute_strings(make_meta(orig, sets=opts.sets, size=im.size), opts.word_rules)
                if not hits:
                    return True, f'· {src.name}   没命中任何词，不动'
                rel = rel.with_name(substitute_strings(rel.name, opts.word_rules)[0])
            dst = output_path(src, rel, opts)
            tag = f'{src.name} → {dst if opts.output or opts.outdir else dst.name}' if dst != src else f'{src.name} (原地)'
            if dst.exists() and dst != src and not opts.overwrite:
                return False, f'{SYM["bad"]} {tag}   输出已存在，跳过（--overwrite 可覆盖）'
            label = '改词写回' if words_meta else '写入投毒'
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

            poison = words_meta if words_meta is not None else poison_for(src, im, scan, opts)
            if fmt == 'JPEG':
                if opts.scrub_all:
                    notes.append('JPEG 不做像素处理')
                new_bytes, removed = strip_jpeg(src.read_bytes(), keep_icc=not opts.strip_icc)
                done += removed
                if poison:
                    new_bytes = jpeg_with_exif(new_bytes, meta_to_exif(poison))
                    done.append(f'{label}（EXIF）')
                writer = lambda p: p.write_bytes(new_bytes)  # noqa: E731
            elif fmt == 'WEBP':
                writer, done_w, notes_w = plan_webp(src, im, opts, poison, label)
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
                    done.append(f'{label}（文本块+隐写）')
                elif poison:
                    notes.append(f'{fmt} 不写投毒')
                    poison = None
                writer = lambda p: save_pixels(clean, im, p, fmt, opts, meta=poison)  # noqa: E731
            found += [d for d in done if d.startswith('隐写')]

        if not found and not done:
            if opts.in_place and dst != src:     # -i -N：没元数据也把名字换掉
                if not opts.dry_run:
                    os.replace(src, dst)
                return True, f'· {tag}   没发现元数据，只改名'
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
            left = [f'回读失败: {", ".join(x)}' for x in [verify_poison(dst, poison)] if x]
            if words_meta is not None:
                left += verify_words(dst, opts.word_rules)
        else:
            left = verify(dst)
        size = f'{fmt_size(src.stat().st_size)} → {fmt_size(dst.stat().st_size)}' if dst != src else fmt_size(dst.stat().st_size)
        if opts.in_place and dst != src:         # -i -N：新名字那份写好了，旧文件删掉
            src.unlink()
        if words_meta is not None:
            hit_desc = ' · '.join(f'{k} ×{n}' for k, n in hits.items())
            line = f'{SYM["ok"]} {tag}   改词: {hit_desc} · {[d for d in done if d.startswith(label)][0] if any(d.startswith(label) for d in done) else label}   {size}'
        else:
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
    ap.add_argument('-N', '--rename', action='store_true', help=(
        '输出改名成 今天日期-编号（20260923-0001…），同一目录接着已有的号往后排。NAI 默认文件名开头是提示词、结尾是种子，'
        '擦了元数据名字照样漏；配 -i 就是原地改名，旧文件删掉'))
    ap.add_argument('-t', '--poison', metavar='内容', help=(
        "剥完再写入假元数据（投毒，文本块/EXIF 与隐写两层都写）：-t '内容' 每个分块都塞这段；"
        '-t 1 内置预设「空格」，-t 2 内置预设「杂鱼~♥」×64；-t 3 起是自己的预设；'
        '-t edit 3 在终端里逐字段改并存为预设 3（edit 1/2 就是改内置那两个）；-t @文件.json 用现成模板；-t list 列预设'))
    ap.add_argument('--set', action='append', default=[], metavar='键=值',
                    help='改单个字段，可重复：--set seed=7 --set uc=lowres；单独用时以原图元数据为底')
    ap.add_argument('-w', '--words', action='append', default=[], metavar='规则|词表', help=(
        '不剥元数据，只把命中的词换掉再写回两层（应付平台内容政策）：-w loli=1011 单条规则；-w discord 用词表'
        '（内置 discord = loli→1011）；-w edit discord 终端里改词表；-w list 列词表。可重复叠加。输出文件名里的词也一并换'))
    ap.add_argument('--drop-alpha', action='store_true', help='alpha 全不透明时去掉 alpha 通道存成 RGB，文件更小')
    ap.add_argument('--scrub-all', action='store_true', help='清掉所有通道所有像素的最低位（应付未知隐写变种；颜色最多变 1/255）')
    ap.add_argument('--strip-icc', action='store_true', help='连 ICC 色彩配置也去掉（默认保留，它不含生成信息）')
    ap.add_argument('--overwrite', action='store_true', help='输出文件已存在时覆盖（默认跳过）')
    ap.add_argument('--no-verify', action='store_true', help='写完不回读验证')
    ap.add_argument('-n', '--dry-run', action='store_true', help='只报告会做什么，不写文件')
    ap.add_argument('-y', '--yes', action='store_true', help='处理文件夹 / 通配符时不问 y/N（脚本里用）')
    a = ap.parse_intermixed_args(argv)
    a.sets = a.set
    if a.words and a.poison:
        print('-w（只改词）和 -t（投毒）是两种模式，不能同时用', file=sys.stderr)
        return 1
    if a.words and a.paths and a.words[-1] == 'edit':      # nais -w edit discord：名字被当成了路径
        for i in range(len(a.paths) - 1, -1, -1):
            if not Path(a.paths[i]).exists() and re.fullmatch(r'[\w-]+', a.paths[i]):
                a.words[-1] = f'edit:{a.paths.pop(i)}'
                break
    if a.poison == 'edit' and a.paths:               # nais -t edit 1：编号被当成了路径，捞回来
        for i in range(len(a.paths) - 1, -1, -1):
            if not Path(a.paths[i]).exists() and re.fullmatch(r'[\w-]+', a.paths[i]):
                a.poison = f'edit:{a.paths.pop(i)}'
                break

    items = list(iter_images(a.paths, a.recursive))
    try:
        rc = resolve_words(a)
        if rc is None:
            rc = resolve_poison(a, items[0][0] if items else None)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f'参数有问题: {e}', file=sys.stderr)
        return 1
    if rc is not None:
        return rc
    if not items:
        if a.poison_meta is not None or (a.words and any(w.startswith('edit') for w in a.words)):
            return 0                              # 只是编辑预设 / 词表
        print('没有找到图片', file=sys.stderr)
        return 1
    if a.output and len(items) > 1:
        print('-o 只能配一个输入文件；多个文件请用 -d 输出目录', file=sys.stderr)
        return 1
    if a.output and a.rename:
        print('-o 已经指定了文件名，不能再 -N 改名', file=sys.stderr)
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
    hint = name_hint(items, a)
    if hint:
        print(hint)
    return 1 if fail else 0


def name_hint(items, opts) -> str:
    """文件名是 NAI 默认命名（提示词开头 + s-种子）而又没改名时提醒一句。"""
    if opts.rename or opts.output:
        return ''
    n = sum(bool(NAI_NAME.search(src.stem)) for src, _ in items)
    if not n:
        return ''
    return (f'{SYM["warn"]} {"这张" if len(items) == 1 else f"其中 {n} 张"}的文件名是 NAI 默认命名（开头是提示词、结尾 s-种子），'
            f'元数据擦了名字还在；加 -N 改成 {today()}-0001 这种')


if __name__ == '__main__':
    sys.exit(main())
