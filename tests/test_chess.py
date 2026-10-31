import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from games.chess.ai import choose_move
from games.chess.clock import ChessClock
from games.chess.engine import ChessEngine
from games.chess.models import GameEndReason, GameResult, TimeControl
from games.chess.service import ChessService
from realtime.app import app
from realtime.auth import consume_ticket, create_ticket, validate_telegram_init_data
from realtime.manager import ConnectionManager
from realtime.protocol import (
    MoveMessage,
    PingMessage,
    RequestSyncMessage,
    ResignMessage,
    parse_client_message,
)
from repositories.games import GameRepository


class _DummyWebSocket:
    def __init__(self):
        self.closed = False
        self.messages = []

    async def close(self, code=1000, reason=""):
        self.closed = True

    async def send_json(self, message):
        self.messages.append(message)


# -------------------------------------------------------------
# 1. Chess Engine Tests
# -------------------------------------------------------------
def test_engine_initial_state():
    engine = ChessEngine()
    assert engine.get_turn() == "white"
    assert not engine.is_check()
    assert not engine.is_checkmate()
    assert not engine.is_game_over()
    assert engine.is_legal_move("e2e4")
    assert engine.is_legal_move("g1f3")
    assert not engine.is_legal_move("e2e5")
    assert not engine.is_legal_move("e7e5")  # black piece on white turn


def test_engine_scholars_mate():
    """Scholar's Mate: 1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7#"""
    engine = ChessEngine()
    engine.make_move("e2e4")
    engine.make_move("e7e5")
    engine.make_move("d1h5")
    engine.make_move("b8c6")
    engine.make_move("f1c4")
    engine.make_move("g8f6")
    san, fen = engine.make_move("h5f7")

    assert san == "Qxf7#"
    assert engine.is_checkmate()
    assert engine.is_game_over()
    res = engine.get_result()
    assert res is not None
    result, reason = res
    assert result == GameResult.WHITE_WINS
    assert reason == GameEndReason.CHECKMATE


def test_engine_castling_and_en_passant():
    # En passant test
    engine = ChessEngine()
    engine.make_move("e2e4")
    engine.make_move("a7a6")
    engine.make_move("e4e5")
    engine.make_move("d7d5")  # Black pawn moves 2 squares past White pawn
    assert engine.is_legal_move("e5d6")  # En passant capture
    san, _ = engine.make_move("e5d6")
    assert "xd6" in san


def test_engine_pawn_promotion():
    # Pawn promotion setup: White pawn on e7, moves to e8
    fen = "8/4P3/8/8/8/8/8/k6K w - - 0 1"
    engine = ChessEngine(fen)
    assert engine.is_legal_move("e7e8q")
    san, fen_after = engine.make_move("e7e8q")
    assert "Q" in san
    assert "Q" in fen_after


# -------------------------------------------------------------
# 2. Chess Clock Tests
# -------------------------------------------------------------
def test_chess_clock_countdown_and_switch():
    clock = ChessClock(white_ms=1000, black_ms=1000, increment_ms=200)
    clock.start("white")
    time.sleep(0.05)
    elapsed = clock.stop_and_switch()
    assert elapsed > 0
    w, b = clock.get_remaining()
    # 1000 - elapsed + 200 > 900
    assert w > 900
    assert b == 1000


def test_chess_clock_timeout():
    clock = ChessClock(white_ms=20, black_ms=1000, increment_ms=0)
    clock.start("white")
    time.sleep(0.04)
    assert clock.is_flag_fallen() == "white"


def test_chess_clock_restore():
    clock = ChessClock(white_ms=10000, black_ms=10000, increment_ms=0)
    turn_start = time.time() - 2.0  # Turn started 2 seconds ago
    clock.restore(white_ms=10000, black_ms=10000, active_color="white", turn_started_at=turn_start)
    w, b = clock.get_remaining()
    assert w <= 8100  # Around 8000ms left
    assert b == 10000


# -------------------------------------------------------------
# 3. Game Repository Tests (SQLite WAL)
# -------------------------------------------------------------
def test_repo_room_crud_and_stats(tmp_path):
    db_file = str(tmp_path / "test_chess.db")
    repo = GameRepository(db_file)

    room_id = "test_room_1"
    repo.create_room(
        room_id=room_id,
        player1_id=101,
        initial_time_ms=300000,
        increment_ms=0,
        invite_code="inv123",
        state_json="{}",
        expires_at=time.time() + 3600,
    )

    room = repo.get_room(room_id)
    assert room is not None
    assert room["status"] == "waiting"
    assert room["player1_id"] == 101

    # Join room
    joined = repo.join_room(
        room_id=room_id,
        player2_id=102,
        white_player_id=101,
        black_player_id=102,
        status="active",
    )
    assert joined is True

    # Save moves and check idempotency
    saved1 = repo.save_move(room_id, 1, 101, "m1", "{}", "e2e4", "e4", "fen1", 298000)
    assert saved1 is True
    # Duplicate move_id rejected
    saved_dup = repo.save_move(room_id, 2, 102, "m1", "{}", "e7e5", "e5", "fen2", 299000)
    assert saved_dup is False

    # Stats tracking
    repo.update_stats(101, "chess", "win")
    repo.update_stats(102, "chess", "loss")
    s1 = repo.get_stats(101)
    assert s1["wins"] == 1
    assert s1["games_played"] == 1


# -------------------------------------------------------------
# 4. Chess Service Lifecycle Tests
# -------------------------------------------------------------
@pytest.mark.anyio
async def test_replaced_websocket_cannot_disconnect_new_connection():
    manager = ConnectionManager()
    old_ws = _DummyWebSocket()
    new_ws = _DummyWebSocket()

    await manager.connect(old_ws, 77, "Player")
    await manager.join_room(77, "room-1")
    await manager.connect(new_ws, 77, "Player")

    assert old_ws.closed is True
    assert await manager.disconnect(77, old_ws) is None
    assert manager.get_connection(77).ws is new_ws

    await manager.join_room(77, "room-1")
    assert manager.get_room_members("room-1") == {77}


@pytest.mark.anyio
async def test_chess_service_full_game(tmp_path):
    db_file = str(tmp_path / "service_test.db")
    repo = GameRepository(db_file)
    manager = ConnectionManager()
    service = ChessService(repo, manager)

    # Simulate both browser sockets being connected before the room is joined.
    await manager.connect(_DummyWebSocket(), 201, "PlayerA")
    await manager.connect(_DummyWebSocket(), 202, "PlayerB")

    # 1. Create room
    create_res = await service.create_room(creator_id=201, creator_name="PlayerA", time_control_key="blitz_5_0")
    room_id = create_res["room_id"]
    invite_code = create_res["invite_code"]

    # 2. Join room via invite
    join_res = await service.join_by_invite(invite_code, player_id=202, player_name="PlayerB")
    assert "room_id" in join_res
    room = service.get_room(room_id)
    assert room.status == "active"
    assert room.white_id in (201, 202)
    assert room.black_id in (201, 202)
    assert manager.get_room_members(room_id) == {201, 202}

    # 3. Execute first legal move by White
    white_id = room.white_id
    move_res = await service.make_move(
        room_id=room_id,
        player_id=white_id,
        uci="e2e4",
        move_id="move_1",
        expected_seq=0,
    )
    assert "state_message" in move_res
    assert move_res["state_message"]["payload"]["last_move"] == "e2e4"
    assert room.current_seq == 1

    # 4. Out of turn move rejected
    bad_move = await service.make_move(
        room_id=room_id,
        player_id=white_id,  # White moving again
        uci="e4e5",
        move_id="move_2",
        expected_seq=1,
    )
    assert "error" in bad_move
    assert bad_move["error"] == "not_your_turn"

    # 5. Black moves
    black_id = room.black_id
    black_move = await service.make_move(
        room_id=room_id,
        player_id=black_id,
        uci="e7e5",
        move_id="move_2",
        expected_seq=1,
    )
    assert "state_message" in black_move
    assert room.current_seq == 2

    # 6. Resignation
    resign_res = await service.resign(room_id, black_id)
    assert "finished_message" in resign_res
    assert resign_res["finished_message"]["payload"]["result"] == "white_wins"
    assert room.status == "finished"


@pytest.mark.anyio
async def test_abandoned_waiting_room_does_not_block_new_game(tmp_path):
    repo = GameRepository(str(tmp_path / "room_lifecycle.db"))
    manager = ConnectionManager()
    service = ChessService(repo, manager)

    first = await service.create_room(401, "Player")
    assert service.get_rooms_for_player(401) == [first["room_id"]]

    cancelled = await service.cancel_waiting_room(first["room_id"], 401)
    assert cancelled["ok"] is True
    assert service.get_rooms_for_player(401) == []
    assert repo.get_room(first["room_id"])["status"] == "abandoned"

    second = await service.create_room(401, "Player")
    assert "error" not in second
    assert second["room_id"] != first["room_id"]
    assert service.get_room(second["room_id"]).status == "waiting"


@pytest.mark.anyio
async def test_bot_game_abandons_old_invite_and_plays_black(tmp_path):
    repo = GameRepository(str(tmp_path / "bot_game.db"))
    manager = ConnectionManager()
    service = ChessService(repo, manager)
    ws = _DummyWebSocket()
    await manager.connect(ws, 402, "Human")

    invite = await service.create_room(402, "Human")
    bot_game = await service.create_bot_game(402, "Human", "easy")
    assert "error" not in bot_game
    assert repo.get_room(invite["room_id"])["status"] == "abandoned"

    room = service.get_room(bot_game["room_id"])
    assert room is not None
    assert room.is_bot_game is True
    assert room.white_id == 402
    assert room.black_id == -1
    assert room.bot_difficulty == "easy"
    assert manager.get_room_members(room.room_id) == {402}

    human_move = await service.make_move(
        room.room_id, 402, "e2e4", "human-first", 0
    )
    assert "error" not in human_move
    assert room.engine.get_turn() == "black"

    bot_move = await service.make_bot_move(room.room_id)
    assert "error" not in bot_move
    assert bot_move["state_message"]["seq"] == 2
    assert room.engine.get_turn() == "white"
    assert room.move_history[1]["player_id"] == -1
    assert room.engine.is_legal_move("g1f3")


@pytest.mark.anyio
async def test_bot_game_blocks_concurrent_match_and_does_not_offer_draw(tmp_path):
    repo = GameRepository(str(tmp_path / "bot_guard.db"))
    service = ChessService(repo, ConnectionManager())

    bot_game = await service.create_bot_game(403, "Human", "medium")
    room_id = bot_game["room_id"]

    second_room = await service.create_room(403, "Human")
    assert second_room["error"] == "already_in_game"
    assert second_room["room_id"] == room_id

    draw = await service.offer_draw(room_id, 403)
    assert draw["error"] == "draw_unavailable"


def test_chess_ai_returns_legal_moves_for_each_difficulty():
    engine = ChessEngine()
    for difficulty in ("easy", "medium", "hard"):
        move = choose_move(engine.get_fen(), difficulty)
        assert engine.is_legal_move(move)


# -------------------------------------------------------------
# 5. Protocol and Ticket Tests
# -------------------------------------------------------------
def test_ticket_creation_and_single_use():
    ticket = create_ticket(user_id=555, name="Ali")
    assert ticket
    consumed = consume_ticket(ticket)
    assert consumed is not None
    assert consumed["user_id"] == 555
    # Second consume must fail (single-use)
    assert consume_ticket(ticket) is None


def test_protocol_message_parsing():
    valid_move = {"type": "game.move", "game_id": "r1", "move_id": "m1", "expected_seq": 3, "payload": {"uci": "e2e4"}}
    msg = parse_client_message(valid_move)
    assert isinstance(msg, MoveMessage)
    assert msg.payload.uci == "e2e4"

    ping = parse_client_message({"type": "ping"})
    assert isinstance(ping, PingMessage)

    bad = parse_client_message({"type": "game.move", "game_id": "r1"})  # Missing fields
    assert bad is None


# -------------------------------------------------------------
# 6. FastAPI HTTP Endpoint Tests
# -------------------------------------------------------------
def test_fastapi_endpoints():
    client = TestClient(app)
    # Health check
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert "connections" in data
    assert "active_games" in data

    # 404 for unknown
    res_404 = client.get("/api/unknown_route")
    assert res_404.status_code == 404

    # Chess SPA serving
    res_chess = client.get("/chess/")
    assert res_chess.status_code == 200
    assert "text/html" in res_chess.headers["content-type"]

    # Legacy static files serving
    res_snake = client.get("/games/snake.js")
    assert res_snake.status_code == 200
    assert "javascript" in res_snake.headers["content-type"]
