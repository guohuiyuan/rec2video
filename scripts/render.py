#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rec2video · render — 把「口播文案 + 画面 + BGM」合成成片。

一条命令把文案、配音、字幕、点击推镜、点击音效、BGM 闪避全部做完。

    python scripts/render.py init  myvideo            # 生成 project.json 骨架
    python scripts/render.py probe myvideo/project.json
    python scripts/render.py run   myvideo/project.json

一般不用直接调它，走上层 `rec2video.py build` 即可。
零 Python 依赖，只要求 PATH 里有 ffmpeg / ffprobe。TTS 走 Latent Audio
（Latent Audio 的 /v1/audio/speech 接口，key 自己填；也可用现成音频文件，
或复用上一次的 narration.wav）。

设计参照 Recordly 的演示节奏：画面嵌在深色画框里，点击处做一次
「ease-out 推近 → 停留 → ease-in 拉回」的推镜，同时打一记合成点击音效；
转场和花哨贴纸一概不要。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import wave
import zlib
from pathlib import Path

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
ROOT = Path(__file__).resolve().parent.parent      # 仓库根（keys.env / assets 在这层）
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/141.0 Safari/537.36"
SR = 48000


# ════════════════════════════════════════════════════════════ 基础工具
def sh(cmd, cwd=None, check=True, binary=False):
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=not binary)
    if check and res.returncode != 0:
        err = res.stderr if isinstance(res.stderr, str) else (res.stderr or b"").decode("utf-8", "replace")
        raise SystemExit(f"[!] 命令失败：{' '.join(map(str, cmd))[:200]}\n{err[-2500:]}")
    return res


def media_info(path: Path) -> dict:
    res = sh([FFPROBE, "-v", "error", "-print_format", "json",
              "-show_format", "-show_streams", str(path)])
    d = json.loads(res.stdout)
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    f = d.get("format", {})
    return {
        "duration": float(f.get("duration") or 0),
        "w": int(v["width"]) if v else 0,
        "h": int(v["height"]) if v else 0,
        "fps": eval_fps(v.get("r_frame_rate", "0/1")) if v else 0.0,
        "size": int(f.get("size") or 0),
        "vcodec": v.get("codec_name", "") if v else "",
    }


def eval_fps(r: str) -> float:
    try:
        a, b = r.split("/")
        return round(float(a) / float(b), 3) if float(b) else 0.0
    except Exception:
        return 0.0


def decode_pcm(path: Path) -> bytes:
    res = sh([FFMPEG, "-v", "error", "-i", str(path), "-ar", str(SR), "-ac", "2",
              "-f", "s16le", "-"], binary=True)
    return res.stdout


def latent_cli() -> str:
    """配音是外部的 latent-media 在做，这里只负责调它的 CLI（不重复实现 TTS）。"""
    home = Path.home()
    for c in (os.environ.get("LATENT_CLI"),
              str(home / ".workbuddy" / "skills" / "latent-media" / "scripts" / "latent.py"),
              shutil.which("latent.py"), shutil.which("latent")):
        if c and Path(str(c)).is_file():
            return str(c)
    raise SystemExit(
        "[!] 找不到 latent-media 的 CLI（配音靠它做，本仓库不内置 TTS）。\n"
        "    装：git clone https://github.com/guohuiyuan/latent-media ~/.workbuddy/skills/latent-media\n"
        "    或设 LATENT_CLI=/path/to/latent.py")


def tts_batch(pr: "Project", voice: str, segs: list[dict], force: bool) -> None:
    """一段一句，交给 latent.py 批量合成（--text-file + --split-lines）。"""
    files = [pr.vo / f"seg{i:02d}.mp3" for i in range(1, len(segs) + 1)]
    if not force and all(f.is_file() for f in files):
        print(f"[=] 复用 {len(files)} 段配音（--force 可重做）")
        return
    cli = latent_cli()
    txt = pr.work / "tts-input.txt"
    txt.write_text("\n".join(s["tts"] for s in segs), encoding="utf-8")
    print(f"[>] 配音 {len(segs)} 段 ← latent-media ({Path(cli).name}, voice={voice})")
    cmd = [sys.executable, cli, "tts", "--voice", voice, "--text-file", str(txt),
           "--split-lines", "-o", str(pr.vo) + os.sep, "--name", "seg", "--json"]
    res = sh(cmd, check=False)
    if res.returncode != 0:
        raise SystemExit(f"[!] latent.py tts 失败\n{(res.stderr or res.stdout or '')[-1500:]}")
    produced = sorted(pr.vo.glob("seg-*.mp3"))
    if len(produced) != len(segs):
        raise SystemExit(f"[!] 期望 {len(segs)} 段，实际拿到 {len(produced)} 段：{pr.vo}")
    for src, dst in zip(produced, files):        # seg-01.mp3 → seg01.mp3
        src.replace(dst)


# ════════════════════════════════════════════════════════════ 工程目录
class Project:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.root = self.path.parent
        self.d = json.loads(self.path.read_text(encoding="utf-8"))
        self.vo = self.root / "vo"
        self.work = self.root / "work"
        self.out = self.root / "out"
        for p in (self.vo, self.work, self.out):
            p.mkdir(parents=True, exist_ok=True)

    def rel(self, k: str) -> Path:
        p = Path(k)
        return p if p.is_absolute() else self.root / p

    def get(self, *keys, default=None):
        cur = self.d
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    # 常用派生值
    @property
    def segments(self) -> list[dict]:
        return self.d.get("segments", [])

    @property
    def timeline(self) -> dict | None:
        p = self.work / "timeline.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None

    @property
    def narration(self) -> Path:
        return self.work / "narration.wav"

    @property
    def ass(self) -> Path:
        return self.work / "subs.ass"

    @property
    def total(self) -> float:
        tl = self.timeline
        return tl["total"] if tl else 0.0


# ════════════════════════════════════════════════════════════ 1. 配音（交给 latent-media）
def stage_tts(pr: Project, force: bool = False) -> dict:
    prov = pr.get("voice", "provider", default="latent")
    gap = float(pr.get("voice", "gap", default=0.32))
    segs = pr.segments
    if not segs:
        raise SystemExit("[!] project.json 里 segments 是空的")

    if prov == "existing":
        if not pr.narration.is_file():
            raise SystemExit(f"[!] provider=existing 但 {pr.narration} 不存在")
        print(f"[=] 复用现成旁白 {pr.narration.name}，total={pr.total:.2f}s")
        return pr.timeline or {}

    voice = pr.get("voice", "voice", default="voice-120")
    if prov == "latent":
        tts_batch(pr, voice, segs, force)

    pcm = bytearray()
    silence = b"\x00" * int(SR * gap) * 4
    timeline, t = [], 0.0
    for i, seg in enumerate(segs, start=1):
        sub = seg.get("sub") or re.sub(r"\[[^\]]+\]", "", seg.get("tts", ""))
        if prov == "files":
            src = pr.rel(seg.get("audio", ""))
            if not src.is_file():
                raise SystemExit(f"[!] 第 {i} 段音频不存在: {src}")
            mp3 = src
        else:
            mp3 = pr.vo / f"seg{i:02d}.mp3"
            if not mp3.is_file():
                raise SystemExit(f"[!] 第 {i} 段配音缺失：{mp3}")
        dur = media_info(mp3)["duration"]
        timeline.append({"i": i, "start": round(t, 3), "dur": round(dur, 3),
                         "end": round(t + dur, 3), "sub": sub, "file": str(mp3)})
        pcm += decode_pcm(mp3)
        t += dur
        if i < len(segs):            # 段间留白，最后一段不留
            pcm += silence
            t += gap

    total = len(pcm) / (SR * 4)
    with wave.open(str(pr.narration), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(pcm))
    plan = {"total": round(total, 3), "gap": gap, "voice": voice, "segs": timeline}
    (pr.work / "timeline.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    print(f"[+] 旁白 {total:.2f}s / {len(segs)} 段 → {pr.narration.name}")
    for s in timeline:
        print(f"    {s['start']:6.2f}-{s['end']:6.2f}  {s['sub'][:34]}")
    return plan


# ════════════════════════════════════════════════════════════ 2. 字幕
def ass_time(t: float) -> str:
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def srt_time(t: float) -> str:
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s - int(s)) * 1000):03d}"


STRONG = "，。：；？！"          # 语义停顿，优先在这断
WEAK = "、"                      # 枚举顿号，万不得已才断（否则会把 txt、epub、html 拆开）


def wrap(text: str, limit: int = 20) -> str:
    """超长一行折两行：只断在标点后，避免 libass 在词中间硬断。"""
    if len(text) <= limit + 4:
        return text
    mid = len(text) / 2
    for charset in (STRONG, WEAK):
        cuts = [i for i, ch in enumerate(text) if ch in charset and 0 < i < len(text) - 1]
        if cuts:
            best = min(cuts, key=lambda i: abs(i - mid))
            return text[:best + 1] + r"\N" + text[best + 1:]
    return text


def stage_subs(pr: Project) -> Path:
    plan = pr.timeline
    if not plan:
        raise SystemExit("[!] 还没有 timeline.json，先跑 tts")
    total = plan["total"]
    segs = plan["segs"]
    sc = pr.get("subtitle", default={}) or {}
    font = sc.get("font", "Microsoft YaHei")
    size = sc.get("size", 46)
    mv = sc.get("margin_v", 46)

    cap = pr.get("caption", default={}) or {}
    c_in = float(cap.get("start_at", 0.30))
    c_out = float(cap.get("start_until", 4.40))
    e_in = float(cap.get("end_at", max(total - 6.4, 0)))

    ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {pr.get('output', 'w', default=1920)}
PlayResY: {pr.get('output', 'h', default=1080)}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00151515,&H64000000,-1,0,0,0,100,100,1,0,1,3.4,1.8,2,120,120,{mv},1
Style: Bar,{font},{max(int(size * 0.82), 24)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,2,0,1,0,0,8,40,40,14,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    caps = []
    if cap.get("start"):
        caps.append(f"Dialogue: 1,{ass_time(c_in)},{ass_time(c_out)},Bar,,0,0,0,,{cap['start']}")
    if cap.get("end"):
        caps.append(f"Dialogue: 1,{ass_time(e_in)},{ass_time(total)},Bar,,0,0,0,,{cap['end']}")
    ass += "\n".join(caps) + ("\n" if caps else "")
    for s in segs:
        end = min(s["start"] + s["dur"] + 0.30, total)
        ass += f"Dialogue: 0,{ass_time(s['start'])},{ass_time(end)},Sub,,0,0,0,,{wrap(s['sub'])}\n"
    pr.ass.write_text(ass, encoding="utf-8")

    srt = []
    for i, s in enumerate(segs, start=1):
        end = min(s["start"] + s["dur"] + 0.30, total)
        srt.append(f"{i}\n{srt_time(s['start'])} --> {srt_time(end)}\n{s['sub']}\n")
    (pr.work / "subs.srt").write_text("\n".join(srt), encoding="utf-8")
    print(f"[+] 字幕 {pr.ass.name} + subs.srt（{len(segs)} 条）")
    return pr.ass


# ════════════════════════════════════════════════════════════ 3. 素材：遮罩 + 点击音
def write_png(path: Path, w: int, h: int, rows: list[bytes]) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + r for r in rows)
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 6))
                     + chunk(b"IEND", b""))


def make_mask(path: Path, w: int, h: int, radius: int) -> None:
    """圆角矩形蒙版：圆内纯白、圆外纯黑。
    alphamerge 取的是灰度通道而不是 alpha，所以只能这么做。"""
    cx, cy = w / 2, h / 2
    hw, hh = cx - radius, cy - radius
    rows = []
    for y in range(h):
        dy = max(0.0, abs(y + 0.5 - cy) - hh)
        row = bytearray()
        for x in range(w):
            dx = max(0.0, abs(x + 0.5 - cx) - hw)
            d = math.hypot(dx, dy)
            v = int(min(1.0, max(0.0, radius - d + 0.5)) * 255)
            row += bytes((v, v, v, 255))
        rows.append(bytes(row))
    write_png(path, w, h, rows)


def one_click(seed: int, pitch: float = 1.0) -> list[float]:
    """合成一记 UI 点击：噪声瞬态 + 两个短音调 + 低频实体感。"""
    import random
    rs = random.Random(seed)
    n = int(0.060 * SR)
    out = []
    noise_prev = 0.0
    for i in range(n):
        t = i / SR
        nz = rs.gauss(0, 1)
        hp = nz - noise_prev          # 一阶差分 = 高通，做出"咔"的锐利起音
        noise_prev = nz
        body = hp * math.exp(-t / 0.0040) * 0.80
        tone = (math.sin(2 * math.pi * 2100 * pitch * t) * math.exp(-t / 0.0085) * 0.42
                + math.sin(2 * math.pi * 1080 * pitch * t) * math.exp(-t / 0.013) * 0.24)
        thump = math.sin(2 * math.pi * 155 * t) * math.exp(-t / 0.026) * 0.30
        out.append(body + tone + thump)
    peak = max(abs(v) for v in out) or 1.0
    out = [v / peak * 0.36 for v in out]
    k = int(0.001 * SR)               # 首尾 1ms 斜坡，避免爆音
    for i in range(k):
        out[i] *= i / k
        out[-1 - i] *= i / k
    return out


def make_sfx(path: Path, clicks: list[float], dur: float, gain: float = 0.55) -> None:
    total = int(dur * SR) + SR // 2
    buf = [0.0] * total
    pitches = [1.0, 0.94, 1.04, 0.97, 1.02, 0.99]
    for i, t0 in enumerate(clicks):
        w = one_click(17 + i * 13, pitch=pitches[i % len(pitches)])
        s = int(t0 * SR)
        for j, v in enumerate(w):
            if s + j < total:
                buf[s + j] += v
    pcm = b"".join(struct.pack("<h", max(-32768, min(32767, int(v * gain * 32767)))) * 2
                   for v in buf)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes(pcm)


def stage_assets(pr: Project) -> dict:
    fr = pr.get("video", "frame", default={}) or {}
    vw, vh = int(fr.get("w", 1560)), int(fr.get("h", 878))
    radius = int(fr.get("radius", 20))
    mask = pr.work / "mask.png"
    make_mask(mask, vw, vh, radius)

    zooms = pr.d.get("zooms", []) or []
    sfx = None
    if zooms and pr.total:
        sfx = pr.work / "sfx.wav"
        make_sfx(sfx, [float(z["at"]) for z in zooms], pr.total,
                 float(pr.get("sfx", "gain", default=0.55)))
    print(f"[+] 遮罩 {vw}x{vh} r={radius}；点击音 {len(zooms)} 记")
    return {"mask": mask, "sfx": sfx}


# ════════════════════════════════════════════════════════════ 4. 合成
def stage_render(pr: Project, preview: float | None = None, bgm: Path | None = None) -> Path:
    plan = pr.timeline
    if not plan:
        raise SystemExit("[!] 还没有 timeline.json，先跑 tts")
    full_total = float(plan["total"])
    total = min(full_total, preview) if preview else full_total

    out_cfg = pr.get("output", default={}) or {}
    W = int(out_cfg.get("w", 1920))
    H = int(out_cfg.get("h", 1080))
    FPS = int(out_cfg.get("fps", 30))
    CRF = int(out_cfg.get("crf", 19))
    SAMP = int(out_cfg.get("supersample", 2))

    vid = pr.get("video", default={}) or {}
    src = pr.rel(vid["src"])
    if not src.is_file():
        raise SystemExit(f"[!] 录屏不存在: {src}")
    info = media_info(src)
    trim = float(vid.get("trim_head", 0))
    crop = vid.get("crop", "")
    fr = vid.get("frame", {}) or {}
    VW, VH = int(fr.get("w", 1560)), int(fr.get("h", 878))
    MX = int(fr.get("x", (W - VW) // 2))
    MY = int(fr.get("y", 56))
    bg0 = (fr.get("bg") or ["0f1324", "2b3358"])[0]
    bg1 = (fr.get("bg") or ["0f1324", "2b3358"])[1]

    # 画面拉伸对齐旁白：录屏有效长度 vs 旁白总长
    stretch = 1.0
    if vid.get("stretch_to_voice", True):
        eff = max(info["duration"] - trim, 0.1)
        stretch = full_total / eff
        if abs(stretch - 1) > 0.35:
            print(f"[!] 拉伸系数 {stretch:.3f} 偏离 1 太多（录屏 {eff:.1f}s vs 旁白 {total:.1f}s），"
                  f"建议重录或改文案；仍按此系数渲染")
    tail = f"（预览只出前 {total:.1f}s）" if preview else ""
    print(f"[i] 源 {info['w']}x{info['h']} {info['duration']:.2f}s → 旁白 {full_total:.2f}s "
          f"(stretch {stretch:.4f}){tail}")

    IW, IH = int(VW * SAMP), int(VH * SAMP)
    ramp_in = float(vid.get("ramp_in", 0.42))
    hold = float(vid.get("hold", 1.15))
    ramp_out = float(vid.get("ramp_out", 0.55))
    zooms = pr.d.get("zooms", []) or []
    cards = pr.d.get("cards", []) or []

    # ---- 输入序列
    inputs: list[list[str]] = [["-ss", f"{trim}", "-i", str(src)]]
    inputs.append(["-i", str(pr.narration)])

    bgm_path = bgm
    if bgm_path is None and pr.get("bgm", "src"):
        bgm_path = pr.rel(pr.get("bgm", "src"))
        if not bgm_path.is_file():
            bgm_path = None
    if bgm_path:
        inputs.append(["-stream_loop", "-1", "-i", str(bgm_path)])
    bgm_idx = len(inputs) - 1 if bgm_path else None

    mask = pr.work / "mask.png"
    need_sfx = bool(zooms) and not (pr.work / "sfx.wav").is_file()
    if not mask.is_file() or need_sfx:
        stage_assets(pr)
    inputs.append(["-loop", "1", "-framerate", str(FPS), "-i", str(mask)])
    mask_idx = len(inputs) - 1

    card_idx = []
    for c in cards:
        p = pr.rel(c["png"])
        inputs.append(["-loop", "1", "-framerate", str(FPS), "-i", str(p)])
        card_idx.append(len(inputs) - 1)

    sfx = pr.work / "sfx.wav"
    sfx_idx = None
    if zooms and sfx.is_file():
        inputs.append(["-i", str(sfx)])
        sfx_idx = len(inputs) - 1

    # ---- 推镜表达式（zoompan 内部用 on/FPS 当时间）
    def T() -> str:
        return f"(on/{FPS})"

    def pulse(t0: float, z: float) -> str:
        t_out = t0 + ramp_in + hold
        tin = f"(1-pow(1-clip(({T()}-{t0})/{ramp_in},0,1),3))"
        tout = f"(1-pow(clip(({T()}-{t_out:.3f})/{ramp_out},0,1),3))"
        return f"(1+{z - 1:.3f}*{tin}*{tout})"

    def zoom_expr() -> str:
        e = "1"
        for z in zooms:
            e = f"max({e},{pulse(float(z['at']), float(z.get('z', 1.35)))})"
        return e

    def anchor(axis: str) -> str:
        if not zooms:
            return str(IW // 2 if axis == "x" else IH // 2)
        starts = [float(z["at"]) - ramp_in - 0.15 for z in zooms]
        e = f"{float(zooms[0].get(axis, IW // 2 if axis == 'x' else IH // 2)) * SAMP:.0f}"
        for i in range(1, len(zooms)):
            e = f"if(gte({T()},{starts[i]:.2f}),{float(zooms[i].get(axis, 0)) * SAMP:.0f},{e})"
        return e

    # ---- 滤镜图
    f = []
    pre = f"crop={crop}," if crop else ""
    zchain = ""
    if zooms:
        zchain = (f",zoompan=z='{zoom_expr()}'"
                  f":x='clip({anchor('x')}-{IW // 2}/zoom,0,{IW}-{IW}/zoom)'"
                  f":y='clip({anchor('y')}-{IH // 2}/zoom,0,{IH}-{IH}/zoom)'"
                  f":d=1:s={VW}x{VH}:fps={FPS}")
    else:
        zchain = f",scale={VW}:{VH}"
    f.append(f"[0:v]{pre}scale={IW}:{IH},fps={FPS},setpts=PTS*{stretch:.6f}"
             f"{zchain},setpts=PTS-STARTPTS,format=rgba,split=2[v0][v1]")
    f.append(f"[{mask_idx}:v]format=rgba,split=2[m1][m2]")
    f.append("[v0][m1]alphamerge[vid]")
    f.append("[v1][m2]alphamerge,colorchannelmixer=rr=0:gg=0:bb=0:aa=1,gblur=sigma=26[shadow]")
    f.append(f"gradients=s={W}x{H}:c0=0x{bg0}:c1=0x{bg1}:x0=0:y0=0:x1={W}:y1={H}:"
             f"d={total:.3f}:r={FPS},format=rgba[bg0]")
    f.append(f"[bg0][shadow]overlay=x={MX + 6}:y={MY + 14}[bg1]")
    f.append(f"[bg1][vid]overlay=x={MX}:y={MY}[base]")

    prev = "base"
    for i, (c, idx) in enumerate(zip(cards, card_idx)):
        t_in = float(c.get("in", 0))
        t_out = float(c.get("out", total))
        fi = float(c.get("fade_in", 0.25))
        fo = float(c.get("fade_out", 0.4))
        chain = f"[{idx}:v]format=rgba,fade=t=in:st={t_in}:d={fi}:alpha=1"
        if fo > 0:
            chain += f",fade=t=out:st={t_out - fo:.2f}:d={fo}:alpha=1"
        f.append(chain + f"[ly{i}]")
        f.append(f"[{prev}][ly{i}]overlay=x=0:y=0:enable='between(t,{t_in},{t_out})'[o{i}]")
        prev = f"o{i}"

    # 字幕每次都重生成（几十毫秒），改文案/样式即时生效，不做缓存
    stage_subs(pr)
    f.append(f"[{prev}]subtitles=filename={pr.ass.name}[vf]")

    # ---- 声音
    vo_gain = float(pr.get("voice", "gain_db", default=6))
    a_parts = []
    if bgm_idx is not None:
        bvol = float(pr.get("bgm", "volume", default=0.26))
        duck = pr.get("bgm", "duck", default=True)
        a_parts.append(f"[{bgm_idx}:a]aresample=48000,atrim=0:{total:.3f},asetpts=N/SR/TB,"
                       f"volume={bvol},afade=t=in:st=0:d=1.8,"
                       f"afade=t=out:st={max(total - 2.6, 0):.3f}:d=2.6[bgm]")
        a_parts.append(f"[1:a]volume={vo_gain}dB,asplit=2[vomix][vosc]")
        if duck:
            a_parts.append("[bgm][vosc]sidechaincompress=threshold=0.035:ratio=8:"
                           "attack=12:release=380:makeup=1[duck]")
            mix_in = "[vomix][duck]"
        else:
            mix_in = "[vomix][bgm]"
    else:
        a_parts.append(f"[1:a]volume={vo_gain}dB[vomix]")
        mix_in = "[vomix]"

    if sfx_idx is not None:
        a_parts.append(f"[{sfx_idx}:a]aresample=48000,atrim=0:{total:.3f},asetpts=N/SR/TB[sfx]")
        mix_in += "[sfx]"
        n_in = 3 if bgm_idx is not None else 2
    else:
        n_in = 2 if bgm_idx is not None else 1
    a_parts.append(f"{mix_in}amix=inputs={n_in}:duration=first:normalize=0,"
                   f"alimiter=limit=0.97[a]")

    fc = ";\n".join(f + a_parts)
    script = pr.work / "filtergraph.txt"
    script.write_text(fc, encoding="utf-8")

    stem = pr.d.get("name") or pr.path.stem
    out = pr.out / (f"{stem}-preview.mp4" if preview else f"{stem}.mp4")

    cmd = [FFMPEG, "-hide_banner", "-y"]
    for grp in inputs:
        cmd += grp
    cmd += ["-filter_complex_script", str(script),
            "-map", "[vf]", "-map", "[a]", "-t", f"{total:.3f}",
            "-c:v", "libx264", "-preset", "slow", "-crf", str(CRF), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart", str(out)]
    print(f"[>] 渲染 {out.name} …")
    # cwd 切到字幕目录，让 subtitles=filename= 拿到纯文件名
    sh(cmd, cwd=str(pr.work))
    mb = out.stat().st_size / 1048576
    print(f"[✓] {out}  ({mb:.1f} MB)")
    return out


# ════════════════════════════════════════════════════════════ 5. 探针 / 抽帧
def stage_probe(pr: Project, every: float = 5.0) -> None:
    src = pr.rel(pr.get("video", "src"))
    info = media_info(src)
    print(f"文件   {src}")
    print(f"时长   {info['duration']:.2f}s")
    print(f"画面   {info['w']}x{info['h']} @ {info['fps']}fps  ({info['vcodec']})")
    print(f"大小   {info['size'] / 1048576:.1f} MB")
    fr = pr.get("video", "frame", default={}) or {}
    print(f"画框   {fr.get('w', 1560)}x{fr.get('h', 878)} 基准坐标，crop="
          f"{pr.get('video', 'crop', default='(未设置，整幅缩放)')}")
    if pr.segments:
        est = sum(len(s.get("sub", "")) for s in pr.segments)
        print(f"文案   {len(pr.segments)} 段 / {est} 字（中文约 4.5 字/秒，估 "
              f"{est / 4.5:.1f}s，实际以 TTS 为准）")

    frames = pr.work / "frames"
    frames.mkdir(exist_ok=True)
    for old in frames.glob("*.jpg"):
        old.unlink()
    sh([FFMPEG, "-hide_banner", "-y", "-i", str(src), "-vf", f"fps=1/{every}",
        "-q:v", "3", str(frames / "f_%03d.jpg")])
    got = sorted(frames.glob("*.jpg"))
    print(f"[+] 抽帧 {len(got)} 张（每 {every}s）→ {frames}")
    if got:
        print("    看这些帧确定 crop 与点击时刻；帧号 i 对应 i×%g 秒" % every)


# ════════════════════════════════════════════════════════════ 6. init
SKELETON = {
    "name": "my-intro",
    "video": {
        "src": "record.mp4",
        "trim_head": 0,
        "crop": "",
        "stretch_to_voice": True,
        "frame": {"w": 1560, "h": 878, "radius": 20, "bg": ["0f1324", "2b3358"],
                  "ramp_in": 0.42, "hold": 1.15, "ramp_out": 0.55},
    },
    "voice": {"provider": "latent", "voice": "voice-120", "gap": 0.32, "gain_db": 6},
    "segments": [
        {"tts": "[笑] GitHub 上又传来喜讯！", "sub": "GitHub 上又传来喜讯！"},
    ],
    "zooms": [{"at": 8.0, "x": 780, "y": 440, "z": 1.36}],
    "cards": [],
    "caption": {"start": "项目名 ｜ 一句话定位", "end": "github.com/owner/repo ｜ ★ 0",
                "start_at": 0.3, "start_until": 4.4},
    "subtitle": {"font": "Microsoft YaHei", "size": 46, "margin_v": 46},
    "bgm": {"src": "", "volume": 0.26, "duck": True},
    "output": {"w": 1920, "h": 1080, "fps": 30, "crf": 19, "supersample": 2},
}


def cmd_init(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    p = d / "project.json"
    if p.exists():
        raise SystemExit(f"[!] {p} 已存在，不覆盖")
    p.write_text(json.dumps(SKELETON, ensure_ascii=False, indent=2), encoding="utf-8")
    for sub in ("vo", "work", "out", "assets"):
        (d / sub).mkdir(exist_ok=True)
    print(f"[+] {p}")
    print("    接下来：把录屏放进来改 video.src → probe 抽帧看 crop → 写 segments → run")


# ════════════════════════════════════════════════════════════ CLI
def main() -> None:
    ap = argparse.ArgumentParser(prog="render.py", description="录屏介绍片渲染（配音/字幕/推镜/音效/混音）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="生成 project.json 骨架")
    s.add_argument("dir", type=Path)

    for name, hlp in (("probe", "看录屏信息并抽帧"), ("tts", "生成配音与时间轴"),
                      ("subs", "生成 ass/srt 字幕"), ("render", "合成成片"),
                      ("run", "tts + subs + render 一条龙")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("project", type=Path)
        if name in ("tts", "run"):
            p.add_argument("--force", action="store_true", help="重做已有分段配音")
        if name in ("render", "run"):
            p.add_argument("--preview", type=float, help="只渲染前 N 秒")
            p.add_argument("--bgm", type=Path, help="覆盖 project.json 里的 BGM")
        if name == "probe":
            p.add_argument("--every", type=float, default=5.0, help="抽帧间隔秒数")

    a = ap.parse_args()
    if a.cmd == "init":
        cmd_init(a.dir)
        return

    pr = Project(a.project)
    if a.cmd == "probe":
        stage_probe(pr, a.every)
    elif a.cmd == "tts":
        stage_tts(pr, a.force)
    elif a.cmd == "subs":
        stage_subs(pr)
    elif a.cmd == "render":
        stage_render(pr, a.preview, a.bgm)
    elif a.cmd == "run":
        stage_tts(pr, a.force)
        stage_subs(pr)
        stage_assets(pr)
        stage_render(pr, a.preview, a.bgm)


if __name__ == "__main__":
    main()
