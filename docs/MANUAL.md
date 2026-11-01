# 手动跑一次（1080×1920，MuMu 实例 0）

自动脚本坏掉、或者你想自己掌控节奏时，按这个来。全程只需要电脑上敲 3 条命令
（`reset` / `hold` / `go`），其余都在模拟器里点。

---

## 0. 开跑前

```powershell
# MuMu 起来了、adb 连上
& 'D:\MuMuPlayer\nx_device\15.0\shell\adb.exe' connect 127.0.0.1:16384

# 操场模式（推荐）：打开 App 读「田径场」图钉 -> 生成 8 圈 400 m -> 推送 -> 关掉 App
python D:\Work\xbud_toolkit\track_route.py --laps 8

# 或者校园随机单圈（不想在操场跑时）
python D:\Work\xbud_toolkit\routegen.py --out D:\Work\xbud_route.json --push

# 让 App 重新加载路线/模块：必须 force-stop 一次
& 'D:\MuMuPlayer\nx_device\15.0\shell\adb.exe' -s 127.0.0.1:16384 shell am force-stop run.xbud.android
```

然后打开小步点（首页）。

> 操场模式说明：路线是标准 400 m 椭圆（直道 84.39 m、半径 36.5 m），
> 每圈换道 0~3 m，跑出来是一圈套一圈的同心椭圆。
> `track_route.py` 会自动读 App 里「田径场」图钉当圆心；如果哪天图钉换了位置，重跑一次即可。

> 命令助手：`D:\Work\xbud_toolkit\xcmd.ps1`，用法 `.\xcmd.ps1 reset`。
> 它会把命令写进 App 私有目录并回显模块日志。

---

## 1. 进「学分跑」开始页

1. 点底部中间黄色圆钮「跑步」（约 `540,1846`）。
2. 确认顶部选中的是 **学分跑**（不是休闲跑）；不是就点 `714,138`。
3. 地图上应能看到「固定点」图钉。

## 2. 钉住起点（关键）

```powershell
cd D:\Work\xbud_toolkit
powershell -NoProfile -ExecutionPolicy Bypass -File .\xcmd.ps1 reset   # 位置回到路线起点
powershell -NoProfile -ExecutionPolicy Bypass -File .\xcmd.ps1 hold    # 冻住，不会提前跑
```

日志应出现 `RESET to 33.55...,119.04...` 和 `HOLD at travelled=0`。

## 3. 开跑

1. 点黄色播放键（约 `540,1715`），进入跑步页。
2. **等 8~10 秒**，让 App 把起点定在这个位置，然后：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\xcmd.ps1 go
```

日志出现 `GO travelled=...` 就开始走了。

## 4. 跑够距离

- 目标：**距离 2.5 km 就够**（规则下限），脚本默认到 2.5 km 自动收尾；怕卡线可以 `--min-km 2.55`。
- 正常节奏：配速 4'00"~4'40"、步频 190~200，**大约 12~13 分钟**。
- 中途想看数据（不用切界面）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\xcmd.ps1 views
# 关注 tvDistance / tvTime / tvSpeed / tvStep 四行
```

- 想停一下：`.\xcmd.ps1 hold`；继续：`.\xcmd.ps1 go`。

## 5. 收尾（顺序别错）

1. 如果当前是**地图版**（底部只有 距离/用时/配速，没有暂停键）：先点左下角箭头 `108,1368`
   切回操作版。
2. 点**暂停**（`540,1659`）。
3. **长按**「结束」（`354,1659`）**5 秒**——不是点，是长按。
4. 弹出要求面板（点位 ❌ 很正常），等按钮上的 `(2)(1)` 倒计时消失，点**结束运动**（`282,1785`）。
5. 弹「伤害值…」→ 点**确认**（`540,1519`）。
6. 出现成绩卡：黄色「恭喜您成绩达标」= 成功；红色「再接再厉」= 没达标（看下面）。

## 6. 上传失败怎么办

大概率第一次会提示 **「网络连接故障数据上传失败」**（这是 App 的老毛病，不是补丁坏了）：

1. 关闭提示 → 底部 **我的**（`970,1885`）。
2. 「累计跑步」那行的 **查看记录**（约 `886,993`）。
3. 最新一条记录右侧 **重新上传**。
4. 传完点开记录，应显示「恭喜您成绩达标」。

## 7. 验收

- 记录页顶部：`计入次数` 比跑之前 +1。
- 首页「课外跑 N 次」可能滞后，以记录页为准。
- **每天只计入 1 次**（每周 7 次）；当天再跑只会加「总次数」。

---

## 常见问题

| 现象 | 处理 |
|---|---|
| 进首页弹「您有跑步未完成」 | 点**放弃**，重新来 |
| GPS 三格不亮 / 「无法获取GPS定位」 | 重启 App；仍不行 `.\xcmd.ps1 state` 看模块是否在供点 |
| 面板「依次经过系统分配点位 ❌」 | 不用管，最终判定看记录页 |
| 结束按钮点了没反应 | 那是**长按** 5 秒；倒计时没走完也点不动 |
| 跑步页自己跳到地图版 | 点左下箭头 `108,1368` 切回操作版再暂停 |
| 记录「再接再厉」 | 看「未达标原因」：距离不够就多跑，配速超范围就调 `profile` |
| 想换路线 | `python routegen.py --seed 123 --out D:\Work\xbud_route.json --push` 后 force-stop 重进 |

## 参数速查

| 项 | 值 |
|---|---|
| 最低里程 | 2500 m（默认跑到 2500 m 停） |
| 有效配速 | 3'00"–12'00" /km |
| 步频目标 | 190~200 spm（模块固定 ~200） |
| 计入上限 | 每日 1 次、每周 7 次、学期 36 次 |
| 学期 | 2026-09-07 ~ 2026-12-25 |
| 命令文件 | `/data/data/run.xbud.android/files/xbud_cmd` |
| 路线文件 | `/data/data/run.xbud.android/files/xbud_route.json` |

