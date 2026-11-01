#requires -Version 5.1
param(
    [switch]$Check,
    [ValidateSet('track', 'campus')][string]$Mode = 'track'
)
<#
.SYNOPSIS
    一键开跑：建路线 -> 开跑 -> 跑到达标 -> 收尾 -> 重传 -> 打计数。

.DESCRIPTION
    前置条件只有两个：
      1. MuMu 已经启动、adb 连得上（脚本自己会 connect）
      2. 小步点停在首页（不一定非要首页，脚本自己会打开「学分跑」页）

    之后全部自动：读今天的「田径场」图钉 -> 生成南北向 400 m 椭圆 8 圈 -> 推送
    -> 起跑钉点(reset/hold) -> 播放 -> go -> 跑到 3.05 km -> 暂停 -> 长按结束
    -> 结束运动 -> 处理弹窗 -> 上传失败自动去「跑步记录」重传 -> 打印计数。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File D:\Work\xbud_toolkit\start.ps1

.EXAMPLE
    # 只检查环境，不跑
    powershell -NoProfile -ExecutionPolicy Bypass -File D:\Work\xbud_toolkit\start.ps1 -Check

.EXAMPLE
    # 不想在操场跑，改用校园随机单圈
    powershell -NoProfile -ExecutionPolicy Bypass -File D:\Work\xbud_toolkit\start.ps1 -Mode campus
#>

$ErrorActionPreference = 'Continue'
$toolkit = 'D:\Work\xbud_toolkit'
$env:PYTHONPATH = $toolkit
Set-Location -LiteralPath $toolkit

$argv = @("$toolkit\daily_run.py", '--mode', $Mode)
if ($Check) { $argv += '--check' }

& python @argv
exit $LASTEXITCODE
