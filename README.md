# rec2video

**一段录屏 → 一条能发布的介绍片。**

给 agent 一个录屏文件，它把剩下的事做完：定位可用画面、写口播、配音、按旁白时长反剪、点击处推镜 + 音效、中文字幕、BGM 闪避、多尺寸封面。

```
你：这录屏帮我做个介绍视频，仓库 https://github.com/owner/repo
它：成片 mp4 + 字幕 srt + 16:9/4:3 封面 + 标题与带话题标签的简介
```

不需要剪辑软件，不需要会 ffmpeg。硬依赖只有 `ffmpeg` / `ffprobe`。

## 它替你做的六件事

| 环节 | 怎么做的 |
|---|---|
| 找素材 | 生成带**原片绝对时间**的联系表，一眼看出第几秒是有效画面 |
| 配音 | 交给 [latent-media](https://github.com/guohuiyuan/latent-media) 的 `latent.py tts` 做：社区音色（165 个），台词里能嵌 `[笑]` `[轻笑]` 这类气声。本仓库不内置 TTS |
| 对齐 | **先配音、再按每段旁白时长反剪画面**，拉伸系数恒定 1.0000，十段之后也不会漂 |
| 字幕 | 从配音时间轴逐段推出来，尾部留 0.3s；超长只断在标点后（`，。：；？！` 优先于 `、`）；`[笑]` 这类气声只进口播不进字幕 |
| 动效 | 点击处 `ease-out 推近 → 停留 → ease-in 拉回`，同帧打一记程序合成的点击音 |
| 混音 | BGM 来自 [go-music-dl](https://github.com/guohuiyuan/go-music-dl)，或你自己的曲子；侧链闪避（说话时自动让路）+ 限幅。字幕烧进画面另存 srt |
| 包装 | HTML 模板 + 无头浏览器出 16:9 / 4:3 封面，抓仓库图标、语言、License、star |

## 安装

```bash
git clone https://github.com/guohuiyuan/rec2video.git
cd rec2video
./install.sh                     # macOS / Linux / Git Bash
powershell -c ./install.ps1      # Windows
```

装到 `~/.workbuddy/skills/rec2video`（其它 agent 把目录复制进你自己的 skills 目录即可，`SKILL.md` 就是入口）。装完在新会话里说「用 rec2video 把这个录屏做成介绍视频」，agent 会自己读 `SKILL.md`。

## 三步上手

```bash
python scripts/rec2video.py doctor                       # 体检：ffmpeg / 配音 CLI / BGM / 封面
python scripts/rec2video.py new ~/Videos/demo.mp4 \
        --repo https://github.com/owner/repo             # 建工程 + 联系表
python scripts/rec2video.py build demo/project.json      # 出片
```

中间只差一件事：**文案**。写 `narration.txt`（**先把发布标题写进 `project.json` 的 `title`**，第一段就是标题的口语版；一行一段，8–12 行 / 60–80 秒）后 `script` 灌进去，agent 会自动做这一步。

```bash
python scripts/rec2video.py script demo/project.json narration.txt --title "好家伙！Github又出狠活神器！…"
                                                                 # ← 灌文案，顺手验首句是不是标题原句
python scripts/rec2video.py hook   demo/project.json            # 只验这一条（标题没落盘 / 项目名 / 数字 / 字面重合）
python scripts/rec2video.py build  demo/project.json            # 配音+对齐+推镜+字幕+BGM+渲染
python scripts/rec2video.py check  demo/project.json            # 交付前自检
python scripts/rec2video.py cover  demo/project.json            # 封面（playwright 截 HTML 模板）
```

产物：

```
demo/
├─ project.json       # 全部参数都在这一个文件里
├─ scan/sheet_*.jpg   # 带时间戳的联系表
├─ work/timeline.json # 每段旁白的起止时间
├─ work/subs.srt      # 外挂字幕（可直接投 B 站）
├─ out/<name>.mp4     # 成片 1080p
└─ cover/*.png        # 16:9 + 4:3 封面
```

## 录屏怎么准备

三条硬要求，踩了就得重录：**16:9 且宽度 ≥1920**、**录之前把窗口定死**、**只要干净画面**（背景/圆角/缩放留给后期，不然成了画中画里再套一层）。

OBS、Recordly、`Win+Alt+R`、`⇧⌘5` 都能用，参数与取舍见 [`references/capture.md`](references/capture.md)。

## 两种模式

- **≤150s 直出**：整段画面按旁白总长对齐，适合一次录好的 60–90 秒演示。
- **>150s 精剪**：`plan` 生成骨架 → 从联系表填取材点和推镜点 → `cut` 按旁白反剪。3 分钟的录屏挑 70 秒，比反复 NG 重录快。改完文案重新 `tts` 之后要再 `cut` 一遍，画面长度才跟得上新旁白。

精剪的真实案例（12 段 / 77.5s / 13 个推镜）在 [`examples/recordly-intro/`](examples/recordly-intro/)。

## 文案：借广告手法，先定标题，第一句就把标题念一遍

标题、封面大字、口播首句、简介首句四处说的是同一句话——这不啰嗦，是短视频的广告打法：一个卖点说一次等于没说，得在四个入口砸四遍。片子本身还是项目介绍（要人拉仓库、去用，不是下单），但开头这两三秒得按广告的做法来，语气也必须配得上——**重复是手法，浮夸是燃料**：只重复不浮夸就是祥林嫂，只浮夸不重复观众记不住你在吹什么。浮夸加在形容词和节奏上，数字和功能一个都不许动（「14 个源，狠活」可以，「全网通吃」不行）。

观众是被标题带进来的，前三秒只干一件事：**让他确认「我点对了」，并且立刻知道这是什么。** 所以顺序是——先定标题（第 7 节那种带「神器」「斩获 Xk Stars」的发布标题，不是自己概括的白描），**把标题原文写进 `project.json` 的 `title`**，再把标题原句整句抄进 `narration.txt` 第一行，最后往下写正文。手上没有标题原句就别动笔，否则第一段只能靠痛点铺垫，开头必软。

**第一句不是"复述"标题，是"照念"标题**——一字不改，只在句首加一个 `[笑]`。这是被用户当面纠过两次的地方：只要允许"翻译成口语"，每个环节都会顺手压几个词，压掉的偏偏是 GitHub、最硬的卖点和情绪落点。

```
标题  GitHub 又出狠活！一个快捷键就冒出一张便签，Markdown 双栏实时预览，斩获 5.6k Stars——花笺

✗ [笑] 一个快捷键，桌面冒出一张便签——花笺 Floral Notepaper，5.6k 星。   ← 重合 33%，判 ✗
✓ [笑] GitHub 又出狠活！一个快捷键就冒出一张便签，Markdown 双栏实时预览，斩获 5.6k Stars——花笺
```

`script` 和 `hook` 会拿标题机检首句：字面重合（LCS）≥ 85%、标题里每个数字都落、标题里写了仓库名就得念出来、首句 ≤ 10 秒。不过就判 ✗。详见 [`references/hook.md`](references/hook.md) 第七节。

三条允许的例外：`[笑]` 标记；TTS 一定念错的词（**改标题，不改首句**，例如 Tauri → Rust）；标题超过 40 字时砍掉最前面的套话句头（机检给 `!`，不算 ✗）。

既然口播就是标题，前三秒能砸出什么，由**标题的语序**决定：前 15 字放最狠的数字或反常识，中间放项目名 + 一句定性，情绪铺满全篇。写标题时就按"念出来听"排，别把数字留到最后。

第一段配的画面必须是最完整的全貌，不能落在加载中、弹窗、滚动半路；它现在要撑 8–10 秒（整条标题的长度），给它一段连续动作或整段缓慢推镜。情绪只放头和尾，中间老老实实讲功能。

七个可直接套的**标题**模板、情绪词能／不能清单、机器校验 + 十条自检 → [`references/hook.md`](references/hook.md)

### 四处对齐：标题 / 封面大字 / 口播首句 / 简介首句

同一个片子有四个地方在说「这是什么」。只有口播那一处是**字面**（逐字等于标题），其余三处对齐的是**口径**——核心数字（14 个音乐源 / 6.5k Stars）、核心名词（一个搜索框）、定性（开源 / Go 写的 / 本地跑）必须一致，句式各写各的。`project.json` 的 `title` 是唯一源头，另外三处从它派生。

| 位置 | 写法 | 字数 |
|---|---|---|
| 发布标题 | 狠活体 / 喜讯体完整版，得能一口念完 | 30–40 字 |
| 封面 `title_cn` | 标题的核心那半句，砍掉平台前缀 | 8–14 字 |
| 口播第一段 | **标题原句照念** + 句首 `[笑]` | = 标题，≤ 50 字 |
| 简介首句 | 同义复述，可换语序 | 15–25 字 |

改标题时的落点顺序：标题 → 封面 `title_cn` 重出 PNG → 口播第一段重配 → 简介首句。封面只能用 `cover.py render <cover/spec.json>` 重出；`rec2video.py cover` 会重抓仓库信息并覆盖 `spec.json`，手写的标题会被冲掉。

### 简介：三行 + 话题标签

简介交付时三行给全，只给首句等于没写完：

```
<首句：标题的同义复述，15–25 字>
<2–3 句干货：替你干了什么 + 一个可验证的细节>
#GitHub #开源 #Chrome扩展 #公众号排版 #自媒体工具 #Wechatsync
```

标签是必填项：**5–8 个，按 6 个写**。少了平台只能拿标题猜流量池，堆到十几个就是营销号特征。取词按三层各挑 1–3 个——运行环境（`#Chrome扩展`）、使用场景（`#公众号排版`）、身份锚点（`#GitHub` `#开源` `#独立开发`），再加上项目名。`#GitHub` 必带，和标题里的平台前缀同一个道理。自造的长句标签（`#一篇文章同步29个平台`）没人搜，别占名额；仓库里没有的能力也别写进标签，蹭错池子只会拉低完播。整行只用空格分隔，B 站/抖音/小红书/头条直接粘；**公众号正文里 `#` 不是话题**，投公众号删掉标签行、改在「话题」里加。细则见 `SKILL.md` 3.5。

## 字幕

字幕**不手打时间码**：一条字幕 = 一段旁白，时间是配音实测时长（`work/timeline.json`）→ 尾部 +0.3s。改文案就跑 `script` → `tts` → `cut` → `build`：配音按「文本+音色」指纹自动重做，切片按新时长重切（切片名带时长指纹，不会拿旧切片凑数），字幕跟着重算。

```bash
python scripts/rec2video.py subs demo/project.json     # 只重出字幕，改字号/文案后秒验
```

- 烧录版 `work/subs.ass`（每次渲染重新生成，不做缓存）+ 外挂版 `work/subs.srt`
- 折行只断在标点后，≤20 字不折；放不下就删字，不挤三行
- `subtitle.size=46` / `margin_v=46` 是 1080p 画框版式的实测值：画框下沿到画面底只剩 146px，两行字幕约 94px，再大就顶到画框
- 片头片尾标版是另一套（顶部 `Bar` 小字），`new --repo` 会自动填仓库地址与 star

细节： [`references/subtitles.md`](references/subtitles.md)

## 依赖与凭证

| 需要 | 必需 | 说明 |
|---|---|---|
| `ffmpeg` / `ffprobe` | ✅ | 进 PATH 即可，唯一的硬依赖 |
| [latent-media](https://github.com/guohuiyuan/latent-media) | ✅ | 配音靠它：本仓库只调它的 `latent.py tts`，不内置 TTS。装好后 `rec2video.py key lat_tk_xxx` 把 key 交给它保管 |
| [go-music-dl](https://github.com/guohuiyuan/go-music-dl) | – | 配乐靠它。有服务就 `MUSIC_BASE` 指过去；没有也不用管，`bgm` 会把**最新**官方 release 下到本地缓存（版本对不上 latest 就弃用，PATH 上的老版不吃）、起个只听 127.0.0.1 的临时实例，用完关掉。不想联网装就 `--no-local` 或 `MUSIC_DL_BIN` 指到已有可执行文件 |
| `playwright` | – | 出封面要：`pip install playwright`（复用本机 Edge），不装就只出 `spec.json` |

```bash
git clone https://github.com/guohuiyuan/latent-media ~/.workbuddy/skills/latent-media
python scripts/rec2video.py key lat_tk_xxx     # 把 key 交给 latent-media 保管
```

配乐可选，三条路随便挑一条：

```bash
# 1) 你已经起了服务：指过去就行
export MUSIC_BASE=http://127.0.0.1:8080/music

# 2) 什么都没起：让脚本自己把 music-dl 拉到本地起一个临时实例（第一次约 13 MB）
python scripts/rec2video.py bgm demo/project.json

# 3) 用自己的曲子
python scripts/rec2video.py bgm demo/project.json --file 曲子.mp3
```

第 2 条走的是官方 release（带 `checksums.txt` 校验），二进制缓存在用户目录：
Windows `%LOCALAPPDATA%\rec2video\music-dl`、macOS `~/Library/Caches/rec2video/music-dl`、
Linux `~/.cache/rec2video/music-dl`。服务只监听 `127.0.0.1` 的随机端口，BGM 取到就关。

**只认 latest**：以 release 的 `tag_name` 为准，机器上那个二进制版本号对不上就不用了（PATH 里塞着老的 TUI 版 v1.3.x 是最常见的情况——它的 `web` 页面挂在 `/`，没有 `/music/api/*`），自动下最新的。想钉死某一份：`MUSIC_DL_BIN`；想将就用手上那份：`--any`。

单独的二进制管理也有命令：

```bash
python scripts/musicdl.py which      # 现在用的是哪个可执行文件 + 版本对不对
python scripts/musicdl.py ensure     # 确保有，且是 latest（--force 重下，--any 不挑版本）
python scripts/musicdl.py serve      # 起服务，打印 base
python scripts/musicdl.py status     # 还在不在
python scripts/musicdl.py stop
```

## 目录

```
SKILL.md                  # agent 入口：只给录屏时的完整执行手册
scripts/rec2video.py      # 编排器（doctor/new/script/plan/cut/bgm/build/check/cover）
scripts/screencut.py      # 扫描联系表 / 反剪 / 推镜坐标换算 / 自检
scripts/render.py         # 配音 / 字幕 / 推镜音效 / 混音 / 编码
scripts/bgm.py            # go-music-dl 的 HTTP 客户端（找 / 下 BGM）
scripts/musicdl.py        # 本地 music-dl 二进制：找 / 下载 release / 起停服务
scripts/cover.py          # HTML 模板 + 浏览器截图出封面
assets/templates/         # 封面模板（横版 repo-page / 竖版 hero）
references/               # hook 开场 · capture 录制 · subtitles 字幕 · lessons 深坑 · pipeline 滤镜实现
examples/recordly-intro/  # 12 段精剪实例：project.json / plan.json / narration.txt
```

## 协议

**AGPL-3.0**，见 [LICENSE](LICENSE)。

**依赖关系写清楚**：本仓库只有编排、剪辑、字幕、混音、封面模板这些自己的代码，其它能力一律调外部，不复制别人的实现进来——

- 配音 → [latent-media](https://github.com/guohuiyuan/latent-media)（`latent.py tts`，需要它自己的 key）
- 配乐 → [go-music-dl](https://github.com/guohuiyuan/go-music-dl)（它的 web 服务接口；没服务时下它的官方 release 到本地缓存临时起一个），或你自己的曲子
- 封面渲染 → `playwright`（截本仓库的 HTML 模板）
- 录屏 → OBS / [Recordly](https://github.com/webadderallorg/Recordly)，纯工具，怎么录都行

语音模型权重为 CC-BY-NC，商用需另行确认；BGM 与录屏素材的版权责任在录制/使用者。
