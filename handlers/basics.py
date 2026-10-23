import logging
import re
import time
import traceback

from telebot import types

from ai import agnes
from bots import admin_notifier, bot
from db import repo
from handlers.shared import (
    _resolve_private_session,
    is_admin,
)
from infra import HTTP
from limits import rate_limiter
from metrics import METRICS
from services import panel_settings as ps
from services.streaming import (
    _handle_stop_generic,
    _safe_reply_to,
    escape_markdown,
    rate_limited_reply,
    sanitize_backtick_span,
)

log = logging.getLogger("L-bot")


# =========================
# Commands
# =========================
# /reset keeps its meaning for GROUPS (admin wipes the shared group memory).
# In private chats it now routes to the session system: resetting a session
# IS deleting it (user's decision) — pointers the user to /session delete.
@bot.message_handler(commands=["reset"])
def handle_reset(message):
    try:
        if message.chat.type == "private":
            _safe_reply_to(bot,
                message,
                "🗣 پرونده‌ها الان سشنی شدند.\n\n"
                "• حذف گفتوگوی فعلی یا هر گفتوگوی دیگر: `/session delete <آیدی>` (آیدی از `/session`)\n"
                "• شروع تازه بدون پاک کردن قدیمی‌ها: `/new`",
            )
            return
        user_status = bot.get_chat_member(
            message.chat.id, message.from_user.id
        ).status
        if user_status not in ["administrator", "creator"]:
            _safe_reply_to(bot,
                message,
                "شما صلاحیت دسترسی به این پرونده و پاکسازی آن را ندارید. فقط مدیران مجاز هستند.",
            )
            return
        repo.clear_chat(message.chat.id)
        _safe_reply_to(bot,
            message,
            "تمام مدارک، شواهد و حافظه این گفتوگو پاکسازی شد. پرونده از نو باز میشود.",
        )
    except Exception:
        log.error("RESET ERROR", exc_info=True)
        _safe_reply_to(bot, message, "خطایی در پروتکل پاکسازی دادهها رخ داد.")

# =========================
# /stop — cancel in-flight LLM processing for THIS user in THIS chat.
# Private chat: plain /stop, no reply needed. Groups: MUST reply to the
# bot's "در حال پردازش..." placeholder — that identifies WHICH run to stop
# (only the run's owner or a group admin may stop it). Nothing in flight →
# friendly no-op. Shared implementation: _handle_stop_generic (bot_light
# registers its own /stop to the same function).
# =========================
@bot.message_handler(commands=["stop"])
def handle_stop(message):
    _handle_stop_generic(bot, message)

@bot.message_handler(commands=["pic"])
def handle_image_generation(message):
    processing_msg = None
    try:
        command_parts = message.text.split(maxsplit=1)
        if len(command_parts) < 2:
            _safe_reply_to(bot, 
                message,
                "درخواست ناقص است. پرامپت خود را بعد از دستور /pic وارد کنید.",
                parse_mode="Markdown",
            )
            return

        if not rate_limited_reply(message, "pic"):
            return

        if not ps.feature_gate_message(bot, message, "pic", explicit=True):
            return

        prompt = command_parts[1].strip()
        bot.send_chat_action(message.chat.id, "upload_photo")
        processing_msg = _safe_reply_to(bot, 
            message, "در حال پردازش دادههای تصویری... منتظر بمانید."
        )

        response = agnes.generate_image(prompt)
        response.encoding = "utf-8"
        if response.status_code != 200:
            log.warning("/pic image API non-200: %d — %s", response.status_code, response.text[:300])
            raise Exception(f"Image API returned {response.status_code}")
        response_data = response.json()

        if "data" in response_data and len(response_data["data"]) > 0:
            image_url = response_data["data"][0]["url"]
            img_response = HTTP.get(image_url, timeout=30)
            if img_response.status_code == 200:
                image_bytes = img_response.content
                try:
                    bot.edit_message_text(
                        text="دیتا دریافت شد. تصویر نهایی آماده است.",
                        chat_id=message.chat.id,
                        message_id=processing_msg.message_id,
                    )
                except Exception:
                    pass
                display_prompt = prompt if len(prompt) <= 900 else prompt[:900] + "..."
                # sanitize_backtick_span: a raw backtick in the prompt would
                # terminate the code span and make sendPhoto 400 (can't parse
                # entities) — after the image was already paid for.
                caption_text = f"خروجی پردازش تصویر:\n\n`{sanitize_backtick_span(display_prompt)}`"
                bot.send_photo(
                    chat_id=message.chat.id,
                    photo=image_bytes,
                    reply_to_message_id=message.message_id,
                    caption=caption_text,
                    parse_mode="Markdown",
                )
            else:
                raise Exception("Failed to download image from Agnes URL")
        else:
            if processing_msg:
                bot.edit_message_text(
                    text="خطا در لایه سرور. امکان جنریت تصویر وجود ندارد.",
                    chat_id=message.chat.id,
                    message_id=processing_msg.message_id,
                )
    except Exception:
        log.error("IMAGE GENERATION ERROR", exc_info=True)
        METRICS["llm_errors"] += 1
        admin_notifier.notify("image_generation_error", traceback.format_exc(limit=5))
        if processing_msg:
            try:
                bot.edit_message_text(
                    text="خطای سیستمی در آپلود یا ساخت تصویر رخ داده است.",
                    chat_id=message.chat.id,
                    message_id=processing_msg.message_id,
                )
            except Exception:
                pass
        else:
            _safe_reply_to(bot, message, "اختلال در پروتکل ساخت تصویر.")

@bot.message_handler(commands=["start"])
def handle_start(message):
    try:
        start_text = (
            "من ال هستم.\n"
            "یک ماتریکس تحلیلی برای پردازش دادهها، بررسی اسکریپتهای فنی و آنالیز منطق.\n\n"
            "پروندهٔ گفتوگو باز شده است؛ فرضیات، کدهای برنامهنویسی یا مدارک تصویری خود را ارسال کنید "
            "تا ارزیابی را شروع کنم. نرخ موفقیت ما به دقت دادههای شما بستگی دارد.\n\n"
            "🔹 /session — فهرست گفتوگوها (سشنها)\n"
            "🔹 /new — گفتوگوی جدید\n"
            "🔹 /session delete <آیدی> — حذف یک گفتگو"
        )
        _safe_reply_to(bot, message, start_text)
    except Exception:
        log.error("START COMMAND ERROR", exc_info=True)

@bot.message_handler(commands=["session"])
def handle_session_command(message):
    """Private chat only.
      /session                → list sessions (active marked, IDs copyable)
      /session <id>           → switch to that session
      /session <id> <title>   → switch AND rename that session
      /session delete <id>    → permanently delete that session
    Groups say so and do nothing — group memory is shared per-group by design."""
    try:
        if message.chat.type != "private":
            _safe_reply_to(bot, message, "این دستور فقط توی پیوی کار می‌کند.")
            return
        user_id = message.from_user.id

        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 2 or not parts[1].strip():
            _send_session_list(message)
            return

        sub = parts[1].strip().lower()

        if sub == "delete":
            target = parts[2].strip().lower() if len(parts) > 2 else ""
            if not re.fullmatch(r"[a-z0-9]{4,16}", target):
                _safe_reply_to(bot, message, "❌ فرمت درست: `/session delete <آیدی>` — آیدی را از `/session` کپی کن.", parse_mode="Markdown")
                return
            if repo.delete_session(user_id, target):
                active = repo.get_active_session(user_id)
                if active and active != "deleted":
                    after = f"گفتوگوی فعال فعلی: `{active}`."
                else:
                    after = "الان گفتوگوی فعالی نداری — اولین پیام بعدی یک پروندهٔ تازه می‌سازد."
                _safe_reply_to(bot, message, f"🗑 گفتوگوی `{target}` و همهٔ حافظهاش حذف شد.\n\n{after}", parse_mode="Markdown")
            else:
                _safe_reply_to(bot, message, "❌ چنین گفتویی با این آیدی نداری. لیست: `/session`", parse_mode="Markdown")
            return

        # /session <id> <title> → rename (and stay on the current session —
        # renaming a non-active one while talking in another would confuse
        # the user; switching too is more predictable).
        new_title = parts[2].strip() if len(parts) > 2 else ""
        if new_title:
            if not re.fullmatch(r"[a-z0-9]{4,16}", sub):
                _safe_reply_to(bot, message, "❌ آیدی نامعتبر. آیدی را از `/session` کپی کن.", parse_mode="Markdown")
                return
            if not repo.session_exists(user_id, sub):
                _safe_reply_to(bot, message, "❌ چنین گفتویی با این آیدی نداری. لیست: `/session`", parse_mode="Markdown")
                return
            title = new_title.strip()[:80]
            repo.rename_session(user_id, sub, title)
            _safe_reply_to(bot, message, f"✏️ عنوان گفتوگوی `{sub}` به «{escape_markdown(title)}» تغییر کرد.", parse_mode="Markdown")
            return

        if repo.set_active_session(user_id, sub):
            rows = {r[0]: r[1] for r in repo.list_sessions(user_id, SESSION_MAX_PER_USER)}
            title = rows.get(sub, "")
            title_suffix = f" — «{escape_markdown(title)}»" if title else ""
            _safe_reply_to(bot, message, f"✅ سوییچ شد به گفتوگوی `{sub}`{title_suffix}\n\nاز این به بعد پیامهایت همین‌جا ادامه پیدا می‌کند.", parse_mode="Markdown")
        else:
            _safe_reply_to(bot, message, "❌ چنین گفتویی با این آیدی نداری. لیست: `/session`", parse_mode="Markdown")
    except Exception:
        log.error("SESSION COMMAND ERROR", exc_info=True)
        _safe_reply_to(bot, message, "❌ خطا در پردازش دستور سشن.")

@bot.message_handler(commands=["new"])
def handle_new_session(message):
    """Start a fresh session and switch to it (private chats). Old sessions
    stay switchable via /session."""
    try:
        if message.chat.type != "private":
            _safe_reply_to(bot, message, "این دستور فقط توی پیوی معنا دارد.")
            return
        user_id = message.from_user.id
        count = repo.count_sessions(user_id)
        if count >= SESSION_MAX_PER_USER:
            _safe_reply_to(bot, message,
                f"⚠️ سقف {SESSION_MAX_PER_USER} گفتوگو پر شده. یکی را با `/session delete <آیدی>` حذف کن و دوباره امتحان کن.",
                parse_mode="Markdown")
            return
        session_id = _new_session_id()
        with _SESSION_ACTIVE_UPDATE_LOCK:
            repo.create_session(user_id, session_id, "")
            repo.set_active_session(user_id, session_id)
        _safe_reply_to(bot, message,
            f"🗂 پروندهٔ جدید باز شد.\nآیدی: `{session_id}`\n\n"
            "اولین پیامت عنوانش می‌شود. گفتوگوهای قبلی با `/session` در دسترس‌اند.",
            parse_mode="Markdown",
        )
    except Exception:
        log.error("NEW SESSION ERROR", exc_info=True)
        _safe_reply_to(bot, message, "❌ خطا در ایجاد گفتوگوی جدید.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("sessw_"))
def handle_session_switch(call):
    """Inline-keyboard switch. Format: sessw_{owner_user_id}_{session_id}."""
    try:
        parts = call.data.split("_", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "❌ Invalid callback data.")
            return
        owner_id = int(parts[1])
        session_id = parts[2]
        if call.from_user.id != owner_id:
            bot.answer_callback_query(call.id, "⛔ فقط صاحب این گفتوگو می‌تواند سوئیچ کند.", show_alert=True)
            return
        if repo.set_active_session(owner_id, session_id):
            bot.answer_callback_query(call.id, "✅ سوئیچ شد.")
            try:
                bot.edit_message_reply_markup(
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                )  # clear the now-stale buttons
            except Exception:
                pass
            bot.send_message(call.message.chat.id, f"✅ روی گفتوگوی `{session_id}` سوییچ شدید.", parse_mode="Markdown")
        else:
            bot.answer_callback_query(call.id, "❌ این گفتگو حذف شده.", show_alert=True)
    except Exception:
        log.error("SESSION SWITCH ERROR", exc_info=True)
        bot.answer_callback_query(call.id, "❌ خطا رخ داد.")

def _build_session_list(rows, active_id, owner_id):
    text_lines = ["🗂 **گفتوگوهای شما**\n"]
    markup = types.InlineKeyboardMarkup(row_width=1)
    for sid, title, created_at in rows:
        marker = "▶️" if sid == active_id else "•"
        label = f"{marker} {title or 'بدون عنوان'}"[:60]
        text_lines.append(f"`{sid}` {marker} {escape_markdown(title or 'بدون عنوان')}")
        markup.add(types.InlineKeyboardButton(label, callback_data=f"sessw_{owner_id}_{sid}"))
    text_lines.append("")
    text_lines.append("سوییچ: `/session <آیدی>` • جدید: `/new` • حذف: `/session delete <آیدی>`")
    return "\n".join(text_lines), markup

def _send_session_list(message):
    user_id = message.from_user.id
    rows = repo.list_sessions(user_id, SESSION_MAX_PER_USER)
    if not rows:
        _safe_reply_to(bot, message,
            "هنوز گفتوگویی نداری. هر پیامی بفرستی اولین پرونده ساخته می‌شود — یا با `/new` یکی بساز.",
            parse_mode="Markdown")
        return
    active = repo.get_active_session(user_id)
    text, markup = _build_session_list(rows, active, user_id)
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=markup)
