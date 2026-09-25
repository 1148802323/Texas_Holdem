import json
import random
import unittest

from backend.app.engine.actions import Action, ActionType
from backend.app.engine.cards import Card
from backend.app.engine.game import HoldemGame, InvalidAction
from backend.app.engine.player import Player


def make_game(stacks, seed=7):
    game = HoldemGame([Player(f"P{i}", stack) for i, stack in enumerate(stacks)], seed=seed)
    game.start_new_hand()
    return game


def act(game, action):
    seat = game.to_act_index
    assert seat is not None
    return game.submit_action(game.players[seat].player_id, action, game.version)


def board(game, cards):
    # Deck.deal() removes cards from the right. Burn cards are distinct.
    codes = ["Ts", "Js", "Qs", *cards]
    river, burn3, turn, burn2, flop1, flop2, flop3, burn1 = (
        cards[4], codes[0], cards[3], codes[1],
        cards[0], cards[1], cards[2], codes[2]
    )
    game.deck._cards = [Card.from_code(c) for c in
                        (river, burn3, turn, burn2, flop1, flop2, flop3, burn1)]


class HoldemEngineTests(unittest.TestCase):
    def test_heads_up_order_and_refresh_snapshot(self):
        game = make_game([100, 100])
        self.assertEqual(game.to_act_index, 0)  # button/small blind acts first
        self.assertEqual(game.legal_actions(game.players[0].player_id).to_call, 1)
        first_hole = game.players[0].hole[:]
        second_hole = game.players[1].hole[:]
        view = game.state_for_player(game.players[0].player_id)
        self.assertEqual(view["players"][0]["hole"], [c.code() for c in first_hole])
        self.assertIsNone(view["players"][1]["hole"])
        self.assertNotIn("deck", json.dumps(view))
        game.set_deadline(1_800_000_000.0)
        restored = HoldemGame.from_private_snapshot(json.loads(json.dumps(game.export_private_snapshot())))
        self.assertEqual(restored.game_state(), game.game_state())
        self.assertEqual(restored.players[1].hole, second_hole)
        self.assertEqual(restored.deadline_at, 1_800_000_000.0)
        act(restored, Action(ActionType.CALL))
        act(restored, Action(ActionType.CHECK))
        self.assertEqual(restored.street, "flop")
        self.assertEqual(restored.to_act_index, 1)  # big blind first after flop

    def test_invalid_action_and_stale_version_do_not_mutate(self):
        game = make_game([100, 100])
        before = game.export_private_snapshot()
        for action in (Action(ActionType.CHECK), Action(ActionType.CALL, 4),
                       Action(ActionType.RAISE, 3), Action(ActionType.RAISE, 200),
                       Action(ActionType.RAISE, True)):
            with self.assertRaises(InvalidAction):
                act(game, action)
            self.assertEqual(game.export_private_snapshot(), before)
        with self.assertRaises(InvalidAction):
            game.submit_action(game.players[0].player_id, Action(ActionType.CALL), game.version - 1)
        self.assertEqual(game.export_private_snapshot(), before)
        with self.assertRaises(InvalidAction):
            game.submit_action(game.players[1].player_id, Action(ActionType.CHECK))

    def test_short_big_blind_and_single_player_must_respond(self):
        game = make_game([20, 1])
        self.assertEqual(game.to_act_index, 0)
        self.assertEqual(game.legal_actions(game.players[0].player_id).to_call, 1)
        self.assertFalse(game.legal_actions(game.players[0].player_id).can_raise)
        act(game, Action(ActionType.CALL))
        self.assertEqual(game.street, "runout_vote")
        game.resolve_runout()
        self.assertEqual(game.street, "complete")
        self.assertEqual(game.table.pot, 0)
        self.assertEqual(sum(p.stack for p in game.players), 21)

    def test_short_all_in_does_not_reopen_raise(self):
        game = make_game([100, 100, 13, 100])
        self.assertEqual(game.to_act_index, 3)
        act(game, Action(ActionType.RAISE, 10))
        act(game, Action(ActionType.CALL))
        act(game, Action(ActionType.CALL))
        self.assertEqual(game.to_act_index, 2)
        act(game, Action(ActionType.RAISE, 13))
        self.assertEqual(game.to_act_index, 3)
        legal = game.legal_actions(game.players[3].player_id)
        self.assertEqual(legal.to_call, 3)
        self.assertFalse(legal.can_raise)
        self.assertEqual(game.last_raise_size, 8)
        with self.assertRaises(InvalidAction):
            act(game, Action(ActionType.RAISE, 21))
        act(game, Action(ActionType.CALL))

    def test_cumulative_short_all_ins_reopen_once_full_raise_is_faced(self):
        game = make_game([100, 100, 100, 100, 13, 18])
        self.assertEqual(game.to_act_index, 3)
        act(game, Action(ActionType.RAISE, 10))
        act(game, Action(ActionType.RAISE, 13))
        act(game, Action(ActionType.RAISE, 18))
        for _ in range(3):
            act(game, Action(ActionType.CALL))
        self.assertEqual(game.to_act_index, 3)
        legal = game.legal_actions(game.players[3].player_id)
        self.assertTrue(legal.can_raise)
        self.assertEqual(legal.min_raise_to, 26)

    def test_multiway_all_in_side_pots_and_no_double_settlement(self):
        game = make_game([5, 10, 20])
        game.players[0].hole = [Card.from_code(c) for c in ("As", "Ah")]
        game.players[1].hole = [Card.from_code(c) for c in ("Ks", "Kh")]
        game.players[2].hole = [Card.from_code(c) for c in ("Qh", "Qd")]
        board(game, ["2c", "3d", "4h", "8s", "9c"])
        act(game, Action(ActionType.RAISE, 5))  # seat 0 all in for 5
        act(game, Action(ActionType.RAISE, 10))  # seat 1 all in for 10
        self.assertEqual(game.to_act_index, 2)
        act(game, Action(ActionType.CALL))
        self.assertEqual(game.street, "runout_vote")
        game.resolve_runout()
        self.assertEqual(game.street, "complete")
        self.assertEqual([p.stack for p in game.players], [15, 10, 10])
        self.assertEqual(game.payouts, [15, 10, 0])
        self.assertEqual(game.table.pot, 0)
        after = game.export_private_snapshot()
        with self.assertRaises(InvalidAction):
            game.submit_action(game.players[2].player_id, Action(ActionType.CALL))
        with self.assertRaises(InvalidAction):
            game.settle_hand()
        self.assertEqual(game.export_private_snapshot(), after)
        self.assertEqual(len(set(c for p in game.players for c in p.hole)), 6)

    def test_split_odd_chip_goes_clockwise_from_button(self):
        game = make_game([10, 10, 10])
        game.players[0].hole = [Card.from_code(c) for c in ("Jh", "Jd")]
        game.players[1].hole = [Card.from_code(c) for c in ("Ac", "Kc")]
        game.players[2].hole = [Card.from_code(c) for c in ("Ad", "Kd")]
        board(game, ["2c", "3d", "4h", "5s", "6c"])
        act(game, Action(ActionType.RAISE, 5))
        act(game, Action(ActionType.CALL))
        act(game, Action(ActionType.CALL))
        act(game, Action(ActionType.CHECK))  # flop seat 1
        act(game, Action(ActionType.BET, 2))  # seat 2
        act(game, Action(ActionType.FOLD))  # seat 0
        act(game, Action(ActionType.CALL))  # seat 1
        while game.street != "complete":
            act(game, Action(ActionType.CHECK))
        self.assertEqual(game.payouts, [0, 10, 9])
        self.assertEqual(sum(p.stack for p in game.players), 30)
        self.assertIsNone(game.state_for_player(game.players[1].player_id)["players"][0]["hole"])

    def test_uncontested_raise_refund_and_button_rotation(self):
        game = make_game([100, 100])
        act(game, Action(ActionType.RAISE, 20))
        act(game, Action(ActionType.FOLD))
        self.assertEqual(game.refunds, [18, 0])
        self.assertEqual(game.payouts, [4, 0])
        self.assertEqual([p.stack for p in game.players], [102, 98])
        old_hole = game.players[0].hole[:]
        game.start_new_hand()
        self.assertEqual(game.button_index, 1)
        self.assertNotEqual(game.players[0].hole, old_hole)

    def test_many_hands_conserve_chips_and_recover(self):
        rng = random.Random(12)
        game = HoldemGame([Player(f"P{i}", 80) for i in range(4)], seed=5)
        for _ in range(20):
            if sum(p.stack > 0 for p in game.players) < 2:
                break
            game.start_new_hand()
            all_cards = ([c for p in game.players for c in p.hole] +
                         game.deck._cards + game.table.community + game.table.burn_pile)
            self.assertEqual(len(all_cards), 52)
            self.assertEqual(len(set(all_cards)), 52)
            for step in range(200):
                if game.street == "complete":
                    break
                if game.street == "runout_vote":
                    game.resolve_runout()
                    continue
                i = game.to_act_index
                assert i is not None
                legal = game.legal_actions(game.players[i].player_id)
                if legal.can_raise and rng.random() < .12:
                    amount = legal.max_raise_to if legal.max_raise_to < legal.min_raise_to else legal.min_raise_to
                    choice = Action(ActionType.RAISE, amount)
                elif legal.can_bet and rng.random() < .12:
                    choice = Action(ActionType.BET, legal.min_bet)
                elif legal.can_check:
                    choice = Action(ActionType.CHECK)
                elif rng.random() < .15:
                    choice = Action(ActionType.FOLD)
                else:
                    choice = Action(ActionType.CALL)
                act(game, choice)
                self.assertEqual(sum(p.stack for p in game.players) + game.table.pot, 320)
                if step % 5 == 0:
                    game = HoldemGame.from_private_snapshot(json.loads(json.dumps(game.export_private_snapshot())))
            else:
                self.fail("Hand did not finish")
            self.assertEqual(game.table.pot, 0)
            self.assertEqual(sum(p.stack for p in game.players), 320)

    def test_per_pot_runout_vote_and_private_snapshot(self):
        game = make_game([5, 10, 20])
        game.players[0].hole = [Card.from_code(c) for c in ("As", "Ah")]
        game.players[1].hole = [Card.from_code(c) for c in ("Ks", "Kh")]
        game.players[2].hole = [Card.from_code(c) for c in ("Qh", "Qd")]
        draw_order = ("Ts", "2c", "3d", "4h", "Js", "8s", "7c", "9c",
                      "Tc", "Qc", "2d", "3h", "Jc", "5c", "7s", "8d")
        game.deck._cards = [Card.from_code(c) for c in reversed(draw_order)]
        act(game, Action(ActionType.RAISE, 5))
        act(game, Action(ActionType.RAISE, 10))
        act(game, Action(ActionType.CALL))
        self.assertEqual(game.street, "runout_vote")
        observer = game.state_for_observer("observer")
        self.assertTrue(all(p["hole"] is None for p in observer["players"]))
        self.assertNotIn("deck", json.dumps(observer))
        game.submit_runout_vote(game.players[0].player_id, "once", game.version)
        game.submit_runout_vote(game.players[1].player_id, "twice", game.version)
        restored = HoldemGame.from_private_snapshot(json.loads(json.dumps(game.export_private_snapshot())))
        restored.submit_runout_vote(restored.players[2].player_id, "twice", restored.version)
        self.assertEqual(restored.street, "complete")
        self.assertEqual([pot["runs"] for pot in restored.runout_pots], [1, 2])
        self.assertEqual([(a["board"], a["player_index"], a["amount"])
                          for a in restored.runout_pots[1]["awards"]],
                         [(1, 1, 5), (2, 2, 5)])
        self.assertEqual([p.stack for p in restored.players], [15, 5, 15])
        self.assertEqual(restored.refunds, [0, 0, 0])
        self.assertEqual(len(restored.runout_boards), 2)
        self.assertEqual(len(set(restored.runout_boards[0] + restored.runout_boards[1])), 10)
        with self.assertRaises(InvalidAction):
            restored.resolve_runout()

    def test_clock_extension_once_and_pause_remaining(self):
        game = make_game([100, 100])
        actor = game.players[game.to_act_index].player_id
        game.start_clock(60, 1000)
        with self.assertRaises(InvalidAction):
            game.extend_clock(actor, 60, 1050)
        game.extend_clock(actor, 60, 1056)
        self.assertEqual(game.deadline_at, 1120)
        with self.assertRaises(InvalidAction):
            game.extend_clock(actor, 60, 1118)
        game.pause_clock(1080)
        self.assertEqual(game.paused_remaining, 40)
        self.assertIsNone(game.deadline_at)
        restored = HoldemGame.from_private_snapshot(game.export_private_snapshot())
        restored.resume_clock(5000)
        self.assertEqual(restored.deadline_at, 5040)


if __name__ == "__main__":
    unittest.main()
