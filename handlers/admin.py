import datetime
import logging
import time
import traceback

from bots import admin_notifier, bot
from db import fetch_all_group_ids, fetch_all_user_ids
from handlers.shared import is_admin, is_owner
from metrics import METRICS, _START_TIME
from services.debate import _active_debates, _debate_lock, stop_all_debates
from services.digest import broadcast_daily_digest
from services.streaming import _safe_reply_to

log = logging.getLogger("L-bot")



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
    """Owner-only, private-chat-only: run today's digest cycle right now
    (modes, ledger, budget — same path as the scheduler). Restricted to
    private chat so this can't be triggered (and re-broadcast to every
    group) by anyone typing /digest inside a group the bot is in."""
    try:
        if not is_owner(message.from_user.id):
            _safe_reply_to(bot, message, "⛔ You are not authorized to use this command.")
            return
        if message.chat.type != "private":
            _safe_reply_to(bot, message, "این دستور فقط داخل پیوی با ادمین کار می‌کند.")
            return

        from services.digest import run_digest_cycle

        _safe_reply_to(bot, message, "⏳ در حال ساخت و ارسال دایجست...")
        report = run_digest_cycle(triggered_by=f"manual:{message.from_user.id}")

        std_text = report["texts"].get("standard", "")
        if not std_text and not report["sent"]:
            bot.send_message(
                message.chat.id,
                "❌ نتونستم دایجست بسازم — یا `TAVILY_API_KEY` ست نیست، یا سقف روزانه‌ی سرچ تموم شده، "
                "یا مدل جواب نداد. لاگ سرور رو چک کن.",
            )
            return

        est, consumed = report["estimate"], report["consumed"]
        est_line = f"تخمین: {est['standard']} + {sum(est['profiles'].values())} سرچ"
        use_line = (f"مصرف: قبل {consumed['before']} → بعد {consumed['after']}"
                    if consumed["before"] is not None else "مصرف: بدون سقف")
        text = (
            f"✅ دایجست اجرا شد.\n\n"
            f"📤 ارسال موفق: {report['sent']}\n"
            f"❌ ناموفق: {report['failed']}\n"
            f"🚫 غیرقابل دسترس: {report['unreachable']}\n"
            f"⏭ خاموش: {report['skipped_off']}, بودجه: {report['skipped_budget']}, "
            f"بی‌نتیجه: {report['skipped_no_results']}\n"
            "ℹ️ گروه‌هایی که امروز رکورد ledger ندارند، در این اجرا دایجست دریافت می‌کنند؛ "
            "وضعیت‌های ناموفق و skip نهایی نیستند و در اجرای دستی بعدی قابل تلاش‌اند.\n"
            f"🔎 {est_line} | {use_line}\n\n"
            f"---\n\n{std_text}"
        )
        bot.send_message(message.chat.id, text[:4000], parse_mode="Markdown")
    except Exception:
        log.error("DIGEST COMMAND ERROR", exc_info=True)
        admin_notifier.notify("digest_command_error", traceback.format_exc(limit=5))
        _safe_reply_to(bot, message, "❌ خطایی در اجرای دستور دایجست رخ داد.")
