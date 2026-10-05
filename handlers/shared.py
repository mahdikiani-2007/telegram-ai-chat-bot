import logging
import random
import re
import threading
import time

from config import ADMIN_ID, ADMIN_IDS, OWNER_ID
from db import repo

log = logging.getLogger("L-bot")


def _valid_uid(user_id):
    """Strict numeric-id check shared by is_admin/is_owner: only positive
    ints pass. 0/None/negatives/non-ints (incl. bools) are never admins."""
    if isinstance(user_id, bool):
        return False
    return isinstance(user_id, int) and user_id > 0


def extract_text_from_message(message):
    return (
        message.text if message.text else (message.caption if message.caption else "")
    )

# =========================
# Admin identity
# =========================
def is_owner(user_id: int) -> bool:
    """True only for the owner (first ID in ADMIN_IDS, else ADMIN_ID)."""
    if not _valid_uid(user_id):
        return False
    return bool(OWNER_ID) and user_id == OWNER_ID


# =========================
# Full-set identity caches (G1): the WHOLE panel_admins / panel_bans
# tables, one query each, 10s TTL. The gate calls is_admin/is_banned on
# EVERY update, so per-update DB reads must be ZERO with a warm cache.
# - invalidate_identity_caches() runs on every panel write in this process.
# - A failed refresh keeps serving the stale copy; with no copy at all the
#   answer is empty (env-listed admins still pass via ADMIN_IDS — fail
#   closed for permissions, fail open for bans).
# =========================
_IDENTITY_TTL = 10.0

_identity_cache = {"admins": (frozenset(), 0.0), "bans": (frozenset(), 0.0)}
_identity_lock = threading.Lock()


def _refresh_identity_set(table, id_column):
    """One full-table SELECT. Returns (frozenset, ok). Never raises."""
    try:
        conn = repo._connect()
        rows = conn.execute(
            f"SELECT {id_column} FROM {table}"
        ).fetchall()
        return frozenset(int(r[0]) for r in rows), True
    except Exception:
        log.warning("identity cache refresh failed for %s", table, exc_info=True)
        return None, False


def get_cached_admin_ids():
    """DB-added admin IDs (env IDs in ADMIN_IDS are always valid on top)."""
    return _get_cached_set("admins", "panel_admins", "user_id")


def _get_cached_set(name, table, id_column):
    now = time.time()
    with _identity_lock:
        ids, expires = _identity_cache[name]
        if now < expires:
            return ids
    fresh, ok = _refresh_identity_set(table, id_column)
    with _identity_lock:
        if ok:
            _identity_cache[name] = (fresh, now + _IDENTITY_TTL)
            return fresh
        old_ids, _exp = _identity_cache[name]
        return old_ids  # stale copy on failure; empty if never loaded


def get_cached_ban_ids():
    """Banned user IDs. Fail open: refresh failure yields stale/empty."""
    return _get_cached_set("bans", "panel_bans", "user_id")


def invalidate_identity_caches():
    """Call after every panel_admins/panel_bans write in this process."""
    with _identity_lock:
        _identity_cache["admins"] = (frozenset(), 0.0)
        _identity_cache["bans"] = (frozenset(), 0.0)


def is_admin(user_id: int) -> bool:
    """True for the owner, any env-listed admin, or any DB-added admin.

    Zero DB reads with a warm cache (full-set cache above). Fail closed:
    refresh failure serves stale/empty, so DB-added admins are denied while
    env-listed admins still pass — never True by default.
    """
    if not _valid_uid(user_id):
        return False
    if user_id in ADMIN_IDS:
        return True
    try:
        return user_id in get_cached_admin_ids()
    except Exception:
        log.warning("is_admin: admin-set lookup failed; env-only answer", exc_info=True)
        return False

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
