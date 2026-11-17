# 装到 agent 的 skills 目录：powershell -c .\install.ps1 [目标目录]
# 实际逻辑在 scripts/install.py（只同步 git 里已提交的文件）
$ErrorActionPreference = "Stop"
$src = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $src "scripts\install.py"
if ($args.Count -ge 1) { python $script $args[0] } else { python $script }
