#!/usr/bin/env sh
# 装到 agent 的 skills 目录（默认 ~/.workbuddy/skills/rec2video）
# 只复制仓库里的文件，不带 .git / 测试产物。
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DST="${1:-$HOME/.workbuddy/skills/rec2video}"

if [ "$SRC" = "$DST" ]; then
  echo "[=] 源目录就是目标目录：$DST"
  exit 0
fi

mkdir -p "$DST"
DST="$(cd "$DST" && pwd)"
case "$DST/" in
  "$SRC"/*) echo "[x] 目标目录不能放在源目录里：$DST"; exit 1 ;;
esac

if git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
  git -C "$SRC" archive --format=tar HEAD | tar -x -C "$DST"
else
  cp -R "$SRC"/. "$DST"/
  rm -rf "$DST/.git"
fi

echo "[+] rec2video -> $DST"
echo "   下一步：python \"$DST/scripts/rec2video.py\" doctor"
