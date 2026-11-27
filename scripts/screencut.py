#!/usr/bin/env python3
"""rec2video · cut — 长录屏 → 介绍片素材。零依赖（只要 ffmpeg/ffprobe）。

子命令
    scan   录屏体检：分辨率/时长/建议 crop，抽联系表（带时间戳），场景切换点
    cut    按旁白时长反剪：每段画面长度 = 该段配音时长 + gap，拼成 screen-edit.mp4
    zooms  用「原片像素坐标」填推镜点，自动换算成画框坐标写进 project.json
    check  交付前自检：拉伸系数、锚点是否越界、推镜是否挤在一起

坐标这件事最容易错：人眼看的是原片（2560x1440），渲染吃的是画框（1560x878）。
所以 shots 里一律填原片坐标，由 zooms 换算 `(x - crop_x) * frame_w / crop_w`。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

FONT_CANDIDATES = [
    r"C\:/Windows/Fonts/msyh.ttc",
    r"C\:/Windows/Fonts/msyhbd.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


# ---------------------------------------------------------------- 基础

def die(msg: str) -> None:
    print(f"[x] {msg}", file=sys.stderr)
    raise SystemExit(1)


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def run(cmd: list[str], cwd: Path | None = None, quiet: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd) if cwd else None,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0 and not quiet:
        print(p.stderr[-1500:], file=sys.stderr)
    return p


def ffprobe(path: Path) -> dict:
    p = run(["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)])
    if p.returncode != 0:
        die(f"ffprobe 读不了 {path}\n{p.stderr[-500:]}")
    d = json.loads(p.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    if not v:
        die(f"{path} 里没有视频流")
    num, den = (v.get("r_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den or 1)
    return {
        "w": int(v["width"]), "h": int(v["height"]), "fps": round(fps, 3),
        "duration": round(float(d["format"].get("duration", 0)), 3),
        "codec": v.get("codec_name"), "path": str(path),
    }


def parse_crop(crop: str, src: dict) -> tuple[int, int, int, int]:
    """'2560:1440:0:60' → (w, h, x, y)；空字符串 = 整幅。"""
    if not crop:
        return src["w"], src["h"], 0, 0
    m = re.fullmatch(r"\s*(\d+):(\d+):(\d+):(\d+)\s*", crop)
    if not m:
        die(f"crop 格式应为 W:H:X:Y，收到 {crop!r}")
    w, h, x, y = (int(g) for g in m.groups())
    if x + w > src["w"] or y + h > src["h"]:
        die(f"crop {crop} 超出源画面 {src['w']}x{src['h']}")
    return w, h, x, y


def load_json(p: Path) -> dict:
    if not p.is_file():
        die(f"找不到 {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def save_json(p: Path, d: dict) -> None:
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def detect_font() -> str | None:
    for f in FONT_CANDIDATES:
        raw = f.replace("\\:", ":")
        if Path(raw).is_file():
            return f
    return None


def hms(t: float) -> str:
    t = max(0.0, t)
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:05.2f}"


# ---------------------------------------------------------------- scan

def cmd_scan(a) -> None:
    src = Path(a.src).expanduser().resolve()
    if not src.is_file():
        die(f"找不到录屏 {src}")
    info = ffprobe(src)
    out = Path(a.out or (src.parent / f"{src.stem}-scan")).resolve()
    out.mkdir(parents=True, exist_ok=True)

    print(f"源     {src.name}")
    print(f"画面   {info['w']}x{info['h']}  {info['fps']:g}fps  {info['codec']}")
    print(f"时长   {info['duration']:.2f}s  ({hms(info['duration'])})")

    # 16:9 建议 crop：宽度取满，高度按 16:9 居中裁
    w, h = info["w"], info["h"]
    if w * 9 / 16 <= h:
        ch = round(w * 9 / 16)
        cy = (h - ch) // 2
        print(f"建议   crop={w}:{ch}:0:{cy}   （16:9 居中；标签栏/任务栏要再手动往里收）")
    else:
        cw = round(h * 16 / 9)
        cx = (w - cw) // 2
        print(f"建议   crop={cw}:{h}:{cx}:0   （16:9 居中）")
    print(f"       16:9 目标宽建议 ≥1920；2x 缩放录屏推镜时不糊")

    # 联系表
    step = float(a.step)
    cols, rows = int(a.cols), int(a.rows)
    per = cols * rows
    t_from, t_to = float(a.start), float(a.end or info["duration"])
    n = max(1, int((t_to - t_from) / step))
    tiles = cols * rows
    frame_step = max(1, round(step * info["fps"]))
    font = detect_font() or a.font
    # 注意：-ss 之后 pts 会归零，标签必须自己加回起点，否则全是 0s 起的相对时间
    label = ("drawtext=fontfile='{}':text='%{{eif\\:trunc(t+{})\\:d}}s':fontsize=26:"
             "fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=6:x=8:y=8,"
             ).format(font, t_from) if font else ""
    vf = (f"select='not(mod(n\\,{frame_step}))',scale={a.tile_w}:-1,{label}"
          f"tile={cols}x{rows}:margin=8:padding=5:color=white")
    pattern = out / "sheet_%02d.jpg"
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y",
           "-ss", f"{t_from}", "-to", f"{t_to}", "-i", str(src),
           "-vf", vf, "-vsync", "0", "-q:v", "3", str(pattern)]
    p = run(cmd)
    sheets = sorted(out.glob("sheet_*.jpg"))
    if p.returncode != 0 or not sheets:
        print(f"[!] 联系表生成失败（多半是 drawtext 缺字体），改用无标签版")
        vf2 = f"select='not(mod(n\\,{frame_step}))',scale={a.tile_w}:-1,tile={cols}x{rows}:margin=8:padding=5"
        run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-ss", f"{t_from}", "-to", f"{t_to}",
             "-i", str(src), "-vf", vf2, "-vsync", "0", "-q:v", "3", str(pattern)])
        sheets = sorted(out.glob("sheet_*.jpg"))

    times = [t_from + i * step for i in range(n + 1)]
    print(f"\n联系表 {len(sheets)} 张 → {out}")
    for k in range(len(sheets)):
        chunk = times[k * per:(k + 1) * per]
        if not chunk:
            break
        print(f"  sheet_{k + 1:02d}.jpg   " + " ".join(f"{t:.0f}s" for t in chunk))
    print("  看图找：应用真正的界面从第几秒开始、哪些段在动、哪些是空转/加载/别的窗口")

    if a.scene:
        print(f"\n场景切换（阈值 {a.scene}，只列前 {a.scene_limit} 个）：")
        p = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(src),
                 "-vf", f"select='gt(scene,{a.scene})',metadata=print:file=-",
                 "-an", "-f", "null", "-"])
        hits = re.findall(r"pts_time:([0-9.]+)", p.stdout + p.stderr)
        times2 = [round(float(t), 2) for t in hits if t_from <= float(t) <= t_to]
        for t in times2[: int(a.scene_limit)]:
            print(f"  {hms(t)}")
        if len(times2) > int(a.scene_limit):
            print(f"  … 共 {len(times2)} 个")
        save_json(out / "scenes.json", {"src": str(src), "threshold": a.scene, "times": times2})


# ---------------------------------------------------------------- cut

def cmd_plan(a) -> None:
    """按 timeline.json 生成 plan.json 骨架：段数、每段需要的秒数都填好，只留「取材起点」。"""
    proj_p = Path(a.project).resolve()
    proj = load_json(proj_p)
    base = proj_p.parent
    tl_p = Path(a.timeline).resolve() if a.timeline else base / "work" / "timeline.json"
    tl = load_json(tl_p)
    gap = float(tl.get("gap", 0.32))
    segs = tl["segs"]
    out = base / "plan.json"
    if out.is_file() and not a.force:
        die(f"{out} 已存在，加 --force 覆盖（会丢已填的取材点）")
    rows = []
    for i, s in enumerate(segs, 1):
        want = round(float(s["dur"]) + (gap if i < len(segs) else 0.0), 2)
        rows.append({"at": None, "max": round(want + 1.5, 2),
                     "note": f"第 {i} 段：{s.get('sub', '')[:18]}"})
    plan = {
        "src": proj["video"]["src"],
        "crop": a.crop if a.crop is not None else proj["video"].get("crop", ""),
        "fps": proj["output"].get("fps", 30),
        "segments": rows,
        "shots": [],
    }
    save_json(out, plan)
    print(f"[+] {out.name}  {len(rows)} 段，总计约 {float(tl['total']):.2f}s")
    print("    下一步：把每段的 at 改成「这段内容在原录屏第几秒」"
          "（用 scan 的联系表找），越界会被 cut 拦下")
    for r, s in zip(rows, segs):
        print(f"    {s.get('sub', '')[:30]:<32} 需 {r['max'] - 1.5:.2f}s")


def _cut_key(src: Path, at: float, want: float, vf: str, fps) -> str:
    """切片缓存键：源视频 / 取材点 / 时长 / 裁切 / 帧率任一变化都算新切片。

    只按文件名判断会复用「长度已经不对」的旧切片 —— 改完文案重跑 cut，
    报告说 58s，实际拼的还是 62s 的旧片子，成片 stretch 直接偏 6%。
    """
    raw = f"{src.resolve()}|{at:.3f}|{want:.3f}|{vf}|{fps}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]


def cmd_cut(a) -> None:
    plan_p = Path(a.plan).resolve()
    plan = load_json(plan_p)
    base = plan_p.parent
    proj_p = Path(a.project).resolve() if a.project else base / "project.json"
    proj = load_json(proj_p)
    tl_p = Path(a.timeline).resolve() if a.timeline else base / "work" / "timeline.json"
    if not tl_p.is_file():
        die(f"找不到 {tl_p}，先跑 intro.py tts（要先有配音，画面才能按它反剪）")
    tl = load_json(tl_p)

    src = Path(plan.get("src") or proj["video"]["src"]).expanduser()
    if not src.is_absolute():
        src = (base / src).resolve()
    if not src.is_file():
        die(f"找不到录屏 {src}")
    info = ffprobe(src)

    crop = plan.get("crop", proj["video"].get("crop", ""))
    cw, ch, cx, cy = parse_crop(crop, info)
    vf = f"crop={cw}:{ch}:{cx}:{cy}" if crop else f"scale={cw}:{ch}"

    gap = float(tl.get("gap", proj.get("voice", {}).get("gap", 0.32)))
    segs = tl["segs"]
    plan_segs = plan["segments"]
    if len(plan_segs) != len(segs):
        die(f"plan 里 {len(plan_segs)} 段，旁白 {len(segs)} 段 —— 必须一一对应")
    missing = [str(i) for i, ps in enumerate(plan_segs, 1) if ps.get("at") is None]
    if missing:
        die(f"plan 里第 {', '.join(missing)} 段还没填 at（这段内容在原录屏的第几秒）")

    cuts = base / "work" / "cuts"
    cuts.mkdir(parents=True, exist_ok=True)
    fps = plan.get("fps", 30)
    parts, total, warns = [], 0.0, []
    info_cache: dict[Path, dict] = {src: info}

    print(f"{'段':>3}  {'取材':>16}  {'时长':>6}  说明")
    for i, (seg, ps) in enumerate(zip(segs, plan_segs), 1):
        want = float(seg["dur"]) + (gap if i < len(segs) else 0.0)
        cap = ps.get("max")
        if cap and want > cap:
            warns.append(f"s{i:02d} 需要 {want:.2f}s，素材上限 {cap}s —— 已截到上限，旁白会挤")
            want = float(cap)

        # 单段可指定外部成品源（如拼接渲染好的成片展示段），自动统一到主裁切分辨率与帧率
        if ps.get("src"):
            seg_src = Path(ps["src"]).expanduser()
            if not seg_src.is_absolute():
                seg_src = (base / seg_src).resolve()
            if not seg_src.is_file():
                die(f"s{i:02d} 找不到分段视频 {seg_src}")
            if seg_src not in info_cache:
                info_cache[seg_src] = ffprobe(seg_src)
            seg_info = info_cache[seg_src]
            sc = ps.get("crop", "")
            if sc:
                scw, sch, scx, scy = parse_crop(sc, seg_info)
                seg_vf = f"crop={scw}:{sch}:{scx}:{scy},scale={cw}:{ch}"
            else:
                seg_vf = (f"scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
                          f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:black")
        else:
            seg_src, seg_info, seg_vf = src, info, vf

        at = float(ps["at"])
        b = at + want
        if b > seg_info["duration"]:
            warns.append(f"s{i:02d} 取材越界 {at:.2f}→{b:.2f}s，源只有 {seg_info['duration']:.2f}s")
            b = seg_info["duration"]
            want = b - at
        out = cuts / f"s{i:02d}_{_cut_key(seg_src, at, want, seg_vf, fps)}.mp4"
        if not out.is_file() or a.force:
            # 统一成 yuv420p + limited(tv) 范围，防止不同源（如 yuvj420p/pc）concat 后让下游 zoompan 卡帧
            norm_vf = f"{seg_vf},scale=in_range=auto:out_range=tv,format=yuv420p"
            p = run(["ffmpeg", "-hide_banner", "-v", "error", "-y",
                     "-ss", f"{at}", "-to", f"{b}", "-i", str(seg_src), "-an",
                     "-vf", norm_vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                     "-threads", "2", "-pix_fmt", "yuv420p", "-color_range", "tv",
                     "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                     "-r", str(fps), str(out)])
            if p.returncode != 0:
                die(f"s{i:02d} 切片失败\n{p.stderr[-800:]}")
        parts.append(out)
        total += want
        print(f"{i:>3}  {at:7.2f}→{b:7.2f}  {want:6.2f}  {ps.get('note', '')}")

    lst = cuts / "list.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    for stale in cuts.glob("s[0-9][0-9]*.mp4"):     # 清掉上一版留下的切片
        if stale not in parts:
            stale.unlink()
    final = base / "assets" / "screen-edit.mp4"
    final.parent.mkdir(parents=True, exist_ok=True)
    # concat demuxer 不认 Windows 绝对路径的冒号 → 相对名 + cwd
    p = run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "concat", "-safe", "0",
             "-i", lst.name, "-c", "copy", str(final)], cwd=cuts)
    if p.returncode != 0:
        die(f"拼接失败\n{p.stderr[-800:]}")

    voice = float(tl["total"])
    stretch = voice / total if total else 1.0
    print(f"\n画面 {total:.2f}s ／ 旁白 {voice:.2f}s → stretch {stretch:.4f}")
    if abs(stretch - 1) > 0.05:
        print(f"[!] 拉伸 {stretch:.3f} 超出 ±5%：画面会明显变速。"
              f"{'缩短' if stretch > 1 else '拉长'}某几段的取材时长，或改文案长度。")
    for w in warns:
        print(f"[!] {w}")

    proj["video"]["src"] = str(final)
    proj["video"]["trim_head"] = 0.0
    proj["video"]["crop"] = ""
    save_json(proj_p, proj)
    print(f"\n[+] {final.name} {total:.2f}s  · {info['w']}x{info['h']} → {cw}x{ch}")
    print(f"[+] project.json 已指向它（trim_head=0, crop 置空）")


# ---------------------------------------------------------------- zooms

def cmd_zooms(a) -> None:
    plan_p = Path(a.plan).resolve()
    plan = load_json(plan_p)
    base = plan_p.parent
    proj_p = Path(a.project).resolve() if a.project else base / "project.json"
    proj = load_json(proj_p)
    tl_p = Path(a.timeline).resolve() if a.timeline else base / "work" / "timeline.json"
    tl = load_json(tl_p)

    # 锚点按「原始录屏」的尺寸换算：cut 之后 project.video.src 已是裁好的画面，
    # 拿它当基准会把 crop 偏移算两遍。
    ref = Path(plan.get("src") or proj["video"]["src"]).expanduser()
    if not ref.is_absolute():
        ref = (base / ref).resolve()
    if not ref.is_file():
        ref = Path(proj["video"]["src"])
        if not ref.is_absolute():
            ref = (base / ref).resolve()
    info = ffprobe(ref)
    crop = plan.get("crop", "")
    if not crop:
        die("plan 里缺 crop —— 换算锚点必须知道裁切区，否则坐标全错")
    cw, ch, cx, cy = parse_crop(crop, info)

    fr = proj["video"]["frame"]
    fw, fh = int(fr["w"]), int(fr["h"])
    sx, sy = fw / cw, fh / ch

    starts = {int(s["i"]): float(s["start"]) for s in tl["segs"]}
    total = float(tl["total"])
    shots = plan.get("shots") or []
    if not shots:
        die("plan 里没有 shots，例如 [{\"seg\":3,\"off\":1.8,\"x\":246,\"y\":443,\"z\":1.55}]")

    zooms, bad = [], []
    for s in shots:
        i, off = int(s["seg"]), float(s.get("off", 1.8))
        if i not in starts:
            bad.append(f"seg {i} 不存在（共 {len(starts)} 段）")
            continue
        at = round(starts[i] + off, 2)
        if at + 2.2 > total:
            bad.append(f"seg{i} @{at:.2f}s 离片尾太近（总长 {total:.2f}s），已跳过")
            continue
        z = float(s.get("z", 1.4))
        fx = round((float(s["x"]) - cx) * sx, 1)
        fy = round((float(s["y"]) - cy) * sy, 1)
        if not (0 <= fx <= fw and 0 <= fy <= fh):
            bad.append(f"seg{i} 锚点 ({s['x']},{s['y']}) 换算后 ({fx},{fy}) 落在画框外")
            continue
        zooms.append({"at": at, "x": int(round(fx)), "y": int(round(fy)), "z": z})

    zooms.sort(key=lambda d: d["at"])
    proj["zooms"] = zooms
    if proj.get("caption"):
        proj["caption"]["end_at"] = round(total - float(a.tail, ), 2)
    save_json(proj_p, proj)

    print(f"换算  原片 {info['w']}x{info['h']} · crop {cw}x{ch}@{cx},{cy} → 画框 {fw}x{fh}"
          f"  比例 {sx:.4f}")
    print(f"推镜  {len(zooms)} 个 / 总长 {total:.2f}s")
    for z, s in zip(zooms, [x for x in shots]):
        print(f"  {z['at']:6.2f}s  原片({s['x']},{s['y']}) → 画框({z['x']},{z['y']})  x{z['z']}"
              f"  {s.get('note', '')}")
    for b in bad:
        print(f"[!] {b}")


# ---------------------------------------------------------------- check

def cmd_check(a) -> None:
    proj_p = Path(a.project).resolve()
    proj = load_json(proj_p)
    base = proj_p.parent
    issues, notes = [], []

    src = Path(proj["video"]["src"])
    if not src.is_absolute():
        src = (base / src).resolve()
    if not src.is_file():
        issues.append(f"video.src 不存在：{src}")
        info = {"duration": 0, "w": 0, "h": 0, "fps": 0}
    else:
        info = ffprobe(src)
        notes.append(f"素材 {info['w']}x{info['h']} {info['fps']:g}fps {info['duration']:.2f}s")

    tl_p = base / "work" / "timeline.json"
    voice = None
    if tl_p.is_file():
        tl = load_json(tl_p)
        voice = float(tl["total"])
        stretch = voice / info["duration"] if info["duration"] else 0
        notes.append(f"旁白 {voice:.2f}s → stretch {stretch:.4f}")
        if abs(stretch - 1) > 0.05:
            issues.append(f"stretch {stretch:.3f} 偏太多，画面会变速；重跑 cut 调整取材时长")
        n_sub = len(tl["segs"])
        if len(proj["segments"]) != n_sub:
            issues.append(f"segments {len(proj['segments'])} 段 ≠ 配音 {n_sub} 段")
    else:
        notes.append("没有 work/timeline.json（还没配音？）")

    fr = proj["video"]["frame"]
    fw, fh = int(fr["w"]), int(fr["h"])
    if int(fr["x"]) + fw > int(proj["output"]["w"]) or int(fr["y"]) + fh > int(proj["output"]["h"]):
        issues.append(f"画框 {fw}x{fh}@{fr['x']},{fr['y']} 超出输出 "
                      f"{proj['output']['w']}x{proj['output']['h']}")
    gap_bottom = int(proj["output"]["h"]) - (int(fr["y"]) + fh)
    if gap_bottom < 130:
        issues.append(f"画框下沿只剩 {gap_bottom}px，两行字幕（约 94px）会贴死画框；"
                      f"想要留白得压 frame.h")

    zooms = proj.get("zooms") or []
    notes.append(f"推镜 {len(zooms)} 个")
    for z in zooms:
        if not (0 <= z["x"] <= fw and 0 <= z["y"] <= fh):
            issues.append(f"推镜 {z['at']}s 锚点 ({z['x']},{z['y']}) 落在画框外")
        if not (1.0 <= z["z"] <= 1.8):
            issues.append(f"推镜 {z['at']}s 倍率 {z['z']} 不合理（1.25–1.6 为宜）")
    for a_, b_ in zip(zooms, zooms[1:]):
        if b_["at"] - a_["at"] < 2.0:
            issues.append(f"推镜 {a_['at']}s 与 {b_['at']}s 间隔不足 2s，看起来像抖了一下")
        if abs(a_["x"] - b_["x"]) < 60 and abs(a_["y"] - b_["y"]) < 60 and abs(a_["z"] - b_["z"]) < 0.08:
            issues.append(f"推镜 {a_['at']}s 与 {b_['at']}s 锚点和倍率几乎一样，会显得重复")

    if voice and zooms and zooms[-1]["at"] > voice - 3:
        issues.append(f"最后一个推镜 {zooms[-1]['at']}s 压在片尾上")

    sub = proj.get("subtitle", {})
    if sub.get("margin_v", 46) > gap_bottom - 90:
        notes.append(f"margin_v={sub.get('margin_v')} 之于底部 {gap_bottom}px 偏小（越小越靠下）")

    print("检查", proj_p.name)
    for n in notes:
        print(f"  · {n}")
    if not issues:
        print("\n[+] 没发现问题。仍然要看图：推镜最大倍率处文字有没有被切。")
    else:
        print()
        for i in issues:
            print(f"[!] {i}")
        print(f"\n共 {len(issues)} 处需要注意。")


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(
        prog="cut.py", description="长录屏 → 介绍片素材（扫描 / 反剪 / 推镜换算 / 自检）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-V", "--version", action="version", version="rec2video-cut 1.0.0")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="录屏体检 + 联系表 + 场景切换点")
    s.add_argument("src")
    s.add_argument("-o", "--out", help="输出目录（默认 <录屏名>-scan）")
    s.add_argument("--step", default=15, type=float, help="联系表采样间隔秒，默认 15")
    s.add_argument("--cols", default=4, type=int)
    s.add_argument("--rows", default=3, type=int)
    s.add_argument("--tile-w", default=480, type=int, help="每格宽度，默认 480")
    s.add_argument("--start", default=0, type=float, help="只扫这段（起点）")
    s.add_argument("--end", default=0, type=float, help="只扫这段（终点，0=到片尾）")
    s.add_argument("--scene", default=0.3, type=float, help="场景切换阈值，0 关闭")
    s.add_argument("--scene-limit", default=40, type=int)
    s.add_argument("--font", help="drawtext 字体路径（默认自动找）")
    s.set_defaults(func=cmd_scan)

    c = sub.add_parser("plan", help="按配音时间轴生成 plan.json 骨架")
    c.add_argument("-p", "--project", required=True, help="project.json")
    c.add_argument("-t", "--timeline", help="timeline.json（默认 work/timeline.json）")
    c.add_argument("--crop", help="裁切区 W:H:X:Y（默认抄 project.video.crop）")
    c.add_argument("--force", action="store_true")
    c.set_defaults(func=cmd_plan)

    c = sub.add_parser("cut", help="按旁白时长反剪并拼接")
    c.add_argument("plan", help="plan.json")
    c.add_argument("-p", "--project", help="project.json（默认同目录）")
    c.add_argument("-t", "--timeline", help="timeline.json（默认 work/timeline.json）")
    c.add_argument("--force", action="store_true", help="丢弃已有切片重切")
    c.set_defaults(func=cmd_cut)

    z = sub.add_parser("zooms", help="推镜锚点换算并写进 project.json")
    z.add_argument("plan")
    z.add_argument("-p", "--project")
    z.add_argument("-t", "--timeline")
    z.add_argument("--tail", default=6.2, type=float, help="片尾标版提前几秒出现，默认 6.2")
    z.set_defaults(func=cmd_zooms)

    k = sub.add_parser("check", help="交付前自检")
    k.add_argument("project")
    k.set_defaults(func=cmd_check)

    a = ap.parse_args()
    if not have("ffmpeg") or not have("ffprobe"):
        die("PATH 里没有 ffmpeg / ffprobe")
    a.func(a)


if __name__ == "__main__":
    main()
