"""Private-room HTTP API, same-origin player UI and WebSocket updates."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import time
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, StrictInt, StrictStr

from ..engine.actions import Action, ActionType
from ..engine.game import InvalidAction
from ..services.storage import AccessDenied, Conflict, PokerStore, StoreError

ROOT = Path(__file__).resolve().parents[3]
FRONTEND = ROOT / "frontend"
logger = logging.getLogger(__name__)


class LoginBody(BaseModel):
    password: StrictStr


class RoomBody(BaseModel):
    small_blind: StrictInt
    big_blind: StrictInt
    max_players: StrictInt
    max_buyin_stack: StrictInt
    allowed_nicknames: list[str] | None = None
    preflop_seconds: StrictInt = 60
    flop_seconds: StrictInt = 60
    turn_seconds: StrictInt = 120
    river_seconds: StrictInt = 180
    runout_vote_seconds: StrictInt = 60


class JoinBody(BaseModel):
    nickname: StrictStr
    seat: StrictInt | None = None


class SeatBody(BaseModel):
    seat: StrictInt


class BuyinBody(BaseModel):
    amount: StrictInt
    request_id: StrictStr


class StartBody(BaseModel):
    request_id: StrictStr


class ReadyBody(BaseModel):
    ready: bool


class VersionBody(BaseModel):
    expected_version: StrictInt


class VoteBody(VersionBody):
    choice: StrictStr


class RecoveryBody(BaseModel):
    code: StrictStr


class ActionBody(BaseModel):
    action: ActionType
    amount: StrictInt = 0
    expected_version: StrictInt
    request_id: StrictStr


class RoomHub:
    def __init__(self, store: PokerStore):
        self.store = store
        self.clients: dict[str, set[tuple[WebSocket, str]]] = {}

    async def send_state(self, room_id: str, socket: WebSocket, token: str) -> None:
        room, game = await asyncio.gather(
            asyncio.to_thread(self.store.public_room, room_id, True),
            asyncio.to_thread(self.store.player_view, room_id, token),
        )
        await socket.send_json({"type": "state", "room": room, "game": game})

    async def broadcast(self, room_id: str) -> None:
        for socket, token in list(self.clients.get(room_id, ())):
            try:
                await self.send_state(room_id, socket, token)
            except Exception:
                self.clients.get(room_id, set()).discard((socket, token))
                try:
                    await socket.close()
                except Exception:
                    pass


def create_app(db_path: str | Path | None = None, admin_password: str | None = None) -> FastAPI:
    password = admin_password if admin_password is not None else os.environ.get("TEXAS_ADMIN_PASSWORD")
    if not password or len(password) < 12:
        raise RuntimeError("Set TEXAS_ADMIN_PASSWORD to at least 12 characters")
    store = PokerStore(db_path or os.environ.get("TEXAS_DB_PATH", ROOT / "database/holdem.sqlite3"))
    store.initialize()
    hub = RoomHub(store)
    login_failures: dict[str, list[float]] = {}
    recovery_failures: dict[str, list[float]] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await asyncio.to_thread(store.arm_missing_deadlines)

        async def timeout_loop():
            while True:
                try:
                    expired = await asyncio.to_thread(store.expire_due_actions, time.time(), None)
                    started = await asyncio.to_thread(store.advance_rooms, time.time())
                    changed = expired + started
                    for changed_room in set(changed):
                        await hub.broadcast(changed_room)
                except Exception:
                    logger.exception("Failed to process poker action deadlines")
                await asyncio.sleep(1)

        task = asyncio.create_task(timeout_loop())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="Texas Hold'em Private Table", docs_url=None, redoc_url=None,
                  lifespan=lifespan)
    app.state.store = store
    app.state.hub = hub
    admin_sessions: dict[str, float] = {}

    @app.middleware("http")
    async def browser_guards(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                if (parsed.scheme != request.url.scheme or
                        parsed.netloc.lower() != request.headers.get("host", "").lower()):
                    return JSONResponse({"detail": "Cross-origin request denied"}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AccessDenied)
    async def denied(_request: Request, exc: AccessDenied):
        return JSONResponse({"detail": str(exc)}, status_code=401)

    @app.exception_handler(Conflict)
    async def conflict(_request: Request, exc: Conflict):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(StoreError)
    async def missing(_request: Request, exc: StoreError):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(InvalidAction)
    async def invalid_action(_request: Request, exc: InvalidAction):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(ValueError)
    async def invalid_value(_request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def require_admin(request: Request) -> None:
        token = request.cookies.get("th_admin", "")
        digest = hashlib.sha256(token.encode()).hexdigest() if token else ""
        expires = admin_sessions.get(digest, 0)
        if expires <= time.monotonic():
            raise HTTPException(status_code=401, detail="Admin login required")

    def player_token(request: Request, room_id: str) -> str:
        token = request.cookies.get(f"th_room_{room_id}")
        if not token:
            raise AccessDenied("Join this room first")
        return token

    @app.post("/api/admin/login")
    async def admin_login(body: LoginBody, request: Request, response: Response):
        address = request.client.host if request.client else "unknown"
        now = time.monotonic()
        recent = [attempt for attempt in login_failures.get(address, []) if now - attempt < 300]
        login_failures[address] = recent
        if len(recent) >= 5:
            raise HTTPException(status_code=429, detail="Too many login attempts; try again later")
        if not secrets.compare_digest(body.password, password):
            recent.append(now)
            raise HTTPException(status_code=401, detail="Wrong admin password")
        login_failures.pop(address, None)
        token = secrets.token_urlsafe(32)
        admin_sessions[hashlib.sha256(token.encode()).hexdigest()] = time.monotonic() + 12 * 3600
        response.set_cookie("th_admin", token, httponly=True, secure=request.url.scheme == "https",
                            samesite="lax", max_age=12 * 3600, path="/")
        return {"ok": True}

    @app.post("/api/admin/logout")
    async def admin_logout(request: Request, response: Response):
        token = request.cookies.get("th_admin", "")
        admin_sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)
        response.delete_cookie("th_admin", path="/")
        return {"ok": True}

    @app.get("/api/admin/session", dependencies=[Depends(require_admin)])
    async def admin_session():
        return {"ok": True}

    @app.get("/api/admin/rooms", dependencies=[Depends(require_admin)])
    async def admin_rooms():
        return await asyncio.to_thread(store.list_rooms)

    @app.post("/api/admin/rooms", dependencies=[Depends(require_admin)])
    async def create_room(body: RoomBody, request: Request):
        room_id = await asyncio.to_thread(
            store.create_room, body.small_blind, body.big_blind, body.max_players,
            body.max_buyin_stack, body.allowed_nicknames,
            body.preflop_seconds, body.flop_seconds, body.turn_seconds,
            body.river_seconds, body.runout_vote_seconds,
        )
        return {"room_id": room_id, "invite_url": str(request.base_url).rstrip("/") + f"/r/{room_id}"}

    @app.get("/api/admin/rooms/{room_id}", dependencies=[Depends(require_admin)])
    async def admin_room(room_id: str):
        overview = await asyncio.to_thread(store.room_overview, room_id)
        tokens = [token for _, token in hub.clients.get(room_id, ())]
        connected = await asyncio.to_thread(store.connected_player_ids, room_id, tokens)
        for player in overview["players"]:
            player["connected"] = player["player_id"] in connected
        return overview

    @app.post("/api/admin/rooms/{room_id}/players/{player_id}/recovery-code",
              dependencies=[Depends(require_admin)])
    async def issue_recovery_code(room_id: str, player_id: str):
        return await asyncio.to_thread(store.issue_recovery_code, room_id, player_id)

    @app.post("/api/admin/rooms/{room_id}/players/{player_id}/stand",
              dependencies=[Depends(require_admin)])
    async def force_stand(room_id: str, player_id: str):
        result = await asyncio.to_thread(store.request_player_action, room_id, player_id, "stand")
        await hub.broadcast(room_id)
        return result

    @app.post("/api/admin/rooms/{room_id}/players/{player_id}/remove",
              dependencies=[Depends(require_admin)])
    async def remove_player(room_id: str, player_id: str):
        result = await asyncio.to_thread(store.request_player_action, room_id, player_id, "remove")
        await hub.broadcast(room_id)
        return result

    @app.delete("/api/admin/rooms/{room_id}", dependencies=[Depends(require_admin)])
    async def delete_room(room_id: str):
        result = await asyncio.to_thread(store.archive_room, room_id)
        await hub.broadcast(room_id)
        return result

    @app.post("/api/admin/rooms/{room_id}/hands", dependencies=[Depends(require_admin)])
    async def start_hand(room_id: str, body: StartBody):
        result = await asyncio.to_thread(store.request_start, room_id)
        await hub.broadcast(room_id)
        return result

    @app.post("/api/admin/rooms/{room_id}/pause", dependencies=[Depends(require_admin)])
    async def pause_room(room_id: str):
        result = await asyncio.to_thread(store.pause_room, room_id)
        await hub.broadcast(room_id)
        return result

    @app.post("/api/admin/rooms/{room_id}/resume", dependencies=[Depends(require_admin)])
    async def resume_room(room_id: str):
        result = await asyncio.to_thread(store.resume_room, room_id)
        await hub.broadcast(room_id)
        return result

    @app.get("/api/rooms/{room_id}")
    async def public_room(room_id: str):
        return await asyncio.to_thread(store.public_room, room_id)

    @app.post("/api/rooms/{room_id}/join")
    async def join(room_id: str, body: JoinBody, request: Request, response: Response):
        existing_token = request.cookies.get(f"th_room_{room_id}")
        if existing_token:
            try:
                await asyncio.to_thread(store.player_view, room_id, existing_token)
            except AccessDenied:
                pass
            else:
                raise Conflict("This browser has already joined this room")
        access = await asyncio.to_thread(store.join_player, room_id, body.nickname, body.seat)
        response.set_cookie(f"th_room_{room_id}", access.session_token,
                            httponly=True, secure=request.url.scheme == "https",
                            samesite="lax", path="/")
        await hub.broadcast(room_id)
        return {"player_id": access.player_id, "nickname": access.nickname, "seat": access.seat}

    @app.post("/api/rooms/{room_id}/recover")
    async def recover(room_id: str, body: RecoveryBody, request: Request, response: Response):
        address = request.client.host if request.client else "unknown"
        now = time.monotonic()
        recent = [attempt for attempt in recovery_failures.get(address, []) if now - attempt < 300]
        recovery_failures[address] = recent
        if len(recent) >= 10:
            raise HTTPException(status_code=429, detail="Too many recovery attempts; try again later")
        try:
            access = await asyncio.to_thread(store.recover_player, room_id, body.code)
        except AccessDenied:
            recent.append(now)
            raise
        recovery_failures.pop(address, None)
        response.set_cookie(f"th_room_{room_id}", access.session_token,
                            httponly=True, secure=request.url.scheme == "https",
                            samesite="lax", path="/")
        await hub.broadcast(room_id)
        return {"player_id": access.player_id, "nickname": access.nickname, "seat": access.seat}

    @app.post("/api/rooms/{room_id}/seat")
    async def take_seat(room_id: str, body: SeatBody, request: Request):
        result = await asyncio.to_thread(store.take_seat, room_id,
                                         player_token(request, room_id), body.seat)
        await hub.broadcast(room_id)
        return result

    @app.post("/api/rooms/{room_id}/stand")
    async def stand_up(room_id: str, request: Request):
        result = await asyncio.to_thread(store.stand_up, room_id,
                                         player_token(request, room_id))
        await hub.broadcast(room_id)
        return result

    @app.post("/api/rooms/{room_id}/leave")
    async def leave_room(room_id: str, request: Request, response: Response):
        result = await asyncio.to_thread(store.leave_room, room_id,
                                         player_token(request, room_id))
        response.delete_cookie(f"th_room_{room_id}", path="/")
        await hub.broadcast(room_id)
        return result

    @app.get("/api/rooms/{room_id}/state")
    async def state(room_id: str, request: Request):
        token = player_token(request, room_id)
        room, game = await asyncio.gather(
            asyncio.to_thread(store.public_room, room_id, True),
            asyncio.to_thread(store.player_view, room_id, token),
        )
        return {"room": room, "game": game}

    @app.post("/api/rooms/{room_id}/buyins")
    async def buyin(room_id: str, body: BuyinBody, request: Request):
        token = player_token(request, room_id)
        result = await asyncio.to_thread(store.buy_in, room_id, token, body.amount, body.request_id)
        await hub.broadcast(room_id)
        return result

    @app.post("/api/rooms/{room_id}/ready")
    async def set_ready(room_id: str, body: ReadyBody, request: Request):
        result = await asyncio.to_thread(store.set_ready, room_id,
                                         player_token(request, room_id), body.ready)
        await hub.broadcast(room_id)
        return result

    @app.post("/api/rooms/{room_id}/confirm-start")
    async def confirm_start(room_id: str, request: Request):
        result = await asyncio.to_thread(store.confirm_start, room_id,
                                         player_token(request, room_id))
        await hub.broadcast(room_id)
        return result

    @app.post("/api/rooms/{room_id}/hands/{hand_id}/actions")
    async def act(room_id: str, hand_id: str, body: ActionBody, request: Request):
        token = player_token(request, room_id)
        result = await asyncio.to_thread(
            store.apply_action, room_id, hand_id, token,
            Action(body.action, body.amount), body.expected_version, body.request_id,
        )
        await hub.broadcast(room_id)
        return result

    @app.post("/api/rooms/{room_id}/hands/{hand_id}/extend")
    async def extend(room_id: str, hand_id: str, body: VersionBody, request: Request):
        result = await asyncio.to_thread(store.extend_decision, room_id, hand_id,
                                         player_token(request, room_id), body.expected_version)
        await hub.broadcast(room_id)
        return result

    @app.post("/api/rooms/{room_id}/hands/{hand_id}/runout-vote")
    async def runout_vote(room_id: str, hand_id: str, body: VoteBody, request: Request):
        result = await asyncio.to_thread(store.vote_runout, room_id, hand_id,
                                         player_token(request, room_id), body.choice,
                                         body.expected_version)
        await hub.broadcast(room_id)
        return result

    @app.get("/api/audio-manifest")
    async def audio_manifest():
        directory = FRONTEND / "src" / "audio"
        pattern = re.compile(r"^(check|bet|raise|call|fold|allin|turn)(?:[1-9]\d*|\([1-9]\d*\))?\.(mp3|wav|ogg|m4a)$", re.I)
        return {category: [f"/assets/audio/{path.name}" for path in sorted(directory.iterdir())
                           if path.is_file() and pattern.fullmatch(path.name) and
                           path.name.lower().startswith(category)]
                for category in ("check", "bet", "raise", "call", "fold", "allin", "turn")}

    @app.get("/api/rooms/{room_id}/history")
    async def history(room_id: str, request: Request):
        token = player_token(request, room_id)
        buyins, hands = await asyncio.gather(
            asyncio.to_thread(store.buyin_history, room_id, token),
            asyncio.to_thread(store.hand_history, room_id, token),
        )
        return {"buyins": buyins, "hands": hands}

    @app.websocket("/ws/rooms/{room_id}")
    async def room_socket(socket: WebSocket, room_id: str):
        origin = socket.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            expected_scheme = "https" if socket.url.scheme == "wss" else "http"
            if (parsed.scheme != expected_scheme or
                    parsed.netloc.lower() != socket.headers.get("host", "").lower()):
                await socket.close(code=1008)
                return
        token = socket.cookies.get(f"th_room_{room_id}")
        if not token:
            await socket.close(code=1008)
            return
        try:
            await asyncio.to_thread(store.player_view, room_id, token)
        except StoreError:
            await socket.close(code=1008)
            return
        await socket.accept()
        connection = (socket, token)
        hub.clients.setdefault(room_id, set()).add(connection)
        try:
            await hub.send_state(room_id, socket, token)
            while True:
                await socket.receive_text()  # actions use the authenticated HTTP endpoint
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.get(room_id, set()).discard(connection)

    app.mount("/assets", StaticFiles(directory=FRONTEND / "src"), name="assets")

    @app.get("/")
    @app.get("/admin")
    @app.get("/r/{room_id}")
    async def page():
        return FileResponse(FRONTEND / "index.html", headers={"Cache-Control": "no-store"})

    return app
