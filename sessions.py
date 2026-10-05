import logging
import threading
import time

from config import SESSION_GC_INTERVAL, SESSION_TTL_SECONDS
from db import repo

log = logging.getLogger("L-bot")


# =========================
# TTL-aware Session Store (fixes the unbounded in-memory session leak)
# =========================
class TTLSessionStore:
    """Thread-safe key/value store with per-entry TTL expiry.

    Used for every short-lived multi-step conversation state (todo/reminder
    creation wizards, admin broadcast flow, waiting-for-text-input state,
    etc.) so abandoned flows don't leak memory over long uptimes. A
    background sweep (see _session_gc_loop) periodically purges expired
    entries; individual get/pop calls also self-expire lazily so a leak is
    bounded even if the sweep is delayed.
    """

    def __init__(self, ttl_seconds=900):
        self._lock = threading.Lock()
        self._data = {}  # key -> (value, expires_at)
        self._ttl = ttl_seconds

    def set(self, key, value):
        with self._lock:
            self._data[key] = (value, time.time() + self._ttl)

    def get(self, key, default=None):
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return default
            value, expires_at = entry
            if time.time() > expires_at:
                del self._data[key]
                return default
            return value

    def pop(self, key, default=None):
        with self._lock:
            entry = self._data.pop(key, None)
            if entry is None:
                return default
            value, expires_at = entry
            if time.time() > expires_at:
                return default
            return value

    def discard(self, key):
        with self._lock:
            self._data.pop(key, None)

    def discard_where(self, key_pred=None, value_pred=None):
        """Remove every entry whose key (and/or value payload) matches.
        Stage 2C user reset. Never raises."""
        try:
            with self._lock:
                for k, (v, _exp) in list(self._data.items()):
                    if ((key_pred is not None and key_pred(k))
                            or (value_pred is not None and value_pred(v))):
                        self._data.pop(k, None)
        except Exception:
            log.warning("discard_where failed", exc_info=True)

    def sweep(self):
        now = time.time()
        with self._lock:
            expired = [k for k, (_, exp) in self._data.items() if now > exp]
            for k in expired:
                del self._data[k]
        return len(expired)

    def __len__(self):
        with self._lock:
            return len(self._data)


# Waiting-for-plain-text-input sessions (reminder/todo custom time & recurrence)
_waiting_text_input = TTLSessionStore(SESSION_TTL_SECONDS)

WAITING_FOR_REMINDER_TIME = "WAITING_FOR_REMINDER_TIME"
WAITING_FOR_REMINDER_RECURRENCE = "WAITING_FOR_REMINDER_RECURRENCE"
WAITING_FOR_TODO_DEADLINE = "WAITING_FOR_TODO_DEADLINE"
WAITING_FOR_TODO_RECURRENCE = "WAITING_FOR_TODO_RECURRENCE"

# Todo wizard session stores (all TTL-backed dicts of payload dicts)
_todo_sessions = TTLSessionStore(SESSION_TTL_SECONDS)          # -> {text, assignee_id, assignee_username}
_todo_pending_cat = TTLSessionStore(SESSION_TTL_SECONDS)       # -> {..., priority}
_todo_pending_dl = TTLSessionStore(SESSION_TTL_SECONDS)        # -> {..., category}
_todo_pending_rec = TTLSessionStore(SESSION_TTL_SECONDS)       # -> {..., deadline}

# Reminder wizard session stores
_remind_sessions = TTLSessionStore(SESSION_TTL_SECONDS)            # -> {text, assignee_id, assignee_username}
_remind_pending_mention = TTLSessionStore(SESSION_TTL_SECONDS)     # -> {..., trigger_time}
_remind_pending_recurrence = TTLSessionStore(SESSION_TTL_SECONDS)  # -> {..., mention_type}

# Admin panel text-input sessions (typed IDs, searches, settings, broadcast)
_panel_input_sessions = TTLSessionStore(SESSION_TTL_SECONDS)  # user_id -> {state, ...}

PANEL_ADD_ADMIN = "PANEL_ADD_ADMIN"
PANEL_USER_SEARCH = "PANEL_USER_SEARCH"
PANEL_GROUP_SEARCH = "PANEL_GROUP_SEARCH"
PANEL_SETTING_VALUE = "PANEL_SETTING_VALUE"
PANEL_LIMIT_ADD = "PANEL_LIMIT_ADD"
PANEL_BROADCAST_CHAT = "PANEL_BROADCAST_CHAT"
PANEL_BROADCAST_CONTENT = "PANEL_BROADCAST_CONTENT"

_ALL_SESSION_STORES = [
    _waiting_text_input,
    _todo_sessions,
    _todo_pending_cat,
    _todo_pending_dl,
    _todo_pending_rec,
    _remind_sessions,
    _remind_pending_mention,
    _remind_pending_recurrence,
    _panel_input_sessions,
]

# Defined later in the module (YouTube section) but swept by the same GC
# loop, which runs well after import time — this deferred binding keeps the
# sweep list complete without reordering module definitions.
_ALL_SESSION_STORES_LATE = []


def _register_late_session_store(store):
    _ALL_SESSION_STORES_LATE.append(store)


def clear_user_state(user_id):
    """Drop all wizard/session entries keyed by this user, across every TTL
    store (todo/remind/waiting-input/admin-broadcast/panel). Keys are either
    the bare uid or (chat_id_str, uid) tuples — matched exactly, never by
    substring. Stage 2C user reset. Never raises."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return

    def _mine(key):
        if key == uid:
            return True
        return isinstance(key, tuple) and uid in key

    for store in list(_ALL_SESSION_STORES) + list(_ALL_SESSION_STORES_LATE):
        try:
            # Opaque token-keyed stores (e.g. the YT picker in
            # services/media.py) never match _mine; they are cleared via
            # their own module function.
            store.discard_where(key_pred=_mine)
        except Exception:
            log.warning("clear_user_state failed for a store", exc_info=True)


def _session_gc_loop():
    """Background daemon: periodically sweeps every TTL session store so
    abandoned wizard flows never accumulate in RAM over long uptimes. Also
    prunes old bot_media_messages rows (see ChatRepository) on the same
    timer, rather than spinning up a whole separate thread for it."""
    log.info("Session GC thread started (interval=%ds, ttl=%ds).", SESSION_GC_INTERVAL, SESSION_TTL_SECONDS)
    while True:
        time.sleep(SESSION_GC_INTERVAL)
        try:
            total = sum(store.sweep() for store in (_ALL_SESSION_STORES + _ALL_SESSION_STORES_LATE))
            if total:
                log.info("Session GC: expired %d stale session entries.", total)
        except Exception:
            log.warning("Session GC sweep failed", exc_info=True)
        try:
            repo.cleanup_old_bot_media_messages()
        except Exception:
            log.warning("bot_media_messages cleanup failed", exc_info=True)
        try:
            repo.purge_old_chat_log(keep_hours=48)
        except Exception:
            log.warning("chat_log cleanup failed", exc_info=True)

