---
name: rec2video
description: "把一段原始录屏变成能直接发布的介绍片：自动体检录屏并生成带时间戳联系表、按文案用 Latent 音色配音、按旁白时长反剪画面（拉伸系数恒定 1.00）、点击处推镜 + 合成音效、烧中文字幕、从 go-music-dl 取 BGM 做侧链闪避、最后出 16:9/4:3 封面与标题简介。当用户说「把这个录屏做成介绍视频」「介绍这个 GitHub 项目 / 这个软件」「录屏太长没法配旁白」「给仓库录个 demo」「录屏和配音对不上」时使用。用户只给一个录屏文件也能跑完。"
display_name: "录屏成片流水线"
agent_created: true
version: 1.0.0
tags: [视频, video, 录屏, 介绍片, ffmpeg, 配音, TTS, 字幕, BGM, 推镜, GitHub, 自动化, OBS, Recordly]
---

# rec2video · 录屏 → 成片

一句话：**给一个录屏，出一条带配音、字幕、推镜、音效、BGM 的 1080p 介绍片 + 封面 + 标题简介。**

只依赖 `ffmpeg` / `ffprobe`。配音走 Latent Audio（[latent-media](https://github.com/guohuiyuan/latent-media)），配乐走 [go-music-dl](https://github.com/guohuiyuan/go-music-dl)，录屏用 OBS / [Recordly](https://github.com/webadderallorg/Recordly) 都行（录制要点见 `references/capture.md`）。

```
录屏 ──new──▶ 联系表 + project.json ──script──▶ 文案 ──build──▶ 成片 ──cover──▶ 封面/标题
                                                 │
                            Latent 配音 → 反剪对齐 → 推镜音效 → 字幕 → BGM 闪避
```

## 0. 用户只丢来一个录屏（默认路径，别问一堆问题）

```bash
R=<本 skill 目录>/scripts/rec2video.py
python "$R" doctor                                    # 环境有缺口先补，别带病往下跑
python "$R" new <录屏> --repo https://github.com/owner/repo   # 体检 + 联系表 + 工程
```

`new` 之后必须做的事，一步都别跳：

1. **真的去看 `scan/sheet_*.jpg`**（用 ViewImage 逐张看）。每格左上角烧的是**原片绝对时间**。要找三件事：界面真正的开始秒数、每个动作的秒数、哪几段是加载/空转/别的窗口。
2. **最多问用户两个问题**：① 这录屏在演示什么、仓库地址是哪个（能从上下文推出来就别问）；② 音色（默认 `voice-120` 大肥鱼，语气在文案里用 `[笑]`/`[轻笑]` 控制，不确定就别换）。
3. **写文案**：`narration.txt`，一行一段，8–12 行 / 60–80 秒。规则见第 3 节。写完 `python "$R" script <工程>/project.json narration.txt`。
4. `python "$R" build <工程>/project.json` —— 配音、对齐、推镜、字幕、BGM、渲染一条龙。
5. `python "$R" check <工程>/project.json`，再抽帧看图（第 5 节）。
6. `python "$R" cover <工程>/project.json` 出封面，然后按第 6 节写标题与简介。

交付四件：`out/<name>.mp4`、`work/subs.srt`、`cover/*.png`、标题+简介（markdown 文本）。

## 1. 命令表

| 命令 | 作用 |
|---|---|
| `doctor` | 体检 ffmpeg / 配音 key / 音乐站 / 可选 playwright |
| `new <录屏> [-d 目录] [--name 名] [--repo URL] [--voice slug] [--script txt]` | 建工程：探测、联系表、`project.json` 预设（crop/画框/标版） |
| `script <project> <txt>` | 把一行一段的文案灌进 `segments` |
| `info <project> [--every 5]` | 看参数 + 每 N 秒抽帧 |
| `tts <project> [--force]` | 配音 → `work/timeline.json`（先配音，才能剪画面） |
| `subs <project>` | 只重出字幕（`subs.ass` 烧录版 + `subs.srt` 外挂版），改字号/文案后秒验 |
| `plan <project>` | 按旁白时长生成取材骨架 `plan.json` |
| `cut <project>` | 按 `plan.json` 反剪画面 + 换算推镜点 |
| `bgm <project> [-q 关键词] [--pick id] [--off]` | 找一段 BGM 并用上（自动挑够长、像纯音乐的） |
| `build <project> [--preview N] [--force] [--bgm auto\|off]` | 配音 →（有 plan 就反剪）→ 配乐 → 渲染 |
| `check <project>` | 交付前自检：stretch、锚点、推镜间距、标版 |
| `cover <project> [--repo URL] [--sizes 16x9,4x3]` | 抓仓库信息出封面 PNG |

## 2. 直出 vs 精剪

`new` 会按录屏时长自动判 `mode`：

| 模式 | 条件 | 做法 |
|---|---|---|
| **short 直出** | ≤150s | 整段画面按旁白总长拉伸对齐（`stretch_to_voice`）。要求文案长度和画面长度接近，否则 `stretch` 偏离 1 太远会明显变速 |
| **long 精剪** | >150s | 联系表挑 10–14 段 → `plan` → 填 `at` 与 `shots` → `cut` → 再 build。stretch 能到 1.0000 |

精剪的 `plan.json`（关键就两个数组，注释里是真实案例，见 `examples/recordly-intro/plan.json`）：

```json
{
  "src": "C:/path/record.mp4",          // 原始录屏，不是剪完的
  "crop": "2560:1440:0:60",             // 页面净区，去掉标题栏/书签栏/任务栏
  "fps": 30,
  "segments": [{"at": 69.8, "max": 9.8, "note": "编辑器全貌"}],
  "shots":    [{"seg": 1, "off": 2.0, "x": 1280, "y": 716, "z": 1.36}]
}
```

- `segments[].at` = 这段内容在**原录屏**第几秒（从联系表读，**填完回去再看一眼那 6 秒里确实是那个面板**）。
- `shots[].x/y` 填**原片像素坐标**（不是画框坐标，也不是 0–1 比例），`cut` 会按 crop 和画框尺寸自动换算。
- `shots[].z` 1.25–1.6；框里有文字别超 1.4。
- `shots` 可以空着——没有推镜照样是能发的片子，只是平一点。

## 3. 文案：按"画面里的一个动作"写一段

- 一段 = 一个动作。不要按"产品有几个卖点"分段，卖点可以三个人挤一段，但一段塞两个不相关画面必然错位。
- 每段 12–25 字，念出来 3–8 秒；8–12 段 / 60–80 秒是技术讲解片的舒适区。
- 第一段给钩子，最后一段给行动号召（求 Star / 报地址）。
- **口播 ≠ 字幕**：`[笑]`、`[轻笑]`、`[叹气]` 只给合成器，`sub` 里会被自动去掉；数字写阿拉伯数字；专有名词用「」或破折号隔开。
- 气声事件放句首，别每段都加。
- **钩子句不能和画面对打**：画面已经是套好背景的漂亮成片，旁白就别说"画面干巴巴"。
- 真实可用的开场（12 段 77.5s，看 `examples/recordly-intro/narration.txt`）：

```
[笑] 录个演示视频，想让画面看着专业，以前真得会剪辑加动效。
这个开源项目叫 Recordly，把录屏和剪辑塞进同一个软件。
左边一排背景，点一下就套上——墙纸、渐变、纯色，不用自己找图。
```

## 4. 字幕：从配音时间轴推出来，不手打时间码

字幕的一条 = 旁白的一段，时间就是 `timeline.json` 里的 `start → start + dur + 0.30`（尾部留 0.3s，避免话音刚落字就没了）。所以**改文案只需要重新 `script` + `build`，时间轴自己重算**。

三条硬规则：

1. **口播 ≠ 字幕**。`[笑]`/`[轻笑]`/`[叹气]` 只给合成器，`sub` 里必须去掉（`script` 已自动处理）；数字写阿拉伯数字；专有名词用「」隔开。
2. **折行只断在标点后**：≤20 字不折；超了选离中点最近的标点断，`，。：；？！` 优先于 `、`（否则 `支持 txt、epub、html` 会被劈成两行）。放不下就删字，别挤三行。
3. **`margin_v` 是从画面底边算的，不是离画框**。画框下沿到画面底只剩 146px，两行字幕约 94px + 描边，所以默认 46 已经是上限；想给字幕更多空间就压 `video.frame.h`，别往上推字幕。

双份输出：`work/subs.ass` 烧进画面（每次 render 重新生成，改文案立即生效，**不做缓存**），`work/subs.srt` 外挂投稿用。片头/片尾标版走 `caption`（顶部 `Bar` 小字，`new --repo` 自动填仓库地址与 star）。

细节与检查清单：`references/subtitles.md`。

## 5. 动效原则

- **画面本身不动，只在点击处轻推一下**：`ease-out 0.42s → 停留 1.15s → ease-in 0.55s`，倍率 1.32–1.52。
- 点击音效是程序合成的，在 `zooms` 每个时间点自动打一记，和推镜严格同步——不要手动找音效素材。
- **不要章节卡、不要高亮框、不要转场特效**。标题卡只留片头一张、片尾一张（`cards`，可选）。
- 两次推镜间隔 < 2s、锚点和倍率又几乎一样时，看起来像"画面抖了一下"——`check` 会拦。
- BGM 音量 0.26、侧链闪避默认开；有人声的曲子会和旁白抢注意力。

## 6. 交付前必看

`check` 只能拦机械错误（stretch 偏离、锚点越界、推镜挤在一起、标版越界），下面这些要靠眼睛：

1. `--preview 8` 先看前 8 秒：画框居中、字幕没贴住画框、片头标版 2 秒内退干净。
2. 抽推镜**最大倍率**那一帧：被框住的文字有没有被切掉。
3. 头尾各一帧：顶部小字在不在，片尾不是黑屏。
4. 数字口径：**封面、标题、简介、口播里的 star 数/功能数四处一致**，star 数现查 GitHub API，别抄参考视频。

## 7. 标题句式（技术区已验证）

- **喜讯体**：`GitHub 又传来喜讯！<领域>这块最高的山来了！<一句能力陈述>，<能力>、<能力>，<格式/帧率>`
- **狠活体**：`<情绪词>！Github又出狠活神器！斩获 <XX.X>k Stars，<领域>这块最高的山来了！支持 A、B、C、D 等功能`

## 8. 依赖与凭证

| 需要 | 说明 |
|---|---|
| `ffmpeg` / `ffprobe` | 唯一硬依赖，进 PATH 即可 |
| 配音 key | Latent Audio，`lat_tk_` 开头。写进仓库根 `keys.env` 或 `~/.config/latent/keys.env`，或环境变量 `LATENT_AUDIO_KEY`。配法见 [latent-media](https://github.com/guohuiyuan/latent-media) |
| 音色 | 默认 `voice-120`（大肥鱼，中文女声）。换音色查 latent-media 的 `voices` 命令 |
| BGM | 默认连自建 [go-music-dl](https://github.com/guohuiyuan/go-music-dl) 实例；`MUSIC_BASE` 换成自己的。连不上就让 `bgm.src` 留空——片子照样有旁白/字幕/音效 |
| `playwright` | 只有出封面要；没装就只出 spec.json，手动补 |

## 9. 出错先看

| 现象 | 先查 |
|---|---|
| `stretch 0.9x` / 画面越走越早 | 文案太长或画面太短。加取材时长，或删几句 |
| 推镜位置偏下/偏上 | `shots` 填的是画框坐标了。必须填**原片**坐标 |
| 字幕贴死画框 | 画框下沿到输出底边不足 130px；压 `video.frame.h` |
| 配音报 504 | `render.py` 会自动用 job_id 续取，不用重跑 |
| 渲染 `x264 malloc failed` | 内存不够，切片已加 `-threads 2`；降低输出分辨率 |
| 中文变方块 | 系统缺 `Microsoft YaHei`；换 `subtitle.font` |
| 音乐站连不上 | BGM 留空，或 `MUSIC_BASE` 指自己的实例 |

深坑清单：`references/lessons.md`（坐标系、反剪顺序、字幕几何）；字幕细节：`references/subtitles.md`；滤镜实现：`references/pipeline.md`；录制参数：`references/capture.md`。
