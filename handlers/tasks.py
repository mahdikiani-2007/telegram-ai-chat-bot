import datetime
import logging
import re
import time
import traceback

from telebot import types

from bots import bot
from db import repo
from handlers.shared import is_admin
from metrics import METRICS
from services.streaming import (
    _safe_reply_to,
    escape_markdown,
    sanitize_backtick_span,
)
from sessions import (
    WAITING_FOR_REMINDER_RECURRENCE,
    WAITING_FOR_REMINDER_TIME,
    WAITING_FOR_TODO_DEADLINE,
    WAITING_FOR_TODO_RECURRENCE,
    _remind_pending_mention,
    _remind_pending_recurrence,
    _remind_sessions,
    _todo_pending_cat,
    _todo_pending_dl,
    _todo_pending_rec,
    _todo_sessions,
    _waiting_text_input,
)
from timeutil import (
    _parse_custom_interval_seconds,
    format_deadline_display,
    format_trigger_time_iran,
    iran_now,
    iran_tomorrow_9am_epoch,
    parse_custom_time_input,
)

log = logging.getLogger("L-bot")


def _parse_assignee(text, chat_id_str):
    """If `text` starts with '@username <task text>', extract the delegation.
    Telegram auto-links plain '@username' mentions in message text natively,
    so no ID resolution is required to notify them — we just also try to
    resolve their numeric ID (via group_members) for bookkeeping/validation.
    Returns (assignee_id_or_None, assignee_username_or_None, remaining_text).
    If the text doesn't look like a delegation, returns (None, None, text)
    unchanged.
    """
    m = re.match(r"^@(\w{3,32})(?:\s+(.*))?$", text.strip(), re.DOTALL)
    if not m:
        return None, None, text
    username = m.group(1)
    remaining = (m.group(2) or "").strip()
    if not remaining:
        # Looks like just a bare mention with no task body — don't treat as delegation.
        return None, None, text
    assignee_id = repo.resolve_username_in_chat(chat_id_str, username)
    return assignee_id, username, remaining

# =========================
# Todo System Helpers
# =========================
PRIORITY_EMOJI = {"High": "🔴", "Medium": "🟡", "Normal": "🔵"}

PRIORITY_LABELS = {"High": "Immediate", "Medium": "Important", "Normal": "Normal"}

CATEGORY_EMOJI = {"Work": "💼", "Personal": "👤", "General": "🌐"}

def _resolve_todo_chat_id(message):
    """Return the effective chat_id for todo storage.

    In private chats → per-user (str of user_id).
    In groups/supergroups → per-group (str of chat_id).
    """
    if message.chat.type == "private":
        return str(message.from_user.id)
    return str(message.chat.id)

def _resolve_todo_chat_id_from_chat(chat, user):
    """Same as _resolve_todo_chat_id but usable from a callback's chat/user context."""
    if chat.type == "private":
        return str(user.id)
    return str(chat.id)

def _build_todo_list_text(chat_id_str):
    """Render the active todo list as a formatted string, including deadlines,
    recurrence, and delegated assignee."""
    todos = repo.get_active_todos(chat_id_str)
    if not todos:
        return None, None
    lines = ["📋 **Active Todo List**\n"]
    for idx, (tid, text, priority, category, creator_id, deadline, recurrence,
              assignee_id, assignee_username) in enumerate(todos, 1):
        p_emoji = PRIORITY_EMOJI.get(priority, "🔵")
        c_emoji = CATEGORY_EMOJI.get(category, "🌐")
        extra = ""
        if deadline is not None:
            extra += f"\n   {format_deadline_display(deadline)}"
        if recurrence and recurrence != "none":
            extra += f"\n   🔄 هر {recurrence}"
        if assignee_username:
            extra += f"\n   👥 @{assignee_username}"
        lines.append(f"{idx}. {p_emoji} {c_emoji} {escape_markdown(text)}{extra}")
    return "\n".join(lines), todos

def _build_todo_list_markup(todos):
    """Build inline keyboard with Done/Delete buttons for each todo item."""
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    for tid, text, priority, category, creator_id, deadline, recurrence, assignee_id, assignee_username in todos:
        short = text[:20] + ("..." if len(text) > 20 else "")
        buttons.append(types.InlineKeyboardButton(
            f"✅ Done ({short})", callback_data=f"todo_done_{tid}"
        ))
        buttons.append(types.InlineKeyboardButton(
            f"🗑 Del ({short})", callback_data=f"todo_del_{tid}"
        ))
    for i in range(0, len(buttons), 2):
        markup.add(*buttons[i:i + 2])
    return markup

def _user_is_authorized_for_todo(todo, call):
    """IDOR guard + Todo Permissions: the callback's chat MUST match the
    todo's stored chat (blocks the forwarded-inline-keyboard cross-chat
    attack), and the clicking user must be either the original creator or a
    group admin."""
    stored_chat_id = str(todo[1])
    creator_id = todo[2]
    if stored_chat_id != str(call.message.chat.id):
        return False, "⛔ این تسک متعلق به این چت نیست."
    if call.from_user.id == creator_id:
        return True, None
    if call.message.chat.type != "private":
        try:
            member = bot.get_chat_member(call.message.chat.id, call.from_user.id)
            if member.status in ("administrator", "creator"):
                return True, None
        except Exception:
            log.warning("get_chat_member failed during todo authorization check", exc_info=True)
    return False, "⛔ فقط سازنده تسک یا ادمین گروه مجاز است."

def _reminder_callback_chat_matches(reminder, call):
    """IDOR guard for reminder snooze/dismiss: only requires the chat to
    match (unlike todos, snooze/dismiss intentionally stays collaborative —
    any member of the CORRECT chat may act on a shared reminder alarm)."""
    return str(reminder[1]) == str(call.message.chat.id)

def _user_is_authorized_for_reminder(reminder, call):
    if not _reminder_callback_chat_matches(reminder, call):
        return False, "⛔ این یادآوری متعلق به این چت نیست."
    return True, None

@bot.message_handler(commands=["todo"])
def handle_todo_command(message):
    """Entry point: /todo [task text] or /todo @username [task text] (groups).

    Validates input, then opens the priority selection inline keyboard.
    Only the command sender can interact with the configuration buttons.
    """
    try:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            _safe_reply_to(bot, 
                message,
                "⚠️ لطفاً متن کار را بعد از دستور `/todo` وارد کنید.\n\n"
                "مثال: `/todo خرید شیر`\n"
                "واگذاری به عضو دیگر گروه: `/todo @username خرید شیر`",
                parse_mode="Markdown",
            )
            return

        body = parts[1].strip()
        chat_id_str = str(message.chat.id)
        user_id = message.from_user.id

        assignee_id, assignee_username, task_text = None, None, body
        if message.chat.type != "private":
            assignee_id, assignee_username, task_text = _parse_assignee(body, chat_id_str)

        if not task_text.strip():
            _safe_reply_to(bot, message, "⚠️ متن کار نمی‌تواند خالی باشد.")
            return

        markup = types.InlineKeyboardMarkup(row_width=3)
        markup.add(
            types.InlineKeyboardButton("🔴 Immediate", callback_data=f"todo_pri_High_{user_id}"),
            types.InlineKeyboardButton("🟡 Important", callback_data=f"todo_pri_Medium_{user_id}"),
            types.InlineKeyboardButton("🔵 Normal", callback_data=f"todo_pri_Normal_{user_id}"),
        )

        session_key = (str(message.chat.id), user_id)
        _todo_sessions.set(session_key, {
            "text": task_text,
            "assignee_id": assignee_id,
            "assignee_username": assignee_username,
        })

        assignee_line = f"\n👥 **مسئول:** @{assignee_username}" if assignee_username else ""
        bot.send_message(
            message.chat.id,
            f"📝 **New Todo Task:**\n`{sanitize_backtick_span(task_text)}`{assignee_line}\n\n"
            "🔽 اولویت را انتخاب کنید:",
            parse_mode="Markdown",
            reply_markup=markup,
        )
    except Exception:
        log.error("TODO COMMAND ERROR", exc_info=True)
        _safe_reply_to(bot, message, "❌ خطایی در ایجاد تسک رخ داد.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("todo_pri_"))
def handle_todo_priority(call):
    """Handle priority selection. Format: todo_pri_{priority}_{original_user_id}"""
    try:
        parts = call.data.split("_", 3)
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Invalid callback data.")
            return

        priority = parts[2]
        original_user_id = int(parts[3])

        if call.from_user.id != original_user_id:
            bot.answer_callback_query(call.id, "⛔ فقط فردی که دستور را زده می‌تواند انتخاب کند.", show_alert=True)
            return

        session_key = (str(call.message.chat.id), original_user_id)
        session = _todo_sessions.pop(session_key)

        if not session:
            bot.answer_callback_query(call.id, "❌ نشست منقضی شده. لطفاً دوباره /todo بزنید.", show_alert=True)
            return

        _todo_pending_cat.set(session_key, {**session, "priority": priority})

        markup = types.InlineKeyboardMarkup(row_width=3)
        markup.add(
            types.InlineKeyboardButton("💼 Work", callback_data=f"todo_cat_Work_{priority}_{original_user_id}"),
            types.InlineKeyboardButton("👤 Personal", callback_data=f"todo_cat_Personal_{priority}_{original_user_id}"),
            types.InlineKeyboardButton("🌐 General", callback_data=f"todo_cat_General_{priority}_{original_user_id}"),
        )

        p_emoji = PRIORITY_EMOJI.get(priority, "🔵")
        p_label = PRIORITY_LABELS.get(priority, priority)

        bot.edit_message_text(
            f"📝 **Task:** `{sanitize_backtick_span(session['text'])}`\n"
            f"🎯 **Priority:** {p_emoji} {p_label}\n\n"
            "🔽 دسته‌بندی را انتخاب کنید:",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="Markdown",
            reply_markup=markup,
        )
        bot.answer_callback_query(call.id)
    except Exception:
        log.error("TODO PRIORITY CALLBACK ERROR", exc_info=True)
        bot.answer_callback_query(call.id, "❌ خطا رخ داد.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("todo_cat_"))
def handle_todo_category(call):
    """Handle category selection → show deadline step. Format:
    todo_cat_{category}_{priority}_{original_user_id}"""
    try:
        parts = call.data.split("_", 4)
        if len(parts) < 5:
            bot.answer_callback_query(call.id, "❌ Invalid callback data.")
            return

        category = parts[2]
        original_user_id = int(parts[4])

        if call.from_user.id != original_user_id:
            bot.answer_callback_query(call.id, "⛔ فقط فردی که دستور را زده میتواند انتخاب کند.", show_alert=True)
            return

        session_key = (str(call.message.chat.id), original_user_id)
        session = _todo_pending_cat.pop(session_key)

        if not session:
            bot.answer_callback_query(call.id, "❌ نشست منقضی شده. لطفاً دوباره /todo بزنید.", show_alert=True)
            return

        # Dead-code bug fixed: the session's stored priority is the single
        # source of truth (previously `priority = priority` was a no-op).
        priority = session["priority"]

        _todo_pending_dl.set(session_key, {**session, "category": category})

        p_emoji = PRIORITY_EMOJI.get(priority, "🔵")
        c_emoji = CATEGORY_EMOJI.get(category, "🌐")

        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("⏰ No Deadline", callback_data=f"todo_dl_none_{original_user_id}"),
            types.InlineKeyboardButton("📅 Add Custom Deadline", callback_data=f"todo_dl_custom_{original_user_id}"),
        )

        bot.edit_message_text(
            f"📝 **Task:** `{sanitize_backtick_span(session['text'])}`\n"
            f"🎯 Priority: {p_emoji} {PRIORITY_LABELS.get(priority, priority)}\n"
            f"📂 Category: {c_emoji} {category}\n\n"
            "🔽 آیا میخواهید مهلت تعیین کنید؟",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="Markdown",
            reply_markup=markup,
        )
        bot.answer_callback_query(call.id)
    except Exception:
        log.error("TODO CATEGORY CALLBACK ERROR", exc_info=True)
        bot.answer_callback_query(call.id, "❌ خطا رخ داد.")

def _build_inline_cancel_button(user_id):
    """Build an inline keyboard with a single Cancel button."""
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data=f"wait_cancel_{user_id}"))
    return markup

def _finalize_todo_save(chat_id_str, user_id, session, deadline, recurrence, call=None, message=None):
    """Common finalization: save todo and display confirmation.

    Provide either `call` (edits the originating callback message) or
    `message` (sends a fresh confirmation message) — exactly one of them."""
    task_text = session["text"]
    priority = session["priority"]
    category = session["category"]
    assignee_id = session.get("assignee_id")
    assignee_username = session.get("assignee_username")

    repo.add_todo(
        chat_id_str, user_id, task_text, priority, category, deadline,
        recurrence=recurrence, assignee_id=assignee_id, assignee_username=assignee_username,
    )

    p_emoji = PRIORITY_EMOJI.get(priority, "🔵")
    c_emoji = CATEGORY_EMOJI.get(category, "🌐")
    dl_str = f"\n⏰ Deadline: {format_deadline_display(deadline)}" if deadline is not None else ""
    rec_str = f"\n🔄 Recurrence: every {recurrence}" if recurrence and recurrence != "none" else ""
    assignee_str = f"\n👥 Assigned to: @{assignee_username}" if assignee_username else ""

    text = (
        "✅ **Task Added Successfully!**\n\n"
        f"📝 `{sanitize_backtick_span(task_text)}`\n"
        f"🎯 Priority: {p_emoji} {PRIORITY_LABELS.get(priority, priority)}\n"
        f"📂 Category: {c_emoji} {category}{dl_str}{rec_str}{assignee_str}\n\n"
        "Use /todos to view the active list."
    )

    if call is not None:
        try:
            bot.edit_message_text(
                text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                parse_mode="Markdown",
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id, "✅ تسک اضافه شد!")
    else:
        bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("todo_dl_none_"))
def handle_todo_deadline_none(call):
    """No deadline selected → finalize todo with deadline=None, recurrence='none'."""
    try:
        parts = call.data.split("_", 3)
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Invalid callback data.")
            return

        original_user_id = int(parts[3])
        if call.from_user.id != original_user_id:
            bot.answer_callback_query(call.id, "⛔ فقط فردی که دستور را زده میتواند انتخاب کند.", show_alert=True)
            return

        session_key = (str(call.message.chat.id), original_user_id)
        session = _todo_pending_dl.pop(session_key)

        if not session:
            bot.answer_callback_query(call.id, "❌ نشست منقضی شده.", show_alert=True)
            return

        storage_chat_id = _resolve_todo_chat_id_from_chat(call.message.chat, call.from_user)
        _finalize_todo_save(storage_chat_id, original_user_id, session, None, "none", call=call)
    except Exception:
        log.error("TODO DEADLINE NONE CALLBACK ERROR", exc_info=True)
        bot.answer_callback_query(call.id, "❌ خطا رخ داد.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("todo_dl_custom_"))
def handle_todo_deadline_custom(call):
    """Custom deadline selected → prompt user for text input."""
    try:
        parts = call.data.split("_", 3)
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Invalid callback data.")
            return

        original_user_id = int(parts[3])
        if call.from_user.id != original_user_id:
            bot.answer_callback_query(call.id, "⛔ فقط فردی که دستور را زده میتواند انتخاب کند.", show_alert=True)
            return

        session_key = (str(call.message.chat.id), original_user_id)
        if _todo_pending_dl.get(session_key) is None:
            bot.answer_callback_query(call.id, "❌ نشست منقضی شده.", show_alert=True)
            return

        _waiting_text_input.set(session_key, {"state": WAITING_FOR_TODO_DEADLINE, "user_id": original_user_id})

        bot.edit_message_text(
            "📅 **Set Deadline**\n\n"
            "Please type the deadline time. Supported formats:\n\n"
            "⏱ **Relative:** `45m`, `2h`, `3d`\n"
            "🕐 **Clock:** `18:30` (today Iran time, or tomorrow if passed)\n"
            "📅 **Full date:** `2026-07-15 10:00` (Iran time)\n\n"
            "Send the time now, or tap ❌ Cancel below.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="Markdown",
            reply_markup=_build_inline_cancel_button(original_user_id),
        )
        bot.answer_callback_query(call.id)
    except Exception:
        log.error("TODO DEADLINE CUSTOM CALLBACK ERROR", exc_info=True)
        bot.answer_callback_query(call.id, "❌ خطا رخ داد.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("todo_dl_retry_"))
def handle_todo_deadline_retry(call):
    """Retry deadline input after a parse error."""
    try:
        parts = call.data.split("_", 3)
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Invalid callback data.")
            return

        original_user_id = int(parts[3])
        if call.from_user.id != original_user_id:
            bot.answer_callback_query(call.id, "⛔ فقط فردی که دستور را زده میتواند انتخاب کند.", show_alert=True)
            return

        _waiting_text_input.set(
            (str(call.message.chat.id), original_user_id),
            {"state": WAITING_FOR_TODO_DEADLINE, "user_id": original_user_id},
        )

        bot.edit_message_text(
            "📅 **Set Deadline**\n\n"
            "Please type the deadline time:\n"
            "⏱ `45m`, `2h`, `3d` | 🕐 `18:30` | 📅 `2026-07-15 10:00`\n\n"
            "(All times in Iran timezone UTC+3:30)",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="Markdown",
            reply_markup=_build_inline_cancel_button(original_user_id),
        )
        bot.answer_callback_query(call.id)
    except Exception:
        log.error("TODO DEADLINE RETRY CALLBACK ERROR", exc_info=True)
        bot.answer_callback_query(call.id, "❌ خطا رخ داد.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("todo_rec_"))
def handle_todo_recurrence(call):
    """Feature 6: recurring todos. Format: todo_rec_{type}_{original_user_id}"""
    try:
        parts = call.data.split("_", 3)
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Invalid callback data.")
            return

        recurrence = parts[2]
        original_user_id = int(parts[3])
        if call.from_user.id != original_user_id:
            bot.answer_callback_query(call.id, "⛔ فقط فردی که دستور را زده میتواند انتخاب کند.", show_alert=True)
            return

        session_key = (str(call.message.chat.id), original_user_id)
        session = _todo_pending_rec.pop(session_key)
        if not session:
            bot.answer_callback_query(call.id, "❌ نشست منقضی شده.", show_alert=True)
            return

        storage_chat_id = _resolve_todo_chat_id_from_chat(call.message.chat, call.from_user)
        _finalize_todo_save(storage_chat_id, original_user_id, session, session["deadline"], recurrence, call=call)
    except Exception:
        log.error("TODO RECURRENCE CALLBACK ERROR", exc_info=True)
        bot.answer_callback_query(call.id, "❌ خطا رخ داد.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("todo_customrec_"))
def handle_todo_custom_recurrence(call):
    """Custom recurrence interval for a recurring todo → prompt text input."""
    try:
        parts = call.data.split("_", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "❌ Invalid callback data.")
            return

        original_user_id = int(parts[2])
        if call.from_user.id != original_user_id:
            bot.answer_callback_query(call.id, "⛔ فقط فردی که دستور را زده میتواند انتخاب کند.", show_alert=True)
            return

        session_key = (str(call.message.chat.id), original_user_id)
        if _todo_pending_rec.get(session_key) is None:
            bot.answer_callback_query(call.id, "❌ نشست منقضی شده.", show_alert=True)
            return

        _waiting_text_input.set(session_key, {"state": WAITING_FOR_TODO_RECURRENCE, "user_id": original_user_id})

        bot.edit_message_text(
            "⚙️ **Custom Recurrence**\n\n"
            "Please type the repeat interval:\n\n"
            "⏱ `12h` — every 12 hours\n📅 `3d` — every 3 days\n⏱ `30m` — every 30 minutes\n\n"
            "Send the interval now, or tap ❌ Cancel below.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="Markdown",
            reply_markup=_build_inline_cancel_button(original_user_id),
        )
        bot.answer_callback_query(call.id)
    except Exception:
        log.error("TODO CUSTOM RECURRENCE CALLBACK ERROR", exc_info=True)
        bot.answer_callback_query(call.id, "❌ خطا رخ داد.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("wait_cancel_"))
def handle_wait_cancel(call):
    """Cancel any waiting text input state (todo/reminder wizard)."""
    try:
        original_user_id = int(call.data.split("_")[2])
        if call.from_user.id != original_user_id:
            bot.answer_callback_query(call.id, "⛔ فقط فردی که دستور را زده میتواند لغو کند.", show_alert=True)
            return

        session_key = (str(call.message.chat.id), original_user_id)
        _waiting_text_input.discard(session_key)
        _remind_sessions.discard(session_key)
        _remind_pending_mention.discard(session_key)
        _remind_pending_recurrence.discard(session_key)
        _todo_pending_dl.discard(session_key)
        _todo_pending_rec.discard(session_key)

        bot.edit_message_text(
            "❎ **Cancelled.**",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="Markdown",
        )
        bot.answer_callback_query(call.id, "❎ لغو شد.")
    except Exception:
        log.error("WAIT CANCEL ERROR", exc_info=True)
        bot.answer_callback_query(call.id, "❌ خطا رخ داد.")

def _recurrence_markup(user_id, custom_prefix="remind_customrec"):
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.add(
        types.InlineKeyboardButton("1️⃣ One-time", callback_data=f"remind_rec_none_{user_id}"),
        types.InlineKeyboardButton("🔁 Daily", callback_data=f"remind_rec_daily_{user_id}"),
        types.InlineKeyboardButton("📆 Weekly", callback_data=f"remind_rec_weekly_{user_id}"),
    )
    markup.add(types.InlineKeyboardButton("⚙️ Custom Recurrence", callback_data=f"{custom_prefix}_{user_id}"))
    return markup

def _reminder_target_label(payload):
    if payload.get("assignee_username"):
        return f"👥 @{payload['assignee_username']}"
    mention_type = payload.get("mention_type", "me")
    return "👤 فقط من" if mention_type == "me" else "📢 همه اعضا"

@bot.message_handler(commands=["remind"])
def handle_remind_command(message):
    """Entry point: /remind [text] or /remind @username [text] (groups).

    Opens a multi-step inline keyboard flow:
    1. Choose time offset
    2. Choose mention target (me / all) — skipped if delegated to @username
    3. Choose recurrence (one-time / daily / weekly / custom)
    """
    try:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            _safe_reply_to(bot, 
                message,
                "⚠️ لطفاً متن یادآوری را بعد از دستور `/remind` وارد کنید.\n\n"
                "مثال: `/remind جلسه تیم`\n"
                "واگذاری به عضو دیگر گروه: `/remind @username جلسه تیم`",
                parse_mode="Markdown",
            )
            return

        body = parts[1].strip()
        chat_id_str = str(message.chat.id)
        user_id = message.from_user.id

        assignee_id, assignee_username, reminder_text = None, None, body
        if message.chat.type != "private":
            assignee_id, assignee_username, reminder_text = _parse_assignee(body, chat_id_str)

        if not reminder_text.strip():
            _safe_reply_to(bot, message, "⚠️ متن یادآوری نمی‌تواند خالی باشد.")
            return

        session_key = (str(message.chat.id), user_id)
        _remind_sessions.set(session_key, {
            "text": reminder_text,
            "assignee_id": assignee_id,
            "assignee_username": assignee_username,
        })

        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("⏱ 10 Minutes Later", callback_data=f"remind_time_10m_{user_id}"),
            types.InlineKeyboardButton("⏱ 30 Minutes Later", callback_data=f"remind_time_30m_{user_id}"),
        )
        markup.add(
            types.InlineKeyboardButton("⏳ 1 Hour Later", callback_data=f"remind_time_1h_{user_id}"),
            types.InlineKeyboardButton("⏳ 3 Hours Later", callback_data=f"remind_time_3h_{user_id}"),
        )
        markup.add(
            types.InlineKeyboardButton("🌅 Tomorrow at 9:00 AM", callback_data=f"remind_time_tom9_{user_id}"),
            types.InlineKeyboardButton("⚙️ Custom Time", callback_data=f"remind_customtime_{user_id}"),
        )

        assignee_line = f"\n👥 **مسئول:** @{assignee_username}" if assignee_username else ""
        bot.send_message(
            message.chat.id,
            f"🔔 **یادآوری جدید:**\n`{reminder_text}`{assignee_line}\n\n"
            "🔽 زمان اجرا را انتخاب کنید:",
            parse_mode="Markdown",
            reply_markup=markup,
        )
    except Exception:
        log.error("REMIND COMMAND ERROR", exc_info=True)
        _safe_reply_to(bot, message, "❌ خطایی در ایجاد یادآوری رخ داد.")

def _parse_remind_time(time_code):
    """Convert time code to an absolute UTC epoch."""
    offsets = {
        "10m": 10 * 60,
        "30m": 30 * 60,
        "1h": 1 * 3600,
        "3h": 3 * 3600,
        "tom9": None,  # Special: calculate tomorrow 9AM Iran time
    }
    if time_code == "tom9":
        return iran_tomorrow_9am_epoch()
    offset = offsets.get(time_code)
    if offset is None:
        return None
    return time.time() + offset

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("remind_time_"))
def handle_remind_time(call):
    """Handle time selection. Format: remind_time_{code}_{user_id}"""
    try:
        parts = call.data.split("_", 3)
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Invalid callback data.")
            return

        time_code = parts[2]
        original_user_id = int(parts[3])

        if call.from_user.id != original_user_id:
            bot.answer_callback_query(call.id, "⛔ فقط فردی که دستور را زده می‌تواند انتخاب کند.", show_alert=True)
            return

        trigger_time = _parse_remind_time(time_code)
        if trigger_time is None:
            bot.answer_callback_query(call.id, "❌ کد زمان نامعتبر.")
            return

        session_key = (str(call.message.chat.id), original_user_id)
        session = _remind_sessions.pop(session_key)
        if not session:
            bot.answer_callback_query(call.id, "❌ نشست منقضی شده. لطفاً دوباره /remind بزنید.", show_alert=True)
            return

        payload = {**session, "trigger_time": trigger_time}
        time_display = format_trigger_time_iran(trigger_time)
        time_labels = {
            "10m": "⏱ 10 دقیقه دیگر", "30m": "⏱ 30 دقیقه دیگر", "1h": "⏳ 1 ساعت دیگر",
            "3h": "⏳ 3 ساعت دیگر", "tom9": "🌅 فردا ساعت ۹:۰۰ صبح",
        }
        time_label = time_labels.get(time_code, time_code)

        if payload.get("assignee_username"):
            # Delegated task — skip the mention-target step; the assignee IS the target.
            _remind_pending_recurrence.set(session_key, {**payload, "mention_type": "assignee"})
            bot.edit_message_text(
                f"🔔 **یادآوری:** `{sanitize_backtick_span(payload['text'])}`\n"
                f"⏰ **زمان:** {time_label} ({time_display})\n"
                f"👥 **مسئول:** @{payload['assignee_username']}\n\n"
                "🔽 تکرار یادآوری؟",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                parse_mode="Markdown", reply_markup=_recurrence_markup(original_user_id),
            )
            bot.answer_callback_query(call.id)
            return

        _remind_pending_mention.set(session_key, payload)
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("👤 Only Me", callback_data=f"remind_ment_me_{original_user_id}"),
            types.InlineKeyboardButton("📢 All Members", callback_data=f"remind_ment_all_{original_user_id}"),
        )
        bot.edit_message_text(
            f"🔔 **یادآوری:** `{sanitize_backtick_span(payload['text'])}`\n"
            f"⏰ **زمان:** {time_label} ({time_display})\n\n"
            "🔽 چه کسی هنگام اجرا اعلان شود؟",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode="Markdown", reply_markup=markup,
        )
        bot.answer_callback_query(call.id)
    except Exception:
        log.error("REMIND TIME CALLBACK ERROR", exc_info=True)
        bot.answer_callback_query(call.id, "❌ خطا رخ داد.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("remind_ment_"))
def handle_remind_mention(call):
    """Handle mention type selection (only reached for non-delegated reminders).
    Format: remind_ment_{type}_{user_id}"""
    try:
        parts = call.data.split("_", 3)
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Invalid callback data.")
            return

        mention_type = parts[2]
        original_user_id = int(parts[3])

        if call.from_user.id != original_user_id:
            bot.answer_callback_query(call.id, "⛔ فقط فردی که دستور را زده می‌تواند انتخاب کند.", show_alert=True)
            return

        session_key = (str(call.message.chat.id), original_user_id)
        payload = _remind_pending_mention.pop(session_key)
        if not payload:
            bot.answer_callback_query(call.id, "❌ نشست منقضی شده.", show_alert=True)
            return

        payload = {**payload, "mention_type": mention_type}
        _remind_pending_recurrence.set(session_key, payload)

        mention_label = "👤 فقط من" if mention_type == "me" else "📢 همه اعضا"
        bot.edit_message_text(
            f"🔔 **یادآوری:** `{sanitize_backtick_span(payload['text'])}`\n"
            f"⏰ **زمان:** {format_trigger_time_iran(payload['trigger_time'])}\n"
            f"👥 **اعلان به:** {mention_label}\n\n"
            "🔽 تکرار یادآوری؟",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode="Markdown", reply_markup=_recurrence_markup(original_user_id),
        )
        bot.answer_callback_query(call.id)
    except Exception:
        log.error("REMIND MENTION CALLBACK ERROR", exc_info=True)
        bot.answer_callback_query(call.id, "❌ خطا رخ داد.")

def _finalize_reminder_save(chat, user_id, payload, recurrence, call=None, message=None):
    storage_chat_id = str(user_id) if chat.type == "private" else str(chat.id)
    repo.add_reminder(
        storage_chat_id, user_id, payload["text"], payload.get("mention_type", "me"), recurrence,
        payload["trigger_time"], assignee_id=payload.get("assignee_id"),
        assignee_username=payload.get("assignee_username"),
    )
    rec_labels = {"none": "1️⃣ One-time", "daily": "🔁 Daily", "weekly": "📆 Weekly"}
    rec_label = rec_labels.get(recurrence, f"⚙️ هر {recurrence}")
    target_label = _reminder_target_label(payload)
    time_display = format_trigger_time_iran(payload["trigger_time"])

    text = (
        "✅ **یادآوری ثبت شد!**\n\n"
        f"📝 `{sanitize_backtick_span(payload['text'])}`\n"
        f"⏰ **زمان:** {time_display}\n"
        f"👥 **اعلان به:** {target_label}\n"
        f"🔄 **تکرار:** {rec_label}\n\n"
        "هنگام اجرا، اعلان دریافت خواهید کرد."
    )
    if call is not None:
        try:
            bot.edit_message_text(
                text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                parse_mode="Markdown",
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id, "✅ یادآوری ثبت شد!")
    else:
        bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("remind_rec_"))
def handle_remind_recurrence(call):
    """Handle recurrence selection and finalize reminder. Format: remind_rec_{type}_{user_id}"""
    try:
        parts = call.data.split("_", 3)
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Invalid callback data.")
            return

        recurrence = parts[2]
        original_user_id = int(parts[3])

        if call.from_user.id != original_user_id:
            bot.answer_callback_query(call.id, "⛔ فقط فردی که دستور را زده میتواند انتخاب کند.", show_alert=True)
            return

        session_key = (str(call.message.chat.id), original_user_id)
        payload = _remind_pending_recurrence.pop(session_key)
        if not payload:
            bot.answer_callback_query(call.id, "❌ نشست منقضی شده.", show_alert=True)
            return

        _finalize_reminder_save(call.message.chat, original_user_id, payload, recurrence, call=call)
    except Exception:
        log.error("REMIND RECURRENCE CALLBACK ERROR", exc_info=True)
        bot.answer_callback_query(call.id, "❌ خطا رخ داد.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("remind_customtime_"))
def handle_remind_custom_time(call):
    """Custom Time button clicked → prompt user for text input."""
    try:
        parts = call.data.split("_", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "❌ Invalid callback data.")
            return

        original_user_id = int(parts[2])
        if call.from_user.id != original_user_id:
            bot.answer_callback_query(call.id, "⛔ فقط فردی که دستور را زده میتواند انتخاب کند.", show_alert=True)
            return

        session_key = (str(call.message.chat.id), original_user_id)
        if _remind_sessions.get(session_key) is None:
            bot.answer_callback_query(call.id, "❌ نشست منقضی شده.", show_alert=True)
            return

        _waiting_text_input.set(session_key, {"state": WAITING_FOR_REMINDER_TIME, "user_id": original_user_id})

        bot.edit_message_text(
            "⚙️ **Custom Time**\n\n"
            "Please type the trigger time. Supported formats:\n\n"
            "⏱ **Relative:** `45m`, `2h`, `3d`\n"
            "🕐 **Clock:** `18:30` (today Iran time, or tomorrow if passed)\n"
            "📅 **Full date:** `2026-07-15 10:00` (Iran time)\n\n"
            "Send the time now, or tap ❌ Cancel below.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="Markdown",
            reply_markup=_build_inline_cancel_button(original_user_id),
        )
        bot.answer_callback_query(call.id)
    except Exception:
        log.error("REMIND CUSTOM TIME CALLBACK ERROR", exc_info=True)
        bot.answer_callback_query(call.id, "❌ خطا رخ داد.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("remind_customrec_"))
def handle_remind_custom_recurrence(call):
    """Custom Recurrence button clicked → prompt user for text input."""
    try:
        parts = call.data.split("_", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "❌ Invalid callback data.")
            return

        original_user_id = int(parts[2])
        if call.from_user.id != original_user_id:
            bot.answer_callback_query(call.id, "⛔ فقط فردی که دستور را زده میتواند انتخاب کند.", show_alert=True)
            return

        session_key = (str(call.message.chat.id), original_user_id)
        if _remind_pending_recurrence.get(session_key) is None:
            bot.answer_callback_query(call.id, "❌ نشست منقضی شده.", show_alert=True)
            return

        _waiting_text_input.set(session_key, {"state": WAITING_FOR_REMINDER_RECURRENCE, "user_id": original_user_id})

        bot.edit_message_text(
            "⚙️ **Custom Recurrence**\n\n"
            "Please type the repeat interval:\n\n"
            "⏱ `12h` — every 12 hours\n📅 `3d` — every 3 days\n⏱ `30m` — every 30 minutes\n\n"
            "Send the interval now, or tap ❌ Cancel below.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="Markdown",
            reply_markup=_build_inline_cancel_button(original_user_id),
        )
        bot.answer_callback_query(call.id)
    except Exception:
        log.error("REMIND CUSTOM RECURRENCE CALLBACK ERROR", exc_info=True)
        bot.answer_callback_query(call.id, "❌ خطا رخ داد.")

def _is_waiting_text_input(message):
    """Check if the user is in a waiting-for-text-input state."""
    if not message.text or message.text.startswith("/"):
        return False
    session_key = (str(message.chat.id), message.from_user.id)
    return _waiting_text_input.get(session_key) is not None

@bot.message_handler(func=lambda m: _is_waiting_text_input(m), content_types=["text"])
def handle_waiting_text_input(message):
    """Handle text messages from users who are in a waiting-for-input state.

    Registered BEFORE handle_all_messages so it intercepts text inputs for
    custom time, recurrence, or deadline entries."""
    try:
        session_key = (str(message.chat.id), message.from_user.id)
        session = _waiting_text_input.pop(session_key)

        if not session:
            return

        state = session["state"]
        user_id = session["user_id"]
        user_text = message.text.strip()

        if state == WAITING_FOR_REMINDER_TIME:
            _handle_reminder_time_text(message, session_key, user_id, user_text)
        elif state == WAITING_FOR_REMINDER_RECURRENCE:
            _handle_reminder_recurrence_text(message, session_key, user_id, user_text)
        elif state == WAITING_FOR_TODO_DEADLINE:
            _handle_todo_deadline_text(message, session_key, user_id, user_text)
        elif state == WAITING_FOR_TODO_RECURRENCE:
            _handle_todo_recurrence_text(message, session_key, user_id, user_text)
    except Exception:
        log.error("WAITING TEXT INPUT HANDLER ERROR", exc_info=True)
        _safe_reply_to(bot, message, "❌ خطایی رخ داد.")

def _handle_reminder_time_text(message, session_key, user_id, user_text):
    """Process custom time input for reminders."""
    trigger_time = parse_custom_time_input(user_text)
    if trigger_time is None:
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("🔄 Retry", callback_data=f"remind_customtime_{user_id}"),
            types.InlineKeyboardButton("❌ Cancel", callback_data=f"wait_cancel_{user_id}"),
        )
        _safe_reply_to(bot, 
            message,
            "❌ **فرمت زمان نامعتبر!**\n\n"
            "فرمتهای مجاز:\n⏱ `45m`, `2h`, `3d`\n🕐 `18:30`\n📅 `2026-07-15 10:00`\n\n"
            "(تمام زمانها به وقت ایران UTC+3:30)",
            parse_mode="Markdown",
            reply_markup=markup,
        )
        return

    session = _remind_sessions.pop(session_key)
    if not session:
        _safe_reply_to(bot, message, "❌ نشست منقضی شده. لطفاً دوباره /remind بزنید.")
        return

    payload = {**session, "trigger_time": trigger_time}
    time_display = format_trigger_time_iran(trigger_time)

    if payload.get("assignee_username"):
        _remind_pending_recurrence.set(session_key, {**payload, "mention_type": "assignee"})
        bot.send_message(
            message.chat.id,
            f"🔔 **یادآوری:** `{sanitize_backtick_span(payload['text'])}`\n"
            f"⏰ **زمان:** {time_display}\n"
            f"👥 **مسئول:** @{payload['assignee_username']}\n\n"
            "🔽 تکرار یادآوری؟",
            parse_mode="Markdown",
            reply_markup=_recurrence_markup(user_id),
        )
        return

    _remind_pending_mention.set(session_key, payload)
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("👤 Only Me", callback_data=f"remind_ment_me_{user_id}"),
        types.InlineKeyboardButton("📢 All Members", callback_data=f"remind_ment_all_{user_id}"),
    )
    bot.send_message(
        message.chat.id,
        f"🔔 **یادآوری:** `{sanitize_backtick_span(payload['text'])}`\n"
        f"⏰ **زمان:** {time_display}\n\n"
        "🔽 چه کسی هنگام اجرا اعلان شود؟",
        parse_mode="Markdown",
        reply_markup=markup,
    )

def _handle_reminder_recurrence_text(message, session_key, user_id, user_text):
    """Process custom recurrence interval input for reminders."""
    interval_seconds = _parse_custom_interval_seconds(user_text)
    if interval_seconds is None or interval_seconds < 60:
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("🔄 Retry", callback_data=f"remind_customrec_{user_id}"),
            types.InlineKeyboardButton("❌ Cancel", callback_data=f"wait_cancel_{user_id}"),
        )
        _safe_reply_to(bot, 
            message,
            "❌ **فرمت تکرار نامعتبر!**\n\n"
            "فرمتهای مجاز:\n⏱ `12h` — هر ۱۲ ساعت\n📅 `3d` — هر ۳ روز\n⏱ `30m` — هر ۳۰ دقیقه\n\n"
            "حداقل فاصله: ۶۰ دقیقه",
            parse_mode="Markdown",
            reply_markup=markup,
        )
        return

    payload = _remind_pending_recurrence.pop(session_key)
    if not payload:
        _safe_reply_to(bot, message, "❌ نشست منقضی شده.")
        return

    recurrence = user_text.strip()
    _finalize_reminder_save(message.chat, user_id, payload, recurrence, message=message)

def _handle_todo_deadline_text(message, session_key, user_id, user_text):
    """Process custom deadline input for todos → advance to the recurrence step."""
    deadline_epoch = parse_custom_time_input(user_text)
    if deadline_epoch is None:
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("🔄 Retry", callback_data=f"todo_dl_retry_{user_id}"),
            types.InlineKeyboardButton("❌ Cancel", callback_data=f"wait_cancel_{user_id}"),
        )
        _safe_reply_to(bot, 
            message,
            "❌ **فرمت زمان نامعتبر!**\n\n"
            "فرمتهای مجاز:\n⏱ `45m`, `2h`, `3d`\n🕐 `18:30`\n📅 `2026-07-15 10:00`\n\n"
            "(تمام زمانها به وقت ایران UTC+3:30)",
            parse_mode="Markdown",
            reply_markup=markup,
        )
        return

    session = _todo_pending_dl.pop(session_key)
    if not session:
        _safe_reply_to(bot, message, "❌ نشست منقضی شده. لطفاً دوباره /todo بزنید.")
        return

    session = {**session, "deadline": deadline_epoch}
    _todo_pending_rec.set(session_key, session)

    dl_display = format_deadline_display(deadline_epoch)
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.add(
        types.InlineKeyboardButton("1️⃣ One-time", callback_data=f"todo_rec_none_{user_id}"),
        types.InlineKeyboardButton("🔁 Daily", callback_data=f"todo_rec_daily_{user_id}"),
        types.InlineKeyboardButton("📆 Weekly", callback_data=f"todo_rec_weekly_{user_id}"),
    )
    markup.add(types.InlineKeyboardButton("⚙️ Custom Recurrence", callback_data=f"todo_customrec_{user_id}"))
    bot.send_message(
        message.chat.id,
        f"📝 **Task:** `{sanitize_backtick_span(session['text'])}`\n"
        f"⏰ **Deadline:** {dl_display}\n\n"
        "🔽 آیا این کار تکرار می‌شود؟",
        parse_mode="Markdown",
        reply_markup=markup,
    )

def _handle_todo_recurrence_text(message, session_key, user_id, user_text):
    """Process custom recurrence interval input for a recurring todo."""
    interval_seconds = _parse_custom_interval_seconds(user_text)
    if interval_seconds is None or interval_seconds < 60:
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("🔄 Retry", callback_data=f"todo_customrec_{user_id}"),
            types.InlineKeyboardButton("❌ Cancel", callback_data=f"wait_cancel_{user_id}"),
        )
        _safe_reply_to(bot, 
            message,
            "❌ **فرمت تکرار نامعتبر!**\n\n"
            "فرمتهای مجاز:\n⏱ `12h` — هر ۱۲ ساعت\n📅 `3d` — هر ۳ روز\n⏱ `30m` — هر ۳۰ دقیقه\n\n"
            "حداقل فاصله: ۶۰ دقیقه",
            parse_mode="Markdown",
            reply_markup=markup,
        )
        return

    session = _todo_pending_rec.pop(session_key)
    if not session:
        _safe_reply_to(bot, message, "❌ نشست منقضی شده.")
        return

    recurrence = user_text.strip()
    storage_chat_id = _resolve_todo_chat_id_from_chat(message.chat, message.from_user)
    _finalize_todo_save(storage_chat_id, user_id, session, session["deadline"], recurrence, message=message)

@bot.message_handler(commands=["todos"])
def handle_todos_command(message):
    """Display the active todo list with inline Done/Delete buttons, plus expired tasks."""
    try:
        chat_id_str = _resolve_todo_chat_id(message)
        text, todos = _build_todo_list_text(chat_id_str)

        expired_todos = repo.get_expired_todos(chat_id_str)
        expired_section = ""
        if expired_todos:
            expired_lines = ["\n\n☠️ **Expired Tasks**\n"]
            for idx, (tid, txt, priority, category, creator_id, deadline, recurrence,
                      assignee_id, assignee_username) in enumerate(expired_todos, 1):
                p_emoji = PRIORITY_EMOJI.get(priority, "🔵")
                c_emoji = CATEGORY_EMOJI.get(category, "🌐")
                expired_lines.append(f"{idx}. {p_emoji} {c_emoji} {escape_markdown(txt)}")
            expired_section = "\n".join(expired_lines)

        if not text and not expired_section:
            _safe_reply_to(bot, message, "📋 لیست کارهای شما خالی است.\nاز `/todo [متن]` برای افزودن استفاده کنید.", parse_mode="Markdown")
            return

        full_text = (text or "") + expired_section
        if not full_text.strip():
            full_text = "📋 لیست کارهای شما خالی است.\nاز `/todo [متن]` برای افزودن استفاده کنید."
            _safe_reply_to(bot, message, full_text, parse_mode="Markdown")
            return

        markup = _build_todo_list_markup(todos) if todos else types.InlineKeyboardMarkup()
        bot.send_message(
            message.chat.id,
            full_text,
            parse_mode="Markdown",
            reply_markup=markup,
        )
    except Exception:
        log.error("TODOS LIST ERROR", exc_info=True)
        _safe_reply_to(bot, message, "❌ خطایی در نمایش لیست کارها رخ داد.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("todo_done_"))
def handle_todo_done(call):
    """Mark a todo as done. Restricted to the creator or a group admin."""
    try:
        todo_id = int(call.data.split("_", 2)[2])
        todo = repo.get_todo_by_id(todo_id)
        if not todo:
            bot.answer_callback_query(call.id, "❌ این تسک یافت نشد.", show_alert=True)
            return

        authorized, reason = _user_is_authorized_for_todo(todo, call)
        if not authorized:
            bot.answer_callback_query(call.id, reason, show_alert=True)
            return

        if todo[6] == 1:  # status already done
            bot.answer_callback_query(call.id, "ℹ️ این تسک قبلاً انجام شده.", show_alert=True)
            return

        repo.mark_todo_done(todo_id)
        bot.answer_callback_query(call.id, "✅ تسک انجام شد!")

        chat_id_str = str(todo[1])
        text, todos = _build_todo_list_text(chat_id_str)
        if text and todos:
            markup = _build_todo_list_markup(todos)
            try:
                bot.edit_message_text(
                    text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                    parse_mode="Markdown", reply_markup=markup,
                )
            except Exception:
                pass
        else:
            try:
                bot.edit_message_text(
                    "📋 تمام تسک‌ها انجام شد! ✅\n\nاز `/todo [متن]` برای افزودن استفاده کنید.",
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                    parse_mode="Markdown",
                )
            except Exception:
                pass
    except Exception:
        log.error("TODO DONE CALLBACK ERROR", exc_info=True)
        bot.answer_callback_query(call.id, "❌ خطا رخ داد.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("todo_del_"))
def handle_todo_delete(call):
    """Delete a todo. Restricted to the creator or a group admin."""
    try:
        todo_id = int(call.data.split("_", 2)[2])
        todo = repo.get_todo_by_id(todo_id)
        if not todo:
            bot.answer_callback_query(call.id, "❌ این تسک یافت نشد.", show_alert=True)
            return

        authorized, reason = _user_is_authorized_for_todo(todo, call)
        if not authorized:
            bot.answer_callback_query(call.id, reason, show_alert=True)
            return

        chat_id_str = str(todo[1])
        repo.delete_todo(todo_id)
        bot.answer_callback_query(call.id, "🗑 تسک حذف شد!")

        text, todos = _build_todo_list_text(chat_id_str)
        if text and todos:
            markup = _build_todo_list_markup(todos)
            try:
                bot.edit_message_text(
                    text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                    parse_mode="Markdown", reply_markup=markup,
                )
            except Exception:
                pass
        else:
            try:
                bot.edit_message_text(
                    "📋 لیست کارها خالی است.\n\nاز `/todo [متن]` برای افزودن استفاده کنید.",
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                    parse_mode="Markdown",
                )
            except Exception:
                pass
    except Exception:
        log.error("TODO DELETE CALLBACK ERROR", exc_info=True)
        bot.answer_callback_query(call.id, "❌ خطا رخ داد.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("remind_snooze_"))
def handle_remind_snooze(call):
    """Snooze a triggered reminder by 15 minutes. Any member of the correct
    chat may click (collaborative by design) — IDOR-guarded against
    forwarded-keyboard cross-chat manipulation."""
    try:
        reminder_id = int(call.data.split("_", 2)[2])
        reminder = repo.get_reminder_by_id(reminder_id)
        if not reminder:
            bot.answer_callback_query(call.id, "❌ یادآوری یافت نشد.", show_alert=True)
            return

        authorized, reason = _user_is_authorized_for_reminder(reminder, call)
        if not authorized:
            bot.answer_callback_query(call.id, reason, show_alert=True)
            return

        new_trigger = time.time() + (15 * 60)
        repo.snooze_reminder(reminder_id, new_trigger)
        bot.answer_callback_query(call.id, "💤 15 دقیقه به تعویق افتاد.")

        try:
            bot.edit_message_text(
                f"💤 **یادآوری به تعویق افتاد**\n\n"
                f"📝 `{sanitize_backtick_span(reminder[3])}`\n"
                f"⏰ اجرای بعدی: {format_trigger_time_iran(new_trigger)}",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode="Markdown",
            )
        except Exception:
            pass
    except Exception:
        log.error("REMIND SNOOZE CALLBACK ERROR", exc_info=True)
        bot.answer_callback_query(call.id, "❌ خطا رخ داد.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("remind_dismiss_"))
def handle_remind_dismiss(call):
    """Dismiss a triggered reminder. Any member of the correct chat may
    click — IDOR-guarded against forwarded-keyboard cross-chat manipulation."""
    try:
        reminder_id = int(call.data.split("_", 2)[2])
        reminder = repo.get_reminder_by_id(reminder_id)
        if not reminder:
            bot.answer_callback_query(call.id, "❌ یادآوری یافت نشد.", show_alert=True)
            return

        authorized, reason = _user_is_authorized_for_reminder(reminder, call)
        if not authorized:
            bot.answer_callback_query(call.id, reason, show_alert=True)
            return

        recurrence = reminder[5]
        if recurrence in ("daily", "weekly"):
            offset = 86400 if recurrence == "daily" else 604800
            new_trigger = time.time() + offset
            repo.reschedule_reminder(reminder_id, new_trigger)
            rec_label = "daily" if recurrence == "daily" else "weekly"
            bot.answer_callback_query(call.id, f"✅ رد شد. یادآوری {rec_label} بعدی تنظیم شد.")
            try:
                bot.edit_message_text(
                    f"✅ **یادآوری رد شد**\n\n"
                    f"📝 `{sanitize_backtick_span(reminder[3])}`\n"
                    f"🔄 اجرای بعدی: {format_trigger_time_iran(new_trigger)}",
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                    parse_mode="Markdown",
                )
            except Exception:
                pass
        elif recurrence and recurrence not in ("none",):
            interval_sec = _parse_custom_interval_seconds(recurrence)
            if interval_sec and interval_sec >= 60:
                new_trigger = time.time() + interval_sec
                repo.reschedule_reminder(reminder_id, new_trigger)
                bot.answer_callback_query(call.id, f"✅ رد شد. یادآوری بعدی هر {recurrence} تنظیم شد.")
                try:
                    bot.edit_message_text(
                        f"✅ **یادآوری رد شد**\n\n"
                        f"📝 `{sanitize_backtick_span(reminder[3])}`\n"
                        f"🔄 اجرای بعدی: {format_trigger_time_iran(new_trigger)} (هر {recurrence})",
                        chat_id=call.message.chat.id, message_id=call.message.message_id,
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
            else:
                repo.dismiss_reminder(reminder_id)
                bot.answer_callback_query(call.id, "✅ یادآوری حذف شد.")
                try:
                    bot.edit_message_text(
                        f"✅ **یادآوری انجام شد و حذف شد**\n\n📝 `{sanitize_backtick_span(reminder[3])}`",
                        chat_id=call.message.chat.id, message_id=call.message.message_id,
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
        else:
            repo.dismiss_reminder(reminder_id)
            bot.answer_callback_query(call.id, "✅ یادآوری حذف شد.")
            try:
                bot.edit_message_text(
                    f"✅ **یادآوری انجام شد و حذف شد**\n\n📝 `{sanitize_backtick_span(reminder[3])}`",
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                    parse_mode="Markdown",
                )
            except Exception:
                pass
    except Exception:
        log.error("REMIND DISMISS CALLBACK ERROR", exc_info=True)
        bot.answer_callback_query(call.id, "❌ خطا رخ داد.")
