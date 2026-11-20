#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开场自检：第一句是不是**照着发布标题念的**。

    python hookcheck.py <project.json> [--title "好家伙！Github又出狠活神器！..."]

不联网、不读视频，只看 `project.json` 里的 `title` 与 `segments[0]`：

  1. `title` 得存在——标题没落盘，就没有"照念"这回事（最常见的失败）
  2. 项目名必须出现在首句
  3. 标题里的数字，一个都不许漏（照念的话必然全落）
  4. 与标题的字面重合（LCS 覆盖率）≥ 85% —— 这才叫"照念"
     低于 85% 但首句是标题的连续片段（占标题 55% 以上）→ 算"从某某起照念"，只提示
     其余一律 ✗：那是自己重写的一句，不是标题

判据只认字面，不认"我觉得复述了"。首句和标题本来是同一句话时，观众在信息流
里扫到的那半句，进视频第一秒又原样听到一遍——承接靠的就是这个"原样"。
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

NUM = re.compile(r"\d+(?:\.\d+)?\s*[kKwW万倍]?")
# 中文口播语速：约 5 字/秒
CPS = 5.0


def _plain(s: str) -> str:
    """去掉 [笑] 这类只给合成器的气声标记。"""
    return re.sub(r"\[[^\]]{1,8}\]", "", s or "")


def _squash(s: str) -> str:
    """只留字、数字、字母，全小写——比较用（标点、空格一律不算）。"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", _plain(s).lower())


def _nums(s: str) -> list[str]:
    out: list[str] = []
    for m in NUM.finditer(_plain(s)):
        v = m.group(0).replace(" ", "").lower()
        if v not in out:
            out.append(v)
    return out


def _lcs(a: str, b: str) -> int:
    """最长公共子序列长度（两行滚动，标题几十字，够快）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0]
        for j, cb in enumerate(b, 1):
            cur.append(prev[j - 1] + 1 if ca == cb else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1]


def _gaps(title: str, first: str) -> list[str]:
    """标题里哪些片段没被念（用原文比，提示里能直接看懂）。"""
    a, b = _plain(title), _plain(first)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out: list[str] = []
    for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            frag = a[i1:i2].strip()
            if len(re.sub(r"[\s\W_]+", "", frag)) >= 2:
                out.append(frag)
    return out[:4]


def _names(proj: dict) -> list[str]:
    repo = (proj.get("repo") or "").rstrip("/").split("/")[-1]
    out: list[str] = []
    for s in (repo, proj.get("name") or ""):
        if not s:
            continue
        for cand in (s, s.replace("-", " ").replace("_", " ")):
            q = _squash(cand)
            if q and q not in out:
                out.append(q)
    return out


def _slice_at(title: str, fsq: str) -> str:
    """首句是从标题哪个词开始念的（给提示用）。"""
    tsq = _squash(title)
    at = tsq.find(fsq)
    if at <= 0:
        return ""
    cnt, pos = 0, 0
    for i, ch in enumerate(title):
        if _squash(ch):
            cnt += 1
        if cnt == at:
            pos = i
            break
    frag = title[:pos + 1]
    for sep in ("！", "，", "!", "\u3000"):
        frag = frag.split(sep)[-1]
    return frag.strip(" 。，！—、-")[:14]


def check(proj: dict, title: str | None = None,
          min_ratio: float = 0.85) -> dict:
    title = (title if title is not None else proj.get("title") or "").strip()
    segs = proj.get("segments") or []
    first = ""
    if segs:
        first = (segs[0].get("sub") or segs[0].get("tts") or "").strip()
    first_plain = _plain(first)

    items: list[dict] = []
    ok = True

    if not title:
        ok = False
        items.append({"ok": False,
                      "msg": "project.json 里没有 title（发布标题原文），首句没法校验"})
    if not first:
        ok = False
        items.append({"ok": False, "msg": "还没有第一段文案"})
    if not title or not first:
        return {"ok": ok, "title": title, "first": first_plain, "items": items,
                "ratio": 0.0, "missing": [], "numbers": [], "hit": [],
                "name": "", "gaps": [], "contig": False}

    tsq, fsq = _squash(title), _squash(first)

    # 2. 项目名：只要求"标题里有，首句就得有"——首句就是标题，标题里没有的
    #    字，照念也变不出来，硬判只会逼人去改首句
    names = _names(proj)
    in_title = [n for n in names if n and n in tsq]
    hit_name = next((n for n in names if n and n in fsq), "")
    if not names:
        pass
    elif in_title:
        if hit_name:
            items.append({"ok": True, "msg": f"项目名 {hit_name}"})
        else:
            ok = False
            items.append({"ok": False,
                          "msg": f"项目名没念出来（标题写的是 {in_title[0]}）"})
    else:
        items.append({"ok": True, "warn": True,
                      "msg": f"标题里没有仓库名（{names[0]}），首句自然没有——"
                             f"中段或片尾标版里要出现一次"})

    # 3. 数字：照念的话标题里的数字一个都不会漏
    t_nums = _nums(title)
    f_nums = _nums(first_plain)
    hit = [n for n in t_nums if n in f_nums]
    miss = [n for n in t_nums if n not in f_nums]
    if t_nums:
        if not miss:
            items.append({"ok": True, "msg": "数字 " + "、".join(hit) + " 全在"})
        else:
            ok = False
            later = []
            for n in miss:
                where = [i for i, s in enumerate(segs, 1)
                         if n in _nums(s.get("sub") or s.get("tts") or "")]
                later.append(f"{n}（只在第 {where[0]} 段）" if where else n)
            items.append({"ok": False,
                          "msg": "标题里的数字没进首句：" + "、".join(later)})

    # 4. 照念判定
    ratio = (_lcs(tsq, fsq) / len(tsq)) if tsq else 0.0
    contig = bool(fsq) and fsq in tsq
    gaps = _gaps(title, first)

    if ratio >= min_ratio:
        if gaps:
            items.append({"ok": True, "warn": True,
                          "msg": f"基本是照念（重合 {ratio * 100:.0f}%），改了几处："
                                 + "、".join(gaps) + "——能用，但能不改就别改"})
        else:
            items.append({"ok": True,
                          "msg": f"照念标题（字面重合 {ratio * 100:.0f}%）"})
    elif contig and len(fsq) >= 0.55 * len(tsq):
        start = _slice_at(title, fsq)
        items.append({"ok": True, "warn": True,
                      "msg": (f"从「{start}」起照念（重合 {ratio * 100:.0f}%），"
                              f"标题句头没念：" + ("、".join(gaps) or "—"))})
    else:
        ok = False
        items.append({"ok": False,
                      "msg": (f"这不是标题，是自己重写的一句（重合 {ratio * 100:.0f}%）"
                              + (f"；标题里这段没念：{'、'.join(gaps)}" if gaps else ""))})

    # 5. 长度：念完几秒（第 1 段允许长，但超过 10 秒就吃掉了整条片子）
    sec = len(fsq) / CPS
    if sec > 10.5:
        items.append({"ok": True, "warn": True,
                      "msg": f"首句 {len(fsq)} 字，念完约 {sec:.1f} 秒——标题该收了："
                             f"删套话句头，或压到 40 字内"})
    else:
        items.append({"ok": True, "msg": f"首句 {len(fsq)} 字，约 {sec:.1f} 秒"})

    return {"ok": ok, "title": title, "first": first_plain, "items": items,
            "ratio": ratio, "missing": miss, "numbers": t_nums, "hit": hit,
            "name": hit_name, "gaps": gaps, "contig": contig}


def render(res: dict, indent: str = "      ") -> list[str]:
    lines: list[str] = []
    for it in res["items"]:
        if it.get("warn"):
            mark = "!"
        else:
            mark = "✓" if it["ok"] else "✗"
        lines.append(f"{indent}{mark} {it['msg']}")
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(prog="hookcheck.py",
                                 description="检查首句有没有照着发布标题念")
    ap.add_argument("project")
    ap.add_argument("--title", help="直接给标题（默认读 project.json 的 title）")
    ap.add_argument("--quiet", action="store_true", help="只给结论，不列细项")
    a = ap.parse_args()
    try:
        proj = json.loads(Path(a.project).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[x] 读不了 {a.project}：{exc}", file=sys.stderr)
        raise SystemExit(1)
    res = check(proj, a.title)
    if not a.quiet:
        print(f"标题  {res['title'] or '(空)'}")
        print(f"首句  {res['first'] or '(空)'}")
        for line in render(res):
            print(line)
    print(("[+] 首句就是标题：过" if res["ok"] else "[!] 首句不是标题：把标题原文抄进第 1 段"),
          flush=True)
    raise SystemExit(0 if res["ok"] else 2)


if __name__ == "__main__":
    main()
