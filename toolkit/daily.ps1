#requires -Version 5.1
param(
    [switch]$Check,
    [ValidateSet('track', 'campus')][string]$Mode = 'track'
)
<#
.SYNOPSIS
    每天一次学分跑（薄封装，真正的逻辑在 daily_run.py）。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File D:\Work\xbud_toolkit\daily.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File D:\Work\xbud_toolkit\daily.ps1 -Mode campus
    powershell -NoProfile -ExecutionPolicy Bypass -File D:\Work\xbud_toolkit\daily.ps1 -Check

.NOTES
    PowerShell 5.1 里 adb 的 stderr 输出会触发 NativeCommandError，
    管道捕获 adb 常驻 daemon 还会让脚本挂住，所以真正的流程放在 Python：
    daily_run.py。这里只负责转交参数。
#>

$ErrorActionPreference = 'Continue'
$toolkit = 'D:\Work\xbud_toolkit'
$env:PYTHONPATH = $toolkit
Set-Location -LiteralPath $toolkit

$argv = @("$toolkit\daily_run.py", '--mode', $Mode)
if ($Check) { $argv += '--check' }

& python @argv
exit $LASTEXITCODE
