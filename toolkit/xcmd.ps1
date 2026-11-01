#requires -Version 5.1
param(
    [Parameter(Position = 0, Mandatory = $true)][string]$Command,
    [int]$WaitMs = 1500
)
<#
.SYNOPSIS
    手动模式下给 xbudloc 模块发一条命令，并回显模块日志。

.DESCRIPTION
    命令写进 /data/data/run.xbud.android/files/xbud_cmd（root 写 + chmod 666），
    模块每 400 ms 轮询一次。命令后面会自动加时间戳，避免和上一条重复被忽略。

.EXAMPLE
    .\xcmd.ps1 state
    .\xcmd.ps1 reset
    .\xcmd.ps1 hold
    .\xcmd.ps1 go
    .\xcmd.ps1 views
    .\xcmd.ps1 cam
    .\xcmd.ps1 points
    .\xcmd.ps1 llog          # 开关 LatLng 日志（长跑前建议关掉）
    .\xcmd.ps1 polygon       # dump 校园围栏
#>

$adb = 'D:\MuMuPlayer\nx_device\15.0\shell\adb.exe'
$serial = '127.0.0.1:16384'
$tmp = Join-Path $env:TEMP 'xbud_cmd.txt'

$stamp = Get-Date -Format 'HHmmssfff'
"$Command $stamp" | Out-File -Encoding ascii $tmp

& $adb -s $serial push $tmp /data/local/tmp/xbud_cmd.txt 2>$null | Out-Null
& $adb -s $serial shell "su -c 'cat /data/local/tmp/xbud_cmd.txt > /data/data/run.xbud.android/files/xbud_cmd; chmod 666 /data/data/run.xbud.android/files/xbud_cmd'" 2>$null | Out-Null

Start-Sleep -Milliseconds $WaitMs

$appPid = (& $adb -s $serial shell pidof run.xbud.android 2>$null) -join ''
if (-not $appPid.Trim()) {
    Write-Host 'app not running: start 小步点 first (the module only polls while the app is alive)' -ForegroundColor Yellow
    exit 0
}

$interesting = 'XbudLoc: (RESET|HOLD|GO |STATE|VIEWS done|V |LLLOG|DISTSTACK|TARGET|ORBIT|SPEED|MARKER|POLY|points n|POBJ|  P |PROJFROM|VIEW )|XbudLoc: cmd'
& $adb -s $serial logcat -d -t 600 |
    Select-String -Pattern $interesting |
    Select-Object -Last 25 |
    ForEach-Object { ($_.Line -split 'XbudLoc: ')[-1] }
