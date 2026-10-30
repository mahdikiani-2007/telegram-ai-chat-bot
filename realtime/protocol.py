import uuid
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field


# -------------------------------------------------------------
# Client-to-Server Messages
# -------------------------------------------------------------
class MovePayload(BaseModel):
    uci: str


class MoveMessage(BaseModel):
    type: Literal["game.move"] = "game.move"
    game_id: str
    move_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    expected_seq: int
    payload: MovePayload


class ResignMessage(BaseModel):
    type: Literal["game.resign"] = "game.resign"
    game_id: str


class DrawOfferMessage(BaseModel):
    type: Literal["game.draw.offer"] = "game.draw.offer"
    game_id: str


class DrawAcceptMessage(BaseModel):
    type: Literal["game.draw.accept"] = "game.draw.accept"
    game_id: str


class DrawDeclineMessage(BaseModel):
    type: Literal["game.draw.decline"] = "game.draw.decline"
    game_id: str


class RequestSyncMessage(BaseModel):
    type: Literal["game.request_sync"] = "game.request_sync"
    game_id: str


class PingMessage(BaseModel):
    type: Literal["ping"] = "ping"


ClientMessage = Union[
    MoveMessage,
    ResignMessage,
    DrawOfferMessage,
    DrawAcceptMessage,
    DrawDeclineMessage,
    RequestSyncMessage,
    PingMessage,
]


def parse_client_message(data: dict) -> Optional[ClientMessage]:
    """Strictly validate and parse incoming client JSON message."""
    if not isinstance(data, dict):
        return None
    msg_type = data.get("type")
    parsers = {
        "game.move": MoveMessage,
        "game.resign": ResignMessage,
        "game.draw.offer": DrawOfferMessage,
        "game.draw.accept": DrawAcceptMessage,
        "game.draw.decline": DrawDeclineMessage,
        "game.request_sync": RequestSyncMessage,
        "ping": PingMessage,
    }
    parser = parsers.get(msg_type)
    if not parser:
        return None
    try:
        return parser.model_validate(data)
    except Exception:
        return None


# -------------------------------------------------------------
# Server-to-Client Message Constructors
# -------------------------------------------------------------
def make_game_state(
    game_id: str,
    seq: int,
    fen: str,
    turn: str,
    last_move: Optional[str],
    check: bool,
    status: str,
    white_ms: int,
    black_ms: int,
    move_history: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "type": "game.state",
        "game_id": game_id,
        "seq": seq,
        "payload": {
            "fen": fen,
            "turn": turn,
            "last_move": last_move,
            "check": check,
            "status": status,
            "white_ms": white_ms,
            "black_ms": black_ms,
            "move_history": move_history or [],
        },
    }


def make_game_start(
    game_id: str,
    white_info: Dict[str, Any],
    black_info: Dict[str, Any],
    fen: str,
    time_control: Dict[str, Any],
    seq: int = 0,
) -> Dict[str, Any]:
    return {
        "type": "game.start",
        "game_id": game_id,
        "seq": seq,
        "payload": {
            "white": white_info,
            "black": black_info,
            "fen": fen,
            "time_control": time_control,
        },
    }


def make_game_finished(
    game_id: str,
    result: str,
    reason: str,
    winner_id: Optional[int],
    seq: int,
) -> Dict[str, Any]:
    return {
        "type": "game.finished",
        "game_id": game_id,
        "seq": seq,
        "payload": {
            "result": result,
            "reason": reason,
            "winner_id": winner_id,
        },
    }


def make_player_event(event_type: str, game_id: str, user_id: int) -> Dict[str, Any]:
    return {
        "type": event_type,
        "game_id": game_id,
        "payload": {"user_id": user_id},
    }


def make_draw_offer(game_id: str, from_id: int) -> Dict[str, Any]:
    return {
        "type": "game.draw.offer",
        "game_id": game_id,
        "payload": {"from_id": from_id},
    }


def make_draw_declined(game_id: str, by_id: int) -> Dict[str, Any]:
    return {
        "type": "game.draw.declined",
        "game_id": game_id,
        "payload": {"by_id": by_id},
    }


def make_clock_sync(game_id: str, white_ms: int, black_ms: int) -> Dict[str, Any]:
    return {
        "type": "game.clock",
        "game_id": game_id,
        "payload": {"white_ms": white_ms, "black_ms": black_ms},
    }


def make_error(code: str, message: str) -> Dict[str, Any]:
    return {
        "type": "error",
        "payload": {"code": code, "message": message},
    }


def make_pong() -> Dict[str, Any]:
    return {"type": "pong"}


def make_sync(game_id: str, seq: int, full_state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "game.sync",
        "game_id": game_id,
        "seq": seq,
        "payload": full_state,
    }
