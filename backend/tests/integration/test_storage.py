import json
import sqlite3
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from backend.app.engine.actions import Action, ActionType
from backend.app.engine.game import InvalidAction
from backend.app.services.storage import AccessDenied, Conflict, PokerStore


class StorageTests(unittest.TestCase):
    def setUp(self):
        # A direct workspace file avoids Windows TemporaryDirectory ACL quirks.
        self.path = Path("database") / f"test_{uuid4().hex}.sqlite3"
        self.store = PokerStore(self.path)
        self.store.initialize()
        self.random_button = patch("backend.app.services.storage.secrets.randbelow", return_value=0)
        self.random_button.start()

    def tearDown(self):
        self.random_button.stop()
        self.path.unlink(missing_ok=True)

    def room(self, first=2000, second=2000):
        room_id = self.store.create_room(1, 2, 3, 2000)
        alice = self.store.join_player(room_id, "Alice", 0)
        bob = self.store.join_player(room_id, "Bob", 1)
        if first:
            self.store.buy_in(room_id, alice.session_token, first, "alice-first")
        if second:
            self.store.buy_in(room_id, bob.session_token, second, "bob-first")
        return room_id, alice, bob

    def test_schema_identity_and_unique_seats(self):
        room_id, alice, bob = self.room()
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
            stored = conn.execute("SELECT session_token_hash FROM room_players WHERE id = ?",
                                  (alice.player_id,)).fetchone()[0]
        self.assertNotEqual(stored, alice.session_token)
        with self.assertRaises(Conflict):
            self.store.join_player(room_id, " alice ", 2)
        with self.assertRaises(Conflict):
            self.store.join_player(room_id, "Carol", 1)
        with self.assertRaises(AccessDenied):
            self.store.buyin_history(room_id, "Bob")
        self.assertEqual(len(self.store.buyin_history(room_id, bob.session_token)), 1)

    def test_topup_limit_is_current_stack_not_lifetime_total(self):
        room_id, alice, bob = self.room(first=401, second=2000)
        hand = self.store.start_hand(room_id, "hand-1")
        with self.assertRaises(Conflict):
            self.store.buy_in(room_id, alice.session_token, 1, "during-hand")
        self.store.apply_action(room_id, hand["hand_id"], alice.session_token,
                                Action(ActionType.FOLD), hand["version"], "fold-1")
        self.assertEqual(self.store.player_view(room_id, alice.session_token)["players"][0]["stack"], 400)
        entry = self.store.buy_in(room_id, alice.session_token, 1600, "topup")
        self.assertEqual((entry["stack_before"], entry["stack_after"]), (400, 2000))
        self.assertEqual(self.store.room_overview(room_id)["players"][0]["max_topup_between_hands"], 0)
        self.assertEqual(self.store.buy_in(room_id, alice.session_token, 1600, "topup"), entry)
        with self.assertRaises(Conflict):
            self.store.buy_in(room_id, alice.session_token, 100, "topup")
        with self.assertRaises(Conflict):
            self.store.buy_in(room_id, alice.session_token, 1, "over-cap")
        self.assertEqual(sum(x["amount"] for x in
                             self.store.buyin_history(room_id, alice.session_token)), 2001)
        # Bob won one chip and keeps it; the cap applies only to added chips.
        self.assertEqual(self.store.player_view(room_id, bob.session_token)["players"][1]["stack"], 2001)
        with self.assertRaises(Conflict):
            self.store.buy_in(room_id, bob.session_token, 1, "bob-over")

    def test_zero_stack_can_buy_full_limit(self):
        room_id, alice, _ = self.room(first=0, second=2000)
        entry = self.store.buy_in(room_id, alice.session_token, 2000, "from-zero")
        self.assertEqual((entry["stack_before"], entry["stack_after"]), (0, 2000))
        with self.assertRaises(Conflict):
            self.store.buy_in(room_id, alice.session_token, 1, "too-much")

    def test_restart_resume_idempotent_action_and_private_history(self):
        room_id, alice, bob = self.room(first=100, second=100)
        hand = self.store.start_hand(room_id, "hand-1")
        self.assertEqual(self.store.start_hand(room_id, "hand-1")["hand_id"], hand["hand_id"])
        first_view = self.store.player_view(room_id, alice.session_token)
        self.assertEqual(first_view["hand_id"], hand["hand_id"])
        self.assertIsNone(first_view["players"][1]["hole"])
        self.assertNotIn("deck", json.dumps(first_view))
        reopened = PokerStore(self.path)
        reopened.initialize()
        self.assertEqual(reopened.load_game(room_id).game_state().version, hand["version"])
        with self.assertRaises(InvalidAction):
            reopened.apply_action(room_id, hand["hand_id"], alice.session_token,
                                  Action(ActionType.CHECK), hand["version"], "bad-check")
        first = reopened.apply_action(room_id, hand["hand_id"], alice.session_token,
                                      Action(ActionType.FOLD), hand["version"], "fold")
        self.assertEqual(first["status"], "complete")
        replay = reopened.apply_action(room_id, hand["hand_id"], alice.session_token,
                                       Action(ActionType.FOLD), hand["version"], "fold")
        self.assertTrue(replay["replayed"])
        with self.assertRaises(Conflict):
            reopened.apply_action(room_id, hand["hand_id"], bob.session_token,
                                  Action(ActionType.FOLD), hand["version"], "fold")
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM hand_actions").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM hand_players WHERE ending_stack IS NOT NULL").fetchone()[0], 2)
        self.assertEqual(len(reopened.hand_history(room_id, alice.session_token)), 1)
        self.assertEqual(reopened.hand_history(room_id, alice.session_token)[0]["blind_paid"], 1)
        bob_view = reopened.player_view(room_id, bob.session_token)
        self.assertIsNone(bob_view["players"][0]["hole"])
        with self.assertRaises(AccessDenied):
            reopened.player_view(room_id, "Alice")
        next_hand = reopened.start_hand(room_id, "hand-2")
        self.assertEqual(next_hand["hand_number"], 2)
        self.assertEqual(reopened.load_game(room_id).button_index, 1)
        with self.assertRaises(Conflict):
            reopened.apply_action(room_id, hand["hand_id"], alice.session_token,
                                  Action(ActionType.CALL), hand["version"], "late")

    def test_first_button_is_random_then_rotates_by_physical_seat(self):
        room_id = self.store.create_room(1, 2, 6, 2000)
        players = {}
        for name, seat in (("Alice", 0), ("Bob", 2), ("Carol", 5)):
            access = self.store.join_player(room_id, name, seat)
            players[access.player_id] = access.session_token
            self.store.buy_in(room_id, access.session_token, 100, f"buy-{name}")
        with patch("backend.app.services.storage.secrets.randbelow", return_value=2) as draw:
            first = self.store.start_hand(room_id, "first-button")
        draw.assert_called_once_with(3)
        self.assertEqual(self.store.load_game(room_id).button_index, 2)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT button_seat FROM hands WHERE id = ?",
                                          (first["hand_id"],)).fetchone()[0], 5)
        self.assertTrue(self.store.start_hand(room_id, "first-button")["replayed"])
        while self.store.load_game(room_id).street != "complete":
            game = self.store.load_game(room_id)
            actor = game.players[game.to_act_index]
            self.store.apply_action(room_id, first["hand_id"], players[actor.player_id],
                                    Action(ActionType.FOLD), game.version,
                                    f"fold-{game.version}")
        second = self.store.start_hand(room_id, "next-button")
        self.assertEqual(self.store.load_game(room_id).button_index, 0)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT button_seat FROM hands WHERE id = ?",
                                          (second["hand_id"],)).fetchone()[0], 0)

    def test_concurrent_buyins_do_not_break_limit(self):
        room_id, alice, _ = self.room(first=1600, second=0)

        def topup(request):
            try:
                return self.store.buy_in(room_id, alice.session_token, 400, request)
            except Conflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(topup, ("concurrent-a", "concurrent-b")))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(sum(x["amount"] for x in
                             self.store.buyin_history(room_id, alice.session_token)), 2000)

    def test_preset_nicknames_and_join_during_hand(self):
        room_id = self.store.create_room(1, 2, 3, 2000, ["Alice", "Bob", "Carol"])
        alice = self.store.join_player(room_id, " alice ", 0)
        bob = self.store.join_player(room_id, "Bob", 1)
        self.assertEqual(alice.nickname, "Alice")
        with self.assertRaises(Conflict):
            self.store.join_player(room_id, "Dave", 2)
        self.store.buy_in(room_id, alice.session_token, 100, "a")
        self.store.buy_in(room_id, bob.session_token, 100, "b")
        hand = self.store.start_hand(room_id, "start")
        carol = self.store.join_player(room_id, "Carol", 2)
        view = self.store.player_view(room_id, carol.session_token)
        self.assertEqual(view["status"], "watching")
        self.assertTrue(all(p["hole"] is None for p in view["players"]))
        self.assertNotIn("deck", json.dumps(view))
        with self.assertRaises(AccessDenied):
            self.store.player_view(room_id, carol.session_token, hand["hand_id"])

    def test_midhand_restart_preserves_turn_deadline_and_chips(self):
        room_id, alice, bob = self.room(first=100, second=100)
        hand = self.store.start_hand(room_id, "start-midhand")
        first = self.store.apply_action(
            room_id, hand["hand_id"], alice.session_token,
            Action(ActionType.CALL), hand["version"], "alice-call",
        )
        deadline_version = self.store.set_deadline(
            room_id, hand["hand_id"], 1_800_000_000.0, first["version"]
        )
        reopened = PokerStore(self.path)
        recovered = reopened.load_game(room_id)
        self.assertEqual(recovered.version, deadline_version)
        self.assertEqual(recovered.deadline_at, 1_800_000_000.0)
        self.assertEqual(recovered.to_act_index, 1)
        with self.assertRaises(InvalidAction):
            reopened.apply_action(room_id, hand["hand_id"], bob.session_token,
                                  Action(ActionType.CHECK), first["version"], "stale")
        reopened.apply_action(room_id, hand["hand_id"], bob.session_token,
                              Action(ActionType.CHECK), deadline_version, "bob-check")
        self.assertEqual(reopened.load_game(room_id).street, "flop")
        for step in range(6):
            game = reopened.load_game(room_id)
            player = game.players[game.to_act_index]
            token = alice.session_token if player.player_id == alice.player_id else bob.session_token
            reopened.apply_action(room_id, hand["hand_id"], token,
                                  Action(ActionType.CHECK), game.version, f"check-{step}")
        game = reopened.load_game(room_id)
        self.assertEqual(game.street, "complete")
        self.assertEqual(sum(p.stack for p in game.players), 200)
        with closing(sqlite3.connect(self.path)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM hand_actions WHERE hand_id = ?",
                                 (hand["hand_id"],)).fetchone()[0]
        self.assertEqual(count, 8)

    def test_timeout_folds_when_facing_bet_and_checks_when_free(self):
        room_id, alice, bob = self.room(first=100, second=100)
        first_hand = self.store.start_hand(room_id, "timeout-fold", deadline_seconds=30)
        self.store.set_deadline(room_id, first_hand["hand_id"], time.time() - 1,
                                first_hand["version"])
        self.assertEqual(self.store.expire_due_actions(), [room_id])
        self.assertEqual(self.store.load_game(room_id).street, "complete")
        self.assertEqual(self.store.load_game(room_id).history[-1].action, "fold")
        self.assertEqual(self.store.expire_due_actions(), [])

        second_hand = self.store.start_hand(room_id, "timeout-check", deadline_seconds=30)
        actor = self.store.load_game(room_id).players[self.store.load_game(room_id).to_act_index]
        actor_token = alice.session_token if actor.player_id == alice.player_id else bob.session_token
        self.store.apply_action(room_id, second_hand["hand_id"], actor_token,
                                Action(ActionType.CALL), second_hand["version"], "call-before-check",
                                deadline_seconds=30)
        game = self.store.load_game(room_id)
        self.assertEqual(game.legal_actions(game.players[game.to_act_index].player_id).to_call, 0)
        self.store.set_deadline(room_id, second_hand["hand_id"], time.time() - 1,
                                game.version)
        self.assertEqual(self.store.expire_due_actions(), [room_id])
        game = self.store.load_game(room_id)
        self.assertEqual(game.history[-1].action, "check")
        self.assertEqual(game.street, "flop")
        self.assertGreater(game.deadline_at, time.time())

    def test_restart_arms_a_legacy_hand_without_deadline(self):
        room_id, _, _ = self.room(first=100, second=100)
        hand = self.store.start_hand(room_id, "old-snapshot")
        self.assertIsNone(self.store.load_game(room_id).deadline_at)
        restarted = PokerStore(self.path)
        self.assertEqual(restarted.arm_missing_deadlines(), [room_id])
        self.assertEqual(restarted.arm_missing_deadlines(), [])
        game = restarted.load_game(room_id)
        self.assertEqual(game.version, hand["version"] + 1)
        self.assertGreater(game.deadline_at, time.time())

    def test_spectator_seat_changes_and_logout_keep_chips_with_nickname(self):
        room_id = self.store.create_room(1, 2, 3, 2000)
        alice = self.store.join_player(room_id, "Alice")
        bob = self.store.join_player(room_id, "Bob")
        self.assertIsNone(alice.seat)
        self.assertEqual(len(self.store.public_room(room_id)["players"]), 2)
        with self.assertRaises(Conflict):
            self.store.buy_in(room_id, alice.session_token, 100, "unseated")
        self.store.take_seat(room_id, alice.session_token, 0)
        self.store.take_seat(room_id, bob.session_token, 1)
        with self.assertRaises(Conflict):
            self.store.take_seat(room_id, bob.session_token, 0)
        self.store.buy_in(room_id, alice.session_token, 100, "alice-buy")
        self.store.buy_in(room_id, bob.session_token, 100, "bob-buy")
        hand = self.store.start_hand(room_id, "first")
        for operation in (
            lambda: self.store.take_seat(room_id, alice.session_token, 2),
            lambda: self.store.stand_up(room_id, alice.session_token),
            lambda: self.store.leave_room(room_id, alice.session_token),
        ):
            with self.assertRaises(Conflict):
                operation()
        carol = self.store.join_player(room_id, "Carol")
        spectator = self.store.player_view(room_id, carol.session_token)
        self.assertEqual(spectator["status"], "watching")
        self.assertIsNone(spectator["legal_actions"])
        self.assertTrue(all(p["hole"] is None for p in spectator["players"]))
        self.assertNotIn("deck", json.dumps(spectator))
        self.store.take_seat(room_id, carol.session_token, 2)
        self.assertEqual(self.store.player_view(room_id, carol.session_token)["status"], "watching")
        self.store.apply_action(room_id, hand["hand_id"], alice.session_token,
                                Action(ActionType.FOLD), hand["version"], "alice-fold")
        self.assertEqual(self.store.stand_up(room_id, alice.session_token)["stack"], 99)
        with self.assertRaises(Conflict):
            self.store.buy_in(room_id, alice.session_token, 1, "standing-buy")
        self.assertEqual(self.store.take_seat(room_id, alice.session_token, 0)["stack"], 99)
        self.assertEqual(self.store.leave_room(room_id, alice.session_token)["cashout"], 99)
        with self.assertRaises(AccessDenied):
            self.store.player_view(room_id, alice.session_token)
        self.assertEqual(next(p for p in self.store.public_room(room_id)["leaderboard"]
                              if p["nickname"] == "Alice")["profit_loss"], -1)
        self.assertNotIn("Alice", [p["nickname"] for p in self.store.public_room(room_id)["players"]])
        new_alice = self.store.join_player(room_id, "Alice")
        self.assertNotEqual(new_alice.player_id, alice.player_id)
        self.assertEqual(self.store.hand_history(room_id, new_alice.session_token), [])

    def test_migrates_v1_database_without_losing_player_references(self):
        self.path.unlink()
        migration = Path("database/migrations/001_initial.sql").read_text(encoding="utf-8")
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript(migration)
            conn.execute("INSERT INTO rooms(id, small_blind, big_blind, max_players, "
                         "max_buyin_stack, created_at) VALUES ('r', 1, 2, 2, 2000, 'now')")
            conn.execute("INSERT INTO room_players(id, room_id, nickname, nickname_key, "
                         "seat, session_token_hash, stack, total_buyin, created_at) "
                         "VALUES ('p', 'r', 'Alice', 'alice', 0, 'hash', 100, 100, 'now')")
            conn.execute("INSERT INTO hands(id, room_id, hand_number, start_request_id, "
                         "button_seat, status, version, private_snapshot, started_at) "
                         "VALUES ('h', 'r', 1, 'request', 0, 'complete', 0, '{}', 'now')")
            conn.execute("INSERT INTO hand_players(hand_id, player_id, seat, starting_stack) "
                         "VALUES ('h', 'p', 0, 100)")
            conn.commit()
        self.store.initialize()
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("SELECT player_id FROM hand_players").fetchone()[0], "p")
            self.assertEqual(conn.execute("SELECT seat FROM room_players WHERE id = 'p'").fetchone()[0], 0)

    def test_ready_confirm_auto_continue_and_pause_resume(self):
        room_id, alice, bob = self.room(first=100, second=100)
        self.store.set_ready(room_id, alice.session_token, True)
        self.store.set_ready(room_id, bob.session_token, True)
        self.assertEqual(self.store.public_room(room_id)["play_state"], "waiting")
        self.store.confirm_start(room_id, alice.session_token)
        self.assertEqual(self.store.public_room(room_id)["play_state"], "waiting")
        self.store.confirm_start(room_id, bob.session_token)
        self.assertEqual(self.store.public_room(room_id)["play_state"], "running")
        self.assertEqual(self.store.advance_rooms(), [room_id])
        hand = self.store.load_game(room_id)
        remaining = hand.deadline_at - time.time()
        self.assertGreater(remaining, 50)
        self.store.pause_room(room_id)
        paused = self.store.load_game(room_id)
        self.assertIsNone(paused.deadline_at)
        self.assertAlmostEqual(paused.paused_remaining, remaining, delta=2)
        self.assertEqual(self.store.expire_due_actions(time.time() + 1000, None), [])
        with self.assertRaises(Conflict):
            self.store.apply_action(room_id, hand.hand_id, alice.session_token,
                                    Action(ActionType.FOLD), paused.version, "while-paused")
        self.store.resume_room(room_id)
        resumed = self.store.load_game(room_id)
        self.assertAlmostEqual(resumed.deadline_at - time.time(), remaining, delta=2)
        self.store.apply_action(room_id, hand.hand_id, alice.session_token,
                                Action(ActionType.FOLD), resumed.version, "finish-first")
        self.assertEqual(self.store.load_game(room_id).street, "complete")
        self.assertTrue(all(p["ready"] for p in self.store.public_room(room_id)["players"]))
        self.assertEqual(self.store.advance_rooms(time.time() + 10), [room_id])
        self.assertEqual(self.store.load_game(room_id).hand_number, 2)

    def test_runout_vote_timeout_survives_restart(self):
        room_id = self.store.create_room(1, 2, 2, 10, runout_vote_seconds=60)
        alice = self.store.join_player(room_id, "Alice", 0)
        bob = self.store.join_player(room_id, "Bob", 1)
        self.store.buy_in(room_id, alice.session_token, 1, "alice")
        self.store.buy_in(room_id, bob.session_token, 1, "bob")
        self.store.set_ready(room_id, alice.session_token, True)
        self.store.set_ready(room_id, bob.session_token, True)
        self.store.advance_rooms()
        game = self.store.load_game(room_id)
        self.assertEqual(game.street, "runout_vote")
        self.store.pause_room(room_id)
        self.assertEqual(self.store.expire_due_actions(time.time() + 1000, None), [])
        self.store.resume_room(room_id)
        game = self.store.load_game(room_id)
        self.store.vote_runout(room_id, game.hand_id, alice.session_token, "twice", game.version)
        reopened = PokerStore(self.path)
        vote = reopened.load_game(room_id)
        self.assertEqual(vote.runout_votes[0], "twice")
        self.assertEqual(reopened.expire_due_actions(vote.deadline_at + 1, None), [room_id])
        settled = reopened.load_game(room_id)
        self.assertEqual(settled.street, "complete")
        self.assertEqual(len(settled.runout_boards), 1)
        self.assertEqual(sum(p.stack for p in settled.players), 2)
        self.assertEqual(reopened.expire_due_actions(vote.deadline_at + 1, None), [])

    def test_extension_is_persisted_and_only_current_actor_can_use_it(self):
        room_id, alice, bob = self.room(first=100, second=100)
        hand = self.store.start_hand(room_id, "extend", deadline_seconds=60)
        game = self.store.load_game(room_id)
        self.store.set_deadline(room_id, hand["hand_id"], time.time() + 4, game.version)
        game = self.store.load_game(room_id)
        with self.assertRaises(InvalidAction):
            self.store.extend_decision(room_id, hand["hand_id"], bob.session_token,
                                       game.version)
        extended = self.store.extend_decision(room_id, hand["hand_id"], alice.session_token,
                                              game.version)
        restored = PokerStore(self.path).load_game(room_id)
        self.assertTrue(restored.extension_used)
        self.assertEqual(restored.version, extended["version"])
        self.assertGreater(restored.deadline_at - time.time(), 60)
        with self.assertRaises(InvalidAction):
            self.store.extend_decision(room_id, hand["hand_id"], alice.session_token,
                                       restored.version)

    def test_archived_room_disappears_but_keeps_buyins_and_completed_hand(self):
        room_id, alice, bob = self.room(first=100, second=100)
        hand = self.store.start_hand(room_id, "archived-hand", deadline_seconds=30)
        archived = self.store.archive_room(room_id)
        self.assertTrue(archived["active_hand_finishing"])
        self.assertEqual(self.store.list_rooms(), [])
        self.assertEqual(self.store.public_room(room_id)["status"], "closed")
        with self.assertRaises(Conflict):
            self.store.join_player(room_id, "Carol")
        with self.assertRaises(Conflict):
            self.store.request_start(room_id)
        with self.assertRaises(Conflict):
            self.store.set_ready(room_id, alice.session_token, True)
        self.store.apply_action(room_id, hand["hand_id"], alice.session_token,
                                Action(ActionType.FOLD), hand["version"], "last-action")
        self.assertEqual(self.store.load_game(room_id).street, "complete")
        self.assertEqual(self.store.advance_rooms(time.time() + 10), [])
        self.assertEqual(len(self.store.buyin_history(room_id, alice.session_token)), 1)
        self.assertEqual(len(self.store.hand_history(room_id, alice.session_token)), 1)
        self.assertTrue(self.store.archive_room(room_id)["replayed"])

    def test_archiving_paused_hand_restores_clock_for_final_settlement(self):
        room_id, alice, bob = self.room(first=100, second=100)
        self.store.set_ready(room_id, alice.session_token, True)
        self.store.set_ready(room_id, bob.session_token, True)
        self.store.confirm_start(room_id, alice.session_token)
        self.store.confirm_start(room_id, bob.session_token)
        self.store.advance_rooms()
        self.store.pause_room(room_id)
        paused = self.store.load_game(room_id)
        self.assertIsNone(paused.deadline_at)
        self.store.archive_room(room_id)
        resumed = self.store.load_game(room_id)
        self.assertIsNotNone(resumed.deadline_at)
        self.assertIsNone(resumed.paused_remaining)
        self.assertEqual(self.store.expire_due_actions(resumed.deadline_at + 1, None), [room_id])
        self.assertEqual(self.store.load_game(room_id).street, "complete")
        self.assertIsNone(self.store.room_overview(room_id)["next_hand_at"])

    def test_archived_hand_keeps_clock_until_every_decision_finishes(self):
        room_id, alice, bob = self.room(first=100, second=100)
        hand = self.store.start_hand(room_id, "archive-mid-hand")
        self.store.archive_room(room_id)
        armed = self.store.load_game(room_id)
        self.assertIsNotNone(armed.deadline_at)
        self.store.apply_action(room_id, hand["hand_id"], alice.session_token,
                                Action(ActionType.CALL), armed.version, "archived-call")
        self.assertIsNotNone(self.store.load_game(room_id).deadline_at)
        for _ in range(8):
            if self.store.load_game(room_id).street == "complete":
                break
            self.assertEqual(self.store.expire_due_actions(time.time() + 1000, None), [room_id])
        self.assertEqual(self.store.load_game(room_id).street, "complete")
        self.assertIsNone(self.store.room_overview(room_id)["next_hand_at"])

    def test_recovery_rotates_token_without_changing_active_hand(self):
        room_id, alice, bob = self.room(first=100, second=100)
        hand = self.store.start_hand(room_id, "recover-active", deadline_seconds=60)
        before = self.store.load_game(room_id)
        first = self.store.issue_recovery_code(room_id, alice.player_id)
        second = self.store.issue_recovery_code(room_id, alice.player_id)
        with self.assertRaises(AccessDenied):
            self.store.recover_player(room_id, first["code"])
        restored = PokerStore(self.path).recover_player(room_id, second["code"])
        self.assertEqual(restored.player_id, alice.player_id)
        self.assertEqual(restored.seat, 0)
        self.assertNotEqual(restored.session_token, alice.session_token)
        with self.assertRaises(AccessDenied):
            self.store.player_view(room_id, alice.session_token)
        with self.assertRaises(AccessDenied):
            self.store.recover_player(room_id, second["code"])
        self.assertEqual(self.store.load_game(room_id).version, before.version)
        self.assertEqual(self.store.load_game(room_id).deadline_at, before.deadline_at)
        self.assertEqual(self.store.player_view(room_id, restored.session_token)["hand_id"], hand["hand_id"])
        self.store.apply_action(room_id, hand["hand_id"], restored.session_token,
                                Action(ActionType.FOLD), before.version, "recovered-fold")
        self.assertEqual(self.store.load_game(room_id).street, "complete")
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertNotIn(second["code"], " ".join(str(row) for row in conn.execute(
                "SELECT code_hash FROM player_recovery_codes")))

    def test_recovery_code_expires_and_admin_removal_revokes_it(self):
        room_id, alice, bob = self.room(first=100, second=100)
        expired = self.store.issue_recovery_code(room_id, alice.player_id)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("UPDATE player_recovery_codes SET expires_at = ? WHERE player_id = ?",
                         (time.time() - 1, alice.player_id))
            conn.commit()
        with self.assertRaises(AccessDenied):
            self.store.recover_player(room_id, expired["code"])
        outstanding = self.store.issue_recovery_code(room_id, bob.player_id)
        self.store.request_player_action(room_id, bob.player_id, "remove")
        with self.assertRaises(AccessDenied):
            self.store.recover_player(room_id, outstanding["code"])
        with self.assertRaises(AccessDenied):
            self.store.player_view(room_id, bob.session_token)
        self.assertEqual(self.store.room_overview(room_id)["admin_events"][0]["action"], "remove_applied")

    def test_admin_stand_and_remove_wait_for_settlement_and_conserve_chips(self):
        room_id, alice, bob = self.room(first=100, second=100)
        hand = self.store.start_hand(room_id, "queued-management")
        self.assertTrue(self.store.request_player_action(room_id, alice.player_id, "stand")["queued"])
        self.assertTrue(self.store.request_player_action(room_id, bob.player_id, "remove")["queued"])
        self.assertTrue(self.store.request_player_action(room_id, bob.player_id, "remove")["replayed"])
        self.assertEqual(self.store.room_overview(room_id)["players"][0]["pending_admin_action"], "stand")
        self.assertEqual(self.store.player_view(room_id, bob.session_token)["hand_id"], hand["hand_id"])
        self.store.apply_action(room_id, hand["hand_id"], alice.session_token,
                                Action(ActionType.FOLD), hand["version"], "finish-before-removal")
        alice_row = next(p for p in self.store.room_overview(room_id)["players"]
                         if p["player_id"] == alice.player_id)
        self.assertIsNone(alice_row["seat"])
        self.assertEqual(alice_row["stack"], 99)
        with self.assertRaises(AccessDenied):
            self.store.player_view(room_id, bob.session_token)
        with closing(sqlite3.connect(self.path)) as conn:
            bob_row = conn.execute("SELECT stack, total_cashout FROM room_players WHERE id = ?",
                                   (bob.player_id,)).fetchone()
            settled = conn.execute("SELECT ending_stack FROM hand_players WHERE hand_id = ? "
                                   "AND player_id = ?", (hand["hand_id"], bob.player_id)).fetchone()[0]
        self.assertEqual(tuple(bob_row), (0, 101))
        self.assertEqual(settled, 101)
        self.assertEqual(alice_row["stack"] + bob_row[1], 200)
        self.assertIn("stand_applied", [e["action"] for e in self.store.room_overview(room_id)["admin_events"]])

    def test_admin_stand_between_hands_keeps_identity_and_stack(self):
        room_id, alice, bob = self.room(first=100, second=100)
        result = self.store.request_player_action(room_id, alice.player_id, "stand")
        self.assertFalse(result["queued"])
        row = next(p for p in self.store.room_overview(room_id)["players"]
                   if p["player_id"] == alice.player_id)
        self.assertIsNone(row["seat"])
        self.assertEqual(row["stack"], 100)
        self.assertEqual(self.store.take_seat(room_id, alice.session_token, 2)["stack"], 100)

    def test_queued_removal_stops_auto_play_after_settlement(self):
        room_id, alice, bob = self.room(first=100, second=100)
        self.store.set_ready(room_id, alice.session_token, True)
        self.store.set_ready(room_id, bob.session_token, True)
        self.store.confirm_start(room_id, alice.session_token)
        self.store.confirm_start(room_id, bob.session_token)
        self.store.advance_rooms()
        game = self.store.load_game(room_id)
        self.assertEqual(self.store.room_overview(room_id)["play_state"], "running")
        self.assertTrue(self.store.request_player_action(room_id, bob.player_id, "remove")["queued"])
        self.store.apply_action(room_id, game.hand_id, alice.session_token,
                                Action(ActionType.FOLD), game.version, "stop-after-remove")
        overview = self.store.room_overview(room_id)
        self.assertEqual(overview["play_state"], "waiting")
        self.assertIsNone(overview["next_hand_at"])
        self.assertEqual(self.store.advance_rooms(time.time() + 100), [])


if __name__ == "__main__":
    unittest.main()
