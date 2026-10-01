import logging
import re
import threading
import time

from telebot import apihelper

from ai import agnes
from bots import bot
from config import STREAM_EDIT_INTERVAL
from limits import rate_limiter
from metrics import METRICS

log = logging.getLogger("L-bot")


# =========================
# Helper Functions
# =========================
def split_text(text, max_length=4000):
    if not text:
        return []
    return [text[i : i + max_length] for i in range(0, len(text), max_length)]

def escape_markdown(text):
    """Escape Telegram legacy-Markdown special chars in user-supplied text.
    Without this, a reminder/todo containing _ * ` or [ makes sendMessage
    return 400 'Can't parse entities', which — inside the scheduler's
    send-then-mark retry loop — eventually drops the reminder entirely."""
    if not text:
        return ""
    return re.sub(r"([_*`\[\]])", r"\\\1", str(text))

def _safe_reply_to(bot_instance, message, text, **kwargs):
    """Like bot_instance.reply_to(message, text, **kwargs), but doesn't crash
    the whole handler if the message being replied to was deleted in the
    meantime (race condition: user/another bot deletes their message between
    Telegram delivering the update and us calling reply_to — Telegram then
    returns 400 'message to be replied not found'). On that specific error we
    retry as a plain send_message (no reply link) instead of raising. Any
    other Telegram API error is re-raised unchanged so existing except
    blocks/logging still catch real problems."""
    try:
        return bot_instance.reply_to(message, text, **kwargs)
    except apihelper.ApiTelegramException as e:
        if "message to be replied not found" in str(e).lower():
            log.info("reply_to target missing (chat %s) — falling back to send_message", message.chat.id)
            return bot_instance.send_message(message.chat.id, text, **kwargs)
        raise

# =========================
# Streaming helper
# =========================
def stream_to_telegram(message, messages, placeholder_id, request_entry=None):
    """Wrapper for the L bot. Returns (final_full_text, status_error, msg_ids)."""
    return stream_to_telegram_generic(bot, message, messages, placeholder_id, request_entry=request_entry)

def finalize_message(message, placeholder_id, full_text, msg_ids=None):
    """Wrapper for the L bot."""
    finalize_message_generic(bot, message, placeholder_id, full_text, msg_ids)

# =========================
# Generic Streaming/Finalize (works with any bot instance)
# =========================
# In-flight request registry for /stop: key = (chat_id, user_id) -> cancel
# flag. The streaming loop checks it every chunk; /stop sets the flag, the
# stream breaks, and the half-finished turn is discarded (user's message is
# removed from memory so an aborted turn doesn't pollute the session).
_active_requests = {}          # (chat_id, user_id) -> latest in-flight entry

_all_inflight_ids = set()      # ids of every live entry (lets group /stop find ANY run, not just the latest)

_active_requests_lock = threading.Lock()

def _register_active_request(chat_id, user_id, placeholder_id):
    """Register an in-flight request and return its UNIQUE entry dict.

    The old code stored plain dicts keyed by (chat_id, user_id): a second
    message from the same user overwrote the slot, so the first request's
    _consume_active_request() popped the SECOND request's registration —
    making it unstoppable and letting /stop cancel the wrong run. Callers
    now pass the returned entry through to stream_to_telegram_generic /
    _consume_active_request so cancellation always targets the right run."""
    key = (int(chat_id), int(user_id))
    entry = {"cancel": False, "placeholder_id": int(placeholder_id), "_key": key}
    with _active_requests_lock:
        _active_requests[key] = entry
        _all_inflight_ids.add(id(entry))
    return entry

def _cancel_active_request(chat_id, user_id):
    """Set the cancel flag on the user's most recent in-flight request, if any.
    Returns the placeholder_id being streamed to, or None if idle."""
    with _active_requests_lock:
        entry = _active_requests.get((int(chat_id), int(user_id)))
    if entry:
        entry["cancel"] = True
        return entry.get("placeholder_id")
    return None

def _consume_active_request(entry):
    """Consume a specific request entry (the one returned by
    _register_active_request). Removes it from the registry only if it is
    still the current entry for its key. Returns whether cancellation was
    requested."""
    if entry is None:
        return False
    with _active_requests_lock:
        key = entry.get("_key")
        if key and _active_requests.get(key) is entry:
            _active_requests.pop(key, None)
        _all_inflight_ids.discard(id(entry))
    return bool(entry["cancel"])

def _is_group_admin(bot_instance, chat_id, user_id):
    """True if user_id is an administrator/creator of the group chat."""
    try:
        status = bot_instance.get_chat_member(chat_id, user_id).status
        return status in ("administrator", "creator")
    except Exception:
        return False

def _handle_stop_generic(bot_instance, message):
    """Shared /stop logic for both bots.

    Private chat: a plain /stop cancels whatever this user has in flight —
    no reply needed. Groups: the /stop MUST be a reply to the bot's
    "در حال پردازش..." placeholder so it's unambiguous WHICH run gets
    stopped; only that run's owner (or a group admin) may stop it. Nothing
    in flight → friendly no-op message.
    """
    try:
        chat_id = message.chat.id
        user_id = message.from_user.id

        if message.chat.type == "private":
            # Private — no reply needed; stop whatever this user has running.
            if _cancel_active_request(chat_id, user_id):
                _safe_reply_to(bot_instance, message, "⏹ متوقف شد.")
            else:
                _safe_reply_to(bot_instance, message, "چیزی در حال پردازش نیست.")
            return

        # Group path: identify the run via the replied-to placeholder.
        reply = message.reply_to_message
        if not reply:
            _safe_reply_to(
                bot_instance, message,
                "⚠️ برای توقف پردازش، روی پیام «در حال پردازش...» ربات ریپلای بزن و `/stop` رو بفرست.",
                parse_mode="Markdown",
            )
            return

        target_key = None
        target_entry = None
        # Search ALL live in-flight entries, not just the latest per user —
        # several group members (or even one user with overlapping runs) may
        # have requests in flight simultaneously. _active_requests itself is
        # the source of truth; its values are the live entry dicts.
        with _active_requests_lock:
            for entry in list(_active_requests.values()):
                key = entry.get("_key")
                if key and key[0] == int(chat_id) and entry.get("placeholder_id") == int(reply.message_id):
                    target_key = key
                    target_entry = entry
                    break
        if not target_entry:
            _safe_reply_to(
                bot_instance, message,
                "این پیام «در حال پردازش» فعالی پیدا نشد — شاید تموم شده باشه.",
            )
            return

        # Only the run's owner (or a group admin) may stop it.
        if user_id != target_key[1] and not _is_group_admin(bot_instance, chat_id, user_id):
            _safe_reply_to(
                bot_instance, message,
                "⛔ این پردازش متعلق به شما نیست؛ فقط صاحبش یا مدیر گروه می‌تونه متوقفش کنه.",
            )
            return

        target_entry["cancel"] = True
        _safe_reply_to(bot_instance, message, "⏹ پردازش متوقف شد.")
    except Exception:
        log.error("STOP COMMAND ERROR", exc_info=True)
        try:
            _safe_reply_to(bot_instance, message, "❌ خطا در توقف پردازش.")
        except Exception:
            pass

def stream_to_telegram_generic(bot_instance, message, messages, placeholder_id, request_entry=None):
    """Consume the streaming LLM response and live-edit the Telegram message.
    Works with any telebot instance.

    `request_entry` is the registration returned by _register_active_request —
    the streaming loop checks THIS entry's cancel flag (not whatever happens
    to be the latest entry for this user), so an overlapping second message
    can't hijack or disable /stop for the first one.

    Returns (final_full_text, status_error, message_ids) where message_ids is a
    list of all (chat_id, message_id) tuples that were used during streaming.
    The caller passes this list to finalize_message_generic so continuation
    messages are updated in-place instead of duplicated.
    """
    full = ""
    last_edit = 0.0
    last_rendered = ""
    got_status_error = None
    interrupted = False
    stopped = False
    MAX_MSG = 3800
    reply_id = message.message_id
    msg_ids = [(message.chat.id, placeholder_id)]

    def _segment_start():
        return (len(msg_ids) - 1) * MAX_MSG

    try:
        for kind, value in agnes.stream_chat(messages):
            if kind == "status":
                got_status_error = value
                break
            if kind == "error":
                interrupted = True
                break

            if (request_entry or _active_requests.get((message.chat.id, message.from_user.id), {})).get("cancel"):
                # /stop arrived: full=="" → nothing streamed, abort clean.
                # Otherwise partial text is returned; the caller decides.
                if not full.strip():
                    return None, "stopped", msg_ids
                interrupted = True
                stopped = True
                break

            full += value
            now = time.time()

            if not full.strip():
                continue

            while len(full) - _segment_start() >= MAX_MSG:
                seg_start = _segment_start()
                try:
                    prev = msg_ids[-1]
                    batch_text = full[seg_start:seg_start + MAX_MSG].rstrip()
                    bot_instance.edit_message_text(
                        text=batch_text,
                        chat_id=message.chat.id,
                        message_id=prev[1],
                    )
                except Exception:
                    pass
                try:
                    continuation = bot_instance.send_message(
                        message.chat.id,
                        "▌ ادامه ...",
                        reply_to_message_id=reply_id,
                    )
                    msg_ids.append((message.chat.id, continuation.message_id))
                except Exception:
                    break

            if now - last_edit >= STREAM_EDIT_INTERVAL and full != last_rendered:
                try:
                    display = full[_segment_start():] + " ▌"
                    bot_instance.edit_message_text(
                        text=display,
                        chat_id=message.chat.id,
                        message_id=msg_ids[-1][1],
                    )
                    last_rendered = full
                    last_edit = now
                except Exception:
                    last_edit = now
    except Exception:
        log.error("stream_to_telegram_generic: unexpected error mid-stream", exc_info=True)
        interrupted = True

    if got_status_error is not None and not full.strip():
        return None, got_status_error, msg_ids

    if interrupted:
        if stopped:
            # User-issued /stop — return the partial text with a distinct
            # status so the caller knows NOT to append the "connection
            # dropped" disclaimer and can clean up memory.
            return (full or None), "stopped", msg_ids
        if full.strip():
            full = full.rstrip() + (
                "\n\n⚠️ _اتصال قطع شد؛ ممکن است پاسخ ناقص باشد. در صورت نیاز دوباره بپرسید._"
            )
        else:
            return None, "network_error", msg_ids

    return full, None, msg_ids

def finalize_message_generic(bot_instance, message, placeholder_id, full_text, msg_ids=None):
    """Render the final text, splitting into multiple messages if needed.
    Works with any telebot instance."""
    parts = split_text(full_text, max_length=3800)
    if not parts:
        parts = ["داده‌ای برای تحلیل یافت نشد."]

    if msg_ids:
        for i, (chat_id, msg_id) in enumerate(msg_ids):
            text = parts[i] if i < len(parts) else "—"
            try:
                bot_instance.edit_message_text(
                    text, chat_id=chat_id, message_id=msg_id, parse_mode="Markdown",
                )
            except Exception:
                try:
                    bot_instance.edit_message_text(text, chat_id=chat_id, message_id=msg_id)
                except Exception:
                    pass
        for part in parts[len(msg_ids):]:
            try:
                bot_instance.send_message(message.chat.id, part, parse_mode="Markdown")
            except Exception:
                bot_instance.send_message(message.chat.id, part)
        return

    first = parts[0]
    try:
        bot_instance.edit_message_text(
            first, chat_id=message.chat.id, message_id=placeholder_id, parse_mode="Markdown",
        )
    except Exception:
        try:
            bot_instance.edit_message_text(first, chat_id=message.chat.id, message_id=placeholder_id)
        except Exception:
            pass

    for part in parts[1:]:
        try:
            bot_instance.send_message(message.chat.id, part, parse_mode="Markdown")
        except Exception:
            bot_instance.send_message(message.chat.id, part)


def sanitize_backtick_span(text):
    """For user text placed inside legacy-Markdown `code spans`: an embedded
    backtick terminates the span and anything after it gets parsed as
    entities (400 on stray _ * [). Inside a code span backslashes are NOT
    escapes, so the only fix is to strip the backticks themselves."""
    return str(text or "").replace("`", "'")


def rate_limited_reply(message, category):
    """Check rate limit; reply + return False if blocked."""
    allowed, scope = rate_limiter.allow(category, message.from_user.id)
    if not allowed:
        METRICS["ratelimited"] += 1
        if scope == "global":
            _safe_reply_to(bot, message, "سرور تحت فشار ترافیکی است. لطفاً کمی بعد تلاش کنید.")
        else:
            _safe_reply_to(bot, 
                message, "تعداد درخواستهای شما بیش از حد مجاز است. کمی صبر کنید."
            )
        return False
    return True
