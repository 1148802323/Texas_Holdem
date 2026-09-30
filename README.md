# Texas Hold’em 私人牌桌

一个供朋友使用的网页版德州扑克项目。房主通过受密码保护的管理页创建牌桌并分享邀请链接；玩家不必注册账号，设置昵称后可先旁观，再选择座位、买入虚拟筹码并参与对局。项目包含独立规则引擎、FastAPI 服务、原生 JavaScript 前端和 SQLite 持久化，目前已部署 [线上牌桌](https://poker.8u7hybby.com)。

当前版本为 **v0.9.0-beta.3**。牌桌支持 2–9 人；真实资金、支付和玩家账号系统均不在当前范围内。

## 已实现功能

- **房间与身份**：管理员设置大小盲、座位数、买入后在桌筹码上限、可用昵称及各阶段决策时间，并生成邀请链接。玩家可以旁观、入座、站起换座或退出；昵称与筹码绑定，参与当前手牌时不能换座或离桌。身份由 30 天有效的 HttpOnly Cookie 保存，换设备可使用管理员签发的一次性恢复码。
- **连续牌局**：入座玩家买入后选择准备；满桌时全员准备即可开局，未满桌时还须全体入座玩家确认。结算后自动开始下一手，直到人数不足或管理员暂停。首手随机抽取庄位，后续按实体座位轮转；管理员可在玩家决策或全下发牌投票期间暂停，恢复后继续剩余时间。
- **规则与结算**：服务端逐次验证过牌、跟注、下注、加注、弃牌和全下；处理短码盲注、最小加注、短码全下加注权、未跟注筹码返还、边池、平局和筹码守恒。全下后可投票发一次或两次，各底池按有资格争夺该池的玩家分别决定发牌次数。
- **牌桌体验**：椭圆座位围绕公共牌；每位入座玩家从自己的座位位于下方的视角看牌桌。当前行动者高亮，牌桌显示公开倒计时、百分比和行动按钮；行动最后 5 秒可对本次决策延时一次。提供发牌过渡、可选提示音和动作音效；手机端采用纵向布局。
- **记录与聊天**：SQLite 保存房间、玩家、买入、手牌及结算数据；左侧可查看昵称盈亏，右侧可查询自己的手牌记录。房间聊天支持入座玩家和旁观者、未读提示与断线重连，但**聊天只在服务进程内存中保存**：删除牌桌或重启服务都会清空，不进入数据库。
- **管理与安全**：管理员可查看玩家在线状态，签发恢复码、强制离座、移出玩家、暂停或恢复游戏、关闭牌桌。关闭牌桌后停止新玩家加入，并保留数据库中的历史。服务端按玩家身份生成可见状态；他人的未公开底牌和剩余牌堆不会发送到玩家接口。刷新页面可恢复当前身份和手牌进度。

## 运行方式

在项目根目录使用 Python 3.11 或更新版本。以下示例适用于 PowerShell；如使用已有的 `Texas_Holdem` Conda 环境，可把 `python` 换成 `D:\miniconda\envs\Texas_Holdem\python.exe`。

```powershell
python -m pip install -r backend/requirements.txt
$env:TEXAS_ADMIN_PASSWORD = '请替换成至少 12 位的私密管理密码'
python -m uvicorn backend.app.api.server:create_app --factory --host 127.0.0.1 --port 8000
```

上述命令只监听本机的 127.0.0.1。在运行服务的同一台电脑打开管理页，可创建房间并验证网页功能。要邀请外网朋友游玩，请先将服务部署到可公开访问的服务器，再从公网管理页创建房间并分享公网邀请链接。默认数据库是 `database/texas_holdem.sqlite3`，首次启动时自动初始化；可用 `TEXAS_DB_PATH` 指定其他位置。数据库包含未公开手牌，不应放在网页静态目录或提交到 Git。

本机命令只监听 `127.0.0.1`。公网部署需要 HTTPS/WSS、单个 Uvicorn worker、进程服务与数据库备份；服务器配置和逐步操作见 [部署指南](docs/deployment.md)。更完整的玩法、权限和聊天生命周期见 [网页牌桌说明](docs/web.md)。

## 项目结构

| 路径 | 作用 |
| --- | --- |
| [`backend/app/engine/`](backend/app/engine/) | 不依赖 HTTP 或数据库的卡牌、合法操作、状态机、牌型评估与结算 |
| [`backend/app/services/storage.py`](backend/app/services/storage.py) | SQLite 房间服务、玩家身份、买入、手牌快照、恢复与历史查询 |
| [`backend/app/api/server.py`](backend/app/api/server.py) | FastAPI 管理与玩家接口、WebSocket 同步、超时处理及内存聊天 |
| [`frontend/index.html`](frontend/index.html)、[`frontend/src/`](frontend/src/) | 管理页和牌桌入口、原生 JavaScript、聊天、样式和音效 |
| [`database/migrations/`](database/migrations/) | SQLite 数据库迁移；真实数据库文件不纳入版本管理 |
| [`backend/tests/`](backend/tests/) | 规则、接口、权限、持久化与部署数据测试 |
| [`deploy/`](deploy/) | systemd、Caddy 示例和 SQLite 备份／恢复工具 |
| [`docs/`](docs/) | 网页使用、数据库结构、部署步骤及开发计划 |

游戏状态的来源在服务端：每次行动先校验身份、手牌 ID、状态版本及合法操作，再更新快照和账本，最后通过 WebSocket 向每位玩家发送各自可见的状态。聊天与这些持久化记录分离。数据库表和每手牌的处理流程见 [数据库说明](docs/database.md)。

## 测试与本地演示

安装开发依赖后，在项目根目录运行规则、数据库和 API 回归测试：

```powershell
python -m pip install -r backend/requirements-dev.txt
python -B -X utf8 -m unittest backend.tests.engine.test_game backend.tests.integration.test_storage backend.tests.integration.test_deploy_data backend.tests.api.test_server
```

`backend/examples/simulate_hand.py` 是不连接网页和数据库的本地策略演示，可用 `python -B -X utf8 -m backend.examples.simulate_hand` 运行；它会打印底牌，不能用作线上玩家接口或生产日志。

## 下一阶段：GTO 分析与训练题

1. **先评估服务器容量，再决定模型部署位置**：确定可用的 GTO 模型或求解方案及其许可、支持的局面范围，实测内存、CPU、磁盘占用和响应时间。只有在不影响现有牌局、倒计时和数据库服务的情况下才部署到同一台服务器；资源不足时另选独立算力，不把耗时计算放进游戏请求链路。目前尚未集成 GTO 模型。
2. **建立独立的分析接口**：把位置、有效筹码、盲注、公共牌和行动历史转换为模型输入，输出适用局面的建议动作、混合频率及可用时的 EV；明确模型无法覆盖的局面。分析服务与现有规则引擎分离，并通过资源限制和超时保护牌局服务。
3. **加入 GTO 训练题**：从已验证的局面中生成或整理翻前、翻后决策题，展示题目条件和可选动作；玩家提交选择后查看参考频率、解析及错题复盘。训练题与实时牌桌分开，不能读取未结束手牌中其他玩家的私密信息。

具体里程碑与验收条件见 [开发计划](docs/development-plan.md)。版本变化见 [版本推进日志](CHANGELOG.md)；原控制台规则引擎的历史说明见 [旧版引擎文档](docs/legacy-engine.md)。
