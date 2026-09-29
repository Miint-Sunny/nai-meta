# -*- coding: utf-8 -*-
"""终端内的编辑界面：``-t edit`` 编辑伪造元数据（预设），``-w edit`` 编辑词表。

预设编辑器列出六个明文字段和 Comment 中的常用字段。输入编号修改对应字段，
``KEY=VALUE`` 直接设置任意字段，``:all TEXT`` 将所有字段设为同一文本，``:json`` 在外部编辑器中编辑完整 JSON。
"""
from __future__ import annotations

import copy
import html
import json
import os
import shlex
import subprocess
import sys

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
HELP = ('编号：修改对应字段 │ KEY=VALUE：设置任意字段，值按 JSON 解析 │ :all TEXT：所有字段设为同一文本 │ '
        ':json：用外部编辑器编辑完整 JSON │ :w 保存 │ :q 取消 │ 回车：重新显示列表')
EDIT_HELP = [
    '本文件为将写入图片的全部元数据，保存并关闭编辑器后生效；清空文件或 JSON 无效时取消修改。',
    '明文层：每个顶层键对应一个 PNG 文本块（WebP、JPEG 为 EXIF 字段），Comment 序列化为 JSON 字符串。',
    '隐写层：Description、Software、Source、Generation time、Comment 经 gzip 压缩后写入 alpha 通道最低位。',
    'Comment.prompt、Description 与 v4_prompt.caption.base_caption 应保持一致（novelai.net/inspect 读取 Comment.prompt）。',
    '写入时 width 与 height 按各图像的实际尺寸设置；seed 为 null 时为每个文件生成随机值。以 _ 开头的键不写入。',
]


def say(text: str, style: str = '') -> None:
    t = html.escape(text)
    pt_print(HTML(f'<{style}>{t}</{style}>' if style else t), style=STYLE)


# ---------------------------------------------------------------- 外部编辑器（完整 JSON）
def edit_json_external(meta: dict) -> dict | None:
    """在外部编辑器（$VISUAL 或 $EDITOR）中编辑元数据的 JSON。

    Returns:
        修改后的元数据；取消或 JSON 无效时返回 None。
    """
    doc = {'_说明': EDIT_HELP, **meta}
    config_dir().mkdir(parents=True, exist_ok=True)
    path = config_dir() / 'edit.json'
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), 'utf-8')
    editor = os.environ.get('VISUAL') or os.environ.get('EDITOR')
    if not editor:
        has_nano = any(os.access(os.path.join(d, 'nano'), os.X_OK) for d in os.environ.get('PATH', '').split(os.pathsep))
        editor = 'notepad' if os.name == 'nt' else ('nano' if has_nano else 'vi')
    print(f'正在用 {editor} 打开 {path}（可通过 EDITOR 环境变量指定编辑器）', file=sys.stderr)
    try:
        subprocess.call([*shlex.split(editor, posix=os.name != 'nt'), str(path)])
    except OSError as e:
        print(f'错误：无法启动编辑器 {editor}：{e}', file=sys.stderr)
        return None
    try:
        if not path.read_text('utf-8').strip():
            return None
        return load_meta_json(path)
    except (ValueError, json.JSONDecodeError) as e:
        print(f'错误：JSON 无效，已取消：{e}', file=sys.stderr)
        return None


# ---------------------------------------------------------------- 终端内逐字段编辑
def _fields(meta: dict) -> list[tuple[str, object]]:
    """返回编辑列表中的 (字段名, 当前值)，编号从 1 开始对应列表顺序。"""
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
    """返回 Comment 对象。Comment 为纯文本（由 :all 或 -t TEXT 生成）时先转换为对象。"""
    c = meta.get('Comment')
    if not isinstance(c, dict):
        s = c if isinstance(c, str) else ''
        meta['Comment'] = {'prompt': s, 'uc': s}
    return meta['Comment']


def apply_value(meta: dict, key: str, v) -> None:
    """设置字段值。顶层字段直接写入，其余写入 Comment；prompt 与 uc 同步到 v4 结构，prompt 同步到 Description。"""
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
    """按字段类型解析输入。

    整数与小数字段输入为空或 null 时返回 None（seed 为 None 表示每个文件随机生成）；
    文本字段原样返回；其余字段按 JSON 解析，失败时作为字符串。

    Raises:
        ValueError: 数值字段的输入无法解析。
    """
    t = raw.strip()
    if key in INT_FIELDS | FLOAT_FIELDS:
        if t.lower() in ('', 'null', 'none'):
            return None
        try:
            return int(t) if key in INT_FIELDS else float(t)
        except ValueError:
            raise ValueError(f'{key} 必须是{"整数" if key in INT_FIELDS else "数字"}：{raw!r}') from None
    if key in TOP or key in ('prompt', 'uc', 'sampler', 'noise_schedule', 'model_name', 'model_hash', 'request_type'):
        return raw
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return raw


def edit_interactive(meta: dict, name: str | None = None) -> dict | None:
    """在终端中编辑元数据。

    Returns:
        修改后的元数据；取消时返回 None。
    """
    meta = copy.deepcopy(meta)
    session = PromptSession(style=STYLE)
    say(f'{SYM["bar"]} 编辑伪造元数据' + (f'（预设 {name}）' if name else ''), 'prompt')
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
                say('用法：:all TEXT', 'bad')
                continue
            meta = fill_meta(text)
            show(meta)
        elif line == ':json':
            m = edit_json_external(meta)
            if m is None:
                say('已取消或 JSON 无效，内容未修改', 'warn')
            else:
                meta = m
                show(meta)
        elif line.isdigit():
            fields = _fields(meta)
            i = int(line)
            if not 1 <= i <= len(fields):
                say(f'无效的编号：{i}', 'bad')
                continue
            key, cur = fields[i - 1]
            default = cur if isinstance(cur, str) else ('' if cur is None else json.dumps(cur, ensure_ascii=False))
            multi = key in MULTILINE
            try:
                new = session.prompt(HTML(f'<key>{html.escape(key)}</key> <dim>›</dim> '), default=default, multiline=multi,
                                     bottom_toolbar=(' 多行输入：Enter 换行，Esc 后按 Enter 提交，Ctrl-C 放弃修改' if multi else None))
            except KeyboardInterrupt:
                say('已放弃修改', 'dim')
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
            say('无法识别的输入。' + HELP, 'bad')


def ask(text: str) -> str:
    """显示提示并读取一行输入；取消时返回空字符串。"""
    try:
        return PromptSession(style=STYLE).prompt(HTML(f'<warn>{html.escape(text)}</warn>')).strip()
    except (EOFError, KeyboardInterrupt):
        return ''


# ---------------------------------------------------------------- 词表（-w）
WORDS_HELP = ('OLD=NEW：添加或修改规则 │ -OLD 或 -编号：删除规则 │ :w 保存 │ :q 取消 │ 回车：重新显示。'
              '按子串匹配，不区分大小写；OLD 写作 /正则表达式/ 时按正则匹配')


def show_words(rules: dict) -> None:
    if not rules:
        say('（空）', 'dim')
    for i, (k, v) in enumerate(rules.items(), 1):
        say(f'{i:>3}  {k}  →  {v}')
    say(WORDS_HELP, 'dim')


def edit_words_interactive(name: str, rules: dict) -> dict | None:
    """在终端中编辑词表。

    Returns:
        修改后的规则；取消时返回 None。
    """
    rules = dict(rules)
    session = PromptSession(style=STYLE)
    say(f'{SYM["bar"]} 编辑词表 {name}', 'prompt')
    show_words(rules)
    while True:
        try:
            line = session.prompt(HTML('<prompt>words</prompt> <dim>›</dim> ')).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not line:
            show_words(rules)
        elif line in (':w', ':wq', 'w'):
            return rules
        elif line in (':q', ':q!', 'q'):
            return None
        elif line.startswith('-') and len(line) > 1:
            key = line[1:].strip()
            if key.isdigit() and 1 <= int(key) <= len(rules):
                key = list(rules)[int(key) - 1]
            if key in rules:
                del rules[key]
                say(f'已删除：{key}', 'dim')
            else:
                say(f'规则不存在：{key}', 'bad')
        elif '=' in line:
            k, _, v = line.partition('=')
            k, v = k.strip(), v.strip()
            if not k:
                say('OLD 不能为空', 'bad')
                continue
            rules[k] = v
            say(f'{k} → {v}', 'dim')
        else:
            say('无法识别的输入。' + WORDS_HELP, 'bad')
