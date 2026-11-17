# 滤镜链路与实现笔记

`render.py render` 生成的滤镜图会写到 `work/filtergraph.txt`，出问题第一件事是打开它。

## 1. 画面链路

```
[0:v] crop=净区 → scale=(VW*2)x(VH*2) → fps → setpts*stretch
     → zoompan(推镜) → setpts-STARTPTS → split 成 2 份
```

- **超采样 2×**：`IW=VW*2`。zoompan 在放大时按整数像素取整，1:1 输入下推镜会有轻微抖动；先放大 2 倍再让 zoompan 缩回目标尺寸，抖动就看不见了。代价是内存和编码时间。
- **`setpts*stretch` 放在 zoompan 之前**：拉伸的是"输入帧的时间戳"，zoompan 输出的帧数不变，它的 `on`（输出帧号）仍等于输出时间轴，所以推镜时刻 `at` 就是最终成片里的秒数，不用二次换算。
- **`stretch = 旁白总长 / (录屏时长 - trim_head)`**。旁白比录屏长就慢放，短就快放。偏离 1 超过 0.35 会警告——那说明文案和录屏节奏对不上，该重写文案，而不是硬拉伸。

## 2. 推镜表达式

```python
zoom(t) = max(1, 1+(Z-1)*(1-(1-clip((t-t0)/0.42,0,1))**3)*(1-clip((t-t1)/0.55,0,1)**3))
x(t)    = clip(anchor_x - IW/2/zoom, 0, IW - IW/zoom)
```

- 两个 `1-pow(...,3)` 分别是 ease-out 推近和 ease-in 拉回，相乘得到单向脉冲；多个脉冲用 `max` 合并，所以**两个推镜在时间上重叠时取较大倍率**，不会打架。
- `anchor` 按时间段切换（`if(gte(t, start_i), anchor_i, ...)`），切换点放在推镜开始前 0.15s，此时 zoom 还是 1，看不见跳变。
- `x/y` 里的 `zoom` 是 zoompan 的**当前缩放变量**，可以直接用；`IW/zoom` 是当前可见宽度。

**倍率上限**：画框宽 1560，倍率 z 时可见宽度是 `1560/z`。z=1.4 → 1114px。如果被框住的卡片有 1200px 宽，就会切掉两侧文字。要么降倍率，要么把锚点移到卡片左边界让右侧自然出血。

## 3. 圆角画框

```python
[0:v]...split=2[v0][v1]
[mask]split=2[m1][m2]
[v0][m1]alphamerge[vid]                              # 取 alpha
[v1][m2]alphamerge,colorchannelmixer=rr=0:gg=0:bb=0:aa=1,gblur=sigma=26[shadow]
gradients=...[bg0]
[bg0][shadow]overlay=x=MX+6:y=MY+14[bg1]
[bg1][vid]overlay=x=MX:y=MY[base]
```

- **mask 必须是"圆内纯白、圆外纯黑"**。`alphamerge` 取的是灰度当 alpha，不是 alpha 通道——画成"白色方块带 alpha 圆"是不生效的，这是当年踩过的坑。
- 投影 = 同一 mask 去色压黑后高斯模糊，向右下偏移 6/14 px。**偏移量小、模糊大**才有"纸片浮起"的感觉。
- 背景用 `gradients` 滤镜斜向渐变，`d=` 要写时长，否则动画源会循环。

## 4. 字幕

- 用 libass 的 `subtitles=filename=subs.ass`。**必须用相对文件名**：filtergraph 里 Windows 盘符的 `:` 会被当成参数分隔符，`filename=C:/x/y.ass` 直接报错。所以渲染时 `cwd` 切到字幕所在目录。
- 折行策略：超过 24 字（含标点）时，在**中点附近最近的标点**后插入 `\N`。直接让 libass 自动换行会在词中间断，中文观感很差。
- 顶部小字（项目名/仓库地址）用独立 `Bar` 样式（Alignment 8 顶部居中），单独控制字号，不要和主字幕挤在一个样式里。
- 字幕底边距 `margin_v` 要避开画框：画框底边在 `MY+VH`（默认 56+878=934），底部只剩 146px，两行字幕占约 94px，所以 **字幕必然贴着画框下方**，`margin_v` 只能给到 46 左右（越小越靠下）。想要明显留白，得压 `frame.h` 并同步把 `crop` 高度改小，别只调 `margin_v`。
- 折行只断在标点后，且**强标点（，。：；？！）优先于顿号**——否则「txt、epub、html」这种枚举会被拆到两行。

## 5. 声音

```
BGM:  volume=0.26, afade in 1.8s / out 2.6s
人声: volume=+6dB, split → 一路混音、一路当侧链触发
      [bgm][vosc] sidechaincompress=threshold=0.035:ratio=8:attack=12:release=380
点击音: sfx.wav, 与 zooms 同时间点
      amix(inputs=3, normalize=0) → alimiter=limit=0.97
```

- **`normalize=0` 很关键**：默认 amix 会把每路除以输入数，人声会被压到 1/3。
- 侧链闪避让人声一出 BGM 自动躲开，`release=380` 偏长一点更自然（不会"抽气"）。
- `alimiter=limit=0.97` 兜底防削波。成片整体响度落在 −14 LUFS 左右，符合视频平台习惯。
- 点击音效是合成的：噪声一阶差分（高通瞬态）+ 2100Hz/1080Hz 两个短衰减音 + 155Hz 低频 thump，总长 60ms。太纯会像"叮"，没有噪声瞬态会像"咚"。

## 6. 性能

- 58 秒 1080p、preset slow、超采样 2×：约 3–5 分钟（取决于机器）。
- 调参数阶段**一定用 `--preview 8`**：只渲前 8 秒，10 秒内就能出结果，能验证画框/字幕/推镜/音效是否对。
- `preset` 想更快可以改 `output.crf` 或临时降到 `medium`；`pix_fmt yuv420p` 必留（否则 B 站/微信不认）。

## 7. 常见报错

| 现象 | 原因 | 处理 |
|---|---|---|
| `Unable to parse option value "C"` / `No such filter` | 滤镜里出现了 `C:/...` 的盘符冒号 | 字幕、遮罩等改相对路径并 `cwd` 到该目录 |
| 画面全黑 | crop 取到了窗口外的区域 | `probe` 抽帧重算净区 |
| 音频只有 BGM 或只有人声 | `amix` 输入数与 mixer 标签不匹配 | 看 `filtergraph.txt` 里 `[xx][yy]amix` 的输入数 |
| 音画不同步越到后面越偏 | 录屏有变速/掉帧，或 `trim_head` 不准 | 量一次源时长，重设 `trim_head`；必要时关 `stretch_to_voice` |
| 推镜到一半画面卡住 | zoompan 的 `d=1` 被改成了 >1 | 保持 `d=1`，靠 `z` 表达式驱动缩放 |
| 字幕不出现 | `fontsdir`/字体名不对，或 ass 的 `PlayResX` 与输出不符 | 字体写系统中存在的名字（`Microsoft YaHei`），PlayRes 与 `output` 一致 |
