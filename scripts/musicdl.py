#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""go-music-dl 的本地二进制：有就找，没有就下一个，然后拉起它的 web 服务。

    python musicdl.py which                 # 打印要用的二进制（没有就空）
    python musicdl.py ensure                # 确保有二进制（PATH → 缓存 → GitHub release）
    python musicdl.py serve [--port N]      # 起服务并等到能访问，打印 base
    python musicdl.py status                # 看上次起的服务还在不在
    python musicdl.py stop                  # 停掉上次起的服务

给 `bgm.py` 兜底用：`MUSIC_BASE` 没配或连不上时，用它把 CLI 拉到本地、
起一个只监听 127.0.0.1 的 web 服务，再走同一套 HTTP 接口取 BGM。
服务起在本机临时目录下的 `data/` 里，不碰你的音乐库。

下载源是官方 release（github.com/guohuiyuan/go-music-dl），带 checksums.txt 校验。
不想联网装：设 `MUSIC_DL_BIN` 指到已有的 music-dl 可执行文件即可。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = "guohuiyuan/go-music-dl"
UA = {"User-Agent": "Mozilla/5.0 (compatible; rec2video-bgm/1.0)",
      "Accept": "application/vnd.github+json"}
EXE = ".exe" if os.name == "nt" else ""
STATE = "server.json"
LOG = "music-dl-web.log"


# ---------------------------------------------------------------- 位置

def cache_dir() -> Path:
    env = os.environ.get("REC2VIDEO_CACHE")
    if env:
        return Path(env).expanduser().resolve()
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return root / "rec2video" / "music-dl"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "rec2video" / "music-dl"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "rec2video" / "music-dl"


def cached_bin() -> Path:
    return cache_dir() / f"music-dl{EXE}"


def find_bin() -> Path | None:
    """已有可执行文件：显式指定 > PATH > 缓存。"""
    env = os.environ.get("MUSIC_DL_BIN")
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p.resolve()
    for name in (f"music-dl{EXE}", "music-dl"):
        hit = shutil.which(name)
        if hit:
            return Path(hit)
    p = cached_bin()
    return p if p.is_file() else None


# ---------------------------------------------------------------- 下载

def asset_name() -> str | None:
    """release 里的文件名后缀，按当前平台挑。"""
    sysname = platform.system().lower()          # windows / darwin / linux
    machine = platform.machine().lower()         # amd64 / arm64 / armv7l / x86_64
    arch = {"x86_64": "amd64", "amd64": "amd64",
            "arm64": "arm64", "aarch64": "arm64",
            "armv7l": "armv7", "armv6l": "armv6",
            "i386": "386", "i686": "386"}.get(machine, machine)
    if sysname == "windows":
        return f"windows_{arch}.zip"
    if sysname == "darwin":
        return f"darwin_{arch}.tar.gz"
    if sysname == "linux":
        return f"linux_{arch}.tar.gz"
    return None


def _get(url: str, timeout: int = 60, what: str = "") -> bytes:
    """普通 GET（API、校验文件这种小东西）。大文件走 _fetch。"""
    req = urllib.request.Request(url, headers=UA)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and "api.github.com" in url:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _progress_bar(total: int, what: str):
    """返回一个「报了进度」的函数。终端里原地刷，日志里每 10% 打一行。"""
    tty = sys.stderr.isatty()
    last = {"pct": -1}

    def report(done: int) -> None:
        pct = done * 100 // max(total, 1)
        if tty:
            sys.stderr.write(f"\r    {what} {done / 1048576:.1f}/{total / 1048576:.1f} MB ({pct}%)")
            sys.stderr.flush()
        elif pct // 10 > last["pct"] // 10:
            print(f"    {what} {done / 1048576:.1f}/{total / 1048576:.1f} MB ({pct}%)", flush=True)
        last["pct"] = pct

    def finish() -> None:
        if tty:
            sys.stderr.write("\r" + " " * 72 + "\r")
            sys.stderr.flush()

    return report, finish


def _stream(url: str, timeout: int, what: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        total = int(r.headers.get("Content-Length") or 0)
        report, finish = _progress_bar(total, what)
        buf = bytearray()
        while True:
            b = r.read(262144)
            if not b:
                break
            buf += b
            if total:
                report(len(buf))
        finish()
        return bytes(buf)


def _probe_total(url: str, timeout: int) -> int:
    """问服务端支持不支持 Range，顺便拿到总大小；不支持就返回 0。"""
    hdr = {**UA, "Range": "bytes=0-0"}
    with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=timeout) as r:
        crange = r.headers.get("Content-Range") or ""
        if r.status == 206 and "/" in crange:
            return int(crange.rsplit("/", 1)[1])
    return 0


def _fetch(url: str, timeout: int = 60, what: str = "下载", conns: int = 4,
           tries: int = 3) -> bytes:
    """下大文件。单连接经代理常常只有十几 KB/s，所以探到 Range 支持就切段并发拉。

    代理偶发 502，所以整轮重试；并发失败一次就退回单连接再试。
    """
    import threading
    import time as _t
    from concurrent.futures import ThreadPoolExecutor

    last: Exception | None = None
    for attempt in range(tries):
        if attempt:
            wait = 2 * attempt
            print(f"    第 {attempt + 1} 次重试（{last}），先等 {wait}s", flush=True)
            _t.sleep(wait)
        total = 0
        try:
            total = _probe_total(url, timeout)
        except Exception as exc:  # noqa: BLE001  探不到就直接单连接
            last = exc
            total = 0

        if total < 2 * 1048576 or conns <= 1:
            try:
                return _stream(url, timeout, what)
            except Exception as exc:  # noqa: BLE001
                last = exc
                continue

        step = total // conns
        spans = [(i * step, total - 1 if i == conns - 1 else (i + 1) * step - 1) for i in range(conns)]
        out = bytearray(total)
        done = 0
        lock = threading.Lock()
        report, finish = _progress_bar(total, what)

        def part(i: int, start: int, end: int) -> None:
            nonlocal done
            need = end - start + 1
            err: Exception | None = None
            for _ in range(3):
                got = 0
                try:
                    h = {**UA, "Range": f"bytes={start}-{end}"}
                    with urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                                timeout=timeout) as r:
                        pos = start
                        while True:
                            b = r.read(131072)
                            if not b:
                                break
                            out[pos:pos + len(b)] = b
                            pos += len(b)
                            got += len(b)
                            with lock:
                                done += len(b)
                                report(done)
                    if got != need:
                        raise IOError(f"只收到 {got}/{need} 字节")
                    return
                except Exception as exc:  # noqa: BLE001
                    err = exc
                    with lock:  # 这段作废，已计的进度退回去重来
                        done -= got
            raise RuntimeError(f"分片 {i + 1}/{conns} 三次都失败：{err}")

        try:
            with ThreadPoolExecutor(max_workers=conns) as pool:
                for f in [pool.submit(part, i, s, e) for i, (s, e) in enumerate(spans)]:
                    f.result()
            finish()
            return bytes(out)
        except Exception as exc:  # noqa: BLE001
            finish()
            last = exc
            print(f"    并发下载失败（{exc}），退回单连接", flush=True)
            try:
                return _stream(url, timeout, what)
            except Exception as exc2:  # noqa: BLE001
                last = exc2

    raise SystemExit(f"[!] 下不下来（试了 {tries} 轮）：{last}\n"
                     f"    可以自己下好 music-dl 再设 MUSIC_DL_BIN 指过去")


def _asset_urls(suffix: str) -> tuple[str, str]:
    """返回 (二进制下载地址, 版本号)。"""
    meta = json.loads(_get(f"https://api.github.com/repos/{REPO}/releases/latest").decode("utf-8"))
    tag = meta.get("tag_name", "?")
    want = f"go-music-dl_{suffix}"
    for a in meta.get("assets", []):
        if a.get("name") == want:
            return a["browser_download_url"], tag
    names = ", ".join(a.get("name", "") for a in meta.get("assets", []))
    raise SystemExit(f"[!] 这个平台的 release 里没有 {want}；有的：{names}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _extract(blob: bytes, suffix: str, workdir: Path) -> Path:
    """从 zip / tar.gz 里找出 music-dl 可执行文件，落到 workdir。"""
    tmp = workdir / f"_dl_{suffix.replace('.', '_')}"
    tmp.write_bytes(blob)
    out = workdir / f"music-dl{EXE}"
    try:
        if suffix.endswith(".zip"):
            with zipfile.ZipFile(tmp) as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    name = Path(info.filename).name.lower()
                    if name in (f"music-dl{EXE}".lower(), "music-dl", "music-dl.exe"):
                        out.write_bytes(z.read(info))
                        break
                else:
                    raise SystemExit(f"[!] zip 里没有 music-dl 可执行文件：{z.namelist()[:8]}")
        else:
            with tarfile.open(tmp) as t:
                for m in t.getmembers():
                    if not m.isfile():
                        continue
                    if Path(m.name).name.lower() in ("music-dl", "music-dl.exe"):
                        src = t.extractfile(m)
                        if src:
                            out.write_bytes(src.read())
                        break
                else:
                    raise SystemExit("[!] tar.gz 里没有 music-dl 可执行文件")
    finally:
        tmp.unlink(missing_ok=True)
    out.chmod(out.stat().st_mode | 0o111)
    return out


def ensure(quiet: bool = False, force: bool = False) -> Path:
    have = find_bin()
    if have and not force:
        if not quiet:
            print(f"[i] 用现成的 music-dl：{have}")
        return have
    suffix = asset_name()
    if not suffix:
        raise SystemExit(f"[!] 不认识的平台 {platform.system()}/{platform.machine()}："
                         "自己下 music-dl 并设 MUSIC_DL_BIN")
    d = cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    bin_url, tag = _asset_urls(suffix)
    if not quiet:
        print(f"[i] 本地没有 music-dl，去 GitHub release 下一个：{tag} {suffix}", flush=True)
        print(f"    {bin_url}", flush=True)
    # checksums.txt 与二进制同一个 release，用它校验完整性
    sum_url = bin_url.rsplit("/", 1)[0] + "/go-music-dl_checksums.txt"
    try:
        sums = _get(sum_url, timeout=30, what="校验文件").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001  老版本可能没有校验文件
        sums = ""
    blob = _fetch(bin_url, timeout=600, what="music-dl")
    want = ""
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*").endswith(suffix):
            want = parts[0].lower()
            break
    if want and _sha256(blob) != want:
        raise SystemExit("[!] checksums.txt 校验不过，安装包可能下坏了，重试一次")
    target = _extract(blob, suffix, d)
    if not quiet:
        print(f"[+] music-dl {tag} → {target}")
    return target


# ---------------------------------------------------------------- 起服务

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def is_up(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/music/", timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def read_state() -> dict:
    p = cache_dir() / STATE
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, errors="replace")
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def serve(port: int | None = None, wait: int = 60, keep: bool = False) -> dict:
    """起一个本地 web 服务；已经有活的就直接复用。"""
    st = read_state()
    if st.get("pid") and st.get("port") and alive(int(st["pid"])) and is_up(int(st["port"])):
        return {"base": st["base"], "port": int(st["port"]), "pid": int(st["pid"]), "reused": True}

    exe = ensure()
    port = port or free_port()
    d = cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    log = open(d / LOG, "ab", buffering=0)
    kwargs: dict = {"cwd": str(d), "stdout": log, "stderr": log, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000200 | 0x08000000  # NEW_PROCESS_GROUP | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([str(exe), "web", "--no-browser", "-p", str(port)], **kwargs)

    deadline = time.time() + wait
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = (d / LOG).read_text(encoding="utf-8", errors="replace")[-600:]
            raise SystemExit(f"[!] music-dl web 起不来（退出码 {proc.returncode}）：\n{tail}")
        if is_up(port):
            base = f"http://127.0.0.1:{port}/music"
            state = {"base": base, "port": port, "pid": proc.pid,
                     "started": time.strftime("%Y-%m-%d %H:%M:%S"), "keep": keep}
            (d / STATE).write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
            return {**state, "reused": False}
        time.sleep(0.5)
    raise SystemExit(f"[!] 等了 {wait}s 服务还没起来，看日志：{d / LOG}")


def stop() -> bool:
    st = read_state()
    pid = st.get("pid")
    if not pid:
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True, errors="replace")
        else:
            os.killpg(os.getpgid(int(pid)), 15)
    except Exception:  # noqa: BLE001
        pass
    (cache_dir() / STATE).unlink(missing_ok=True)
    return True


# ---------------------------------------------------------------- 命令行

def main() -> None:
    ap = argparse.ArgumentParser(prog="musicdl.py", description="本地 music-dl 二进制与服务管理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("which", help="打印要用的二进制路径")

    e = sub.add_parser("ensure", help="确保本地有二进制（没有就下）")
    e.add_argument("--force", action="store_true", help="无视现成的，重新下")
    e.add_argument("--quiet", action="store_true")

    s = sub.add_parser("serve", help="起服务并等到能访问")
    s.add_argument("--port", type=int, help="端口，默认随机挑一个空闲的")
    s.add_argument("--wait", type=int, default=60, help="等多久算超时（秒）")
    s.add_argument("--keep", action="store_true", help="退出后不自动停（默认由调用方决定）")
    s.add_argument("--json", action="store_true", help="只打印 JSON，给脚本用")

    sub.add_parser("status", help="看服务状态")
    sub.add_parser("stop", help="停掉上次起的服务")

    a = ap.parse_args()

    if a.cmd == "which":
        p = find_bin()
        print(p or "")
        return

    if a.cmd == "ensure":
        print(ensure(quiet=a.quiet, force=a.force))
        return

    if a.cmd == "serve":
        info = serve(a.port, a.wait, a.keep)
        if a.json:
            print(json.dumps(info, ensure_ascii=False))
        else:
            print(info["base"])
        return

    if a.cmd == "status":
        st = read_state()
        if not st.get("pid"):
            print("[i] 没起过")
            return
        ok = alive(int(st["pid"]))
        print(f"[{'✓' if ok else '×'}] pid={st['pid']} {st.get('base')} "
              f"开于 {st.get('started')}" + ("" if ok else "（进程已退出）"))
        return

    if a.cmd == "stop":
        print("[+] 已停" if stop() else "[i] 没有在跑的服务")


if __name__ == "__main__":
    main()
