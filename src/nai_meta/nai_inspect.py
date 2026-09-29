# -*- coding: utf-8 -*-
"""nai-inspect：读取 NovelAI 图片的生成参数。

数据来源依次为 PNG 文本块、LSB 隐写层、EXIF 与注释中的 JSON。默认显示明文层，不存在时显示隐写层。
两层同时存在时比对其内容：明文层被修改或写入伪造数据后，隐写层通常仍保留原始内容。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import wcwidth

from PIL import Image
from PIL.ExifTags import IFD, TAGS

from . import __version__
from .argparse_zh import ArgumentParser
from .core import (COLOR_TYPES, SYM, diff_meta, error, expand_comment, find_stealth, is_nai,
                   iter_images, meta_from_text, num, parse_a1111, scan_png, setup_console, summarize)

REPO_URL = 'https://github.com/Miint-Sunny/nai-meta'


# ---------------------------------------------------------------- 读取
def _exif_value(v):
    """将 EXIF 值转换为可显示的形式。字节串按 UTF-8 解码，并去掉 UserComment 的字符集前缀和首尾 NUL。"""
    if isinstance(v, bytes):
        if v[:8] in (b'ASCII\x00\x00\x00', b'UNICODE\x00', b'\x00' * 8):   # UserComment 的 8 字节字符集标识
            v = v[8:]
        try:
            return v.decode('utf-8').strip('\x00')
        except UnicodeDecodeError:
            return f'<{len(v)} bytes>'
    if isinstance(v, str):                       # 首尾 NUL 无意义，原因见 exif_meta
        return v.strip('\x00')
    if isinstance(v, (int, float)):
        return v
    return str(v)


def read_exif(im: Image.Image) -> dict | None:
    """读取 IFD0 与 Exif IFD 中的全部标签，返回 {标签名: 值}；没有 EXIF 时返回 None。"""
    try:
        ex = im.getexif()
    except Exception:
        return None
    if not ex:
        return None
    out = {}
    for k, v in ex.items():
        out[TAGS.get(k, hex(k))] = _exif_value(v)
    try:
        for k, v in ex.get_ifd(IFD.Exif).items():
            out[TAGS.get(k, hex(k))] = _exif_value(v)
    except Exception:
        pass
    return out


def exif_meta(ex: dict | None) -> dict | None:
    """将 EXIF 中的 NovelAI 元数据整理为与 PNG 文本块相同的结构。

    NovelAI 的 WebP 将元数据写入 EXIF：Software 为模型名与哈希（对应 Source），DocumentName 为 Title，
    ImageDescription 为提示词，UserComment 为完整元数据的 JSON。也识别其他工具写入 UserComment 的 A1111 参数。

    Returns:
        元数据 dict；不含可识别的元数据时返回 None。
    """
    if not ex:
        return None
    m = None
    for key in ('UserComment', 'ImageDescription'):
        v = ex.get(key)
        m = meta_from_text(v) if isinstance(v, str) else None
        if m:
            break
    if not m:
        return None
    sw = str(ex.get('Software') or '')
    if 'NovelAI' in sw:
        m.setdefault('Source', sw)
        m.setdefault('Software', 'NovelAI')
    elif sw:
        m.setdefault('Software', sw)
    if isinstance(ex.get('ImageDescription'), str) and 'Description' not in m:
        m['Description'] = ex['ImageDescription']
    if isinstance(ex.get('DocumentName'), str):
        m.setdefault('Title', ex['DocumentName'])
    # 提示词含中文时，NovelAI 的 WebP 在 ImageDescription 和 UserComment 内 Description 的开头写入 4 个 NUL，
    # 隐写层中没有。去掉 NUL，避免误报两层不一致，也避免 -p 输出中带有 NUL
    return {k: v.strip('\x00') if isinstance(v, str) else v for k, v in m.items()}


def inspect_file(path: Path) -> dict:
    """读取文件的全部元数据层，并在两层均为 NovelAI 元数据时比对其内容。

    Returns:
        记录 dict，主要字段：text_chunks（文本块原文）、text_meta（解析后的文本块元数据）、
        stealth（隐写层）、exif、exif_meta、outer_layer（明文层来源）、consistent（两层是否一致，
        无法比较时为 None）。文件无法打开时仅含 file 与 error。
    """
    rec: dict = {'file': str(path)}
    try:
        im = Image.open(path)
        im.load()
    except Exception as e:
        rec['error'] = f'无法打开：{e}'
        return rec
    rec.update(format=im.format, mode=im.mode, width=im.size[0], height=im.size[1])

    # 1. PNG 文本块
    scan = scan_png(path)
    text_meta = None
    if scan:
        rec['png'] = {'bit_depth': scan.bit_depth, 'color_type': COLOR_TYPES.get(scan.color_type, scan.color_type),
                      'chunks': dict(scan.chunks)}
        rec['text_chunks'] = scan.texts
        if scan.texts:
            text_meta = expand_comment(dict(scan.texts))
    else:
        # JPEG 注释等由 Pillow 放在 info 中
        cm = im.info.get('comment')
        if cm:
            rec['comment'] = _exif_value(cm)
    rec['text_meta'] = text_meta

    # 2. LSB 隐写
    st = find_stealth(im)
    rec['stealth'] = None
    if st:
        rec['stealth'] = {'channel': st.channel, 'compressed': st.compressed, 'magic': st.magic,
                          'bytes': st.nbytes, 'fec_bytes': st.fec_bytes, 'raw': st.text, 'meta': st.meta}

    # 3. EXIF（PNG eXIf 块、JPEG APP1 段、WebP EXIF 块）。NovelAI 的 WebP 将参数写在此处
    rec['exif'] = read_exif(im)
    rec['exif_meta'] = exif_meta(rec['exif'])
    rec['xmp'] = len(im.info['xmp']) if im.info.get('xmp') else None

    # 明文层（文本块，不存在时为 EXIF）与隐写层均为 NovelAI 元数据时进行比对
    outer, outer_name = (text_meta, '文本块') if text_meta else (rec['exif_meta'], 'EXIF')
    rec['outer_layer'] = outer_name if outer else None
    sm = rec['stealth']['meta'] if rec['stealth'] else None
    rec['consistent'] = None
    if is_nai(outer) and is_nai(sm):
        d = diff_meta(outer, sm)
        rec['consistent'] = not d
        rec['diff_keys'] = d
    return rec


def choose_meta(rec: dict, prefer: str) -> tuple[dict | None, str | None]:
    """选择用于显示的元数据。

    Args:
        prefer: ``text`` 仅用明文层，``stealth`` 仅用隐写层，``auto`` 依次尝试明文层（PNG 文本块或 WebP 的 EXIF）、
            隐写层、A1111 参数和 JPEG 注释。

    Returns:
        (元数据, 来源名称)；没有可用数据时返回 (None, None)。
    """
    text_meta, ex_meta = rec.get('text_meta'), rec.get('exif_meta')
    st_meta = rec['stealth']['meta'] if rec.get('stealth') else None
    outer, outer_name = (text_meta, '文本块') if is_nai(text_meta) else (ex_meta, 'EXIF')
    if prefer == 'text':
        return (outer, outer_name) if is_nai(outer) else (None, None)
    if prefer == 'stealth':
        return (st_meta, '隐写层') if is_nai(st_meta) else (None, None)
    if is_nai(outer):
        return outer, outer_name
    if is_nai(st_meta):
        return st_meta, '隐写层'
    a1111 = parse_a1111((rec.get('text_chunks') or {}).get('parameters', ''))
    if a1111:
        return a1111, '文本块 parameters'
    if ex_meta:                                  # 例如 EXIF 中的 A1111 参数
        return ex_meta, 'EXIF'
    m = meta_from_text(rec['comment']) if isinstance(rec.get('comment'), str) else None
    return (m, '注释') if m else (None, None)


# ---------------------------------------------------------------- 输出
def _width() -> int:
    return max(48, min(100, shutil.get_terminal_size((80, 24)).columns))


def _dw(text: str) -> int:
    """返回终端显示宽度，宽字符计 2 列。"""
    return sum(max(wcwidth.wcwidth(ch), 0) for ch in text)


def _lab(label: str) -> str:
    return label + ' ' * max(2, 10 - _dw(label))


def _rule(title: str, width: int) -> str:
    head = f"{SYM['rule'] * 3} {title} "
    return head + SYM['rule'] * max(0, width - _dw(head))


def _meta_line(rec: dict, src: str | None) -> str:
    tc = rec.get('text_chunks') or {}
    parts = [f"文本块 {SYM['yes']} {len(tc)} 个" if tc else f"文本块 {SYM['no']}"]
    st = rec.get('stealth')
    if st:
        parts.append(f"隐写 {SYM['yes']} {st['channel']}{'+gzip' if st['compressed'] else ''} {st['bytes']} B"
                     + (f" + FEC {st['fec_bytes']} B" if st.get('fec_bytes') else ''))
    else:
        parts.append(f"隐写 {SYM['no']}")
    if rec.get('exif'):
        parts.append(f"EXIF {SYM['yes']} {len(rec['exif'])} 项" + ('（含 NovelAI 参数）' if is_nai(rec.get('exif_meta')) else ''))
    if rec.get('xmp'):
        parts.append(f"XMP {SYM['yes']} {rec['xmp']} B")
    if rec.get('consistent') is True:
        parts.append('两层一致')
    elif rec.get('consistent') is False:
        parts.append(f"{SYM['warn']} {rec.get('outer_layer') or '明文层'}与隐写层不一致：" + '、'.join(rec['diff_keys']))
    if src:
        parts.append(f'显示来源：{src}')
    return _lab('元数据') + ' · '.join(parts)


def _describe_chunk(key: str, text: str, full: bool) -> str:
    if not full:
        if key in ('workflow', 'prompt'):
            try:
                d = json.loads(text)
                nodes = d.get('nodes') if isinstance(d, dict) and 'nodes' in d else d
                return f'{key}：ComfyUI 工作流，{len(nodes)} 个节点（使用 -f 显示全文）'
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        if key.startswith(('XML:', 'Raw profile')) or len(text) > 600:
            return f'{key}：{len(text)} 个字符（使用 -f 显示全文）'
    if '\n' not in text and len(text) <= 80:
        return f'{key}：{text}'
    return f'{key}：\n{text}'


def render(rec: dict, prefer: str, full: bool, raw: bool) -> str:
    """将 :func:`inspect_file` 的记录格式化为终端显示文本。"""
    W = _width()
    L = [f"{SYM['bar']} {rec['file']}"]
    if 'error' in rec:
        L.append('    ' + rec['error'])
        return '\n'.join(L)
    L[0] += f"   {rec['format']} · {rec['mode']} · {rec['width']}×{rec['height']}"

    meta, src = choose_meta(rec, prefer)
    L.append(_meta_line(rec, src))
    tc = rec.get('text_chunks') or {}
    if meta is None:
        for k, v in tc.items():                  # 既非 NovelAI 也非 A1111 格式：可识别的给出摘要，其余原样显示
            L.append(_describe_chunk(k, v, full))
        if rec.get('stealth'):
            L.append(_describe_chunk('隐写层原文', rec['stealth']['raw'], full))
        if rec.get('exif'):
            L.append('EXIF：')
            for k, v in rec['exif'].items():
                L.append(f'    {k}：{str(v)[:200]}')
        if not tc and not rec.get('exif') and not rec.get('stealth'):
            L.append('    未检测到元数据（可能已被转发平台移除，或图像经过重新编码、缩放）')
        return '\n'.join(L)

    p = summarize(meta)
    rows = []
    mdl = p['model']
    rows.append(('模型', ' · '.join(x for x in (mdl['name'], f"哈希 {mdl['hash']}" if mdl['hash'] else None) if x)
                 or mdl['source'] or '?'))
    t = p['type']
    rows.append(('类型', t['label'] + ''.join(f' · {lab} {num(t[k])}' for k, lab in
                                             (('strength', '强度'), ('noise', '噪声'), ('defry', 'defry')) if k in t)))
    if p['addons']:
        rows.append(('附加', ' · '.join(f"{a['label']}（{a['detail']}）" if a['detail'] else a['label']
                                        for a in p['addons'])))
    if p['width'] and p['height']:
        size = f"{p['width']}×{p['height']}"
        if (p['width'], p['height']) != (rec['width'], rec['height']):
            size += f"（文件实际尺寸 {rec['width']}×{rec['height']}）"
    else:
        size = f"{rec['width']}×{rec['height']}"
    if p['generation_time'] is not None:
        try:
            size += f"   耗时 {float(p['generation_time']):.1f} s"
        except (TypeError, ValueError):
            size += f"   耗时 {p['generation_time']}"
    rows.append(('尺寸', size))
    samp = []
    if p['sampler']:
        samp.append(f"{p['sampler_name']} ({p['sampler']})" if p['sampler_name'] else p['sampler'])
    if p['noise_schedule']:
        samp.append(p['noise_schedule'])
    if p['steps'] is not None:
        samp.append(f"{p['steps']} 步")
    rows.append(('采样', ' · '.join(samp) or '?'))
    guid = []
    if p['scale'] is not None:
        guid.append(f"Prompt Guidance {num(p['scale'])}")
    if p['cfg_rescale'] is not None:
        guid.append(f"Rescale {num(p['cfg_rescale'])}")
    rows.append(('引导', ' · '.join(guid) or '?'))
    rows.append(('种子', str(p['seed']) if p['seed'] is not None else '?'))
    if p['toggles']:
        rows.append(('开关', ' · '.join(k if v is True else f'{k} {v}' for k, v in p['toggles'].items())))
    if p['signed_hash']:
        rows.append(('签名', f"{p['signed_hash'][:12]}…（未验证）"))
    L.append('')
    L += [_lab(k) + v for k, v in rows]

    def block(title, text):
        L.append('')
        L.append(_rule(title, W))
        L.append(text if text else '（空）')

    block('正向', p['prompt'])
    for ch in p['char_prompts']:
        pos = ''.join(f' @ ({num(x)}, {num(y)})' for x, y in ch['centers']) if p['use_coords'] else ''
        block(f"角色 {ch['index']}{pos}", ch['caption'])
    if p['uc']:
        block('负面', p['uc'])
    for ch in p['char_uc']:
        block(f"角色 {ch['index']} 负面", ch['caption'])
    if full and isinstance(meta.get('Comment'), dict):
        block('Comment 全部字段', json.dumps(meta['Comment'], ensure_ascii=False, indent=1))
    if raw:
        for k, v in tc.items():
            block(f'文本块 {k}', v)
        if rec.get('stealth'):
            block('隐写层原文', rec['stealth']['raw'])
    return '\n'.join(L)


def prompt_only(rec: dict, prefer: str) -> str | None:
    """返回正向提示词与各角色提示词；没有元数据时返回 None。"""
    meta, _ = choose_meta(rec, prefer)
    if meta is None:
        return None
    p = summarize(meta)
    out = [p['prompt']]
    for ch in p['char_prompts']:
        out.append(f'\n# 角色 {ch["index"]}\n{ch["caption"]}')
    return '\n'.join(out)


EPILOG = f'''\
示例：
  naii a.png b.png           显示生成参数
  naii -r ./dir -j > a.json  递归读取目录并导出 JSON
  naii -p a.png | pbcopy     复制正向提示词（macOS）
  naii --stealth a.png       仅读取隐写层

文档：{REPO_URL}'''


def build_parser(prog: str) -> ArgumentParser:
    ap = ArgumentParser(
        prog=prog,
        description='读取 NovelAI 图片的生成参数。依次检查 PNG 文本块、LSB 隐写层和 EXIF，\n'
                    '两层同时存在时比对其内容是否一致。',
        epilog=EPILOG, add_help=False)
    ap.add_argument('paths', nargs='+', metavar='PATH', help='图片文件或目录，支持通配符')
    g = ap.add_argument_group('数据来源')
    src = g.add_mutually_exclusive_group()
    src.add_argument('--text', action='store_true', help='仅读取明文层（PNG 文本块或 WebP 的 EXIF）')
    src.add_argument('--stealth', action='store_true', help='仅读取 LSB 隐写层')
    g = ap.add_argument_group('输出')
    g.add_argument('-f', '--full', action='store_true', help='显示 Comment 中的全部字段')
    g.add_argument('--raw', action='store_true', help='附加显示原始文本块与隐写层 JSON')
    g.add_argument('-j', '--json', action='store_true', help='以 JSON 格式输出（单个文件为对象，多个文件为数组）')
    g.add_argument('-p', '--prompt', action='store_true', help='仅输出正向提示词与角色提示词')
    g = ap.add_argument_group('其他')
    g.add_argument('-h', '--help', action='help', help='显示此帮助信息并退出')
    g.add_argument('-r', '--recursive', action='store_true', help='递归处理子目录')
    g.add_argument('-V', '--version', action='version', version=f'nai-meta {__version__}', help='显示版本信息并退出')
    return ap


def main(argv=None, prog: str | None = None) -> int:
    setup_console()
    if prog is None:
        invoked = Path(sys.argv[0]).stem
        prog = invoked if invoked in ('naii', 'nai-inspect') else 'naii'
    a = build_parser(prog).parse_args(argv)
    prefer = 'text' if a.text else 'stealth' if a.stealth else 'auto'

    files = [f for f, _ in iter_images(a.paths, a.recursive)]
    if not files:
        error('未找到图片文件')
        return 1
    recs = [inspect_file(f) for f in files]
    errors = sum('error' in r for r in recs)

    if a.json:
        for r in recs:
            m, src = choose_meta(r, prefer)
            r['params'] = summarize(m) if m else None
            r['params_from'] = src
            if not a.raw and r.get('stealth'):
                r['stealth'].pop('raw', None)
        print(json.dumps(recs if len(recs) > 1 else recs[0], ensure_ascii=False, indent=1))
    elif a.prompt:
        for r in recs:
            t = prompt_only(r, prefer)
            if len(recs) > 1:
                print(f"# ===== {r['file']}")
            print(t if t is not None else '（无提示词）')
    else:
        print('\n\n'.join(render(r, prefer, a.full, a.raw) for r in recs))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
