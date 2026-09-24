# Texas Hold’em 私人牌桌

目标：管理员创建牌局并分享邀请链接，朋友无需注册即可凭昵称加入；支持买入记录、连续手牌、刷新恢复和私密底牌。

开发进度见 [版本推进日志](CHANGELOG.md)。

## 当前状态

已具备逐次操作的规则引擎、SQLite 持久化、管理员建房、邀请入座、浏览器牌桌和 WebSocket 同步。当前可在本机运行；公网部署与部分管理功能仍待完成。

**运行网页牌桌：**按 [网页运行说明](docs/web.md) 设置管理员密码并启动服务，访问 `http://127.0.0.1:8000/admin`。

## 目录职责

```text
backend/
  app/
    engine/         卡牌、发牌、牌型评估、下注和结算
    api/            管理员、房间、买入和历史 HTTP 接口
    realtime/       实时消息、连接管理和重连同步
    services/       房间、玩家身份、买入账本等业务
    models/         数据库模型
    schemas/        请求、响应及消息数据结构
    core/           配置、访问控制和数据库连接
  examples/         本地运行示例
  tests/
    engine/         规则和结算测试
    api/            接口和权限测试
    integration/    联机、持久化及恢复测试
frontend/
  src/
    pages/
      admin/        管理员页面
      join/         邀请和入座页面
      table/        牌桌页面
      history/      玩家记录页面
    components/     公共界面组件
    services/       API 调用和实时连接
    styles/         样式
  public/           静态资源
database/
  migrations/       数据库结构迁移；不存放真实数据库和备份
docs/               需求、设计与原引擎说明
deploy/             部署配置
```

空目录使用 `.gitkeep` 保留，实际实现时可以移除占位文件。

## 运行现有演示

使用 Python 3.10 或更高版本，目前仅依赖标准库。在项目根目录运行：

```powershell
python -B -X utf8 -m backend.examples.simulate_hand
```

原 `test.py` 是随机策略演示，并非自动化测试，现改名为 `backend/examples/simulate_hand.py`。示例会打印所有玩家底牌，仅用于本地调试，不能直接作为玩家接口或生产日志。

原说明保存在 [docs/legacy-engine.md](docs/legacy-engine.md)，其中旧路径和启动命令仅作历史参考。

## 运行规则测试

```powershell
D:\miniconda\envs\Texas_Holdem\python.exe -B -X utf8 -m unittest discover -s backend/tests/engine -v
D:\miniconda\envs\Texas_Holdem\python.exe -B -X utf8 -m unittest discover -s backend/tests/integration -v
```

## 使用引擎

```python
from backend.app.engine.actions import Action, ActionType
from backend.app.engine.game import HoldemGame
from backend.app.engine.player import Player

game = HoldemGame([Player("Alice", 100), Player("Bob", 100)], 1, 2)
game.start_new_hand()
seat = game.to_act_index
player = game.players[seat]
legal = game.legal_actions(player.player_id)
game.submit_action(player.player_id, Action(ActionType.CALL), expected_version=game.version)
player_view = game.state_for_player(player.player_id)
private_snapshot = game.export_private_snapshot()  # 只供服务端持久化
```

`private_snapshot` 包含全部底牌和剩余牌堆，绝不能发送给玩家。房间服务必须串行处理同一桌的命令，并将操作结果与快照一起持久化。行动截止时间由房间服务设置和执行，引擎负责保存该时间。

## 数据库

`PokerStore` 位于 `backend/app/services/storage.py`，负责创建房间、发放玩家凭证、逐笔买入、开始手牌、提交动作、恢复快照及查询个人历史。数据库结构和一致性规则见 [数据库说明](docs/database.md)。房间的买入上限限制买入**之后**的在桌筹码；获胜筹码可以超过上限。

初始化本地数据库：

```powershell
D:\miniconda\envs\Texas_Holdem\python.exe -B -X utf8 -m backend.examples.init_database
```

默认生成 `database/holdem.sqlite3`，该文件在 `.gitignore` 中，不会被提交。数据库迁移脚本会随代码进入 Git。

## 下一步

完善管理员操作、跨设备恢复与公网部署。详细顺序见 [开发计划](docs/development-plan.md)，已确认范围见 [需求说明](docs/requirements.md)。
