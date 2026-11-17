#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rec2video · cover — 用 HTML 模板 + headless 浏览器生成多尺寸封面图。

依赖可选：pip install playwright && playwright install chromium（或复用本机 Edge）。

子命令：
  spec   从 GitHub 仓库抓信息，生成 spec.json 草稿
  render 按 spec 渲染 PNG（可一次出多种比例）
  sizes  列出内置尺寸预设

设计要点：
  1. 模板占位符是 {{key}}（双花括号），用正则替换 —— CSS 的 {} 完全不用转义。
  2. 所有尺寸在模板里写成 calc(var(--u) * N)，N 为 1920x1080 基准下的像素值。
     --u = min(100vw/1920, 100vh/1080) * k，k 由宽高比自动补偿，因此同一套
     版式换任何比例都等比成立，不需要为每个尺寸写一套参数表。
  3. 图片一律转成绝对 file:// URI，HTML 放临时目录也不会掉图。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TPL_DIR = ROOT / "assets" / "templates"

# 尺寸预设：(宽, 高, 用途说明)
SIZES = {
    "16x9": (1920, 1080, "B站/YouTube 封面主图"),
    "4x3": (1440, 1080, "B站动态、图文、老平台 4:3"),
    "16x10": (1920, 1200, "部分平台信息流"),
    "1x1": (1080, 1080, "方形：小图、头像位、微博"),
    "9x16": (1080, 1920, "竖版：抖音/快手/Reels"),
    "yt": (1280, 720, "YouTube 最低要求"),
}

# nav 用的内置线性图标（24x24 stroke）
ICONS = {
    "book": '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H10a2 2 0 0 1 2 2v13a2 2 0 0 0-2-2H4z"/><path d="M20 5.5A1.5 1.5 0 0 0 18.5 4H14a2 2 0 0 0-2 2v13a2 2 0 0 1 2-2h6z"/>',
    "branch": '<circle cx="6" cy="6" r="2.4"/><circle cx="6" cy="18" r="2.4"/><circle cx="18" cy="18" r="2.4"/><path d="M6 8.4v7.2M8.4 6h5.1a2.4 2.4 0 0 1 2.4 2.4v7.2"/>',
    "shield": '<path d="M12 3l7 3v6c0 4.2-2.9 7.4-7 9-4.1-1.6-7-4.8-7-9V6z"/><path d="M9 12l2 2 4-4"/>',
    "clock": '<circle cx="12" cy="12" r="8.2"/><path d="M12 7.6V12l3.2 2"/>',
    "lock": '<rect x="4.5" y="10" width="15" height="10" rx="2.4"/><path d="M8 10V7.6a4 4 0 0 1 8 0V10"/>',
    "star": '<path d="M12 3.6l2.6 5.3 5.8.8-4.2 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8L3.6 9.7l5.8-.8z"/>',
    "fork": '<circle cx="12" cy="5.5" r="2.2"/><circle cx="6" cy="18.5" r="2.2"/><circle cx="18" cy="18.5" r="2.2"/><path d="M12 7.7v3.8M6 16.3v-2.6a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v2.6"/>',
    "code": '<path d="M9 7l-5 5 5 5M15 7l5 5-5 5"/>',
    "download": '<path d="M12 4v11m0 0l4-4m-4 4l-4-4"/><path d="M5 19h14"/>',
    "globe": '<circle cx="12" cy="12" r="8.4"/><path d="M3.6 12h16.8M12 3.6c4.4 4.6 4.4 12.2 0 16.8-4.4-4.6-4.4-12.2 0-16.8z"/>',
    "bolt": '<path d="M13.5 3L6 13.5h5l-1 7.5L18 10.5h-5z"/>',
    "dot": '<circle cx="12" cy="12" r="4"/>',
}

BADGE_STYLES = {
    "line": "p-line",   # 描边（次要信息）
    "dark": "p-dark",   # 深色底（语言徽章，可带彩色点）
    "mit": "p-mit",     # 紫（许可证）
    "blue": "p-blue",   # 蓝（数量/特性）
    "warn": "p-dry",    # 黄（状态）
    "red": "p-red",     # 红（强调/警示）
}


# ---------------------------------------------------------------- 工具函数
def ratio_k(w: int, h: int, base=(1920, 1080)) -> float:
    """宽高比补偿系数：比例越方，字号相对放大，防止内容缩成一条。"""
    r = min(w / base[0], h / base[1])
    if r <= 0:
        return 1.0
    return round(r ** -0.35, 4)


def file_uri(p: str | Path) -> str:
    p = Path(p).resolve()
    return p.as_uri()


def nav_html(items) -> str:
    out = []
    for it in items or []:
        if isinstance(it, str):
            it = {"text": it}
        cls = "item on" if it.get("on") else "item"
        icon = it.get("icon")
        svg = f'<svg viewBox="0 0 24 24">{ICONS.get(icon, ICONS["dot"])}</svg>' if icon else ""
        out.append(f'<div class="{cls}">{svg}<span>{it.get("text", "")}</span></div>')
    return "\n  ".join(out)


def badges_html(items) -> str:
    out = []
    for b in items or []:
        if isinstance(b, str):
            b = {"text": b}
        cls = BADGE_STYLES.get(b.get("style", "line"), "p-line")
        dot = f'<i style="background:{b["dot"]}"></i>' if b.get("dot") else ""
        out.append(f'<span class="pill {cls}">{dot}{b.get("text", "")}</span>')
    return "\n  ".join(out)


def fill(tpl: str, vars_: dict) -> str:
    """{{key}} 占位符替换；未提供的键原样保留，方便发现拼写错误。"""
    def rep(m):
        k = m.group(1)
        return str(vars_[k]) if k in vars_ else m.group(0)
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", rep, tpl)


def load_spec(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    spec.setdefault("template", "repo-page")
    spec.setdefault("theme", "light")
    spec.setdefault("sizes", ["16x9"])
    spec["_dir"] = str(path.resolve().parent)
    return spec


def resolve_asset(spec: dict, key: str):
    """spec 里的图片路径按 spec 文件所在目录解析。"""
    v = spec.get(key)
    if not v:
        return None
    p = Path(v)
    if not p.is_absolute():
        p = Path(spec["_dir"]) / p
    if not p.exists():
        raise SystemExit(f"[!] {key} 指向的文件不存在: {p}")
    return file_uri(p)


# ---------------------------------------------------------------- 渲染
def build_html(spec: dict, w: int, h: int, tpl_text: str) -> str:
    k = spec.get("k", ratio_k(w, h))
    if isinstance(k, dict):
        raise SystemExit("[!] spec 里的 k 必须是数字；分尺寸微调请用 k_by_size")
    kbs = spec.get("k_by_size") or {}
    for name, val in kbs.items():
        if SIZES.get(name, (None, None))[:2] == (w, h):
            k = val
    u = round(min(w / 1920, h / 1080) * k, 4)
    vars_ = {
        "W": w, "H": h, "k": k, "u": u, "px": round(u, 4),
        "theme": spec.get("theme", "light"),
        "title_en": spec.get("title_en", ""),
        "title_cn": spec.get("title_cn", ""),
        "kicker": spec.get("kicker", ""),
        "desc": spec.get("desc", ""),
        "nav": nav_html(spec.get("nav")),
        "badges": badges_html(spec.get("badges")),
        "icon": resolve_asset(spec, "icon") or "",
        "icon_px": spec.get("icon_px", 214),
        "shot": resolve_asset(spec, "shot") or "",
        "shot_w": spec.get("shot_w", 1520),
        "shot_display": "" if spec.get("shot") else "display:none",
        "footer": spec.get("footer", ""),
        "bg_image": resolve_asset(spec, "bg_image") or "",
        "kicker_display": "" if spec.get("kicker") else "display:none",
        "veil_display": "" if spec.get("bg_image") else "display:none",
    }
    return fill(tpl_text, vars_)


CHECK_JS = """() => {
  const de = document.documentElement;
  const over = [];
  document.querySelectorAll('[data-critical]').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.right > innerWidth + 1 || r.left < -1 || r.bottom > innerHeight + 1 || r.top < -1) {
      over.push({cls: el.className || el.tagName,
                 box: [r.left, r.top, r.right, r.bottom].map(v => Math.round(v))});
    }
  });
  return {sw: de.scrollWidth, sh: de.scrollHeight, over,
          fonts: document.fonts ? document.fonts.status : 'n/a'};
}"""


def launch(pw, channel: str | None):
    tries = [channel] if channel else ["msedge", "chrome", None]
    err = None
    for ch in tries:
        try:
            return pw.chromium.launch(channel=ch) if ch else pw.chromium.launch()
        except Exception as e:  # noqa: BLE001
            err = e
    raise SystemExit(f"[!] 找不到可用浏览器（试过 msedge / chrome / 内置 chromium）: {err}")


def render(spec_path: Path, outdir: Path, only: list[str] | None, scale: float, channel, quiet=False):
    from playwright.sync_api import sync_playwright  # 延迟导入，spec/sizes 子命令不需要它

    spec_path = Path(spec_path).resolve()
    outdir = Path(outdir).resolve()
    spec = load_spec(spec_path)
    tpl_path = TPL_DIR / f"{spec['template']}.html"
    if not tpl_path.exists():
        names = ", ".join(p.stem for p in sorted(TPL_DIR.glob("*.html")))
        raise SystemExit(f"[!] 模板不存在: {tpl_path.name}；可选: {names}")
    tpl_text = tpl_path.read_text(encoding="utf-8")

    sizes = only or spec["sizes"]
    unknown = [s for s in sizes if s not in SIZES]
    if unknown:
        raise SystemExit(f"[!] 未知尺寸 {unknown}；可选: {', '.join(SIZES)}")

    outdir.mkdir(parents=True, exist_ok=True)
    build_dir = outdir / ".build"
    build_dir.mkdir(exist_ok=True)
    results = []

    with sync_playwright() as pw:
        browser = launch(pw, channel)
        for s in sizes:
            w, h, _ = SIZES[s]
            html = build_html(spec, w, h, tpl_text)
            hp = build_dir / f"{spec_path.stem}-{s}.html"
            hp.write_text(html, encoding="utf-8")
            page = browser.new_page(viewport={"width": w, "height": h},
                                    device_scale_factor=scale)
            page.goto(hp.as_uri())
            page.wait_for_load_state("load")
            page.evaluate("async () => { if (document.fonts) await document.fonts.ready; }")
            page.wait_for_timeout(spec.get("wait_ms", 350))
            stem = spec.get("output_stem") or spec_path.stem
            png = outdir / f"{stem}-{s}.png"
            page.screenshot(path=str(png), clip={"x": 0, "y": 0, "width": w, "height": h})
            info = page.evaluate(CHECK_JS)
            page.close()
            flags = []
            if info["sh"] > h + 1 or info["sw"] > w + 1:
                flags.append(f"内容溢出画布 {info['sw']}x{info['sh']}")
            if info["over"]:
                flags.append(f"{len(info['over'])} 个元素出界")
            if info["fonts"] != "loaded":
                flags.append(f"字体状态 {info['fonts']}")
            results.append({"size": s, "file": str(png), "w": w, "h": h,
                            "k": round(_k_of(spec, w, h), 3), "warn": flags})
            if not quiet:
                mark = "OK " if not flags else "!! "
                print(f"{mark}{png.name}  {w}x{h}  k={results[-1]['k']}"
                      + (f"  -> {'; '.join(flags)}" if flags else ""))
        browser.close()
    return results


def _k_of(spec, w, h):
    k = spec.get("k", ratio_k(w, h))
    for name, val in (spec.get("k_by_size") or {}).items():
        if SIZES.get(name, (None, None))[:2] == (w, h):
            k = val
    return k


# ---------------------------------------------------------------- spec 抓取
def _get(url: str, raw=False):
    req = urllib.request.Request(url, headers={
        "User-Agent": "cover-forge/1.0",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read()
    return data if raw else json.loads(data.decode("utf-8"))


def clean_desc(t: str, limit: int = 96) -> str:
    """仓库描述直接上封面会带联系方式/群号，按句切并丢掉这类句子。"""
    if not t:
        return ""
    parts = re.split(r"(?<=。)", t)
    keep = [x for x in parts
            if not re.search(r"(交流群|讨论群|QQ|微信|Telegram|群[:：])", x, re.I)]
    return "".join(keep).strip().rstrip("。")[:limit]


def spec_from_repo(url: str, out: Path, sizes: list[str]):
    """从 GitHub 仓库抓 icon / 截图 / 描述 / 徽章，生成 spec 草稿。"""
    m = re.search(r"github\.com/([^/]+)/([^/#?]+)", url)
    if not m:
        raise SystemExit("[!] 需要一个 GitHub 仓库地址，例如 https://github.com/owner/repo")
    owner, repo = m.group(1), m.group(2).removesuffix(".git")
    api = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        meta = _get(api)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"[!] 拉取仓库信息失败: {e}")

    branch = meta.get("default_branch", "main")
    out.mkdir(parents=True, exist_ok=True)

    # 1) README 里第一张图当 icon 候选
    icon_rel, shot_rel = None, None
    try:
        rd = _get(f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md", raw=True).decode("utf-8", "ignore")
        imgs = re.findall(r'<img[^>]+src="([^"]+)"', rd) + re.findall(r'!\[[^\]]*\]\(([^)]+)\)', rd)
        imgs = [i for i in imgs if not i.startswith("http") or "githubusercontent" in i]
        if imgs:
            icon_rel = imgs[0].lstrip("./")
        m2 = re.search(r'!\[[^\]]*\]\((\.?/?screenshots/[^)]+)\)', rd)
        if m2:
            shot_rel = m2.group(1).lstrip("./")
    except Exception:  # noqa: BLE001
        pass

    for label, rel in (("icon", icon_rel), ("shot", shot_rel)):
        if not rel:
            continue
        for cand in (rel, rel.replace(".png", ".jpg")):
            u = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{cand}"
            try:
                data = _get(u, raw=True)
            except Exception:  # noqa: BLE001
                continue
            ext = os.path.splitext(cand)[1] or ".png"
            dst = out / f"{label}{ext}"
            dst.write_bytes(data)
            print(f"[+] {label} -> {dst.name}  ({len(data)//1024} KB)")
            break

    lang = meta.get("language") or ""
    stars = meta.get("stargazers_count", 0)
    lic = (meta.get("license") or {}).get("spdx_id") or ""
    badge = [{"text": f"100% {lang}", "style": "dark", "dot": "#00add8"}] if lang else []
    if lic and lic != "NOASSERTION":
        badge.append({"text": lic, "style": "mit"})
    badge.append({"text": f"{stars} Stars", "style": "blue"})

    spec = {
        "template": "repo-page",
        "theme": "light",
        "sizes": sizes,
        "output_stem": repo,
        "title_en": repo.upper(),
        "title_cn": "一句话写清它是什么",
        "desc": clean_desc(meta.get("description") or "") or "在这里写两行卖点，用 <b> 高亮关键词。",
        "nav": [
            {"icon": "book", "text": "阅读", "on": True},
            {"icon": "branch", "text": "贡献"},
            {"icon": "clock", "text": "更新日志"},
            {"icon": "lock", "text": "安全性"},
        ],
        "badges": badge,
        "icon": "icon.png" if (out / "icon.png").exists() else "",
        "shot": "shot.png" if (out / "shot.png").exists() else "",
    }
    sp = out / "spec.json"
    sp.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[+] spec -> {sp}")
    print(f"    标题、文案、徽章都还是占位，改完再 render。")
    return sp


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(prog="cover-forge", description="HTML 模板 + 浏览器截图，生成多尺寸封面")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("render", help="按 spec.json 渲染 PNG")
    s1.add_argument("spec", type=Path)
    s1.add_argument("-o", "--outdir", type=Path, default=Path("."))
    s1.add_argument("-s", "--sizes", help="逗号分隔，覆盖 spec 里的 sizes，如 16x9,4x3")
    s1.add_argument("--scale", type=float, default=1.0, help="输出倍率，2 = 高清 2x")
    s1.add_argument("--channel", help="msedge / chrome；默认自动探测")
    s1.add_argument("--json", action="store_true", help="机器可读输出")

    s2 = sub.add_parser("spec", help="从 GitHub 仓库生成 spec 草稿")
    s2.add_argument("--repo", required=True, help="https://github.com/owner/repo")
    s2.add_argument("-o", "--outdir", type=Path, default=Path("."))
    s2.add_argument("-s", "--sizes", default="16x9,4x3")

    sub.add_parser("sizes", help="列出内置尺寸")

    a = ap.parse_args()
    if a.cmd == "sizes":
        for k, (w, h, note) in SIZES.items():
            print(f"{k:6s} {w}x{h:<5d} {note}")
        print("\n模板:", ", ".join(p.stem for p in sorted(TPL_DIR.glob("*.html"))))
        return
    if a.cmd == "spec":
        spec_from_repo(a.repo, a.outdir, [s.strip() for s in a.sizes.split(",") if s.strip()])
        return
    sizes = [s.strip() for s in a.sizes.split(",")] if a.sizes else None
    res = render(a.spec, a.outdir, sizes, a.scale, a.channel, quiet=a.json)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    bad = [r for r in res if r["warn"]]
    sys.exit(1 if bad and not a.json else 0)


if __name__ == "__main__":
    main()
