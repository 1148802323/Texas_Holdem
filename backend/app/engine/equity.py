from __future__ import annotations
import random
from itertools import combinations
from typing import List, Optional, Tuple
from .cards import Card, Rank, Suit
from .evaluator import compare_hands


def _full_deck() -> List[Card]:
    return [Card(r, s) for s in Suit for r in Rank]


def monte_carlo_equity_heads_up(
    hero_hole: Tuple[Card, Card],
    community: Tuple[Card, ...] = (),
    iters: int = 20_000,
    seed: int | None = None,
) -> dict:
    """
    估计：Hero vs 1个未知对手（对手两张随机且不冲突）
    community 可以传入 0~5 张已知公共牌。
    """
    if len(community) > 5:
        raise ValueError("community must have 0..5 cards")

    rng = random.Random(seed)

    used = set(hero_hole) | set(community)
    deck = [c for c in _full_deck() if c not in used]

    wins = ties = losses = 0

    for _ in range(iters):
        rng.shuffle(deck)

        opp_hole = (deck[0], deck[1])
        need = 5 - len(community)
        board = tuple(community) + tuple(deck[2 : 2 + need])

        hero7 = hero_hole + board
        opp7 = opp_hole + board

        cmp_ = compare_hands(hero7, opp7)
        if cmp_ > 0:
            wins += 1
        elif cmp_ < 0:
            losses += 1
        else:
            ties += 1

    total = wins + losses + ties
    return {
        "iters": total,
        "win": wins / total,
        "tie": ties / total,
        "loss": losses / total,
    }
