import hashlib
import logging
import threading
import time

from telebot import types as tb_types

from db import repo
from handlers.shared import (
    get_cached_ban_ids,
    invalidate_identity_caches,
    is_admin,
    is_owner,
)

log = logging.getLogger("L-bot")


# =========================
# Failure semantics (panel security model — read carefully)
# =========================
# - Permissions (is_admin/is_owner in handlers/shared.py) FAIL CLOSED: a DB
#   error yields the env-only answer; DB-added admins are denied, nobody is
#   ever granted access by an exception path.
# - Feature-toggle, maintenance-mode and ban reads below FAIL OPEN: a DB
#   error returns current behavior (features on, maintenance off, not
#   banned). Rationale: these gates sit in front of EVERY user message, so a
#   transient DB hiccup must degrade to "bot works as before", never to "bot
#   goes dark for everyone". Permission checks stay closed; behavior flags
#   stay open. Never mix the two directions.
# =========================

# =========================
# Runtime settings (DB-backed, short-TTL cached)
# =========================
_SETTINGS_CACHE_TTL = 10.0  # seconds; keeps hot-path gates free without risking stale flips for long

_settings_cache = {}  # key -> (value, expires_at)
_settings_lock = threading.Lock()


def _cache_invalidate(key=None):
    with _settings_lock:
        if key is None:
            _settings_cache.clear()
        else:
            _settings_cache.pop(str(key), None)


def clear_settings_cache():
    """Test hook + post-write invalidation entry point."""
    _cache_invalidate()


def get_setting(key, default=None):
    """Read one panel setting. DB errors return `default` (fail open)."""
    key = str(key)
    now = time.time()
    with _settings_lock:
        entry = _settings_cache.get(key)
        if entry is not None and entry[1] > now:
            return entry[0]
    try:
        value = repo.get_panel_setting(key, default)
    except Exception:
        log.warning("panel settings read failed for %r; using default", key, exc_info=True)
        return default
    with _settings_lock:
        _settings_cache[key] = (value, now + _SETTINGS_CACHE_TTL)
    return value


def set_setting(key, value):
    """Write one panel setting and invalidate the cache entry."""
    repo.set_panel_setting(str(key), str(value))
    _cache_invalidate(key)


def delete_setting(key):
    repo.delete_panel_setting(str(key))
    _cache_invalidate(key)


# =========================
# Maintenance mode + bans (fail open — see semantics above)
# =========================
MAINTENANCE_KEY = "maintenance.enabled"
MAINTENANCE_NOTICE_KEY = "maintenance.notice"
MAINTENANCE_NOTICE_MINUTES_KEY = "maintenance.notice_minutes"

DEFAULT_MAINTENANCE_NOTICE = (
    "🚧 ربات موقتاً در دست تعمیر است. لطفاً دقایقی دیگر تلاش کنید.\n"
    "Bot is temporarily under maintenance. Please try again later."
)
DEFAULT_NOTICE_MINUTES = 10


def maintenance_active():
    try:
        return get_setting(MAINTENANCE_KEY, "0") == "1"
    except Exception:
        return False


def is_banned(user_id):
    """True if the user is app-banned. Admins can never be banned (checked
    first, so a stale bans row can't lock out an admin). Zero DB reads with
    a warm cache; fail open."""
    try:
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
            return False
        if is_admin(user_id):
            return False
        return user_id in get_cached_ban_ids()
    except Exception:
        log.warning("panel ban lookup failed; treating as not banned", exc_info=True)
        return False


# =========================
# Identity write wrappers: repo write + immediate cache invalidation so a
# ban/unban (or admin add/remove) takes effect on the very next update.
# =========================
def grant_admin(user_id, added_by):
    repo.add_panel_admin(int(user_id), int(added_by))
    invalidate_identity_caches()


def revoke_admin(user_id):
    removed = repo.remove_panel_admin(int(user_id))
    invalidate_identity_caches()
    return removed


def ban_uid(user_id, reason, banned_by):
    repo.ban_user(int(user_id), reason, int(banned_by))
    invalidate_identity_caches()


def unban_uid(user_id):
    removed = repo.unban_user(int(user_id))
    invalidate_identity_caches()
    return removed


# =========================
# Feature toggles (Stage 2B): global on/off per feature, read from the
# cached settings (zero DB reads warm), fail open (absent/error = enabled).
# Admins and the owner are EXEMPT from toggles (same as maintenance).
# Per-group debate override: chat:<cid>:debate = "0" disables debates in
# that group only (global off wins everywhere). No per-group digest toggle
# (deferred to the digest-control feature) and no global digest toggle.
# =========================
FEATURES = (
    "chat", "pic", "tr", "tldr", "instagram", "youtube",
    "debate", "inline", "guest", "reminders", "relay",
)


def _feature_key(name):
    return f"feature.{name}"


def feature_enabled(name, chat_id=None):
    """True unless explicitly disabled. `chat_id` enables the per-group
    debate override. Any error → True (fail open)."""
    try:
        if get_setting(_feature_key(name), "1") == "0":
            return False
        if name == "debate" and chat_id is not None:
            try:
                if get_setting(f"chat:{int(chat_id)}:debate", "1") == "0":
                    return False
            except (TypeError, ValueError):
                pass
        return True
    except Exception:
        log.warning("feature flag read failed for %r; treating as enabled", name, exc_info=True)
        return True


def set_feature(name, enabled):
    set_setting(_feature_key(name), "1" if enabled else "0")


def set_chat_feature(chat_id, name, enabled):
    set_setting(f"chat:{int(chat_id)}:{name}", "1" if enabled else "0")


# =========================
# Clamped numeric settings (Stage 2B): ONLY these keys are panel-editable.
# Everything else from the Part-1 table is SKIPPED (restart-class or
# secrets-adjacent). Stored values are re-validated at every read: invalid
# or out-of-range → default + warning.
# =========================
# key -> (label, default, minimum, maximum)
SETTINGS_SPEC = {
    "ratelimit.chat.permin": ("Chat rate /user/min", 10, 1, 60),
    "ratelimit.pic.permin": ("Pic rate /user/min", 3, 1, 60),
    "ratelimit.vision.permin": ("Vision rate /user/min", 5, 1, 60),
    "tldr.cooldown_sec": ("TLDR cooldown (s, 0=off)", 300, 0, 3600),
    "tavily.daily_limit": ("Tavily daily cap", 40, 1, 1000),
    "tavily.max_results": ("Tavily max results", 5, 1, 10),
    "debate.delay_sec": ("Debate delay (s)", 4, 1, 60),
    "debate.max_rounds": ("Debate max rounds", 50, 1, 200),
    "digest.search_reserve": ("Digest search reserve", 10, 0, 20),
    "digest.max_profiles": ("Digest max profiles", 5, 1, 20),
}

# Per-user rate overrides live under limit.<uid>.<cat> keys; loaded as ONE
# cached query (never one lookup per message, never one cache entry per
# user). Categories mirror limits.py buckets.
LIMIT_CATEGORIES = ("chat", "pic", "vision", "inline", "instagram", "youtube")
LIMIT_OVERRIDE_TTL = 10.0

_limit_snapshot = ({}, 0.0)  # ({(uid, cat): per_min}, expires_at)
_limit_lock = threading.Lock()


def _list_limit_overrides():
    """All limit.* rows in ONE query: {(uid, cat): per_min} (validated)."""
    try:
        conn = repo._connect()
        rows = conn.execute(
            "SELECT key, value FROM panel_settings WHERE key LIKE 'limit.%'"
        ).fetchall()
    except Exception:
        log.warning("limit override read failed", exc_info=True)
        return {}
    out = {}
    for key, value in rows:
        try:
            _, uid_s, cat = str(key).split(".", 2)
            uid, per_min = int(uid_s), int(value)
            if uid > 0 and cat in LIMIT_CATEGORIES and 1 <= per_min <= 60:
                out[(uid, cat)] = per_min
            else:
                log.warning("ignoring out-of-range limit override %r=%r", key, value)
        except (ValueError, AttributeError):
            log.warning("ignoring malformed limit override %r", key)
    return out


def get_limit_overrides():
    global _limit_snapshot
    now = time.time()
    with _limit_lock:
        snapshot, expires = _limit_snapshot
        if now < expires:
            return snapshot
    fresh = _list_limit_overrides()
    with _limit_lock:
        _limit_snapshot = (fresh, now + LIMIT_OVERRIDE_TTL)
    return fresh


def invalidate_limit_snapshot():
    global _limit_snapshot
    with _limit_lock:
        _limit_snapshot = ({}, 0.0)


def get_clamped_int(key, default, minimum, maximum):
    """Read a numeric setting with hard min/max. Invalid/out-of-range
    stored values fall back to default + warning (fail open)."""
    try:
        raw = get_setting(key, None)
        if raw is None:
            return default
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        log.warning("setting %r has non-numeric value %r; using default %r", key, raw, default)
        return default
    if not (minimum <= value <= maximum):
        log.warning("setting %r value %r out of range [%d, %d]; using default %r",
                    key, value, minimum, maximum, default)
        return default
    return value


def validate_setting_value(key, raw):
    """UI-side validation: returns (ok, value_or_error). Out-of-range is
    REJECTED (not silently clamped) so typos can't surprise."""
    spec = SETTINGS_SPEC.get(key)
    if spec is None:
        return False, "unknown setting"
    _label, default, minimum, maximum = spec
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return False, f"not a number (allowed {minimum}–{maximum})"
    if not (minimum <= value <= maximum):
        return False, f"out of range (allowed {minimum}–{maximum})"
    return True, value


def setting_value(key):
    """Effective value of a SETTINGS_SPEC key: clamped, validated, fail-open
    to the hard-coded default. Use at every read site instead of the
    import-time constant."""
    _label, default, minimum, maximum = SETTINGS_SPEC[key]
    return get_clamped_int(key, default, minimum, maximum)


def write_setting(key, value):
    """Panel write path for runtime settings: persist + immediately sync
    in-process state when the key affects it (rate limits, Tavily budget,
    per-user overrides). Features/maintenance need no sync (read-through)."""
    set_setting(key, value)
    if (str(key).startswith("ratelimit.") or str(key) == "tavily.daily_limit"
            or str(key).startswith("limit.")):
        invalidate_limit_snapshot()
        sync_rate_limits()


def sync_rate_limits():
    """Push DB settings into in-process state: global per-category limits,
    per-user overrides (one query), Tavily budget cap. Called at startup
    and after every related panel write. Never raises."""
    try:
        from limits import rate_limiter

        for key, _cat in (("ratelimit.chat.permin", "chat"),
                          ("ratelimit.pic.permin", "pic"),
                          ("ratelimit.vision.permin", "vision")):
            _label, default, minimum, maximum = SETTINGS_SPEC[key]
            rate_limiter.set_global_override(_cat, get_clamped_int(key, default, minimum, maximum))
        rate_limiter.set_user_overrides(get_limit_overrides())
    except Exception:
        log.warning("rate limit sync failed", exc_info=True)
    try:
        from services.search import tavily_budget

        _label, default, minimum, maximum = SETTINGS_SPEC["tavily.daily_limit"]
        tavily_budget.set_limit(get_clamped_int("tavily.daily_limit", default, minimum, maximum))
    except Exception:
        log.warning("tavily budget sync failed", exc_info=True)


def reapply_runtime_settings():
    """Re-apply every DB-backed in-process override. Called once at startup
    (wiring.register_all) so a restart picks up panel-changed values."""
    clear_settings_cache()
    invalidate_limit_snapshot()
    sync_rate_limits()


# =========================
# Feature gate helpers for message/callback handlers (Stage 2B)
# =========================
# Semantics (approved Part 1 + 2B corrections):
# - Admins/owner EXEMPT (same as maintenance).
# - Explicit invocation (command typed, button tapped, link pasted in a
#   context that already posted status, inline/guest query) is ALWAYS
#   answered — never silent, never hanging.
# - Ambient auto-triggers (chat catch-all, debate phrase, group link pastes)
#   are SILENT in groups and get a RATE-LIMITED notice in private chat.
# Helpers return True to proceed, False when the disabled path was handled.
FEATURE_NOTICE_MINUTES = 10


def _feature_notice_ok(bot_instance, user_id, feature):
    key = (id(bot_instance), f"feat:{feature}", user_id)
    now = time.time()
    window = FEATURE_NOTICE_MINUTES * 60
    with _notice_lock:
        last = _notice_sent_at.get(key, 0)
        if now - last < window:
            return False
        _notice_sent_at[key] = now
        _prune_notice_map_locked(now)
        return True


def _feature_disabled_note(feature):
    return f"⏸ {feature} is temporarily disabled by the admins."


def feature_gate_message(bot_instance, message, feature, explicit, chat_id=None):
    """Message-handler gate. See semantics above."""
    try:
        user = getattr(message, "from_user", None)
        uid = getattr(user, "id", None)
        chat = getattr(message, "chat", None)
        private = getattr(chat, "type", None) == "private"
        if isinstance(uid, int) and not isinstance(uid, bool) and is_admin(uid):
            return True
        if feature_enabled(feature, chat_id=chat_id if chat_id is not None
                           else getattr(chat, "id", None)):
            return True
        if explicit or private:
            if explicit or _feature_notice_ok(bot_instance, uid, feature):
                try:
                    bot_instance.reply_to(message, _feature_disabled_note(feature))
                except Exception:
                    try:
                        bot_instance.send_message(chat.id, _feature_disabled_note(feature))
                    except Exception:
                        pass
        return False
    except Exception:
        log.warning("feature gate failed; allowing (fail open)", exc_info=True)
        return True


def feature_gate_callback(bot_instance, call, feature):
    """Callback gate: ack-dismiss with a 'disabled' alert. Admins exempt."""
    try:
        user = getattr(call, "from_user", None)
        uid = getattr(user, "id", None)
        if isinstance(uid, int) and not isinstance(uid, bool) and is_admin(uid):
            return True
        if feature_enabled(feature):
            return True
        try:
            bot_instance.answer_callback_query(call.id, "⏸ Disabled by the admins.", show_alert=True)
        except Exception:
            pass
        return False
    except Exception:
        log.warning("feature callback gate failed; allowing (fail open)", exc_info=True)
        return True
# =========================
# Reset tombstones (Stage 2C fix F1): uid -> reset epoch, 1h TTL.
# Background writers (fact extractor, behavior profiler, summarizer,
# in-flight streams) compare the START time of their unit of work against
# the reset time: work that began BEFORE a reset is dropped instead of
# resurrecting wiped rows. New messages after the reset save normally
# (their work starts after the tombstone time).
# =========================
_RESET_TOMBSTONE_TTL = 3600.0

_reset_tombstones = {}
_reset_tombstone_lock = threading.Lock()


def note_reset(user_id):
    """Record a reset. Called by the reset handler after a successful
    reset_user_data. Prunes expired entries."""
    try:
        now = time.time()
        with _reset_tombstone_lock:
            _reset_tombstones[int(user_id)] = now
            for uid, ts in list(_reset_tombstones.items()):
                if now - ts > _RESET_TOMBSTONE_TTL:
                    del _reset_tombstones[uid]
    except (TypeError, ValueError):
        pass


def tombstoned_after(user_id, ts):
    """True if uid was reset strictly after ts. Never raises."""
    try:
        with _reset_tombstone_lock:
            return _reset_tombstones.get(int(user_id), 0) > float(ts)
    except (TypeError, ValueError):
        return False


def _memory_owner_uid(memory_id):
    """Best-effort owner of a memory namespace, or None for shared ones
    (groups, debates, tldr markers). light_<negative> is a group → None."""
    s = str(memory_id)
    if s.isdigit():
        return int(s)
    for prefix in ("inline_light_", "inline_", "guest_light_", "guest_", "light_"):
        if s.startswith(prefix):
            rest = s[len(prefix):]
            return int(rest) if rest.isdigit() else None
    if "#" in s:
        head = s.split("#", 1)[0]
        return int(head) if head.isdigit() else None
    return None


def save_memory_guarded(memory_id, role, text, sender_uid=None, born=None):
    """repo.save_message wrapper that drops resurrection writes: if the
    sender was reset after this turn started, the save is skipped (logged).
    Missing sender/born → save normally (fail open). Returns True if saved."""
    try:
        if (sender_uid is not None and born is not None
                and tombstoned_after(sender_uid, born)):
            log.info("dropping post-reset %s save for uid %s", role, sender_uid)
            return False
        repo.save_message(memory_id, role, text)
        return True
    except Exception:
        log.warning("guarded save failed", exc_info=True)
        return False
# =========================
# Digest mode cache (one query, 10s TTL, fail open to all-standard).
# Consulted only in the digest path — never per user message.
# =========================
_DIGEST_MODES_TTL = 10.0

_digest_modes_snapshot = ({}, 0.0)
_digest_modes_lock = threading.Lock()


def get_digest_modes():
    """{chat_id: (mode, profile_id)}. Absent chat = standard."""
    global _digest_modes_snapshot
    now = time.time()
    with _digest_modes_lock:
        snapshot, expires = _digest_modes_snapshot
        if now < expires:
            return snapshot
    try:
        fresh = repo.get_digest_modes()
    except Exception:
        log.warning("digest modes read failed; all-standard fallback", exc_info=True)
        with _digest_modes_lock:
            old, _exp = _digest_modes_snapshot
            return old
    with _digest_modes_lock:
        _digest_modes_snapshot = (fresh, now + _DIGEST_MODES_TTL)
    return fresh


def invalidate_digest_modes():
    global _digest_modes_snapshot
    with _digest_modes_lock:
        _digest_modes_snapshot = ({}, 0.0)
# =========================
# Audit log (best-effort: never breaks the action it records)
# =========================
def audit(admin_id, action, target="", result=""):
    try:
        repo.write_panel_audit(admin_id, action, target, result)
    except Exception:
        log.warning("panel audit write failed (%s %s)", action, target, exc_info=True)


# =========================
# check_panel_access: pure permission logic for ALL panel entry points
# =========================
# The Telegram-facing decorator lives in handlers/panel.py (it needs the
# bot instance to answer callbacks/report errors; services must not import
# bots). This helper takes a Message or CallbackQuery and recomputes
# private-chat + admin (or owner) from scratch every time — callback data is
# never trusted for permission. Returns (allowed, chat, user_id).
def check_panel_access(message_or_call, owner_only=False):
    from config import OWNER_ID

    if hasattr(message_or_call, "data"):
        call = message_or_call
        user_id = getattr(getattr(call, "from_user", None), "id", None)
        chat = getattr(getattr(call, "message", None), "chat", None)
    else:
        user_id = getattr(getattr(message_or_call, "from_user", None), "id", None)
        chat = getattr(message_or_call, "chat", None)
    if not OWNER_ID:
        return False, chat, user_id  # panel disabled while no owner is configured
    if chat is None or getattr(chat, "type", None) != "private":
        return False, chat, user_id
    if owner_only:
        return bool(is_owner(user_id)), chat, user_id
    return bool(is_admin(user_id)), chat, user_id


# =========================
# Central gate: ban + maintenance for BOTH bots, ALL update types
# =========================
# Verified against installed pyTelegramBotAPI 4.36.0: TeleBot.polling /
# infinity_polling both funnel through the single TeleBot.process_new_updates
# method, and process_middlewares() return values are ignored (observational
# only — the existing function-style middleware CANNOT cancel dispatch).
# Wrapping process_new_updates per instance is therefore the one choke point.
#
# Side effect to know: dropped updates never reach process_middlewares, so
# banned/maintenance-held updates skip known_chats / chat_log /
# behavior_stats tracking entirely.
_UPDATE_USER_ATTRS = (
    "message",
    "edited_message",
    "callback_query",
    "inline_query",
    "chosen_inline_result",
    "guest_message",
)


def extract_gate_identity(update):
    """Return (user_id, chat_id_or_None, kind) for the sender-carrying update
    types in POLLING_ALLOWED_UPDATES, else None. message/edited/guest carry
    .from_user + .chat; callback carries .from (+.message.chat); inline and
    chosen_inline carry only a sender, no chat."""
    for kind in _UPDATE_USER_ATTRS:
        part = getattr(update, kind, None)
        if part is None:
            continue
        if kind in ("message", "edited_message", "guest_message"):
            user = getattr(part, "from_user", None)
            chat = getattr(part, "chat", None)
            uid = getattr(user, "id", None)
            return uid, getattr(chat, "id", None), kind
        if kind == "callback_query":
            user = getattr(part, "from_user", None) or getattr(part, "from", None)
            msg = getattr(part, "message", None)
            chat = getattr(msg, "chat", None) if msg else None
            return getattr(user, "id", None), getattr(chat, "id", None), kind
        user = getattr(part, "from_user", None) or getattr(part, "from", None)
        return getattr(user, "id", None), None, kind
    return None


_notice_sent_at = {}  # (id(bot), user_id) -> epoch of last maintenance notice
_notice_lock = threading.Lock()

_NOTICE_MAP_MAX = 5000  # hard bound; prune oldest down to _NOTICE_MAP_KEEP past it
_NOTICE_MAP_KEEP = 4000

# Global cap on concurrent notice sends: the per-user limiter bounds sends
# per user, but N distinct users in one batch would otherwise spawn N
# threads. Past the cap the notice is silently skipped (debug-logged).
_NOTICE_MAX_IN_FLIGHT = 8
_notice_semaphore = threading.BoundedSemaphore(_NOTICE_MAX_IN_FLIGHT)


def _prune_notice_map_locked(now):
    if len(_notice_sent_at) <= _NOTICE_MAP_MAX:
        return
    victims = sorted(_notice_sent_at, key=_notice_sent_at.get)[: len(_notice_sent_at) - _NOTICE_MAP_KEEP]
    for key in victims:
        del _notice_sent_at[key]
    log.debug("notice map pruned to %d entries", len(_notice_sent_at))


def _notice_rate_ok(bot_instance, user_id):
    window = DEFAULT_NOTICE_MINUTES * 60
    try:
        window = max(60, int(get_setting(MAINTENANCE_NOTICE_MINUTES_KEY, str(DEFAULT_NOTICE_MINUTES)))) * 60
    except (TypeError, ValueError):
        pass
    key = (id(bot_instance), user_id)
    now = time.time()
    with _notice_lock:
        last = _notice_sent_at.get(key, 0)
        if now - last < window:
            return False
        _notice_sent_at[key] = now
        _prune_notice_map_locked(now)
        return True


def _send_maintenance_notice(bot_instance, update, kind):
    """Best-effort, rate-limited, once per user per window.
    - message/edited/guest_message... : DM reply (message kinds).
    - callback_query: ack popup (already the behavior).
    - inline_query: empty answer + "under maintenance" button (an unanswered
      inline query leaves the client spinner hanging).
    - guest_message: EXACTLY ONE answer_guest_query text result (one-shot
      contract — silence would hang the mention AND burn the single chance).
    - chosen_inline_result: nothing to ack (no API exists) — held silently.
    Banned users never reach here (dropped with no ack of any kind).

    Runs on a daemon worker thread, NEVER on the polling thread (see
    _send_notice_async). Send failures are swallowed + logged."""
    try:
        if kind in ("message", "edited_message"):
            msg = getattr(update, kind)
            bot_instance.send_message(msg.chat.id, get_setting(MAINTENANCE_NOTICE_KEY, DEFAULT_MAINTENANCE_NOTICE))
        elif kind == "callback_query":
            bot_instance.answer_callback_query(update.callback_query.id, "🚧 Maintenance — try later")
        elif kind == "inline_query":
            iq = update.inline_query
            bot_instance.answer_inline_query(
                iq.id, [],
                button=tb_types.InlineQueryResultsButton(
                    text="🚧 Bot under maintenance", start_parameter="maintenance"),
            )
        elif kind == "guest_message":
            guest_msg = update.guest_message
            guest_query_id = getattr(guest_msg, "guest_query_id", None)
            if guest_query_id:
                result = tb_types.InlineQueryResultArticle(
                    id=hashlib.md5(guest_query_id.encode("utf-8")).hexdigest(),
                    title="🚧 Maintenance",
                    input_message_content=tb_types.InputTextMessageContent(
                        "🚧 Bot is temporarily under maintenance. Please try again later."),
                )
                bot_instance.answer_guest_query(guest_query_id, result)
    except Exception:
        log.debug("maintenance notice send failed", exc_info=True)


def _send_notice_async(bot_instance, update, kind):
    # Global cap first (non-blocking): skip silently past _NOTICE_MAX_IN_FLIGHT.
    if not _notice_semaphore.acquire(blocking=False):
        log.debug("notice concurrency cap reached; skipping notice")
        return

    def _run():
        try:
            _send_maintenance_notice(bot_instance, update, kind)
        finally:
            _notice_semaphore.release()

    worker = threading.Thread(
        target=_run,
        daemon=True,
        name="panel-gate-notice",
    )
    worker.start()


# =========================
# Throttled gate-error logging: one full traceback per error type per
# minute; repeats counted and reported on the next emission.
# =========================
_GATE_ERROR_WINDOW = 60.0
_gate_error_log = {}  # error_type -> [window_start, suppressed_count]
_gate_error_lock = threading.Lock()


def _log_gate_error_throttled(error_type):
    now = time.time()
    with _gate_error_lock:
        entry = _gate_error_log.get(error_type)
        if entry is None or now - entry[0] >= _GATE_ERROR_WINDOW:
            suppressed = entry[1] if entry is not None else 0
            _gate_error_log[error_type] = [now, 0]
            emit = True
        else:
            entry[1] += 1
            emit = False
    if emit:
        if suppressed:
            log.warning("gate: %d similar %r failures suppressed in the last minute", suppressed, error_type)
        log.warning("gate failed on one update; passing it through (%s)", error_type, exc_info=True)


def _banned_lookup(user_id):
    """Cached bans-set membership. No DB reads with a warm cache; DB errors
    inside refresh serve stale/empty (fail open)."""
    try:
        return int(user_id) in get_cached_ban_ids()
    except Exception:
        log.warning("panel ban lookup failed; treating as not banned", exc_info=True)
        return False


def _gate_decision(update):
    """pass | drop | notice (+ uid). Admins always pass (and can never be
    banned). Banned users are dropped with no reply at all.

    Hot-path cost per update (see risk review): tuple scan + at most TWO
    indexed PK SELECTs (panel_admins, panel_bans) for non-env users, ZERO
    for env-listed admins; settings reads come from the 10s cache when warm
    (one SELECT per key per 10s worst case). No network, no locks held
    across IO."""
    ident = extract_gate_identity(update)
    if ident is None:
        return "pass", None
    uid, _chat_id, kind = ident
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        return "pass", uid
    if is_admin(uid):
        return "pass", uid
    if _banned_lookup(uid):
        return "drop", uid
    if maintenance_active():
        return "notice", uid
    return "pass", uid


def install_central_gate(bot_instance):
    """Wrap one TeleBot instance's process_new_updates with the ban +
    maintenance gate. Idempotent; call once per instance at startup from
    wiring.register_all(). Incoming-only: schedulers, digest, backup and
    relay threads are unaffected (Stage 3 broadcast runner must separately
    skip banned recipients)."""
    orig = bot_instance.process_new_updates
    if getattr(orig, "_panel_gate_installed", False):
        return

    def gated(updates):
        # Failure isolation: the ENTIRE per-update body is inside try/except.
        # If gate code raises on any update (hostile shape, DB error,
        # unhashable uid, cache bug), that update is dispatched NORMALLY
        # (fail open, traceback logged) and the rest of the batch is
        # unaffected — one bad update can never block its siblings.
        kept = []
        for update in updates:
            try:
                decision, uid = _gate_decision(update)
                if decision == "pass":
                    kept.append(update)
                elif decision == "notice":
                    try:
                        kind = next(
                            k for k in _UPDATE_USER_ATTRS if getattr(update, k, None) is not None
                        )
                    except StopIteration:
                        kind = "message"
                    if _notice_rate_ok(bot_instance, uid):
                        _send_notice_async(bot_instance, update, kind)
                    # held: never reaches dispatch
                # "drop": silent, nothing sent
            except Exception as e:
                _log_gate_error_throttled(type(e).__name__)
                kept.append(update)
        if kept:
            orig(kept)

    gated._panel_gate_installed = True
    bot_instance.process_new_updates = gated
