# -*- coding: utf-8 -*-
"""nai：统一入口，将子命令分发给各命令的 main 函数。

新增子命令时，在 COMMANDS 中添加一项 (名称, 入口函数, 说明)。入口函数的签名为
``main(argv: list[str], prog: str) -> int``，自行解析 argv。
gen 与 agent 两个名称保留给后续的生成功能（通过 NovelAI API 生成图片）。
"""
from __future__ import annotations

import sys

from . import __version__
from .nai_inspect import main as inspect_main
from .nai_strip import main as strip_main

COMMANDS = [
    (('i', 'inspect'), inspect_main, '读取生成参数（等同于 naii）'),
    (('s', 'strip'), strip_main, '移除元数据（等同于 nais；nai s tui 进入交互模式）'),
]


def usage() -> str:
    lines = ['用法：nai <命令> [选项] [参数...]', '', '命令：']
    for names, _, desc in COMMANDS:
        lines.append(f'  {", ".join(names):<14}{desc}')
    lines += ['', '选项：',
              f'  {"-h, --help":<14}显示此帮助信息并退出',
              f'  {"-V, --version":<14}显示版本信息并退出',
              '', '使用 nai <命令> -h 查看该命令的选项。']
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
        print(f'nai-meta {__version__}')
        return 0
    for names, fn, _ in COMMANDS:
        if argv[0] in names:
            return fn(argv[1:], prog=f'nai {names[0]}')
    print(f'错误：未知命令：{argv[0]}\n提示：使用 nai -h 查看可用命令', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
