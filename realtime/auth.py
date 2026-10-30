import logging
import secrets
import threading
import time
from typing import Any, Dict, Optional

log = logging.getLogger("L-bot")

# In-memory ticket cache: {ticket_str: {"user_id": int, "name": str, "created_at": float}}
_tickets: Dict[str, Dict[str, Any]] = {}
_ticket_lock = threading.Lock()
TICKET_TTL_SECONDS = 30.0
TICKET_MAX_PENDING = 2000


def validate_telegram_init_data(
    bot_token: str,
    raw_init_data: str,
    dev_user: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Verify Telegram Mini App HMAC signature using telebot.util.parse_web_app_data.
    Rejects missing, forged, or stale authentication (> 24 hours).
    When opened directly in a browser for local testing without initData, allows dev_user fallback.
    """
    if not raw_init_data:
        if dev_user is not None:
            try:
                dev_id = int(dev_user)
                return {
                    "user_id": dev_id,
                    "name": f"بازیکن تست {dev_id}",
                    "username": f"test_player_{dev_id}",
                    "start_param": "",
                }
            except (ValueError, TypeError):
                pass
        return None
    try:
        from telebot import util

        parsed = util.parse_web_app_data(str(bot_token), str(raw_init_data))
        if not parsed:
            return None

        user = parsed.get("user")
        if not isinstance(user, dict):
            return None

        try:
            uid = int(user.get("id"))
        except (TypeError, ValueError):
            return None

        if uid <= 0:
            return None

        # Check auth_date for expiration
        auth_date = parsed.get("auth_date")
        if auth_date:
            try:
                age = time.time() - int(auth_date)
                if age > 86400:  # 24 hours max
                    log.warning("Telegram initData expired (age=%.0fs) for user %d", age, uid)
                    return None
            except (TypeError, ValueError):
                pass

        first_name = str(user.get("first_name") or "").strip()
        last_name = str(user.get("last_name") or "").strip()
        name = f"{first_name} {last_name}".strip()[:64] or f"User{uid}"

        return {
            "user_id": uid,
            "name": name,
            "username": str(user.get("username") or ""),
            "start_param": str(parsed.get("start_param") or ""),
        }
    except Exception:
        log.debug("Telegram initData validation exception", exc_info=True)
        return None


def create_ticket(user_id: int, name: str) -> str:
    """Generate a high-entropy, short-lived, single-use ticket for WebSocket upgrade."""
    ticket = secrets.token_urlsafe(32)
    now = time.time()
    with _ticket_lock:
        # Periodic cleanup of expired tickets if cache grows
        if len(_tickets) > TICKET_MAX_PENDING:
            expired = [
                k for k, v in _tickets.items() if now - v["created_at"] > TICKET_TTL_SECONDS
            ]
            for k in expired:
                _tickets.pop(k, None)

        _tickets[ticket] = {
            "user_id": user_id,
            "name": name,
            "created_at": now,
        }
    return ticket


def consume_ticket(ticket: str) -> Optional[Dict[str, Any]]:
    """Consume and invalidate the ticket atomically. Returns user dict or None."""
    now = time.time()
    with _ticket_lock:
        info = _tickets.pop(ticket, None)
    if not info:
        return None
    if now - info["created_at"] > TICKET_TTL_SECONDS:
        log.info("WebSocket ticket expired for user %d", info.get("user_id", 0))
        return None
    return {"user_id": info["user_id"], "name": info["name"]}
