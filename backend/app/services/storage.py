"""SQLite persistence for private rooms and stepwise poker hands.

Every write opens a BEGIN IMMEDIATE transaction. A future HTTP layer must
authenticate administrators before calling room creation or hand start.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ..engine.actions import Action, ActionType
from ..engine.game import HoldemGame, InvalidAction
from ..engine.player import Player


class StoreError(ValueError):
    pass


class AccessDenied(StoreError):
    pass


class Conflict(StoreError):
    pass


@dataclass(frozen=True)
class PlayerAccess:
    player_id: str
    room_id: str
    nickname: str
    seat: int | None
    session_token: str  # Returned only once; persist in an HttpOnly cookie.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid4().hex


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _request_id(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or not value.strip():
        raise ValueError("request_id must be a nonempty string of at most 128 characters")
    return value


class PokerStore:
    INTER_HAND_SECONDS = 4

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def _transaction(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                migration = (Path(__file__).resolve().parents[3] /
                             "database/migrations/001_initial.sql")
                conn.executescript(migration.read_text(encoding="utf-8"))
                version = 1
            if version == 1:
                migration = (Path(__file__).resolve().parents[3] /
                             "database/migrations/002_observers.sql")
                conn.executescript(migration.read_text(encoding="utf-8"))
                if conn.execute("PRAGMA foreign_key_check").fetchone():
                    raise RuntimeError("Observer migration left invalid foreign keys")
                version = 2
            if version == 2:
                migration = (Path(__file__).resolve().parents[3] /
                             "database/migrations/003_live_play.sql")
                conn.executescript(migration.read_text(encoding="utf-8"))
                version = 3
            if version == 3:
                migration = (Path(__file__).resolve().parents[3] /
                             "database/migrations/004_player_recovery.sql")
                conn.executescript(migration.read_text(encoding="utf-8"))
            elif version != 4:
                raise RuntimeError(f"Unsupported database schema version: {version}")

    @staticmethod
    def _room(conn: sqlite3.Connection, room_id: str) -> sqlite3.Row:
        room = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
        if room is None:
            raise StoreError("Room does not exist")
        return room

    @staticmethod
    def _player(conn: sqlite3.Connection, room_id: str, token: str) -> sqlite3.Row:
        if not isinstance(token, str) or not token:
            raise AccessDenied("Missing player token")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        player = conn.execute(
            "SELECT * FROM room_players WHERE room_id = ? AND session_token_hash = ? AND is_active = 1",
            (room_id, digest),
        ).fetchone()
        if player is None:
            raise AccessDenied("Invalid player token")
        return player

    @staticmethod
    def _latest_hand(conn: sqlite3.Connection, room_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM hands WHERE room_id = ? ORDER BY hand_number DESC LIMIT 1",
            (room_id,),
        ).fetchone()

    @staticmethod
    def _player_by_id(conn: sqlite3.Connection, room_id: str, player_id: str) -> sqlite3.Row:
        player = conn.execute(
            "SELECT * FROM room_players WHERE room_id = ? AND id = ? AND is_active = 1",
            (room_id, player_id),
        ).fetchone()
        if player is None:
            raise StoreError("Active player does not exist")
        return player

    @staticmethod
    def _admin_event(conn: sqlite3.Connection, room_id: str, player_id: str | None,
                     action: str) -> None:
        conn.execute(
            "INSERT INTO room_admin_events(id, room_id, player_id, action, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (_new_id(), room_id, player_id, action, _now()),
        )

    @staticmethod
    def _clear_confirmations(conn: sqlite3.Connection, room_id: str) -> None:
        conn.execute("UPDATE room_players SET start_confirmed = 0 WHERE room_id = ?", (room_id,))

    @staticmethod
    def _decision_seconds(room: sqlite3.Row, game: HoldemGame) -> int:
        return room["runout_vote_seconds" if game.street == "runout_vote" else
                    f"{game.street}_seconds"]

    @staticmethod
    def _eligible_rows(conn: sqlite3.Connection, room_id: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM room_players WHERE room_id = ? AND is_active = 1 "
            "AND seat IS NOT NULL AND stack > 0 AND ready = 1 ORDER BY seat", (room_id,)
        ).fetchall()

    @classmethod
    def _maybe_start(cls, conn: sqlite3.Connection, room: sqlite3.Row) -> bool:
        if room["status"] != "open" or room["play_state"] != "waiting":
            return False
        seated = conn.execute(
            "SELECT * FROM room_players WHERE room_id = ? AND is_active = 1 "
            "AND seat IS NOT NULL ORDER BY seat", (room["id"],)
        ).fetchall()
        if len(seated) < 2 or any(not p["ready"] or p["stack"] <= 0 for p in seated):
            return False
        if len(seated) < room["max_players"] and any(not p["start_confirmed"] for p in seated):
            return False
        conn.execute("UPDATE rooms SET play_state = 'running', next_hand_at = ? WHERE id = ?",
                     (time.time(), room["id"]))
        return True

    def set_ready(self, room_id: str, token: str, ready: bool) -> dict:
        if type(ready) is not bool:
            raise ValueError("ready must be a boolean")
        with self._transaction() as conn:
            room = self._room(conn, room_id)
            if room["status"] != "open":
                raise Conflict("Room is closed")
            player = self._player(conn, room_id, token)
            if player["seat"] is None:
                raise Conflict("Take a seat before becoming ready")
            if ready and player["stack"] <= 0:
                raise Conflict("Buy chips before becoming ready")
            self._require_not_in_hand(conn, room_id, player["id"])
            conn.execute("UPDATE room_players SET ready = ?, start_confirmed = 0 WHERE id = ?",
                         (int(ready), player["id"]))
            if room["play_state"] == "waiting":
                self._clear_confirmations(conn, room_id)
                self._maybe_start(conn, room)
            return {"ready": ready}

    def confirm_start(self, room_id: str, token: str) -> dict:
        with self._transaction() as conn:
            room = self._room(conn, room_id)
            if room["status"] != "open":
                raise Conflict("Room is closed")
            player = self._player(conn, room_id, token)
            if room["play_state"] != "waiting":
                raise Conflict("Room has already started")
            if player["seat"] is None or not player["ready"] or player["stack"] <= 0:
                raise Conflict("Sit, buy chips and become ready first")
            seated = conn.execute("SELECT * FROM room_players WHERE room_id = ? AND is_active = 1 "
                                  "AND seat IS NOT NULL", (room_id,)).fetchall()
            if len(seated) < 2 or any(not p["ready"] or p["stack"] <= 0 for p in seated):
                raise Conflict("All seated players must be ready")
            conn.execute("UPDATE room_players SET start_confirmed = 1 WHERE id = ?", (player["id"],))
            started = self._maybe_start(conn, room)
            return {"confirmed": True, "started": started}

    def request_start(self, room_id: str) -> dict:
        with self._transaction() as conn:
            room = self._room(conn, room_id)
            if room["status"] != "open":
                raise Conflict("Room is closed")
            if room["play_state"] == "paused":
                raise Conflict("Resume the paused decision instead")
            if room["play_state"] == "running":
                raise Conflict("Room is already running")
            if not self._maybe_start(conn, room):
                raise Conflict("Waiting for all seated players to ready and confirm")
            return {"started": True}

    def archive_room(self, room_id: str) -> dict:
        """Remove a room from the admin list without deleting its financial history.

        An active hand remains playable until settlement, including when the
        administrator had paused its current decision.
        """
        with self._transaction() as conn:
            room = self._room(conn, room_id)
            if room["status"] == "closed":
                return {"room_id": room_id, "archived": True, "replayed": True}
            hand = self._latest_hand(conn, room_id)
            active = hand is not None and hand["status"] == "active"
            if active and room["play_state"] == "paused":
                game = HoldemGame.from_private_snapshot(json.loads(hand["private_snapshot"]))
                game.resume_clock(time.time())
                conn.execute("UPDATE hands SET version = ?, private_snapshot = ? WHERE id = ?",
                             (game.version, _json(game.export_private_snapshot()), hand["id"]))
            elif active:
                game = HoldemGame.from_private_snapshot(json.loads(hand["private_snapshot"]))
                if game.deadline_at is None:
                    game.start_clock(self._decision_seconds(room, game), time.time())
                    conn.execute("UPDATE hands SET version = ?, private_snapshot = ? WHERE id = ?",
                                 (game.version, _json(game.export_private_snapshot()), hand["id"]))
            conn.execute("UPDATE rooms SET status = 'closed', play_state = 'waiting', "
                         "next_hand_at = NULL WHERE id = ?", (room_id,))
            conn.execute("UPDATE player_recovery_codes SET revoked_at = ? "
                         "WHERE room_id = ? AND consumed_at IS NULL AND revoked_at IS NULL",
                         (time.time(), room_id))
            self._admin_event(conn, room_id, None, "room_archived")
            return {"room_id": room_id, "archived": True,
                    "active_hand_finishing": active, "replayed": False}

    def issue_recovery_code(self, room_id: str, player_id: str) -> dict:
        """Generate a short-lived, one-use secret; only the authenticated admin may call this."""
        with self._transaction() as conn:
            room = self._room(conn, room_id)
            if room["status"] != "open":
                raise Conflict("Room is closed")
            player = self._player_by_id(conn, room_id, player_id)
            now = time.time()
            conn.execute("UPDATE player_recovery_codes SET revoked_at = ? "
                         "WHERE room_id = ? AND player_id = ? AND consumed_at IS NULL "
                         "AND revoked_at IS NULL", (now, room_id, player_id))
            code = secrets.token_urlsafe(24)
            expires_at = now + 15 * 60
            conn.execute(
                "INSERT INTO player_recovery_codes(id, room_id, player_id, code_hash, "
                "expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (_new_id(), room_id, player_id, hashlib.sha256(code.encode()).hexdigest(),
                 expires_at, _now()),
            )
            self._admin_event(conn, room_id, player_id, "recovery_issued")
            return {"room_id": room_id, "player_id": player_id,
                    "nickname": player["nickname"], "code": code, "expires_at": expires_at}

    def recover_player(self, room_id: str, code: str) -> PlayerAccess:
        if not isinstance(code, str) or not 8 <= len(code.strip()) <= 128:
            raise AccessDenied("Invalid or expired recovery code")
        digest = hashlib.sha256(code.strip().encode()).hexdigest()
        with self._transaction() as conn:
            room = self._room(conn, room_id)
            if room["status"] != "open":
                raise AccessDenied("Invalid or expired recovery code")
            recovery = conn.execute(
                "SELECT c.id, c.player_id, c.expires_at, c.consumed_at, c.revoked_at, "
                "p.nickname, p.seat, p.is_active FROM player_recovery_codes c "
                "JOIN room_players p ON p.id = c.player_id "
                "WHERE c.room_id = ? AND c.code_hash = ?", (room_id, digest),
            ).fetchone()
            if (recovery is None or recovery["consumed_at"] is not None or
                    recovery["revoked_at"] is not None or
                    recovery["expires_at"] <= time.time() or not recovery["is_active"]):
                raise AccessDenied("Invalid or expired recovery code")
            token = secrets.token_urlsafe(32)
            now = time.time()
            conn.execute("UPDATE player_recovery_codes SET consumed_at = ? WHERE id = ?",
                         (now, recovery["id"]))
            conn.execute("UPDATE player_recovery_codes SET revoked_at = ? WHERE room_id = ? "
                         "AND player_id = ? AND consumed_at IS NULL AND revoked_at IS NULL",
                         (now, room_id, recovery["player_id"]))
            conn.execute("UPDATE room_players SET session_token_hash = ? WHERE id = ?",
                         (hashlib.sha256(token.encode()).hexdigest(), recovery["player_id"]))
            self._admin_event(conn, room_id, recovery["player_id"], "recovery_used")
            return PlayerAccess(recovery["player_id"], room_id, recovery["nickname"],
                                recovery["seat"], token)

    @classmethod
    def _apply_admin_action(cls, conn: sqlite3.Connection, room_id: str,
                            player_id: str, action: str) -> None:
        player = cls._player_by_id(conn, room_id, player_id)
        if action == "stand":
            conn.execute("UPDATE room_players SET seat = NULL, ready = 0, "
                         "start_confirmed = 0, pending_admin_action = NULL WHERE id = ?",
                         (player_id,))
            cls._admin_event(conn, room_id, player_id, "stand_applied")
        elif action == "remove":
            conn.execute(
                "UPDATE room_players SET seat = NULL, stack = 0, "
                "total_cashout = total_cashout + ?, is_active = 0, ready = 0, "
                "start_confirmed = 0, pending_admin_action = NULL, left_at = ? WHERE id = ?",
                (player["stack"], _now(), player_id),
            )
            conn.execute("UPDATE player_recovery_codes SET revoked_at = ? "
                         "WHERE player_id = ? AND consumed_at IS NULL AND revoked_at IS NULL",
                         (time.time(), player_id))
            cls._admin_event(conn, room_id, player_id, "remove_applied")
        else:
            raise ValueError("Unknown administrator action")

    @classmethod
    def _stop_if_insufficient(cls, conn: sqlite3.Connection, room_id: str) -> None:
        room = cls._room(conn, room_id)
        hand = cls._latest_hand(conn, room_id)
        if (room["play_state"] == "running" and
                (hand is None or hand["status"] != "active") and
                len(cls._eligible_rows(conn, room_id)) < 2):
            conn.execute("UPDATE rooms SET play_state = 'waiting', next_hand_at = NULL "
                         "WHERE id = ?", (room_id,))

    def request_player_action(self, room_id: str, player_id: str, action: str) -> dict:
        if action not in ("stand", "remove"):
            raise ValueError("Action must be stand or remove")
        with self._transaction() as conn:
            room = self._room(conn, room_id)
            if room["status"] != "open":
                raise Conflict("Room is closed")
            player = self._player_by_id(conn, room_id, player_id)
            if action == "stand" and player["seat"] is None:
                raise Conflict("Player is already standing")
            hand = self._latest_hand(conn, room_id)
            participating = bool(hand and hand["status"] == "active" and conn.execute(
                "SELECT 1 FROM hand_players WHERE hand_id = ? AND player_id = ?",
                (hand["id"], player_id),
            ).fetchone())
            if participating:
                if player["pending_admin_action"] == action:
                    return {"queued": True, "action": action, "replayed": True}
                conn.execute("UPDATE room_players SET pending_admin_action = ? WHERE id = ?",
                             (action, player_id))
                self._admin_event(conn, room_id, player_id, f"{action}_queued")
                return {"queued": True, "action": action, "replayed": False}
            self._apply_admin_action(conn, room_id, player_id, action)
            if room["play_state"] == "waiting":
                self._clear_confirmations(conn, room_id)
            self._stop_if_insufficient(conn, room_id)
            return {"queued": False, "action": action, "replayed": False}

    def connected_player_ids(self, room_id: str, tokens: list[str]) -> set[str]:
        if not tokens:
            return set()
        digests = [hashlib.sha256(token.encode()).hexdigest() for token in tokens]
        with closing(self._connect()) as conn:
            return {row["id"] for row in conn.execute(
                "SELECT id FROM room_players WHERE room_id = ? AND is_active = 1 AND "
                f"session_token_hash IN ({','.join('?' for _ in digests)})",
                [room_id, *digests],
            )}

    def pause_room(self, room_id: str, now: float | None = None) -> dict:
        with self._transaction() as conn:
            room = self._room(conn, room_id)
            if room["play_state"] != "running":
                raise Conflict("Room is not running")
            hand = self._latest_hand(conn, room_id)
            if hand is None or hand["status"] != "active":
                raise Conflict("Pause is available only during a decision")
            game = HoldemGame.from_private_snapshot(json.loads(hand["private_snapshot"]))
            try:
                game.pause_clock(time.time() if now is None else now)
            except InvalidAction as exc:
                raise Conflict(str(exc)) from exc
            conn.execute("UPDATE hands SET version = ?, private_snapshot = ? WHERE id = ?",
                         (game.version, _json(game.export_private_snapshot()), hand["id"]))
            conn.execute("UPDATE rooms SET play_state = 'paused' WHERE id = ?", (room_id,))
            return {"paused": True, "remaining": game.paused_remaining}

    def resume_room(self, room_id: str, now: float | None = None) -> dict:
        with self._transaction() as conn:
            room = self._room(conn, room_id)
            if room["play_state"] != "paused":
                raise Conflict("Room is not paused")
            hand = self._latest_hand(conn, room_id)
            if hand is None or hand["status"] != "active":
                raise Conflict("No paused decision")
            game = HoldemGame.from_private_snapshot(json.loads(hand["private_snapshot"]))
            game.resume_clock(time.time() if now is None else now)
            conn.execute("UPDATE hands SET version = ?, private_snapshot = ? WHERE id = ?",
                         (game.version, _json(game.export_private_snapshot()), hand["id"]))
            conn.execute("UPDATE rooms SET play_state = 'running' WHERE id = ?", (room_id,))
            return {"resumed": True, "deadline_at": game.deadline_at}

    def create_room(self, small_blind: int, big_blind: int, max_players: int,
                    max_buyin_stack: int, allowed_nicknames: list[str] | None = None,
                    preflop_seconds: int = 60, flop_seconds: int = 60,
                    turn_seconds: int = 120, river_seconds: int = 180,
                    runout_vote_seconds: int = 60) -> str:
        if any(type(x) is not int for x in
               (small_blind, big_blind, max_players, max_buyin_stack)):
            raise ValueError("Room settings must be integers")
        if (not 0 < small_blind < big_blind or not 2 <= max_players <= 9 or
                max_buyin_stack < big_blind):
            raise ValueError("Invalid blinds, table size, or buy-in stack limit")
        timing = (preflop_seconds, flop_seconds, turn_seconds, river_seconds,
                  runout_vote_seconds)
        if any(type(value) is not int or not 5 <= value <= 600 for value in timing):
            raise ValueError("Decision times must be integers between 5 and 600 seconds")
        names: dict[str, str] = {}
        if allowed_nicknames is not None:
            if not isinstance(allowed_nicknames, list) or not 1 <= len(allowed_nicknames) <= 100:
                raise ValueError("Preset nickname list must have 1 to 100 names")
            for raw_name in allowed_nicknames:
                if not isinstance(raw_name, str) or not 1 <= len(raw_name.strip()) <= 32:
                    raise ValueError("Invalid preset nickname")
                name = raw_name.strip()
                if name.casefold() in names:
                    raise ValueError("Duplicate preset nickname")
                names[name.casefold()] = name
        room_id = _new_id()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO rooms(id, small_blind, big_blind, max_players, "
                "max_buyin_stack, nickname_policy, created_at, preflop_seconds, "
                "flop_seconds, turn_seconds, river_seconds, runout_vote_seconds) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (room_id, small_blind, big_blind, max_players, max_buyin_stack,
                 "preset" if allowed_nicknames is not None else "free", _now(), *timing),
            )
            for key, name in names.items():
                conn.execute(
                    "INSERT INTO allowed_nicknames(room_id, nickname, nickname_key) VALUES (?, ?, ?)",
                    (room_id, name, key),
                )
        return room_id

    def join_player(self, room_id: str, nickname: str, seat: int | None = None) -> PlayerAccess:
        if not isinstance(nickname, str):
            raise ValueError("Nickname must be text")
        name = nickname.strip()
        if not 1 <= len(name) <= 32:
            raise ValueError("Nickname must have 1 to 32 characters")
        if seat is not None and type(seat) is not int:
            raise ValueError("Seat must be an integer")
        token = secrets.token_urlsafe(32)
        player_id = _new_id()
        with self._transaction() as conn:
            room = self._room(conn, room_id)
            if room["status"] != "open":
                raise Conflict("Room is closed")
            if seat is not None and not 0 <= seat < room["max_players"]:
                raise ValueError("Seat is outside the table")
            if room["nickname_policy"] == "preset":
                approved = conn.execute(
                    "SELECT nickname FROM allowed_nicknames WHERE room_id = ? AND nickname_key = ?",
                    (room_id, name.casefold()),
                ).fetchone()
                if approved is None:
                    raise Conflict("Nickname is not allowed in this room")
                name = approved["nickname"]
            try:
                conn.execute(
                    "INSERT INTO room_players(id, room_id, nickname, nickname_key, seat, "
                    "session_token_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (player_id, room_id, name, name.casefold(), seat,
                     hashlib.sha256(token.encode("utf-8")).hexdigest(), _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("Nickname or seat is already occupied") from exc
            if seat is not None and room["play_state"] == "waiting":
                self._clear_confirmations(conn, room_id)
        return PlayerAccess(player_id, room_id, name, seat, token)

    @staticmethod
    def _require_not_in_hand(conn: sqlite3.Connection, room_id: str, player_id: str) -> None:
        hand = PokerStore._latest_hand(conn, room_id)
        if hand and hand["status"] == "active" and conn.execute(
            "SELECT 1 FROM hand_players WHERE hand_id = ? AND player_id = ?",
            (hand["id"], player_id),
        ).fetchone():
            raise Conflict("Finish the current hand before changing seats or leaving")

    def take_seat(self, room_id: str, token: str, seat: int) -> dict:
        if type(seat) is not int:
            raise ValueError("Seat must be an integer")
        with self._transaction() as conn:
            room = self._room(conn, room_id)
            player = self._player(conn, room_id, token)
            if room["status"] != "open":
                raise Conflict("Room is closed")
            if not 0 <= seat < room["max_players"]:
                raise ValueError("Seat is outside the table")
            if player["seat"] == seat:
                return {"seat": seat, "stack": player["stack"]}
            self._require_not_in_hand(conn, room_id, player["id"])
            try:
                conn.execute("UPDATE room_players SET seat = ?, ready = 0, start_confirmed = 0 WHERE id = ?", (seat, player["id"]))
            except sqlite3.IntegrityError as exc:
                raise Conflict("Seat is already occupied") from exc
            if room["play_state"] == "waiting":
                self._clear_confirmations(conn, room_id)
            return {"seat": seat, "stack": player["stack"]}

    def stand_up(self, room_id: str, token: str) -> dict:
        with self._transaction() as conn:
            player = self._player(conn, room_id, token)
            self._require_not_in_hand(conn, room_id, player["id"])
            conn.execute("UPDATE room_players SET seat = NULL, ready = 0, start_confirmed = 0 WHERE id = ?", (player["id"],))
            if self._room(conn, room_id)["play_state"] == "waiting":
                self._clear_confirmations(conn, room_id)
            return {"seat": None, "stack": player["stack"]}

    def leave_room(self, room_id: str, token: str) -> dict:
        with self._transaction() as conn:
            player = self._player(conn, room_id, token)
            self._require_not_in_hand(conn, room_id, player["id"])
            conn.execute(
                "UPDATE room_players SET seat = NULL, stack = 0, total_cashout = total_cashout + ?, "
                "is_active = 0, ready = 0, start_confirmed = 0, left_at = ? WHERE id = ?",
                (player["stack"], _now(), player["id"]),
            )
            if self._room(conn, room_id)["play_state"] == "waiting":
                self._clear_confirmations(conn, room_id)
            return {"nickname": player["nickname"], "cashout": player["stack"]}

    def buy_in(self, room_id: str, token: str, amount: int, request_id: str) -> dict:
        if type(amount) is not int or amount <= 0:
            raise ValueError("Buy-in amount must be a positive integer")
        _request_id(request_id)
        with self._transaction() as conn:
            room = self._room(conn, room_id)
            player = self._player(conn, room_id, token)
            existing = conn.execute(
                "SELECT * FROM buyins WHERE room_id = ? AND request_id = ?",
                (room_id, request_id),
            ).fetchone()
            if existing:
                if existing["player_id"] != player["id"] or existing["amount"] != amount:
                    raise Conflict("request_id was used for a different buy-in")
                return dict(existing)
            if room["status"] != "open":
                raise Conflict("Room is closed")
            if player["seat"] is None:
                raise Conflict("Take a seat before buying chips")
            latest = self._latest_hand(conn, room_id)
            if latest and latest["status"] == "active":
                raise Conflict("Buy-ins are allowed between hands only")
            before = player["stack"]
            after = before + amount
            if after > room["max_buyin_stack"]:
                raise Conflict(f"Buy-in would exceed {room['max_buyin_stack']} chips on the table")
            entry = {
                "id": _new_id(), "room_id": room_id, "player_id": player["id"],
                "request_id": request_id, "amount": amount,
                "stack_before": before, "stack_after": after, "created_at": _now(),
            }
            conn.execute(
                "INSERT INTO buyins(id, room_id, player_id, request_id, amount, "
                "stack_before, stack_after, created_at) VALUES "
                "(:id, :room_id, :player_id, :request_id, :amount, "
                ":stack_before, :stack_after, :created_at)", entry,
            )
            conn.execute(
                "UPDATE room_players SET stack = ?, total_buyin = total_buyin + ? WHERE id = ?",
                (after, amount, player["id"]),
            )
            if room["play_state"] == "waiting":
                self._maybe_start(conn, room)
            return entry

    def start_hand(self, room_id: str, request_id: str,
                   deadline_seconds: int | None = None,
                   ready_only: bool = False) -> dict:
        _request_id(request_id)
        with self._transaction() as conn:
            room = self._room(conn, room_id)
            existing = conn.execute(
                "SELECT * FROM hands WHERE room_id = ? AND start_request_id = ?",
                (room_id, request_id),
            ).fetchone()
            if existing:
                snapshot = json.loads(existing["private_snapshot"])
                return {"hand_id": existing["id"], "hand_number": existing["hand_number"],
                        "version": existing["version"],
                        "to_act_index": snapshot["to_act_index"],
                        "status": snapshot["street"], "replayed": True}
            if room["status"] != "open":
                raise Conflict("Room is closed")
            last = self._latest_hand(conn, room_id)
            if last and last["status"] == "active":
                raise Conflict("Current hand is still active")
            rows = (self._eligible_rows(conn, room_id) if ready_only else conn.execute(
                "SELECT * FROM room_players WHERE room_id = ? AND is_active = 1 "
                "AND seat IS NOT NULL AND stack > 0 ORDER BY seat", (room_id,),
            ).fetchall())
            if len(rows) < 2:
                raise Conflict("At least two players with chips are required")
            game = HoldemGame([Player(row["nickname"], row["stack"], row["id"])
                               for row in rows], room["small_blind"], room["big_blind"])
            number = last["hand_number"] + 1 if last else 1
            if last:
                previous_button_seat = last["button_seat"]
                next_button_index = next(
                    (i for i, row in enumerate(rows) if row["seat"] > previous_button_seat), 0
                )
                game.hand_number = number - 1
                game.button_index = (next_button_index - 1) % len(rows)
            else:
                # Draw only for the first hand; later hands rotate from the
                # last stored physical seat, including after players move.
                game.button_index = secrets.randbelow(len(rows))
            game.start_new_hand()
            if game.street != "complete" and (deadline_seconds is not None or ready_only):
                game.start_clock(deadline_seconds if deadline_seconds is not None else
                                 self._decision_seconds(room, game), time.time())
            button_seat = rows[game.button_index]["seat"]
            small_blind_index, big_blind_index = game._blinds()
            conn.execute(
                "INSERT INTO hands(id, room_id, hand_number, start_request_id, button_seat, status, "
                "version, private_snapshot, started_at, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (game.hand_id, room_id, number, request_id, button_seat,
                 "complete" if game.street == "complete" else "active", game.version,
                 _json(game.export_private_snapshot()), _now(),
                 _now() if game.street == "complete" else None),
            )
            for i, row in enumerate(rows):
                conn.execute(
                    "INSERT INTO hand_players(hand_id, player_id, seat, starting_stack, blind_paid) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (game.hand_id, row["id"], row["seat"], row["stack"],
                     min(row["stack"], room["small_blind"]) if i == small_blind_index else
                     min(row["stack"], room["big_blind"]) if i == big_blind_index else 0),
                )
            if game.street == "complete":
                self._save_settlement(conn, game)
            if ready_only:
                continuing = self._room(conn, room_id)["play_state"] == "running"
                conn.execute("UPDATE rooms SET next_hand_at = ? WHERE id = ?",
                             (time.time() + self.INTER_HAND_SECONDS if
                              game.street == "complete" and continuing
                              else None, room_id))
            return {"hand_id": game.hand_id, "hand_number": number,
                    "version": game.version, "to_act_index": game.to_act_index,
                    "status": game.street, "replayed": False}

    def _save_settlement(self, conn: sqlite3.Connection, game: HoldemGame) -> None:
        for i, player in enumerate(game.players):
            conn.execute(
                "UPDATE room_players SET stack = ? WHERE id = ?",
                (player.stack, player.player_id),
            )
            conn.execute(
                "UPDATE hand_players SET ending_stack = ?, payout = ?, refund = ? "
                "WHERE hand_id = ? AND player_id = ?",
                (player.stack, game.payouts[i], game.refunds[i], game.hand_id, player.player_id),
            )
        room_id = conn.execute("SELECT room_id FROM hands WHERE id = ?",
                               (game.hand_id,)).fetchone()["room_id"]
        pending = conn.execute(
            "SELECT id, pending_admin_action FROM room_players WHERE room_id = ? "
            "AND is_active = 1 AND pending_admin_action IS NOT NULL ORDER BY seat",
            (room_id,),
        ).fetchall()
        for row in pending:
            self._apply_admin_action(conn, room_id, row["id"], row["pending_admin_action"])
        if pending:
            self._clear_confirmations(conn, room_id)
        self._stop_if_insufficient(conn, room_id)

    def apply_action(self, room_id: str, hand_id: str, token: str, action: Action,
                     expected_version: int, request_id: str,
                     deadline_seconds: int | None = None) -> dict:
        _request_id(request_id)
        if type(expected_version) is not int:
            raise ValueError("expected_version must be an integer")
        if not isinstance(action, Action) or not isinstance(action.type, ActionType):
            raise InvalidAction("Unknown action")
        with self._transaction() as conn:
            room = self._room(conn, room_id)
            player = self._player(conn, room_id, token)
            hand = conn.execute(
                "SELECT * FROM hands WHERE id = ? AND room_id = ?", (hand_id, room_id)
            ).fetchone()
            if hand is None:
                raise StoreError("Hand does not exist")
            existing = conn.execute(
                "SELECT * FROM hand_actions WHERE hand_id = ? AND request_id = ?",
                (hand_id, request_id),
            ).fetchone()
            if existing:
                if (existing["player_id"] != player["id"] or
                        existing["action"] != action.type.value or
                        existing["amount"] != action.amount):
                    raise Conflict("request_id was used for a different action")
                return {"hand_id": hand_id, "version": existing["version_after"],
                        "replayed": True}
            if hand["status"] != "active" or self._latest_hand(conn, room_id)["id"] != hand_id:
                raise Conflict("Hand is not active")
            game = HoldemGame.from_private_snapshot(json.loads(hand["private_snapshot"]))
            if room["play_state"] == "paused":
                raise Conflict("Room is paused")
            if game.deadline_at is not None and game.deadline_at <= time.time():
                raise Conflict("Decision time has expired")
            game.submit_action(player["id"], action, expected_version)
            self._persist_action(conn, hand_id, player["id"], request_id,
                                 game, room, deadline_seconds)
            return {"hand_id": hand_id, "version": game.version,
                    "status": game.street, "replayed": False}

    def _persist_action(self, conn: sqlite3.Connection, hand_id: str, player_id: str,
                        request_id: str, game: HoldemGame, room: sqlite3.Row,
                        deadline_seconds: int | None) -> None:
        if game.street != "complete" and (deadline_seconds is not None or
                                          room["play_state"] == "running" or
                                          room["status"] == "closed"):
            game.start_clock(deadline_seconds if deadline_seconds is not None else
                             self._decision_seconds(room, game), time.time())
        record = game.history[-1]
        conn.execute(
            "INSERT INTO hand_actions(id, hand_id, player_id, request_id, sequence, "
            "street, action, amount, paid, to_call, pot_after, version_after, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_new_id(), hand_id, player_id, request_id, len(game.history),
             record.street, record.action, record.amount, record.paid,
             record.to_call, record.pot_after, game.version, _now()),
        )
        completed = game.street == "complete"
        conn.execute(
            "UPDATE hands SET status = ?, version = ?, private_snapshot = ?, "
            "completed_at = ? WHERE id = ?",
            ("complete" if completed else "active", game.version,
             _json(game.export_private_snapshot()), _now() if completed else None, hand_id),
        )
        if completed:
            self._save_settlement(conn, game)
            if self._room(conn, room["id"])["play_state"] == "running":
                conn.execute("UPDATE rooms SET next_hand_at = ? WHERE id = ?",
                             (time.time() + self.INTER_HAND_SECONDS, room["id"]))

    def extend_decision(self, room_id: str, hand_id: str, token: str,
                        expected_version: int, now: float | None = None) -> dict:
        with self._transaction() as conn:
            room = self._room(conn, room_id)
            player = self._player(conn, room_id, token)
            hand = self._latest_hand(conn, room_id)
            if room["play_state"] == "paused" or hand is None or hand["id"] != hand_id or hand["status"] != "active":
                raise Conflict("No running decision")
            game = HoldemGame.from_private_snapshot(json.loads(hand["private_snapshot"]))
            if game.version != expected_version:
                raise InvalidAction("Stale game version")
            game.extend_clock(player["id"], self._decision_seconds(room, game),
                              time.time() if now is None else now)
            conn.execute("UPDATE hands SET version = ?, private_snapshot = ? WHERE id = ?",
                         (game.version, _json(game.export_private_snapshot()), hand_id))
            return {"version": game.version, "deadline_at": game.deadline_at}

    def vote_runout(self, room_id: str, hand_id: str, token: str, choice: str,
                    expected_version: int) -> dict:
        with self._transaction() as conn:
            room = self._room(conn, room_id)
            player = self._player(conn, room_id, token)
            hand = self._latest_hand(conn, room_id)
            if room["play_state"] == "paused" or hand is None or hand["id"] != hand_id or hand["status"] != "active":
                raise Conflict("No active runout vote")
            game = HoldemGame.from_private_snapshot(json.loads(hand["private_snapshot"]))
            if game.deadline_at is not None and game.deadline_at <= time.time():
                raise Conflict("Runout vote has expired")
            game.submit_runout_vote(player["id"], choice, expected_version)
            self._persist_game(conn, hand, game, room)
            return {"version": game.version, "status": game.street}

    def _persist_game(self, conn: sqlite3.Connection, hand: sqlite3.Row,
                      game: HoldemGame, room: sqlite3.Row) -> None:
        completed = game.street == "complete"
        conn.execute("UPDATE hands SET status = ?, version = ?, private_snapshot = ?, "
                     "completed_at = ? WHERE id = ?",
                     ("complete" if completed else "active", game.version,
                      _json(game.export_private_snapshot()), _now() if completed else None,
                      hand["id"]))
        if completed:
            self._save_settlement(conn, game)
            if self._room(conn, room["id"])["play_state"] == "running":
                conn.execute("UPDATE rooms SET next_hand_at = ? WHERE id = ?",
                             (time.time() + self.INTER_HAND_SECONDS, room["id"]))

    def advance_rooms(self, now: float | None = None) -> list[str]:
        """Start scheduled hands, including after a server restart."""
        now = time.time() if now is None else now
        with closing(self._connect()) as conn:
            candidates = [row["id"] for row in conn.execute(
                "SELECT id FROM rooms WHERE play_state = 'running' AND next_hand_at <= ?", (now,)
            )]
        changed = []
        for room_id in candidates:
            with self._transaction() as conn:
                room = self._room(conn, room_id)
                latest = self._latest_hand(conn, room_id)
                if (room["play_state"] != "running" or room["next_hand_at"] is None or
                        room["next_hand_at"] > now or (latest and latest["status"] == "active")):
                    continue
                if len(self._eligible_rows(conn, room_id)) < 2:
                    conn.execute("UPDATE rooms SET play_state = 'waiting', next_hand_at = NULL "
                                 "WHERE id = ?", (room_id,))
                    changed.append(room_id)
                    continue
                # Consume the due marker before starting, so a failed attempt cannot loop forever.
                conn.execute("UPDATE rooms SET next_hand_at = NULL WHERE id = ?", (room_id,))
            try:
                self.start_hand(room_id, f"auto-{_new_id()}", ready_only=True)
                changed.append(room_id)
            except Conflict:
                with self._transaction() as conn:
                    room = self._room(conn, room_id)
                    latest = self._latest_hand(conn, room_id)
                    if room["play_state"] == "running" and (not latest or latest["status"] != "active"):
                        if len(self._eligible_rows(conn, room_id)) < 2:
                            conn.execute("UPDATE rooms SET play_state = 'waiting', next_hand_at = NULL "
                                         "WHERE id = ?", (room_id,))
                        else:
                            conn.execute("UPDATE rooms SET next_hand_at = ? WHERE id = ?",
                                         (time.time() + 1, room_id))
                        changed.append(room_id)
        return changed

    def expire_due_actions(self, now: float | None = None,
                           deadline_seconds: int | None = 30) -> list[str]:
        """Trusted scheduler: check if possible, otherwise fold, then persist."""
        now = time.time() if now is None else now
        with closing(self._connect()) as conn:
            due_candidates = [row["id"] for row in conn.execute(
                "SELECT id FROM hands WHERE status = 'active'"
            )]
        changed_rooms: list[str] = []
        for hand_id in due_candidates:
            with self._transaction() as conn:
                hand = conn.execute("SELECT * FROM hands WHERE id = ?", (hand_id,)).fetchone()
                if hand is None or hand["status"] != "active":
                    continue
                room = self._room(conn, hand["room_id"])
                if room["play_state"] == "paused":
                    continue
                game = HoldemGame.from_private_snapshot(json.loads(hand["private_snapshot"]))
                if game.deadline_at is None or game.deadline_at > now:
                    continue
                if game.street == "runout_vote":
                    game.resolve_runout()
                    self._persist_game(conn, hand, game, room)
                    changed_rooms.append(hand["room_id"])
                    continue
                if game.to_act_index is None:
                    continue
                actor = game.players[game.to_act_index]
                legal = game.legal_actions(actor.player_id)
                action = Action(ActionType.CHECK if legal.can_check else ActionType.FOLD)
                old_version = game.version
                game.submit_action(actor.player_id, action, old_version)
                self._persist_action(conn, hand_id, actor.player_id,
                                     f"auto-timeout-{old_version}", game, room, deadline_seconds)
                changed_rooms.append(hand["room_id"])
        return changed_rooms

    def arm_missing_deadlines(self, deadline_seconds: int | None = None) -> list[str]:
        """Give older active snapshots a deadline when the server starts."""
        with closing(self._connect()) as conn:
            hand_ids = [row["id"] for row in conn.execute(
                "SELECT id FROM hands WHERE status = 'active'"
            )]
        changed_rooms: list[str] = []
        for hand_id in hand_ids:
            with self._transaction() as conn:
                hand = conn.execute("SELECT * FROM hands WHERE id = ?", (hand_id,)).fetchone()
                if hand is None or hand["status"] != "active":
                    continue
                room = self._room(conn, hand["room_id"])
                if room["play_state"] == "paused":
                    continue
                game = HoldemGame.from_private_snapshot(json.loads(hand["private_snapshot"]))
                if game.deadline_at is not None or (game.to_act_index is None and game.street != "runout_vote"):
                    continue
                game.start_clock(deadline_seconds if deadline_seconds is not None else
                                 self._decision_seconds(room, game), time.time())
                conn.execute(
                    "UPDATE hands SET version = ?, private_snapshot = ? WHERE id = ?",
                    (game.version, _json(game.export_private_snapshot()), hand_id),
                )
                changed_rooms.append(hand["room_id"])
        return changed_rooms

    def set_deadline(self, room_id: str, hand_id: str, deadline_at: float | None,
                     expected_version: int) -> int:
        with self._transaction() as conn:
            hand = conn.execute(
                "SELECT * FROM hands WHERE id = ? AND room_id = ? AND status = 'active'",
                (hand_id, room_id),
            ).fetchone()
            if hand is None:
                raise Conflict("Hand is not active")
            game = HoldemGame.from_private_snapshot(json.loads(hand["private_snapshot"]))
            if game.version != expected_version:
                raise InvalidAction("Stale game version")
            game.set_deadline(deadline_at)
            conn.execute(
                "UPDATE hands SET version = ?, private_snapshot = ? WHERE id = ?",
                (game.version, _json(game.export_private_snapshot()), hand_id),
            )
            return game.version

    def load_game(self, room_id: str, hand_id: str | None = None) -> HoldemGame:
        """Trusted server-only access to the complete private hand state."""
        with closing(self._connect()) as conn:
            self._room(conn, room_id)
            hand = (conn.execute(
                "SELECT * FROM hands WHERE id = ? AND room_id = ?", (hand_id, room_id)
            ).fetchone() if hand_id else self._latest_hand(conn, room_id))
            if hand is None:
                raise StoreError("Hand does not exist")
            return HoldemGame.from_private_snapshot(json.loads(hand["private_snapshot"]))

    def room_overview(self, room_id: str) -> dict:
        """Trusted administrator data; the HTTP layer must protect this method."""
        with closing(self._connect()) as conn:
            room = self._room(conn, room_id)
            last = self._latest_hand(conn, room_id)
            active_stacks = {}
            if last and last["status"] == "active":
                game = HoldemGame.from_private_snapshot(json.loads(last["private_snapshot"]))
                active_stacks = {p.player_id: p.stack for p in game.players}
            players = []
            for row in conn.execute(
                "SELECT id, nickname, seat, stack, total_buyin, total_cashout, ready, "
                "start_confirmed, pending_admin_action "
                "FROM room_players WHERE room_id = ? AND is_active = 1 "
                "ORDER BY seat IS NULL, seat, created_at", (room_id,)
            ):
                players.append({
                    "player_id": row["id"], "nickname": row["nickname"],
                    "seat": row["seat"],
                    "ready": bool(row["ready"]),
                    "start_confirmed": bool(row["start_confirmed"]),
                    "pending_admin_action": row["pending_admin_action"],
                    "stack": active_stacks.get(row["id"], row["stack"]),
                    "total_buyin": row["total_buyin"],
                    "total_cashout": row["total_cashout"],
                    "profit_loss": row["stack"] + row["total_cashout"] - row["total_buyin"],
                    "max_topup_between_hands": (None if active_stacks or row["seat"] is None else
                        max(0, room["max_buyin_stack"] - row["stack"])),
                })
            leaderboard = [
                {"nickname": row["nickname"], "profit_loss": row["profit_loss"]}
                for row in conn.execute(
                    "SELECT nickname, SUM(stack + total_cashout - total_buyin) AS profit_loss "
                    "FROM room_players WHERE room_id = ? GROUP BY nickname_key "
                    "ORDER BY profit_loss DESC, nickname COLLATE NOCASE", (room_id,),
                )
            ]
            allowed = [row["nickname"] for row in conn.execute(
                "SELECT nickname FROM allowed_nicknames WHERE room_id = ? ORDER BY nickname",
                (room_id,),
            )]
            admin_events = [dict(row) for row in conn.execute(
                "SELECT e.action, e.created_at, p.nickname FROM room_admin_events e "
                "LEFT JOIN room_players p ON p.id = e.player_id "
                "WHERE e.room_id = ? ORDER BY e.rowid DESC LIMIT 10", (room_id,),
            )]
            return {
                "room_id": room_id, "small_blind": room["small_blind"],
                "big_blind": room["big_blind"], "max_players": room["max_players"],
                "max_buyin_stack": room["max_buyin_stack"],
                "preflop_seconds": room["preflop_seconds"],
                "flop_seconds": room["flop_seconds"],
                "turn_seconds": room["turn_seconds"],
                "river_seconds": room["river_seconds"],
                "runout_vote_seconds": room["runout_vote_seconds"],
                "play_state": room["play_state"],
                "next_hand_at": room["next_hand_at"],
                "nickname_policy": room["nickname_policy"],
                "allowed_nicknames": allowed, "status": room["status"],
                "latest_hand_id": last["id"] if last else None,
                "latest_hand_status": last["status"] if last else None,
                "players": players, "leaderboard": leaderboard,
                "admin_events": admin_events,
            }

    def list_rooms(self) -> list[dict]:
        """Trusted administrator list; authentication belongs in the HTTP layer."""
        with closing(self._connect()) as conn:
            return [
                {"room_id": row["id"], "small_blind": row["small_blind"],
                 "big_blind": row["big_blind"], "max_players": row["max_players"],
                 "max_buyin_stack": row["max_buyin_stack"], "status": row["status"],
                 "play_state": row["play_state"],
                 "created_at": row["created_at"]}
                for row in conn.execute(
                    "SELECT * FROM rooms WHERE status = 'open' ORDER BY created_at DESC"
                )
            ]

    def public_room(self, room_id: str, include_closed: bool = False) -> dict:
        """Room information suitable for anyone holding its invite link."""
        overview = self.room_overview(room_id)
        if overview["status"] == "closed" and not include_closed:
            return {"room_id": room_id, "status": "closed"}
        return {
            key: overview[key] for key in (
                "room_id", "small_blind", "big_blind", "max_players",
                "max_buyin_stack", "nickname_policy", "allowed_nicknames",
                "preflop_seconds", "flop_seconds", "turn_seconds", "river_seconds",
                "runout_vote_seconds", "play_state", "next_hand_at",
                "status", "latest_hand_id", "latest_hand_status"
            )
        } | {
            "players": [
                {"player_id": p["player_id"], "nickname": p["nickname"],
                 "seat": p["seat"], "stack": p["stack"],
                 "ready": p["ready"], "start_confirmed": p["start_confirmed"],
                 "pending_admin_action": p["pending_admin_action"]}
                for p in overview["players"]
            ], "leaderboard": overview["leaderboard"],
        }

    def player_view(self, room_id: str, token: str, hand_id: str | None = None) -> dict:
        with closing(self._connect()) as conn:
            self._room(conn, room_id)
            player = self._player(conn, room_id, token)
            hand = (conn.execute(
                "SELECT * FROM hands WHERE id = ? AND room_id = ?", (hand_id, room_id)
            ).fetchone() if hand_id else self._latest_hand(conn, room_id))
            if hand is None:
                return {"room_id": room_id, "hand_id": None,
                        "player_id": player["id"], "viewer_player_id": player["id"],
                        "status": "watching", "stack": player["stack"]}
            # Historical hands are visible only to their participants.
            participant = conn.execute(
                "SELECT 1 FROM hand_players WHERE hand_id = ? AND player_id = ?",
                (hand["id"], player["id"]),
            ).fetchone()
            if not participant:
                if hand_id is not None:
                    raise AccessDenied("Player did not participate in this hand")
                view = HoldemGame.from_private_snapshot(
                    json.loads(hand["private_snapshot"])
                ).state_for_observer(player["id"])
                view.update({"room_id": room_id, "status": "watching",
                             "stack": player["stack"]})
                return view
            return HoldemGame.from_private_snapshot(
                json.loads(hand["private_snapshot"])
            ).state_for_player(player["id"])

    def buyin_history(self, room_id: str, token: str) -> list[dict]:
        with closing(self._connect()) as conn:
            player = self._player(conn, room_id, token)
            return [dict(row) for row in conn.execute(
                "SELECT id, amount, stack_before, stack_after, created_at FROM buyins "
                "WHERE room_id = ? AND player_id = ? ORDER BY created_at, rowid",
                (room_id, player["id"]),
            )]

    def hand_history(self, room_id: str, token: str) -> list[dict]:
        with closing(self._connect()) as conn:
            player = self._player(conn, room_id, token)
            rows = conn.execute(
                "SELECT h.*, hp.starting_stack, hp.blind_paid, hp.ending_stack, hp.payout, hp.refund "
                "FROM hands h JOIN hand_players hp ON hp.hand_id = h.id "
                "WHERE h.room_id = ? AND hp.player_id = ? ORDER BY h.hand_number",
                (room_id, player["id"]),
            ).fetchall()
            result = []
            for row in rows:
                view = HoldemGame.from_private_snapshot(
                    json.loads(row["private_snapshot"])
                ).state_for_player(player["id"])
                result.append({
                    "hand_id": row["id"], "hand_number": row["hand_number"],
                    "status": row["status"], "starting_stack": row["starting_stack"],
                    "blind_paid": row["blind_paid"],
                    "ending_stack": row["ending_stack"], "payout": row["payout"],
                    "refund": row["refund"], "view": view,
                })
            return result
