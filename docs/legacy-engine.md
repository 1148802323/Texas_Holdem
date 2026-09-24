````md
# Texas Hold’em Poker Simulation (Python)

一个用于学习德州扑克（Texas Hold’em）策略/训练判断力的牌桌模拟系统，包含：

- 扑克牌基础模型（花色/点数/卡牌）
- 牌堆与发牌（含 burn）
- 桌面公共牌池与底池
- 玩家状态（stack、hole、fold、all-in）
- 牌型评估与比较（7 选 5）
- 四街下注轮（preflop / flop / turn / river）
- 行动历史（action history）
- 最小可用策略接口（StrategyFn）

---

## 目录结构

```text
  ./
  cards.py
  deck.py
  table.py
  player.py
  actions.py
  evaluator.py
  game.py
  equity.py
  test.py
````

---

## 快速开始

在项目目录下运行：

```bash
python test.py
```

你会看到一手完整牌局的发牌/下注过程（verbose 模式）以及最终筹码与行动历史。

---

## 架构总览

系统由三个层级组成：

1. **数据模型层**：`cards.py`, `player.py`, `actions.py`
   定义“牌/玩家/动作”的结构与表示方式。
2. **规则与运算层**：`deck.py`, `table.py`, `evaluator.py`, `equity.py`
   实现发牌、公共牌、牌型评估、胜率估计等基础能力。
3. **游戏驱动层**：`game.py`
   串起一手牌从发牌到摊牌的流程，包含下注轮、历史记录、边池结算、策略接口。

---

## 各文件说明

### 1) `cards.py` — 扑克牌基础模型

**职责**

* 定义一张牌的不可变表示，并提供统一的显示/编码方式。

**核心结构**

* `Suit(Enum)`：花色（♣♦♥♠），提供 `.short`（c/d/h/s）用于短码输出。
* `Rank(IntEnum)`：点数（2..14），提供 `.label`（2..9,T,J,Q,K,A）用于显示。
* `Card(dataclass, frozen=True)`：牌对象，由 `(rank, suit)` 组成，可 hash（便于 set 去重）。

**核心逻辑**

* `__str__()`：输出 `A♠` 等可读格式。
* `code()`：输出 `As` / `Td` 等标准短码。

**与其他模块关系**

* 全项目统一以 `Card` 为基本单位：发牌、桌面、评估、胜率估计都使用它。

---

### 2) `deck.py` — 牌堆与随机发牌

**职责**

* 管理一副 52 张牌：初始化、洗牌、发牌、烧牌。

**核心结构**

* `Deck`：内部维护 `_cards: List[Card]` 和 `_rng: random.Random`（可 seed）。

**核心逻辑**

* 初始化时生成 52 张 `Card` 并 `shuffle()`。
* `deal(n)`：从牌堆取 n 张并移除。
* `burn()`：烧一张（等价于 `deal(1)[0]`）。

**设计要点**

* 随机性集中在 `Deck` 中，便于复现实验/训练（seed）。
* 牌堆只负责“发牌”，不关心下注与规则流程。

---

### 3) `table.py` — 桌面公共区（公共牌/底池）

**职责**

* 保存并管理桌面共享状态：公共牌、烧牌堆、底池金额。

**核心结构**

* `Table.community: List[Card]`：公共牌（0~5）。
* `Table.burn_pile: List[Card]`：烧掉的牌（用于调试/复盘）。
* `Table.pot: int`：底池。

**核心逻辑**

* `reset()`：清空公共牌/烧牌/底池。
* `deal_flop/turn/river()`：按德州规则 burn 后发公共牌并记录。

**与其他模块关系**

* `game.py` 驱动发公共牌。
* 摊牌时 `evaluator.py` 使用 `player.hole + table.community` 评估牌力。

---

### 4) `player.py` — 玩家状态容器

**职责**

* 保存玩家状态（不包含策略逻辑）。

**核心结构**

* `Player.name`
* `Player.stack`：筹码
* `Player.hole`：手牌两张
* `Player.folded`：弃牌
* `Player.all_in`：全下（stack==0 时置 True）

**核心逻辑**

* `reset_for_new_hand()`：开新手前重置 hole/fold/all-in。

**与其他模块关系**

* `game.py` 会更新 stack、folded、all_in。
* 策略函数会读取玩家 hole/stack 来做决策。

---

### 5) `actions.py` — 动作类型、动作记录、合法动作集合

**职责**

* 将下注动作、动作参数、行动历史、合法动作范围抽象为统一结构，解耦规则与策略。

**核心结构**

* `ActionType(Enum)`：

  * `FOLD / CHECK / CALL / BET / RAISE`
* `Action(dataclass)`：

  * `type`
  * `amount`

    * `BET`: bet size
    * `RAISE`: raise_to（该玩家本街累计投入到多少）
* `ActionRecord(dataclass)`：一次行动的历史记录字段

  * street、玩家、动作、amount、to_call、pot_after、current_bet
* `LegalActions(dataclass)`：当前玩家可执行动作及下注范围

  * `to_call`
  * `can_*` flags
  * bet 区间：`min_bet/max_bet`
  * raise 区间：`min_raise_to/max_raise_to`

**设计要点**

* `ActionRecord` 是做 GTO/CFR 信息集的基础。
* `LegalActions` 让策略无需理解所有规则细节，只在合法范围内选择动作。

---

### 6) `evaluator.py` — 牌型评估与比较（7 选 5）

**职责**

* 输入 7 张牌（2 手牌 + 5 公共牌），输出最强 5 张组合与可比较分数。

**核心结构**

* `HandRank(score, best5)`

  * `score`: tuple（越大越强，Python 字典序比较）
  * `best5`: 最佳五张牌（用于展示/解释）
* `CATEGORY_NAME`: 牌型类别名映射

**核心逻辑**

1. `evaluate_7(cards7)`

   * 枚举 7 张牌所有 5 张组合（21 种）
   * 调 `_rank_5()` 得到分数
   * 取最大分数作为最佳牌型
2. `_rank_5(cards5)`

   * 计算是否同花、是否顺子（含 A2345 特判）、重复结构（对子/三条/四条）
   * 返回类别与踢脚组成的 score tuple
3. `compare_hands(a7, b7)`

   * evaluate 后直接比较 score

**与其他模块关系**

* `game.py` 在摊牌/边池结算时调用。
* `equity.py` 多次调用 compare/evaluate 做胜率估计。

---

### 7) `game.py` — 游戏引擎（四街下注 + 历史 + 策略接口 + 边池结算）

项目主引擎，负责串起一手牌从发牌到摊牌的全部流程。

#### 7.1 职责

* 发牌、下盲注、发公共牌
* 四街下注轮驱动（轮到谁、何时结束、raise/bet 如何重置响应者）
* 维护 action history
* 处理 fold / all-in
* 构造边池（side pots）并摊牌结算分配筹码

#### 7.2 策略接口（最小可用）

```python
(state: GameState, player: Player, legal: LegalActions) -> Action
```

* `GameState`：策略可见的状态快照（含 history）
* `player`：当前行动玩家对象（含 hole、stack）
* `legal`：当前合法动作与 bet/raise 区间

#### 7.3 `GameState` 包含字段

* `street`：preflop/flop/turn/river
* `community`：公共牌
* `pot`：底池
* `button_index`：按钮位置
* `to_act_index`：当前行动座位
* `current_bet`：本街最高投入
* `last_raise_size`：本街最近一次 raise 的增量（用于 min raise）
* `stacks`：每人筹码
* `bet_this_street`：每人本街已投入
* `contributed_total`：每人整手累计投入（用于边池）
* `folded/all_in`：每人状态
* `history`：`ActionRecord` 序列（行动历史）

#### 7.4 一手牌流程（`play_hand()`）

1. `start_new_hand()`

   * 洗牌、清空桌面/历史
   * 重置玩家
   * 下盲注（更新 pot、bet_this_street、contributed_total）
   * 发两张手牌
2. Preflop 下注轮（`_betting_round`）
3. 发 flop，重置本街下注（`_reset_street_bets`），flop 下注轮
4. 发 turn，重置下注，turn 下注轮
5. 发 river，重置下注，river 下注轮
6. 摊牌与边池结算（`_award_pots_showdown`）

#### 7.5 下注轮核心（`_betting_round()`）

* 维护 `need_action`：本街“还需要回应当前下注”的玩家集合

  * 初始：所有未 fold 且未 all-in 的玩家
  * 有人 bet/raise：`need_action` 重置为“除行动者之外所有未 fold 未 all-in 的人”
  * 有人 check/call：从 `need_action` 移除
* 每次轮到行动者：

  1. `_legal_actions_for(i)` 计算合法动作与区间
  2. 构造 `GameState` 快照
  3. 调策略函数返回 `Action`
  4. 非法动作做 fallback（例如不能 check 时改为 call）
  5. 更新 pot / stack / current_bet / last_raise_size
  6. 写入 `history`（ActionRecord）
* 早停：

  * 若只剩一人未 fold，立即结束整手并把 pot 给赢家（`_award_if_only_one_left`）

#### 7.6 边池结算（`_build_side_pots()` + `_award_pots_showdown()`）

* 用 `contributed_total` 构造层级边池：

  * 按投入额度分层，每层 pot = (level - prev) * 参与人数
  * 每个 pot 的 eligible 为未 fold 且投入 ≥ 该层的玩家
* 对每个 pot：

  * 评估 eligible 中最大牌力
  * 平分 pot，余数按座位顺序分配

---

### 8) `equity.py` — 蒙特卡洛胜率估计（训练判断力）

**职责**

* 给定 hero 手牌与（可选）部分公共牌，随机补齐对手手牌与剩余公共牌，估算 win/tie/loss。

**核心函数**

* `monte_carlo_equity_heads_up(hero_hole, community=(), iters=20000, seed=None)`

**核心逻辑**

1. 构造完整 52 张牌
2. 去掉已知牌（hero 手牌 + 已知公共牌）
3. 每次迭代：

   * 抽对手两张
   * 补齐公共牌到 5 张
   * 用 `compare_hands` 判断胜负
4. 汇总胜平负比例

**扩展方向**

* 将“对手随机两张”改为“对手按 range 采样”，用于更真实的 EV 估计与 GTO 训练。

---

### 9) `test.py` — 示例入口（演示策略与历史）

**职责**

* 提供可跑通 demo：

  * 多人开局
  * 随机策略驱动四街下注
  * 输出最终筹码与行动历史

**关键点**

* `random_strategy()` 根据 `LegalActions` 决定动作与下注大小（在合法区间内随机）。

---

## 扩展建议（面向 GTO / CFR）

常见下一步：

1. **训练器交互策略**：写一个 `human_strategy()` 从命令行输入动作（fold/call/raise_to/bet）。
2. **range / 抽象**：

   * 用 `equity.py` 或 `evaluator.py` 特征做 hand strength buckets
   * 将 board texture 做分桶（wet/dry 等）
3. **CFR/MCCFR**：

   * 信息集 key 可用：`(street, position, bucketed_hand, board_bucket, action_history)`
   * 策略输出混合策略（按概率采样 Action）
4. **更严格规则**：

   * 全下不足最小加注是否“重开下注”的细则
   * 更精确的最小 bet / 最小 raise 规则
   * 强制顺序（例如短筹码 all-in 对行动顺序的影响）

---

## 许可证

按你的需要补充（MIT / Apache-2.0 / Private）。

```
```
