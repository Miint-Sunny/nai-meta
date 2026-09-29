# nai-meta

读取、移除和改写 NovelAI 生成图片中的元数据（PNG 文本块、EXIF 与 LSB 隐写），不修改像素数据。

- [背景](#背景)
- [安装](#安装)
- [用法](#用法)
  - [命令概览](#命令概览)
  - [naii：读取生成参数](#naii读取生成参数)
  - [nais：移除元数据](#nais移除元数据)
  - [按日期重命名（-N）](#按日期重命名-n)
  - [写入伪造元数据（-t）](#写入伪造元数据-t)
  - [替换词语（-w）](#替换词语-w)
  - [交互模式（nais tui）](#交互模式nais-tui)
  - [退出码](#退出码)
- [配置文件](#配置文件)
- [工作原理](#工作原理)
- [限制与注意事项](#限制与注意事项)
- [相关项目](#相关项目)
- [开发](#开发)
- [许可证](#许可证)

## 背景

NovelAI 生成的图片包含两层内容相同的元数据：

- **明文层**：PNG 使用文本块，WebP 使用 EXIF。exiftool 等通用工具可以读取；转发平台通常会移除这一层。
- **LSB 隐写层**：写入 alpha 通道各像素的最低位。novelai.net/inspect 读取的是这一层；只要图像没有重新编码，这一层就会保留。

只移除明文层无法清除生成参数。现有能清除隐写层的开源工具会删除整个 alpha 通道。nai-meta 同时处理两层：
对隐写层，只恢复被隐写数据占用的最低位，其余像素保持原值。

nai-meta 提供以下功能：

- 读取生成参数，并比对两层内容是否一致。
- 移除全部元数据。
- 移除后写入伪造元数据（下文称“投毒”）。
- 仅替换元数据中的指定词语，其余内容保持不变。

## 安装

需要 [uv](https://docs.astral.sh/uv/)。uv 会自动安装所需的 Python（3.10 或更高版本），并为本工具创建独立环境。

从 GitHub 安装：

```bash
uv tool install git+https://github.com/Miint-Sunny/nai-meta
```

也可以克隆仓库后从本地源码安装：

```bash
git clone https://github.com/Miint-Sunny/nai-meta.git
cd nai-meta
uv tool install .
```

检查是否安装成功：

```bash
nais --version
```

| 操作 | 命令 |
|---|---|
| 更新（从 GitHub 安装） | `uv tool install --reinstall git+https://github.com/Miint-Sunny/nai-meta` |
| 更新（从本地源码安装） | 在仓库目录中执行 `git pull`，再执行 `uv tool install . --reinstall` |
| 不安装，直接运行一次 | `uvx --from git+https://github.com/Miint-Sunny/nai-meta naii a.png` |
| 卸载 | `uv tool uninstall nai-meta` |

在 Windows 上，先在 PowerShell 中安装 uv，再执行上面的安装命令，最后把命令目录加入 PATH：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv tool install git+https://github.com/Miint-Sunny/nai-meta
uv tool update-shell
```

执行 `uv tool update-shell` 后，需要重新打开终端。

## 用法

### 命令概览

| 命令 | 等价写法 | 功能 |
|---|---|---|
| `naii` | `nai i`、`nai-inspect` | 读取生成参数 |
| `nais` | `nai s`、`nai-strip` | 移除元数据；使用 `-t` 时写入伪造元数据；使用 `-w` 时替换词语；`nais tui` 进入交互模式 |
| `nai` | | 统一入口，不带参数时显示用法 |

各命令均支持 `-h` 查看完整选项，支持 `-V` 查看版本。下文统一使用 `naii` 和 `nais`。

### naii：读取生成参数

```bash
naii a.png b.png             # 显示生成参数
naii -r ./images             # 递归读取目录
naii -p a.png | pbcopy       # 仅输出正向提示词（含角色提示词），复制到剪贴板（macOS）
naii -j a.png > a.json       # 以 JSON 格式输出
naii --stealth a.png         # 仅读取隐写层
```

| 选项 | 说明 |
|---|---|
| `--text` | 仅读取明文层（PNG 文本块或 WebP 的 EXIF） |
| `--stealth` | 仅读取 LSB 隐写层 |
| `-f`, `--full` | 显示 Comment 中的全部字段 |
| `--raw` | 附加显示原始文本块与隐写层 JSON |
| `-j`, `--json` | 以 JSON 格式输出。单个文件输出为对象，多个文件输出为数组 |
| `-p`, `--prompt` | 仅输出正向提示词与角色提示词。多个文件以 `# ===== 文件名` 分隔 |
| `-r`, `--recursive` | 递归处理子目录 |

默认先显示明文层；明文层不存在时，显示隐写层。输出示例：

```
━━ a.png   PNG · RGBA · 2176×896
元数据    文本块 ✓ 6 个 · 隐写 ✓ alpha+gzip 12406 B · 两层一致 · 显示来源：文本块

模型      NovelAI Diffusion V5 · 哈希 0ADF9AB7
类型      图生图 i2i · 强度 0.7 · 噪声 0.8
附加      Vibe Transfer ×2（强度 0.6, 0.35 · 信息提取 1, 0.8） · 角色参考 ×1（强度 1）
尺寸      2176×896   耗时 6.6 s
采样      Euler Ancestral (k_euler_ancestral) · karras · 28 步
引导      Prompt Guidance 5.5 · Rescale 0.2
种子      1699232568
开关      Variety+ · 质量标签 · UC 预设 #2
签名      7Pc89N+E8gjW…（未验证）

─── 正向 ──────────────────────────────────────────────────────
1girl, ...

─── 角色 1 @ (0.3, 0.5) ────────────────────────────────────────
girl, red hair, smile

─── 负面 ──────────────────────────────────────────────────────
nsfw, lowres, ...

─── 角色 1 负面 ────────────────────────────────────────────────
hat
```

各行说明：

| 行 | 内容 |
|---|---|
| 元数据 | 存在哪些层、隐写数据的大小、两层是否一致，以及当前显示的是哪一层。两层均为 NovelAI 元数据但内容不同时，标出 ⚠ 并列出差异字段。以下差异属于正常情况，不计入：两层的签名各自独立；隐写层不保存参考图等大字段 |
| 类型 | 文生图、图生图、局部重绘、增强（Enhance）或导演工具（emotion、lineart 等，附 defry 值）。图生图类显示强度和噪声 |
| 附加 | Vibe Transfer、角色参考、ControlNet 及其强度 |
| 尺寸 | 生成尺寸。与文件的实际尺寸不同时（例如图片经过放大），两者都显示 |
| 开关 | 仅列出已启用的项：Variety+、Decrisper、SMEA、质量标签、UC 预设、透明背景、Upscale、角色坐标 |
| 角色区块 | 按 NovelAI 的角色序号编号，负面区块中的“角色 2”对应正向区块中的“角色 2”。启用角色坐标时，同时显示坐标 |

模型名的取值顺序：先取 Comment 中的模型名；没有时从 Source 字段解析；仍无法确定时，按模型哈希对照表识别。

也能读取非 NovelAI 格式：

- A1111、Forge 的 `parameters` 文本（位于 PNG 文本块或 EXIF UserComment）以相同格式显示。
- ComfyUI 工作流只显示节点数。
- 其他文本块和 EXIF 字段按原样列出；过长的内容只显示长度，使用 `-f` 显示全文。

### nais：移除元数据

```bash
nais a.png                    # 输出 a_clean.png，与源文件位于同一目录
nais a.png -o clean.png       # 指定输出文件
nais -r ./images -d ./clean   # 递归处理目录，输出目录中保留相对路径
nais -i *.png                 # 直接修改源文件，不保留原文件
nais -n ./images              # 试运行，只报告将执行的操作
```

| 选项 | 说明 |
|---|---|
| `-o FILE` | 输出文件，只能用于单个输入文件 |
| `-d DIR` | 输出目录。输入为目录时，保留相对路径 |
| `-i` | 直接修改源文件，不保留原文件 |
| `--suffix SUFFIX` | 输出到源文件所在目录时追加的文件名后缀。默认为 `_clean`；使用 `-t` 时为 `_poison`；使用 `-w` 时为词表名或 `_w` |
| `-N`, `--rename` | 按“日期-序号”重命名输出文件，详见[下文](#按日期重命名-n) |
| `--overwrite` | 覆盖已存在的输出文件。默认跳过 |
| `-t SPEC` / `--set KEY=VALUE` / `-w RULE` | 写入伪造元数据、修改单个字段、替换词语，详见下文 |
| `--drop-alpha` | alpha 通道完全不透明时移除该通道，输出 RGB 图像 |
| `--scrub-all` | 清零所有通道的最低位，用于处理未知格式的隐写。每个颜色分量最多变化 1 |
| `--strip-icc` | 同时移除 ICC 色彩配置文件。默认保留，因为它不含生成信息 |
| `-r`, `--recursive` | 递归处理子目录 |
| `-n`, `--dry-run` | 试运行：报告将执行的操作，不写入文件 |
| `-y`, `--yes` | 处理目录或通配符时，不请求确认 |
| `--no-verify` | 跳过写入后的回读验证 |

`-o`、`-d`、`-i` 只能三选一。三者都不指定时，输出到源文件所在目录。

输入包含目录或通配符时，处理前会显示文件数量和输出位置，并请求确认。逐个指定的文件不请求确认。

```
$ nais ./images
./images：共 12 个文件（png 10、jpg 2），输出到源文件所在目录，文件名追加 _clean
是否继续？[y/N] y
✔ a.png → a_clean.png  已移除文本块 ×6（Title、Description、…）、LSB 隐写（alpha+gzip 4726 B）；alpha 已恢复为 255；1.64 MiB → 1.65 MiB
✔ b.jpg → b_clean.jpg  已移除 APP1/EXIF-XMP 段（6.06 KiB）；812.40 KiB → 806.34 KiB
· c.png  已跳过：输出文件 c_clean.png 已存在（使用 --overwrite 覆盖）
…
完成：共 12 个文件，成功 11 个，跳过 1 个，失败 0 个
```

每个文件写入后都会重新读取并验证：移除模式下，输出文件中不能残留任何元数据；写入模式下，两层都必须读出写入的内容。验证未通过的文件标记为 ✗，并列出原因。

各格式的处理方式：

| 格式 | 处理方式 |
|---|---|
| PNG | 移除全部文本块（tEXt、iTXt、zTXt）、eXIf 与 tIME；清除隐写数据占用的最低位，然后无损重新编码。隐写区域内被改为 254 的 alpha 恢复为 255，区域外的像素保持原值，真实的透明度不受影响。文件大小可能因压缩参数不同而变化 |
| JPEG | 按段处理：移除 APP1（EXIF、XMP）、APP13（Photoshop、IPTC）、COM 等段；保留 APP0（JFIF）、APP14（Adobe 色彩变换标记，移除会导致偏色）和 APP2（ICC，可选）。扫描数据不变，不重新编码 |
| WebP | NovelAI 网站提供的 WebP 为无损 VP8L 格式，按 PNG 的方式处理后无损重新编码。有损且 alpha 完全不透明的文件，在 RIFF 容器层移除 ALPH、EXIF、XMP 块，VP8 数据不变。有损且含透明像素的文件只能有损重新编码，处理时会提示。动图只移除容器层元数据 |
| 其他 | 由 Pillow 重新编码，画质会有损失，处理时会提示 |

### 按日期重命名（-N）

NovelAI 默认的下载文件名由提示词开头和 ` s-<种子>` 组成。移除元数据后，文件名中仍保留这些信息。
使用 `-N` 后，输出文件改为“日期-序号”格式的文件名。

```bash
nais ./images -N               # 输出 20260929-0001.png、20260929-0002.png…，源文件保留
nais ./images -N -d ./clean    # 在输出目录中编号；使用 -r 时，每个子目录分别从 0001 开始
nais ./images -N -i            # 重命名源文件：写入新文件名后删除原文件
```

- 日期为处理当天的本地日期。目标目录中已有当天的编号时，从最大编号的下一个开始，不区分扩展名：已有 `0003.webp` 时，下一个 PNG 文件为 `0004.png`。其他日期的编号不影响排序。
- 与 `-i` 同时使用时，文件名已是“日期-序号”格式的文件保留原名；不含元数据的文件只重命名。
- 试运行时，按实际运行的顺序报告每个文件的新文件名。
- `-N` 不能与 `-o` 同时使用。
- 序号按处理顺序分配，与原文件名没有对应关系。需要保留对应关系时，先用 `-n` 试运行并记录输出。

未使用 `-N`，而输入的文件名为 NovelAI 默认格式时，`nais` 会在处理结束后输出一条警告。

### 写入伪造元数据（-t）

先移除全部元数据，再写入指定内容。明文层和隐写层都会写入。

```bash
nais a.png -t 1                  # 内置预设 1：所有字段写入 512 个半角空格
nais a.png -t 2                  # 内置预设 2：所有字段写入“杂鱼~♥”，共 64 个，以逗号分隔
nais a.png -t 'TEXT'             # 所有字段写入 TEXT
nais a.png -t 3                  # 使用用户预设 3
nais -t edit 3                   # 在终端中编辑，并保存为预设 3；不指定图片时只编辑预设（也可写作 edit:3）
nais a.png -t edit               # 以该图片的元数据为基础编辑，完成后可选择保存为预设
nais a.png -t @template.json     # 使用 JSON 模板
nais -t list                     # 列出全部预设
```

内置预设：

| 编号 | 名称 | 写入内容 |
|---|---|---|
| 1 | 空格 | 512 个半角空格。元数据查看工具显示为空，导入 NovelAI 时提示词为空 |
| 2 | 杂鱼 | `杂鱼~♥, 杂鱼~♥, …`，共 64 个 |

“所有字段”指六个明文字段（Title、Description、Software、Source、Generation time、Comment）和隐写层中的对应字段。

用户预设保存在 `~/.config/nai-meta/presets/<编号或名称>.json`，建议从 3 开始编号。与内置预设同编号的用户预设会覆盖内置预设，`-t list` 会标出覆盖关系；删除该文件后，恢复使用内置预设。
`-t` 的参数与已有预设的名称相同时，使用该预设；否则将参数作为写入文本。

写入时，宽和高按每张图像的实际尺寸设置；预设中 seed 为 `null` 时，为每个文件生成随机值。
JPEG 和有损 WebP 没有可以写入隐写数据的 alpha 通道，只写入 EXIF。RGB 格式的 PNG 会先添加一个完全不透明的 alpha 通道，再写入隐写数据。

`-t edit` 在终端中列出六个明文字段和 Comment 中的常用字段：

```
━━ 编辑伪造元数据（预设 3）
  1  Title            NovelAI generated image
  2  Description      1girl, solo, …
  3  Software         NovelAI
  4  Source           NovelAI Diffusion V5 0ADF9AB7
  5  Generation time  6.6105579499853775
  6  prompt           1girl, solo, …
  7  uc               nsfw, lowres, …
  8  seed             1699232568
 ...
edit ›
```

| 输入 | 作用 |
|---|---|
| 编号 | 修改对应字段，输入框中预填当前值。prompt、uc 和 Description 支持多行输入：Enter 换行，按 Esc 后再按 Enter 提交，Ctrl-C 放弃修改 |
| `KEY=VALUE` | 设置任意字段，包括列表中未显示的 Comment 字段。值按 JSON 解析；`seed=null` 表示每个文件随机生成 |
| `:all TEXT` | 将所有字段设为同一文本 |
| `:json` | 在 `$VISUAL` 或 `$EDITOR` 指定的编辑器中编辑完整 JSON。未设置时，使用 nano；Windows 使用记事本 |
| `:w`、`:q`、回车 | 保存；取消；重新显示列表 |

`--set KEY=VALUE` 用于修改单个字段，可以重复使用，值按 JSON 解析。单独使用时，以源文件的元数据为基础，只修改指定字段；与 `-t` 同时使用时，在 `-t` 的内容上修改。

```bash
nais a.png --set seed=7 --set uc=lowres    # 输出 a_poison.png，只修改 seed 和负面提示词
```

### 替换词语（-w）

部分平台会根据元数据中的特定词语处理图片。`-w` 不移除元数据，只替换命中的词语，其余内容保持不变。

```bash
nais a.png -w discord              # 使用词表 discord（内置规则：loli→1011），输出 a_discord.png
nais a.png -w loli=1011            # 使用单条规则，输出 a_w.png
nais a.png -w discord -w foo=bar   # 多个参数的规则依次合并
nais -w edit discord               # 在终端中编辑词表
nais -w list                       # 列出全部词表
```

- 读取源文件的元数据，替换其中所有字符串（正向提示词、负面提示词、角色提示词、Description 等），再写回明文层和隐写层。
- 输出文件名中的词语也会替换，因为 NovelAI 默认的文件名包含提示词。
- 匹配方式为子串匹配，不区分大小写。需要精确匹配时，使用正则表达式：`-w '/\bloli\b/=1011'`。
- 词表保存在 `~/.config/nai-meta/words/<名称>.txt`，每行一条 `OLD=NEW` 规则，以 `#` 开头的行为注释。
- 不含 NovelAI 元数据的文件和未匹配任何规则的文件都会跳过，不生成输出文件。
- `-w` 与 `-t` 不能同时使用。

### 交互模式（nais tui）

```bash
nais tui              # 也可以写作 nai s tui 或 nai-strip tui
nais tui ./clean      # 启动时将输出目录设为 ./clean
```

将图片或目录从 Finder 或文件资源管理器拖入终端，按回车后，按当前设置处理并显示结果。一次可以拖入多个文件；目录会先显示文件数量，并请求确认。
底部状态栏显示当前设置，以 `/` 开头的命令用于修改设置：

| 命令 | 作用 |
|---|---|
| `/out DIR` | 输出到指定目录，可以拖入目录作为参数；`/out -` 恢复为输出到源文件所在目录 |
| `/suffix SUFFIX` | 设置输出文件名后缀 |
| `/n` | 切换按“日期-序号”重命名 |
| `/i` | 切换直接修改源文件（不保留原文件） |
| `/ow` | 切换覆盖已存在的输出文件 |
| `/t SPEC`、`/t -` | 设置或关闭伪造元数据写入，`SPEC` 的取值与 `-t` 相同 |
| `/w RULE`、`/w -` | 设置或关闭词语替换，`RULE` 的取值与 `-w` 相同 |
| `/alpha`、`/icc`、`/scrub` | 切换移除 alpha 通道；切换移除 ICC 色彩配置文件；切换清零所有通道的最低位 |
| `/r` | 切换递归处理子目录 |
| `/dry` | 切换试运行 |
| `/help`、`/q` | 显示帮助；退出（也可以按 Ctrl-D） |

- 支持用 Tab 补全路径，用 ↑ ↓ 查看历史输入。
- `/t` 与 `/w` 互斥，开启其中一个时，另一个自动关闭。
- 退出时保存以下设置：输出目录、后缀、重命名、alpha、ICC、递归、覆盖。
- 以下设置不保存，每次启动时均为关闭状态：直接修改源文件、试运行、伪造元数据写入、词语替换。

### 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 全部成功。跳过的文件不计为失败 |
| 1 | 至少有一个文件处理失败或验证未通过；未找到输入文件；参数取值无效；用户取消 |
| 2 | 命令行参数错误 |

## 配置文件

配置目录为 `~/.config/nai-meta`。设置了 `XDG_CONFIG_HOME` 时，为 `$XDG_CONFIG_HOME/nai-meta`；Windows 上为 `%APPDATA%\nai-meta`。

| 文件 | 内容 |
|---|---|
| `tui.json` | 交互模式保存的设置 |
| `history` | 交互模式的输入历史 |
| `presets/<名称>.json` | 用户预设。内置预设 1、2 不在此目录中 |
| `words/<名称>.txt` | 用户词表 |
| `edit.json` | 执行 `:json` 时使用的临时文件 |

## 工作原理

NovelAI 生成图片时，会把同一份元数据写入两处：

1. **明文层**
   - PNG 写入文本块 Title、Description、Software、Source、Generation time 和 Comment。Comment 为 JSON，包含全部生成参数。
   - WebP 写入 EXIF：Software 为模型名与哈希，ImageDescription 为提示词，UserComment 为完整元数据的 JSON。
2. **LSB 隐写层**
   - 将 Description、Software、Source、Generation time 和 Comment 序列化为 JSON，经 gzip 压缩后，按列优先顺序写入 alpha 通道各像素的最低位。
   - 布局为 `[magic，15 字节][数据长度，32 位][数据][FEC 长度，32 位][FEC]`，magic 为 `stealth_pngcomp`。
   - FEC 长度为 `0xffffffff` 时表示没有纠错码，NovelAI 目前只写入这个标记。
   - 经官方 `nai_add_fec.py` 添加的纠错码也能识别，并一并清除。
   - 同时支持 A1111 插件使用的 `stealth_pnginfo`，以及 RGB 通道的 `stealth_rgbinfo` 和 `stealth_rgbcomp`。

格式定义见官方仓库 [NovelAI/novelai-image-metadata](https://github.com/NovelAI/novelai-image-metadata)。

## 限制与注意事项

- **签名**：NovelAI 会对元数据签名（`signed_hash`）。写入伪造元数据或替换词语后，签名必然失效，因此 nai-meta 会删除该字段，`naii` 显示为无签名。签名无法伪造。
- **词语替换的范围**：替换基于字符串匹配，例如规则 `loli` 同样会命中 `Lolita`。需要精确匹配时，使用正则表达式。
- **隐写层的保留条件**：图像经过格式转换、缩放或有损压缩后，隐写层会被破坏。QQ、微信转发通常会移除文本块，但保留隐写层。
- **NovelAI 的 WebP 格式特点**：
  - 提示词含中文时，NovelAI 会在 EXIF 的 Description 开头多写 4 个 NUL 字节，隐写层中没有。读取时会去掉这些字节，不计为两层差异。
  - NovelAI 的 WebP 边缘常有少量 alpha 小于 254 的像素，这些像素保持原值。
- **非 NovelAI 图片**：A1111、Forge 的 `parameters` 文本、ComfyUI 工作流和相机 JPEG 都可以读取；无法识别的内容按原样列出。
- **Windows 支持**：已在代码中处理以下差异，但尚未在 Windows 上实测。
  - cmd 和 PowerShell 不会为外部程序展开 `*.png`，由 nai-meta 自行展开通配符。
  - 输出重定向到文件时，统一使用 UTF-8 编码。
  - 传统控制台字体缺少 ✔ ▸ 等符号时，自动改用 √ > !。可用环境变量 `NAI_META_ASCII=1` 或 `0` 强制开启或关闭。

## 相关项目

| 项目 | 读取隐写层 | 清除隐写层 | 说明 |
|---|---|---|---|
| [NovelAI/novelai-image-metadata](https://github.com/NovelAI/novelai-image-metadata) | ✓ | ✗ | 官方实现，支持读取、写入和验签 |
| [receyuki/stable-diffusion-prompt-reader](https://github.com/receyuki/stable-diffusion-prompt-reader) | ✓ | ✗ | 其“清除”功能不处理 LSB 隐写 |
| [Takenoko3333/remove-meta-alpha](https://github.com/Takenoko3333/remove-meta-alpha) | ✗ | 删除整个 alpha 通道 | 2023 年后停止更新 |
| [zhulinyv/Semi-Auto-NovelAI-to-Pixiv](https://github.com/zhulinyv/Semi-Auto-NovelAI-to-Pixiv) | ✓ | 以新的隐写数据覆盖 | WebUI，AGPL 许可证 |
| [iris-out/naisu](https://github.com/iris-out/naisu) | ✓ | 清除 alpha 最低位 | Chrome 扩展，仅处理 NovelAI 网站上的下载 |
| [wiltodelta/remove-ai-watermarks](https://github.com/wiltodelta/remove-ai-watermarks) | ✗ | ✗ | 保留 alpha 通道 |

## 开发

```bash
git clone https://github.com/Miint-Sunny/nai-meta.git
cd nai-meta
uv run naii a.png      # 首次运行时自动创建 .venv
uv run pytest          # 运行测试
```

命令名定义在 `pyproject.toml` 的 `[project.scripts]` 中。修改后，执行 `uv tool install . --reinstall` 生效。

```
src/nai_meta/core.py         PNG 块扫描、隐写的读取/清除/写入、参数整理、词语替换、跨平台处理
src/nai_meta/nai_inspect.py  naii
src/nai_meta/nai_strip.py    nais：移除、伪造元数据写入、词语替换、预设与词表
src/nai_meta/edit.py         -t edit 与 -w edit 的终端编辑界面
src/nai_meta/tui.py          nais tui
src/nai_meta/cli.py          nai 统一入口；新增子命令时，在 COMMANDS 中添加一项
src/nai_meta/argparse_zh.py  argparse 的中文界面与按显示宽度折行
tests/                       测试：合成图往返、各格式、解析、伪造元数据与内置预设、词语替换、重命名、交互模式、命令行
```

已用 900 多张实际生成的图片测试，覆盖以下范围：

- NovelAI V4、V4.5、V5 的全部 14 个模型哈希。
- 文生图、图生图、局部重绘、增强和导演工具。
- Vibe Transfer 和角色参考。
- 文本块已被转发平台移除、只剩隐写层的图片。
- A1111、ComfyUI 生成的图片和相机拍摄的 JPEG。

测试结果如下：

- 全部图片均读取成功，没有出现错误。
- 移除元数据后，PNG 的 RGB 像素逐位相同，JPEG 的扫描数据逐字节相同。
- NovelAI 网站的 WebP 下载用两个样本验证了读取、比对、移除、伪造元数据写入和重命名：一个样本的提示词为纯英文，另一个含中文。

## 许可证

[MIT](LICENSE)
