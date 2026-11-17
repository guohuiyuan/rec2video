# 装到 agent 的 skills 目录（默认 $HOME\.workbuddy\skills\rec2video）
$ErrorActionPreference = "Stop"
$src = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($args.Count -ge 1) { $dst = $args[0] } else { $dst = Join-Path $HOME ".workbuddy\skills\rec2video" }

New-Item -ItemType Directory -Force -Path $dst | Out-Null
robocopy $src $dst /E /NFL /NDL /NJH /NJS /XD .git .tmp-test __pycache__ | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy 失败，退出码 $LASTEXITCODE" }

Write-Output "[+] rec2video -> $dst"
Write-Output "    下一步：python `"$dst\scripts\rec2video.py`" doctor"
