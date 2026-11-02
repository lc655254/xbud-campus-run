# xbud-campus-run

小米步数…不是。这是 **小步点（run.xbud.android）校园跑** 的一套自用工具：
一个 LSPosed 模块负责喂定位与步频，一套 Python 脚本负责把那趟跑「跑」得像个正常人。

> 目标不是破解 App，而是让模拟器里的这一趟：起点在分配点位、轨迹像在操场绕圈、
> 配速步频合理、上传体自洽，最后在记录页拿到「达标」。

---

## 它做了什么

```
┌──────────────────────┐        ┌───────────────────────────┐
│ LSPosed 模块          │        │ 电脑侧脚本                 │
│ com.dsh.xbudloc      │        │ xbud_toolkit/*.py          │
│                      │        │                            │
│ LocationManager hook │◀──────▶│ 发 reset/hold/go 命令      │
│ 合成 GNSS fix        │  文件   │ 读控件树判断界面状态        │
│ 传感器 hook 合成步频  │  通道   │ 生成路线/配速曲线           │
│ 上传体补丁           │        │ 点按钮、收尾、重传、验收     │
└──────────────────────┘        └───────────────────────────┘
```

模块侧：

- 接管 `LocationManager`（`getLastKnownLocation` / `requestLocationUpdates` / providers），
  按 500 ms 派发合成 `Location`：`isMock=false`、12 星、精度 3~8 m、速度/方位/时间戳齐全；
- 接管加速度计，用正弦合成步频（`CADENCE_HZ` → ~200 spm）；
- 按路线 JSON 前进，支持**逐点速度曲线**（`profile`）与 GPS 抖动（AR(1) + 偶发多径跳点）；
- 在 `Gson#toJson` 之前把 `RunUploadBean` 的 `complete` 置 true、`unCompleteReason` 置 0；
- 自带一堆现场探针：`views` / `points` / `markers` / `cam` / `polygon` / `passhook` / `md5hook`。

脚本侧：

- `trackgen.py`：生成标准 **400 m 椭圆**（直道 84.39 m、半径 36.5 m），一圈换一次道，
  默认长轴南北向；8 圈 ≈ 3.3 km；`--center auto` 直接读 App 里「田径场」图钉当圆心；
- `routegen.py`：不想在操场跑时，生成一条 4~5.5 km 的校园随机单圈（围栏内缩 + 横向噪声 + 支路），
  带「别踩湖」后处理；每天换种子，两天不重样；
- `autorun.py`：全流程状态机——开「学分跑」→ `reset/hold` 钉住起点 → 播放 → 9 s 后 `go`
  → 跑到 2.5 km → 暂停 → 长按结束 → 结束运动 → 处理广告/弹窗 → 上传失败自动去记录页重传；
- `daily_run.py` / `start.ps1`：一条命令跑完上面全部，并打印跑前/跑后计数。

## 目录

```
module/               LSPosed 模块（Java，约 2700 行）
  src/com/dsh/xbudloc/Module.java
  stubs/             编译用的 Xposed 接口桩
  build.ps1          javac + d8 + aapt2 + apksigner 一键打包
  AndroidManifest.xml
toolkit/             电脑侧脚本
  adbcfg.py          自动识别「当前运行的 MuMu 实例」及其 adb 端口
  autorun.py         核心状态机（开跑/监控/收尾/重传）
  trackgen.py        400 m 操场椭圆路线
  track_route.py     读图钉 → 生成 → 推送 → 重启 App
  routegen.py        校园随机单圈路线
  routeplan.py       截图路网提取 + A*（实验）
  routewander.py     截图掩膜随机游走（实验）
  mapfit.py          截图 ↔ GCJ 仿射拟合（实验）
  daily_run.py       每日一键（Python 实现）
  start.ps1          每日一键（PowerShell 薄封装）
  daily.ps1          同上（旧入口）
  xcmd.ps1           手动模式下给模块发命令
  cadence.py route.py model.py ui.py pin.py checkpoints.py xbud_campus.js
                     最早基于 FakeLocation 的一版，留作参考
docs/
  HANDOVER.md        完整的逆向与联调记录（坑、判定模型、上传体字段）
  MANUAL.md          手动跑一遍的 SOP
```

## 环境

| 项 | 值 |
|---|---|
| 模拟器 | MuMu Player 15（Android 15 x86_64），KernelSU root |
| ADB | `D:\MuMuPlayer\nx_device\15.0\shell\adb.exe`，端口**自动识别** |
| Xposed | LSPosed + Zygisk Next |
| 目标 App | 小步点 2.6.8 `run.xbud.android` |

### 多开 / 端口自动识别

MuMu 每个实例都有自己的 adb 端口（实例 0 常见 `16384`，实例 1 常见 `16416`），
`adbcfg.py` 会读 `MuMuManager.exe info -v all`，挑**真正在运行**的那个实例：

```powershell
python toolkit\adbcfg.py
# serial=127.0.0.1:16416  instances=[1:127.0.0.1:16416(实例名)]
```

优先级：

1. `XBUD_SERIAL`：直接指定端点，如 `set XBUD_SERIAL=127.0.0.1:16416`
2. `XBUD_MUMU_INDEX`：指定实例序号，如 `set XBUD_MUMU_INDEX=1`
3. MuMuManager 里 `is_android_started=true` 的第一个实例
4. `adb devices` 里唯一处于 `device` 状态的设备
5. 兜底 `127.0.0.1:16384`

所有脚本（`autorun.py` / `trackgen.py` / `track_route.py` / `daily_run.py` / `xcmd.ps1` …）
都走这套解析，换实例、换机器不用再改代码。剩下的路径常量只有
`adbcfg.py` 的 `ADB/MUMU`、`daily_run.py` 的 `TOOLKIT`、`module/build.ps1` 的 `$sdk`。

## 快速开始

```powershell
# 1) 编模块并装上（改完源码要重装 + force-stop 目标 App 才会生效）
cd module
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
adb -s 127.0.0.1:16384 install -r .\build\xbudloc.apk

# 2) 每日一键：建路线 → 开跑 → 收尾 → 重传 → 打计数
powershell -NoProfile -ExecutionPolicy Bypass -File ..\toolkit\start.ps1

# 只检查环境
powershell -NoProfile -ExecutionPolicy Bypass -File ..\toolkit\start.ps1 -Check

# 不想在操场跑
powershell -NoProfile -ExecutionPolicy Bypass -File ..\toolkit\start.ps1 -Mode campus
```

手动跑（自己点播放、自己收尾）：

```powershell
cd toolkit
.\xcmd.ps1 reset     # 位置回到路线起点
.\xcmd.ps1 hold      # 冻住，别提前跑
# 点播放，等 8~10 秒
.\xcmd.ps1 go
.\xcmd.ps1 views     # 看 tvDistance / tvTime / tvSpeed / tvStep
```

详细步骤见 [docs/MANUAL.md](docs/MANUAL.md)，踩坑与逆向结论见 [docs/HANDOVER.md](docs/HANDOVER.md)。

## 判定是怎么过的（简版）

服务端只看 `RunUploadBean` 的标量字段，**没有轨迹数组**：

```
totalDis / totalTime / validDis / speed / avgStepFreq
fivePointJson（isPass 由 getFivePoint() 序列化时置 true）
complete / unCompleteReason / policy / selDistance
```

客户端把 `complete` 写成 true、`unCompleteReason` 清零，这一趟就被判「恭喜您成绩达标」。
本地点位判定永远是 ❌，不用管它——面板只是闸门，不是结论。

计入次数另有规则：**每日 1 次、每周 7 次**，学期目标 36 次；
达标 ≠ 计入，当天已计入后新的达标记录只加「总次数」。

## 免责

仅供学习交流，别拿去交作业以外的用途。跑坏了蓝屏不负责。
