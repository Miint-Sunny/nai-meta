# -*- coding: utf-8 -*-
"""nais tui：把图片或文件夹拖进终端，回车就处理。

终端里「拖文件」= 把路径粘贴到输入行（macOS 用反斜杠转义空格，Windows 用双引号），
所以这个 TUI 本质是一个带补全、带状态栏的输入循环：每行路径立刻按当前设置处理，
文件夹先报数量再问 y/N。设置用 / 开头的命令切换，退出时记住输出目录等设置。
"""
from __future__ import annotations

import html
import json
import os
import shlex
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit import print_formatted_text as pt_print
from prompt_toolkit.completion import PathCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.styles import Style

from .core import IMG_EXTS, SYM, iter_images
from .nai_strip import describe_plan, make_opts, strip_one

STYLE = Style.from_dict({
    'prompt': 'bold ansicyan',
    'dim': 'ansibrightblack',
    'ok': 'ansigreen',
    'bad': 'ansired',
    'warn': 'bold ansiyellow',
})
# 退出时记住的设置。原地覆盖、dry-run 故意不记：每次进来都该从安全状态开始
SAVED_KEYS = ('outdir', 'suffix', 'drop_alpha', 'strip_icc', 'scrub_all', 'recursive', 'overwrite')

HELP = """\
拖图片 / 文件夹进来，回车即处理（文件夹会先问 y/N）。命令：
  /out <目录>     输出到指定目录            /out -     恢复写在原图旁边
  /suffix <后缀>  旁边模式的文件名后缀      /i         切换原地覆盖（不留备份）
  /alpha          切换去 alpha 通道         /icc       切换去 ICC 色彩配置
  /r              切换文件夹递归            /scrub     切换全通道 LSB 清零
  /dry            切换 dry-run              /ow        切换覆盖同名输出
  /help           这份说明                  /q         退出（Ctrl-D 也行）"""


# ---------------------------------------------------------------- 设置持久化
def config_dir() -> Path:
    if os.name == 'nt':
        base = Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming')
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return base / 'nai-meta'


def load_settings() -> dict:
    try:
        d = json.loads((config_dir() / 'tui.json').read_text('utf-8'))
        return {k: v for k, v in d.items() if k in SAVED_KEYS}
    except Exception:
        return {}


def save_settings(opts) -> None:
    try:
        d = config_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / 'tui.json').write_text(
            json.dumps({k: getattr(opts, k) for k in SAVED_KEYS}, ensure_ascii=False, indent=1), 'utf-8')
    except Exception:
        pass


# ---------------------------------------------------------------- 输入解析
def parse_paths(line: str) -> list[Path]:
    """一行里可能有多个拖进来的路径。macOS 反斜杠转义空格，Windows 加双引号；手敲的带空格路径也认。"""
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


def show_result(line: str) -> None:
    head = line[:1]
    say(line, 'ok' if head == SYM['ok'] else 'bad' if head == SYM['bad'] else 'dim' if head in '·—' else '')


def toolbar(opts) -> HTML:
    if opts.in_place:
        out = '<warn>原地覆盖</warn>'
    elif opts.outdir:
        out = f'目录 {html.escape(str(opts.outdir))}'
    else:
        out = f'原图旁边 +{html.escape(opts.suffix)}'
    flags = [f'去alpha {"开" if opts.drop_alpha else "关"}',
             f'ICC {"去" if opts.strip_icc else "留"}',
             f'递归 {"开" if opts.recursive else "关"}']
    if opts.scrub_all:
        flags.append('<warn>全LSB清零</warn>')
    if opts.overwrite:
        flags.append('覆盖同名')
    if opts.dry_run:
        flags.append('<warn>dry-run</warn>')
    return HTML(f' 输出: {out}   ·   ' + '   ·   '.join(flags) + '   ·   /help')


# ---------------------------------------------------------------- 命令
def _toggle(opts, key: str, label: str) -> None:
    setattr(opts, key, not getattr(opts, key))
    say(f'{label}: {"开" if getattr(opts, key) else "关"}', 'dim')


def handle_command(line: str, opts) -> bool:
    """返回 False 表示退出。"""
    cmd, _, arg = line.partition(' ')
    cmd, arg = cmd.lower(), arg.strip()
    if cmd in ('/q', '/quit', '/exit'):
        return False
    if cmd in ('/help', '/h', '/?'):
        say(HELP)
    elif cmd == '/out':
        if arg in ('', '-'):
            opts.outdir = None
            say(f'输出: 写在原图旁边，后缀 {opts.suffix}', 'dim')
        else:
            p = parse_paths(arg)[0]
            opts.outdir, opts.in_place = str(p), False
            say(f'输出: 目录 {p}' + ('' if p.is_dir() else '（不存在，写入时创建）'), 'dim')
    elif cmd == '/suffix':
        if arg:
            opts.suffix = arg
        say(f'后缀: {opts.suffix}', 'dim')
    elif cmd in ('/i', '/inplace'):
        _toggle(opts, 'in_place', '原地覆盖')
        if opts.in_place:
            opts.outdir = None
            say(f'{SYM["warn"]} 原地覆盖不留备份，确定再拖', 'warn')
    elif cmd == '/alpha':
        _toggle(opts, 'drop_alpha', '去 alpha')
    elif cmd == '/icc':
        _toggle(opts, 'strip_icc', '去 ICC')
    elif cmd in ('/r', '/recursive'):
        _toggle(opts, 'recursive', '文件夹递归')
    elif cmd == '/scrub':
        _toggle(opts, 'scrub_all', '全通道 LSB 清零')
    elif cmd == '/dry':
        _toggle(opts, 'dry_run', 'dry-run')
    elif cmd in ('/ow', '/overwrite'):
        _toggle(opts, 'overwrite', '覆盖同名输出')
    else:
        say(f'未知命令 {cmd}，/help 看说明', 'bad')
    return True


# ---------------------------------------------------------------- 处理
def process(paths: list[Path], opts, confirm) -> None:
    items = []
    for p in paths:
        if p.is_dir():
            found = list(iter_images([p], opts.recursive))
            if not found:
                say(f'{p.name}/ 里没有图片' + ('' if opts.recursive else '（/r 可开递归）'), 'dim')
                continue
            say(describe_plan(found, opts, f'文件夹 {p.name}/'))
            if confirm('处理？[y/N] '):
                items += found
            else:
                say('跳过', 'dim')
        elif p.is_file():
            if p.suffix.lower() in IMG_EXTS:
                items.append((p, Path(p.name)))
            else:
                say(f'跳过非图片: {p.name}', 'dim')
        else:
            say(f'找不到: {p}', 'bad')
    if not items:
        return
    ok = fail = 0
    for src, rel in items:
        try:
            good, line = strip_one(src, rel, opts)
        except KeyboardInterrupt:
            say('中断', 'warn')
            break
        show_result(line)
        ok += good
        fail += not good
    if len(items) > 1:
        say(f'—— 共 {len(items)} 张：成功 {ok}，失败/跳过 {fail}', 'dim')


def run_tui(argv=None) -> int:
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
    say('把图片或文件夹拖进来，回车即处理；文件夹会先问 y/N。/help 看命令，/q 退出。', 'dim')
    for a in argv or []:                          # nais tui <目录> 直接当输出目录
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
        if line.startswith('/'):
            if not handle_command(line, opts):
                break
            continue
        if line.lower() in ('q', 'quit', 'exit') and not Path(line).exists():
            break
        process(parse_paths(line), opts, confirm)
    save_settings(opts)
    say('设置已记住，再见', 'dim')
    return 0
