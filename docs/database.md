# 数据库与持久化

当前使用 Python 标准库 `sqlite3`，无需额外依赖；数据库文件应放在服务器受保护的数据目录，不能放入前端静态资源目录。`database/migrations/001_initial.sql` 建立结构，`PokerStore.initialize()` 在新库上执行迁移。

## 房间规则

`max_buyin_stack` 是**买入后的在桌筹码上限**。买入额度可以自由选择，但必须大于 0 且 `当前在桌筹码 + 买入额 <= 上限`。历史累计买入不参与此上限计算；赢牌超过上限时保留所得筹码，之后只能等筹码低于上限再补买。买入仅限两手牌之间。

房间可使用自由昵称，或预设昵称列表。同一房间内昵称采用去除首尾空格及大小写折叠后的值比较；同一座位和昵称都只能被占用一次。玩家首次加入时获得随机会话令牌，数据库只保存其 SHA-256 摘要。后续应由 HTTP 层通过 `HttpOnly`、`Secure` Cookie 保管令牌。

## 主要表

| 表 | 用途 |
| --- | --- |
| `rooms`, `allowed_nicknames` | 盲注、人数、买入上限和昵称规则 |
| `room_players` | 身份、座位、当前筹码、累计买入和令牌摘要 |
| `buyins` | 每笔买入金额及前后筹码；请求 ID 防重复 |
| `hands`, `hand_players` | 手牌、庄位、参与者、盲注实付、起止筹码、派彩与返还 |
| `hand_actions` | 玩家动作、实际投入、底池、版本及请求 ID |

`hands.private_snapshot` 包含底牌和剩余牌堆，是服务端恢复牌局所需的**私密数据**。玩家界面只能调用 `player_view()` 或 `hand_history()`，不能访问 `load_game()` 或数据库原始行。管理员概览由 `room_overview()` 提供；未来的 HTTP 层必须先验证管理员身份。

## 一致性与恢复

- `buy_in()`、`start_hand()`、`apply_action()` 和 `set_deadline()` 在 SQLite `BEGIN IMMEDIATE` 事务中执行。同桌写入会串行化。
- 每次动作同时保存动作记录、最新私密快照和状态版本；结算时同一事务更新玩家筹码与 `hand_players` 结果。
- 买入、开手牌和动作使用调用方提供的请求 ID 防止重复执行；动作还必须带手牌 ID 与预期状态版本，以拒绝过期页面的请求。
- 重新创建 `PokerStore` 并调用 `load_game()` 可继续未完成手牌；截止时间随私密快照恢复。超时判断及代玩家自动操作仍由后续房间服务负责。
- 服务端应定期备份数据库，备份文件与在线库具有相同的私密性要求。

运行数据库测试：

```powershell
D:\miniconda\envs\Texas_Holdem\python.exe -B -X utf8 -m unittest discover -s backend/tests/integration -v
```
