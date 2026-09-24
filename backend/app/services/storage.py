"""SQLite persistence for private rooms and stepwise poker hands.

Every write opens a BEGIN IMMEDIATE transaction. A future HTTP layer must
authenticate administrators before calling room creation or hand start.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
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
    seat: int
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
            elif version != 1:
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
            "SELECT * FROM room_players WHERE room_id = ? AND session_token_hash = ?",
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

    def create_room(self, small_blind: int, big_blind: int, max_players: int,
                    max_buyin_stack: int, allowed_nicknames: list[str] | None = None) -> str:
        if any(type(x) is not int for x in
               (small_blind, big_blind, max_players, max_buyin_stack)):
            raise ValueError("Room settings must be integers")
        if (not 0 < small_blind < big_blind or not 2 <= max_players <= 9 or
                max_buyin_stack < big_blind):
            raise ValueError("Invalid blinds, table size, or buy-in stack limit")
        names: dict[str, str] = {}
        if allowed_nicknames is not None:
            if not isinstance(allowed_nicknames, list) or not 1 <= len(allowed_nicknames) <= max_players:
                raise ValueError("Preset nicknames must fit the table")
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
                "max_buyin_stack, nickname_policy, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (room_id, small_blind, big_blind, max_players, max_buyin_stack,
                 "preset" if allowed_nicknames is not None else "free", _now()),
            )
            for key, name in names.items():
                conn.execute(
                    "INSERT INTO allowed_nicknames(room_id, nickname, nickname_key) VALUES (?, ?, ?)",
                    (room_id, name, key),
                )
        return room_id

    def join_player(self, room_id: str, nickname: str, seat: int) -> PlayerAccess:
        if not isinstance(nickname, str):
            raise ValueError("Nickname must be text")
        name = nickname.strip()
        if not 1 <= len(name) <= 32:
            raise ValueError("Nickname must have 1 to 32 characters")
        if type(seat) is not int:
            raise ValueError("Seat must be an integer")
        token = secrets.token_urlsafe(32)
        player_id = _new_id()
        with self._transaction() as conn:
            room = self._room(conn, room_id)
            if room["status"] != "open":
                raise Conflict("Room is closed")
            if not 0 <= seat < room["max_players"]:
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
        return PlayerAccess(player_id, room_id, name, seat, token)

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
            return entry

    def start_hand(self, room_id: str, request_id: str) -> dict:
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
            rows = conn.execute(
                "SELECT * FROM room_players WHERE room_id = ? AND stack > 0 ORDER BY seat",
                (room_id,),
            ).fetchall()
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
            game.start_new_hand()
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
            return {"hand_id": game.hand_id, "hand_number": number,
                    "version": game.version, "to_act_index": game.to_act_index,
                    "status": game.street, "replayed": False}

    @staticmethod
    def _save_settlement(conn: sqlite3.Connection, game: HoldemGame) -> None:
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

    def apply_action(self, room_id: str, hand_id: str, token: str, action: Action,
                     expected_version: int, request_id: str) -> dict:
        _request_id(request_id)
        if type(expected_version) is not int:
            raise ValueError("expected_version must be an integer")
        if not isinstance(action, Action) or not isinstance(action.type, ActionType):
            raise InvalidAction("Unknown action")
        with self._transaction() as conn:
            self._room(conn, room_id)
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
            game.submit_action(player["id"], action, expected_version)
            record = game.history[-1]
            conn.execute(
                "INSERT INTO hand_actions(id, hand_id, player_id, request_id, sequence, "
                "street, action, amount, paid, to_call, pot_after, version_after, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_new_id(), hand_id, player["id"], request_id, len(game.history),
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
            return {"hand_id": hand_id, "version": game.version,
                    "status": game.street, "replayed": False}

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
                "SELECT id, nickname, seat, stack, total_buyin FROM room_players "
                "WHERE room_id = ? ORDER BY seat", (room_id,)
            ):
                players.append({
                    "player_id": row["id"], "nickname": row["nickname"],
                    "seat": row["seat"],
                    "stack": active_stacks.get(row["id"], row["stack"]),
                    "total_buyin": row["total_buyin"],
                    "max_topup_between_hands": (None if active_stacks else
                        max(0, room["max_buyin_stack"] - row["stack"])),
                })
            allowed = [row["nickname"] for row in conn.execute(
                "SELECT nickname FROM allowed_nicknames WHERE room_id = ? ORDER BY nickname",
                (room_id,),
            )]
            return {
                "room_id": room_id, "small_blind": room["small_blind"],
                "big_blind": room["big_blind"], "max_players": room["max_players"],
                "max_buyin_stack": room["max_buyin_stack"],
                "nickname_policy": room["nickname_policy"],
                "allowed_nicknames": allowed, "status": room["status"],
                "latest_hand_id": last["id"] if last else None,
                "latest_hand_status": last["status"] if last else None,
                "players": players,
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
                        "player_id": player["id"], "stack": player["stack"]}
            # Historical hands are visible only to their participants.
            participant = conn.execute(
                "SELECT 1 FROM hand_players WHERE hand_id = ? AND player_id = ?",
                (hand["id"], player["id"]),
            ).fetchone()
            if not participant:
                if hand_id is not None:
                    raise AccessDenied("Player did not participate in this hand")
                return {"room_id": room_id, "hand_id": hand["id"],
                        "status": "waiting_next_hand", "player_id": player["id"],
                        "stack": player["stack"]}
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
