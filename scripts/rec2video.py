#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rec2video — 一段录屏 → 一条能发布的介绍片。

一条命令串起整条流水线：体检录屏 → 按文案配音 → 按旁白时长反剪画面 →
点击推镜与音效 → 中文字幕 → BGM 闪避混音 → 多尺寸封面。

    python scripts/rec2video.py new   ~/Videos/demo.mp4 --repo https://github.com/you/proj
    python scripts/rec2video.py build demo/project.json

子命令
    doctor   体检环境（ffmpeg / 配音 key / 音乐站 / 可选的 playwright）
    new      建工程：探测录屏、抽带时间戳联系表、写 project.json 预设
    script   把 txt 文案（一行一段）灌进 project.json
    info     看录屏参数 + 抽帧
    tts      配音 + 时间轴
    subs     只重出字幕（ass 烧录版 + srt 外挂版）
    plan     按配音时长生成取材骨架 plan.json
    cut      按 plan.json 反剪画面，并把推镜点换算进 project.json
    bgm      从音乐站找一段 BGM 并下载、写进工程
    build    配音 →（可选）反剪 →（可选）配乐 → 渲染成片
    check    交付前自检
    cover    抓仓库信息，出 16:9 + 4:3 封面

零第三方依赖，只要求 PATH 里有 ffmpeg / ffprobe。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = sys.executable

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- 小工具

def die(msg: str) -> None:
    print(f"[x] {msg}", file=sys.stderr)
    raise SystemExit(1)


def say(msg: str) -> None:
    print(msg, flush=True)


def tool(script: str, *args, capture: bool = False, py: str | None = None) -> str:
    """调同目录下的子脚本：capture=True 时返回 stdout，否则把输出直接透给终端。"""
    cmd = [py or PY, str(HERE / script), *[str(a) for a in args]]
    if capture:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode != 0:
            die(f"{script} 失败\n{(p.stderr or p.stdout)[-1500:]}")
        return p.stdout
    p = subprocess.run(cmd)
    if p.returncode != 0:
        die(f"{script} 失败（见上方报错）")
    return ""


def probe(path: Path) -> dict:
    p = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        die(f"ffprobe 读不了 {path}（ffmpeg 装了吗？）\n{p.stderr[-400:]}")
    d = json.loads(p.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    if not v:
        die(f"{path} 里没有视频流")
    num, den = (v.get("r_frame_rate") or "30/1").split("/")
    return {"w": int(v["width"]), "h": int(v["height"]),
            "fps": round(float(num) / float(den or 1), 3),
            "duration": round(float(d["format"].get("duration", 0)), 3),
            "codec": v.get("codec_name"), "size": int(d["format"].get("size") or 0)}


def load(p: Path) -> dict:
    if not p.is_file():
        die(f"找不到 {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def save(p: Path, d: dict) -> None:
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def auto_crop(info: dict) -> str:
    """整幅要是 16:9 就不用裁；否则给一个居中 16:9 的裁切区。"""
    w, h = info["w"], info["h"]
    if abs(w / h - 16 / 9) < 0.006:
        return ""
    if w * 9 / 16 <= h:
        ch = round(w * 9 / 16)
        return f"{w}:{ch}:0:{(h - ch) // 2}"
    cw = round(h * 16 / 9)
    return f"{cw}:{h}:{(w - cw) // 2}:0"


def gh_meta(url: str) -> dict | None:
    m = re.search(r"github\.com[/:]([\w.-]+)/([\w.-]+)", url or "")
    if not m:
        return None
    owner, repo = m.group(1), m.group(2).removesuffix(".git")
    req = urllib.request.Request(f"https://api.github.com/repos/{owner}/{repo}",
                                 headers={"User-Agent": "rec2video/1.0",
                                          "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        say(f"[!] 抓仓库信息失败（{exc}），封面/片尾标版自己填")
        return None
    lic = (d.get("license") or {}).get("spdx_id") or ""
    return {"owner": owner, "repo": d.get("name") or repo, "url": f"github.com/{owner}/{repo}",
            "stars": d.get("stargazers_count") or 0, "desc": d.get("description") or "",
            "lang": d.get("language") or "", "license": lic,
            "forks": d.get("forks_count") or 0}


def parse_script(path: Path) -> list[dict]:
    """一行一段。`[轻笑]` 这类方括号保留给 TTS，字幕里去掉。"""
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if not lines:
        die(f"{path} 里没有可用文案（一行一段）")
    return [{"tts": ln, "sub": re.sub(r"\[[^\]]+\]", "", ln).strip()} for ln in lines]


# ---------------------------------------------------------------- doctor

def cmd_doctor(a) -> None:
    ok = True
    say(f"python   {sys.version.split()[0]}  ({sys.executable})")
    for t in ("ffmpeg", "ffprobe"):
        got = shutil.which(t)
        if not got:
            say(f"{t:8s} ✗ 不在 PATH")
            ok = False
            continue
        line = subprocess.run([t, "-version"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace").stdout.splitlines()
        say(f"{t:8s} ✓ {got}\n         {line[0][:90] if line else ''}")

    key, key_src = os.environ.get("LATENT_AUDIO_KEY"), "环境变量"
    if not key:
        for f in (ROOT / "keys.env", Path.home() / ".config" / "latent" / "keys.env",
                  Path.home() / ".workbuddy" / "skills" / "latent-media" / "keys.env"):
            if f.is_file():
                for ln in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if ln.strip().startswith("LATENT_AUDIO_KEY"):
                        key, key_src = ln.split("=", 1)[1].strip().strip('"').strip("'"), str(f)
                        break
            if key:
                break
    if key:
        say(f"配音key  ✓ {key[:8]}…{key[-4:]}  ({key_src})")
    else:
        say("配音key  ✗ 没找到 —— 见 https://github.com/guohuiyuan/latent-media")
        ok = False

    out = tool("bgm.py", "search", "lofi", "--json", capture=True)
    try:
        n = len(json.loads(out).get("rows", []))
        say(f"音乐站    ✓ 返回 {n} 条候选（{os.environ.get('MUSIC_BASE') or 'go-music-dl 默认实例'}）")
    except Exception:  # noqa: BLE001
        say("音乐站    ✗ 连不上 —— BGM 可以留空，或用 MUSIC_BASE / --base 指自己的 go-music-dl")
    try:
        import playwright  # noqa: F401
        say("封面      ✓ playwright 已装")
    except Exception:  # noqa: BLE001
        alt = python_for_cover()
        if alt:
            say(f"封面      ✓ 当前解释器没装，但会用 {alt} 出封面")
        else:
            say("封面      – 没装 playwright（只有出封面需要：pip install playwright）")
    say("\n环境" + ("就绪，可以开录了。" if ok else "有缺口，先补齐再往下。"))


# ---------------------------------------------------------------- new

FRAME = {"w": 1560, "h": 878, "radius": 20, "bg": ["0f1324", "2b3358"],
         "ramp_in": 0.42, "hold": 1.15, "ramp_out": 0.55}


def cmd_new(a) -> None:
    src = Path(a.src).expanduser().resolve()
    if not src.is_file():
        die(f"找不到录屏 {src}")
    info = probe(src)
    name = a.name or re.sub(r"[^\w\u4e00-\u9fff-]+", "-", src.stem)[:40] or "video"
    out_dir = Path(a.dir).expanduser().resolve() if a.dir else Path.cwd() / name
    pj = out_dir / "project.json"
    if pj.exists() and not a.force:
        die(f"{pj} 已存在（改参数重来加 --force）")
    out_dir.mkdir(parents=True, exist_ok=True)

    say(f"[i] {src.name}  {info['w']}x{info['h']}  {info['fps']:g}fps  "
        f"{info['duration']:.1f}s  {info['size'] / 1048576:.1f}MB")
    step = 15 if info["duration"] <= 600 else 30
    tool("screencut.py", "scan", src, "-o", out_dir / "scan", "--step", step)

    crop = auto_crop(info)
    W, H = 1920, 1080
    fw, fh = FRAME["w"], FRAME["h"]
    proj = {
        "name": name,
        "repo": a.repo or "",
        "mode": "short" if info["duration"] <= 150 else "long",
        "video": {"src": str(src), "trim_head": 0, "crop": crop,
                  "stretch_to_voice": True,
                  "frame": {**FRAME, "x": (W - fw) // 2, "y": 56}},
        "voice": {"provider": "latent", "voice": a.voice, "gap": 0.32, "gain_db": 6},
        "segments": [],
        "zooms": [],
        "cards": [],
        "caption": {"start": "", "end": "", "start_at": 0.3, "start_until": 4.4,
                    "end_at": 0},
        "subtitle": {"font": "Microsoft YaHei", "size": 46, "margin_v": 46},
        "bgm": {"src": "", "volume": 0.26, "duck": True},
        "output": {"w": W, "h": H, "fps": 30, "crf": 19, "supersample": 2},
    }
    meta = gh_meta(a.repo) if a.repo else None
    if meta:
        star = f"{meta['stars'] / 1000:.1f}k" if meta["stars"] >= 1000 else str(meta["stars"])
        proj["caption"]["end"] = f"{meta['url']} ｜ ★ {star}"
        proj["caption"]["start"] = f"{meta['repo']} ｜ {(meta['desc'] or '')[:34]}"
        proj["cover"] = {"repo_url": a.repo}
        say(f"[i] 仓库 {meta['url']}  ★{meta['stars']}  {meta['lang']}  {meta['license']}")

    if a.script:
        proj["segments"] = parse_script(Path(a.script).expanduser())
        say(f"[i] 文案 {len(proj['segments'])} 段（来自 {a.script}）")
    save(pj, proj)

    say(f"\n[+] {pj}")
    say(f"    crop={crop or '(整幅)'}  画框 {fw}x{fh}@{(W - fw) // 2},56  "
        f"配音 {a.voice}  模式 {proj['mode']}")
    say(f"    联系表 {out_dir / 'scan'}（每格烧了原片绝对时间，先看它）")
    if not proj["segments"]:
        say("\n下一步：写文案（一行一段，8–12 行 / 60–80 秒），然后")
        say(f"    python scripts/rec2video.py script {pj} narration.txt")
    say(f"    python scripts/rec2video.py build  {pj}")


# ---------------------------------------------------------------- 各项 stage

def cmd_script(a) -> None:
    pj = Path(a.project).resolve()
    proj = load(pj)
    proj["segments"] = parse_script(Path(a.txt).expanduser())
    save(pj, proj)
    say(f"[+] {len(proj['segments'])} 段已写入 {pj.name}")
    for i, s in enumerate(proj["segments"], 1):
        say(f"    {i:>2}. {s['sub'][:38]}")


def cmd_info(a) -> None:
    tool("render.py", "probe", a.project, "--every", a.every)


def cmd_tts(a) -> None:
    args = ["tts", a.project] + (["--force"] if a.force else [])
    tool("render.py", *args)


def cmd_subs(a) -> None:
    """只重出字幕（改字号/位置/文案后立刻看效果，不用等渲染）。"""
    pj = Path(a.project).resolve()
    tool("render.py", "subs", pj)
    proj = load(pj)
    segs = proj.get("segments") or []
    sub = proj.get("subtitle") or {}
    say(f"[i] 字幕 {len(segs)} 条  {sub.get('font', 'Microsoft YaHei')} {sub.get('size', 46)}px"
        f"  margin_v={sub.get('margin_v', 46)}")
    say(f"    {pj.parent / 'work' / 'subs.ass'}   （烧进画面）")
    say(f"    {pj.parent / 'work' / 'subs.srt'}   （外挂投稿）")


def cmd_plan(a) -> None:
    pj = Path(a.project).resolve()
    proj = load(pj)
    if not (pj.parent / "work" / "timeline.json").is_file():
        die("还没有配音时间轴，先跑 tts（先配音、再剪画面，顺序不能反）")
    args = ["plan", "-p", pj] + (["--crop", a.crop] if a.crop else [])
    if a.force:
        args.append("--force")
    tool("screencut.py", *args)
    # 顺手把「取材上限」放宽一点：默认 max 只比旁白多 1.5s，看不出可呼吸空间
    say(f"\n下一步：填 {pj.parent / 'plan.json'} 里每段的 at（这段内容在原录屏第几秒）"
        f"，以及 shots（推镜点）")


def _all_at_filled(plan: dict) -> bool:
    return all(s.get("at") is not None for s in plan.get("segments", []))


def cmd_cut(a) -> None:
    pj = Path(a.project).resolve()
    base = pj.parent
    plan_p = base / "plan.json"
    plan = load(plan_p)
    if not _all_at_filled(plan):
        die("plan.json 里还有段没填 at —— 先看 scan/ 的联系表把取材点填上")
    tool("screencut.py", "cut", plan_p, "-p", pj, *(["--force"] if a.force else []))
    if plan.get("shots"):
        tool("screencut.py", "zooms", plan_p, "-p", pj)


def _timeline_total(base: Path) -> float:
    tl = base / "work" / "timeline.json"
    if tl.is_file():
        try:
            return float(json.loads(tl.read_text(encoding="utf-8"))["total"])
        except Exception:  # noqa: BLE001
            pass
    return 0.0


def cmd_bgm(a) -> None:
    pj = Path(a.project).resolve()
    base = pj.parent
    proj = load(pj)
    if a.off:
        proj["bgm"]["src"] = ""
        save(pj, proj)
        say("[+] BGM 已关掉（成片照样有旁白/字幕/音效）")
        return
    want = _timeline_total(base) or 80.0
    if a.pick:
        tool("bgm.py", "get", a.pick, base / "assets" / "bgm.mp3")
    else:
        keys = a.q or ["upbeat technology", "lofi study", "chill background"]
        out = tool("bgm.py", "search", *keys, "--json", capture=True)
        rows = json.loads(out).get("rows", [])
        rows = [r for r in rows if r.get("dur", 0) >= 45]
        if not rows:
            die("音乐站没搜到合适的，换关键词，或手头有曲子就 --pick 不了、直接把文件放到 assets/bgm.mp3")
        best = max(rows, key=lambda r: r.get("score", 0) + (6 if r["dur"] >= want else 0))
        m, s = divmod(best["dur"], 60)
        say(f"[i] 选中 {best['name']} — {best['artist']}  {m}:{s:02d}  id={best['id']}")
        tool("bgm.py", "get", best["id"], base / "assets" / "bgm.mp3")
    proj["bgm"]["src"] = "assets/bgm.mp3"
    proj["bgm"]["volume"] = a.volume
    save(pj, proj)
    say(f"[+] 写进 {pj.name}：bgm.src=assets/bgm.mp3 volume={a.volume} duck=True")


def cmd_build(a) -> None:
    pj = Path(a.project).resolve()
    base = pj.parent
    proj = load(pj)
    if not proj.get("segments"):
        die(f"{pj.name} 里 segments 是空的 —— 先写文案（rec2video.py script）")

    plan_p = base / "plan.json"
    cut_done = "screen-edit" in str(Path(proj["video"]["src"]).name)
    if plan_p.is_file() and _all_at_filled(load(plan_p)) and not cut_done:
        tool("render.py", "tts", pj, *(["--force"] if a.force else []))
        cmd_cut(argparse.Namespace(project=str(pj), force=a.force))
    elif plan_p.is_file() and not cut_done:
        say("[!] plan.json 里还有段没填 at —— 这次直接用原录屏整段渲染（stretch 对齐）")
    if a.bgm == "auto" and not (proj.get("bgm") or {}).get("src"):
        cmd_bgm(argparse.Namespace(project=str(pj), q=None, pick=None, off=False,
                                   volume=0.26))

    args = ["run", pj] + (["--force"] if a.force else [])
    if a.preview:
        args += ["--preview", str(a.preview)]
    tool("render.py", *args)
    out = base / "out" / f"{proj.get('name') or pj.stem}.mp4"
    if out.is_file():
        info = probe(out)
        say(f"\n[✓] {out}  {info['duration']:.2f}s  {info['size'] / 1048576:.1f}MB")
        say(f"    字幕 work/subs.srt（可外挂投 B 站）")


def cmd_check(a) -> None:
    pj = Path(a.project).resolve()
    proj = load(pj)
    base = pj.parent
    tool("screencut.py", "check", pj)
    out = base / "out" / f"{proj.get('name') or pj.stem}.mp4"
    say("")
    if out.is_file():
        info = probe(out)
        say(f"成片  {out.name}  {info['duration']:.2f}s  {info['w']}x{info['h']}  "
            f"{info['size'] / 1048576:.1f}MB")
    else:
        say("成片  还没渲染")
    caps = proj.get("caption") or {}
    say(f"标版  片头「{caps.get('start', '') or '(空)'}」／片尾「{caps.get('end', '') or '(空)'}」")
    say("      数字口径：封面、标题、简介、口播里的 star 数/功能数必须一致")


def python_for_cover() -> str | None:
    """封面要 playwright。当前解释器没装就找机器上装了的那个，别让用户自己猜。"""
    def ok(p: str) -> bool:
        try:
            return subprocess.run([p, "-c", "import playwright"], capture_output=True,
                                  timeout=25).returncode == 0
        except Exception:  # noqa: BLE001
            return False
    if ok(PY):
        return PY
    home = Path.home()
    for c in (os.environ.get("REC2VIDEO_PY"), home / "miniconda3" / "python.exe",
              home / "anaconda3" / "python.exe", "/usr/bin/python3"):
        if c and Path(str(c)).is_file() and ok(str(c)):
            return str(c)
    return None


def cmd_cover(a) -> None:
    pj = Path(a.project).resolve()
    base = pj.parent
    proj = load(pj)
    repo = a.repo or proj.get("repo") or (proj.get("cover") or {}).get("repo_url")
    if not repo:
        die("没给仓库地址：--repo https://github.com/owner/repo")
    outdir = base / "cover"
    tool("cover.py", "spec", "--repo", repo, "-o", outdir, "-s", a.sizes)
    specs = sorted(outdir.glob("*.json"))
    if not specs:
        die(f"没生成 spec.json，看 {outdir}")
    py = python_for_cover()
    if not py:
        say(f"\n[!] spec 已生成：{specs[0]}\n"
            f"    渲染要 playwright：pip install playwright  （用本机 Edge 当内核，"
            f"不用再装 chromium）\n"
            f"    装好后重跑：python scripts/cover.py render {specs[0]} -o {outdir} -s {a.sizes}")
        return
    tool("cover.py", "render", specs[0], "-o", outdir, "-s", a.sizes, py=py)
    say(f"\n[+] 封面在 {outdir}（改 spec.json 里的文案再 render 一次即可）")
    say("    标题/简介套句式和封面数字保持一致，别抄参考视频的 star 数")


# ---------------------------------------------------------------- CLI

def main() -> None:
    ap = argparse.ArgumentParser(
        prog="rec2video",
        description="一段录屏 → 一条能发布的介绍片（配音/字幕/推镜/音效/BGM/封面）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="典型流程：new → script → build → check → cover")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="体检环境").set_defaults(func=cmd_doctor)

    n = sub.add_parser("new", help="建工程（探测录屏 + 联系表 + project.json）")
    n.add_argument("src", help="录屏文件（OBS / Recordly / 系统录屏都行）")
    n.add_argument("-d", "--dir", help="工程目录（默认 ./<录屏名>）")
    n.add_argument("--name", help="工程名，也是成片文件名")
    n.add_argument("--repo", help="GitHub 仓库地址（填片尾标版 + 封面）")
    n.add_argument("--voice", default="voice-120", help="音色 slug，默认 voice-120（大肥鱼）")
    n.add_argument("--script", help="文案 txt（一行一段），建工程时一起灌进去")
    n.add_argument("--force", action="store_true")
    n.set_defaults(func=cmd_new)

    s = sub.add_parser("script", help="把 txt 文案灌进 project.json")
    s.add_argument("project")
    s.add_argument("txt")
    s.set_defaults(func=cmd_script)

    i = sub.add_parser("info", help="录屏参数 + 抽帧")
    i.add_argument("project")
    i.add_argument("--every", type=float, default=5.0)
    i.set_defaults(func=cmd_info)

    t = sub.add_parser("tts", help="配音 + 时间轴")
    t.add_argument("project")
    t.add_argument("--force", action="store_true")
    t.set_defaults(func=cmd_tts)

    sb = sub.add_parser("subs", help="只重出字幕（ass + srt）")
    sb.add_argument("project")
    sb.set_defaults(func=cmd_subs)

    p = sub.add_parser("plan", help="生成 plan.json 骨架")
    p.add_argument("project")
    p.add_argument("--crop")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_plan)

    c = sub.add_parser("cut", help="按 plan.json 反剪 + 换算推镜")
    c.add_argument("project")
    c.add_argument("--force", action="store_true")
    c.set_defaults(func=cmd_cut)

    b = sub.add_parser("bgm", help="找一段 BGM 并用上")
    b.add_argument("project")
    b.add_argument("-q", "--q", nargs="+", help="搜索关键词")
    b.add_argument("--pick", help="直接指定音乐站 id")
    b.add_argument("--off", action="store_true", help="关掉 BGM")
    b.add_argument("--volume", type=float, default=0.26)
    b.set_defaults(func=cmd_bgm)

    d = sub.add_parser("build", help="配音 → 反剪 → 配乐 → 渲染")
    d.add_argument("project")
    d.add_argument("--preview", type=float, help="只渲前 N 秒")
    d.add_argument("--force", action="store_true", help="重做配音/重切")
    d.add_argument("--bgm", choices=["auto", "off"], default="auto")
    d.set_defaults(func=cmd_build)

    k = sub.add_parser("check", help="交付前自检")
    k.add_argument("project")
    k.set_defaults(func=cmd_check)

    v = sub.add_parser("cover", help="出封面（16:9 + 4:3）")
    v.add_argument("project")
    v.add_argument("--repo")
    v.add_argument("--sizes", default="16x9,4x3")
    v.set_defaults(func=cmd_cover)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
