# 实例：Recordly 介绍片（12 段 / 77.5s / 13 个推镜）

真实跑通过的一版，用来对照格式和手感，不是模板。

> 文案是 rec2video 1.0 写的，首句「录个演示视频，想让画面看着专业，以前真得会剪辑加动效」属于痛点铺垫式软开场。1.1 起按 `references/hook.md` 要求**第一句复述标题**（例：`一个搜索框，搜遍 14 个音乐源——开源项目 go-music-dl`），这版 `narration.txt` 保留原样作对照，别照抄首句。

```
recordly-intro/
├─ project.json      # 渲染参数：画框、配音、标版、字幕、BGM
├─ narration.txt     # 口播文案，一行一段（[笑] 这类气声只给合成器）
├─ plan.json         # 精剪取材点（segments[].at）+ 推镜点（shots，原片坐标）
└─ README.md
```

## 这版的来历

- 源录屏 346s / 2560×1600，前 60 秒是开浏览器找素材，全是废的 → `crop=2560:1440:0:60`
- 从联系表挑出 12 段有效画面对应 12 句文案，按旁白反剪 → `stretch = 1.0000`
- 配音 `voice-120`（大肥鱼），钩子句用了 `[笑]`
- 推镜 13 个，锚点从**原片坐标**换算（`plan.json` 里就是原片坐标，`cut` 自动转画框坐标）

## 复现

```bash
R=../../scripts/rec2video.py
cp -r recordly-intro /tmp/demo && cd /tmp/demo
python $R script project.json narration.txt          # 灌文案
# 改 plan.json 的 src 指向你自己的录屏，再把 segments[].at 按联系表核对一遍
python $R build project.json --preview 8             # 先看 8 秒
python $R build project.json
python $R check project.json
```

`project.json` 里的 `video.src` 指向 `assets/screen-edit.mp4` —— 那是 `cut` 的产物，第一次跑之前它会由 `cut` 自动写入。
