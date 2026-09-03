# -*- coding: utf-8 -*-
"""nai-inspect：读出 NovelAI 图片的生成参数。

三路取数：PNG 文本块 → LSB 隐写 → EXIF / 注释里塞的 JSON。默认展示文本块（没有就用隐写），
两层都在时顺手比对一遍，不一致会提示——文本块被人改过 / 投毒时隐写往往还是原样。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import wcwidth

from PIL import Image
from PIL.ExifTags import IFD, TAGS

from .core import (COLOR_TYPES, SYM, diff_meta, expand_comment, find_stealth, is_nai,
                   iter_images, meta_from_text, num, scan_png, setup_console, summarize)


# ---------------------------------------------------------------- 取数
def _exif_value(v):
    if isinstance(v, bytes):
        if v[:8] in (b'ASCII\x00\x00\x00', b'UNICODE\x00', b'\x00' * 8):   # UserComment 的字符集前缀
            v = v[8:]
        try:
            return v.decode('utf-8').strip('\x00')
        except UnicodeDecodeError:
            return f'<{len(v)} bytes>'
    if isinstance(v, (int, float, str)):
        return v
    return str(v)


def read_exif(im: Image.Image) -> dict | None:
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


def inspect_file(path: Path) -> dict:
    rec: dict = {'file': str(path)}
    try:
        im = Image.open(path)
        im.load()
    except Exception as e:
        rec['error'] = f'打不开: {e}'
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
        # JPEG 注释 / WebP 等：Pillow 放在 info 里
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

    # 3. EXIF（PNG eXIf 块 / JPEG APP1）
    rec['exif'] = read_exif(im)
    rec['xmp'] = len(im.info['xmp']) if im.info.get('xmp') else None

    # 两层都是 NAI 数据时比对
    sm = rec['stealth']['meta'] if rec['stealth'] else None
    rec['consistent'] = None
    if is_nai(text_meta) and is_nai(sm):
        d = diff_meta(text_meta, sm)
        rec['consistent'] = not d
        rec['diff_keys'] = d
    return rec


def choose_meta(rec: dict, prefer: str) -> tuple[dict | None, str | None]:
    """按 --text/--stealth 或自动顺序挑一份来做摘要。返回 (meta, 来源名)。"""
    text_meta = rec.get('text_meta')
    st_meta = rec['stealth']['meta'] if rec.get('stealth') else None
    if prefer == 'text':
        return (text_meta, '文本块') if is_nai(text_meta) else (None, None)
    if prefer == 'stealth':
        return (st_meta, '隐写') if is_nai(st_meta) else (None, None)
    if is_nai(text_meta):
        return text_meta, '文本块'
    if is_nai(st_meta):
        return st_meta, '隐写'
    # EXIF UserComment / ImageDescription / JPEG 注释里的 JSON
    for src, s in (('EXIF', (rec.get('exif') or {}).get('UserComment')),
                   ('EXIF', (rec.get('exif') or {}).get('ImageDescription')),
                   ('注释', rec.get('comment'))):
        m = meta_from_text(s) if isinstance(s, str) else None
        if m:
            return m, src
    return None, None


# ---------------------------------------------------------------- 输出
def _width() -> int:
    return max(48, min(100, shutil.get_terminal_size((80, 24)).columns))


def _dw(text: str) -> int:
    """终端显示宽度：中文占两格。"""
    return sum(max(wcwidth.wcwidth(ch), 0) for ch in text)


def _lab(label: str) -> str:
    return label + ' ' * max(2, 10 - _dw(label))


def _rule(title: str, width: int) -> str:
    head = f"{SYM['rule'] * 3} {title} "
    return head + SYM['rule'] * max(0, width - _dw(head))


def _meta_line(rec: dict, src: str | None) -> str:
    tc = rec.get('text_chunks') or {}
    parts = [f"文本块 {SYM['yes']} {len(tc)}" if tc else f"文本块 {SYM['no']}"]
    st = rec.get('stealth')
    if st:
        parts.append(f"隐写 {SYM['yes']} {st['channel']}{'+gzip' if st['compressed'] else ''} {st['bytes']} B"
                     + (f" + FEC {st['fec_bytes']} B" if st.get('fec_bytes') else ''))
    else:
        parts.append(f"隐写 {SYM['no']}")
    if rec.get('exif'):
        parts.append(f"EXIF {SYM['yes']} {len(rec['exif'])} 项")
    if rec.get('xmp'):
        parts.append(f"XMP {SYM['yes']} {rec['xmp']} B")
    if rec.get('consistent') is True:
        parts.append('两层一致')
    elif rec.get('consistent') is False:
        parts.append(f"{SYM['warn']} 文本块与隐写不一致: " + ', '.join(rec['diff_keys']))
    if src:
        parts.append(f'读自{src}')
    return _lab('元数据') + ' · '.join(parts)


def render(rec: dict, prefer: str, full: bool, raw: bool) -> str:
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
        if tc:                                   # 不是 NAI 的文本块（A1111 的 parameters 之类），原样给
            for k, v in tc.items():
                L.append(f'{k}:')
                L.append(v if full else v[:600] + ('…' if len(v) > 600 else ''))
        if rec.get('exif'):
            L.append('EXIF:')
            for k, v in rec['exif'].items():
                L.append(f'    {k}: {str(v)[:200]}')
        if not tc and not rec.get('exif') and not rec.get('stealth'):
            L.append('    没有任何元数据（可能被转发剥掉、或重编码 / 缩放过）')
        return '\n'.join(L)

    p = summarize(meta)
    rows = []
    mdl = p['model']
    rows.append(('模型', ' · '.join(x for x in (mdl['name'], f"哈希 {mdl['hash']}" if mdl['hash'] else None) if x)
                 or mdl['source'] or '?'))
    t = p['type']
    rows.append(('类型', t['label'] + ''.join(f' · {lab} {num(t[k])}' for k, lab in (('strength', '强度'), ('noise', '噪声')) if k in t)))
    if p['addons']:
        rows.append(('附加', ' · '.join(f"{a['label']}（{a['detail']}）" if a['detail'] else a['label']
                                        for a in p['addons'])))
    if p['width'] and p['height']:
        size = f"{p['width']}×{p['height']}"
        if (p['width'], p['height']) != (rec['width'], rec['height']):
            size += f"（文件实际 {rec['width']}×{rec['height']}）"
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
        samp.append(f"{p['steps']} steps")
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
        rows.append(('签名', f"有（NAI 签名 {p['signed_hash'][:12]}…，未验证）"))
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
            block('隐写原文', rec['stealth']['raw'])
    return '\n'.join(L)


def prompt_only(rec: dict, prefer: str) -> str | None:
    meta, _ = choose_meta(rec, prefer)
    if meta is None:
        return None
    p = summarize(meta)
    out = [p['prompt']]
    for ch in p['char_prompts']:
        out.append(f'\n# 角色 {ch["index"]}\n{ch["caption"]}')
    return '\n'.join(out)


def main(argv=None) -> int:
    setup_console()
    ap = argparse.ArgumentParser(
        prog='nai-inspect',
        description='读出 NovelAI 图片的生成参数：PNG 文本块 + LSB 隐写（+ EXIF）。',
        epilog='示例：nai-inspect a.png b.png   |   nai-inspect -r ./图 --json > meta.json   |   nai-inspect -p a.png | pbcopy')
    ap.add_argument('paths', nargs='+', help='图片文件或目录')
    ap.add_argument('-r', '--recursive', action='store_true', help='目录递归')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--text', action='store_true', help='只用文本块（不看隐写）')
    g.add_argument('--stealth', action='store_true', help='只用隐写（不看文本块）')
    ap.add_argument('-f', '--full', action='store_true', help='把 Comment 里的全部参数也打出来')
    ap.add_argument('--raw', action='store_true', help='附带原始文本块 / 隐写 JSON 字符串')
    ap.add_argument('-j', '--json', action='store_true', help='输出 JSON（单图一个对象，多图为数组）')
    ap.add_argument('-p', '--prompt', action='store_true', help='只输出正向提示词（含角色），方便复制')
    a = ap.parse_args(argv)
    prefer = 'text' if a.text else 'stealth' if a.stealth else 'auto'

    files = [f for f, _ in iter_images(a.paths, a.recursive)]
    if not files:
        print('没有找到图片', file=sys.stderr)
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
            print(t if t is not None else '（没有提示词）')
    else:
        print('\n\n'.join(render(r, prefer, a.full, a.raw) for r in recs))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
