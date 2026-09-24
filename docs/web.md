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

然后访问 `http://127.0.0.1:8000/admin`。管理员创建房间后，页面会给出 `/r/<room_id>` 邀请链接。玩家无需账号，选择昵称和座位，浏览器用 `HttpOnly` Cookie 保存房间身份。刷新页面后再次请求最新状态，并重新连接 WebSocket。

默认数据库在 `database/holdem.sqlite3`，可通过 `TEXAS_DB_PATH` 指向别的文件。数据库文件包含未公开底牌，不应放到公开的静态文件目录。

## 当前流程

1. 管理员登录、创建房间，设置大小盲、牌桌人数、买入后在桌筹码上限，以及自由昵称或预设昵称。
2. 把邀请链接发给朋友。玩家入座后在两手牌之间自行买入；同一昵称和座位不能重复占用。
3. 至少两名玩家有筹码后，管理员点击“开始下一手”。玩家通过牌桌操作，所有动作由服务端校验和持久化。
4. 每次操作后，服务端分别给各玩家推送其有权看到的牌局状态；未公开底牌不会发送给其他玩家。
5. 每位玩家可查看自己的买入、手牌、底牌和动作记录。行动有 30 秒倒计时，超时能过牌则过牌，否则弃牌。

## 权限与运行边界

- 管理员密码由环境变量提供；服务未配置密码时拒绝启动。管理员会话在进程内保存 12 小时，重启后重新登录。
- 房间链接使用随机房间 ID；知道链接的人可以尝试加入空座位。昵称不是身份证明，已加入玩家使用 Cookie 令牌恢复身份。
- WebSocket 同样检查玩家 Cookie，并逐个生成私人视图；跨来源浏览器请求被拒绝。
- 当前实时连接管理位于单个服务进程内。启动 Uvicorn 时使用一个 worker；未来多实例部署需要共享消息系统。
- 邀请朋友从公网访问之前，需要提供 HTTPS/WSS、可达的主机和备份方案。当前命令只在本机开放，尚未部署公网服务。
- 房间关闭、踢人、跨设备凭证重置和买入审批尚未实现；这些属于后续管理功能。

## 验证

安装开发测试依赖 `backend/requirements-dev.txt` 后运行：

```powershell
D:\miniconda\envs\Texas_Holdem\python.exe -B -X utf8 -m unittest discover -s backend/tests/engine -q
D:\miniconda\envs\Texas_Holdem\python.exe -B -X utf8 -m unittest discover -s backend/tests/integration -q
D:\miniconda\envs\Texas_Holdem\python.exe -B -X utf8 -m unittest discover -s backend/tests/api -q
```
