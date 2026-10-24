import logging

from bots import bot
from handlers.shared import is_admin
from services.debate import stop_all_debates
from services.streaming import _safe_reply_to

log = logging.getLogger("L-bot")


@bot.message_handler(commands=["stopall"])
def handle_stopall(message):
    try:
        if not is_admin(message.from_user.id):
            _safe_reply_to(bot, message, "⛔ You are not authorized.")
            return
        count = stop_all_debates()
        _safe_reply_to(bot, message, f"⏹ {count} مناظره متوقف شد.")
    except Exception:
        log.error("STOPALL ERROR", exc_info=True)
        _safe_reply_to(bot, message, "❌ خطا در توقف مناظره‌ها.")
