import logging
import random
import re
import threading

from config import ADMIN_ID
from db import repo

log = logging.getLogger("L-bot")


def extract_text_from_message(message):
    return (
        message.text if message.text else (message.caption if message.caption else "")
    )

# =========================
# Admin Panel & Broadcast System
# =========================
def is_admin(user_id: int) -> bool:
    """Check whether the given user ID belongs to the bot administrator."""
    return user_id == ADMIN_ID

# =========================
# Sessions (private chats only) — each user gets independent conversation
# threads. A session is just a memory_id namespace (`<user_id>#<sid>`); the
# active pointer lives in user_active_sessions (NULL = legacy plain-user
# memory, so users who never touch /session see zero change).
# =========================
SESSION_MAX_PER_USER = 20

SESSION_ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"

_SESSION_ACTIVE_UPDATE_LOCK = threading.Lock()

def _new_session_id():
    return "".join(random.choice(SESSION_ID_ALPHABET) for _ in range(8))

def _session_title_from_text(text):
    """Derive a short title from the message that starts a session."""
    clean = re.sub(r"\s+", " ", (text or "")).strip()
    if not clean:
        return ""
    return clean[:60] + ("…" if len(clean) > 60 else "")

def _resolve_private_session(user_id, text):
    """Return (active_session_id, memory_id) for an incoming private-chat
    message. Auto-creates the first session lazily on the user's first
    message so `/session` always has something to show; title is seeded from
    that first message. Called on a telebot worker thread."""
    try:
        active = repo.get_active_session(user_id)
        if active == "deleted":
            active = None
        if active is None:
            with _SESSION_ACTIVE_UPDATE_LOCK:
                active = repo.get_active_session(user_id)
                if active is None:
                    repo.create_session(user_id, _new_session_id(), _session_title_from_text(text))
                    active = repo.get_active_session(user_id)
            if not active:
                return None, str(user_id)  # DB unavailable — degrade to plain memory
        else:
            try:
                if not repo.sessions_has_title(user_id, active):
                    title = _session_title_from_text(text)
                    if title:
                        repo.rename_session(user_id, active, title)
            except Exception:
                pass
        return active, f"{user_id}#{active}"
    except Exception:
        log.warning("Session resolution failed; falling back to plain memory", exc_info=True)
        return None, str(user_id)
