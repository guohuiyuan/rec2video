#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rec2video — 一段录屏 → 一条能发布的介绍片。

一条命令串起整条流水线：体检录屏 → 按文案配音 → 按旁白时长反剪画面 →
点击推镜与音效 → 中文字幕 → BGM 闪避混音 → 多尺寸封面。

    python scripts/rec2video.py new   ~/Videos/demo.mp4 --repo https://github.com/you/proj
    python scripts/rec2video.py build demo/project.json

子命令
    doctor   体检环境（ffmpeg / 配音 CLI / BGM 站 / 封面引擎）
    new      建工程：探测录屏、抽带时间戳联系表、写 project.json 预设
    script   把 txt 文案（一行一段）灌进 project.json，并验首句有没有复述标题
    hook     只验这一条：首句复述发布标题（title / 项目名 / 数字 / 字面重合）
    info     看录屏参数 + 抽帧
    tts      配音 + 时间轴
    subs     只重出字幕（ass 烧录版 + srt 外挂版）
    plan     按配音时长生成取材骨架 plan.json
    cut      按 plan.json 反剪画面，并把推镜点换算进 project.json
    bgm      配 BGM：本地曲子，或 go-music-dl（没有服务就现场拉一个）
    build    配音 →（可选）反剪 →（可选）配乐 → 渲染成片
    check    交付前自检
    cover    抓仓库信息，出 16:9 + 4:3 封面（playwright 截 HTML）

本仓库只管编排与渲染，能力都调外部：
  配音 → latent-media 的 latent.py（不在本仓库里）
  配乐 → go-music-dl（有服务就用服务；没服务就下官方 release 到本地起临时实例），
         或者你自己给的曲子
  封面 → playwright 截本仓库的 HTML 模板
硬依赖只有 PATH 里的 ffmpeg / ffprobe。
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

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import hookcheck  # noqa: E402  同目录的「首句有没有复述标题」校验


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

    # 配音：能力在 latent-media，这里只确认它的 CLI 和 key 状态
    cli = latent_cli()
    if cli:
        say(f"配音CLI  ✓ {cli}")
        p = subprocess.run([PY, cli, "keys"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        try:
            info = json.loads(p.stdout)          # keys 输出的是格式化 JSON
            audio = info.get("audio") or "(未配置)"
            if audio == "(未配置)":
                say("配音key  ✗ 没配 —— `rec2video.py key lat_tk_xxx`")
                ok = False
            else:
                say(f"配音key  ✓ {audio}  ({info.get('file')})")
        except Exception:  # noqa: BLE001
            say("配音key  ? 读不到状态，跑 `latent.py doctor` 看")
    else:
        say("配音CLI  ✗ 没有 latent-media —— 配音靠它做：\n"
            "         git clone https://github.com/guohuiyuan/latent-media ~/.workbuddy/skills/latent-media")
        ok = False

    base = os.environ.get("MUSIC_BASE")
    cli = HERE / "musicdl.py"
    local_bin = ""
    if cli.is_file():
        p = subprocess.run([PY, str(cli), "which"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        local_bin = (p.stdout or "").strip()
    if base and _music_up(base):
        try:
            n = len(json.loads(tool("bgm.py", "--base", base, "search", "lofi",
                                    "--json", capture=True)).get("rows", []))
            say(f"音乐站    ✓ {base}  返回 {n} 条候选")
        except SystemExit:
            say(f"音乐站    ✗ {base} 能开首页但搜不出东西，再看看")
    elif local_bin:
        say(f"音乐站    – 没在跑。本地有 music-dl：{local_bin}\n"
            "         要配乐时会自己起一个临时实例（只听 127.0.0.1），用完关掉")
    else:
        say("音乐站    – 没在跑，本地也没装 music-dl。\n"
            "         第一次配乐会从 GitHub release 下一个（约 13 MB，缓存在用户目录）；\n"
            "         不想联网就 `--file` 给自己的曲子，或加 `--no-local` 关掉自动安装")

    py = python_for_cover()
    if py:
        say(f"封面      ✓ playwright 可用（{py}）")
    else:
        say("封面      ✗ 缺 playwright：pip install playwright（复用本机 Edge，不用下 chromium）")
    say("\n环境" + ("就绪，可以开录了。" if ok else "有缺口，先补齐再往下。"))


def latent_cli() -> str | None:
    """配音交给 latent-media 的 CLI，本仓库不内置 TTS。"""
    for c in (os.environ.get("LATENT_CLI"),
              str(Path.home() / ".workbuddy" / "skills" / "latent-media" / "scripts" / "latent.py"),
              shutil.which("latent.py"), shutil.which("latent")):
        if c and Path(str(c)).is_file():
            return str(c)
    return None


def need_latent() -> str:
    cli = latent_cli()
    if cli:
        return cli
    die("配音要用 latent-media（本仓库不内置 TTS）：\n"
        "    git clone https://github.com/guohuiyuan/latent-media ~/.workbuddy/skills/latent-media\n"
        "    或设 LATENT_CLI=/path/to/latent.py")


def cmd_key(a) -> None:
    """key 归 latent-media 管，这里只是转调它的 keys 命令写进它自己的 keys.env。"""
    cli = need_latent()
    p = subprocess.run([PY, cli, "keys", "--audio", a.token.strip()],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        die(f"写 key 失败：\n{(p.stderr or p.stdout)[-800:]}")
    try:
        info = json.loads(p.stdout)
        say(f"[+] key 已交给 latent-media：{info.get('file')}  audio={info.get('audio')}")
    except Exception:  # noqa: BLE001
        say("[+] key 已写入 latent-media 的 keys.env")
    say("    接着 `rec2video.py build` 就能配音了")


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
        "title": a.title or "",
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
        say("\n下一步：**先把发布标题写进 title**（口播第一段要复述它），再写文案")
        say(f"    python scripts/rec2video.py script {pj} narration.txt --title \"好家伙！...\"")
    if not proj.get("title"):
        say("    [!] title 还空着：没有标题，首句复述就没法校验（script / hook 会报 ✗）")
    say(f"    python scripts/rec2video.py build  {pj}")


# ---------------------------------------------------------------- 各项 stage

def cmd_script(a) -> None:
    pj = Path(a.project).resolve()
    proj = load(pj)
    if getattr(a, "title", None):
        proj["title"] = a.title.strip()
    proj["segments"] = parse_script(Path(a.txt).expanduser())
    save(pj, proj)
    say(f"[+] {len(proj['segments'])} 段已写入 {pj.name}")
    for i, s in enumerate(proj["segments"], 1):
        say(f"    {i:>2}. {s['sub'][:38]}")
    report_hook(proj, pj)


def report_hook(proj: dict, pj: Path | None = None, title: str | None = None) -> bool:
    """当场验一遍：第一句有没有复述发布标题。"""
    res = hookcheck.check(proj, title)
    say("")
    say("文案  首句复述标题")
    for line in hookcheck.render(res):
        say(line)
    if not res["ok"]:
        tip = ("      → 先把发布标题写进 project.json 的 title（或 script --title），首句再复述它"
               if not res["title"] else "      → 改第一段，别往下配")
        say(tip)
    return bool(res["ok"])


def cmd_hook(a) -> None:
    pj = Path(a.project).resolve()
    proj = load(pj)
    if not report_hook(proj, pj, getattr(a, "title", None)):
        raise SystemExit(2)


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


def _music_up(base: str, timeout: float = 3.0) -> bool:
    """音乐站在不在：首页能取到就算在。"""
    if not base:
        return False
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/", timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def music_base(explicit: str | None = None, no_local: bool = False) -> tuple[str, bool]:
    """找一个能用的音乐站，返回 (base, 是不是这次现场拉起来的)。

    顺序：`--base` / `MUSIC_BASE` 指的地方 → 能连上就用；连不上（或压根没配）
    就调 `musicdl.py serve`：本地有 music-dl 就用，没有就从 GitHub release 下一个，
    起一个只听 127.0.0.1 的临时实例。`no_local` 时不许联网装，直接报错。
    """
    want = (explicit or os.environ.get("MUSIC_BASE") or "").strip().rstrip("/")
    if want and _music_up(want):
        return want, False
    if no_local:
        die(f"音乐站连不上：{want or '(没配 MUSIC_BASE)'}\n"
            "    a) 起一个：music-dl web --no-browser -p 8080，再设 "
            "MUSIC_BASE=http://127.0.0.1:8080/music\n"
            "    b) 用自己的曲子：bgm <project> --file 曲子.mp3")
    if want:
        say(f"[i] {want} 连不上，改用本地 music-dl 实例")
    else:
        say("[i] 没配音乐站，用本地 music-dl 实例")
    out = tool("musicdl.py", "serve", "--json", capture=True)
    info = json.loads(out.strip().splitlines()[-1])
    say(f"[i] 音乐站在跑了：{info['base']}（pid {info['pid']}）")
    return info["base"], not info.get("reused")


def cmd_bgm(a) -> None:
    """配乐来自 go-music-dl 的服务，或者你自己给的曲子；本仓库不造音乐。"""
    pj = Path(a.project).resolve()
    base = pj.parent
    proj = load(pj)
    if a.off:
        proj["bgm"]["src"] = ""
        save(pj, proj)
        say("[+] BGM 已关掉（成片照样有旁白/字幕/音效）")
        return
    want = _timeline_total(base) or 80.0
    (base / "assets").mkdir(parents=True, exist_ok=True)
    mp3 = base / "assets" / "bgm.mp3"

    if a.file:
        src = Path(a.file).expanduser().resolve()
        if not src.is_file():
            die(f"找不到 {src}")
        if src != mp3:
            shutil.copyfile(src, mp3)
        via = f"本地文件 {src.name}"
    else:
        want_base, booted = music_base(a.base, a.no_local)
        try:
            if a.pick:
                tool("bgm.py", "--base", want_base, "--local", "off",
                     "get", a.pick, mp3, "--source", a.source)
                via = f"音乐站 id={a.pick} [{a.source}]"
            else:
                keys = a.q or ["upbeat technology", "lofi study", "chill background"]
                out = tool("bgm.py", "--base", want_base, "--local", "off",
                           "search", *keys, "--json", capture=True)
                rows = [r for r in json.loads(out).get("rows", []) if r.get("dur", 0) >= 45]
                if not rows:
                    die("音乐站没搜到合适的：换关键词，或 --file 用自己的曲子")
                best = max(rows, key=lambda r: r.get("score", 0) + (6 if r["dur"] >= want else 0))
                m, s = divmod(best["dur"], 60)
                say(f"[i] 选中 {best['name']} — {best['artist']}  {m}:{s:02d}  "
                    f"id={best['id']} source={best.get('source') or 'netease'}")
                tool("bgm.py", "--base", want_base, "--local", "off",
                     "get", best["id"], mp3, "--source", best.get("source") or "netease")
                via = f"音乐站 {best['name'][:20]}"
        finally:
            if booted:
                subprocess.run([PY, str(HERE / "musicdl.py"), "stop"],
                               capture_output=True, text=True)

    proj["bgm"]["src"] = "assets/bgm.mp3"
    proj["bgm"]["volume"] = a.volume
    save(pj, proj)
    say(f"[+] BGM ← {via}；写进 {pj.name}：assets/bgm.mp3 volume={a.volume} duck=True")


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
    have_bgm = bool((proj.get("bgm") or {}).get("src")) and \
        (base / proj["bgm"]["src"]).is_file()
    if a.bgm == "off":
        if (proj.get("bgm") or {}).get("src"):
            proj["bgm"]["src"] = ""
            save(pj, proj)
    elif a.bgm_file:
        cmd_bgm(argparse.Namespace(project=str(pj), q=None, pick=None, off=False,
                                   file=a.bgm_file, volume=0.26, source="netease",
                                   base=None, no_local=False))
    elif not have_bgm:
        try:
            cmd_bgm(argparse.Namespace(project=str(pj), q=None, pick=None, off=False,
                                       file=None, volume=0.26, source="netease",
                                       base=None, no_local=getattr(a, "no_local", False)))
        except SystemExit as exc:
            if exc.code:
                say("[!] 配乐没做成，这次干声出片"
                    "（要曲子：`bgm <project> --file 曲子.mp3`）")

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
    report_hook(proj, pj)
    say("")
    caps = proj.get("caption") or {}
    say(f"标版  片头「{caps.get('start', '') or '(空)'}」／片尾「{caps.get('end', '') or '(空)'}」")
    say("      数字口径：封面、标题、简介、口播里的 star 数/功能数必须一致")


def python_for_cover() -> str | None:
    """封面靠 playwright 截 HTML。当前解释器没装就找机器上装了的那个。"""
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
            f"    封面要 playwright 截 HTML：pip install playwright\n"
            f"    装好后重跑：python scripts/cover.py render {specs[0]} -o {outdir} -s {a.sizes}")
        return
    tool("cover.py", "render", specs[0], "-o", outdir, "-s", a.sizes, py=py)
    say(f"\n[+] 封面在 {outdir}（改 spec.json 里的文案再 render 一次即可）")
    say("    标题/简介套句式和封面数字保持一致，别抄参考视频的 star 数")
    say("    简介三行给全：首句 / 干货 / 5–8 个话题标签（必带 #GitHub，SKILL.md 3.5）")


# ---------------------------------------------------------------- CLI

def main() -> None:
    ap = argparse.ArgumentParser(
        prog="rec2video",
        description="一段录屏 → 一条能发布的介绍片（配音/字幕/推镜/音效/BGM/封面）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="典型流程：new --title → script（顺手验首句）→ build → check → cover")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="体检环境").set_defaults(func=cmd_doctor)

    n = sub.add_parser("new", help="建工程（探测录屏 + 联系表 + project.json）")
    n.add_argument("src", help="录屏文件（OBS / Recordly / 系统录屏都行）")
    n.add_argument("-d", "--dir", help="工程目录（默认 ./<录屏名>）")
    n.add_argument("--name", help="工程名，也是成片文件名")
    n.add_argument("--repo", help="GitHub 仓库地址（填片尾标版 + 封面）")
    n.add_argument("--title", help="发布标题原文（口播第一段要复述它，先定标题再写文案）")
    n.add_argument("--voice", default="voice-120", help="音色 slug，默认 voice-120（大肥鱼）")
    n.add_argument("--script", help="文案 txt（一行一段），建工程时一起灌进去")
    n.add_argument("--force", action="store_true")
    n.set_defaults(func=cmd_new)

    s = sub.add_parser("script", help="把 txt 文案灌进 project.json")
    s.add_argument("project")
    s.add_argument("txt")
    s.add_argument("--title", help="顺手把发布标题写进 project.json 的 title")
    s.set_defaults(func=cmd_script)

    hk = sub.add_parser("hook", help="查首句有没有复述发布标题")
    hk.add_argument("project")
    hk.add_argument("--title", help="临时给一个标题（不写回 project.json）")
    hk.set_defaults(func=cmd_hook)

    i = sub.add_parser("info", help="录屏参数 + 抽帧")
    i.add_argument("project")
    i.add_argument("--every", type=float, default=5.0)
    i.set_defaults(func=cmd_info)

    t = sub.add_parser("tts", help="配音 + 时间轴")
    t.add_argument("project")
    t.add_argument("--force", action="store_true")
    t.set_defaults(func=cmd_tts)

    kk = sub.add_parser("key", help="把配音 key 交给 latent-media 保存")
    kk.add_argument("token", help="lat_tk_xxx")
    kk.set_defaults(func=cmd_key)

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
    b.add_argument("-q", "--q", nargs="+", help="音乐站搜索关键词（需 MUSIC_BASE）")
    b.add_argument("--pick", help="直接指定音乐站 id")
    b.add_argument("--source", default="netease",
                   help="配合 --pick：曲子的来源（search 结果里的 source 字段）")
    b.add_argument("--base", help="音乐站地址，默认取 MUSIC_BASE")
    b.add_argument("--no-local", action="store_true",
                   help="音乐站连不上时不要现场拉 music-dl（默认会去 GitHub release 下）")
    b.add_argument("--file", help="用本地曲子（最省事）")
    b.add_argument("--off", action="store_true", help="关掉 BGM")
    b.add_argument("--volume", type=float, default=0.26)
    b.set_defaults(func=cmd_bgm)

    d = sub.add_parser("build", help="配音 → 反剪 → 配乐 → 渲染")
    d.add_argument("project")
    d.add_argument("--preview", type=float, help="只渲前 N 秒")
    d.add_argument("--force", action="store_true", help="重做配音/重切")
    d.add_argument("--bgm", choices=["auto", "off"], default="auto",
                   help="auto=工程里有 BGM 或配了音乐站就用；off=不要 BGM")
    d.add_argument("--bgm-file", help="用自己的曲子（复制进 assets/bgm.mp3）")
    d.add_argument("--no-local", action="store_true",
                   help="配乐时不要现场拉 music-dl（默认连不上就去 GitHub release 下）")
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
