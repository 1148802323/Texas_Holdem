from __future__ import annotations
from dataclasses import dataclass, field
from typing import List
from .cards import Card


@dataclass
class Player:
    name: str
    stack: int = 0
    hole: List[Card] = field(default_factory=list)
    folded: bool = False
    all_in: bool = False

    def reset_for_new_hand(self) -> None:
        self.hole.clear()
        self.folded = False
        self.all_in = False

    def __str__(self) -> str:
        hc = " ".join(str(c) for c in self.hole) if self.hole else "-- --"
        return f"{self.name} [{hc}] stack={self.stack} folded={self.folded} all_in={self.all_in}"
