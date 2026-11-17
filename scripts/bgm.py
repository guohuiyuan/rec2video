#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BGM：从 go-music-dl 的服务接口按关键词找曲子并下载。

    python bgm.py search "upbeat technology" --json
    python bgm.py get 12345 assets/bgm.mp3 --source netease

配乐能力来自 [go-music-dl](https://github.com/guohuiyuan/go-music-dl)（不在本仓库里，
本脚本只是它的 HTTP 客户端）。先起服务：

    music-dl web --no-browser -p 8080        # 接口 http://127.0.0.1:8080/music

再指路：`MUSIC_BASE=http://127.0.0.1:8080/music` 或 `--base`。
不接音乐站也行：把自己的曲子放到 `assets/bgm.mp3`、`bgm.src` 指过去即可。

选曲标准：中速、无明显人声、循环不炸——有人声会和旁白抢注意力。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE = os.environ.get("MUSIC_BASE") or "http://127.0.0.1:8080/music"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"}
CARD = re.compile(r'<li class="song-card"(.*?)>', re.S)
ATTR = re.compile(r'data-([a-z-]+)="([^"]*)"')
# 名字里带这些的优先（纯音乐、轻快），带 live/翻唱/remix 的往后排
GOOD = ("instrumental", "纯音乐", "bgm", "ost", "piano", "lofi", "chill", "upbeat", "ambient")
BAD = ("live", "remix", "cover", "翻唱", "dj", "抖音", "女声", "男声")


def search(base: str, q: str, source: str = "netease") -> list[dict]:
    if not base:
        raise SystemExit("[!] 没配音乐站：设 MUSIC_BASE，或 --base 指到 music-dl web 的地址")
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


def download(base: str, song_id: str, out: Path, source: str = "netease") -> Path:
    """下载单曲。music-dl 的 /download 是 `id` + `source` 两个参数都要的：
    只给 id 会直接 400 Missing params（老版本少了 source，一直下不下来）。
    stream=1 让它把音频直接吐回来，不走「存到下载目录 / 传 WebDAV」那条路。
    """
    q = urllib.parse.urlencode({"id": song_id, "source": source, "stream": "1"})
    url = f"{base}/download?{q}"
    req = urllib.request.Request(url, headers=UA)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = r.read()
            ctype = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        body = exc.read()[:200].decode("utf-8", "replace").strip()
        hint = ""
        if exc.code == 400:
            hint = "（/download 需要 id + source，source 空或不对就会 400）"
        elif exc.code == 502:
            hint = "（这个源没拿到直链：换一首，或换 source）"
        raise SystemExit(f"[!] 下载失败 {exc.code}{hint}：{body}") from None
    if ctype and not ctype.startswith(("audio/", "application/octet-stream", "binary/")):
        raise SystemExit(f"[!] 返回的不是音频（Content-Type: {ctype}）：{data[:200]!r}")
    if len(data) < 20000:
        raise SystemExit(f"[!] 下载到的内容太小（{len(data)} 字节），可能不是音频")
    out.write_bytes(data)
    print(f"[+] {out}  ({len(data) / 1048576:.1f} MB)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(prog="bgm.py", description="从 go-music-dl 找并下载 BGM")
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
    g.add_argument("--source", default="netease", help="曲子的来源（search 结果里的 source 字段）")
    a = ap.parse_args()

    if a.cmd == "get":
        download(a.base, a.id, a.out, a.source)
        return

    all_rows: list[dict] = []
    for kw in a.keys:
        try:
            rows = search(a.base, kw, a.source)
        except Exception as exc:  # noqa: BLE001
            if not a.json:
                print(f"\n===== {kw} =====\n  失败：{exc}（music-dl web 起了吗？）")
            continue
        for r in rows:
            r["query"] = kw
            r["score"] = round(score(r), 2)
        all_rows += rows
        if not a.json:
            print(f"\n===== {kw} =====  {len(rows)} 条")
            for r in sorted(rows, key=score, reverse=True)[: a.limit]:
                m, s2 = divmod(r["dur"], 60)
                print(f"  {str(r['id']):<12} {m}:{s2:02d}  {r['name'][:36]:<36} | "
                      f"{r['artist'][:20]:<20} [{r.get('source') or '?'}]")
    if a.json:
        all_rows.sort(key=lambda r: r["score"], reverse=True)
        print(json.dumps({"base": a.base, "rows": all_rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
