# 网页牌桌（本地运行）

本阶段提供管理员管理页、邀请入座页、牌桌、个人记录、HTTP 接口和 WebSocket 实时同步。使用 FastAPI、Uvicorn 与浏览器原生 JavaScript；后端仍采用现有 SQLite 数据库与游戏引擎。

## 启动

在项目根目录，用项目 Conda 解释器安装依赖：

```powershell
D:\miniconda\envs\Texas_Holdem\python.exe -m pip install -r backend/requirements.txt
```

在**当前 PowerShell 窗口**设置至少 12 位、只有你知道的管理员密码，并启动本地服务：

```powershell
$env:TEXAS_ADMIN_PASSWORD = '请替换成你自己的至少十二位强密码'
D:\miniconda\envs\Texas_Holdem\python.exe -m uvicorn backend.app.api.server:create_app --factory --host 127.0.0.1 --port 8000
```

然后访问 `http://127.0.0.1:8000/admin`。管理员创建房间后，页面会给出 `/r/<room_id>` 邀请链接。玩家无需账号，先选择昵称进入旁观区，再点击空座位上的加号入座。浏览器用 `HttpOnly` Cookie 保存房间身份。刷新页面后再次请求最新状态，并重新连接 WebSocket。

默认数据库在 `database/holdem.sqlite3`，可通过 `TEXAS_DB_PATH` 指向别的文件。数据库文件包含未公开底牌，不应放到公开的静态文件目录。

## 当前流程

1. 管理员登录、创建房间，设置大小盲、人数、买入上限、各阶段与全下发牌投票时限，以及昵称规则。默认时限依次为翻前 60 秒、翻牌 60 秒、转牌 120 秒、河牌 180 秒、投票 60 秒。
2. 把邀请链接发给朋友。玩家可先旁观；入座后在两手牌之间自行买入。同一有效昵称和座位不能重复占用。站起后筹码留在该昵称下，换座不会重置筹码。
3. 入座并买入后勾选准备。满桌时全员准备自动开局；未满桌时全体入座玩家还需分别确认“现在开局”。准备状态跨手牌保留，结算后约 4 秒自动开始下一手；少于两名已准备且有筹码的玩家时停止。首手庄位随机抽取，后续按实体座位轮转；牌桌在庄家座位前方显示 D 按钮。
4. 每次操作后，服务端分别给各玩家推送其有权看到的牌局状态；未公开底牌不会发送给其他玩家。
5. 牌桌左侧显示按已结算筹码计算的昵称盈亏，右侧可按手牌编号或牌面查找自己的记录。正在参与本手的玩家不能换座、站起或退出；未参与本手的旁观者可以入座，等待下一手。
6. 倒计时秒数与百分比对全桌公开。当前行动玩家在最后 5 秒可以单次延时，增加该阶段的完整时限。超时能过牌则过牌，否则弃牌。全下且后续无人能下注时，入池玩家投票发一次或两次；超时视为一次。每个底池按有资格争夺该池的玩家分别决定，发两次的底池均分后分别判牌（奇数筹码给第一次）。
7. 管理员只能在玩家行动或全下投票决策期间暂停；恢复时继续同一决策的剩余时间。退出牌桌会注销当前身份，将剩余筹码记录为离桌兑出并清除浏览器令牌。
8. 点击“开启声音”后，轮到自己行动会播放提示音，动作会随机播放对应类别音效。音频放入 `frontend/src/audio/`，按其中说明命名；没有提示音文件时会生成短提示音。

## 权限与运行边界

- 管理员密码由环境变量提供；服务未配置密码时拒绝启动。管理员会话在进程内保存 12 小时，重启后重新登录。
- 房间链接使用随机房间 ID；知道链接的人可以尝试加入空座位。昵称不是身份证明，已加入玩家使用 Cookie 令牌恢复身份。
- WebSocket 同样检查玩家 Cookie，并逐个生成私人视图；跨来源浏览器请求被拒绝。
- 当前实时连接管理位于单个服务进程内。启动 Uvicorn 时使用一个 worker；未来多实例部署需要共享消息系统。
- 手机端牌桌采用纵向椭圆并缩小卡牌和座位，桌面区域无需横向滑动；盈亏和个人记录仍排列在牌桌下方。
- 邀请朋友从公网访问之前，需要提供 HTTPS/WSS、可达的主机和备份方案。当前命令只在本机开放，尚未部署公网服务。
- 房间关闭、踢人、跨设备凭证重置和买入审批尚未实现；这些属于后续管理功能。
- 从旧版启动时，数据库自动迁移到 v3 结构；重启已有的服务进程后才会执行迁移。升级前可先备份 `database/holdem.sqlite3`。

## 验证

安装开发测试依赖 `backend/requirements-dev.txt` 后运行：

```powershell
D:\miniconda\envs\Texas_Holdem\python.exe -B -X utf8 -m unittest discover -s backend/tests/engine -q
D:\miniconda\envs\Texas_Holdem\python.exe -B -X utf8 -m unittest discover -s backend/tests/integration -q
D:\miniconda\envs\Texas_Holdem\python.exe -B -X utf8 -m unittest discover -s backend/tests/api -q
```
