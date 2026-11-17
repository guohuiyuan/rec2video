#!/usr/bin/env sh
# 装到 agent 的 skills 目录：sh install.sh [目标目录]
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
exec python "$SRC/scripts/install.py" "$@"
