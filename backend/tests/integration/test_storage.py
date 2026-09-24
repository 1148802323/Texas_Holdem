import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
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

    def tearDown(self):
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
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
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
        self.assertEqual(view["status"], "waiting_next_hand")
        self.assertNotIn("hole", json.dumps(view))
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


if __name__ == "__main__":
    unittest.main()
