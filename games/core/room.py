import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RoomStatus(str, Enum):
    WAITING = "waiting"
    ACTIVE = "active"
    FINISHED = "finished"
    ABANDONED = "abandoned"


@dataclass
class PlayerInfo:
    user_id: int
    name: str
    connected: bool = False


@dataclass
class BaseRoom:
    room_id: str
    game_type: str
    status: RoomStatus
    player1: Optional[PlayerInfo] = None
    player2: Optional[PlayerInfo] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
