#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本仓库装到 agent 的 skills 目录（默认 ~/.workbuddy/skills/rec2video）。

    python scripts/install.py [目标目录]

只同步 git 里已提交的文件：不带 .git、不带测试产物；仓库里删掉的文件，
目标目录里也会一并清掉（仅当目标目录确实是本 skill 时）。
"""
from __future__ import annotations

import io
import subprocess
import sys
import tarfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent


def git(*args: str) -> bytes:
    p = subprocess.run(["git", "-C", str(SRC), *args], capture_output=True)
    if p.returncode != 0:
        sys.exit(f"[x] git {' '.join(args)} 失败\n{p.stderr.decode('utf-8', 'replace')[-600:]}")
    return p.stdout


def main() -> None:
    dst = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 \
        else Path.home() / ".workbuddy" / "skills" / "rec2video"
    if dst == SRC:
        print(f"[=] 源目录就是目标目录：{dst}")
        return
    if SRC in dst.parents:
        sys.exit(f"[x] 目标目录不能放在源目录里：{dst}")

    dst.mkdir(parents=True, exist_ok=True)
    blob = git("archive", "--format=tar", "HEAD")
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        try:
            tf.extractall(dst, filter="data")     # py3.12+
        except TypeError:
            tf.extractall(dst)

    keep = set(git("ls-tree", "-r", "--name-only", "HEAD").decode("utf-8").splitlines())
    removed = 0
    if (dst / "SKILL.md").is_file() and keep:
        for p in sorted(dst.rglob("*"), key=lambda x: len(x.parts), reverse=True):
            rel = p.relative_to(dst).as_posix()
            if p.is_file() and rel not in keep:
                p.unlink()
                removed += 1
            elif p.is_dir() and not any(p.iterdir()):
                p.rmdir()

    n = sum(1 for p in dst.rglob("*") if p.is_file())
    print(f"[+] rec2video -> {dst}  ({n} 个文件" + (f"，清掉 {removed} 个残留" if removed else "") + ")")
    print(f'   下一步：python "{dst / "scripts" / "rec2video.py"}" doctor')


if __name__ == "__main__":
    main()
