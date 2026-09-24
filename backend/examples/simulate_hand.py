from __future__ import annotations
import random
from typing import Dict

from backend.app.engine.player import Player
from backend.app.engine.game import HoldemGame, GameState
from backend.app.engine.actions import Action, ActionType, LegalActions


def random_strategy(rng: random.Random):
    """
    一个最小可用策略：
    - 有to_call时：随机 fold / call / 偶尔 raise
    - 无to_call时：随机 check / bet
    - bet/raise 大小在合法区间里随机
    """
    def _strat(state: GameState, player: Player, legal: LegalActions) -> Action:
        # fold/call/raise when facing a bet
        if legal.to_call > 0:
            choices = []
            if legal.can_fold:
                choices.append(ActionType.FOLD)
            if legal.can_call:
                choices.append(ActionType.CALL)
            if legal.can_raise:
                # 偶尔加注
                if rng.random() < 0.25:
                    choices.append(ActionType.RAISE)

            t = rng.choice(choices)

            if t == ActionType.RAISE:
                max_to = legal.max_raise_to or (state.bet_this_street[state.to_act_index] + player.stack)
                min_to = legal.min_raise_to or (state.current_bet + state.last_raise_size)
                # 简化：在[min_to, max_to]里抽
                if max_to <= min_to:
                    return Action(ActionType.RAISE, max_to)  # all-in short raise
                return Action(ActionType.RAISE, rng.randint(min_to, max_to))

            return Action(t)

        # no bet faced
        choices = []
        if legal.can_check:
            choices.append(ActionType.CHECK)
        if legal.can_bet and rng.random() < 0.5:
            choices.append(ActionType.BET)

        t = rng.choice(choices)

        if t == ActionType.BET:
            mn = legal.min_bet or 1
            mx = legal.max_bet or mn
            return Action(ActionType.BET, rng.randint(mn, mx))

        return Action(ActionType.CHECK)

    return _strat


def main():
    players = [
        Player("Alice", stack=200),
        Player("Bob", stack=200),
        Player("Carol", stack=200),
    ]

    g = HoldemGame(players, small_blind=1, big_blind=2, seed=7)

    rng = random.Random(42)
    default = random_strategy(rng)
    strategies: Dict[int, callable] = {}  # 你也可以给某些座位单独策略

    g.play_hand(strategies=strategies, default_strategy=default, verbose=True)

    print("\n=== Final stacks ===")
    for i, p in enumerate(players):
        print(f"seat{i} {p.name}: {p.stack}")

    print("\n=== Action history ===")
    g.pretty_print_history()


if __name__ == "__main__":
    main()
