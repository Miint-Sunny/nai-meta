# -*- coding: utf-8 -*-
"""nai-strip：移除 NovelAI 图片中的元数据，不修改像素数据。

各格式的处理方式：

PNG   移除全部文本块（tEXt、iTXt、zTXt）、eXIf 与 tIME，清除 LSB 隐写后无损重新编码。
      隐写占用区域内 alpha 为 254 的像素恢复为 255，其余像素保持原值。
JPEG  按段过滤：移除 APP1（EXIF、XMP）、APP13（Photoshop、IPTC）、COM 等，保留 APP0（JFIF）、
      APP14（Adobe 色彩变换标记）及可选的 APP2（ICC）。扫描数据不做改动，不重新编码。
WebP  无损（VP8L）：按 PNG 的方式处理像素后无损重新编码。
      有损（VP8）且 alpha 完全不透明：在 RIFF 容器层移除 ALPH、EXIF、XMP 块，VP8 数据不做改动。
      有损且含透明像素：只能有损重新编码。
其他  由 Pillow 重新编码，画质会有损失。

另有两种模式：``-t`` 在移除后写入伪造元数据（投毒）；``-w`` 不移除元数据，仅替换其中的指定词语后写回。
"""
from __future__ import annotations

import copy
import json
import os
import random
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

from . import __version__
from .argparse_zh import ArgumentParser
from .core import (GLOB_CHARS, SYM, confirm, config_dir, embed_stealth, error, expand_comment, fill_meta,
                   find_stealth, fmt_size, is_nai, iter_images, load_meta_json, make_meta, meta_to_exif, meta_to_text,
                   parse_set, scan_png, setup_console, stealth_payload, substitute_strings, warn, wipe_stealth)

REPO_URL = 'https://github.com/Miint-Sunny/nai-meta'


# ---------------------------------------------------------------- PNG 与通用格式（像素处理）
def clean_pixels(im: Image.Image, opts) -> tuple[Image.Image, list[str], list[str], list[str]]:
    """清除像素中的隐写数据。

    Returns:
        (image, removed, actions, notes)：不含 info 的新图像、已移除内容、其他已执行操作、需提示的情况。
        保存时须显式传入要写入的元数据。
    """
    removed, actions, notes = [], [], []
    if im.mode not in ('RGB', 'RGBA'):
        notes.append(f'{im.mode} 模式不支持隐写检测')
        clean = im.copy()
        for k in ('exif', 'xmp', 'comment', 'dpi'):
            clean.info.pop(k, None)
        return clean, removed, actions, notes

    arr = np.array(im)                           # np.array 复制数据，可写
    st = find_stealth(im)
    restored = 0
    if st:
        restored = wipe_stealth(arr, st.channel, st.used_bits)
        removed.append(f'LSB 隐写（{st.describe()}）')
    if opts.scrub_all:
        arr[:, :, :3] &= 0xFE
        if im.mode == 'RGBA':                    # alpha 采用与隐写区域相同的规则
            a = arr[:, :, 3]
            restored += int(np.count_nonzero(a == 254))
            arr[:, :, 3] = np.where(a >= 254, 255, a & 0xFE)
        actions.append('所有通道最低位已清零')
    if im.mode == 'RGBA':
        a = arr[:, :, 3]
        if a.min() >= 254 and (a != 255).any():  # 完全不透明但最低位被修改过，来源未被识别
            restored += int(np.count_nonzero(a == 254))
            a[:] = 255
        if restored:
            actions.append('alpha 已恢复为 255')
        if opts.drop_alpha:
            if (a == 255).all():
                arr = arr[:, :, :3]
                actions.append('已移除 alpha 通道')
            else:
                notes.append('alpha 通道含透明像素，已保留')
    return Image.fromarray(arr), removed, actions, notes


def save_pixels(clean: Image.Image, im: Image.Image, dst: Path, fmt: str, opts,
                lossless: bool = False, meta: dict | None = None) -> None:
    """保存处理后的图像。

    Args:
        clean: 待保存的图像。
        im: 原图像，用于取 ICC 配置文件和调色板透明色。
        meta: 要写入明文层的元数据（PNG 写入文本块，WebP 写入 EXIF）；隐写数据须已写入像素。
    """
    kw = {'icc_profile': None if opts.strip_icc else im.info.get('icc_profile')}
    if clean.mode == im.mode and 'transparency' in im.info:
        kw['transparency'] = im.info['transparency']
    if fmt == 'WEBP':
        if lossless:
            kw['lossless'] = True
        else:                                    # alpha 无损压缩，并保留透明像素下的 RGB 值
            kw.update(quality=95, alpha_quality=100, exact=True)
    if meta:
        if fmt == 'PNG':
            info = PngInfo()
            for k, v in meta_to_text(meta).items():
                info.add_text(k, v)
            kw['pnginfo'] = info
        elif fmt == 'WEBP':
            kw['exif'] = meta_to_exif(meta)
    clean.save(dst, format=fmt, **kw)


def poison_pixels(clean: Image.Image, meta: dict) -> Image.Image:
    """将元数据写入 alpha 通道的隐写层。没有 alpha 通道的图像先补一个完全不透明的 alpha 通道。"""
    if clean.mode != 'RGBA':
        clean = clean.convert('RGBA')
    arr = np.array(clean)
    embed_stealth(arr, stealth_payload(meta))
    return Image.fromarray(arr)


# ---------------------------------------------------------------- JPEG（按段处理，不重新编码）
APP_NAMES = {0xE0: 'APP0/JFIF', 0xE1: 'APP1/EXIF-XMP', 0xE2: 'APP2/ICC', 0xEC: 'APP12',
             0xED: 'APP13/Photoshop', 0xEE: 'APP14/Adobe', 0xFE: 'COM'}


def strip_jpeg(data: bytes, keep_icc: bool) -> tuple[bytes, list[str]]:
    """移除 JPEG 中的元数据段。

    保留 APP0（JFIF）、APP14（Adobe 色彩变换标记，移除会导致偏色）和可选的 APP2（ICC），
    移除其余 APPn 段与 COM 段。从 SOS 起的数据原样复制。

    Returns:
        (新文件内容, 已移除段的说明)。

    Raises:
        ValueError: 数据不是 JPEG 或结构损坏。
    """
    if data[:2] != b'\xff\xd8':
        raise ValueError('不是有效的 JPEG 文件')
    out = bytearray(b'\xff\xd8')
    removed = []
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            raise ValueError(f'JPEG 结构异常（偏移 {pos}）')
        m = data[pos + 1]
        if m == 0xFF:                            # 填充字节
            pos += 1
            continue
        if m == 0xDA or m == 0xD9:               # SOS 之后均为扫描数据；EOI 为文件结尾
            out += data[pos:]
            break
        if m == 0xD8 or 0xD0 <= m <= 0xD7 or m == 0x01:   # 不带长度字段的标记
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
            removed.append(f'{APP_NAMES.get(m, f"APP{m - 0xE0}")} 段（{fmt_size(ln)}）')
        else:
            out += seg
        pos += 2 + ln
    return bytes(out), removed


def jpeg_with_exif(data: bytes, exif: bytes) -> bytes:
    """在 SOI 之后插入 APP1 EXIF 段，扫描数据不变。exif 须以 ``Exif\\0\\0`` 开头。"""
    return data[:2] + b'\xff\xe1' + struct.pack('>H', len(exif) + 2) + exif + data[2:]


# ---------------------------------------------------------------- WebP（容器层）
WEBP_META = {b'EXIF': 'EXIF', b'XMP ': 'XMP', b'ICCP': 'ICCP', b'ALPH': 'ALPH'}


def webp_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """解析 WebP 的 RIFF 块列表，返回 (块类型, 内容)。

    Raises:
        ValueError: 数据不是 WebP。
    """
    if data[:4] != b'RIFF' or data[8:12] != b'WEBP':
        raise ValueError('不是有效的 WebP 文件')
    out, pos = [], 12
    while pos + 8 <= len(data):
        tag, ln = data[pos:pos + 4], struct.unpack('<I', data[pos + 4:pos + 8])[0]
        out.append((tag, data[pos + 8:pos + 8 + ln]))
        pos += 8 + ln + (ln & 1)                 # 奇数长度的块后有一个填充字节
    return out


def build_webp(chunks) -> bytes:
    """由块列表生成 WebP 文件内容。"""
    body = b''.join(tag + struct.pack('<I', len(p)) + p + (b'\0' if len(p) & 1 else b'')
                    for tag, p in chunks)
    return b'RIFF' + struct.pack('<I', 4 + len(body)) + b'WEBP' + body


def strip_webp_container(data: bytes, keep_icc: bool, drop_alpha: bool) -> tuple[bytes, list[str]]:
    """在容器层移除 EXIF 与 XMP 块（可选移除 ICCP、ALPH 块），并更新 VP8X 标志位。图像数据不变。

    Returns:
        (新文件内容, 已移除块的说明)。
    """
    kept, removed = [], []
    for tag, p in webp_chunks(data):
        if tag in (b'EXIF', b'XMP ') or (tag == b'ICCP' and not keep_icc) or (tag == b'ALPH' and drop_alpha):
            removed.append(f'{WEBP_META[tag]} 块（{fmt_size(len(p))}）')
        else:
            kept.append((tag, p))
    tags = {t for t, _ in kept}
    out = []
    for tag, p in kept:
        if tag == b'VP8X':                       # 标志位：ICC 0x20、Alpha 0x10、EXIF 0x08、XMP 0x04、动画 0x02
            flags = p[0] & ~(0x08 | 0x04)
            if b'ICCP' not in tags:
                flags &= ~0x20
            if b'ALPH' not in tags and b'ANIM' not in tags:
                flags &= ~0x10
            if flags == 0:                       # 不再使用扩展特性时改用简单格式，省略 VP8X
                continue
            p = bytes([flags]) + p[1:]
        out.append((tag, p))
    return build_webp(out), removed


def webp_with_exif(data: bytes, exif: bytes, size: tuple[int, int]) -> bytes:
    """向 WebP 容器添加 EXIF 块（去掉 ``Exif\\0\\0`` 头）；缺少 VP8X 块时一并添加。"""
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


def plan_webp(src: Path, im: Image.Image, opts, found: list[str], poison: dict | None = None,
              label: str = '已写入伪造元数据'):
    """确定 WebP 的处理方式。

    - VP8L（无损）：按像素处理，无损重新编码。
    - VP8（有损）且 alpha 完全不透明：在容器层移除 ALPH、EXIF、XMP 块，VP8 数据不变。
      NovelAI 的有损 WebP 即为此类，隐写数据位于无损压缩的 ALPH 块中。
    - VP8（有损）且含透明像素：只能有损重新编码。

    Args:
        found: 已检测到的容器级元数据，像素处理路线使用。
        poison: 要写入的元数据；为 None 时仅移除。
        label: 写入元数据时报告的操作名称。

    Returns:
        (writer, removed, actions, notes)：writer 接收输出路径并写入文件。
    """
    raw = src.read_bytes()
    tags = {t for t, _ in webp_chunks(raw)}
    removed, actions, notes = [], [], []

    def container(new: bytes, chunk_removed: list[str]):
        if poison:                               # 有损 WebP 没有可写入隐写的 alpha，仅写入 EXIF
            new = webp_with_exif(new, meta_to_exif(poison), im.size)
            actions.append(f'{label}（EXIF）')
        removed.extend(chunk_removed)
        return lambda p: p.write_bytes(new)

    def pixels(lossless: bool):
        clean, rm, act, nt = clean_pixels(im, opts)
        if poison:
            clean = poison_pixels(clean, poison)
            act.append(f'{label}（EXIF、隐写）')
        writer = lambda p: save_pixels(clean, im, p, 'WEBP', opts, lossless=lossless, meta=poison)  # noqa: E731
        return writer, found + rm, act, notes + nt

    if b'ANIM' in tags:
        new, chunk_removed = strip_webp_container(raw, keep_icc=not opts.strip_icc, drop_alpha=False)
        notes.append('动图仅移除容器层元数据，未检测隐写')
        return container(new, chunk_removed), removed, actions, notes
    if b'VP8L' in tags:
        return pixels(lossless=True)
    has_alpha = b'ALPH' in tags
    if has_alpha:
        alpha = np.asarray(im.convert('RGBA'))[:, :, 3]
        if alpha.min() < 254:
            notes.append('有损 WebP 含透明像素，已按有损方式重新编码')
            return pixels(lossless=False)
        st = find_stealth(im)
        if st:
            removed.append(f'LSB 隐写（{st.describe()}）')
    if opts.scrub_all:
        notes.append('有损 WebP 不进行像素处理，--scrub-all 未生效')
    new, chunk_removed = strip_webp_container(raw, keep_icc=not opts.strip_icc, drop_alpha=has_alpha)
    return container(new, chunk_removed), removed, actions, notes


# ---------------------------------------------------------------- 选项与输出路径
DEFAULTS = dict(paths=[], recursive=False, output=None, outdir=None, in_place=False, suffix=None,
                drop_alpha=False, scrub_all=False, strip_icc=False, overwrite=False, no_verify=False,
                dry_run=False, yes=False, poison=None, sets={}, poison_meta=None,
                words=[], word_rules={}, word_presets=[], rename=False)


def make_opts(**overrides) -> Namespace:
    """创建与命令行解析结果结构相同的选项对象，供 TUI 与测试使用。"""
    return Namespace(**{**DEFAULTS, **overrides})


def describe_plan(items, opts, label: str) -> str:
    """生成批量处理前的确认摘要，包括文件数、格式和输出位置。"""
    ext = Counter(f.suffix.lower().lstrip('.') for f, _ in items)
    kinds = '、'.join(f'{k} {n}' for k, n in ext.most_common())
    if opts.in_place:
        dest = '直接修改源文件（不保留原文件）' + ('，并按“日期-序号”重命名' if opts.rename else '')
    elif opts.output:
        dest = f'输出到 {opts.output}'
    elif opts.outdir:
        dest = f'输出到目录 {opts.outdir}' + ('，按“日期-序号”命名' if opts.rename else '')
    else:
        dest = '输出到源文件所在目录，' + ('按“日期-序号”命名' if opts.rename else f'文件名追加 {suffix_of(opts)}')
    return f'{label}：共 {len(items)} 个文件（{kinds}），{dest}'


def today() -> str:
    """返回本地日期，格式 YYYYMMDD。"""
    return time.strftime('%Y%m%d')


DATE_NAME = re.compile(r'\d{8}-\d{4,}')          # -N 生成的文件名，如 20260929-0001
NAI_NAME = re.compile(r' s-\d+$')                # NovelAI 默认文件名的结尾：“ s-<种子>”


def next_name(opts, folder: Path, ext: str) -> Path:
    """返回 folder 中下一个“日期-序号”文件名（``-N``）。

    序号接续该目录中当天已有的最大序号，不区分扩展名，以免 0001.png 与 0001.webp 并存。
    已分配的序号记录在 opts 中，因此试运行报告的文件名与实际运行一致。
    """
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
    """根据选项确定输出路径。rel 为相对路径，``-w`` 模式下其文件名已完成词语替换。"""
    if opts.in_place:
        if opts.rename and not DATE_NAME.fullmatch(src.stem):     # 已是“日期-序号”格式的文件保留原名
            return next_name(opts, src.parent, src.suffix)
        return src
    if opts.output:
        return Path(opts.output)
    if opts.rename:
        return next_name(opts, Path(opts.outdir) / rel.parent if opts.outdir else src.parent, src.suffix)
    if opts.outdir:
        return Path(opts.outdir) / rel
    return src.with_name(Path(rel.name).stem + suffix_of(opts) + src.suffix)


def suffix_of(opts) -> str:
    """返回写在源文件所在目录时使用的文件名后缀。"""
    if opts.suffix:
        return opts.suffix
    if opts.word_rules:                          # 仅使用一个词表时以词表名作后缀，如 a_discord.png
        return f'_{opts.word_presets[0]}' if len(opts.word_presets) == 1 and len(opts.words) == 1 else '_w'
    return '_poison' if (opts.poison or opts.sets) else '_clean'


# ---------------------------------------------------------------- 词语替换（-w）与词表
BUILTIN_WORDS = {'discord': {'loli': '1011'}}


def words_dir() -> Path:
    return config_dir() / 'words'


def words_path(name: str) -> Path:
    return words_dir() / f'{name}.txt'


def parse_words_text(text: str) -> dict:
    """解析词表文件：每行一条 ``OLD=NEW``，忽略空行和以 # 开头的行。"""
    rules = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith('#') or '=' not in ln:
            continue
        k, _, v = ln.partition('=')
        rules[k.strip()] = v.strip()
    return rules


def load_words(name: str) -> dict | None:
    """读取词表。用户词表优先于同名的内置词表；都不存在时返回 None。"""
    if words_path(name).exists():
        return parse_words_text(words_path(name).read_text('utf-8'))
    return dict(BUILTIN_WORDS[name]) if name in BUILTIN_WORDS else None


def save_words(name: str, rules: dict) -> Path:
    """保存词表，返回文件路径。"""
    words_dir().mkdir(parents=True, exist_ok=True)
    p = words_path(name)
    p.write_text('# 每行一条规则，格式为 OLD=NEW。按子串匹配，不区分大小写；OLD 写作 /正则表达式/ 时按正则匹配。\n'
                 + ''.join(f'{k}={v}\n' for k, v in rules.items()), 'utf-8')
    return p


def list_words() -> str:
    """返回全部词表的列表文本。"""
    user = {p.stem for p in words_dir().glob('*.txt')} if words_dir().exists() else set()
    lines = [f'词表（用户词表目录：{words_dir()}）：']
    for n in sorted(set(BUILTIN_WORDS) | user):
        rules = load_words(n) or {}
        origin = '' if n in user else '[内置] '
        lines.append(f'  {n:<10} {origin}' + '、'.join(f'{k}→{v}' for k, v in rules.items()))
    return '\n'.join(lines)


def resolve_words(opts) -> int | None:
    """解析全部 ``-w`` 参数并合并为 opts.word_rules。

    每个参数可以是 ``OLD=NEW``、词表名称、``list`` 或 ``edit NAME``（也可写作 ``edit:NAME``）。

    Returns:
        None 表示继续处理；整数表示应以该退出码结束。
    """
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
                error('-w edit 需要指定词表名称', '例如 nais -w edit discord')
                return 1
            rules = edit_words_interactive(name, load_words(name) or {})
            if rules is None:
                print('已取消')
                return 1
            print(f'词表 {name} 已保存：{save_words(name, rules)}')
            opts.word_rules.update(rules)
            opts.word_presets.append(name)
            continue
        if '=' in spec:
            k, _, v = spec.partition('=')
            opts.word_rules[k.strip()] = v.strip()
            continue
        rules = load_words(spec)
        if rules is None:
            error(f'词表 {spec} 不存在', '使用 nais -w list 查看可用词表')
            return 1
        opts.word_rules.update(rules)
        opts.word_presets.append(spec)
    return None


# ---------------------------------------------------------------- 伪造元数据（-t）与预设
def preset_dir() -> Path:
    return config_dir() / 'presets'


def preset_path(name: str) -> Path:
    return preset_dir() / f'{name}.json'


# 内置预设：编号 → (名称, 写入所有字段的文本)。用户预设目录中的同名文件优先。
BUILTIN_PRESETS = {
    '1': ('空格', ' ' * 512),
    '2': ('杂鱼', ', '.join(['杂鱼~♥'] * 64)),
}


def load_preset(name: str) -> dict | None:
    """读取预设。用户预设优先于同编号的内置预设；都不存在时返回 None。"""
    p = preset_path(name)
    if p.exists():
        return load_meta_json(p)
    if name in BUILTIN_PRESETS:
        return fill_meta(BUILTIN_PRESETS[name][1])
    return None


def _builtin_desc(name: str) -> str:
    label, text = BUILTIN_PRESETS[name]
    if not text.strip():
        return f'{label}：所有字段写入 {len(text)} 个半角空格'
    unit = text.split(', ')[0]
    return f'{label}：所有字段写入“{unit}”×{text.count(unit)}，以逗号分隔'


def save_preset(name: str, meta: dict) -> Path:
    """保存预设，返回文件路径。"""
    preset_dir().mkdir(parents=True, exist_ok=True)
    p = preset_path(name)
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), 'utf-8')
    return p


def list_presets() -> str:
    """返回全部预设的列表文本。"""
    files = {f.stem: f for f in preset_dir().glob('*.json')} if preset_dir().exists() else {}
    names = sorted({*BUILTIN_PRESETS, *files}, key=lambda x: (not x.isdigit(), len(x), x))
    lines = [f'预设（用户预设目录：{preset_dir()}）：']
    for n in names:
        if n not in files:
            lines.append(f'  {n:>4}  [内置] {_builtin_desc(n)}')
            continue
        try:
            m = load_meta_json(files[n])
            c = m.get('Comment') if isinstance(m.get('Comment'), dict) else {}
            prompt = str(c.get('prompt') or m.get('Description') or '').replace('\n', ' ')
            if not prompt.strip():
                prompt = f'（{len(prompt)} 个空白字符）' if prompt else '（空）'
            over = f'[覆盖内置预设“{BUILTIN_PRESETS[n][0]}”] ' if n in BUILTIN_PRESETS else ''
            lines.append(f'  {n:>4}  {over}{prompt[:70]}{"…" if len(prompt) > 70 else ""}')
        except Exception as e:
            lines.append(f'  {n:>4}  <无法读取：{e}>')
    lines.append('使用 nais -t edit N 新建或修改预设 N')
    return '\n'.join(lines)


def resolve_poison(opts, first: Path | None = None) -> int | None:
    """解析 ``-t`` 与 ``--set``，结果写入 opts.poison_meta 与 opts.sets。

    ``-t`` 的取值：

    - ``list``：列出预设。
    - ``edit`` / ``edit N``（也可写作 ``edit:N``）：在终端中编辑，以预设 N 或 first 的元数据为基础，并保存为预设。
    - ``@FILE``：读取 JSON 模板。
    - 数字，或与已有预设同名的文本：使用该预设。
    - 其他文本：所有字段写入该文本（由 :func:`poison_for` 逐个文件生成）。

    Returns:
        None 表示继续处理；整数表示应以该退出码结束。
    """
    opts.sets = dict(parse_set(x) for x in (opts.sets or [])) if isinstance(opts.sets, list) else (opts.sets or {})
    opts.poison_meta = None
    spec = opts.poison
    if spec is None:
        return None
    if spec == 'list':
        print(list_presets())
        return 0
    if spec == 'edit' or spec.startswith(('edit:', 'edit ')):
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
            name = ask('保存为预设（输入编号或名称，留空则不保存）：') or None
        if name:
            print(f'预设 {name} 已保存：{save_preset(name, meta)}')
        opts.poison_meta = make_meta(meta)
        return None
    if spec.startswith('@'):
        opts.poison_meta = make_meta(load_meta_json(Path(spec[1:]).expanduser()), sets=opts.sets)
        return None
    if spec.isdigit() or preset_path(spec).exists():
        base = load_preset(spec)
        if base is None:
            error(f'预设 {spec} 不存在', '使用 nais -t list 查看可用预设')
            return 1
        opts.poison_meta = make_meta(base, sets=opts.sets)
    return None


def _orig_meta(src: Path, im: Image.Image | None = None, scan=None) -> dict | None:
    """读取源文件自带的 NovelAI 元数据，依次查找文本块、隐写层和 EXIF；未找到时返回 None。"""
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
    """返回要写入该文件的元数据；不写入时返回 None。

    使用预设或模板时，宽和高按该图像的实际尺寸设置；seed 为空时生成随机值。
    """
    if opts.poison_meta is not None:
        meta = copy.deepcopy(opts.poison_meta)
        c = meta['Comment']
        if isinstance(c, dict):                  # 纯文本预设的 Comment 不含这些字段
            c['width'], c['height'] = im.size
            if c.get('seed') is None:
                c['seed'] = random.randrange(1, 2 ** 32)
        return meta
    if opts.poison:
        return fill_meta(opts.poison, opts.sets)
    if opts.sets:                                # 仅使用 --set：以源文件元数据为基础修改字段
        return make_meta(_orig_meta(src, im, scan), sets=opts.sets, size=im.size)
    return None


# ---------------------------------------------------------------- 写入后验证
def verify_poison(dst: Path, meta: dict) -> list[str]:
    """重新读取输出文件，检查写入的 Description 能否从明文层与隐写层读出。返回问题列表。"""
    from .nai_inspect import inspect_file
    rec = inspect_file(dst)
    want = str(meta.get('Description', ''))
    layers = []
    outer = rec.get('text_meta') or rec.get('exif_meta') or {}
    if str(outer.get('Description', '')) != want:
        layers.append('明文层')
    if dst.suffix.lower() not in ('.jpg', '.jpeg') and rec.get('mode') == 'RGBA':
        stm = (rec.get('stealth') or {}).get('meta') or {}
        if str(stm.get('Description', '')) != want:
            layers.append('隐写层')
    return [f'回读内容与写入内容不一致（{"、".join(layers)}）'] if layers else []


def verify_words(dst: Path, rules: dict) -> list[str]:
    """重新读取输出文件的全部文本层，检查被替换的词语是否已不存在。正则规则不检查。返回问题列表。"""
    from .core import compile_rules
    from .nai_inspect import inspect_file
    rec = inspect_file(dst)
    texts = list((rec.get('text_chunks') or {}).values())
    if rec.get('stealth'):
        texts.append(rec['stealth']['raw'])
    for v in (rec.get('exif') or {}).values():
        if isinstance(v, str):
            texts.append(v)
    left = [old for old, _new, pat in compile_rules(rules)
            if not old.startswith('/') and any(pat.search(t) for t in texts)]
    return [f'输出文件仍含 {"、".join(left)}'] if left else []


def verify(dst: Path) -> list[str]:
    """重新读取输出文件，检查元数据是否已全部移除。返回问题列表，为空表示通过。"""
    left = []
    scan = scan_png(dst)
    if scan:
        if scan.texts:
            left.append('文本块（' + '、'.join(scan.texts) + '）')
        if scan.exif:
            left.append('eXIf 块')
        if 'tIME' in scan.chunks:
            left.append('tIME 块')
    with Image.open(dst) as im:
        im.load()
        if im.getexif():
            left.append('EXIF')
        for k, label in (('xmp', 'XMP'), ('comment', '注释'), ('photoshop', 'Photoshop 数据')):
            if im.info.get(k):
                left.append(label)
        if im.format != 'JPEG' and find_stealth(im):
            left.append('LSB 隐写')
    return [f'输出文件仍含 {"、".join(left)}'] if left else []


# ---------------------------------------------------------------- 单个文件
def _removed_phrase(removed: list[str]) -> str:
    items = '、'.join(removed)
    return f'已移除{" " if items[:1].isascii() else ""}{items}'


def strip_one(src: Path, rel: Path, opts) -> tuple[str, str]:
    """处理单个文件。

    Returns:
        (status, line)：status 为 ``ok``、``skip`` 或 ``fail``；line 为报告行。
    """
    name = src.name
    try:
        found, removed, actions, notes = [], [], [], []
        scan = scan_png(src)
        with Image.open(src) as im:
            im.load()
            fmt = im.format or 'PNG'
            words_meta, hits = None, None
            if opts.word_rules:                  # -w：读取源文件元数据，替换词语后写回，不做移除
                orig = _orig_meta(src, im, scan)
                if not is_nai(orig):
                    return 'skip', f'· {name}  已跳过：未检测到 NovelAI 元数据'
                words_meta, hits = substitute_strings(make_meta(orig, sets=opts.sets, size=im.size), opts.word_rules)
                if not hits:
                    return 'skip', f'· {name}  已跳过：未匹配任何替换规则'
                rel = rel.with_name(substitute_strings(rel.name, opts.word_rules)[0])
            dst = output_path(src, rel, opts)
            tag = f'{name} → {dst if opts.output or opts.outdir else dst.name}' if dst != src else f'{name}（原地修改）'
            if dst.exists() and dst != src and not opts.overwrite:
                return 'skip', f'· {name}  已跳过：输出文件 {dst.name} 已存在（使用 --overwrite 覆盖）'
            label = '已写回替换后的元数据' if words_meta else '已写入伪造元数据'
            if scan:
                if scan.texts:
                    found.append(f"文本块 ×{len(scan.texts)}（{'、'.join(scan.texts)}）")
                for t in ('eXIf', 'tIME'):
                    if t in scan.chunks:
                        found.append(f'{t} 块')
                if scan.bit_depth == 16:
                    notes.append('16 位 PNG 输出为 8 位')
            elif im.getexif():
                found.append('EXIF')
            for k, text in (('xmp', 'XMP'), ('comment', '注释'), ('photoshop', 'Photoshop 数据')):
                if im.info.get(k):
                    found.append(text)

            poison = words_meta if words_meta is not None else poison_for(src, im, scan, opts)
            if fmt == 'JPEG':
                if opts.scrub_all:
                    notes.append('JPEG 不进行像素处理，--scrub-all 未生效')
                new_bytes, removed = strip_jpeg(src.read_bytes(), keep_icc=not opts.strip_icc)
                if poison:
                    new_bytes = jpeg_with_exif(new_bytes, meta_to_exif(poison))
                    actions.append(f'{label}（EXIF）')
                writer = lambda p: p.write_bytes(new_bytes)  # noqa: E731
            elif fmt == 'WEBP':
                writer, removed, actions, notes_w = plan_webp(src, im, opts, found, poison, label)
                notes += notes_w
            else:
                if fmt != 'PNG':
                    notes.append(f'{fmt} 格式需要重新编码，画质会有损失')
                clean, removed_px, actions, notes_px = clean_pixels(im, opts)
                removed = found + removed_px
                notes += notes_px
                if poison and fmt == 'PNG':
                    clean = poison_pixels(clean, poison)
                    actions.append(f'{label}（文本块、隐写）')
                elif poison:
                    notes.append(f'{fmt} 格式不支持写入元数据，{"-w" if words_meta else "-t"} 未生效')
                    poison = None
                writer = lambda p: save_pixels(clean, im, p, fmt, opts, meta=poison)  # noqa: E731

        note = f'（注意：{"；".join(notes)}）' if notes else ''
        if not removed and not actions:
            if opts.in_place and dst != src:     # -i -N：没有元数据时仍执行重命名
                if opts.dry_run:
                    return 'ok', f'· {tag}  [试运行] 未检测到元数据，将重命名'
                os.replace(src, dst)
                return 'ok', f'{SYM["ok"]} {tag}  未检测到元数据，已重命名'
            if opts.in_place:
                return 'skip', f'· {name}  已跳过：未检测到元数据'
            if opts.dry_run:
                return 'ok', f'· {tag}  [试运行] 未检测到元数据，将写出副本{note}'
            notes.append('未检测到元数据，已写出副本')
            note = f'（注意：{"；".join(notes)}）'

        if opts.dry_run:                         # 操作说明由“已…”改为“将…”
            plan = ([_removed_phrase(removed)] if removed else []) + actions
            return 'ok', f'· {tag}  [试运行] ' + '；'.join(x.replace('已', '将', 1) for x in plan) + note

        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + '.tmp~')
        writer(tmp)
        os.replace(tmp, dst)

        if opts.no_verify:
            problems = []
        elif poison:
            problems = verify_poison(dst, poison)
            if words_meta is not None:
                problems += verify_words(dst, opts.word_rules)
        else:
            problems = verify(dst)
        size = f'{fmt_size(src.stat().st_size)} → {fmt_size(dst.stat().st_size)}' if dst != src else fmt_size(dst.stat().st_size)
        if opts.in_place and dst != src:         # -i -N：新文件已写入，删除源文件
            src.unlink()
        if problems:
            return 'fail', f'{SYM["bad"]} {tag}  验证失败：{"；".join(problems)}；{size}{note}'
        if words_meta is not None:
            parts = ['已替换 ' + '、'.join(f'{k} ×{n}' for k, n in hits.items())]
        else:
            parts = [_removed_phrase(removed)] if removed else []
        return 'ok', f'{SYM["ok"]} {tag}  ' + '；'.join(parts + actions + [size]) + note
    except Exception as e:
        return 'fail', f'{SYM["bad"]} {name}  处理失败：{e}'


def name_hint(items, opts) -> tuple[str, str] | None:
    """文件名为 NovelAI 默认格式且未使用 ``-N`` 时，返回 (警告, 提示)；否则返回 None。"""
    if opts.rename or opts.output:
        return None
    n = sum(bool(NAI_NAME.search(src.stem)) for src, _ in items)
    if not n:
        return None
    who = '该文件' if len(items) == 1 else f'{n} 个文件'
    return (f'{who}使用 NovelAI 默认文件名（以提示词开头、以 s-<种子> 结尾），移除元数据后文件名中仍保留这些信息',
            f'使用 -N 将输出文件重命名为“日期-序号”格式，如 {today()}-0001')


def summary(counts: Counter) -> str:
    """返回批量处理的汇总行。"""
    total = sum(counts.values())
    return f'完成：共 {total} 个文件，成功 {counts["ok"]} 个，跳过 {counts["skip"]} 个，失败 {counts["fail"]} 个'


# ---------------------------------------------------------------- 命令行
EPILOG = f'''\
-t 的取值：
  TEXT       所有字段写入该文本
  1          内置预设“空格”：所有字段写入 512 个半角空格
  2          内置预设“杂鱼”：所有字段写入“杂鱼~♥”×64
  N          用户预设（编号或名称）
  edit [N]   在终端中编辑，并保存为预设 N
  @FILE      使用 JSON 模板
  list       列出预设

-w 的取值（可重复，规则依次合并）：
  OLD=NEW    单条替换规则
  NAME       词表，内置 discord（loli→1011）
  edit NAME  在终端中编辑词表
  list       列出词表

示例：
  nais a.png                 输出 a_clean.png
  nais -i *.png              直接修改源文件
  nais -r ./in -d ./out      递归处理目录，结果写入 ./out
  nais a.png -N              输出为 {today()}-0001.png
  nais a.png -t 2            移除元数据后写入内置预设 2
  nais a.png -w discord      仅替换词表 discord 中的词语
  nais tui [DIR]             进入交互模式，DIR 为输出目录

文档：{REPO_URL}'''


def build_parser(prog: str) -> ArgumentParser:
    ap = ArgumentParser(
        prog=prog,
        description='移除 NovelAI 图片中的元数据（PNG 文本块、EXIF 与 LSB 隐写），不修改像素数据。\n'
                    '也可在移除后写入伪造元数据（-t），或仅替换元数据中的指定词语（-w）。',
        epilog=EPILOG, add_help=False)
    ap.add_argument('paths', nargs='*', metavar='PATH', help='图片文件或目录，支持通配符')

    g = ap.add_argument_group('输出')
    dest = g.add_mutually_exclusive_group()
    dest.add_argument('-o', '--output', metavar='FILE', help='输出文件，仅适用于单个输入文件')
    dest.add_argument('-d', '--outdir', metavar='DIR', help='输出目录；输入为目录时保留相对路径')
    dest.add_argument('-i', '--in-place', action='store_true', help='直接修改源文件，不保留原文件')
    g.add_argument('--suffix', metavar='SUFFIX',
                   help='输出到源文件所在目录时追加的文件名后缀（默认：_clean；使用 -t 时为 _poison；使用 -w 时为词表名或 _w）')
    g.add_argument('-N', '--rename', action='store_true',
                   help='按“日期-序号”重命名输出文件（如 20260929-0001），序号接续目标目录中当天已有的编号；与 -i 同用时重命名源文件')
    g.add_argument('--overwrite', action='store_true', help='覆盖已存在的输出文件（默认：跳过）')

    g = ap.add_argument_group('元数据写入')
    g.add_argument('-t', '--poison', metavar='SPEC', help='移除元数据后写入伪造元数据（投毒），取值见下文')
    g.add_argument('--set', action='append', default=[], metavar='KEY=VALUE',
                   help='修改单个字段，可重复，值按 JSON 解析；单独使用时以源文件的元数据为基础')
    g.add_argument('-w', '--words', action='append', default=[], metavar='RULE',
                   help='不移除元数据，仅替换其中的词语后写回明文层与隐写层，文件名中的词语一并替换；取值见下文')

    g = ap.add_argument_group('像素处理')
    g.add_argument('--drop-alpha', action='store_true', help='alpha 通道完全不透明时将其移除，输出 RGB 图像')
    g.add_argument('--scrub-all', action='store_true', help='清零所有通道的最低位，用于处理未知格式的隐写（各分量最多变化 1）')
    g.add_argument('--strip-icc', action='store_true', help='同时移除 ICC 色彩配置文件（默认：保留）')

    g = ap.add_argument_group('其他')
    g.add_argument('-h', '--help', action='help', help='显示此帮助信息并退出')
    g.add_argument('-r', '--recursive', action='store_true', help='递归处理子目录')
    g.add_argument('-n', '--dry-run', action='store_true', help='试运行：报告将执行的操作，不写入文件')
    g.add_argument('-y', '--yes', action='store_true', help='处理目录或通配符时不请求确认')
    g.add_argument('--no-verify', action='store_true', help='跳过写入后的回读验证')
    g.add_argument('-V', '--version', action='version', version=f'nai-meta {__version__}', help='显示版本信息并退出')
    return ap


def _pull_edit_name(a, attr: str) -> None:
    """``-t edit 1`` 与 ``-w edit NAME`` 中的名称会被解析为路径参数，将其移回对应选项。"""
    values = getattr(a, attr)
    if attr == 'poison':
        if values != 'edit':
            return
    elif not (values and values[-1] == 'edit'):
        return
    for i in range(len(a.paths) - 1, -1, -1):
        if not Path(a.paths[i]).exists() and re.fullmatch(r'[\w-]+', a.paths[i]):
            name = a.paths.pop(i)
            if attr == 'poison':
                a.poison = f'edit:{name}'
            else:
                values[-1] = f'edit:{name}'
            return


def main(argv=None, prog: str | None = None) -> int:
    setup_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    if prog is None:
        invoked = Path(sys.argv[0]).stem
        prog = invoked if invoked in ('nais', 'nai-strip') else 'nais'
    if argv and argv[0] == 'tui':
        from .tui import run_tui
        return run_tui(argv[1:])
    a = build_parser(prog).parse_intermixed_args(argv)
    a.sets = a.set
    if a.words and a.poison:
        error('-w 与 -t 不能同时使用')
        return 1
    _pull_edit_name(a, 'words')
    _pull_edit_name(a, 'poison')

    items = list(iter_images(a.paths, a.recursive))
    try:
        rc = resolve_words(a)
        if rc is None:
            rc = resolve_poison(a, items[0][0] if items else None)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        error(str(e))
        return 1
    if rc is not None:
        return rc
    if not items:
        if a.poison_meta is not None or (a.words and any(w.startswith('edit') for w in a.words)):
            return 0                              # 仅编辑预设或词表
        error('未找到图片文件', f'使用 {prog} -h 查看用法')
        return 1
    if a.output and len(items) > 1:
        error('-o 只能用于单个输入文件', '处理多个文件时，使用 -d DIR 指定输出目录')
        return 1
    if a.output and a.rename:
        error('-o 不能与 -N 同时使用')
        return 1
    # 目录与通配符属于批量操作，处理前请求确认；逐个指定的文件不确认
    batch = [p for p in a.paths if Path(p).is_dir() or any(ch in p for ch in GLOB_CHARS)]
    if batch and not a.yes and not a.dry_run:
        print(describe_plan(items, a, '、'.join(batch)))
        if not confirm('是否继续？[y/N] '):
            print('已取消')
            return 1

    counts = Counter()
    for src, rel in items:
        status, line = strip_one(src, rel, a)
        print(line)
        counts[status] += 1
    if len(items) > 1:
        print(summary(counts))
    hint = name_hint(items, a)
    if hint:
        warn(*hint)
    return 1 if counts['fail'] else 0


if __name__ == '__main__':
    sys.exit(main())
