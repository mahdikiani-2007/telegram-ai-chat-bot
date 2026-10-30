import asyncio
import logging
import time
from typing import Any, Dict, Optional

log = logging.getLogger("L-bot")


class MatchmakingQueue:
    """In-memory matchmaking queue for pairing players with identical time controls.
    Lightweight, no database polling.
    """

    def __init__(self):
        self._queue: Dict[int, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def add_to_queue(self, user_id: int, name: str, time_control_key: str) -> bool:
        async with self._lock:
            self._queue[user_id] = {
                "user_id": user_id,
                "name": name,
                "time_control": time_control_key,
                "joined_at": time.time(),
            }
            log.info("Player %d (%s) entered matchmaking queue for %s", user_id, name, time_control_key)
            return True

    async def remove_from_queue(self, user_id: int) -> bool:
        async with self._lock:
            removed = self._queue.pop(user_id, None)
            if removed:
                log.info("Player %d left matchmaking queue", user_id)
                return True
            return False

    async def find_match(self, time_control_key: str, exclude_user_id: int) -> Optional[Dict[str, Any]]:
        """Look for a waiting opponent with the same time control (FIFO)."""
        async with self._lock:
            # Sort by joined_at to prioritize longest-waiting players
            candidates = [
                entry
                for entry in self._queue.values()
                if entry["time_control"] == time_control_key and entry["user_id"] != exclude_user_id
            ]
            if candidates:
                candidates.sort(key=lambda x: x["joined_at"])
                matched = candidates[0]
                self._queue.pop(matched["user_id"], None)
                log.info(
                    "Matchmaking found opponent %d for player %d on %s",
                    matched["user_id"],
                    exclude_user_id,
                    time_control_key,
                )
                return matched
            return None

    async def is_in_queue(self, user_id: int) -> bool:
        async with self._lock:
            return user_id in self._queue

    async def cleanup_stale(self, max_age_seconds: float = 120.0) -> int:
        now = time.time()
        async with self._lock:
            stale_ids = [
                uid
                for uid, item in self._queue.items()
                if now - item.get("joined_at", now) > max_age_seconds
            ]
            for uid in stale_ids:
                self._queue.pop(uid, None)
            if stale_ids:
                log.info("Cleaned up %d stale matchmaking entries", len(stale_ids))
            return len(stale_ids)
