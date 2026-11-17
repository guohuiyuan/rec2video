# rec2video

**一段录屏 → 一条能发布的介绍片。**

给 agent 一个录屏文件，它把剩下的事做完：定位可用画面、写口播、配音、按旁白时长反剪、点击处推镜 + 音效、中文字幕、BGM 闪避、多尺寸封面。

```
你：这录屏帮我做个介绍视频，仓库 https://github.com/owner/repo
它：成片 mp4 + 字幕 srt + 16:9/4:3 封面 + 标题简介
```

不需要剪辑软件，不需要会 ffmpeg。硬依赖只有 `ffmpeg` / `ffprobe`。

## 它替你做的六件事

| 环节 | 怎么做的 |
|---|---|
| 找素材 | 生成带**原片绝对时间**的联系表，一眼看出第几秒是有效画面 |
| 配音 | [Latent Audio](https://github.com/guohuiyuan/latent-media) 社区音色（165 个），台词里能嵌 `[笑]` `[轻笑]` 这类气声 |
| 对齐 | **先配音、再按每段旁白时长反剪画面**，拉伸系数恒定 1.0000，十段之后也不会漂 |
| 字幕 | 从配音时间轴逐段推出来，尾部留 0.3s；超长只断在标点后（`，。：；？！` 优先于 `、`）；`[笑]` 这类气声只进口播不进字幕 |
| 动效 | 点击处 `ease-out 推近 → 停留 → ease-in 拉回`，同帧打一记程序合成的点击音 |
| 混音 | BGM 侧链闪避（说话时自动让路）+ 限幅，字幕烧进画面另存 srt |
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
python scripts/rec2video.py doctor                       # 体检：ffmpeg / 配音 key / 音乐站
python scripts/rec2video.py new ~/Videos/demo.mp4 \
        --repo https://github.com/owner/repo             # 建工程 + 联系表
python scripts/rec2video.py build demo/project.json      # 出片
```

中间只差一件事：**文案**。写 `narration.txt`（一行一段，8–12 行 / 60–80 秒）后 `script` 灌进去，agent 会自动做这一步。

```bash
python scripts/rec2video.py script demo/project.json narration.txt
python scripts/rec2video.py build  demo/project.json            # 配音+对齐+推镜+字幕+BGM+渲染
python scripts/rec2video.py check  demo/project.json            # 交付前自检
python scripts/rec2video.py cover  demo/project.json            # 封面（要 playwright）
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
- **>150s 精剪**：`plan` 生成骨架 → 从联系表填取材点和推镜点 → `cut` 按旁白反剪。3 分钟的录屏挑 70 秒，比反复 NG 重录快。

精剪的真实案例（12 段 / 77.5s / 13 个推镜）在 [`examples/recordly-intro/`](examples/recordly-intro/)。

## 字幕

字幕**不手打时间码**：一条字幕 = 一段旁白，时间是配音实测时长（`work/timeline.json`）→ 尾部 +0.3s。改文案重跑 `build` 就自动重算。

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
| `ffmpeg` / `ffprobe` | ✅ | 进 PATH 即可 |
| 配音 key | ✅ | Latent Audio `lat_tk_…`，写 `keys.env` 或 `LATENT_AUDIO_KEY`。见 [latent-media](https://github.com/guohuiyuan/latent-media) |
| BGM 站 | – | 默认连自建 [go-music-dl](https://github.com/guohuiyuan/go-music-dl) 实例，`MUSIC_BASE` 可换。连不上就留空，片子照样成 |
| `playwright` | – | 只有出封面要（用本机 Edge 当内核） |

```bash
cp keys.env.example keys.env    # 填 LATENT_AUDIO_KEY=lat_tk_xxx
```

## 目录

```
SKILL.md                  # agent 入口：只给录屏时的完整执行手册
scripts/rec2video.py      # 编排器（doctor/new/script/plan/cut/bgm/build/check/cover）
scripts/screencut.py      # 扫描联系表 / 反剪 / 推镜坐标换算 / 自检
scripts/render.py         # 配音 / 字幕 / 推镜音效 / 混音 / 编码
scripts/bgm.py            # 从 go-music-dl 找并下载 BGM
scripts/cover.py          # HTML 模板 + 浏览器截图出封面
assets/templates/         # 封面模板（横版 repo-page / 竖版 hero）
references/               # capture 录制 · subtitles 字幕 · lessons 深坑 · pipeline 滤镜实现
examples/recordly-intro/  # 12 段精剪实例：project.json / plan.json / narration.txt
```

## 协议

**AGPL-3.0**，见 [LICENSE](LICENSE)。

用到的第三方：Latent Audio（配音服务，模型权重为 CC-BY-NC，商用另行确认）、[go-music-dl](https://github.com/guohuiyuan/go-music-dl)（AGPL-3.0，曲库版权归各平台）、[Recordly](https://github.com/webadderallorg/Recordly)（AGPL-3.0，仅作录屏工具）。BGM 与录屏素材的版权责任在录制/使用者。
