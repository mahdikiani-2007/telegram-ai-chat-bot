import logging
import time

from ai import agnes
from bots import bot
from db import repo
from limits import rate_limiter
from metrics import METRICS
from services import panel_settings as ps
from services.memory import (
    TLDR_MAX_CHARS,
    TLDR_MAX_MESSAGES,
    TLDR_PROMPT,
    TLDR_WINDOW_MINUTES,
    _mark_tldr_used,
    _tldr_cooldown_seconds,
    _tldr_on_cooldown,
)
from services.streaming import _safe_reply_to, escape_markdown

log = logging.getLogger("L-bot")


@bot.message_handler(commands=["tldr"])
def handle_tldr(message):
    """Summarize recent group messages. Default window: TLDR_WINDOW_MINUTES.
    `/tldr 127` overrides the window with "the last 127 messages" instead.
    NOTE: registered BEFORE handle_all_messages (which is at line ~6360, so
    this must stay above it) — telebot is first-match-wins and the catch-all
    chat handler would otherwise swallow this command."""
    try:
        if message.chat.type == "private":
            _safe_reply_to(bot, message, "این دستور برای گروه‌هاست — توی پیوی چیزی برای خلاصه کردن نیست.")
            return

        chat_id_str = str(message.chat.id)

        # Optional message-count argument: /tldr 127 → last 127 messages.
        limit_n = None
        parts = (message.text or "").split()
        if len(parts) > 1:
            if not parts[1].isdigit():
                _safe_reply_to(bot, message,
                    "❌ فرمت: `/tldr` (خلاصه‌ی ۳ ساعت اخیر) یا `/tldr <تعداد>` مثل `/tldr 127`",
                    parse_mode="Markdown")
                return
            limit_n = int(parts[1])
            if not (1 <= limit_n <= TLDR_MAX_MESSAGES):
                _safe_reply_to(bot, message,
                    f"❌ تعداد باید بین ۱ تا {TLDR_MAX_MESSAGES} باشه.",
                    parse_mode="Markdown")
                return

        # Rate limit: global bucket + per-group cooldown.
        if not rate_limiter.allow("chat", message.from_user.id)[0]:
            _safe_reply_to(bot, message, "درخواست‌های شما بیش از حد مجاز است. کمی صبر کنید.")
            return

        if not ps.feature_gate_message(bot, message, "tldr", explicit=True):
            return
        if _tldr_on_cooldown(chat_id_str):
            _safe_reply_to(bot, message,
                f"⏳ یه خلاصه همین الانا گرفتن — {int(_tldr_cooldown_seconds() // 60)} دقیقه دیگه دوباره امتحان کن.")
            return

        placeholder = _safe_reply_to(bot, message, "🔍 در حال بررسی پرونده‌ی گفتگوها...")

        # Transcript source: the group chat_log buffer (EVERYONE's messages,
        # logged by the middleware — not just bot-directed turns). With a
        # count argument: exactly the last N logged messages. Without:
        # everything in the default time window.
        if limit_n is not None:
            rows = repo.get_chat_log_window(chat_id_str, hours=48, limit=limit_n)
        else:
            rows = repo.get_chat_log_window(chat_id_str, hours=TLDR_WINDOW_MINUTES // 60 + 1,
                                            limit=TLDR_MAX_MESSAGES)
        # Cap total size, oldest-first ordering preserved by get_chat_log_window.
        rows = rows[-TLDR_MAX_MESSAGES:]

        transcript_lines = []
        total_chars = 0
        for uid, fname, text, ts in rows:
            text = (text or "").strip()
            if not text or text.startswith("[tldr-cooldown]"):
                continue
            name = fname or f"user{uid}"
            transcript_lines.append(f"{name}: {text}")
            total_chars += len(text)
            if total_chars >= TLDR_MAX_CHARS:
                break

        # Bots' own debate/media chatter and non-human rows are already tagged;
        # if the window has too little substance, say so instead of calling the model.
        if len(transcript_lines) < 5 or total_chars < 300:
            try:
                bot.edit_message_text(
                    "کمتر از حدی که بشه ازش نتیجه‌ی معناداری کشید حرف زده شده. "
                    "بعداً دوباره امتحان کن.",
                    chat_id=message.chat.id, message_id=placeholder.message_id,
                )
            except Exception:
                pass
            return

        transcript = "\n".join(transcript_lines)[-TLDR_MAX_CHARS:]
        scope_desc = (f"THE LAST {limit_n} MESSAGES" if limit_n is not None
                      else f"THE LAST {TLDR_WINDOW_MINUTES} MINUTES")
        summary = agnes.generate([
            {"role": "system", "content": TLDR_PROMPT},
            {"role": "user", "content": f"TRANSCRIPT OF {scope_desc}:\n\n{transcript}"},
        ])

        if not summary or not summary.strip():
            try:
                bot.edit_message_text(
                    "❌ نتونستم الان خلاصه‌ی قابل‌اعتمادی بسازم. کمی بعد دوباره امتحان کن.",
                    chat_id=message.chat.id, message_id=placeholder.message_id,
                )
            except Exception:
                pass
            METRICS["llm_errors"] += 1
            return

        if limit_n is not None:
            window_label = f"{len(transcript_lines)} پیام اخیر"
        else:
            hours = TLDR_WINDOW_MINUTES // 60
            window_label = f"{hours} ساعت گذشته" if hours else f"{TLDR_WINDOW_MINUTES} دقیقه گذشته"
        full = f"📋 **خلاصه‌ی {window_label}:**\n\n{summary.strip()}"
        _mark_tldr_used(chat_id_str)
        METRICS["tldr_requests"] = METRICS.get("tldr_requests", 0) + 1

        try:
            bot.edit_message_text(full[:4000], chat_id=message.chat.id,
                                  message_id=placeholder.message_id, parse_mode="Markdown")
        except Exception:
            bot.edit_message_text(full[:4000], chat_id=message.chat.id,
                                  message_id=placeholder.message_id)
    except Exception:
        log.error("TLDR ERROR", exc_info=True)
        try:
            _safe_reply_to(bot, message, "❌ خطا در خلاصه‌گیری. دوباره تلاش کن.")
        except Exception:
            pass

# =========================
# /myfacts + /forget — user-controlled long-term memory
# =========================
@bot.message_handler(commands=["myfacts"])
def handle_myfacts(message):
    """Show what the bot has durably learned about the requester in THIS
    chat's scope (private facts in DMs; group facts + private facts in groups)."""
    try:
        facts = repo.list_user_facts_scoped(message.from_user.id,
                                            source_chat_id=str(message.chat.id))
        if not facts:
            _safe_reply_to(bot, message,
                "هنوز چیزی درباره‌ات توی حافظه‌ی دائمی‌ام ثبت نکردم. "
                "هر چیزی درباره‌ی خودت بگی (شغل، علایق، پروژه‌ها...) یاد می‌گیرم و همین‌طور حفظ می‌کنم.")
            return
        lines = [f"🧠 چیزهایی که درباره‌ات یاد گرفتم ({len(facts)} مورد):\n"]
        for i, f in enumerate(facts, 1):
            lines.append(f"{i}. {escape_markdown(f)}")
        lines.append("\nحذف یکی: `/forget <شماره>` — پاک کردن همه: `/forget all`")
        _safe_reply_to(bot, message, "\n".join(lines), parse_mode="Markdown")
    except Exception:
        log.error("MYFACTS ERROR", exc_info=True)
        _safe_reply_to(bot, message, "❌ خطا در نمایش حافظه.")

@bot.message_handler(commands=["forget"])
def handle_forget(message):
    """Delete one fact by its /myfacts number, or wipe the whole profile."""
    try:
        parts = (message.text or "").split(maxsplit=1)
        arg = parts[1].strip().lower() if len(parts) > 1 else ""
        user_id = message.from_user.id

        if not arg:
            _safe_reply_to(bot, message,
                "❌ فرمت: `/forget <شماره>` (شماره از `/myfacts`) یا `/forget all` برای پاک کردن همه.",
                parse_mode="Markdown")
            return

        if arg in ("all", "همه"):
            count = repo.delete_all_user_facts(user_id)
            _safe_reply_to(bot, message,
                f"🧹 {count} مورد از حافظه‌ی دائمی‌ات پاک شد. الان هیچی درباره‌ات نمی‌دانم.")
            return

        if not arg.isdigit():
            _safe_reply_to(bot, message, "❌ شماره معتبر نیست. لیست: `/myfacts`", parse_mode="Markdown")
            return

        # Numbering must match /myfacts in THIS chat's scope.
        facts = repo.list_user_facts_scoped(user_id, source_chat_id=str(message.chat.id))
        idx = int(arg)
        if idx < 1 or idx > len(facts):
            _safe_reply_to(bot, message,
                f"❌ شماره خارج از محدوده است (۱ تا {len(facts)}). لیست: `/myfacts`",
                parse_mode="Markdown")
            return

        fact = facts[idx - 1]
        if repo.delete_user_fact(user_id, fact):
            _safe_reply_to(bot, message, f"🗑 فراموش شد: {escape_markdown(fact)}")
        else:
            _safe_reply_to(bot, message, "❌ حذف نشد — دوباره تلاش کن.")
    except Exception:
        log.error("FORGET ERROR", exc_info=True)
        _safe_reply_to(bot, message, "❌ خطا در حذف از حافظه.")
