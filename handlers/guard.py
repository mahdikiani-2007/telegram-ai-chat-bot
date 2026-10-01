import logging
import re
import time

from bots import bot, bot_light, get_bot_identity, get_bot_light_identity
from db import repo
from handlers.shared import extract_text_from_message
from timeutil import iran_now

log = logging.getLogger("L-bot")


# =========================
# Multi-bot-group fix: ignore commands addressed to a DIFFERENT bot
# =========================
# pyTelegramBotAPI's `commands=[...]` filter matches "/start" and
# "/start@SomeOtherBot" identically — it strips the @username suffix
# without checking it belongs to THIS bot. In a group with more than one
# bot, that means every command meant for another bot also triggers ours.
# Fix: register a catch-all handler BEFORE any real command handler that
# recognizes "this command is explicitly addressed to someone else" and
# does nothing — telebot's dispatch stops at the first matching handler
# (verified earlier against the actual library source), so this silently
# absorbs the update before /start, /tr, /todo, etc. ever see it. Commands
# with no @username at all (the normal case) are unaffected.
def _is_command_for_another_bot(message, own_username):
    if not message.text or not message.text.startswith("/"):
        return False
    first_token = message.text.split()[0]
    if "@" not in first_token:
        return False
    mentioned_username = first_token.split("@", 1)[1].lower()
    return bool(own_username) and mentioned_username != own_username

@bot.message_handler(func=lambda m: _is_command_for_another_bot(m, get_bot_identity().get("username")))
def _ignore_foreign_bot_commands(message):
    return  # swallow on purpose — see module note above

if bot_light:
    @bot_light.message_handler(func=lambda m: _is_command_for_another_bot(m, get_bot_light_identity().get("username")))
    def _ignore_foreign_bot_commands_light(message):
        return  # swallow on purpose — see module note above

def _collect_behavior_signals(message, chat_id_str, user):
    """Zero-LLM-cost behavior signal collection, called from the middleware
    for every group message. Cheap heuristics only (no model calls): counts,
    flags, and a few capped sample lines. The nightly analyzer turns this
    into a prose behavioral profile."""
    try:
        if user.is_bot:
            return
        text = extract_text_from_message(message) or ""
        text_stripped = text.strip()
        if message.content_type not in ("text", "photo", "video", "sticker", "voice"):
            return

        is_media = message.content_type != "text"
        has_link = bool(text_stripped) and bool(
            re.search(r"https?://|t\.me/|www\.", text_stripped, re.IGNORECASE))
        is_question = bool(text_stripped) and (
            text_stripped.endswith("؟") or text_stripped.endswith("?")
            or re.match(r"^(چرا|چی|کجاست|کی|چطور|چگونه|چه|آیا|کدوم|کدوم|who|what|why|how|where|when)\b",
                        text_stripped, re.IGNORECASE) is not None)
        sample = text_stripped[:80] if (text_stripped and len(text_stripped) >= 8
                                        and not text_stripped.startswith("/")) else ""

        repo.record_behavior(
            chat_id_str, user.id,
            msg_len=len(text_stripped),
            is_reply=bool(message.reply_to_message),
            has_link=has_link,
            is_question=is_question,
            is_media=is_media,
            sample_line=sample,
            hour=iran_now().hour,
        )
    except Exception:
        pass  # telemetry must never break message handling

def _log_group_chatter(message, chat_id_str, user):
    """Persist every group text message (bots/commands excluded) into the
    chat_log buffer. This is what lets the nightly profiler read the WHOLE
    day's conversation and /tldr summarize real group chatter. Best-effort —
    a logging failure must never surface to users."""
    try:
        if message.content_type == "text":
            text = (message.text or "").strip()
            if not text or text.startswith("/"):
                return
            if message.text and message.text.startswith("/"):
                return
            repo.log_group_chat(chat_id_str, user.id,
                                message.from_user.first_name or "", text)
        elif message.content_type in ("photo", "video", "sticker", "voice", "animation"):
            # Media noted as a marker so participation still shows up.
            cap = message.caption or f"[{message.content_type}]"
            repo.log_group_chat(chat_id_str, user.id,
                                message.from_user.first_name or "", cap[:200])
    except Exception:
        pass

def _track_known_chat_and_member(message):
    """Upserts this chat into known_chats, and — if it's a group message —
    upserts the sender into group_members. This is what powers real
    broadcast targeting (replacing the old mock DB) and the "tag everyone"
    reminder workaround. Runs via telebot middleware on every update, so it
    fires even for messages the bot doesn't respond to.

    Caveat: Telegram's Bot API never hands a bot a full member list without
    extra permissions, so "known members" here means "users this bot has
    personally observed messaging in this chat" — not the full roster.
    """
    try:
        chat = message.chat
        user = message.from_user
        title = getattr(chat, "title", None)
        repo.upsert_known_chat(str(chat.id), chat.type, chat.username, title)
        if chat.type in ("group", "supergroup") and user:
            repo.upsert_group_member(str(chat.id), user.id, user.username, user.first_name)
            _collect_behavior_signals(message, str(chat.id), user)
            # Full-chatter log for the nightly profiler + /tldr (bots and
            # commands excluded; text messages only, media counted as a note).
            if not user.is_bot:
                _log_group_chatter(message, str(chat.id), user)
    except Exception:
        log.debug("chat tracking failed", exc_info=True)

@bot.middleware_handler(update_types=["message"])
def _track_chat_middleware(bot_instance, message):
    _track_known_chat_and_member(message)

if bot_light:
    @bot_light.middleware_handler(update_types=["message"])
    def _track_chat_middleware_light(bot_instance, message):
        _track_known_chat_and_member(message)
