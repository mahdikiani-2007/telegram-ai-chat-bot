import asyncio
import logging
import time
from typing import Any, Dict, Optional, Set
from fastapi import WebSocket

log = logging.getLogger("L-bot")


class Connection:
    __slots__ = ("ws", "user_id", "name", "room_id", "connected_at", "last_active")

    def __init__(self, ws: WebSocket, user_id: int, name: str):
        self.ws = ws
        self.user_id = user_id
        self.name = name
        self.room_id: Optional[str] = None
        self.connected_at = time.time()
        self.last_active = time.time()


class ConnectionManager:
    """Manages active WebSockets, user bindings, room memberships, and broadcast routing."""

    def __init__(self):
        self._connections: Dict[int, Connection] = {}  # user_id -> Connection
        self._room_members: Dict[str, Set[int]] = {}  # room_id -> {user_ids}
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, user_id: int, name: str) -> Connection:
        """Register newly upgraded WebSocket connection. Closes previous connection if any."""
        async with self._lock:
            existing = self._connections.get(user_id)
            if existing:
                try:
                    await existing.ws.close(code=1000, reason="superseded_by_new_connection")
                except Exception:
                    pass
            conn = Connection(ws, user_id, name)
            self._connections[user_id] = conn
            log.info("Registered WS connection for user %d (%s). Total: %d", user_id, name, len(self._connections))
            return conn

    async def disconnect(
        self, user_id: int, expected_ws: Optional[WebSocket] = None
    ) -> Optional[str]:
        """Unregister only the expected socket, protecting newer reconnects.

        A replaced WebSocket can finish its handler after a new connection for
        the same Telegram user has already been registered. In that case the
        old handler must not remove the new connection or its room membership.
        """
        async with self._lock:
            conn = self._connections.get(user_id)
            if conn is None:
                return None
            if expected_ws is not None and conn.ws is not expected_ws:
                return None

            self._connections.pop(user_id, None)
            room_id = conn.room_id
            if room_id and room_id in self._room_members:
                self._room_members[room_id].discard(user_id)
                if not self._room_members[room_id]:
                    self._room_members.pop(room_id, None)
            log.info("Unregistered WS connection for user %d. Remaining: %d", user_id, len(self._connections))
            return room_id

    async def join_room(self, user_id: int, room_id: str) -> None:
        async with self._lock:
            conn = self._connections.get(user_id)
            if conn:
                if conn.room_id and conn.room_id != room_id:
                    old_room = conn.room_id
                    if old_room in self._room_members:
                        self._room_members[old_room].discard(user_id)
                conn.room_id = room_id
                self._room_members.setdefault(room_id, set()).add(user_id)

    async def leave_room(self, user_id: int, room_id: str) -> None:
        async with self._lock:
            conn = self._connections.get(user_id)
            if conn and conn.room_id == room_id:
                conn.room_id = None
            if room_id in self._room_members:
                self._room_members[room_id].discard(user_id)
                if not self._room_members[room_id]:
                    self._room_members.pop(room_id, None)

    async def send_to_user(self, user_id: int, message: Dict[str, Any]) -> bool:
        """Send JSON message directly to a user's active WebSocket."""
        conn = self._connections.get(user_id)
        if not conn:
            return False
        try:
            await conn.ws.send_json(message)
            conn.last_active = time.time()
            return True
        except Exception:
            log.debug("Failed sending message to user %d", user_id)
            return False

    async def broadcast_to_room(
        self,
        room_id: str,
        message: Dict[str, Any],
        exclude_user_id: Optional[int] = None,
    ) -> int:
        """Broadcast message to all connected members of a room."""
        members = self._room_members.get(room_id, set()).copy()
        sent = 0
        for uid in members:
            if exclude_user_id is not None and uid == exclude_user_id:
                continue
            if await self.send_to_user(uid, message):
                sent += 1
        return sent

    async def send_to_room(self, room_id: str, message: Dict[str, Any]) -> int:
        return await self.broadcast_to_room(room_id, message, exclude_user_id=None)

    def is_connected(self, user_id: int) -> bool:
        return user_id in self._connections

    def get_connection(self, user_id: int) -> Optional[Connection]:
        return self._connections.get(user_id)

    def get_room_members(self, room_id: str) -> Set[int]:
        return self._room_members.get(room_id, set()).copy()

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    @property
    def room_count(self) -> int:
        return len(self._room_members)

    def get_stats(self) -> Dict[str, int]:
        return {
            "connections": self.connection_count,
            "active_rooms": self.room_count,
        }
