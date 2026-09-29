# -*- coding: utf-8 -*-
"""argparse 的简体中文界面。

提供三项功能：

- 翻译 argparse 内置的界面文字（用法行、分组标题、错误信息）。argparse 在调用时才查找模块级的
  ``_`` 和 ``ngettext``，替换这两个名字即可生效；未收录的文字保持英文原样。
- 按终端显示宽度折行的帮助格式。argparse 按字符数折行，一个汉字占两列，中文说明会超出右边界。
- 统一的参数错误输出：用法行、``错误：…``、``提示：…``，退出码 2。
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import unicodedata

ZH = {
    'usage: ': '用法：',
    '%(heading)s:': '%(heading)s：',
    'positional arguments': '位置参数',
    'options': '选项',
    'show this help message and exit': '显示此帮助信息并退出',
    "show program's version number and exit": '显示版本信息并退出',
    ' (default: %(default)s)': '（默认：%(default)s）',
    'argument %(argument_name)s: %(message)s': '参数 %(argument_name)s：%(message)s',
    'unrecognized arguments: %s': '无法识别的参数：%s',
    'not allowed with argument %s': '不能与参数 %s 同时使用',
    'ambiguous option: %(option)s could match %(matches)s': '选项有歧义：%(option)s 可匹配 %(matches)s',
    'ignored explicit argument %r': '已忽略显式参数 %r',
    'the following arguments are required: %s': '缺少必需的参数：%s',
    'one of the arguments %s is required': '必须指定以下参数之一：%s',
    'expected one argument': '需要一个值',
    'expected at most one argument': '最多接受一个值',
    'expected at least one argument': '至少需要一个值',
    'invalid %(type)s value: %(value)r': '无效的 %(type)s 值：%(value)r',
    'invalid choice: %(value)r (choose from %(choices)s)': '无效的取值：%(value)r（可选：%(choices)s）',
}
ZH_PLURAL = {
    'expected %s argument': '需要 %s 个值',
}


def _gettext(msg: str) -> str:
    return ZH.get(msg, msg)


def _ngettext(singular: str, plural: str, n: int) -> str:
    return ZH_PLURAL.get(singular, singular if n == 1 else plural)


def install() -> None:
    """替换 argparse 的翻译函数。重复调用无副作用。"""
    argparse._ = _gettext
    argparse.ngettext = _ngettext


# ---------------------------------------------------------------- 按显示宽度折行
def display_width(text: str) -> int:
    """返回文本在等宽终端中占用的列数（全角与宽字符计 2 列）。"""
    return sum(0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in 'WF' else 1
               for ch in text)


# 折行单位：连续空白、一串非宽字符（英文单词、选项名、路径）、单个宽字符连同其后的行尾禁用标点
_CLOSING = '，。、；：？！）」』》”’…'
_TOKEN = re.compile(rf'\s+|[^\s⺀-鿿豈-﫿＀-￯　-〿]+[{_CLOSING}]*'
                    rf'|[⺀-鿿豈-﫿＀-￯　-〿][{_CLOSING}]*')


def wrap(text: str, width: int) -> list[str]:
    """按显示宽度贪心折行。宽字符之间可断行；英文单词不拆开；行首不出现句读标点。"""
    lines, cur, cur_w = [], '', 0
    for tok in _TOKEN.findall(' '.join(text.split())):
        if tok.isspace():
            if cur:
                cur, cur_w = cur + ' ', cur_w + 1
            continue
        w = display_width(tok)
        if cur.strip() and cur_w + w > width:
            lines.append(cur.rstrip())
            cur, cur_w = '', 0
        cur, cur_w = cur + tok, cur_w + w
    if cur.strip():
        lines.append(cur.rstrip())
    return lines or ['']


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """选项说明按显示宽度折行；description 与 epilog 保留原有换行。"""

    def __init__(self, prog: str, indent_increment: int = 2, max_help_position: int = 30, width: int | None = None):
        if width is None:
            width = min(shutil.get_terminal_size((100, 24)).columns, 100) - 2
        super().__init__(prog, indent_increment, max_help_position, width)

    def _split_lines(self, text: str, width: int) -> list[str]:
        return wrap(text, width)


class ArgumentParser(argparse.ArgumentParser):
    """使用中文界面文字与 :class:`HelpFormatter` 的 ArgumentParser。"""

    def __init__(self, *args, **kwargs):
        install()
        kwargs.setdefault('formatter_class', HelpFormatter)
        super().__init__(*args, **kwargs)

    def error(self, message: str):
        self.print_usage(sys.stderr)
        self.exit(2, f'错误：{message}\n提示：使用 {self.prog} -h 查看全部选项\n')
