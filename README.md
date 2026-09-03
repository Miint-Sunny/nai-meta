# nai-meta — NovelAI 图片元数据工具

读、剥、改 NovelAI 出图里的元数据，像素一个都不动。纯 Python，macOS / Windows / Linux 都能跑。

| 短名 | 子命令 | 完整名 | 干什么 |
|---|---|---|---|
| `naii` | `nai i` | `nai-inspect` | **读**：把生成参数排成一眼能看的版式，明文层和隐写层都读，两层都在时顺手比对 |
| `nais` | `nai s` | `nai-strip` | **剥**：去掉文本块 / EXIF / LSB 隐写；`-t` 剥完写假的（投毒）；`-w` 不剥只换词；`nais tui` 拖图进终端就处理 |
| `nai` | | | 伞形总入口：`nai i …` `nai s …`，不带参数打印用法，`nai -V` 看版本 |

三种叫法完全等价，挑顺手的用。下文统一写 `naii` / `nais`。

## 须知

- **像素不动。** 剥、投毒、改词都只碰元数据；PNG 无损重编码，JPEG 和有损 WebP 在容器层按段操作，图像数据一个字节不改。
  只有「有损 WebP 又带真透明」这一种情况必须重编码，工具会提示。
- **NAI 的元数据有两层**：明文层（PNG 文本块，WebP 是 EXIF）和 alpha 通道最低位里的 LSB 隐写。novelai.net/inspect
  读的是隐写层。只删文本块、或者只改文本块，都骗不过它。这里两层一起处理。
- **`nais` 有三种互斥模式**，看开关就知道是哪种：不带 `-t` / `-w` = 全剥；`-t` = 剥完写假的；`-w` = 不剥，只把命中的词换掉。
- **改过内容签名就作废。** NAI 会给元数据签名（`signed_hash`），投毒和改词之后必然对不上，工具直接去掉它，
  `naii` 会显示无签名。这个没法伪造。
- **默认不覆盖、不原地改。** 输出写在原图旁边加后缀（`_clean` / `_poison` / `_w` 或词表名），同名已存在就跳过，
  `--overwrite` 才覆盖，`-i` 才原地。文件夹和通配符是批量操作，先报数量再问一句 y/N，`-y` 跳过。
- **写完都会回读验证。** 剥：三层都得为空；投毒：两层都得读出写入的内容；改词：旧词一个不剩。不通过标 ✗ 并说明。
- **改词只是换字符串。** 匹配不分大小写、按子串，`Lolita` 也会变；精确匹配写正则。它应付的是平台按元数据里的词封图这种事，
  词本身只是生图用的形象提示词。
- **不是 NAI 的图也尽量认**：A1111 / Forge 的 `parameters` 文本、ComfyUI 工作流、相机 JPEG，能读的读，不能读的报一句。
- **Windows**：cmd / PowerShell 不替外部程序展开 `*.png`，工具自己展开；stdout 重定向到文件时强制 UTF-8；
  老式窗口字体缺 ✔ ▸ 这类符号时自动换成 √ > !（`NAI_META_ASCII=1` / `0` 强制）。带空格的路径照常加引号。
  实测在 macOS 上做的，Windows 没有真机验证。
- 隐写层能读的前提是图没被重编码过：转 JPEG、缩放、二压都会毁掉它。QQ / 微信转发会剥掉文本块但通常保留隐写。

## 安装

依赖 Pillow、numpy、prompt_toolkit，全是纯 Python，用 uv 管理，不用自己建 venv：

```bash
uv tool install git+https://github.com/Miint-Sunny/nai-meta      # 装成全局命令，uv 给它建独立环境
```

| 场景 | 命令 |
|---|---|
| 克隆后本地装 | 仓库里 `uv tool install .` |
| 更新 / 改了代码或命令名 | `uv tool install . --reinstall` 或重跑上面的 git 安装 |
| 不装一次性跑 | `uvx --from git+https://github.com/Miint-Sunny/nai-meta naii a.png` |
| 开发期 | 仓库里 `uv run naii a.png`（自动建 `.venv`）；测试 `uv run pytest` |
| 卸载 | `uv tool uninstall nai-meta` |

Windows：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # 装 uv
uv tool install git+https://github.com/Miint-Sunny/nai-meta
uv tool update-shell                     # 把 %USERPROFILE%\.local\bin 加进 PATH，重开终端
```

命令名在 `pyproject.toml` 的 `[project.scripts]` 里，左边就是命令名，改完 `uv tool install . --reinstall`。

## naii：读参数

```bash
naii a.png b.png             # 人读版式
naii -r ./图库               # 目录递归
naii -p a.png | pbcopy       # 只输出正向提示词（含角色），直接复制
naii -j a.png > a.json       # JSON：文本块、隐写、EXIF、整理后的 params 全在
naii -f a.png                # 附带 Comment 里的全部字段
naii --raw a.png             # 附带原始文本块 / 隐写 JSON 字符串
naii --stealth a.png         # 只用隐写层（怀疑明文层被改过时）
naii --text a.png            # 只用明文层（PNG 文本块 / WebP 的 EXIF）
```

| 选项 | 说明 |
|---|---|
| `-r` `--recursive` | 目录递归 |
| `--text` / `--stealth` | 只用明文层 / 只用隐写层（默认明文层优先，没有再看隐写） |
| `-f` `--full` | 把 Comment 里的全部字段也打出来 |
| `--raw` | 附带原始文本块 / 隐写 JSON 字符串 |
| `-j` `--json` | JSON 输出，单图一个对象，多图为数组 |
| `-p` `--prompt` | 只输出正向提示词（含角色），多图时用 `# ===== 文件名` 分隔 |

输出示例：

```
━━ a.png   PNG · RGBA · 2176×896
元数据    文本块 ✓ 6 · 隐写 ✓ alpha+gzip 12406 B · 两层一致 · 读自文本块

模型      NovelAI Diffusion V5 · 哈希 0ADF9AB7
类型      图生图 i2i · 强度 0.7 · 噪声 0.8
附加      Vibe Transfer ×2（强度 0.6, 0.35 · 信息提取 1, 0.8） · 角色参考 ×1（强度 1）
尺寸      2176×896   耗时 6.6 s
采样      Euler Ancestral (k_euler_ancestral) · karras · 28 steps
引导      Prompt Guidance 5.5 · Rescale 0.2
种子      1699232568
开关      Variety+ · 质量标签 · UC 预设 #2
签名      有（NAI 签名 7Pc89N+E8gjW…，未验证）

─── 正向 ──────────────────────────────────────────────────────
1girl, ...

─── 角色 1 @ (0.3, 0.5) ────────────────────────────────────────
girl, red hair, smile

─── 负面 ──────────────────────────────────────────────────────
nsfw, lowres, ...

─── 角色 1 负面 ────────────────────────────────────────────────
hat
```

- **元数据行**：哪几层存在、隐写多大、两层是否一致、这次显示的是哪一层。两层都是 NAI 数据但不一致时标 ⚠ 并列出差异字段
  （隐写层本来就不存 vibe 参考图这类大字段，两层各自签名，这些正常差异不算）。
- **类型**：文生图 / 图生图 i2i / 局部重绘 inpaint / 增强 Enhance / 导演工具（emotion、lineart 等，带 defry）；i2i 系带强度和噪声。
- **附加**：Vibe Transfer、角色参考、ControlNet，有几个列几个，带强度。
- **开关**：只列打开的：Variety+、Decrisper、SMEA、质量标签、UC 预设、透明背景、Upscale、角色坐标。
- **角色区块**按 NAI 的角色序号编号，负面区块的「角色 2」就是正向的「角色 2」；开了角色坐标时标出位置。
- 尺寸与文件实际尺寸不一致（放大过）时两个都显示。模型名从 Comment 取，缺了就从 Source 拆，再不行按哈希表兜底。
- 不是 NAI 的图：A1111 / Forge 的 `parameters` 文本（PNG 文本块或 EXIF UserComment 里的）解析成同样版式；
  ComfyUI 工作流报节点数；其余文本块和 EXIF 原样列出，长的只报长度，`-f` 看全文。

## nais：剥元数据

```bash
nais a.png                    # → a_clean.png（写在原图旁边）
nais a.png -o 干净.png         # 指定输出文件
nais -r ./图库 -d ./干净       # 整个目录，输出目录里保持相对层级
nais -i *.png                 # 原地覆盖，不留备份
nais -n ./图库                 # dry-run：只报告会去掉什么
```

| 选项 | 说明 |
|---|---|
| `-r` `--recursive` | 目录递归 |
| `-o FILE` / `-d DIR` / `-i` | 输出文件（单输入）/ 输出目录 / 原地覆盖，三选一 |
| `--suffix X` | 写在原图旁边时的后缀（默认 `_clean`，投毒 `_poison`，改词 `_w` 或词表名） |
| `--drop-alpha` | alpha 全不透明时去掉 alpha 通道存成 RGB，文件更小 |
| `--scrub-all` | 所有通道所有像素的最低位清零，应付未知隐写变种（颜色最多变 1/255） |
| `--strip-icc` | 连 ICC 色彩配置也去掉（默认保留，它不含生成信息） |
| `--overwrite` | 输出已存在时覆盖（默认跳过） |
| `--no-verify` | 写完不回读验证 |
| `-n` `--dry-run` | 只报告，不写 |
| `-y` `--yes` | 处理文件夹 / 通配符时不问 y/N |
| `-t` `--set` `-w` | 投毒 / 改字段 / 改词，见下面三节 |

批量确认长这样：

```
$ nais ./图库
./图库：12 张（png 10 · jpg 2）→ 原图旁边 +_clean
继续？[y/N]（-y 可跳过确认）
```

各格式具体做了什么：

- **PNG**：解出像素 → 只擦隐写占用的那些最低位（头 + 数据 + FEC 段覆盖到的像素）→ 不带任何文本块重新编码。
  NAI 出图 alpha 本来全 255，被隐写改成 254 的归回 255；带真透明的图透明保留。文件大小会变（压缩等级不同），
  pHYs 之类无关紧要的块也一并没了。
- **JPEG**：按段过滤，丢 APP1（EXIF/XMP）、APP13（Photoshop/IPTC）、COM 等，保留 APP0（JFIF）、APP14（Adobe 色彩变换标记，
  去了会偏色）、可选 APP2（ICC）。扫描数据一个字节不动。
- **WebP**：NAI 网站的 WebP 下载 = 无损 VP8L + alpha 隐写 + EXIF。无损的走像素路线无损重存；有损且 alpha 全不透明的在
  RIFF 容器层直接丢掉 ALPH / EXIF / XMP 块，RGB 数据不动；有损又带真透明的只能有损重编码，会提示。动图只去容器层元数据。
- **其他格式**：Pillow 重编码，有损，会提示。

跟别的工具的区别：现有能抹 LSB 的开源工具都是把整个 alpha 通道砍掉。这里只清隐写占用的位，归回去和生成时一模一样。

### `-t`：投毒

剥干净之后再写一套假的进去，明文层和隐写层都写。

```bash
nais a.png -t '杂鱼'            # 每个分块都塞这段：六个文本块 + 隐写全是「杂鱼」 → a_poison.png
nais a.png -t 1                 # 用预设 1（整套字段：seed、模型、提示词都按预设）
nais -t edit 1                  # 终端里逐字段改，存为预设 1；不带图片就只是建预设（写成 edit:1 也行）
nais a.png -t edit              # 以这张图的元数据为底临时改一份，用完可选存为预设
nais a.png -t @模板.json         # 用现成 JSON
nais -t list                    # 列出预设
nais a.png -t '杂鱼' --set seed=7          # 填充之余改单个字段；动了 Comment 内部字段时 Comment 变成 JSON
```

`-t edit` 的界面在终端里，先列出每一块和 Comment 里的关键字段：

```
━━ 编辑投毒内容（预设 1）
  1  Title            NovelAI generated image
  2  Description      1girl, solo, …
  3  Software         NovelAI
  4  Source           NovelAI Diffusion V5 0ADF9AB7
  5  Generation time  6.6105579499853775
  6  prompt           1girl, solo, …
  7  uc               nsfw, lowres, …
  8  seed             1699232568
  9  steps            28
 10  scale            5.5
 ...
edit ›
```

| 输入 | 作用 |
|---|---|
| 编号 | 改那一项，当前值预填好直接改；prompt、uc、Description 是多行的，Enter 换行，Esc 再 Enter 提交，Ctrl-C 放弃 |
| `键=值` | 直接改任何字段，Comment 里没列出来的也行，值按 JSON 解析；`seed=null` 表示每张随机 |
| `:all 内容` | 一键把每块都塞成同一段 |
| `:json` | 用 `$VISUAL` / `$EDITOR` 改完整 JSON，改完回到这里（没设就 nano，Windows 是记事本） |
| `:w` / `:q` / 回车 | 保存 / 取消 / 重看列表 |

- 预设在 `~/.config/nai-meta/presets/<编号或名字>.json`。`-t 名字` 时有同名预设就用预设，没有就当填充内容。
- 预设写入时 width / height 按每张图实际尺寸，seed 为 null 则每张随机。
- JPEG 和有损 WebP 没有能装隐写的 alpha，只写 EXIF。RGB 的 PNG 会补一层全 255 的 alpha 来装隐写。

### `--set`：改单个字段

```bash
nais a.png --set seed=7 --set uc=lowres    # 以原图元数据为底，只改这几个字段，提示词原样 → a_poison.png
```

可重复；值按 JSON 解析，不合法就当字符串。和 `-t` 一起用时叠加在 `-t` 之上。

### `-w`：只改词

有些平台按元数据里的词封图。这时候不想全剥，只想把那几个词换掉，其余提示词、seed、模型一个不动：

```bash
nais a.png -w discord             # 用词表 discord（内置 loli→1011）→ a_discord.png
nais a.png -w loli=1011           # 单条规则 → a_w.png
nais a.png -w discord -w foo=bar  # 可叠加
nais -w edit discord              # 终端里改词表，存在 ~/.config/nai-meta/words/discord.txt
nais -w list                      # 列词表（没有文件的是内置）
```

- 读出原图两层元数据，递归替换里面所有字符串（正向、负面、角色、Description 全覆盖），写回明文层和隐写层。
- **输出文件名里的词也一并换**：NAI 直接下载的文件名就是提示词。
- 匹配不分大小写、按子串；精确匹配写正则：`-w '/\bloli\b/=1011'`。
- 没有 NAI 元数据或没命中任何词的图不动。和 `-t` 互斥。

`-w edit` 的界面：`旧=新` 添加或改一条，`-旧` 或 `-编号` 删一条，`:w` 保存，`:q` 取消，回车重看。
词表文件一行一条 `旧=新`，`#` 开头是注释，可以直接手改。

### `nais tui`：拖图进来就处理

```bash
nais tui              # 或 nai s tui / nai-strip tui；nais tui ./干净 = 进去顺便把输出目录设好
```

把图片或文件夹从 Finder / 资源管理器拖进终端窗口，回车就按当前设置处理并打印结果；文件夹先报数量再问 y/N，
一次拖多个也行。底部状态栏一直显示当前设置，斜杠命令切换：

| 命令 | 作用 |
|---|---|
| `/out <目录>` | 输出到指定目录（可以把文件夹拖进来当参数）；`/out -` 恢复写在原图旁边 |
| `/suffix <后缀>` | 旁边模式的文件名后缀 |
| `/i` | 切换原地覆盖（不留备份，会警告） |
| `/alpha` `/icc` `/scrub` | 切换去 alpha / 去 ICC / 全通道 LSB 清零 |
| `/r` | 切换文件夹递归 |
| `/dry` `/ow` | 切换 dry-run / 覆盖同名输出 |
| `/t <内容>` `/t 1` `/t edit 1` `/t @文件` `/t list` `/t -` | 投毒：填充 / 预设 / 编辑预设 / 模板 / 列预设 / 关 |
| `/w discord` `/w loli=1011` `/w edit discord` `/w list` `/w -` | 只改词：词表 / 单条 / 改词表 / 列词表 / 关 |
| `/help` `/q` | 说明 / 退出（Ctrl-D 也行） |

Tab 补全路径，↑↓ 翻历史。`/t` 和 `/w` 互斥，设一个另一个自动关。退出时记住输出目录、后缀、alpha / ICC / 递归这些设置；
原地覆盖、dry-run、投毒、改词故意不记，每次进来都从安全状态开始。

## 配置文件

| 内容 | 位置（Windows 把 `~/.config` 换成 `%APPDATA%`） |
|---|---|
| TUI 设置 | `~/.config/nai-meta/tui.json` |
| TUI 历史 | `~/.config/nai-meta/history` |
| 投毒预设 | `~/.config/nai-meta/presets/<名>.json` |
| 改词表 | `~/.config/nai-meta/words/<名>.txt` |
| `:json` 编辑临时文件 | `~/.config/nai-meta/edit.json` |

## 原理

NAI 出图时把同一份元数据写了两遍：

1. **明文层**：PNG 是文本块（tEXt）Title / Description / Software / Source / Generation time / Comment，
   WebP 是 EXIF（Software = 模型名+哈希，ImageDescription = 提示词，UserComment = 整份 JSON）。
   Comment 装着全部生成参数。exiftool 能看到，也最容易被转发剥掉。
2. **LSB 隐写**（stealth pnginfo）：把 Description、Software、Source、Generation time、Comment 这份 JSON gzip 后，
   按列优先写进 alpha 通道每个像素的最低位。布局 `[magic 15 字节][32 位大端长度][数据][32 位 FEC 长度][FEC]`，
   magic 是 `stealth_pngcomp`，FEC 长度 `0xffffffff` 表示没有（NAI 目前只写这个标记；用官方 `nai_add_fec.py`
   加过纠错码的图也认，一并擦）。A1111 插件的 `stealth_pnginfo` 和 RGB 通道的 `stealth_rgbinfo/rgbcomp` 也认。

格式对照官方仓库 [NovelAI/novelai-image-metadata](https://github.com/NovelAI/novelai-image-metadata)；
读取思路来自 `nai5-prompting/反推/stealth_decode.py`，这里补上了擦除和写入。

## 同类项目

| 项目 | 读隐写 | 抹隐写 | 备注 |
|---|---|---|---|
| [NovelAI/novelai-image-metadata](https://github.com/NovelAI/novelai-image-metadata) | ✓ | ✗ | 官方，读 / 写 / 验签 |
| [receyuki/stable-diffusion-prompt-reader](https://github.com/receyuki/stable-diffusion-prompt-reader) | ✓ | ✗ | 1.3k★，「清除」实测不碰 LSB |
| [Takenoko3333/remove-meta-alpha](https://github.com/Takenoko3333/remove-meta-alpha) | ✗ | 删整个 alpha | 2023 年后停更 |
| [zhulinyv/Semi-Auto-NovelAI-to-Pixiv](https://github.com/zhulinyv/Semi-Auto-NovelAI-to-Pixiv) | ✓ | 用新隐写覆盖 | WebUI，AGPL |
| [iris-out/naisu](https://github.com/iris-out/naisu) | ✓ | 清 alpha LSB | Chrome 扩展，只管 NAI 站上的下载 |
| [wiltodelta/remove-ai-watermarks](https://github.com/wiltodelta/remove-ai-watermarks) | ✗ | ✗ | 5.4k★，明确保留 alpha |

## 结构

```
pyproject.toml              依赖 + 命令名
src/nai_meta/core.py        共用：PNG 块扫描、隐写读 / 擦 / 写、参数整理、改词、跨平台杂项
src/nai_meta/nai_inspect.py naii
src/nai_meta/nai_strip.py   nais：剥 / 投毒 / 改词、预设与词表
src/nai_meta/edit.py        -t edit 与 -w edit 的终端编辑界面
src/nai_meta/tui.py         nais tui
src/nai_meta/cli.py         nai 伞形总入口（COMMANDS 表加一行就是新子命令）
tests/                      合成图往返、格式、解析、投毒、改词、TUI、命令行
```

## 实测覆盖

本机 900 多张真图跑过：NovelAI V4 / V4.5 / V5 全部 14 个模型哈希、文生图 / i2i / inpaint / Enhance / 导演工具、
vibe transfer、角色参考、被转发剥掉文本块只剩隐写的图、A1111 / ComfyUI / 相机 JPEG，零崩溃；剥离后 PNG 像素逐位相同、
JPEG 扫描数据逐字节相同。NAI 网站的 WebP 下载用真样本验证过读取、比对、剥离与投毒。Windows 没有真机测试。

## 许可

MIT。
