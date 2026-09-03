# -*- coding: utf-8 -*-
"""-t edit：在终端里逐字段改要写进图里的元数据（投毒预设）。

列出六个文本块和 Comment 里的关键字段，输入编号改那一项，键=值 直接改，
:all 内容 一键全塞同一段，:json 才跳到外部编辑器改完整 JSON。
"""
from __future__ import annotations

import copy
import html
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit import print_formatted_text as pt_print
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.styles import Style

from .core import NAI_TEXT_KEYS, SYM, config_dir, fill_meta, load_meta_json, parse_set, set_prompt, set_uc

STYLE = Style.from_dict({'prompt': 'bold ansicyan', 'dim': 'ansibrightblack', 'warn': 'bold ansiyellow',
                         'bad': 'ansired', 'key': 'bold'})
TOP = ('Title', 'Description', 'Software', 'Source', 'Generation time')
COMMENT_FIELDS = ('prompt', 'uc', 'seed', 'steps', 'scale', 'cfg_rescale', 'sampler', 'noise_schedule',
                  'width', 'height', 'model_name', 'model_hash', 'request_type')
INT_FIELDS = {'seed', 'steps', 'width', 'height'}
FLOAT_FIELDS = {'scale', 'cfg_rescale'}
MULTILINE = {'prompt', 'uc', 'Description'}
HELP = ('编号 → 改那一项 · 键=值 直接改（Comment 里任何字段都行，值按 JSON 解析）· :all 内容 → 每块都塞这段 · '
        ':json → 用 $EDITOR 改完整 JSON · :w 保存 · :q 取消 · 回车重看列表')
EDIT_HELP = [
    '这就是要写进图里的全部内容，保存退出即生效；清空文件或 JSON 不合法则取消。',
    'PNG 文本块 / WebP 与 JPEG 的 EXIF：下面每个键各一块，Comment 会转成 JSON 字符串。',
    '隐写层：Description、Software、Source、Generation time、Comment 五项打包 gzip 写进 alpha 最低位。',
    'Comment.prompt、Description、v4_prompt.caption.base_caption 要一致（novelai.net/inspect 读 Comment.prompt）；',
    '写入时 width / height 按每张图实际尺寸覆盖，seed 为 null 则每张随机。_ 开头的键不写入。',
]


def say(text: str, style: str = '') -> None:
    t = html.escape(text)
    pt_print(HTML(f'<{style}>{t}</{style}>' if style else t), style=STYLE)


# ---------------------------------------------------------------- 外部编辑器（完整 JSON）
def edit_json_external(meta: dict) -> dict | None:
    """用 $VISUAL / $EDITOR 打开一份 JSON 让用户改，读回来。取消返回 None。"""
    doc = {'_说明': EDIT_HELP, **meta}
    config_dir().mkdir(parents=True, exist_ok=True)
    path = config_dir() / 'edit.json'
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), 'utf-8')
    editor = os.environ.get('VISUAL') or os.environ.get('EDITOR')
    if not editor:
        has_nano = any(os.access(os.path.join(d, 'nano'), os.X_OK) for d in os.environ.get('PATH', '').split(os.pathsep))
        editor = 'notepad' if os.name == 'nt' else ('nano' if has_nano else 'vi')
    print(f'用 {editor} 打开 {path}（EDITOR 环境变量可换编辑器）', file=sys.stderr)
    try:
        subprocess.call([*shlex.split(editor, posix=os.name != 'nt'), str(path)])
    except OSError as e:
        print(f'打不开编辑器 {editor}: {e}', file=sys.stderr)
        return None
    try:
        if not path.read_text('utf-8').strip():
            return None
        return load_meta_json(path)
    except (ValueError, json.JSONDecodeError) as e:
        print(f'JSON 不合法，已取消：{e}', file=sys.stderr)
        return None


# ---------------------------------------------------------------- 终端内逐字段
def _fields(meta: dict) -> list[tuple[str, object]]:
    rows = [(k, meta.get(k)) for k in TOP]
    c = meta.get('Comment')
    if isinstance(c, dict):
        rows += [(k, c.get(k)) for k in COMMENT_FIELDS]
    else:
        rows.append(('Comment', c))
    return rows


def _short(v, width: int = 70) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = s.replace('\n', '⏎ ')
    return s if len(s) <= width else s[:width] + '…'


def show(meta: dict) -> None:
    for i, (k, v) in enumerate(_fields(meta), 1):
        say(f'{i:>3}  {k:<16} {_short(v)}')
    say(HELP, 'dim')


def _comment_dict(meta: dict) -> dict:
    """Comment 是整段文本（-t 填充的那种）时，改内部字段前先变成 dict。"""
    c = meta.get('Comment')
    if not isinstance(c, dict):
        s = c if isinstance(c, str) else ''
        meta['Comment'] = {'prompt': s, 'uc': s}
    return meta['Comment']


def apply_value(meta: dict, key: str, v) -> None:
    """把一个值写到它该在的位置：顶层块 / Comment 内部；prompt、uc 顺带同步 v4 结构和 Description。"""
    if key in NAI_TEXT_KEYS and key != 'Comment':
        meta[key] = v
        return
    if key == 'Comment':
        meta['Comment'] = v
        return
    c = _comment_dict(meta)
    if key == 'prompt':
        set_prompt(c, str(v))
        meta['Description'] = str(v)
    elif key == 'uc':
        set_uc(c, str(v))
    else:
        c[key] = v


def parse_typed(key: str, raw: str):
    """按字段类型解释输入：整数 / 小数字段，空或 null 表示 None（seed 为 None = 每张随机）。"""
    t = raw.strip()
    if key in INT_FIELDS | FLOAT_FIELDS:
        if t.lower() in ('', 'null', 'none'):
            return None
        try:
            return int(t) if key in INT_FIELDS else float(t)
        except ValueError:
            raise ValueError(f'{key} 要是{"整数" if key in INT_FIELDS else "数字"}：{raw!r}') from None
    if key in TOP or key in ('prompt', 'uc', 'sampler', 'noise_schedule', 'model_name', 'model_hash', 'request_type'):
        return raw
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return raw


def edit_interactive(meta: dict, name: str | None = None) -> dict | None:
    """返回改好的元数据；取消返回 None。"""
    meta = copy.deepcopy(meta)
    session = PromptSession(style=STYLE)
    say(f'{SYM["bar"]} 编辑投毒内容' + (f'（预设 {name}）' if name else ''), 'prompt')
    show(meta)
    while True:
        try:
            line = session.prompt(HTML('<prompt>edit</prompt> <dim>›</dim> ')).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not line:
            show(meta)
        elif line in (':w', ':wq', 'w'):
            return meta
        elif line in (':q', ':q!', 'q'):
            return None
        elif line.startswith(':all'):
            text = line[4:].strip()
            if not text:
                say('用法：:all 内容', 'bad')
                continue
            meta = fill_meta(text)
            show(meta)
        elif line == ':json':
            m = edit_json_external(meta)
            if m is None:
                say('取消或 JSON 不合法，保持不变', 'warn')
            else:
                meta = m
                show(meta)
        elif line.isdigit():
            fields = _fields(meta)
            i = int(line)
            if not 1 <= i <= len(fields):
                say('没有这一项', 'bad')
                continue
            key, cur = fields[i - 1]
            default = cur if isinstance(cur, str) else ('' if cur is None else json.dumps(cur, ensure_ascii=False))
            multi = key in MULTILINE
            try:
                new = session.prompt(HTML(f'<key>{html.escape(key)}</key> <dim>›</dim> '), default=default, multiline=multi,
                                     bottom_toolbar=(' 多行：Enter 换行，Esc 再 Enter 提交；Ctrl-C 放弃' if multi else None))
            except KeyboardInterrupt:
                say('放弃', 'dim')
                continue
            except EOFError:
                return None
            try:
                apply_value(meta, key, parse_typed(key, new))
            except ValueError as e:
                say(str(e), 'bad')
        elif '=' in line:
            try:
                k, v = parse_set(line)
            except ValueError as e:
                say(str(e), 'bad')
                continue
            if isinstance(v, str) and (k in INT_FIELDS or k in FLOAT_FIELDS):
                try:
                    v = parse_typed(k, v)
                except ValueError as e:
                    say(str(e), 'bad')
                    continue
            apply_value(meta, k, v)
            say(f'{k} = {_short(v)}', 'dim')
        else:
            say('不认识这个；' + HELP, 'bad')


def ask(text: str) -> str:
    try:
        return PromptSession(style=STYLE).prompt(HTML(f'<warn>{html.escape(text)}</warn>')).strip()
    except (EOFError, KeyboardInterrupt):
        return ''
