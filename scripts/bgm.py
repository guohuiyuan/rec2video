#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BGM 三条路，按可用性自动选：本地曲子 / 内置合成 / 在线音乐站。

    python bgm.py synth out/bgm.mp3 --seconds 80 --mood tech   # 纯合成，无依赖
    python bgm.py search "upbeat technology" --json            # 需要先配 MUSIC_BASE
    python bgm.py get 12345 out/bgm.mp3

优先用你自己有的曲子（`--file`）；没有就用 `synth` 合一段氛围垫底音（纯 math +
wave，不联网、无版权问题）；音乐站是可选增强，配了 `MUSIC_BASE` 才启用。
选曲标准：中速、无明显人声、循环不炸——有人声会和旁白抢注意力。
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import struct
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE = os.environ.get("MUSIC_BASE") or ""
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"}
CARD = re.compile(r'<li class="song-card"(.*?)>', re.S)
ATTR = re.compile(r'data-([a-z-]+)="([^"]*)"')
# 名字里带这些的优先（纯音乐、轻快），带 live/翻唱/remix 的往后排
GOOD = ("instrumental", "纯音乐", "bgm", "ost", "piano", "lofi", "chill", "upbeat", "ambient")
BAD = ("live", "remix", "cover", "翻唱", "dj", "抖音", "女声", "男声")


def search(base: str, q: str, source: str = "netease") -> list[dict]:
    if not base:
        raise SystemExit("[!] 没配音乐站。用 --file 给本地曲子，或用 synth 合成一段；"
                         "想接在线站就设 MUSIC_BASE")
    url = f"{base}/search?" + urllib.parse.urlencode({"q": q, "type": "song", "sources": source})
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        page = r.read().decode("utf-8", "replace")
    out = []
    for block in CARD.findall(page):
        d = {k: html.unescape(v) for k, v in ATTR.findall(block)}
        if not d.get("name"):
            continue
        out.append({"id": d.get("id"), "source": d.get("source"), "name": d.get("name"),
                    "artist": d.get("artist", ""), "dur": int(d.get("duration") or 0)})
    return out


def score(row: dict, want: float = 0.0) -> float:
    """越大越合适：够长、名字像纯音乐、不是 live/翻唱。"""
    dur, name = row.get("dur") or 0, (row.get("name") or "").lower()
    if dur < 45:
        return -1e9
    s = 0.0
    s -= max(0.0, 70 - dur) * 0.6          # 太短扣分
    if want and dur < want:
        s -= (want - dur) * 1.2            # 比成片还短，得循环，扣分
    s += sum(3.0 for w in GOOD if w in name)
    s -= sum(4.0 for w in BAD if w in name)
    return s


def download(base: str, song_id: str, out: Path) -> Path:
    url = f"{base}/download?id={urllib.parse.quote(str(song_id))}&stream=1"
    req = urllib.request.Request(url, headers=UA)
    out.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, timeout=180) as r:
        data = r.read()
    if len(data) < 20000:
        raise SystemExit(f"[!] 下载到的内容太小（{len(data)} 字节），可能不是音频")
    out.write_bytes(data)
    print(f"[+] {out}  ({len(data) / 1048576:.1f} MB)")
    return out


# ---------------------------------------------------------------- 内置合成 BGM
# 没有网络、没有曲子的时候用它兜底：四和弦氛围垫底音，纯 math + wave 合成，
# 不依赖任何模型/素材/服务。四段和弦循环拼接，接缝按 0.9s 交叉淡化处理。
SR = 48000
BAR = 8.0                     # 每个和弦 8 秒
MOODS = {
    # 名称: 四个和弦（Hz 列表），低音 + 中音铺开，避免和人声抢 2–4kHz
    "tech": [(110.00, 164.81, 220.00, 261.63),      # Am
             (87.31, 130.81, 174.61, 220.00),       # F
             (130.81, 196.00, 261.63, 329.63),      # C
             (98.00, 146.83, 196.00, 246.94)],      # G
    "calm": [(130.81, 196.00, 246.94, 329.63),      # Cmaj7
             (110.00, 164.81, 220.00, 329.63),      # Am7
             (87.31, 130.81, 174.61, 261.63),       # F
             (98.00, 146.83, 196.00, 293.66)],      # Gadd9
    "warm": [(98.00, 146.83, 196.00, 293.66),       # G
             (110.00, 164.81, 220.00, 261.63),      # Am
             (130.81, 196.00, 261.63, 392.00),      # C
             (87.31, 130.81, 174.61, 220.00)],      # F
}


def _chord_block(notes, seconds: float = BAR) -> list[float]:
    n = int(seconds * SR)
    out = [0.0] * n
    two_pi = 2 * 3.141592653589793
    for i in range(n):
        t = i / SR
        v = 0.0
        for f in notes:
            v += math.sin(two_pi * f * t) * 0.25 + math.sin(two_pi * 2 * f * t) * 0.055
        # 缓慢呼吸感，避免长音发死
        out[i] = v * (0.86 + 0.14 * math.sin(two_pi * 0.11 * t))
    return out


def synth(path: Path, seconds: float, mood: str = "tech", gain: float = 0.55) -> Path:
    """合成一段可无缝循环的氛围 BGM，长度裁到 seconds。"""
    import wave

    notes = MOODS.get(mood) or MOODS["tech"]
    blocks = [_chord_block(n) for n in notes]
    blk = len(blocks[0])
    fade = int(0.9 * SR)                      # 和弦交叠长度

    # 每个块尾部淡出、下一块头部淡入后叠加 → 32s 无缝循环
    loop = [0.0] * (blk * len(blocks))
    for ci, b in enumerate(blocks):
        off = ci * blk
        for i in range(blk):
            w = 1.0
            if i > blk - fade:
                w = (blk - i) / fade          # 尾部收
            elif i < fade:
                w = i / fade                  # 头部起（和上一块尾巴互补）
            loop[off + i] += b[i] * w
    # 尾部与头部再补一次交叠，保证 loop 首尾相接
    for i in range(fade):
        loop[i] += blocks[-1][blk - fade + i] * (fade - i) / fade

    peak = max(abs(v) for v in loop) or 1.0
    loop = [v / peak * gain for v in loop]

    total = int(seconds * SR)
    path.parent.mkdir(parents=True, exist_ok=True)
    delay = int(0.008 * SR)                   # 右声道延时一点，做出宽度
    frames = bytearray()
    for i in range(total):
        v = loop[i % len(loop)]
        r = loop[(i - delay) % len(loop)] * 0.92
        frames += struct.pack("<hh", int(max(-1, min(1, v)) * 32767),
                             int(max(-1, min(1, r)) * 32767))
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes(bytes(frames))
    print(f"[+] 合成 BGM（{mood}）{path}  {seconds:.1f}s  "
          f"{path.stat().st_size / 1048576:.1f}MB —— 纯合成，无版权问题")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(prog="bgm.py", description="BGM：本地曲子 / 合成 / 在线音乐站")
    ap.add_argument("--base", default=DEFAULT_BASE)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help="按关键词搜，打印候选与时长")
    s.add_argument("keys", nargs="+")
    s.add_argument("--source", default="netease")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--json", action="store_true", help="输出结构化结果（给编排器用）")
    g = sub.add_parser("get", help="按 id 下载")
    g.add_argument("id")
    g.add_argument("out", type=Path)
    sy = sub.add_parser("synth", help="合成一段氛围 BGM（无网络/无版权素材时兜底）")
    sy.add_argument("out", type=Path)
    sy.add_argument("--seconds", type=float, default=80.0)
    sy.add_argument("--mood", default="tech", choices=sorted(MOODS))
    sy.add_argument("--gain", type=float, default=0.55, help="0.55 约等于常见成品曲的电平（均值 -15dB）")
    a = ap.parse_args()

    if a.cmd == "get":
        download(a.base, a.id, a.out)
        return
    if a.cmd == "synth":
        synth(a.out, a.seconds, a.mood, a.gain)
        return

    all_rows: list[dict] = []
    for kw in a.keys:
        try:
            rows = search(a.base, kw, a.source)
        except Exception as exc:  # noqa: BLE001
            if not a.json:
                print(f"\n===== {kw} =====\n  失败：{exc}（站点不可达就换手头的音乐）")
            continue
        for r in rows:
            r["query"] = kw
            r["score"] = round(score(r), 2)
        all_rows += rows
        if not a.json:
            print(f"\n===== {kw} =====  {len(rows)} 条")
            for r in sorted(rows, key=score, reverse=True)[: a.limit]:
                m, s2 = divmod(r["dur"], 60)
                print(f"  {str(r['id']):<12} {m}:{s2:02d}  {r['name'][:36]:<36} | {r['artist'][:20]}")
    if a.json:
        all_rows.sort(key=lambda r: r["score"], reverse=True)
        print(json.dumps({"base": a.base, "rows": all_rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
