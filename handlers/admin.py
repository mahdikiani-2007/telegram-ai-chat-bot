import datetime
import logging
import time
import traceback

from telebot import types

from bots import admin_notifier, bot
from config import ADMIN_ID
from db import fetch_all_group_ids, fetch_all_user_ids, repo
from handlers.shared import is_admin
from metrics import METRICS, _START_TIME
from services.debate import _active_debates, _debate_lock, stop_all_debates
from services.digest import broadcast_daily_digest
from services.streaming import _safe_reply_to, split_text
from sessions import _admin_broadcast_sessions

log = logging.getLogger("L-bot")


def get_admin_keyboard():
    """Build and return the admin panel ReplyKeyboardMarkup."""
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    keyboard.row(
        types.KeyboardButton("👥 Broadcast to Groups"),
        types.KeyboardButton("👤 Broadcast to Users"),
    )
    keyboard.row(
        types.KeyboardButton("🔄 Broadcast to Both"),
        types.KeyboardButton("🎯 Send to Specific Chat"),
    )
    keyboard.row(
        types.KeyboardButton("📋 Group List"),
    )
    return keyboard

def broadcast_message(source_chat_id: int, source_message_id: int, target_chat_ids: list, label: str) -> str:
    """Copy `source_message_id` from `source_chat_id` to every chat in
    `target_chat_ids`. Returns a human-readable status report string."""
    sent, failed = 0, 0
    errors = []

    for chat_id in target_chat_ids:
        try:
            bot.copy_message(
                chat_id=chat_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
            )
            sent += 1
        except Exception as e:
            failed += 1
            error_msg = f"Chat {chat_id}: {e}"
            errors.append(error_msg)
            log.warning("Broadcast failed for %s — %s", chat_id, e)

        time.sleep(0.05)  # respect Telegram's rate limits

    report = (
        f"📢 Broadcast to {label} completed.\n\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}"
    )
    if errors:
        error_preview = "\n".join(errors[:5])
        report += f"\n\nErrors:\n{error_preview}"
    return report

@bot.message_handler(commands=["admin"])
def handle_admin(message):
    """Show the admin panel keyboard if the sender is the admin."""
    try:
        if not is_admin(message.from_user.id):
            _safe_reply_to(bot, message, "⛔ You are not authorized to use this command.")
            return

        welcome = (
            "🔧 **Admin Panel**\n\n"
            "Select an action from the keyboard below:\n\n"
            "👥 **Broadcast to Groups** — Send a message to all groups\n"
            "👤 **Broadcast to Users** — Send a message to all users\n"
            "🔄 **Broadcast to Both** — Send to groups + users\n"
            "🎯 **Send to Specific Chat** — Send to a single chat ID\n\n"
            "Use /stats to see bot metrics."
        )
        bot.send_message(
            message.chat.id,
            welcome,
            parse_mode="Markdown",
            reply_markup=get_admin_keyboard(),
        )
    except Exception:
        log.error("ADMIN PANEL ERROR", exc_info=True)
        _safe_reply_to(bot, message, "❌ An error occurred while opening the admin panel.")

@bot.message_handler(func=lambda m: m.text == "❌ Cancel")
def handle_admin_cancel(message):
    """Allow the admin to cancel an ongoing broadcast flow."""
    try:
        if not is_admin(message.from_user.id):
            return
        _admin_broadcast_sessions.discard(message.from_user.id)
        bot.send_message(
            message.chat.id,
            "❎ Action cancelled.",
            reply_markup=get_admin_keyboard(),
        )
    except Exception:
        log.error("ADMIN CANCEL ERROR", exc_info=True)

# --------------------------------------------------------------------------
# Broadcast flow — migrated OFF register_next_step_handler (which does not
# suppress normal handler dispatch) and onto the same TTL-session +
# "intercept before handle_all_messages" pattern already used for
# todo/reminder text input. This is the Phase-2 fix for admin broadcast text
# bleeding into the LLM chat history.
# --------------------------------------------------------------------------
ADMIN_BROADCAST_CONTENT_TYPES = [
    "text", "photo", "video", "document", "audio", "voice",
    "video_note", "animation", "sticker", "contact", "location", "poll",
]

@bot.message_handler(func=lambda m: m.text == "👥 Broadcast to Groups")
def handle_broadcast_groups(message):
    try:
        if not is_admin(message.from_user.id):
            return
        cancel_kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        cancel_kb.add(types.KeyboardButton("❌ Cancel"))
        bot.send_message(
            message.chat.id,
            "👥 **Broadcast to Groups**\n\n"
            "Send or forward the message you want to broadcast to all groups.\n"
            "You can send text, photo, video, document, or any media.",
            parse_mode="Markdown",
            reply_markup=cancel_kb,
        )
        _admin_broadcast_sessions.set(message.from_user.id, {"stage": "groups"})
    except Exception:
        log.error("BROADCAST GROUPS STEP 1 ERROR", exc_info=True)
        _safe_reply_to(bot, message, "❌ An error occurred.")

@bot.message_handler(func=lambda m: m.text == "👤 Broadcast to Users")
def handle_broadcast_users(message):
    try:
        if not is_admin(message.from_user.id):
            return
        cancel_kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        cancel_kb.add(types.KeyboardButton("❌ Cancel"))
        bot.send_message(
            message.chat.id,
            "👤 **Broadcast to Users**\n\n"
            "Send or forward the message you want to broadcast to all users.\n"
            "You can send text, photo, video, document, or any media.",
            parse_mode="Markdown",
            reply_markup=cancel_kb,
        )
        _admin_broadcast_sessions.set(message.from_user.id, {"stage": "users"})
    except Exception:
        log.error("BROADCAST USERS STEP 1 ERROR", exc_info=True)
        _safe_reply_to(bot, message, "❌ An error occurred.")

@bot.message_handler(func=lambda m: m.text == "🔄 Broadcast to Both")
def handle_broadcast_both(message):
    try:
        if not is_admin(message.from_user.id):
            return
        cancel_kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        cancel_kb.add(types.KeyboardButton("❌ Cancel"))
        bot.send_message(
            message.chat.id,
            "🔄 **Broadcast to Both**\n\n"
            "Send or forward the message you want to broadcast to all groups AND users.\n"
            "You can send text, photo, video, document, or any media.",
            parse_mode="Markdown",
            reply_markup=cancel_kb,
        )
        _admin_broadcast_sessions.set(message.from_user.id, {"stage": "both"})
    except Exception:
        log.error("BROADCAST BOTH STEP 1 ERROR", exc_info=True)
        _safe_reply_to(bot, message, "❌ An error occurred.")

@bot.message_handler(func=lambda m: m.text == "🎯 Send to Specific Chat")
def handle_send_specific(message):
    try:
        if not is_admin(message.from_user.id):
            return
        cancel_kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        cancel_kb.add(types.KeyboardButton("❌ Cancel"))
        bot.send_message(
            message.chat.id,
            "🎯 **Send to Specific Chat**\n\n"
            "Step 1/2: Enter the target **Chat ID**\n\n"
            "Examples:\n"
            "• User: `123456789`\n"
            "• Group: `-1001234567890`\n"
            "• Channel: `-1009876543210`",
            parse_mode="Markdown",
            reply_markup=cancel_kb,
        )
        _admin_broadcast_sessions.set(message.from_user.id, {"stage": "specific_id"})
    except Exception:
        log.error("SPECIFIC CHAT STEP 1 ERROR", exc_info=True)
        _safe_reply_to(bot, message, "❌ An error occurred.")

def _build_group_list_text():
    """Render every known group as 'name — numeric id' lines for easy
    copy-paste into the Send-to-Specific-Chat flow. Sorted by most recent
    activity. Returns (text, count)."""
    rows = repo.get_known_chats_with_titles(("group", "supergroup"))
    if not rows:
        return "هیچ گروهی ثبت نشده — ربات هنوز توی گروهی پیامی ندیده.", 0
    lines = []
    for chat_id, title, username, _last_seen in rows:
        name = title.strip() or (f"@{username}" if username else "بدون عنوان")
        lines.append(f"• {name} — `{chat_id}`")
    header = f"📋 **گروه‌های ثبت‌شده ({len(rows)} مورد):**\n\n"
    return header + "\n".join(lines), len(rows)

@bot.message_handler(func=lambda m: m.text == "📋 Group List")
def handle_group_list(message):
    """Admin-only: list every group the bot is in, as 'name — numeric id'
    for easy copying into the send-to-specific-chat flow."""
    try:
        if not is_admin(message.from_user.id):
            return
        text, count = _build_group_list_text()
        if count == 0:
            bot.send_message(message.chat.id, text, reply_markup=get_admin_keyboard())
            return
        # Split safely under Telegram's 4096-char limit for big group lists.
        chunks = split_text(text, max_length=3800)
        for i, chunk in enumerate(chunks):
            bot.send_message(
                message.chat.id, chunk, parse_mode="Markdown",
                reply_markup=get_admin_keyboard() if i == len(chunks) - 1 else None,
            )
    except Exception:
        log.error("GROUP LIST ERROR", exc_info=True)
        _safe_reply_to(bot, message, "❌ خطا در دریافت لیست گروه‌ها.")

def _run_broadcast(message, target_ids, label):
    if not target_ids:
        bot.send_message(
            message.chat.id,
            f"⚠️ هیچ {label} ای در دیتابیس یافت نشد. Broadcast لغو شد.",
            reply_markup=get_admin_keyboard(),
        )
        return
    bot.send_message(message.chat.id, f"📡 Broadcasting to {len(target_ids)} chat(s)... Please wait.")
    report = broadcast_message(message.chat.id, message.message_id, target_ids, label)
    bot.send_message(message.chat.id, report, reply_markup=get_admin_keyboard())

def _run_specific_send(message, target_chat_id):
    try:
        bot.copy_message(chat_id=target_chat_id, from_chat_id=message.chat.id, message_id=message.message_id)
        bot.send_message(
            message.chat.id,
            f"✅ Message successfully sent to `{target_chat_id}`.",
            parse_mode="Markdown",
            reply_markup=get_admin_keyboard(),
        )
    except Exception as e:
        error_detail = str(e)
        log.error("SPECIFIC SEND FAILED for %s — %s", target_chat_id, error_detail)
        bot.send_message(
            message.chat.id,
            f"❌ Failed to send message to `{target_chat_id}`.\n\nError: `{error_detail}`",
            parse_mode="Markdown",
            reply_markup=get_admin_keyboard(),
        )

def _is_admin_broadcast_waiting(message):
    if not message.from_user or not is_admin(message.from_user.id):
        return False
    if message.text == "❌ Cancel":
        return False  # handled by the dedicated Cancel handler above
    # Slash-commands always route to their real handlers instead — typing
    # /stats mid-flow used to copy "/stats" to every known chat. (Note:
    # _specific_id stage explicitly accepts numeric input, unaffected.)
    if message.text and message.text.startswith("/"):
        return False
    return _admin_broadcast_sessions.get(message.from_user.id) is not None

@bot.message_handler(func=_is_admin_broadcast_waiting, content_types=ADMIN_BROADCAST_CONTENT_TYPES)
def handle_admin_broadcast_input(message):
    """Intercepts the admin's next message while a broadcast flow is pending —
    registered ahead of handle_all_messages, so telebot's first-match
    dispatch consumes it here and it never reaches the LLM chat handler."""
    try:
        user_id = message.from_user.id
        session = _admin_broadcast_sessions.get(user_id)
        if not session:
            return
        stage = session["stage"]

        if stage == "specific_id":
            if message.content_type != "text":
                _safe_reply_to(bot, message, "❌ لطفاً یک عدد به‌عنوان Chat ID ارسال کنید.")
                return
            raw_id = message.text.strip()
            try:
                target_chat_id = int(raw_id)
            except ValueError:
                _safe_reply_to(bot, 
                    message,
                    f"❌ `{raw_id}` معتبر نیست. یک عدد وارد کنید یا ❌ Cancel را بزنید.",
                    parse_mode="Markdown",
                )
                return
            _admin_broadcast_sessions.set(user_id, {"stage": "specific_msg", "target_chat_id": target_chat_id})
            cancel_kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            cancel_kb.add(types.KeyboardButton("❌ Cancel"))
            bot.send_message(
                message.chat.id,
                f"✅ Target Chat ID: `{target_chat_id}`\n\n"
                "Step 2/2: Now send or forward the message you want to deliver to this chat.\n"
                "You can send text, photo, video, document, or any media.",
                parse_mode="Markdown",
                reply_markup=cancel_kb,
            )
            return

        _admin_broadcast_sessions.discard(user_id)

        if stage == "groups":
            _run_broadcast(message, fetch_all_group_ids(), "Groups")
        elif stage == "users":
            _run_broadcast(message, fetch_all_user_ids(), "Users")
        elif stage == "both":
            group_ids, user_ids = fetch_all_group_ids(), fetch_all_user_ids()
            all_ids = list(set(group_ids + user_ids))
            if not all_ids:
                bot.send_message(
                    message.chat.id,
                    "⚠️ هیچ گروه یا کاربری در دیتابیس یافت نشد. Broadcast لغو شد.",
                    reply_markup=get_admin_keyboard(),
                )
                return
            bot.send_message(
                message.chat.id,
                f"📡 Broadcasting to {len(all_ids)} chat(s) "
                f"({len(group_ids)} groups + {len(user_ids)} users)... Please wait.",
            )
            report = broadcast_message(message.chat.id, message.message_id, all_ids, "Groups + Users")
            bot.send_message(message.chat.id, report, reply_markup=get_admin_keyboard())
        elif stage == "specific_msg":
            _run_specific_send(message, session["target_chat_id"])
    except Exception:
        log.error("ADMIN BROADCAST INPUT ERROR", exc_info=True)
        _safe_reply_to(bot, message, "❌ خطایی در پردازش پیام همه‌گیر رخ داد.", reply_markup=get_admin_keyboard())

# --------------------------------------------------------------------------
# Feature 4: /stats — admin-only metrics dashboard
# --------------------------------------------------------------------------
@bot.message_handler(commands=["stats"])
def handle_stats(message):
    try:
        if not is_admin(message.from_user.id):
            _safe_reply_to(bot, message, "⛔ You are not authorized to use this command.")
            return

        uptime_seconds = int(time.time() - _START_TIME)
        uptime_str = str(datetime.timedelta(seconds=uptime_seconds))
        known_groups = len(fetch_all_group_ids())
        known_users = len(fetch_all_user_ids())
        with _debate_lock:
            active_debates = sum(1 for e in _active_debates.values() if e["active"])

        text = (
            "📊 **آمار سیستم**\n\n"
            f"⏱ Uptime: `{uptime_str}`\n\n"
            f"✅ پیام‌های پردازش‌شده: `{METRICS['handled']}`\n"
            f"❌ خطاهای LLM: `{METRICS['llm_errors']}`\n"
            f"🚫 محدودشده (Rate Limit): `{METRICS['ratelimited']}`\n"
            f"🗜 خلاصه‌سازی‌ها: `{METRICS['summaries']}`\n"
            f"🔎 جستجوهای وب (Tavily): `{METRICS['web_searches']}`\n"
            f"📰 دایجست‌های ارسال‌شده: `{METRICS['digests_sent']}`\n"
            f"📥 دانلودهای اینستاگرام: `{METRICS['instagram_downloads']}`\n"
            f"🔁 ریلی اینستاگرام→تلگرام: `{METRICS.get('ig_relay_relayed', 0)}`\n"
            f"🎬 دانلودهای یوتیوب: `{METRICS['youtube_downloads']}`\n"
            f"🧠 فکت‌های آموخته‌شده: `{METRICS.get('facts_learned', 0)}`\n"
            f"💾 بکاپ‌های ارسال‌شده: `{METRICS.get('backups_sent', 0)}`\n"
            f"📋 خلاصه‌گیری‌های /tldr: `{METRICS.get('tldr_requests', 0)}`\n"
            f"🔄 ری‌استارت‌های poller: `{METRICS.get('poller_restarts', 0)}`\n\n"
            f"👥 گروه‌های شناخته‌شده: `{known_groups}`\n"
            f"👤 کاربران شناخته‌شده: `{known_users}`\n"
            f"🎭 مناظره‌های فعال: `{active_debates}`\n"
        )
        _safe_reply_to(bot, message, text, parse_mode="Markdown")
    except Exception:
        log.error("STATS COMMAND ERROR", exc_info=True)
        _safe_reply_to(bot, message, "❌ خطایی در نمایش آمار رخ داد.")

@bot.message_handler(commands=["digest"])
def handle_digest_command(message):
    """Admin-only, private-chat-only: build today's digest right now and
    push it to every known group immediately, instead of waiting for the
    scheduled DIGEST_HOUR_IRAN run. Restricted to private chat so this
    can't be triggered (and re-broadcast to every group) by anyone typing
    /digest inside a group the bot is in."""
    try:
        if not is_admin(message.from_user.id):
            _safe_reply_to(bot, message, "⛔ You are not authorized to use this command.")
            return
        if message.chat.type != "private":
            _safe_reply_to(bot, message, "این دستور فقط داخل پیوی با ادمین کار می‌کند.")
            return

        _safe_reply_to(bot, message, "⏳ در حال ساخت و ارسال دایجست...")
        sent, failed, digest_text = broadcast_daily_digest(triggered_by=f"manual:{message.from_user.id}")

        if not digest_text:
            bot.send_message(
                message.chat.id,
                "❌ نتونستم دایجست بسازم — یا `TAVILY_API_KEY` ست نیست، یا سقف روزانه‌ی سرچ تموم شده، "
                "یا مدل جواب نداد. لاگ سرور رو چک کن.",
            )
            return

        report = (
            f"✅ دایجست ساخته و ارسال شد.\n\n"
            f"📤 ارسال موفق: {sent}\n"
            f"❌ ارسال ناموفق: {failed}\n\n"
            f"---\n\n{digest_text}"
        )
        bot.send_message(message.chat.id, report[:4000], parse_mode="Markdown")
    except Exception:
        log.error("DIGEST COMMAND ERROR", exc_info=True)
        admin_notifier.notify("digest_command_error", traceback.format_exc(limit=5))
        _safe_reply_to(bot, message, "❌ خطایی در اجرای دستور دایجست رخ داد.")

