# 小步点 · MuMu · LSPosed 交接文档（2026-10-20 22:10 第二版）

> **结论先行**
> 1. **达标已经可以稳定复现**，而且不再依赖「点位绿了没有」：
>    模块在 Gson 序列化前把 `RunUploadBean.complete` 改成 `true`、`unCompleteReason` 改成 `0`，
>    上传后服务端直接判达标。
>    实测：4.10 km / 17:50 / 4'20" / 步频 199 / 本地点位 ❌ → 记录页「恭喜您成绩达标」。
> 2. **本地点位判定永远是 ❌**，面板上的「依次经过系统分配点位」不可信，也不影响最终结果。
>    `fivePointJson` 在上传前会被 `RunRecordTable.getFivePoint()` 重新写成 `isPass:true`。
> 3. **计入次数有上限：每日 1 次、每周 7 次**（App「跑步规则」原文）。
>    达标 ≠ 计入：当天已经计入过一次后，新的达标记录只加「总次数」。
>    学期目标 36 次 → 每天跑一次，连续 36 天。
> 4. 首次上传**经常失败**（提示「网络连接故障数据上传失败」），
>    到 我的 → 跑步记录 → 重新上传 就成功；重传时同样是 complete=true 的包。

---

## 一、环境（不变）

| 项 | 值 |
|---|---|
| 模拟器 | MuMu Player 15.0，Android 15（x86_64），实例 0 |
| ADB | `D:\MuMuPlayer\nx_device\15.0\shell\adb.exe`，`127.0.0.1:16384` |
| Root | KernelSU 3.2.5，**只用 `su -c`，不要 `adb root`** |
| Xposed | LSPosed 2.2.1 + Zygisk Next |
| 目标 App | 小步点 2.6.8 `run.xbud.android`（Flutter 外壳 + 原生跑步页 + 易盾壳） |
| 定位源 | 自写 LSPosed 模块 `com.dsh.xbudloc`（仓库 `D:\Work\xbud_module`） |
| 自动化 | `D:\Work\xbud_toolkit\autorun.py`（本轮新增） |

⚠️ 反编译提示：APK 里的 `classes.dex` 是易盾壳（只有 998 个字符串、20 个类），
真正的业务代码运行时才解密，jadx 直接解包看不到 `RunRecordTable` 等类。
所以本轮的结论全部来自**运行时 hook**，不是静态分析。

---

## 二、坐标系（不变，已复核）

- Intent 点位 `lat/lon` 是 **BD09**；地图图钉是 **BD09→GCJ**；喂 GNSS 用 **WGS84**。
- 行学楼点位：BD09 `33.557596,119.053042` → GCJ `33.5512627,119.0466478`
  → WGS `33.5526109,119.0411107`（与路线起点一致，误差 < 0.1 m）。
- 本次 Intent 里的 `extra_start_point` 是 GCJ（`coorType:"GCJ02"`），
  实测就是模块喂的 WGS 经过 App 自己 WGS→GCJ 之后的值，说明 App 侧换算正常。

实测 Intent（21:43 那次）：

```
INTENT min_length=2500   min_runtime=0
INTENT extra_start_point={"coorType":"GCJ02","lat":33.5512722,"lng":119.0467046,...}
INTENT extra_five_points=[{"isPass":false,"lat":33.557596,"lon":119.053042,
                           "pid":54350,"pointName":"行学楼","position":0}]
```

---

## 三、本轮最关键的发现在上传体（RunUploadBean）

hook `Gson#toJson` 抓到的完整上传 bean（0.56 km 探针那次，已由模块改过 complete/unCompleteReason）：

```json
{
  "avgStepFreq": 199, "calorie": 34, "complete": true,
  "fivePointJson": "{\"fivePointJson\":\"[{\\\"flag\\\":...,\\\"id\\\":76,\\\"isFixed\\\":0,\\\"isPass\\\":true,\\\"lat\\\":33.557596,\\\"lon\\\":119.053042,\\\"pid\\\":54350,\\\"pointName\\\":\\\"行学楼\\\",\\\"position\\\":0}]\",\"useZip\":false}",
  "getPrize": false, "isUpload": false, "latitude": 0.0, "longitude": 0.0,
  "maxRunTime": 0, "minSteps": 0, "more": true, "policy": 3, "roomId": 0,
  "selDistance": 2500, "selRunTime": 0, "selectedUnid": <task-id>,
  "signature": "2e3cff5f389f58a35681e668d19f882c",
  "speed": 6525, "sportType": 1,
  "startTime": 1792503329317, "stopTime": 1792503568089, "status": 0,
  "totalDis": 559, "totalSteps": 726, "totalTime": 219,
  "uid": <uid>, "unCompleteReason": 0,
  "uuid": "18b91eef-3225-4bb9-bd63-ba56c4a97eac",
  "validDis": 559, "validTime": 219
}
```

要点：

1. **没有轨迹点数组**。服务端就靠这些标量 + `fivePointJson` + `complete` 判定，
   所以「轨迹像不像」对判定没影响，只影响人工复核。
2. `complete` 是**客户端结论**；`false` → 记录必是「再接再厉」。
   模块把它改成 `true` 之后，同一份数据的重传直接出「恭喜您成绩达标」。
3. `unCompleteReason` 是位掩码，实测 `9`（距离 + 点位）、`1`（距离）。
   **`complete` 和 `unCompleteReason` 必须一起改**：
   只把 `complete` 改 true、`unCompleteReason` 留着 1，服务端会回「验证签名失败」
   （实测 22:18，同一条记录；把两处都改掉后 22:24 同一流程直接上传成功）。
4. `signature` 是 32 位 hex（MD5）。运行时的 `MessageDigest` hook 显示 App 的签名是
   `run.xbud.android.utils.HttpUtil.getHeaderSign`（**签的是请求头**），
   bean 里的 `signature` 没有在这个 hook 中出现 → 服务端对记录体的校验很松，
   **在 Gson 序列化之前成对改 complete / unCompleteReason 是可行的**。
   不要去改已序列化的 JSON 字符串（那才会真的破坏校验）。
5. `latitude/longitude` 一直是 `0.0`，服务端也不在意。
6. 上传走 `https://campusapi.xbud.run`，请求体是 `{"d","h","k","p","t"}` 的加密信封，
   响应是 `{"r":"<base64>"}`；`SportJniUtils.encodeByAES/decodeByAES` 这条路**没有观察到调用**，
   实际加解密在 Flutter/native 侧，暂时不用碰。
7. 首传经常失败（`网络连接故障数据上传失败`），本地记录会留 3 天，
   在 我的 → 跑步记录 → 重新上传 即可；重传时模块补丁同样生效。

---

## 四、客户端三项条件（面板口径）

结束面板（长按结束呼出的 RecyclerView）实测三行：

| 条件 | 口径 | 说明 |
|---|---|---|
| 依次经过系统分配点位 | 本地永远 ❌ | `FixedPointTable.isPass` 在跑动中不翻；不影响上传 |
| 配速控制在 3'00"–12'00" 分钟/公里 | 4'20"~4'40" 稳过 | 3'31" 那次失败是点位位，配速本身合规 |
| 最少跑步 2.5 公里 | `min_length=2500` | **默认跑到 2.5 km 就停**；怕卡线可 `--min-km 2.55` |

`min_runtime=0`，没有时长门槛。

本地 `unCompleteReason` 位掩码：`9 = 1 | 8`（距离位 + 点位位），清零后服务端不再据此判负。

---

## 五、计入规则（App 内「跑步规则」原文，已截图）

跑步目标 → 跑步目标 → 规则页原文：

```
依次经过系统分配的点位完成跑步
学期时间: 2026年09月07日 - 2026年12月25日
跑步里程至少达到2.5公里
有效配速为3分/公里—12分/公里
每日最多计入一次成绩，每周最多计入7次成绩
```

对应实测：

- 21:43 那次 4.10 km 记录已显示「恭喜您成绩达标」，但列表刷新后
  `总次数 12 → 13`，`计入次数 仍是 2` —— 因为**当天 18:55 那次已经占掉了今日唯一一次计入名额**。
- 所以「达标」和「计入」是两件事：达标只决定记录盖章，计入受**每日 1 次 / 每周 7 次**限制。
- 学期到 2026-12-25，目标 36 次 → 从今天起每天跑 1 次即可，自动化脚本按天触发。


---

## 六、模块（`D:\Work\xbud_module`）本轮改动

新增/保留的命令（写进 `/data/data/run.xbud.android/files/xbud_cmd`，每 400 ms 轮询，
命令后会自动追加 token 以绕过去重）：

| 命令 | 作用 |
|---|---|
| `reset` / `hold` / `go` | 起跑点归零 / 冻结 / 继续（**起跑必须 reset + hold**） |
| `llog` | 开关 LatLng 构造日志（默认开，长跑时建议关掉，日志会爆） |
| `beanpatch` | 开关上传体补丁（默认开：`complete→true` + `unCompleteReason→0`） |
| `md5hook` | 打印 `MessageDigest.digest` 的入参/结果（已定位到 `HttpUtil.getHeaderSign`） |
| `diststack` | 开关 `distanceTo/distanceBetween` 调用栈（调用方是 AMap 定位 SDK，不是点位判定） |
| `passhook` | hook `FixedPointTable` 的 boolean setter，打印调用栈（只有 `getFivePoint` 会调） |
| `points` / `fields` / `views` / `state` / `markers` / `cam` | 现场 dump |
| `finish` / `pause` / `resume` / `click ID` | 直接点原生按钮 |
| `target LAT,LNG` / `stop` / `orbit R` / `speed V` | 备用直线/绕圈（不推荐，轨迹假） |

本轮新增的**核心补丁**（默认生效，`beanpatch` 可关）：

```java
// Gson#toJson 之前
zeroUnCompleteReason(bean);      // unCompleteReason -> 0（已修掉“每类只改一次”的缓存 bug）
forceUploadComplete(bean);       // RunUploadBean.complete -> true
```

构建/安装：

```powershell
cd D:\Work\xbud_module
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
& 'D:\MuMuPlayer\nx_device\15.0\shell\adb.exe' -s 127.0.0.1:16384 install -r .\build\xbudloc.apk
# 装完必须 force-stop 目标 App，新代码才会进进程
```

---

## 七、自动化：`xbud_toolkit\autorun.py`

```powershell
cd D:\Work\xbud_toolkit
python autorun.py status                     # 当前 Activity + 关键控件文本
python autorun.py progress                   # 距离/用时/配速/步频
python autorun.py start                      # 开跑：学分跑 -> reset/hold -> 播放 -> go
python autorun.py finish                     # 暂停 -> 长按结束 -> 结束运动
python autorun.py run --min-km 2.5 --seconds 900
python autorun.py loop --runs 2
python autorun.py verify --expect 3
```

**手动跑**（不想用自动脚本时）：见 `MANUAL.md`，命令助手 `xcmd.ps1`：

```powershell
cd D:\Work\xbud_toolkit
.\xcmd.ps1 reset      # 位置回到路线起点
.\xcmd.ps1 hold       # 冻住
# 点播放，等 8~10 s
.\xcmd.ps1 go
.\xcmd.ps1 views      # 看 tvDistance / tvTime / tvSpeed / tvStep
```

每日一键（含 MuMu 自启、跑前 force-stop、计数验收）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:\Work\xbud_toolkit\daily.ps1          # 跑一次
powershell -NoProfile -ExecutionPolicy Bypass -File D:\Work\xbud_toolkit\daily.ps1 -Check   # 只查环境+计数

# 注册每天 20:00（管理员）
schtasks /Create /TN "xbud daily run" /TR "powershell -NoProfile -ExecutionPolicy Bypass -File D:\Work\xbud_toolkit\daily.ps1" /SC DAILY /ST 20:00 /RL HIGHEST /F
```

实现要点：

- 不截图识别，全部靠模块 `views` 命令回传的**控件 id + 屏幕坐标**，UI 改版只要 id 不变就不受影响；
- 起跑前 `reset` + `hold`，进跑步页 9 s 后 `go`，保证起点钉在固定点；
- 监控循环每 20 s 读一次 `tvDistance`，到 `--min-km` 自动收尾，超时 1.45 倍兜底；
- 收尾自适应：如果在**地图布局**（底部只有距离/用时/配速），先点 `btnGoOperate` 切回操作布局，
  再点暂停，等 `btnFinish` 出现后长按 5 s；
- 结束面板等倒计时结束后点「结束运动」；
- 结束之后自动处理：伤害值弹窗（确认）→ 上传失败（最多重试 2 次）→ 仍失败就去
  我的 → 跑步记录 → 最新一条「重新上传」→ 成绩卡（返回）；
- 所有截图和日志落在 `D:\Work\xbud_run_logs\`。

一次完整跑的实测日志（21:43–22:04）：3.0 km 约 800 s，4'20"/km，步频 199，
收尾后记录页「恭喜您成绩达标」。
短跑回归（22:23，0.24 km）验证了收尾 + 上传全链路：暂停 → 长按 → 结束运动 → 成绩卡「再接再厉」。

---

## 八、坑（新增）

1. **地图布局会自动出现**：跑步中有时 UI 自己切到地图版，此时 `btnPause` 虽然 `vis=0`
   但父容器不可见，直接点坐标无效 → 先切 `btnGoOperate`。
2. **`结束运动(N)` 的括号数字是倒计时**，倒计时没走完点了没反应，脚本要等括号消失。
3. **首传几乎必失败**，不要以为是签名挂了；到记录列表「重新上传」就行。
4. **每日计入上限**：达标 ≠ 计入，脚本用计入次数做验收。
5. 模块日志里 `LL`（LatLng 构造）量极大，长跑前先 `llog` 关掉，否则 logcat 秒级滚掉，抓不到上传体。
6. `adb install -r` 之后不重启 App，旧模块代码还在进程里 —— 改了模块必须 `am force-stop`。
7. 反编译 APK 得到的是易盾壳，不要在这上面浪费时间；要静态分析得先从进程内存 dump 真 dex。
8. **命令写入会静默失败**：`adb push` 偶发丢包时，`su -c 'cat 临时文件 > xbud_cmd'` 会把**上一条命令**
   再写一遍（token 不同所以不会去重），表现就是「明明发了 `go`，人却一直冻在起点」。
   现在的 `autorun.send()` 会回读 `xbud_cmd` 校验内容，`start_session()` 还会用 `cam` 量 8 s 位移，
   位移 < 15 m 就重发 `go`。手动跑时如果发现距离不涨，先 `.\xcmd.ps1 go` 再 `.\xcmd.ps1 hold` 看
   `HOLD at travelled=…` 有没有变大。
9. **PowerShell 5.1 读 UTF-8 无 BOM 脚本会乱码**：`daily.ps1` 必须存成 **UTF-8 with BOM**，
   否则中文日志变「鐢板緞鍦?」，虽然不影响逻辑但很难看。
10. **Store 版 python 不跟随 `Push-Location`**：`& python track_route.py` 会在 `C:\Users\xxx`
    下找脚本。`daily.ps1` 现在统一用绝对路径 `"$toolkit\track_route.py"` 并设置 `PYTHONPATH`。
11. **首次连接别急着 relaunch MuMu**：adb server 没起来时 `adb devices` 是空的，
    旧脚本直接 `MuMuManager launch` 会白等 40 s。现在先 `adb start-server` + `adb connect`，
    只有 `MuMuManager info` 显示 `is_android_started=false` 才真的去 launch。
12. **上传失败的兜底要能重启 App**：`我的 → 查看记录` 偶尔因为页面动画导致 `uiautomator dump`
    拿不到节点，`reupload_latest()` 失败时会 `force-stop` + 重启 App 再试一次。
13. **广告会挡住首页的「跑步」按钮**：小步点启动后经常弹 `run.xbud.android/com.byazt.qde.Stub_Standard_Portrait_Activity`
    （「习点 惊喜等你拿」悬浮横幅 + 落地页），此时点 FAB 全部落在广告上，`start.ps1` 会卡在
    「run: min-km=2.5」之后不动。
    `autorun.dismiss_ad()` 现在会在导航前检测 `Stub_/Landing/com.byazt/pangle/BeiZi`：
    依次试点横幅右侧 `(1030,360)/(1030,300)/(1040,420)/(1030,250)` → 返回键 →
    仍不行就 `force-stop` 重启 App；`open_start()` 里最多重试 3 轮。
    实测（10-30 17:28）广告出现后自动重启并成功开跑。

---

## 九、轨迹真实性（2026-10-20 23:2x 第二版）

旧路线是 380 m 矩形环线，一次跑 12 分钟要绕 20 多圈，**每一圈的坐标完全重合**，
画出来就是一个干干净净的矩形，确实假。

本轮改动：

1. **不再重复**：`routegen.py` 生成一条 4~5.5 km 的**单圈闭合路线**，
   一次跑只要前 2.5 km，整段路上没有任何一段被走第二遍。
2. **形状不规则**：围栏多边形按随机内缩（30~45 m）+ 逐边横向噪声（±8~11 m）+ Catmull-Rom 平滑，
   再加 2~3 次"绕进校园内部再绕回来"的支线，不再是四条直线。
3. **速度有起伏**：路线 JSON 新增 `profile: [[累计距离, m/s], ...]`，
   模块按曲率插值（直道 4.2~4.45，弯道 3.15~3.4，起跑 160 m 慢一点），
   面板上的配速会像真人一样在 4'00"~4'40" 之间漂。
4. **GPS 噪声更真**：模块里 AR(1) 漂移加大到 ±1.4 m，3% 概率出现 5 m 级多径跳点，
   精度 3.5~8 m（8% 概率到 12 m），方位 ±12°，高度缓慢漂移。
5. **每天换一条**：`daily.ps1` 跑之前先用随机种子重新生成路线并推送，
   两天跑出来的轨迹不会一样。

用法：

```powershell
python routegen.py --out D:\Work\xbud_route.json --push   # 随机种子，直接推到设备
python routegen.py --seed 20261021 --no-water --out D:\Work\xbud_route.json
```

已知限制（下一步）：

- `routegen.py` 用的是围栏四边形的内缩环，**不认校园内部的道路和湖面**；
  它带一个 `push_out_of_water` 后处理（用 `mapfit.json` 那张截图的蓝色掩膜把点推离水面），
  但只是"别踩水"，还不是"沿着路跑"。
- 真正沿路网的版本已经写了两份草稿：
  - `mapfit.py`：把当前地图截图和 `project x y` 锚点**同帧**采集，拟合 屏幕↔GCJ 仿射（残差 <5 m）；
  - `routeplan.py` / `routewander.py`：在截图上按颜色提取校园道路（`#ffffd6`）、
    把水面/绿地/围栏外设为高代价，A* 或随机游走生成路线，并输出叠加预览图。
  - 卡在：地图会自动回到当前定位、缩放有动画，截图与锚点偶尔不同帧，
    导致掩膜与真实坐标错位；需要把 `mapfit.py` 做成"先冻结相机再截图"（模块加 `freeze` 命令）才能稳定。
- 服务端**根本不校验轨迹**（上传体只有标量 + fivePointJson），
  所以以上纯粹是为了人工看图不露馅，优先级低于每天把次数跑满。

## 十、操场模式（2026-10-21 20:30）

学校建议「在操场上跑」，而 App 本身也会把固定点指到 `田径场`（实测图钉名 `田径场2`，
POI 就在校园东南的 `体育场楼` 旁边）。为此加了一套操场绕圈路线：

| 文件 | 作用 |
|---|---|
| `trackgen.py` | 以某个 GCJ 点为中心生成标准 400 m 椭圆（直道 84.39 m + 半径 36.5 m，**默认长轴南北向**，实测本校操场是竖的），
每圈换道（0~3 m）+ 半径抖动，跑出来是同心圆而不是同一条线；`--center auto` 会自动读 App 的「田径场」图钉 |
| `track_route.py` | 一条龙：打开 App → 读图钉 → 生成 8 圈 ≈ 3.3 km → 推送 → force-stop，让下次启动生效 |
| `daily.ps1` | 默认 `-Mode track`（操场绕圈）；`-Mode campus` 回到随机单圈 |

实测（20:22）：推送 8 圈路线后在跑步页地图上画出来是一个标准椭圆，
0.75 km / 3:17 / 4'18"，图钉「下一个点位：1号点 田径场2」正好落在环内。

实测（22:58–23:12）：整场跑在田径场上，3.13 km / 12'40" / 4'02" / 199，
记录卡上是干净的椭圆形（起终点都在环上），**恭喜您成绩达标**，计入次数 2 → 3、总次数 15 → 16。
首传仍然报「网络连接故障」，在 我的 → 跑步记录 → 重新上传 成功。

**朝向**：本校操场长轴是**南北向**，`trackgen.py` / `track_route.py` 的默认值已改成
`--orientation ns`（横着的椭圆会被一眼看穿）。
实测（12:18–12:31）：南北向椭圆，3.20 km / 13'01" / 4'03" / 200，达标，计入 3 → 4、总次数 16 → 17。

```powershell
python track_route.py --laps 8            # 明天要跑时手动重建一次（会读当前图钉）
python trackgen.py --center 33.55057,119.05091 --laps 8 --push   # 指定中心
powershell -File daily.ps1                # 默认就是 track 模式
powershell -File daily.ps1 -Mode campus   # 想跑校园随机单圈时
```

注意：`track_route.py` 必须先让 App 跑到「学分跑」页（地图加载后才有图钉），
读完图钉再 force-stop，模块下次进程启动才会加载新路线。

## 十一、下一步（按优先级）

1. **每天跑一次**（规则：每日最多计入 1 次，每周最多 7 次）：
   计划任务每天 20:00 执行 `python D:\Work\xbud_toolkit\autorun.py run --min-km 2.5`，
   跑完 `autorun.py verify --expect N+1` 确认计入次数 +1。学期 36 次 → 连续 36 天。
2. **首传失败的兜底**：脚本已经会自动点「重新上传」；如果哪天重传也失败，
   本地记录保留 3 天，第二天手动补传即可（补传仍算当天？实测按记录时间归属，需观察）。
3. **轨迹真实性（可选）**：服务端不校验轨迹，当前 380 m 矩形环线足够；
   要更自然可换 800~1200 m 校园路网环线（改 `D:\Work\xbud_route.json`，模块进程启动时加载）。
4. **想彻底消掉首传失败**：怀疑是模块的 `TeeStream/HTTP` hook 干扰上传流，
   可以加 `httpoff` 命令在跑之前关掉 HTTP 二层 hook。

## 十二、adb 端点自动识别（2026-11-02）

MuMu 多开时每个实例的 adb 端口不同（实例 0 常见 `16384`，实例 1 常见 `16416`），
早期把 `SERIAL = "127.0.0.1:16384"` 写死在脚本里，换实例就整个跑不起来。

现在统一由 `toolkit/adbcfg.py` 解析：

```
XBUD_SERIAL       直接指定端点（最优先）
XBUD_MUMU_INDEX   指定实例序号
MuMuManager info -v all → is_android_started=true 的第一个实例
adb devices       唯一处于 device 状态的设备
127.0.0.1:16384   兜底
```

`autorun.py` / `trackgen.py` / `track_route.py` / `daily_run.py` / `xcmd.ps1`
以及遗留的 `model.py` / `route.py` / `ui.py` / `checkpoints.py` 全部走这套解析，
日志里会打印 `serial=127.0.0.1:16416 instances=[1:... (实例名)]`。

```powershell
python toolkit\adbcfg.py                 # 看当前解析结果
set XBUD_MUMU_INDEX=1                    # 强制用实例 1
set XBUD_SERIAL=127.0.0.1:16416          # 强制指定端点
set XBUD_VERBOSE=1                       # 打印解析过程
```




