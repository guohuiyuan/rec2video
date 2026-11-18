#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开场自检：第一句到底有没有复述**发布标题**。

    python hookcheck.py <project.json> [--title "好家伙！Github又出狠活神器！..."]

不联网、不读视频，只看 `project.json` 里的 `title` 与 `segments[0]`：

  1. `title` 得存在——标题没落盘，就没有"复述"这回事（最常见的失败）
  2. 项目名必须出现在首句
  3. 标题里有数字，首句至少要落一个
  4. 字面重合（3-gram 覆盖率）≥ 20%，低于这条基本是另起一句了（35% 以下只提示）

第 4 条是软指标，只影响提示；前三条不过就是 ✗。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 标题里"抓眼球"的套话/情绪词：复述时允许丢掉，不计入字面重合
HYPE = ("好家伙", "狠活", "神器", "王炸", "炸裂", "天花板", "喜讯", "传来", "斩获",
        "最高的山", "又出", "来了", "github", "stars", "支持", "还能", "自己写",
        "太强了", "绝了", "无敌")

NUM = re.compile(r"\d+(?:\.\d+)?\s*[kKwW万]?")
MARK = "\x00"


def _plain(s: str) -> str:
    """去掉 [笑] 这类只给合成器的气声标记。"""
    return re.sub(r"\[[^\]]{1,8}\]", "", s or "")


def _squash(s: str) -> str:
    """只留字、数字、字母，全小写——比较用。"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", _plain(s).lower())


def _drop_hype(s: str) -> str:
    for h in HYPE:
        s = s.replace(h, MARK)
    return s


def _nums(s: str) -> list[str]:
    out: list[str] = []
    for m in NUM.finditer(_plain(s)):
        v = m.group(0).replace(" ", "").lower()
        if v not in out:
            out.append(v)
    return out


def _grams(s: str) -> set[str]:
    """3-gram，套话部分先挖掉。"""
    grams: set[str] = set()
    for part in _drop_hype(_squash(s)).split(MARK):
        for i in range(len(part) - 2):
            grams.add(part[i:i + 3])
    return grams


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


def check(proj: dict, title: str | None = None,
          min_ratio: float = 0.35) -> dict:
    title = (title if title is not None else proj.get("title") or "").strip()
    segs = proj.get("segments") or []
    first = ""
    if segs:
        first = (segs[0].get("sub") or segs[0].get("tts") or "").strip()
    first_plain = _plain(first)
    fsq = _squash(first)

    items: list[dict] = []
    ok = True

    if not title:
        ok = False
        items.append({"ok": False, "msg": "project.json 里没有 title（发布标题原文），首句没法校验"})
    if not first:
        ok = False
        items.append({"ok": False, "msg": "还没有第一段文案"})
    if not title or not first:
        return {"ok": ok, "title": title, "first": first_plain, "items": items,
                "ratio": 0.0, "missing": [], "numbers": [], "hit": []}

    # 2. 项目名
    names = _names(proj)
    hit_name = next((n for n in names if n and n in fsq), "")
    if names:
        if hit_name:
            items.append({"ok": True, "msg": f"项目名 {hit_name}"})
        else:
            ok = False
            items.append({"ok": False,
                          "msg": f"项目名没出现（标题写的是 {names[0]}）"})

    # 3. 数字：标题里有数字，首句至少要落一个（全落更好，落不全只提示）
    t_nums = _nums(title)
    f_nums = _nums(first_plain)
    hit = [n for n in t_nums if n in f_nums]
    miss = [n for n in t_nums if n not in f_nums]
    if t_nums:
        if hit:
            items.append({"ok": True, "msg": "数字 " + "、".join(hit)})
            if miss:
                items.append({"ok": True, "warn": True,
                              "msg": "这几个没提（留给中段/片尾也行）：" + "、".join(miss)})
        else:
            ok = False
            later = []
            for n in miss:
                where = [i for i, s in enumerate(segs, 1)
                         if n in _nums(s.get("sub") or s.get("tts") or "")]
                if where:
                    later.append(f"{n}（只在第 {where[0]} 段，前三秒没有）")
                else:
                    later.append(n)
            items.append({"ok": False,
                          "msg": "标题里的数字没进首句：" + "、".join(later)})

    # 4. 字面重合：太低就是另起一句了
    gt, gf = _grams(title), _grams(first_plain)
    ratio = (len(gt & gf) / len(gt)) if gt else 0.0
    if ratio < 0.20:
        ok = False
        items.append({"ok": False,
                      "msg": f"与标题的字面重合只有 {ratio * 100:.0f}%，基本是另起一句"})
    elif ratio < min_ratio:
        items.append({"ok": True, "warn": True,
                      "msg": f"与标题的字面重合 {ratio * 100:.0f}%（低于 {min_ratio * 100:.0f}%，勉强）"})
    else:
        items.append({"ok": True,
                      "msg": f"与标题的字面重合 {ratio * 100:.0f}%"})

    return {"ok": ok, "title": title, "first": first_plain, "items": items,
            "ratio": ratio, "missing": miss, "numbers": t_nums, "hit": hit,
            "name": hit_name}


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
    ap = argparse.ArgumentParser(prog="hookcheck.py", description="检查首句有没有复述发布标题")
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
    print(("[+] 首句复述标题：过" if res["ok"] else "[!] 首句没复述标题：重写第 1 段"),
          flush=True)
    raise SystemExit(0 if res["ok"] else 2)


if __name__ == "__main__":
    main()
