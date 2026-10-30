from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class GameResult(str, Enum):
    WHITE_WINS = "white_wins"
    BLACK_WINS = "black_wins"
    DRAW = "draw"


class GameEndReason(str, Enum):
    CHECKMATE = "checkmate"
    STALEMATE = "stalemate"
    RESIGNATION = "resignation"
    TIMEOUT = "timeout"
    DRAW_AGREEMENT = "draw_agreement"
    THREEFOLD_REPETITION = "threefold_repetition"
    FIVEFOLD_REPETITION = "fivefold_repetition"
    FIFTY_MOVES = "fifty_moves"
    SEVENTY_FIVE_MOVES = "seventy_five_moves"
    INSUFFICIENT_MATERIAL = "insufficient_material"
    ABANDONED = "abandoned"


TIME_CONTROLS: Dict[str, tuple[int, int]] = {
    "bullet_1_0": (60_000, 0),
    "blitz_3_0": (180_000, 0),
    "blitz_3_2": (180_000, 2000),
    "blitz_5_0": (300_000, 0),
    "rapid_10_0": (600_000, 0),
}


@dataclass
class TimeControl:
    key: str
    initial_ms: int
    increment_ms: int
    label: str = ""

    @classmethod
    def from_key(cls, key: str) -> "TimeControl":
        if key not in TIME_CONTROLS:
            key = "blitz_5_0"
        initial, increment = TIME_CONTROLS[key]
        labels = {
            "bullet_1_0": "۱+۰ بولت",
            "blitz_3_0": "۳+۰ بلیتز",
            "blitz_3_2": "۳+۲ بلیتز",
            "blitz_5_0": "۵+۰ بلیتز",
            "rapid_10_0": "۱۰+۰ رپید",
        }
        return cls(
            key=key,
            initial_ms=initial,
            increment_ms=increment,
            label=labels.get(key, key),
        )


@dataclass
class MoveRecord:
    seq: int
    player_id: int
    move_id: str
    uci: str
    san: str
    fen_after: str
    time_remaining_ms: int
    timestamp: float


@dataclass
class ChessGameState:
    fen: str = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    turn: str = "white"  # "white" | "black"
    move_history: List[Dict[str, Any]] = field(default_factory=list)
    check: bool = False
    status: str = "active"
    result: Optional[str] = None
    end_reason: Optional[str] = None
    last_move: Optional[str] = None
    draw_offered_by: Optional[int] = None
    white_remaining_ms: int = 0
    black_remaining_ms: int = 0
