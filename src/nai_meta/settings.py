# -*- coding: utf-8 -*-
"""开关：可以保存为默认值的选项，命令行与交互模式共用。

开关保存在配置目录的 settings.json 中。取值的优先级从高到低依次为：命令行中明确指定的选项、开关、内置默认值。

只有不会造成不可恢复后果、也不改变操作性质的选项才提供开关。永久删除、原地修改、试运行、覆盖已有文件、
跳过确认、跳过验证、伪造元数据写入和词语替换均不提供开关，只能逐次指定。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .argparse_zh import ArgumentParser, display_width
from .core import config_dir, error
from .trash import UNSUPPORTED as TRASH_UNSUPPORTED
from .trash import supported as trash_supported


@dataclass(frozen=True)
class Switch:
    name: str        # settings.json 与 nais config 使用的名称
    kind: type       # bool 或 str
    option: str      # 对应的命令行选项
    desc: str        # 说明
    on_text: str     # 启用时在运行提示中的描述；str 类型以 {} 代表取值

    @property
    def attr(self) -> str:
        """选项对象中的属性名。"""
        return self.name.replace('-', '_')

    @property
    def default(self):
        return False if self.kind is bool else None


SWITCHES = (
    Switch('trash', bool, '--trash', '处理成功后将原文件移到废纸篓', '处理后原文件移到废纸篓'),
    Switch('rename', bool, '-n, --rename', '按“日期-序号”重命名输出文件', '按“日期-序号”重命名'),
    Switch('outdir', str, '-d, --outdir', '输出目录', '输出目录 {}'),
    Switch('suffix', str, '--suffix', '输出文件名后缀', '后缀 {}'),
    Switch('recursive', bool, '-r, --recursive', '递归处理子目录', '递归处理子目录'),
    Switch('drop-alpha', bool, '--drop-alpha', '移除完全不透明的 alpha 通道', '移除 alpha 通道'),
    Switch('strip-icc', bool, '--strip-icc', '移除 ICC 色彩配置文件', '移除 ICC 色彩配置文件'),
    Switch('scrub-all', bool, '--scrub-all', '清零所有通道的最低位', '所有通道最低位清零'),
)
BY_NAME = {s.name: s for s in SWITCHES}
BY_ATTR = {s.attr: s for s in SWITCHES}
TRUE_WORDS = ('on', 'true', 'yes', '1', '开')
FALSE_WORDS = ('off', 'false', 'no', '0', '关')


def settings_path() -> Path:
    return config_dir() / 'settings.json'


def _valid(sw: Switch, value) -> bool:
    if sw.kind is bool:
        return isinstance(value, bool)
    return value is None or (isinstance(value, str) and value != '')


def _migrate_legacy() -> dict:
    """读取旧版交互模式的 tui.json，转换为 settings.json 的格式并保存。

    旧版文件中的 overwrite 不再是开关，予以丢弃；suffix 为 _clean 时与内置默认值相同，视为未设置。
    """
    legacy = config_dir() / 'tui.json'
    try:
        old = json.loads(legacy.read_text('utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(old, dict):
        return {}
    raw = {}
    for sw in SWITCHES:
        v = old.get(sw.attr)
        if sw.name == 'suffix' and v == '_clean':
            v = None
        if _valid(sw, v) and v != sw.default:
            raw[sw.name] = v
    _write(raw)
    return raw


def _read() -> dict:
    p = settings_path()
    if not p.exists():
        return _migrate_legacy()
    try:
        raw = json.loads(p.read_text('utf-8'))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(raw: dict) -> Path:
    p = settings_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        full = {sw.name: raw.get(sw.name, sw.default) for sw in SWITCHES}
        p.write_text(json.dumps(full, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    except OSError:
        pass
    return p


def load() -> dict:
    """读取全部开关，返回 {属性名: 取值}。未设置或取值无效的开关使用内置默认值。"""
    raw = _read()
    out = {}
    for sw in SWITCHES:
        v = raw.get(sw.name, sw.default)
        out[sw.attr] = v if _valid(sw, v) else sw.default
    if out['outdir']:
        out['outdir'] = str(Path(out['outdir']).expanduser())
    return out


def set_value(attr: str, value) -> Path:
    """保存单个开关。只修改该项，不覆盖其他进程同时修改的开关。返回设置文件路径。"""
    sw = BY_ATTR[attr]
    raw = {k: v for k, v in _read().items() if k in BY_NAME}
    raw[sw.name] = value if _valid(sw, value) else sw.default
    return _write(raw)


def reset() -> Path:
    """将全部开关恢复为内置默认值。"""
    return _write({})


def describe(attr: str, value) -> str:
    """返回开关取值的显示文本。"""
    if BY_ATTR[attr].kind is bool:
        return '开' if value else '关'
    return '未设置' if value is None else str(value)


def parse_value(sw: Switch, text: str):
    """解析 nais config 的取值。``-`` 表示恢复默认值。

    Raises:
        ValueError: 取值无效。
    """
    t = text.strip()
    if t == '-':
        return sw.default
    if sw.kind is bool:
        if t.lower() in TRUE_WORDS:
            return True
        if t.lower() in FALSE_WORDS:
            return False
        raise ValueError(f'{sw.name} 的取值应为 on 或 off：{text}')
    if not t:
        raise ValueError(f'{sw.name} 的取值不能为空')
    return t


def active_from_file(applied: list[str], opts) -> list[str]:
    """返回本次运行中取自设置文件、且不同于默认值的开关描述，用于运行前的提示。"""
    out = []
    for attr in applied:
        sw = BY_ATTR[attr]
        v = getattr(opts, attr)
        if v != sw.default:
            out.append(sw.on_text.format(v) if sw.kind is str else sw.on_text)
    return out


# ---------------------------------------------------------------- nais config
def _table(values: dict) -> str:
    width = max(display_width(describe(sw.attr, values[sw.attr])) for sw in SWITCHES)
    lines = [f'开关（设置文件：{settings_path()}）：']
    for sw in SWITCHES:
        shown = describe(sw.attr, values[sw.attr])
        pad = ' ' * (width - display_width(shown))
        lines.append(f'  {sw.name:<11}{shown}{pad}  {sw.desc}（{sw.option}）')
    return '\n'.join(lines)


def config_main(argv: list[str], prog: str) -> int:
    """``nais config``：查看或修改开关。"""
    names = ', '.join(BY_NAME)
    ap = ArgumentParser(
        prog=prog,
        description='查看或修改开关。开关是可以保存为默认值的选项，命令行与交互模式共用；\n'
                    '命令行中明确指定的选项优先于开关。',
        epilog=f'''\
可用的 KEY：
  {names}

VALUE 的取值：
  on、off         布尔开关
  任意文本        outdir 与 suffix
  -               恢复内置默认值

示例：
  {prog}                    列出全部开关
  {prog} trash on           处理后将原文件移到废纸篓
  {prog} outdir ~/clean     默认输出到 ~/clean
  {prog} outdir -           取消默认输出目录
  {prog} --reset            将全部开关恢复为默认值''')
    ap.add_argument('key', nargs='?', metavar='KEY', choices=list(BY_NAME), help='开关名称')
    ap.add_argument('value', nargs='?', metavar='VALUE', help='新的取值；省略时显示当前取值')
    ap.add_argument('--reset', action='store_true', help='将全部开关恢复为内置默认值')
    a = ap.parse_args(argv)

    if a.reset:
        if a.key:
            error('--reset 不能与 KEY 同时使用')
            return 2
        print(f'已将全部开关恢复为默认值：{reset()}')
        return 0
    values = load()
    if a.key is None:
        print(_table(values))
        print(f'使用 {prog} KEY VALUE 修改开关，VALUE 为 - 时恢复默认值')
        return 0
    sw = BY_NAME[a.key]
    if a.value is None:
        print(f'{sw.name}：{describe(sw.attr, values[sw.attr])}')
        return 0
    try:
        value = parse_value(sw, a.value)
    except ValueError as e:
        error(str(e))
        return 2
    if sw.name == 'trash' and value and not trash_supported():
        error(TRASH_UNSUPPORTED)
        return 1
    path = set_value(sw.attr, value)
    print(f'已设置 {sw.name}：{describe(sw.attr, value)}（{path}）')
    return 0
