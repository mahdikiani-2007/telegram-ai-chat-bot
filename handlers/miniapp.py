"""Mini App games entry: the /play command.

Private chats get a native web_app button (opens the Mini App integrated,
global leaderboard). Groups get a t.me URL button carrying
`?startapp=g<chat_id>` — Telegram signs start_param into initData, which is
how the server scopes the leaderboard to THIS group (inline web_app buttons
are private-only per Bot API, so the URL button is the group mechanism).
"""
import logging

from telebot import types

from bots import bot, get_bot_identity
from config import MINIAPP_PUBLIC_URL
from services import panel_settings as ps
from services.streaming import _safe_reply_to

log = logging.getLogger("L-bot")

_PLAY_TEXT = (
    "🎮 **بازی‌های مینی‌اپ**\n\n"
    "👑 **حکم آنلاین**: ۲ نفره، ۳ نفره و ۴ نفره تیمی با ربات هوشمند، انتخاب حکم و اتاق دوستان\n"
    "♟️ **شطرنج آنلاین دونفره**: رقابت زنده در زمان واقعی، ساعت مسابقه‌ای و لینک دعوت دوستان\n"
    "🐍 **مار نئون**: با چهار حالت، پاورآپ و جدول رتبه‌بندی سراسری و گروهی\n\n"
    "دکمه رو بزن تا وارد بشی:"
)


@bot.message_handler(commands=["play"])
def handle_play(message):
    try:
        if not ps.feature_gate_message(bot, message, "miniapp", explicit=True):
            return
        if not MINIAPP_PUBLIC_URL:
            _safe_reply_to(
                bot, message,
                "🎮 بازی‌ها هنوز راه نیفتاده — دامنه عمومی سرور تنظیم نشده.\n"
                "بعداً دوباره امتحان کن یا به ادمین بگو MINIAPP_PUBLIC_URL رو ست کنه.")
            return

        markup = types.InlineKeyboardMarkup()
        if message.chat.type == "private":
            markup.add(types.InlineKeyboardButton(
                "🎮 باز کردن بازی‌ها",
                web_app=types.WebAppInfo(url=MINIAPP_PUBLIC_URL)))
        else:
            identity = get_bot_identity()
            username = (identity.get("username") or "").strip()
            if not username:
                log.error("/play: bot username unknown — cannot build startapp URL")
                _safe_reply_to(bot, message, "❌ موقتاً در دسترس نیست. کمی بعد دوباره امتحان کن.")
                return
            markup.add(types.InlineKeyboardButton(
                "🎮 باز کردن بازی‌ها",
                url=f"https://t.me/{username}?startapp=g{message.chat.id}"))
        _safe_reply_to(bot, message, _PLAY_TEXT, parse_mode="Markdown",
                       reply_markup=markup)
    except Exception:
        log.error("PLAY COMMAND ERROR", exc_info=True)
        _safe_reply_to(bot, message, "❌ خطا در باز کردن بازی‌ها.")
