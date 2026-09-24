from __future__ import annotations
from dataclasses import dataclass, field
from typing import List
from .cards import Card
from .deck import Deck


@dataclass
class Table:
    community: List[Card] = field(default_factory=list)
    burn_pile: List[Card] = field(default_factory=list)
    pot: int = 0

    def reset(self) -> None:
        self.community.clear()
        self.burn_pile.clear()
        self.pot = 0

    def deal_flop(self, deck: Deck, burn: bool = True) -> List[Card]:
        if burn:
            self.burn_pile.append(deck.burn())
        flop = deck.deal(3)
        self.community.extend(flop)
        return flop

    def deal_turn(self, deck: Deck, burn: bool = True) -> Card:
        if burn:
            self.burn_pile.append(deck.burn())
        turn = deck.deal(1)[0]
        self.community.append(turn)
        return turn

    def deal_river(self, deck: Deck, burn: bool = True) -> Card:
        if burn:
            self.burn_pile.append(deck.burn())
        river = deck.deal(1)[0]
        self.community.append(river)
        return river
