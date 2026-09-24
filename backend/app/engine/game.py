from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Dict, List, Tuple

from .cards import Card
from .deck import Deck
from .table import Table
from .player import Player
from .evaluator import evaluate_7

from .actions import Action, ActionType, ActionRecord, LegalActions


@dataclass(frozen=True)
class GameState:
    street: str
    community: tuple[Card, ...]
    pot: int

    button_index: int
    to_act_index: int

    current_bet: int
    last_raise_size: int

    stacks: tuple[int, ...]
    bet_this_street: tuple[int, ...]
    contributed_total: tuple[int, ...]

    folded: tuple[bool, ...]
    all_in: tuple[bool, ...]

    history: tuple[ActionRecord, ...]


StrategyFn = Callable[[GameState, Player, LegalActions], Action]


class HoldemGame:
    def __init__(
        self,
        players: List[Player],
        small_blind: int = 1,
        big_blind: int = 2,
        seed: int | None = None,
    ) -> None:
        if len(players) < 2:
            raise ValueError("Need at least 2 players.")
        self.players = players
        self.sb = int(small_blind)
        self.bb = int(big_blind)
        self.seed = seed

        self.deck = Deck(seed=seed)
        self.table = Table()
        self.button_index = 0

        # per hand runtime
        self.history: List[ActionRecord] = []
        self.bet_this_street: List[int] = [0] * len(players)
        self.contributed_total: List[int] = [0] * len(players)
        self.current_bet: int = 0
        self.last_raise_size: int = self.bb

    # ---------- seating helpers ----------
    def _blind_indices(self) -> tuple[int, int]:
        n = len(self.players)
        if n == 2:
            # heads-up: button is SB
            sb_i = self.button_index
            bb_i = (self.button_index + 1) % n
        else:
            sb_i = (self.button_index + 1) % n
            bb_i = (self.button_index + 2) % n
        return sb_i, bb_i

    def rotate_button(self) -> None:
        self.button_index = (self.button_index + 1) % len(self.players)

    def _active_not_folded(self) -> List[int]:
        return [i for i, p in enumerate(self.players) if not p.folded]

    def _can_anyone_act(self) -> bool:
        # at least two players can still take actions (not folded, not all-in)
        idx = [i for i, p in enumerate(self.players) if (not p.folded and not p.all_in)]
        return len(idx) >= 2

    # ---------- money helpers ----------
    def _take_chips(self, i: int, amount: int) -> int:
        """Take up to 'amount' from player's stack. Return actual taken."""
        if amount <= 0:
            return 0
        p = self.players[i]
        take = min(amount, p.stack)
        p.stack -= take
        if p.stack == 0:
            p.all_in = True
        self.table.pot += take
        self.contributed_total[i] += take
        self.bet_this_street[i] += take
        return take

    def _record(self, street: str, i: int, action: Action, to_call_before: int) -> None:
        p = self.players[i]
        self.history.append(
            ActionRecord(
                street=street,
                player_index=i,
                player_name=p.name,
                action=action.type.value,
                amount=int(action.amount),
                to_call=int(to_call_before),
                pot_after=int(self.table.pot),
                current_bet=int(self.current_bet),
            )
        )

    # ---------- dealing / init ----------
    def start_new_hand(self) -> None:
        self.deck = Deck(seed=self.seed)  # optional reproducibility
        self.table.reset()
        self.history.clear()

        for p in self.players:
            p.reset_for_new_hand()

        n = len(self.players)
        self.bet_this_street = [0] * n
        self.contributed_total = [0] * n

        # post blinds
        sb_i, bb_i = self._blind_indices()

        self.current_bet = 0
        self.last_raise_size = self.bb

        sb_paid = self._take_chips(sb_i, self.sb)
        bb_paid = self._take_chips(bb_i, self.bb)
        self.current_bet = max(sb_paid, bb_paid)  # preflop current bet is BB (unless short)

        # deal hole cards (round-robin)
        for _ in range(2):
            for p in self.players:
                p.hole.extend(self.deck.deal(1))

    def _reset_street_bets(self) -> None:
        self.bet_this_street = [0] * len(self.players)
        self.current_bet = 0
        self.last_raise_size = self.bb  # minimal bet baseline postflop

    # ---------- legal actions ----------
    def _legal_actions_for(self, i: int) -> LegalActions:
        p = self.players[i]
        if p.folded or p.all_in:
            return LegalActions(
                to_call=0,
                can_fold=False,
                can_check=False,
                can_call=False,
                can_bet=False,
                can_raise=False,
            )

        to_call = max(0, self.current_bet - self.bet_this_street[i])
        stack = p.stack

        can_fold = to_call > 0
        can_check = to_call == 0
        can_call = to_call > 0 and stack > 0
        can_bet = self.current_bet == 0 and stack > 0
        can_raise = self.current_bet > 0 and stack > to_call  # must have chips beyond calling

        min_bet = max(1, min(self.bb, stack)) if can_bet else None
        max_bet = stack if can_bet else None

        # raise_to is "total投入到本街的值"
        max_raise_to = (self.bet_this_street[i] + stack) if can_raise else None
        min_raise_to = None
        if can_raise and max_raise_to is not None:
            # standard min raise: current_bet + last_raise_size
            min_raise_to = self.current_bet + self.last_raise_size
            # 如果筹码不够标准最小加注，这里仍允许“全下加注”（简化规则）
            # 让策略可以直接选 max_raise_to 当作 all-in raise
            # 如果你想严格规则（不重开），后面我也能帮你加上
        return LegalActions(
            to_call=to_call,
            can_fold=can_fold,
            can_check=can_check,
            can_call=can_call,
            can_bet=can_bet,
            can_raise=can_raise,
            min_bet=min_bet,
            max_bet=max_bet,
            min_raise_to=min_raise_to,
            max_raise_to=max_raise_to,
        )

    # ---------- betting round ----------
    def _betting_round(
        self,
        street: str,
        first_to_act: int,
        strategies: Dict[int, StrategyFn],
        default_strategy: StrategyFn,
        verbose: bool = False,
    ) -> bool:
        """
        Return: True if hand ends early (everyone folded except one), else False.
        """
        # need_action: players who still must respond before street ends
        need_action = {
            i
            for i, p in enumerate(self.players)
            if (not p.folded and not p.all_in)
        }

        # if nobody/only one can act, round ends immediately
        if len(need_action) <= 1:
            return False

        i = first_to_act
        n = len(self.players)

        def advance_to_next(start: int) -> int:
            j = start
            for _ in range(n):
                if j in need_action:
                    return j
                j = (j + 1) % n
            return start  # shouldn't happen

        while need_action:
            # early end: only one player not folded
            active = self._active_not_folded()
            if len(active) == 1:
                return True

            i = advance_to_next(i)

            p = self.players[i]
            legal = self._legal_actions_for(i)

            state = GameState(
                street=street,
                community=tuple(self.table.community),
                pot=self.table.pot,
                button_index=self.button_index,
                to_act_index=i,
                current_bet=self.current_bet,
                last_raise_size=self.last_raise_size,
                stacks=tuple(pp.stack for pp in self.players),
                bet_this_street=tuple(self.bet_this_street),
                contributed_total=tuple(self.contributed_total),
                folded=tuple(pp.folded for pp in self.players),
                all_in=tuple(pp.all_in for pp in self.players),
                history=tuple(self.history),
            )

            strat = strategies.get(i, default_strategy)
            action = strat(state, p, legal)

            # sanitize / apply
            to_call_before = legal.to_call

            if action.type == ActionType.FOLD:
                if not legal.can_fold:
                    # illegal fold when can check; treat as check
                    action = Action(ActionType.CHECK)
                else:
                    p.folded = True
                    need_action.discard(i)
                    self._record(street, i, action, to_call_before)
                    if verbose:
                        print(f"[{street}] {p.name}: fold")
                    i = (i + 1) % n
                    continue

            if action.type == ActionType.CHECK:
                if not legal.can_check:
                    # cannot check -> must call or fold; default to call if possible else fold
                    if legal.can_call:
                        action = Action(ActionType.CALL)
                    else:
                        action = Action(ActionType.FOLD)
                        p.folded = True
                        need_action.discard(i)
                        self._record(street, i, action, to_call_before)
                        if verbose:
                            print(f"[{street}] {p.name}: forced fold (no chips)")
                        i = (i + 1) % n
                        continue
                # check ok
                need_action.discard(i)
                self._record(street, i, action, to_call_before)
                if verbose:
                    print(f"[{street}] {p.name}: check")
                i = (i + 1) % n
                continue

            if action.type == ActionType.CALL:
                if not legal.can_call:
                    # cannot call -> fold if possible else check
                    if legal.can_fold:
                        action = Action(ActionType.FOLD)
                        p.folded = True
                        need_action.discard(i)
                        self._record(street, i, action, to_call_before)
                        if verbose:
                            print(f"[{street}] {p.name}: forced fold (can't call)")
                        i = (i + 1) % n
                        continue
                    else:
                        action = Action(ActionType.CHECK)
                        need_action.discard(i)
                        self._record(street, i, action, to_call_before)
                        if verbose:
                            print(f"[{street}] {p.name}: forced check")
                        i = (i + 1) % n
                        continue

                paid = self._take_chips(i, legal.to_call)
                # if all-in call paid < to_call, still ok; player removed from need_action
                need_action.discard(i)
                self._record(street, i, action, to_call_before)
                if verbose:
                    print(f"[{street}] {p.name}: call {paid} (to_call={to_call_before})")
                i = (i + 1) % n
                continue

            if action.type == ActionType.BET:
                if not legal.can_bet:
                    # if can't bet, fallback to check/call
                    if legal.can_check:
                        action = Action(ActionType.CHECK)
                        need_action.discard(i)
                        self._record(street, i, action, to_call_before)
                        if verbose:
                            print(f"[{street}] {p.name}: fallback check")
                        i = (i + 1) % n
                        continue
                    action = Action(ActionType.CALL)
                    paid = self._take_chips(i, legal.to_call)
                    need_action.discard(i)
                    self._record(street, i, action, to_call_before)
                    if verbose:
                        print(f"[{street}] {p.name}: fallback call {paid}")
                    i = (i + 1) % n
                    continue

                amt = int(action.amount)
                # clamp
                amt = max(1, min(amt, legal.max_bet or amt))
                if legal.min_bet is not None:
                    amt = max(amt, legal.min_bet)

                # apply
                paid = self._take_chips(i, amt)
                self.current_bet = self.bet_this_street[i]  # since current_bet was 0
                self.last_raise_size = max(self.bb, paid)

                # bet reopens: everyone else must respond
                need_action = {
                    j for j, pp in enumerate(self.players)
                    if (not pp.folded and not pp.all_in and j != i)
                }

                self._record(street, i, Action(ActionType.BET, paid), to_call_before)
                if verbose:
                    print(f"[{street}] {p.name}: bet {paid}")
                i = (i + 1) % n
                continue

            if action.type == ActionType.RAISE:
                if not legal.can_raise:
                    # fallback to call
                    action = Action(ActionType.CALL)
                    if legal.can_call:
                        paid = self._take_chips(i, legal.to_call)
                        need_action.discard(i)
                        self._record(street, i, action, to_call_before)
                        if verbose:
                            print(f"[{street}] {p.name}: fallback call {paid}")
                        i = (i + 1) % n
                        continue
                    # else fold/check
                    if legal.can_fold:
                        action = Action(ActionType.FOLD)
                        p.folded = True
                        need_action.discard(i)
                        self._record(street, i, action, to_call_before)
                        if verbose:
                            print(f"[{street}] {p.name}: fallback fold")
                        i = (i + 1) % n
                        continue
                    action = Action(ActionType.CHECK)
                    need_action.discard(i)
                    self._record(street, i, action, to_call_before)
                    if verbose:
                        print(f"[{street}] {p.name}: fallback check")
                    i = (i + 1) % n
                    continue

                desired_raise_to = int(action.amount)

                max_raise_to = legal.max_raise_to or (self.bet_this_street[i] + p.stack)
                desired_raise_to = min(desired_raise_to, max_raise_to)

                # ensure it actually raises above current_bet
                desired_raise_to = max(desired_raise_to, self.current_bet + 1)

                # if we know min_raise_to, try to respect it unless all-in prevents it
                if legal.min_raise_to is not None and desired_raise_to < legal.min_raise_to:
                    # if can't reach min, go all-in
                    desired_raise_to = max_raise_to

                # pay difference
                pay = max(0, desired_raise_to - self.bet_this_street[i])
                paid = self._take_chips(i, pay)

                old_current = self.current_bet
                self.current_bet = max(self.current_bet, self.bet_this_street[i])

                raise_size = self.current_bet - old_current
                if raise_size > 0:
                    self.last_raise_size = raise_size

                # raise reopens (simplified): everyone else must respond
                need_action = {
                    j for j, pp in enumerate(self.players)
                    if (not pp.folded and not pp.all_in and j != i)
                }

                self._record(street, i, Action(ActionType.RAISE, self.current_bet), to_call_before)
                if verbose:
                    print(f"[{street}] {p.name}: raise_to {self.current_bet} (paid {paid})")
                i = (i + 1) % n
                continue

            # unknown action -> default check/call
            if legal.can_check:
                need_action.discard(i)
                self._record(street, i, Action(ActionType.CHECK), to_call_before)
            else:
                paid = self._take_chips(i, legal.to_call)
                need_action.discard(i)
                self._record(street, i, Action(ActionType.CALL), to_call_before)

            i = (i + 1) % n

        return False

    # ---------- street order ----------
    def _first_to_act_preflop(self) -> int:
        n = len(self.players)
        sb_i, bb_i = self._blind_indices()
        if n == 2:
            # HU: SB(button) acts first preflop
            return sb_i
        # multiway: UTG = left of BB
        return (bb_i + 1) % n

    def _first_to_act_postflop(self) -> int:
        n = len(self.players)
        sb_i, bb_i = self._blind_indices()
        if n == 2:
            # HU: BB acts first postflop
            return bb_i
        # multiway: first left of button (SB seat)
        return (self.button_index + 1) % n

    # ---------- showdown + side pots ----------
    def _build_side_pots(self) -> List[Tuple[int, List[int]]]:
        """
        Return list of (pot_amount, eligible_player_indices)
        pot_amount includes chips from folded players too, but eligible excludes folded.
        """
        contrib = self.contributed_total
        unique_levels = sorted(set(contrib))
        unique_levels = [x for x in unique_levels if x > 0]
        if not unique_levels:
            return []

        side_pots: List[Tuple[int, List[int]]] = []
        prev = 0
        for level in unique_levels:
            involved = [i for i, c in enumerate(contrib) if c >= level]
            pot_amt = (level - prev) * len(involved)
            eligible = [i for i in involved if not self.players[i].folded]
            if pot_amt > 0:
                side_pots.append((pot_amt, eligible))
            prev = level
        return side_pots

    def _award_pots_showdown(self, verbose: bool = False) -> None:
        side_pots = self._build_side_pots()
        if not side_pots:
            return

        # precompute hand ranks for all non-folded players
        handranks: Dict[int, Tuple[tuple[int, ...], str]] = {}
        for i, p in enumerate(self.players):
            if p.folded:
                continue
            hr = evaluate_7(p.hole + self.table.community)
            handranks[i] = (hr.score, hr.name)

        for pot_amt, eligible in side_pots:
            if not eligible:
                continue

            best_score = None
            winners: List[int] = []
            for i in eligible:
                sc = handranks[i][0]
                if best_score is None or sc > best_score:
                    best_score = sc
                    winners = [i]
                elif sc == best_score:
                    winners.append(i)

            split = pot_amt // len(winners)
            rem = pot_amt - split * len(winners)

            for i in winners:
                self.players[i].stack += split

            # remainder: deterministic distribute by seat order
            for k in range(rem):
                self.players[winners[k % len(winners)]].stack += 1

            if verbose:
                names = ", ".join(self.players[i].name for i in winners)
                print(f"[showdown] pot {pot_amt} -> winners: {names} (each {split}, rem {rem})")

    # ---------- main hand runner ----------
    def play_hand(
        self,
        strategies: Dict[int, StrategyFn],
        default_strategy: StrategyFn,
        verbose: bool = False,
    ) -> None:
        self.start_new_hand()

        if verbose:
            print("=== NEW HAND ===")
            for i, p in enumerate(self.players):
                btn = " (BTN)" if i == self.button_index else ""
                print(f" - seat {i}: {p}{btn}")
            print(f"Pot after blinds: {self.table.pot}")

        # Preflop betting
        ended = self._betting_round(
            street="preflop",
            first_to_act=self._first_to_act_preflop(),
            strategies=strategies,
            default_strategy=default_strategy,
            verbose=verbose,
        )
        if ended:
            self._award_if_only_one_left(verbose=verbose)
            return

        # If everyone is all-in or only one can act, just run board
        if not self._can_anyone_act():
            self._run_out_board_to_showdown(verbose=verbose)
            self._award_pots_showdown(verbose=verbose)
            return

        # Flop
        self.table.deal_flop(self.deck)
        self._reset_street_bets()
        if verbose:
            print("Flop:", " ".join(str(c) for c in self.table.community))

        ended = self._betting_round(
            street="flop",
            first_to_act=self._first_to_act_postflop(),
            strategies=strategies,
            default_strategy=default_strategy,
            verbose=verbose,
        )
        if ended:
            self._award_if_only_one_left(verbose=verbose)
            return

        if not self._can_anyone_act():
            self._run_out_board_to_showdown(verbose=verbose)
            self._award_pots_showdown(verbose=verbose)
            return

        # Turn
        self.table.deal_turn(self.deck)
        self._reset_street_bets()
        if verbose:
            print("Turn:", str(self.table.community[-1]))

        ended = self._betting_round(
            street="turn",
            first_to_act=self._first_to_act_postflop(),
            strategies=strategies,
            default_strategy=default_strategy,
            verbose=verbose,
        )
        if ended:
            self._award_if_only_one_left(verbose=verbose)
            return

        if not self._can_anyone_act():
            self._run_out_board_to_showdown(verbose=verbose)
            self._award_pots_showdown(verbose=verbose)
            return

        # River
        self.table.deal_river(self.deck)
        self._reset_street_bets()
        if verbose:
            print("River:", str(self.table.community[-1]))

        ended = self._betting_round(
            street="river",
            first_to_act=self._first_to_act_postflop(),
            strategies=strategies,
            default_strategy=default_strategy,
            verbose=verbose,
        )
        if ended:
            self._award_if_only_one_left(verbose=verbose)
            return

        # Showdown
        if verbose:
            print("Board:", " ".join(str(c) for c in self.table.community))
        self._award_pots_showdown(verbose=verbose)

    def _award_if_only_one_left(self, verbose: bool = False) -> None:
        active = self._active_not_folded()
        if len(active) != 1:
            return
        winner = active[0]
        self.players[winner].stack += self.table.pot
        if verbose:
            print(f"[hand end] everyone folded, winner = {self.players[winner].name}, wins pot {self.table.pot}")

    def _run_out_board_to_showdown(self, verbose: bool = False) -> None:
        while len(self.table.community) < 5:
            if len(self.table.community) == 0:
                self.table.deal_flop(self.deck)
                if verbose:
                    print("Flop:", " ".join(str(c) for c in self.table.community))
            elif len(self.table.community) == 3:
                self.table.deal_turn(self.deck)
                if verbose:
                    print("Turn:", str(self.table.community[-1]))
            elif len(self.table.community) == 4:
                self.table.deal_river(self.deck)
                if verbose:
                    print("River:", str(self.table.community[-1]))
            else:
                break

    # ---------- utilities ----------
    def pretty_print_history(self) -> None:
        for r in self.history:
            amt = f" {r.amount}" if r.amount else ""
            print(f"[{r.street}] seat{r.player_index} {r.player_name}: {r.action}{amt} "
                  f"(to_call={r.to_call}) pot={r.pot_after} current_bet={r.current_bet}")
