# -*- coding: utf-8 -*-
"""nai：伞形总入口。现在挂两个子命令，以后的生图 agent 之类也往这里挂。

加子命令只需往 COMMANDS 里加一行：(名字们, 入口函数, 一句说明)。
入口函数签名 main(argv: list[str]) -> int，自己用 argparse 解析 argv。
已预留的名字：gen / agent（LLM 驱动 NovelAI API 生图）。
"""
from __future__ import annotations

import sys

from . import __version__
from .nai_inspect import main as inspect_main
from .nai_strip import main as strip_main

COMMANDS = [
    # (名字们, 入口, 说明)
    (('i', 'inspect'), inspect_main, '读出生成参数        （同 naii / nai-inspect）'),
    (('s', 'strip'), strip_main, '剥掉元数据          （同 nais / nai-strip；nai s tui 进交互模式）'),
]


def usage() -> str:
    lines = ['用法: nai <子命令> [参数...]', '']
    for names, _, desc in COMMANDS:
        lines.append(f'  nai {" | ".join(names):<16} {desc}')
    lines += ['', '  nai i a.png           nai s -i *.png           nai s tui           nai s -h 看全部选项']
    return '\n'.join(lines)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(usage(), file=sys.stderr)
        return 1
    if argv[0] in ('-h', '--help'):
        print(usage())
        return 0
    if argv[0] in ('-V', '--version'):
        print(f'pynai {__version__}')
        return 0
    for names, fn, _ in COMMANDS:
        if argv[0] in names:
            return fn(argv[1:])
    print(f'未知子命令: {argv[0]}\n\n{usage()}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
