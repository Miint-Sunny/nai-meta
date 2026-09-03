# nai-meta — NovelAI 图片元数据小工具

两个功能，像素一个都不动，每个功能三种叫法随便用：

| 短 | 子命令 | 完整 | 干什么 |
|---|---|---|---|
| `naii` | `nai i` | `nai-inspect` | 读出生成参数：明文层（PNG 文本块 / WebP 的 EXIF）+ LSB 隐写，两层都在时顺手比对 |
| `nais` | `nai s` | `nai-strip` | 剥掉元数据：PNG 文本块 / eXIf / tIME / LSB 隐写；JPEG 按段无损剥 EXIF、XMP、注释；`-t` 剥完写假的（投毒）；`-w` 不剥只改词 |

`nai` 是伞形总入口，不带参数打印用法，`nai -V` 看版本。以后的生图 agent 之类也挂在它下面
（`src/nai_meta/cli.py` 的 `COMMANDS` 加一行即可，`gen` / `agent` 这两个名字先留着）。
命令名都在 `pyproject.toml` 的 `[project.scripts]` 里，左边就是命令名，改完 `uv tool install . --reinstall`。

## 安装

依赖 Pillow + numpy + prompt_toolkit（TUI 用），全是纯 Python，用 uv 管理，**不用自己建 venv**：

```bash
uv tool install git+https://github.com/Miint-Sunny/nai-meta   # 装成全局命令，uv 给它建独立隔离环境
# 或者克隆后在仓库里：uv tool install .
```

改了代码或命令名之后：

```bash
uv tool install . --reinstall
```

开发期不装也能跑：`uv run naii a.png`（uv 自动建 `.venv` 并同步依赖）。
卸载：`uv tool uninstall nai-meta`。

### Windows

跟 macOS 一样靠 uv，依赖全是纯 Python，没有平台相关的东西：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # 装 uv
uv tool install C:\path\to\py-nai      # 或 uv tool install git+https://github.com/<你>/py-nai
uv tool update-shell                     # 把 %USERPROFILE%\.local\bin 加进 PATH，重开终端
```

Windows 上的几个差异都在工具里处理掉了：

- **通配符**：cmd / PowerShell 不替外部程序展开 `*.png`，工具自己展开，`**` 也认。
- **输出编码**：stdout 重定向到文件时默认 GBK，✔ ▸ 会报错；工具启动时把 stdout 强制成 UTF-8。
- **符号**：老式 cmd / PowerShell 窗口字体常缺 ✔ ✗ ▸ ⚠，非 Windows Terminal 时自动换成 √ × > !。
  `NAI_META_ASCII=1` 或 `=0` 可强制。
- 带空格的路径照常加引号。

不想装也能一次性跑：`uvx --from git+https://github.com/Miint-Sunny/nai-meta naii a.png`。

## naii / nai i / nai-inspect

```bash
naii a.png b.png            # 人读格式
naii -r ./图库              # 目录递归
naii -p a.png | pbcopy      # 只要正向提示词（含角色），直接复制
naii -j a.png > a.json      # JSON：文本块、隐写、EXIF、整理后的 params 全在
naii -f a.png               # 附带 Comment 里的全部字段
naii --stealth a.png        # 只看隐写层（怀疑文本块被改过时）
```

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

- **类型**：文生图 / 图生图 i2i / 局部重绘 inpaint / 增强 Enhance / 导演工具（emotion、lineart 等，带 defry），
  i2i 系带强度和噪声。
- **附加**：Vibe Transfer、角色参考、ControlNet，有几个列几个，带强度。
- **开关**：只列打开的：Variety+、Decrisper、SMEA、质量标签、UC 预设、透明背景、Upscale、角色坐标。
- **角色区块**按 NAI 的角色序号编号，负面区块的「角色 2」就是正向的「角色 2」；开了角色坐标时标出位置。
- 尺寸与文件实际尺寸不一致时（放大过）两个都显示。`-f` 把 Comment 里的全部字段也打出来。

文本块和隐写都是 NAI 数据但内容不一致时会标 ⚠ 并列出差异字段（隐写层本来就不存 vibe 参考图这类大字段，
两层各自签名，这些正常差异不算）。

不是 NAI 的图也尽量认：A1111 / Forge 那种 `parameters` 文本（PNG 文本块或 EXIF UserComment 里的）解析成同样的版式，
ComfyUI 工作流报节点数，`-f` 看全文。

## nais / nai s / nai-strip

```bash
nais a.png                    # → a_clean.png（写在原图旁边）
nais a.png -o 干净.png         # 指定输出文件
nais -r ./图库 -d ./干净       # 整个目录，输出目录里保持相对层级
nais -i *.png                 # 原地覆盖，不留备份
nais -n ./图库                 # dry-run：只报告会去掉什么
```

| 选项 | 说明 |
|---|---|
| `--suffix _clean` | 默认模式下加的后缀 |
| `--drop-alpha` | alpha 全不透明时去掉 alpha 通道存成 RGB，文件更小 |
| `--scrub-all` | 把所有通道所有像素的最低位清零，应付未知隐写变种（颜色最多变 1/255） |
| `--strip-icc` | 连 ICC 色彩配置也去掉（默认保留，它不含生成信息） |
| `--overwrite` | 输出已存在时覆盖（默认跳过） |
| `--no-verify` | 写完不回读验证 |
| `-y` | 处理文件夹 / 通配符时不问 y/N（脚本里用） |

**文件夹和通配符是批量操作，会先报数量再问一句 y/N**，逐个点名的文件不问：

```
$ nais ./图库
./图库：12 张（png 10 · jpg 2）→ 原图旁边 +_clean
继续？[y/N]（-y 可跳过确认）
```

每张图写完默认会**回读验证**：文本块、eXIf、EXIF、隐写都得为空才算 ✔，否则标 ✗ 并列出残留。

### 投毒：`-t`

剥干净之后再写一套假的进去。文本块（WebP、JPEG 是 EXIF）和隐写两层都写，novelai.net/inspect 读的是隐写层，
只改文本块骗不过它。

```bash
nais a.png -t '杂鱼'            # 每个分块都塞这段：六个文本块 + 隐写全是「杂鱼」 → a_poison.png
nais a.png -t 1                 # 用预设 1（整套字段，seed / 模型 / 提示词都按预设）
nais -t edit 1                  # 终端里逐字段改，存为预设 1；不带图片就只是建预设（edit:1 也行）
nais a.png -t edit              # 以这张图的元数据为底临时改一份，用完可选存为预设
nais a.png -t @模板.json         # 用现成 JSON
nais -t list                    # 列出预设
nais a.png -t '杂鱼' --set seed=7          # 填充之余改单个字段；动了 Comment 内部字段时 Comment 变成 JSON
nais a.png --set seed=7 --set uc=lowres    # 只 --set：以原图元数据为底改字段，提示词原样
```

`-t edit` 的界面就在终端里，先列出每一块和 Comment 里的关键字段：

```
  1  Title            NovelAI generated image
  2  Description      1girl, solo, …
  3  Software         NovelAI
  4  Source           NovelAI Diffusion V5 0ADF9AB7
  5  Generation time  6.6105579499853775
  6  prompt           1girl, solo, …
  7  uc               nsfw, lowres, …
  8  seed             1699232568
  9  steps            28
 ...
edit ›
```

输入编号改那一项（当前值预填好，直接改；prompt 这种多行的 Esc 再 Enter 提交），`键=值` 直接改任何字段
（`seed=null` 表示每张随机），`:all 内容` 一键全塞同一段，`:json` 才跳到 `$EDITOR` 改完整 JSON，`:w` 保存，`:q` 取消。

- 预设在 `~/.config/nai-meta/presets/<编号或名字>.json`（Windows `%APPDATA%\\nai-meta\\presets\\`）。
  `-t 名字` 时有同名预设就用预设，没有就当填充内容。
- 预设写入时 width / height 按每张图实际尺寸，seed 为 null 则每张随机。改过内容签名必然失效，
  `signed_hash` 一律去掉，`naii` 会显示无签名。
- JPEG 和有损 WebP 没有可写隐写的 alpha，只写 EXIF。RGB 的 PNG 会补一层全 255 的 alpha 来装隐写。
- 写完回读验证：明文层和隐写层都得读出写入的内容才算 ✔。TUI 里对应 `/t`。

### 只改词：`-w`

有些平台按元数据里的词封图（比如 Discord 对 `loli` 这种生图形象词）。这时候不想全剥，只想把那几个词换掉，
其余提示词、seed、模型一个不动：

```bash
nais a.png -w discord             # 用词表 discord（内置 loli→1011）→ a_discord.png
nais a.png -w loli=1011           # 单条规则 → a_w.png
nais a.png -w discord -w foo=bar  # 可叠加
nais -w edit discord              # 终端里改词表（旧=新 加，-旧 删，:w 存）；存在 ~/.config/nai-meta/words/
nais -w list                      # 列词表
```

- 读出原图两层元数据，递归替换里面所有字符串（正向、负面、角色、Description 全覆盖），写回文本块 / EXIF 和隐写。
- **文件名里的词也一并换**：NAI 直接下载的文件名就是提示词。
- 匹配不分大小写、按子串（`Lolita` 也会变 `1011ta`，要精确就写正则：`/\bloli\b/=1011`）。
- 改过内容签名作废，`signed_hash` 去掉。写完回读，旧词一个不剩才算 ✔。
- 没有 NAI 元数据或没命中任何词的图不动。和 `-t` 互斥：`-w` 是只改词，`-t` 是全换假的。TUI 里对应 `/w`。

### 交互模式：`nais tui`

```bash
nais tui              # 或 nai s tui / nai-strip tui；nais tui ./干净 = 进去顺便把输出目录设好
```

进去之后把图片或文件夹**从 Finder / 资源管理器拖进终端窗口**（终端会把路径贴进输入行），
回车就按当前设置处理并打印结果；文件夹先报数量再问 y/N，一次拖多个也行。
底部状态栏一直显示当前设置，斜杠命令切换：

| 命令 | 作用 |
|---|---|
| `/out <目录>` | 输出到指定目录（可以把文件夹拖进来当参数）；`/out -` 恢复写在原图旁边 |
| `/suffix <后缀>` | 旁边模式的文件名后缀 |
| `/i` | 切换原地覆盖（不留备份，会警告） |
| `/alpha` `/icc` `/scrub` | 切换去 alpha / 去 ICC / 全通道 LSB 清零 |
| `/r` | 切换文件夹递归 |
| `/dry` `/ow` | 切换 dry-run / 覆盖同名输出 |
| `/t <内容>` `/t 1` `/t edit 1` `/t @文件` `/t -` | 投毒：填充 / 预设 / 编辑预设 / 模板 / 关；`/t list` 列预设 |
| `/w discord` `/w loli=1011` `/w edit discord` `/w -` | 只改词：词表 / 单条 / 改词表 / 关；`/w list` 列词表 |
| `/help` `/q` | 说明 / 退出（Ctrl-D 也行） |

Tab 补全路径，↑↓ 翻历史。退出时记住输出目录、后缀、alpha / ICC / 递归这些设置
（原地覆盖和 dry-run 故意不记，每次进来都从安全状态开始），存在 `~/.config/nai-meta/tui.json`，
Windows 在 `%APPDATA%\nai-meta\`。

### 具体做了什么

- **PNG**：解出像素 → 擦掉隐写占用的那些最低位（只动头 + 数据覆盖到的像素）→ 不带任何文本块重新编码。
  PNG 无损，重编码不掉画质。NAI 出图 alpha 本来全是 255，被隐写改成 254 的会归回 255。
  文件大小会变（Pillow 的压缩等级和 NAI 的不同），pHYs 之类无关紧要的块也一并没了。
- **JPEG**：按段过滤，丢 APP1（EXIF/XMP）、APP13（Photoshop/IPTC）、COM 等，保留 APP0（JFIF）、
  APP14（Adobe 色彩变换标记，去了会偏色）、可选 APP2（ICC）。扫描数据一个字节不动。
- **WebP**：NAI 网站的 WebP 下载 = 无损 VP8L + alpha 隐写 + 8 KB EXIF（Software 存模型名，UserComment 存整个
  Comment JSON），实测一张 832×1216 剥完 0.7 s，RGB 逐位相同，EXIF 与隐写全无。无损 WebP 走像素路线无损重存；
  有损且 alpha 全不透明的在 RIFF 容器层直接丢掉 ALPH / EXIF / XMP 块，RGB 数据一字节不动；有损又带真透明的
  只能有损重编码，会提示。动图只去容器层元数据。
- **其他格式**：走 Pillow 重编码，有损，会提示。

跟别的工具的区别：现有能抹 LSB 的开源工具都是把整个 alpha 通道砍掉转成 RGB。这里只清隐写占用的那些最低位，
NAI 出图 alpha 本来全 255，归回去就和生成时一模一样；带真透明的图透明也保留。

## 原理

NAI 出图时把同一份元数据写了两遍：

1. **明文层**：PNG 是文本块（tEXt）Title / Description / Software / Source / Generation time / Comment，
   WebP 是 EXIF（Software = 模型名+哈希，ImageDescription = 提示词，UserComment = Comment JSON）。
   Comment 装着全部生成参数。exiftool 能看到，也最容易被 QQ / 微信转发剥掉。
2. **LSB 隐写**（stealth pnginfo）：把 Description、Software、Source、Generation time、Comment 这份
   JSON gzip 后，按列优先写进 alpha 通道每个像素的最低位。novelai.net/inspect 读的就是它。
   布局 `[magic 15 字节][32 位大端长度][数据][32 位 FEC 长度][FEC]`，magic 是 `stealth_pngcomp`，
   FEC 长度 `0xffffffff` 表示没有（NAI 目前只写这个标记；用官方 `nai_add_fec.py` 加过纠错码的图也认，一并擦）。
   A1111 插件的 `stealth_pnginfo` 和 RGB 通道的 `stealth_rgbinfo/rgbcomp` 也认。

格式对照的是官方仓库 [NovelAI/novelai-image-metadata](https://github.com/NovelAI/novelai-image-metadata)；
读取思路来自 `nai5-prompting/反推/stealth_decode.py`，这里补上了擦除那一半。

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
pyproject.toml           依赖 + 命令名
src/nai_meta/core.py        共用：PNG 块扫描、隐写读/擦、参数整理、跨平台杂项
src/nai_meta/nai_inspect.py naii / nai-inspect
src/nai_meta/nai_strip.py   nais / nai-strip
src/nai_meta/cli.py         nai 伞形总入口（nai i / nai s，以后的 agent 也挂这）
src/nai_meta/tui.py         nais tui 交互模式
tests/test_roundtrip.py  合成带隐写 / 文本块 / EXIF 的图，读 → 剥 → 验证
tests/test_cli.py        总入口分发、通配符展开
tests/test_tui.py        拖拽路径解析、文件夹 y/N、设置持久化、命令行确认
```

跑测试：`uv run pytest`

## 实测覆盖

本机 900 多张真图跑过：NovelAI V4 / V4.5 / V5 全部 14 个模型哈希、文生图 / i2i / inpaint / Enhance / 导演工具、
vibe transfer、角色参考、被转发剥掉文本块只剩隐写的图、A1111 / ComfyUI / 相机 JPEG，零崩溃；剥离后 PNG 像素逐位相同、
JPEG 扫描数据逐字节相同。NAI 网站的 WebP 下载用一张真样本验证过读取、比对与剥离。Windows 没有真机测试。

## 许可

MIT。
