# -*- coding: utf-8 -*-
"""nais tui：交互模式。将图片或目录拖入终端并按回车即可处理。

在终端中拖入文件，效果等同于在输入行粘贴路径（macOS 以反斜杠转义空格，Windows 加双引号）。
因此交互模式实现为带路径补全和状态栏的输入循环：每行输入的路径按当前设置立即处理，目录先请求确认。
以 / 开头的命令用于修改设置，退出时保存部分设置。
"""
from __future__ import annotations

import html
import json
import os
import shlex
from collections import Counter
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit import print_formatted_text as pt_print
from prompt_toolkit.completion import PathCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.styles import Style

from .core import IMG_EXTS, SYM, config_dir, iter_images
from .nai_strip import (describe_plan, list_presets, list_words, make_opts, name_hint, resolve_poison, resolve_words,
                        strip_one, suffix_of, summary, today)

STYLE = Style.from_dict({
    'prompt': 'bold ansicyan',
    'dim': 'ansibrightblack',
    'ok': 'ansigreen',
    'bad': 'ansired',
    'warn': 'bold ansiyellow',
})
# 退出时保存的设置。原地修改与试运行不保存，每次启动都从不修改源文件的状态开始
SAVED_KEYS = ('outdir', 'suffix', 'drop_alpha', 'strip_icc', 'scrub_all', 'recursive', 'overwrite', 'rename')

HELP = """\
将图片或目录拖入终端并按回车即可处理；处理目录前会请求确认。

输出
  /out DIR       输出到指定目录；/out - 恢复为源文件所在目录
  /suffix SUFFIX 设置输出文件名后缀
  /n             切换按“日期-序号”重命名（与 /i 同时开启时重命名源文件）
  /i             切换直接修改源文件（不保留原文件）
  /ow            切换覆盖已存在的输出文件

元数据写入
  /t SPEC        写入伪造元数据：TEXT、1（空格）、2（杂鱼）、N、edit N、@FILE、list；/t - 关闭
  /w RULE        仅替换词语：OLD=NEW、词表名称、edit NAME、list；/w - 关闭

像素处理
  /alpha         切换移除完全不透明的 alpha 通道
  /icc           切换移除 ICC 色彩配置文件
  /scrub         切换清零所有通道的最低位

其他
  /r             切换递归处理子目录
  /dry           切换试运行
  /help          显示此帮助
  /q             退出（也可按 Ctrl-D）"""


# ---------------------------------------------------------------- 设置的保存与读取
def load_settings() -> dict:
    """读取已保存的设置；文件不存在或无法解析时返回空 dict。"""
    try:
        d = json.loads((config_dir() / 'tui.json').read_text('utf-8'))
        return {k: v for k, v in d.items() if k in SAVED_KEYS}
    except Exception:
        return {}


def save_settings(opts) -> None:
    """保存 SAVED_KEYS 中的设置。写入失败时不报错。"""
    try:
        d = config_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / 'tui.json').write_text(
            json.dumps({k: getattr(opts, k) for k in SAVED_KEYS}, ensure_ascii=False, indent=1), 'utf-8')
    except Exception:
        pass


# ---------------------------------------------------------------- 输入解析
def parse_paths(line: str) -> list[Path]:
    """解析一行输入中的路径。

    支持多个拖入的路径、macOS 的反斜杠转义、Windows 的双引号，以及手动输入的含空格路径。
    """
    posix = os.name != 'nt'
    try:
        toks = shlex.split(line, posix=posix)
    except ValueError:
        toks = [line]
    if not posix:
        toks = [t.strip('"\'') for t in toks]
    paths = [Path(t).expanduser() for t in toks if t]
    whole = Path(line.strip().strip('"\'')).expanduser()
    if paths and not all(p.exists() for p in paths) and whole.exists():
        return [whole]
    return paths


# ---------------------------------------------------------------- 输出
def say(text: str, style: str = '') -> None:
    t = html.escape(text)
    pt_print(HTML(f'<{style}>{t}</{style}>' if style else t), style=STYLE)


def show_result(status: str, line: str) -> None:
    say(line, {'ok': 'ok', 'fail': 'bad', 'skip': 'dim'}.get(status, ''))


def toolbar(opts) -> HTML:
    named = f'按 {today()}-NNNN 重命名' if opts.rename else ''
    if opts.in_place:
        out = '<warn>直接修改源文件</warn>' + (f'，{named}' if named else '')
    elif opts.outdir:
        out = f'目录 {html.escape(str(opts.outdir))}' + (f'，{named}' if named else '')
    else:
        out = '源文件所在目录，' + (named or f'后缀 {html.escape(suffix_of(opts))}')
    flags = [f'alpha：{"移除" if opts.drop_alpha else "保留"}',
             f'ICC：{"移除" if opts.strip_icc else "保留"}',
             f'递归：{"开" if opts.recursive else "关"}']
    if opts.scrub_all:
        flags.append('<warn>全通道最低位清零</warn>')
    if opts.overwrite:
        flags.append('覆盖已有文件')
    if opts.dry_run:
        flags.append('<warn>试运行</warn>')
    if opts.word_rules:
        desc = ' '.join(opts.word_presets) if opts.word_presets else ' '.join(f'{k}→{v}' for k, v in opts.word_rules.items())
        flags.append(f'<warn>词语替换：{html.escape(desc[:24])}</warn>')
    elif opts.poison or opts.sets:
        desc = opts.poison if opts.poison_meta is None else f'预设/模板 {opts.poison}'
        flags.append(f'<warn>投毒：{html.escape(str(desc)[:24])}</warn>')
    return HTML(f' 输出：{out}  │  ' + '  │  '.join(flags) + '  │  /help')


# ---------------------------------------------------------------- 命令
def _toggle(opts, key: str, label: str) -> None:
    setattr(opts, key, not getattr(opts, key))
    say(f'{label}：{"已开启" if getattr(opts, key) else "已关闭"}', 'dim')


COMMANDS = {'/q', '/quit', '/exit', '/help', '/h', '/?', '/out', '/suffix', '/i', '/inplace', '/alpha', '/icc',
            '/r', '/recursive', '/scrub', '/dry', '/ow', '/overwrite', '/t', '/w', '/n', '/rename'}


def is_command(line: str) -> bool:
    """判断输入是否为命令。仅识别 COMMANDS 中的命令，因为 macOS 与 Linux 的绝对路径同样以 / 开头。"""
    return line.split(maxsplit=1)[0].lower() in COMMANDS


def handle_command(line: str, opts) -> bool:
    """执行一条命令。返回 False 表示退出。"""
    cmd, _, arg = line.partition(' ')
    cmd, arg = cmd.lower(), arg.strip()
    if cmd in ('/q', '/quit', '/exit'):
        return False
    if cmd in ('/help', '/h', '/?'):
        say(HELP)
    elif cmd == '/out':
        if arg in ('', '-'):
            opts.outdir = None
            say(f'输出：源文件所在目录，后缀 {suffix_of(opts)}', 'dim')
        else:
            p = parse_paths(arg)[0]
            opts.outdir, opts.in_place = str(p), False
            say(f'输出：目录 {p}' + ('' if p.is_dir() else '（不存在，将在写入时创建）'), 'dim')
    elif cmd == '/suffix':
        if arg:
            opts.suffix = arg
        say(f'后缀：{suffix_of(opts)}', 'dim')
    elif cmd == '/w':
        if arg in ('', '-'):
            opts.words, opts.word_rules, opts.word_presets = [], {}, []
            say('词语替换：已关闭', 'dim')
        elif arg == 'list':
            say(list_words())
        else:
            opts.words = list(opts.words) + [arg]
            try:
                rc = resolve_words(opts)
            except Exception as e:
                rc = 1
                say(f'错误：{e}', 'bad')
            if rc == 1:
                opts.words = opts.words[:-1]
                resolve_words(opts)
            else:
                opts.poison, opts.poison_meta = None, None      # 与 -t 互斥
                say('词语替换：' + '、'.join(f'{k}→{v}' for k, v in opts.word_rules.items()), 'warn')
    elif cmd == '/t':
        if arg in ('', '-'):
            opts.poison, opts.poison_meta = None, None
            say('投毒：已关闭', 'dim')
        elif arg == 'list':
            say(list_presets())
        else:
            opts.poison = arg
            try:
                rc = resolve_poison(opts)
            except Exception as e:
                rc = 1
                say(f'错误：{e}', 'bad')
            if rc == 1:
                opts.poison, opts.poison_meta = None, None
            else:
                opts.words, opts.word_rules, opts.word_presets = [], {}, []   # 与 -w 互斥
                say(f'投毒：{"预设/模板 " if opts.poison_meta is not None else "文本 "}{arg}', 'warn')
    elif cmd in ('/i', '/inplace'):
        _toggle(opts, 'in_place', '直接修改源文件')
        if opts.in_place:
            opts.outdir = None
            say('警告：处理后不保留原文件', 'warn')
    elif cmd in ('/n', '/rename'):
        _toggle(opts, 'rename', '按“日期-序号”重命名')
    elif cmd == '/alpha':
        _toggle(opts, 'drop_alpha', '移除 alpha 通道')
    elif cmd == '/icc':
        _toggle(opts, 'strip_icc', '移除 ICC 色彩配置文件')
    elif cmd in ('/r', '/recursive'):
        _toggle(opts, 'recursive', '递归处理子目录')
    elif cmd == '/scrub':
        _toggle(opts, 'scrub_all', '全通道最低位清零')
    elif cmd == '/dry':
        _toggle(opts, 'dry_run', '试运行')
    elif cmd in ('/ow', '/overwrite'):
        _toggle(opts, 'overwrite', '覆盖已存在的输出文件')
    else:
        say(f'错误：未知命令 {cmd}（输入 /help 查看命令）', 'bad')
    return True


# ---------------------------------------------------------------- 处理
def process(paths: list[Path], opts, confirm) -> None:
    """处理一行输入中的全部路径。目录先显示摘要并请求确认。"""
    items = []
    for p in paths:
        if p.is_dir():
            found = list(iter_images([p], opts.recursive))
            if not found:
                say(f'{p.name}/ 中没有图片文件' + ('' if opts.recursive else '（输入 /r 开启递归）'), 'dim')
                continue
            say(describe_plan(found, opts, f'目录 {p.name}/'))
            if confirm('是否继续？[y/N] '):
                items += found
            else:
                say('已跳过', 'dim')
        elif p.is_file():
            if p.suffix.lower() in IMG_EXTS:
                items.append((p, Path(p.name)))
            else:
                say(f'已跳过非图片文件：{p.name}', 'dim')
        else:
            say(f'错误：{p} 不存在', 'bad')
    if not items:
        return
    vars(opts).pop('_numbers', None)             # 每批重新读取目录中已有的序号
    counts = Counter()
    for src, rel in items:
        try:
            status, line = strip_one(src, rel, opts)
        except KeyboardInterrupt:
            say('已中断', 'warn')
            break
        show_result(status, line)
        counts[status] += 1
    if len(items) > 1:
        say(summary(counts), 'dim')
    hint = None if vars(opts).get('_hinted') else name_hint(items, opts)
    if hint:                                     # 每次会话只提示一次
        say(f'警告：{hint[0]}', 'warn')
        say('提示：输入 /n 开启按“日期-序号”重命名', 'dim')
        opts._hinted = True


def run_tui(argv=None) -> int:
    """运行交互模式。argv 中的第一个参数（如有）作为输出目录。"""
    opts = make_opts(**load_settings())
    try:
        cfg = config_dir()
        cfg.mkdir(parents=True, exist_ok=True)
        history = FileHistory(str(cfg / 'history'))
    except Exception:
        history = InMemoryHistory()
    session = PromptSession(history=history, completer=PathCompleter(expanduser=True),
                            complete_while_typing=False, bottom_toolbar=lambda: toolbar(opts), style=STYLE)
    ask = PromptSession(style=STYLE)

    def confirm(q: str) -> bool:
        try:
            return ask.prompt(HTML(f'<warn>{html.escape(q)}</warn>')).strip().lower() in ('y', 'yes')
        except (EOFError, KeyboardInterrupt):
            return False

    say(f'{SYM["bar"]} nai-strip 交互模式', 'prompt')
    say('将图片或目录拖入终端并按回车即可处理。输入 /help 查看命令，/q 退出。', 'dim')
    for a in argv or []:
        handle_command(f'/out {a}', opts)
    while True:
        try:
            line = session.prompt(HTML('<prompt>nais</prompt> <dim>›</dim> ')).strip()
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        if not line:
            continue
        if is_command(line):
            if not handle_command(line, opts):
                break
            continue
        if line.lower() in ('q', 'quit', 'exit') and not Path(line).exists():
            break
        process(parse_paths(line), opts, confirm)
    save_settings(opts)
    say('设置已保存', 'dim')
    return 0
