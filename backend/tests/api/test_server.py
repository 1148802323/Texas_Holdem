import json
import os
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app.api.server import create_app


class BrowserApiTests(unittest.TestCase):
    def test_server_refuses_to_start_without_admin_password(self):
        with patch.dict(os.environ, {"TEXAS_ADMIN_PASSWORD": ""}):
            with self.assertRaises(RuntimeError):
                create_app("database/unused.sqlite3")

    def test_admin_login_is_rate_limited(self):
        for _ in range(5):
            self.assertEqual(self.client.post("/api/admin/login", json={"password": "wrong"}).status_code, 401)
        self.assertEqual(self.client.post("/api/admin/login", json={"password": "wrong"}).status_code, 429)

    def setUp(self):
        self.path = Path("database") / f"api_test_{uuid4().hex}.sqlite3"
        self.random_button = patch("backend.app.services.storage.secrets.randbelow", return_value=0)
        self.random_button.start()
        self.app = create_app(self.path, "test-admin-password")
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.random_button.stop()
        self.path.unlink(missing_ok=True)

    def create_room(self):
        self.assertEqual(self.client.post("/api/admin/login", json={"password": "bad"}).status_code, 401)
        login = self.client.post("/api/admin/login", json={"password": "test-admin-password"})
        self.assertEqual(login.status_code, 200)
        self.assertIn("httponly", login.headers["set-cookie"].lower())
        response = self.client.post("/api/admin/rooms", json={
            "small_blind": 1, "big_blind": 2, "max_players": 2,
            "max_buyin_stack": 2000,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["room_id"]

    def test_admin_auth_join_cookie_reconnect_and_private_websocket(self):
        self.assertEqual(self.client.get("/admin").status_code, 200)
        self.assertEqual(self.client.get("/assets/main.js").status_code, 200)
        self.assertEqual(self.client.get("/assets/styles/app.css").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/rooms").status_code, 401)
        room_id = self.create_room()
        self.assertEqual(self.client.get(f"/r/{room_id}").status_code, 200)
        self.assertEqual(self.client.post("/api/admin/rooms", json={
            "small_blind": 1, "big_blind": 2, "max_players": 2,
            "max_buyin_stack": 2000,
        }, headers={"Origin": "https://evil.example"}).status_code, 403)
        joined = self.client.post(f"/api/rooms/{room_id}/join", json={"nickname": "Alice", "seat": 0})
        self.assertEqual(joined.status_code, 200, joined.text)
        cookie_name = f"th_room_{room_id}"
        alice_cookie = self.client.cookies.get(cookie_name)
        self.assertIn("httponly", joined.headers["set-cookie"].lower())
        self.assertEqual(self.client.post(f"/api/rooms/{room_id}/join", json={"nickname": "alice", "seat": 1}).status_code, 409)
        self.client.cookies.delete(cookie_name)  # a second browser joins Bob
        self.assertEqual(self.client.post(f"/api/rooms/{room_id}/join", json={"nickname": "Bob", "seat": 1}).status_code, 200)
        bob_cookie = self.client.cookies.get(cookie_name)
        self.assertNotEqual(alice_cookie, bob_cookie)
        self.client.cookies.set(cookie_name, alice_cookie)
        self.assertEqual(self.client.post(f"/api/rooms/{room_id}/buyins", json={"amount": 100, "request_id": "a"}).status_code, 200)
        self.client.cookies.set(cookie_name, bob_cookie)
        self.assertEqual(self.client.post(f"/api/rooms/{room_id}/buyins", json={"amount": 100, "request_id": "b"}).status_code, 200)
        self.assertEqual(self.client.post(f"/api/rooms/{room_id}/ready", json={"ready": True}).status_code, 200)
        self.client.cookies.set(cookie_name, alice_cookie)
        self.assertEqual(self.client.post(f"/api/rooms/{room_id}/ready", json={"ready": True}).status_code, 200)
        self.app.state.store.advance_rooms()
        with self.client.websocket_connect(f"/ws/rooms/{room_id}") as socket:
            waiting = socket.receive_json()
            self.assertEqual(waiting["type"], "state")
            alice_state = waiting["game"]
            hand_id = alice_state["hand_id"]
            self.assertEqual(alice_state["to_act_index"], 0)
            self.assertEqual(len(alice_state["players"][0]["hole"]), 2)
            self.assertIsNone(alice_state["players"][1]["hole"])
            self.assertNotIn("deck", json.dumps(waiting))
        self.client.cookies.set(cookie_name, bob_cookie)
        with self.client.websocket_connect(f"/ws/rooms/{room_id}") as socket:
            bob_state = socket.receive_json()["game"]
            self.assertIsNone(bob_state["players"][0]["hole"])
            self.assertEqual(len(bob_state["players"][1]["hole"]), 2)
            self.client.cookies.set(cookie_name, alice_cookie)
            action = self.client.post(
                f"/api/rooms/{room_id}/hands/{hand_id}/actions",
                json={"action": "fold", "amount": 0, "expected_version": alice_state["version"],
                      "request_id": "alice-fold"},
            )
            self.assertEqual(action.status_code, 200, action.text)
            update = socket.receive_json()
            self.assertEqual(update["game"]["street"], "complete")
            self.assertIsNone(update["game"]["players"][0]["hole"])
        self.client.cookies.set(cookie_name, alice_cookie)
        history = self.client.get(f"/api/rooms/{room_id}/history")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(len(history.json()["buyins"]), 1)
        self.assertEqual(len(history.json()["hands"]), 1)

    def test_bad_identity_cannot_take_seat_or_see_history(self):
        room_id = self.create_room()
        self.client.post(f"/api/rooms/{room_id}/join", json={"nickname": "Alice", "seat": 0})
        self.client.cookies.clear()
        self.assertEqual(self.client.get(f"/api/rooms/{room_id}/history").status_code, 401)
        self.assertEqual(self.client.get(f"/api/rooms/{room_id}/state").status_code, 401)
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(f"/ws/rooms/{room_id}"):
                pass
        self.assertEqual(self.client.post(f"/api/rooms/{room_id}/join", json={"nickname": "Alice", "seat": 1}).status_code, 409)
        public = self.client.get(f"/api/rooms/{room_id}").json()
        self.assertEqual(public["players"][0]["nickname"], "Alice")
        self.assertNotIn("session_token_hash", json.dumps(public))

    def test_spectator_can_sit_watch_and_leave_after_hand(self):
        room_id = self.create_room()
        cookie_name = f"th_room_{room_id}"
        join = self.client.post(f"/api/rooms/{room_id}/join", json={"nickname": "Alice"})
        self.assertEqual(join.status_code, 200)
        self.assertIsNone(join.json()["seat"])
        alice_cookie = self.client.cookies.get(cookie_name)
        self.assertEqual(self.client.post(f"/api/rooms/{room_id}/buyins",
                         json={"amount": 100, "request_id": "before-seat"}).status_code, 409)
        self.assertEqual(self.client.post(f"/api/rooms/{room_id}/seat", json={"seat": 0}).status_code, 200)
        self.assertEqual(self.client.post(f"/api/rooms/{room_id}/buyins",
                         json={"amount": 100, "request_id": "alice-buy"}).status_code, 200)
        self.client.cookies.delete(cookie_name)
        self.client.post(f"/api/rooms/{room_id}/join", json={"nickname": "Bob", "seat": 1})
        bob_cookie = self.client.cookies.get(cookie_name)
        self.client.post(f"/api/rooms/{room_id}/buyins",
                         json={"amount": 100, "request_id": "bob-buy"})
        self.client.post(f"/api/rooms/{room_id}/ready", json={"ready": True})
        self.client.cookies.set(cookie_name, alice_cookie)
        self.client.post(f"/api/rooms/{room_id}/ready", json={"ready": True})
        self.app.state.store.advance_rooms()
        started = self.client.get(f"/api/rooms/{room_id}/state").json()["game"]
        self.client.cookies.delete(cookie_name)
        self.client.post(f"/api/rooms/{room_id}/join", json={"nickname": "Carol"})
        with self.client.websocket_connect(f"/ws/rooms/{room_id}") as socket:
            view = socket.receive_json()["game"]
            self.assertEqual(view["status"], "watching")
            self.assertEqual(len(view["players"]), 2)
            self.assertTrue(all(p["hole"] is None for p in view["players"]))
            self.assertNotIn("deck", json.dumps(view))
        self.client.cookies.set(cookie_name, alice_cookie)
        for endpoint, body in (("seat", {"seat": 1}), ("stand", None), ("leave", None)):
            self.assertEqual(self.client.post(f"/api/rooms/{room_id}/{endpoint}",
                             json=body).status_code, 409)
        self.client.post(f"/api/rooms/{room_id}/hands/{started['hand_id']}/actions",
                         json={"action": "fold", "amount": 0,
                               "expected_version": started["version"], "request_id": "fold"})
        self.assertEqual(self.client.post(f"/api/rooms/{room_id}/stand").json()["stack"], 99)
        self.assertEqual(self.client.post(f"/api/rooms/{room_id}/seat", json={"seat": 0}).json()["stack"], 99)
        leave = self.client.post(f"/api/rooms/{room_id}/leave")
        self.assertEqual(leave.json()["cashout"], 99)
        self.assertEqual(self.client.get(f"/api/rooms/{room_id}/state").status_code, 401)
        self.assertEqual(self.client.get(f"/api/rooms/{room_id}/history").status_code, 401)
        leaderboard = self.client.get(f"/api/rooms/{room_id}").json()["leaderboard"]
        self.assertEqual(next(p["profit_loss"] for p in leaderboard if p["nickname"] == "Alice"), -1)
        self.assertNotEqual(bob_cookie, alice_cookie)

    def test_ready_confirmation_pause_extend_and_audio_manifest(self):
        room_id = self.create_room()
        cookie_name = f"th_room_{room_id}"
        self.client.post(f"/api/rooms/{room_id}/join", json={"nickname": "Alice", "seat": 0})
        alice_cookie = self.client.cookies.get(cookie_name)
        self.client.post(f"/api/rooms/{room_id}/buyins", json={"amount": 100, "request_id": "a"})
        self.client.cookies.delete(cookie_name)
        self.client.post(f"/api/rooms/{room_id}/join", json={"nickname": "Bob", "seat": 1})
        bob_cookie = self.client.cookies.get(cookie_name)
        self.client.post(f"/api/rooms/{room_id}/buyins", json={"amount": 100, "request_id": "b"})
        self.assertEqual(self.client.post(f"/api/admin/rooms/{room_id}/hands",
                         json={"request_id": "before-ready"}).status_code, 409)
        self.client.post(f"/api/rooms/{room_id}/ready", json={"ready": True})
        self.client.cookies.set(cookie_name, alice_cookie)
        self.client.post(f"/api/rooms/{room_id}/ready", json={"ready": True})
        self.app.state.store.advance_rooms()
        state = self.client.get(f"/api/rooms/{room_id}/state").json()
        hand_id = state["game"]["hand_id"]
        self.assertEqual(state["game"]["decision_total_seconds"], 60)
        self.assertEqual(self.client.post(f"/api/admin/rooms/{room_id}/pause").status_code, 200)
        paused = self.client.get(f"/api/rooms/{room_id}/state").json()
        self.assertEqual(paused["room"]["play_state"], "paused")
        self.assertIsNone(paused["game"]["deadline_at"])
        self.assertEqual(self.client.post(f"/api/rooms/{room_id}/hands/{hand_id}/actions",
                         json={"action": "fold", "expected_version": paused["game"]["version"],
                               "request_id": "paused-action"}).status_code, 409)
        self.assertEqual(self.client.post(f"/api/admin/rooms/{room_id}/resume").status_code, 200)
        resumed = self.client.get(f"/api/rooms/{room_id}/state").json()["game"]
        self.assertIsNotNone(resumed["deadline_at"])
        self.assertEqual(self.client.post(f"/api/rooms/{room_id}/hands/{hand_id}/extend",
                         json={"expected_version": resumed["version"]}).status_code, 422)
        manifest = self.client.get("/api/audio-manifest")
        self.assertEqual(manifest.status_code, 200)
        self.assertIn("allin", manifest.json())
        self.client.cookies.set(cookie_name, bob_cookie)

    def test_admin_can_delete_room_without_erasing_its_records(self):
        room_id = self.create_room()
        self.client.post(f"/api/rooms/{room_id}/join", json={"nickname": "Alice", "seat": 0})
        self.client.post(f"/api/rooms/{room_id}/buyins",
                         json={"amount": 100, "request_id": "archive-buyin"})
        self.client.cookies.delete("th_admin")
        self.assertEqual(self.client.delete(f"/api/admin/rooms/{room_id}").status_code, 401)
        self.client.post("/api/admin/login", json={"password": "test-admin-password"})
        deleted = self.client.delete(f"/api/admin/rooms/{room_id}")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(self.client.get("/api/admin/rooms").json(), [])
        closed_invite = self.client.get(f"/api/rooms/{room_id}").json()
        self.assertEqual(closed_invite["status"], "closed")
        self.assertNotIn("players", closed_invite)
        self.assertEqual(self.client.get(f"/api/rooms/{room_id}/state").json()["room"]["players"][0]["nickname"], "Alice")
        self.assertEqual(self.client.get(f"/api/admin/rooms/{room_id}").json()["players"][0]["total_buyin"], 100)
        self.assertEqual(self.client.post(f"/api/rooms/{room_id}/join",
                         json={"nickname": "Bob"}).status_code, 409)
        self.assertTrue(self.client.delete(f"/api/admin/rooms/{room_id}").json()["replayed"])

    def test_recovery_cookie_rotation_and_admin_player_controls(self):
        room_id = self.create_room()
        cookie_name = f"th_room_{room_id}"
        joined = self.client.post(f"/api/rooms/{room_id}/join",
                                  json={"nickname": "Alice", "seat": 0}).json()
        old_cookie = self.client.cookies.get(cookie_name)
        self.client.post(f"/api/rooms/{room_id}/buyins",
                         json={"amount": 100, "request_id": "recovery-buyin"})
        player_id = joined["player_id"]
        self.client.cookies.delete("th_admin")
        self.assertEqual(self.client.post(
            f"/api/admin/rooms/{room_id}/players/{player_id}/recovery-code").status_code, 401)
        self.client.post("/api/admin/login", json={"password": "test-admin-password"})
        issued = self.client.post(
            f"/api/admin/rooms/{room_id}/players/{player_id}/recovery-code").json()
        self.assertEqual(issued["nickname"], "Alice")
        self.client.cookies.delete(cookie_name)
        recovered = self.client.post(f"/api/rooms/{room_id}/recover",
                                     json={"code": issued["code"]})
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()["player_id"], player_id)
        self.assertIn("httponly", recovered.headers["set-cookie"].lower())
        new_cookie = self.client.cookies.get(cookie_name)
        self.assertNotEqual(old_cookie, new_cookie)
        self.assertEqual(self.client.get(f"/api/rooms/{room_id}/state").status_code, 200)
        self.client.cookies.set(cookie_name, old_cookie)
        self.assertEqual(self.client.get(f"/api/rooms/{room_id}/state").status_code, 401)
        self.assertEqual(self.client.post(f"/api/rooms/{room_id}/recover",
                         json={"code": issued["code"]}).status_code, 401)
        self.client.cookies.set(cookie_name, new_cookie)
        with self.client.websocket_connect(f"/ws/rooms/{room_id}") as socket:
            self.assertEqual(socket.receive_json()["type"], "state")
            overview = self.client.get(f"/api/admin/rooms/{room_id}").json()
            self.assertTrue(overview["players"][0]["connected"])
        overview = self.client.get(f"/api/admin/rooms/{room_id}").json()
        self.assertFalse(overview["players"][0]["connected"])
        stood = self.client.post(f"/api/admin/rooms/{room_id}/players/{player_id}/stand")
        self.assertFalse(stood.json()["queued"])
        self.assertEqual(self.client.get(f"/api/admin/rooms/{room_id}").json()["players"][0]["stack"], 100)
        removed = self.client.post(f"/api/admin/rooms/{room_id}/players/{player_id}/remove")
        self.assertFalse(removed.json()["queued"])
        self.assertEqual(self.client.get(f"/api/rooms/{room_id}/state").status_code, 401)
        self.assertEqual(self.client.get(f"/api/admin/rooms/{room_id}").json()["admin_events"][0]["action"], "remove_applied")


if __name__ == "__main__":
    unittest.main()
