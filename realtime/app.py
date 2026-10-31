import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import MINIAPP_PORT, TELEGRAM_TOKEN
from db import repo as db_repo
from games.chess.models import TIME_CONTROLS
from games.chess.service import ChessService
from games.core.matchmaking import MatchmakingQueue
from games.hokm.service import HokmService
from realtime.auth import create_ticket, validate_telegram_init_data
from realtime.manager import ConnectionManager
from realtime.websocket import handle_websocket
from realtime.websocket_hokm import handle_hokm_websocket
from repositories.games import game_repo

log = logging.getLogger("L-bot")

# Global singletons
manager = ConnectionManager()
chess_service = ChessService(game_repo, manager)
matchmaking = MatchmakingQueue()
hokm_service = HokmService(game_repo, manager)

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC_DIR = os.path.join(_BASE_DIR, "miniapp", "static")
_CHESS_STATIC_DIR = os.path.join(_STATIC_DIR, "chess")
_HOKM_STATIC_DIR = os.path.join(_STATIC_DIR, "hokm")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json",
}

_GAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
MAX_SCORE = 10_000_000


def _scope_from(start_param: Optional[str]) -> str:
    s = str(start_param or "")
    if s.startswith("g") and len(s) > 1 and s[1:].lstrip("-").isdigit():
        return s
    return "global"


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app_instance: FastAPI):
        log.info("FastAPI service starting up...")
        recovered = await chess_service.recover_from_db()
        recovered_hokm = await hokm_service.recover_from_db()
        log.info("Restored %d active chess room(s) from SQLite", recovered)
        log.info("Restored %d active Hokm room(s) from SQLite", recovered_hokm)
        timeout_task = asyncio.create_task(_timeout_checker_loop())
        hokm_timeout_task = asyncio.create_task(_hokm_timeout_checker_loop())
        cleaner_task = asyncio.create_task(_matchmaking_cleaner_loop())
        try:
            yield
        finally:
            timeout_task.cancel()
            hokm_timeout_task.cancel()
            cleaner_task.cancel()

    app = FastAPI(
        title="L-Bot Unified Mini App & Chess API",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    async def _timeout_checker_loop():
        """Checks clock flag falls every 1 second."""
        while True:
            try:
                timed_out = await chess_service.check_timeouts()
                for item in timed_out:
                    room_id = item["room_id"]
                    await manager.send_to_room(room_id, item["state_message"])
                    await manager.send_to_room(room_id, item["finished_message"])
            except Exception:
                log.error("Timeout checker exception", exc_info=True)
            await asyncio.sleep(1.0)

    async def _matchmaking_cleaner_loop():
        """Cleans stale queue entries every 30 seconds."""
        while True:
            try:
                await matchmaking.cleanup_stale(120.0)
            except Exception:
                pass
            await asyncio.sleep(30.0)

    async def _hokm_timeout_checker_loop():
        """Checks Hokm action deadlines every 1 second."""
        while True:
            try:
                await hokm_service.check_timeouts()
            except asyncio.CancelledError:
                break
            except Exception:
                log.error("Hokm timeout checker exception", exc_info=True)
            await asyncio.sleep(1.0)

    # -------------------------------------------------------------
    # Diagnostics & Health
    # -------------------------------------------------------------
    @app.get("/health")
    async def health_check():
        stats = manager.get_stats()
        stats["active_games"] = len(chess_service._rooms)
        stats["active_hokm_rooms"] = len(hokm_service.rooms)
        return {"ok": True, **stats}

    # -------------------------------------------------------------
    # Chess SPA Frontend Serving
    # -------------------------------------------------------------
    @app.get("/chess")
    @app.get("/chess/")
    @app.get("/chess/{path:path}")
    async def serve_chess_spa(path: str = ""):
        """Serve Chess React single-page app or assets."""
        index_file = os.path.join(_CHESS_STATIC_DIR, "index.html")

        # Check if requesting specific sub-asset (e.g. /chess/assets/main.js)
        if path:
            candidate = os.path.normpath(os.path.join(_CHESS_STATIC_DIR, path))
            if candidate.startswith(_CHESS_STATIC_DIR) and os.path.isfile(candidate):
                ext = os.path.splitext(candidate)[1]
                return FileResponse(candidate, media_type=_CONTENT_TYPES.get(ext, "application/octet-stream"))

        # Fallback to SPA index.html for client-side routing
        if os.path.isfile(index_file):
            return FileResponse(index_file, media_type="text/html; charset=utf-8")

        return JSONResponse(
            {"ok": False, "error": "chess_spa_not_built", "message": "Chess SPA is being assembled."},
            status_code=404,
        )

    # -------------------------------------------------------------
    # Hokm SPA Frontend Serving
    # -------------------------------------------------------------
    @app.get("/hokm")
    @app.get("/hokm/")
    @app.get("/hokm/{path:path}")
    async def serve_hokm_spa(path: str = ""):
        """Serve Hokm React single-page app or assets."""
        index_file = os.path.join(_HOKM_STATIC_DIR, "index.html")

        if path:
            candidate = os.path.normpath(os.path.join(_HOKM_STATIC_DIR, path))
            if candidate.startswith(_HOKM_STATIC_DIR) and os.path.isfile(candidate):
                ext = os.path.splitext(candidate)[1]
                return FileResponse(candidate, media_type=_CONTENT_TYPES.get(ext, "application/octet-stream"))

        if os.path.isfile(index_file):
            return FileResponse(index_file, media_type="text/html; charset=utf-8")

        return JSONResponse(
            {"ok": False, "error": "hokm_spa_not_built", "message": "Hokm SPA is being assembled."},
            status_code=404,
        )

    # -------------------------------------------------------------
    # Chess REST API Endpoints
    # -------------------------------------------------------------
    @app.post("/api/chess/ticket")
    async def get_ws_ticket(request: Request):
        """Exchange verified Telegram initData for a single-use WebSocket ticket."""
        data = await request.json()
        user = validate_telegram_init_data(TELEGRAM_TOKEN, data.get("initData"), dev_user=data.get("dev_user"))
        if not user:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        ticket = create_ticket(user["user_id"], user["name"])
        return {"ok": True, "ticket": ticket, "user_id": user["user_id"], "name": user["name"]}

    @app.post("/api/chess/create")
    async def create_chess_room(request: Request):
        data = await request.json()
        user = validate_telegram_init_data(TELEGRAM_TOKEN, data.get("initData"), dev_user=data.get("dev_user"))
        if not user:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

        tc_key = str(data.get("time_control") or "blitz_5_0")
        if tc_key not in TIME_CONTROLS:
            tc_key = "blitz_5_0"

        # Discard abandoned invite rooms before looking for an existing match.
        await chess_service.abandon_waiting_rooms(user["user_id"])
        active = chess_service.get_rooms_for_player(user["user_id"])
        if active:
            return JSONResponse(
                {"ok": False, "error": "already_in_game", "room_id": active[0]},
                status_code=409,
            )

        res = await chess_service.create_room(user["user_id"], user["name"], tc_key)
        if "error" in res:
            return JSONResponse({"ok": False, **res}, status_code=409)
        return {"ok": True, **res}

    @app.post("/api/chess/cancel")
    async def cancel_chess_room(request: Request):
        data = await request.json()
        user = validate_telegram_init_data(TELEGRAM_TOKEN, data.get("initData"), dev_user=data.get("dev_user"))
        if not user:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

        room_id = str(data.get("room_id") or "").strip()
        if not room_id:
            return JSONResponse({"ok": False, "error": "missing_identifier"}, status_code=400)

        result = await chess_service.cancel_waiting_room(room_id, user["user_id"])
        if "error" in result:
            return JSONResponse({"ok": False, **result}, status_code=400)
        return result

    @app.post("/api/chess/bot")
    async def create_chess_bot_game(request: Request):
        data = await request.json()
        user = validate_telegram_init_data(TELEGRAM_TOKEN, data.get("initData"), dev_user=data.get("dev_user"))
        if not user:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

        difficulty = str(data.get("difficulty") or "medium").lower()
        if difficulty not in {"easy", "medium", "hard"}:
            return JSONResponse({"ok": False, "error": "invalid_difficulty"}, status_code=400)

        tc_key = str(data.get("time_control") or "blitz_5_0")
        if tc_key not in TIME_CONTROLS:
            tc_key = "blitz_5_0"

        await chess_service.abandon_waiting_rooms(user["user_id"])
        active = chess_service.get_rooms_for_player(user["user_id"])
        if active:
            return JSONResponse({
                "ok": False,
                "error": "already_in_game",
                "room_id": active[0],
                "message": "یک بازی فعال دارید؛ ابتدا آن را ادامه دهید یا تمام کنید.",
            }, status_code=409)

        result = await chess_service.create_bot_game(
            creator_id=user["user_id"],
            creator_name=user["name"],
            difficulty=difficulty,
            time_control_key=tc_key,
        )
        if "error" in result:
            status = 409 if result["error"] == "already_in_game" else 400
            return JSONResponse({"ok": False, **result}, status_code=status)

        if result.get("start_message"):
            await manager.send_to_room(result["room_id"], result["start_message"])

        return {
            "ok": True,
            "room_id": result["room_id"],
            "is_bot_game": True,
            "bot_difficulty": difficulty,
        }

    @app.post("/api/chess/join")
    async def join_chess_room(request: Request):
        data = await request.json()
        user = validate_telegram_init_data(TELEGRAM_TOKEN, data.get("initData"), dev_user=data.get("dev_user"))
        if not user:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

        room_id = data.get("room_id")
        invite_code = data.get("invite_code")
        if not room_id and not invite_code:
            return JSONResponse(
                {"ok": False, "error": "missing_identifier", "message": "کد یا شناسه اتاق الزامی است."},
                status_code=400,
            )

        if invite_code:
            res = await chess_service.join_by_invite(invite_code, user["user_id"], user["name"])
        else:
            res = await chess_service.join_room(room_id, user["user_id"], user["name"])

        if "error" in res:
            return JSONResponse({"ok": False, **res}, status_code=400)

        # Broadcast game start event if game began
        if res.get("start_message"):
            await manager.send_to_room(res["room_id"], res["start_message"])

        return {"ok": True, "room_id": res["room_id"]}

    @app.post("/api/chess/matchmake")
    async def matchmake_player(request: Request):
        data = await request.json()
        user = validate_telegram_init_data(TELEGRAM_TOKEN, data.get("initData"), dev_user=data.get("dev_user"))
        if not user:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

        tc_key = str(data.get("time_control") or "blitz_5_0")
        if tc_key not in TIME_CONTROLS:
            tc_key = "blitz_5_0"

        await chess_service.abandon_waiting_rooms(user["user_id"])
        active = chess_service.get_rooms_for_player(user["user_id"])
        if active:
            return JSONResponse(
                {"ok": False, "error": "already_in_game", "room_id": active[0]},
                status_code=409,
            )

        # Look for existing opponent
        matched = await matchmaking.find_match(tc_key, user["user_id"])
        if matched:
            # Create room with matched opponent
            create_res = await chess_service.create_room(matched["user_id"], matched["name"], tc_key)
            room_id = create_res["room_id"]
            join_res = await chess_service.join_room(room_id, user["user_id"], user["name"])

            if join_res.get("start_message"):
                await manager.send_to_room(room_id, join_res["start_message"])

            return {"ok": True, "status": "matched", "room_id": room_id}
        else:
            # Enter queue
            await matchmaking.add_to_queue(user["user_id"], user["name"], tc_key)
            return {"ok": True, "status": "queued"}

    @app.post("/api/chess/matchmake/cancel")
    async def cancel_matchmaking(request: Request):
        data = await request.json()
        user = validate_telegram_init_data(TELEGRAM_TOKEN, data.get("initData"), dev_user=data.get("dev_user"))
        if not user:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        await matchmaking.remove_from_queue(user["user_id"])
        return {"ok": True}

    @app.post("/api/chess/profile")
    async def get_player_profile(request: Request):
        data = await request.json()
        user = validate_telegram_init_data(TELEGRAM_TOKEN, data.get("initData"), dev_user=data.get("dev_user"))
        if not user:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

        stats = game_repo.get_stats(user["user_id"], "chess")
        active = chess_service.get_rooms_for_player(user["user_id"])
        active_rooms = [
            room_id for room_id in active
            if (chess_service.get_room(room_id) and chess_service.get_room(room_id).status == "active")
        ]
        return {
            "ok": True,
            "user_id": user["user_id"],
            "name": user["name"],
            "stats": stats,
            "active_room": active_rooms[0] if active_rooms else None,
        }

    @app.get("/api/chess/room/{room_id}")
    async def get_public_room_info(room_id: str):
        room = chess_service.get_room(room_id)
        if not room:
            db_room = game_repo.get_room(room_id)
            if not db_room:
                return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
            return {
                "ok": True,
                "room_id": room_id,
                "status": db_room["status"],
                "time_control": db_room["initial_time_ms"],
                "player1_id": db_room["player1_id"],
                "player2_id": db_room["player2_id"],
            }
        return {
            "ok": True,
            "room_id": room_id,
            "status": room.status,
            "time_control": room.time_control_key,
            "player1": {"user_id": room.player1_id, "name": room.player1_name},
            "player2": {"user_id": room.player2_id, "name": room.player2_name} if room.player2_id else None,
        }

    # -------------------------------------------------------------
    # WebSocket Gameplay Endpoint
    # -------------------------------------------------------------
    @app.websocket("/ws/chess")
    async def chess_websocket_route(ws: WebSocket, ticket: str = Query(...)):
        await handle_websocket(ws, ticket, manager, chess_service)

    @app.websocket("/ws/hokm")
    async def hokm_websocket_route(ws: WebSocket, ticket: str = Query(...)):
        await handle_hokm_websocket(ws, ticket, manager, hokm_service)

    # -------------------------------------------------------------
    # Hokm REST API Endpoints
    # -------------------------------------------------------------
    @app.post("/api/hokm/ticket")
    async def get_hokm_ws_ticket(request: Request):
        data = await request.json()
        user = validate_telegram_init_data(TELEGRAM_TOKEN, data.get("initData"), dev_user=data.get("dev_user"))
        if not user:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        ticket = create_ticket(user["user_id"], user["name"])
        return {"ok": True, "ticket": ticket, "user": user}

    @app.post("/api/hokm/profile")
    async def get_hokm_profile(request: Request):
        data = await request.json()
        user = validate_telegram_init_data(TELEGRAM_TOKEN, data.get("initData"), dev_user=data.get("dev_user"))
        if not user:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        ratings = game_repo.get_user_ratings_all_modes(user["user_id"])
        stats = {
            "hokm_2p": game_repo.get_stats(user["user_id"], "hokm_2p"),
            "hokm_3p": game_repo.get_stats(user["user_id"], "hokm_3p"),
            "hokm_4p": game_repo.get_stats(user["user_id"], "hokm_4p"),
        }
        active_room_id = hokm_service._user_to_room.get(user["user_id"])
        return {
            "ok": True,
            "user_id": user["user_id"],
            "name": user["name"],
            "ratings": ratings,
            "stats": stats,
            "active_room_id": active_room_id,
        }

    @app.get("/api/hokm/leaderboard")
    async def get_hokm_leaderboard(mode: str = Query("hokm_4p")):
        rows = game_repo.get_leaderboard(mode, limit=30)
        return {"ok": True, "mode": mode, "rows": rows}

    @app.get("/api/hokm/rooms")
    async def get_hokm_rooms_count():
        return {"ok": True, "active_rooms": len(hokm_service.rooms)}

    @app.post("/api/hokm/create")
    async def create_hokm_room_http(request: Request):
        data = await request.json()
        user = validate_telegram_init_data(TELEGRAM_TOKEN, data.get("initData"), dev_user=data.get("dev_user"))
        if not user:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        mode = data.get("mode", "hokm_4p")
        time_limit = int(data.get("turn_time_limit", 30))
        is_ranked = bool(data.get("is_ranked", False))
        allow_spec = bool(data.get("allow_spectators", True))

        room = await hokm_service.create_room(
            host_id=user["user_id"],
            host_name=user["name"],
            mode_str=mode,
            turn_time_limit=time_limit,
            is_ranked=is_ranked,
            allow_spectators=allow_spec,
        )
        return {
            "ok": True,
            "room_id": room.room_id,
            "invite_code": room.invite_code,
            "game_mode": room.game_mode.value,
        }

    @app.post("/api/hokm/join")
    async def join_hokm_room_http(request: Request):
        data = await request.json()
        user = validate_telegram_init_data(TELEGRAM_TOKEN, data.get("initData"), dev_user=data.get("dev_user"))
        if not user:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        code_or_id = data.get("room_id_or_code", "").strip()
        ok, msg, room = await hokm_service.join_room(user["user_id"], user["name"], code_or_id)
        if not ok or not room:
            return JSONResponse({"ok": False, "error": msg}, status_code=400)
        return {"ok": True, "room_id": room.room_id, "invite_code": room.invite_code}

    # -------------------------------------------------------------
    # Legacy Mini App API (Backwards Compatibility with Snake & Scores)
    # -------------------------------------------------------------
    @app.post("/api/submit")
    async def submit_legacy_score(request: Request):
        data = await request.json()
        user = validate_telegram_init_data(TELEGRAM_TOKEN, data.get("initData"))
        if not user:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

        game = str(data.get("game") or "")
        if not _GAME_RE.fullmatch(game):
            return JSONResponse({"ok": False, "error": "bad game"}, status_code=400)

        score = data.get("score")
        if (
            not isinstance(score, int)
            or isinstance(score, bool)
            or not 0 <= score <= MAX_SCORE
        ):
            return JSONResponse({"ok": False, "error": "bad score"}, status_code=400)

        scope = _scope_from(user.get("start_param"))
        try:
            best, is_new_best = db_repo.upsert_miniapp_score(
                scope, user["user_id"], game, score, user["name"]
            )
            rank, _ = db_repo.miniapp_user_rank(scope, user["user_id"], game)
        except Exception:
            log.error("MiniApp score submit failure", exc_info=True)
            return JSONResponse({"ok": False, "error": "storage"}, status_code=500)

        return {
            "ok": True,
            "game": game,
            "scope": scope,
            "title": db_repo.miniapp_scope_title(scope),
            "best": best,
            "new_best": is_new_best,
            "rank": rank,
        }

    @app.post("/api/leaderboard")
    async def get_legacy_leaderboard(request: Request):
        data = await request.json()
        user = validate_telegram_init_data(TELEGRAM_TOKEN, data.get("initData"))
        if not user:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

        game = str(data.get("game") or "")
        if not _GAME_RE.fullmatch(game):
            return JSONResponse({"ok": False, "error": "bad game"}, status_code=400)

        scope = _scope_from(user.get("start_param"))
        try:
            rows = db_repo.miniapp_leaderboard(scope, game, 20)
            rank, best = db_repo.miniapp_user_rank(scope, user["user_id"], game)
        except Exception:
            log.error("MiniApp leaderboard retrieval failure", exc_info=True)
            return JSONResponse({"ok": False, "error": "storage"}, status_code=500)

        return {
            "ok": True,
            "game": game,
            "scope": scope,
            "title": db_repo.miniapp_scope_title(scope)
            or ("رتبه‌بندی سراسری" if scope == "global" else "جدول این گروه"),
            "rows": [
                {"uid": r[0], "name": r[1], "best": r[2], "plays": r[3]} for r in rows
            ],
            "you": {"rank": rank, "best": best},
        }

    # -------------------------------------------------------------
    # Legacy Static Files (Snake game, style.css, app.js, index.html)
    # -------------------------------------------------------------
    _LEGACY_WHITELIST = {
        "": "index.html",
        "/": "index.html",
        "index.html": "index.html",
        "style.css": "style.css",
        "app.js": "app.js",
        "games/snake.js": "games/snake.js",
    }

    @app.get("/{path:path}")
    async def serve_static_root(path: str):
        clean_path = path.strip("/")
        fname = _LEGACY_WHITELIST.get(clean_path)
        if fname is None:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)

        fp = os.path.join(_STATIC_DIR, fname)
        if not os.path.isfile(fp):
            return JSONResponse({"ok": False, "error": "file missing"}, status_code=500)

        ext = os.path.splitext(fname)[1]
        return FileResponse(fp, media_type=_CONTENT_TYPES.get(ext, "application/octet-stream"))

    return app


app = create_app()
