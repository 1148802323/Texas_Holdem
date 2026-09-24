from __future__ import annotations
from dataclasses import dataclass
from itertools import combinations
from collections import Counter
from typing import Iterable, List, Tuple
from .cards import Card, Rank


# hand category: 0..8
# 8 Straight Flush
# 7 Four of a Kind
# 6 Full House
# 5 Flush
# 4 Straight
# 3 Three of a Kind
# 2 Two Pair
# 1 One Pair
# 0 High Card

CATEGORY_NAME = {
    8: "Straight Flush",
    7: "Four of a Kind",
    6: "Full House",
    5: "Flush",
    4: "Straight",
    3: "Three of a Kind",
    2: "Two Pair",
    1: "One Pair",
    0: "High Card",
}


@dataclass(frozen=True, slots=True)
class HandRank:
    score: Tuple[int, ...]      # 可直接比较的 tuple（越大越强）
    best5: Tuple[Card, ...]     # 最佳 5 张牌

    @property
    def category(self) -> int:
        return self.score[0]

    @property
    def name(self) -> str:
        return CATEGORY_NAME.get(self.category, "Unknown")


def _is_straight(ranks_desc: List[int]) -> tuple[bool, int]:
    """
    ranks_desc: 5张牌点数(降序，可能含A=14)
    返回 (是否顺子, 顺子高张点数)
    """
    uniq = sorted(set(ranks_desc), reverse=True)
    if len(uniq) != 5:
        return (False, 0)

    high = uniq[0]
    low = uniq[-1]
    if high - low == 4:
        return (True, high)

    # Wheel: A-5-4-3-2
    if set(uniq) == {14, 5, 4, 3, 2}:
        return (True, 5)

    return (False, 0)


def _rank_5(cards5: Tuple[Card, ...]) -> Tuple[int, ...]:
    ranks = sorted([int(c.rank) for c in cards5], reverse=True)
    suits = [c.suit for c in cards5]

    is_flush = len(set(suits)) == 1
    is_straight, straight_high = _is_straight(ranks)

    cnt = Counter(ranks)
    # 按(数量降序, 点数降序)排序，如：四条/葫芦/三条/两对/一对
    groups = sorted(cnt.items(), key=lambda x: (x[1], x[0]), reverse=True)
    counts_sorted = sorted(cnt.values(), reverse=True)

    if is_straight and is_flush:
        return (8, straight_high)

    if counts_sorted == [4, 1]:
        quad = groups[0][0]
        kicker = groups[1][0]
        return (7, quad, kicker)

    if counts_sorted == [3, 2]:
        trips = groups[0][0]
        pair = groups[1][0]
        return (6, trips, pair)

    if is_flush:
        return (5, *ranks)

    if is_straight:
        return (4, straight_high)

    if counts_sorted == [3, 1, 1]:
        trips = groups[0][0]
        kickers = sorted([r for r, c in cnt.items() if c == 1], reverse=True)
        return (3, trips, *kickers)

    if counts_sorted == [2, 2, 1]:
        pairs = sorted([r for r, c in cnt.items() if c == 2], reverse=True)
        kicker = [r for r, c in cnt.items() if c == 1][0]
        return (2, pairs[0], pairs[1], kicker)

    if counts_sorted == [2, 1, 1, 1]:
        pair = [r for r, c in cnt.items() if c == 2][0]
        kickers = sorted([r for r, c in cnt.items() if c == 1], reverse=True)
        return (1, pair, *kickers)

    return (0, *ranks)


def evaluate_7(cards: Iterable[Card]) -> HandRank:
    cards_list = list(cards)
    if len(cards_list) != 7:
        raise ValueError("Texas Hold'em evaluation expects exactly 7 cards (2 hole + 5 community).")

    best_score: Tuple[int, ...] | None = None
    best5: Tuple[Card, ...] | None = None

    for comb in combinations(cards_list, 5):
        sc = _rank_5(comb)
        if best_score is None or sc > best_score:
            best_score = sc
            best5 = comb

    assert best_score is not None and best5 is not None
    # 为了展示更好看：最佳5按点数降序排列（不是必须）
    best5_sorted = tuple(sorted(best5, key=lambda c: int(c.rank), reverse=True))
    return HandRank(score=best_score, best5=best5_sorted)


def compare_hands(p1_cards7: Iterable[Card], p2_cards7: Iterable[Card]) -> int:
    """
    返回：
      1  => p1赢
      0  => 平局
     -1  => p2赢
    """
    r1 = evaluate_7(p1_cards7)
    r2 = evaluate_7(p2_cards7)
    if r1.score > r2.score:
        return 1
    if r1.score < r2.score:
        return -1
    return 0
