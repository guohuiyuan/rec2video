#!/usr/bin/env sh
# 装到 agent 的 skills 目录（默认 ~/.workbuddy/skills/rec2video）
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DST="${1:-$HOME/.workbuddy/skills/rec2video}"
mkdir -p "$DST"
DST="$(cd "$DST" && pwd)"
case "$DST/" in
  "$SRC"/*) echo "[x] 目标目录不能放在源目录里：$DST"; exit 1 ;;
esac
cp -R "$SRC"/. "$DST"/
rm -rf "$DST/.git" "$DST/.tmp-test"
echo "[+] rec2video -> $DST"
echo "   下一步：python \"$DST/scripts/rec2video.py\" doctor"
