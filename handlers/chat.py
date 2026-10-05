import base64
import logging
import random
import re
import time
import traceback

from ai import agnes
from bots import (
    admin_notifier,
    bot,
    bot_light,
    get_bot_identity,
    get_bot_light_identity,
)
from config import TELEGRAM_FILE_BASE_URL, TELEGRAM_TOKEN
from db import repo
from handlers.shared import (
    _resolve_private_session,
    extract_text_from_message,
)
from infra import HTTP
from limits import rate_limiter, spam_tracker
from metrics import METRICS
from services import panel_settings as ps
from services.debate import (
    extract_debate_topic,
    is_debate_active,
    is_debate_trigger,
    start_debate,
    stop_all_debates,
)
from services.memory import (
    FACT_GROUP_MIN_LEN,
    FACT_MIN_TEXT_LEN,
    _inject_user_profile,
    build_context,
    build_context_light,
    fact_extractor,
    maybe_summarize,
)
from services.search import inject_search_context, maybe_web_search_with_status
from services.streaming import (
    _consume_active_request,
    _handle_stop_generic,
    _register_active_request,
    _safe_reply_to,
    finalize_message,
    finalize_message_generic,
    rate_limited_reply,
    stream_to_telegram,
    stream_to_telegram_generic,
)
from timeutil import iran_now

log = logging.getLogger("L-bot")


def should_respond(message):
    """Private chat OR group-with-mention OR group-reply-to-bot OR group-with-name-call. Else silent."""
    try:
        if message.chat.type == "private":
            return True
        identity = get_bot_identity()
        bot_username, bot_id = identity["username"], identity["id"]
        if not bot_username or not bot_id:
            return False
        raw_text = extract_text_from_message(message)
        is_mentioned = f"@{bot_username}" in raw_text.lower()
        is_reply_to_bot = bool(
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == bot_id
        )
        if raw_text:
            stripped = raw_text.strip()
            if stripped == "L" or stripped == "ال":
                return True
            if stripped.startswith("L"):
                if len(stripped) == 1 or stripped[1] in ' \t\n\r\f\v':
                    return True
            elif stripped.startswith("ال"):
                if len(stripped) == 2 or stripped[2] in ' \t\n\r\f\v':
                    return True
        return is_mentioned or is_reply_to_bot
    except Exception:
        log.error("should_respond error", exc_info=True)
        return False

# =========================
# Name Call Detection Responses
# =========================
PERSIAN_NAME_RESPONSES = [
    "بله؟ چه خبره؟", "در خدمتتم.", "گوشم به حرفت هست.", "چی میخوای؟", "بفرما.",
    "حاضرم.", "چه کاره‌ای؟", "میگم؟", "بله، دارم گوش میدم.", "چطور میتونم کمکت کنم؟",
    "بگو چی شده؟", "وسط یک پرونده مهم هستم، کاری پیش اومده؟", "دقیق و سریع در خدمتتم.",
    "چه خبرا؟", "حرفتو بزن.", "گفتنته؟", "بله، چی؟", "در انتظار دستورت هستم.",
    "چه میخوای بگی؟", "گوش دارم.", "چی؟", "بله، چی شده؟", "در خدمت شما هستم.",
    "بفرماید.", "چه کار دارید؟", "حاضر و آماده‌ام.", "چطور میتونم کمک کنم؟",
    "بگو چی میخوای؟", "گوش میدم.", "چه خبره؟", "بله؟", "در خدمت شما.",
    "چه میخوای؟", "حرفتو بزن.", "گفتنته؟",
]

def is_name_call(text):
    """Check if the text is exactly 'L' or 'ال' or starts with 'L ' or 'ال '."""
    if not text:
        return False, None

    stripped = text.strip()
    if stripped == "L" or stripped == "ال":
        return True, stripped

    if stripped.startswith("L"):
        if len(stripped) == 1 or stripped[1] in ' \t\n\r\f\v':
            return True, "L"
    elif stripped.startswith("ال"):
        if len(stripped) == 2 or stripped[2] in ' \t\n\r\f\v':
            return True, "ال"

    return False, None

# =========================
# Chat & Memory System (summarization + streaming)
# =========================
@bot.message_handler(content_types=["text", "photo"])
def handle_all_messages(message):
    if message.text and message.text.startswith("/"):
        return

    if not should_respond(message):
        return

    # Replying to a media message the bot re-uploaded (Instagram download,
    # etc.) is a reply about the MEDIA, not something the bot said — running
    # it through the AI pipeline just produces an irrelevant answer and
    # burns a model call for nothing.
    if message.reply_to_message and repo.is_bot_media_message(str(message.chat.id), message.reply_to_message.message_id):
        return

    try:
        identity = get_bot_identity()
        bot_username = identity["username"] or ""
        raw_text = extract_text_from_message(message)
        turn_born = time.time()  # resurrection guard: post-reset saves are dropped

        if message.content_type == "text" and raw_text and is_debate_trigger(raw_text):
            # Ambient trigger: silent in groups when disabled, rate-limited
            # notice in private; admins exempt. No fall-through (handled).
            if not ps.feature_gate_message(bot, message, "debate", explicit=False,
                                           chat_id=str(message.chat.id)):
                return
            topic = extract_debate_topic(raw_text)
            if topic:
                start_debate(message, topic)
                return

        # Bot-to-Bot Communication Mode (Bot API 10.x) means L's own polling
        # now receives Light's debate replies as regular incoming messages
        # (they're replies to L's own messages, which is exactly one of the
        # two delivery conditions Telegram documents for that mode). Without
        # this guard, L's normal chat pipeline — full AI call, streaming,
        # Rich Message heading — fires on every debate turn as if a human
        # had replied, producing a stray extra message alongside the real,
        # scripted debate reply. Checked AFTER the trigger check above, so a
        # human trying to start a second debate mid-debate still gets
        # start_debate's own "already in progress" notice instead of being
        # silently ignored. Light's handler already had this same guard.
        if is_debate_active(str(message.chat.id)):
            return

        if message.content_type == "text" and raw_text:
            is_call, name_used = is_name_call(raw_text)
            if is_call:
                stripped = raw_text.strip()
                if stripped == "L" or stripped == "ال":
                    response = random.choice(PERSIAN_NAME_RESPONSES)
                    _safe_reply_to(bot, message, response)
                    return
                if stripped.startswith("L ") or stripped.startswith("L\t") or stripped.startswith("L\n"):
                    clean_text = stripped[1:].lstrip()
                elif stripped.startswith("ال ") or stripped.startswith("ال\t") or stripped.startswith("ال\n"):
                    clean_text = stripped[2:].lstrip()
                else:
                    clean_text = stripped
                raw_text = clean_text

        spam_key = f"{message.chat.id}_{message.from_user.id}"
        action, value = spam_tracker.check(spam_key)
        if action == "muted":
            _safe_reply_to(bot, 
                message,
                f"{value} ثانیه دیگر من تو را به خاطر زیاده گویی هایت نادیده میگیرم",
            )
            return
        if action == "warn":
            _safe_reply_to(bot, message, "رفتار تو تمرکز من را به هم میزند، ۱ دقیقه سکوت کن.")
            return

        if not ps.feature_gate_message(bot, message, "chat", explicit=False,
                                       chat_id=str(message.chat.id)):
            return

        category = "vision" if message.content_type == "photo" else "chat"
        if not rate_limited_reply(message, category):
            return

        clean_text = raw_text
        if bot_username and f"@{bot_username}" in clean_text.lower():
            clean_text = re.sub(f"@{bot_username}", "", clean_text, flags=re.IGNORECASE)
        clean_text = clean_text.strip()
        if message.content_type == "photo" and not clean_text:
            clean_text = "این تصویر را آنالیز کن و تمام جزئیات منطقیاش را بیرون بکش."

        memory_id = str(message.chat.id)
        session_id = None
        if message.chat.type == "private":
            session_id, memory_id = _resolve_private_session(message.from_user.id, clean_text)

        # Long-term memory: queue this turn for fact extraction (private AND
        # group — groups get a higher length bar to skip noise).
        if message.content_type == "text" and clean_text:
            group_bar = FACT_GROUP_MIN_LEN if message.chat.type != "private" else 0
            if len(clean_text.strip()) >= max(FACT_MIN_TEXT_LEN, group_bar):
                fact_extractor.submit(message.chat.id, message.from_user, clean_text)

        user_first_name = message.from_user.first_name or "مظنون"
        tagged_user_text = f"[{user_first_name}]: {clean_text}"

        image_part = None
        if message.content_type == "photo":
            bot.send_chat_action(message.chat.id, "typing")
            try:
                file_id = message.photo[-1].file_id
                file_info = bot.get_file(file_id)
                raw_content = None

                try:
                    direct_url = TELEGRAM_FILE_BASE_URL.format(TELEGRAM_TOKEN, file_info.file_path)
                    img_response = HTTP.get(direct_url, timeout=30)
                    if img_response.status_code == 200 and len(img_response.content) > 100:
                        raw_content = img_response.content
                        log.info("Photo downloaded: %d bytes for chat %s",
                                 len(raw_content), message.chat.id)
                    else:
                        log.error("Photo download failed: status=%d, size=%d",
                                  img_response.status_code, len(img_response.content))
                except Exception as direct_err:
                    log.error("Photo download error: %s", direct_err)

                if raw_content is not None:
                    encoded_image = base64.b64encode(raw_content).decode("utf-8")
                    image_part = {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{encoded_image}"},
                    }
                else:
                    log.error("Photo download failed for chat %s", message.chat.id)
                    _safe_reply_to(bot, message, "⚠️ قادر به دریافت تصویر نیستم. لطفاً دوباره تلاش کنید.")
                    return

            except Exception as photo_err:
                log.error("Photo download exception: %s", photo_err, exc_info=True)
                _safe_reply_to(bot, message, "⚠️ خطا در دریافت تصویر. لطفاً دوباره تلاش کنید.")
                return

        user_message_saved = ps.save_memory_guarded(
            memory_id, "user", tagged_user_text, message.from_user.id, turn_born)

        bot.send_chat_action(message.chat.id, "typing")
        placeholder = _safe_reply_to(bot, message, "در حال پردازش...")
        search_context = maybe_web_search_with_status(bot, message.chat.id, placeholder.message_id, clean_text)

        messages = build_context(memory_id)
        _inject_user_profile(messages, message.from_user.id, chat_id=message.chat.id)

        content_structure = []
        if image_part:
            content_structure.append(image_part)
        content_structure.append({"type": "text", "text": inject_search_context(tagged_user_text, search_context)})
        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"] = content_structure
        else:
            messages.append({"role": "user", "content": content_structure})

        # Register for /stop cancellation, then stream.
        req_entry = _register_active_request(message.chat.id, message.from_user.id, placeholder.message_id)
        full_text, status_err, msg_ids = stream_to_telegram(
            message, messages, placeholder.message_id, request_entry=req_entry
        )
        _consume_active_request(req_entry)  # always consume, cancel or not

        if status_err == "stopped":
            # /stop during processing: wipe this turn from the session's
            # memory so the aborted ask doesn't shape later answers.
            try:
                repo.delete_last_user_message(memory_id)
            except Exception:
                pass
            # A partially-streamed answer may have spawned continuation
            # messages — remove them so nothing dangling is left behind.
            for _cid, _mid in (msg_ids or [])[1:]:
                try:
                    bot.delete_message(_cid, _mid)
                except Exception:
                    pass
            try:
                bot.edit_message_text(
                    "⏹ متوقف شد.", chat_id=message.chat.id, message_id=placeholder.message_id
                )
            except Exception:
                pass
            return

        if status_err is not None:
            METRICS["llm_errors"] += 1
            # The turn died with no answer — remove the user's message from
            # memory too, so an unanswered ask doesn't sit in the context
            # shaping later replies (same cleanup the /stop path does).
            if user_message_saved:
                try:
                    repo.delete_last_user_message(memory_id)
                except Exception:
                    pass
            if status_err == 429:
                txt = "سرور تحت فشار ترافیکی است. پردازش متوقف شد."
            elif status_err == "network_error":
                txt = "اتصال با سرور هوش مصنوعی قطع شد. لطفاً دوباره تلاش کنید."
            else:
                txt = f"خطایی در ارتباط با سرور هوش مصنوعی رخ داد. وضعیت سیستم: {status_err}"
            try:
                bot.edit_message_text(
                    txt, chat_id=message.chat.id, message_id=placeholder.message_id
                )
            except Exception:
                bot.send_message(message.chat.id, txt)
            return

        if not full_text or not full_text.strip():
            full_text = "دادهای برای تحلیل یافت نشد."

        finalize_message(message, placeholder.message_id, full_text, msg_ids)

        ps.save_memory_guarded(memory_id, "assistant", full_text, message.from_user.id, turn_born)

        maybe_summarize(memory_id)

        METRICS["handled"] += 1
    except Exception:
        log.error("CHAT/VISION ERROR", exc_info=True)
        METRICS["llm_errors"] += 1
        admin_notifier.notify("chat_handler_error", traceback.format_exc(limit=5))
        try:
            _safe_reply_to(bot, message, "خطایی در پردازش دادهها رخ داد. سیستم پاسخگو نیست.")
        except Exception:
            pass

# =========================
# Light Yagami: should_respond
# =========================
def should_respond_light(message):
    """Check if Light bot should respond to this message."""
    try:
        if message.chat.type == "private":
            return True
        identity = get_bot_light_identity()
        bot_username, bot_id = identity["username"], identity["id"]
        if not bot_username or not bot_id:
            return False
        raw_text = extract_text_from_message(message)
        is_mentioned = f"@{bot_username}" in raw_text.lower()
        is_reply_to_bot = bool(
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == bot_id
        )
        if raw_text:
            stripped = raw_text.strip()
            if stripped == "لایت" or stripped == "Light":
                return True
            if stripped.startswith("لایت"):
                if len(stripped) == 4 or stripped[4] in ' \t\n\r\f\v':
                    return True
            elif stripped.lower().startswith("light"):
                if len(stripped) == 5 or stripped[5] in ' \t\n\r\f\v':
                    return True
        return is_mentioned or is_reply_to_bot
    except Exception:
        log.error("should_respond_light error", exc_info=True)
        return False

# =========================
# Light Yagami Handlers
# =========================
if bot_light:

    @bot_light.message_handler(commands=["start"])
    def handle_start_light(message):
        try:
            start_text = (
                "من لایت یاگامی هستم.\n"
                "کیرا — خدای جهان نو.\n\n"
                "من عدالت هستم. هر کسی که دست به شر بزند، مجازات خواهد شد.\n"
                "دنیا در حال فساد است و من آمده‌ام تا آن را پاک کنم.\n\n"
                "اگر سؤالی داری یا می‌خواهی در مورد چیزی بحث کنی، بفرما."
            )
            _safe_reply_to(bot_light, message, start_text)
        except Exception:
            log.error("LIGHT START ERROR", exc_info=True)

    @bot_light.message_handler(commands=["stopall"])
    def handle_stopall_light(message):
        try:
            if not is_admin(message.from_user.id):
                _safe_reply_to(bot_light, message, "⛔ You are not authorized.")
                return
            count = stop_all_debates()
            _safe_reply_to(bot_light, message, f"⏹ {count} مناظره متوقف شد.")
        except Exception:
            log.error("STOPALL LIGHT ERROR", exc_info=True)
            _safe_reply_to(bot_light, message, "❌ خطا در توقف مناظره‌ها.")

    @bot_light.message_handler(commands=["stop"])
    def handle_stop_light(message):
        # Same UX as the L bot: private → plain /stop, no reply needed;
        # groups → must reply to the bot's "در حال پردازش..." placeholder.
        _handle_stop_generic(bot_light, message)

    @bot_light.message_handler(content_types=["text"])
    def handle_light_messages(message):
        if message.text and message.text.startswith("/"):
            return

        if not should_respond_light(message):
            return

        if message.reply_to_message and repo.is_bot_media_message(str(message.chat.id), message.reply_to_message.message_id):
            return

        try:
            chat_id_str = str(message.chat.id)
            turn_born = time.time()  # resurrection guard: post-reset saves are dropped

            if is_debate_active(chat_id_str):
                return

            raw_text = extract_text_from_message(message)

            # Strip the @mention of this bot from the text (same as the L bot's
            # handler) so it never leaks into the model prompt / history.
            if raw_text:
                identity = get_bot_light_identity()
                light_username = identity.get("username") or ""
                if light_username and f"@{light_username}" in raw_text.lower():
                    raw_text = re.sub(f"@{light_username}", "", raw_text, flags=re.IGNORECASE)

            # Same name-call prefix strip as the L bot: "لایت ..." / "Light ..."
            # should pass the text AFTER the name to the model, not the name itself.
            # Only strip when content follows — a bare "لایت" is a name-call,
            # not a prompt, and stripping it would send empty text.
            if raw_text:
                stripped = raw_text.strip()
                if (stripped.startswith("لایت") and len(stripped) > 4
                        and stripped[4] in ' \t\n\r\f\v'):
                    raw_text = stripped[4:].lstrip()
                elif (stripped.lower().startswith("light") and len(stripped) > 5
                        and stripped[5] in ' \t\n\r\f\v'):
                    raw_text = stripped[5:].lstrip()

            spam_key = f"light_{message.chat.id}_{message.from_user.id}"
            action, value = spam_tracker.check(spam_key)
            if action == "muted":
                _safe_reply_to(bot_light, message, f"{value} ثانیه دیگر من تو را نادیده میگیرم.")
                return
            if action == "warn":
                _safe_reply_to(bot_light, message, "صبرت رو از دست نده. ۱ دقیقه سکوت کن.")
                return

            if not ps.feature_gate_message(bot_light, message, "chat", explicit=False,
                                           chat_id=str(message.chat.id)):
                return

            allowed, scope = rate_limiter.allow("chat", message.from_user.id)
            if not allowed:
                METRICS["ratelimited"] += 1
                if scope == "global":
                    _safe_reply_to(bot_light, message, "سرور تحت فشار است. کمی صبر کن.")
                else:
                    _safe_reply_to(bot_light, message, "درخواستهای شما بیش از حد است. صبر کنید.")
                return

            memory_id = f"light_{chat_id_str}"
            user_first_name = message.from_user.first_name or "کاربر"
            tagged_user_text = f"[{user_first_name}]: {raw_text}"

            user_message_saved = ps.save_memory_guarded(
                memory_id, "user", tagged_user_text, message.from_user.id, turn_born)

            bot_light.send_chat_action(message.chat.id, "typing")
            placeholder = _safe_reply_to(bot_light, message, "در حال پردازش...")
            search_context = maybe_web_search_with_status(bot_light, message.chat.id, placeholder.message_id, raw_text)

            messages = build_context_light(memory_id)
            if search_context and messages and messages[-1]["role"] == "user":
                messages[-1]["content"] = inject_search_context(messages[-1]["content"], search_context)
            _inject_user_profile(messages, message.from_user.id, chat_id=message.chat.id)

            req_entry = _register_active_request(message.chat.id, message.from_user.id, placeholder.message_id)
            full_text, status_err, msg_ids = stream_to_telegram_generic(
                bot_light, message, messages, placeholder.message_id, request_entry=req_entry
            )
            _consume_active_request(req_entry)

            if status_err == "stopped":
                try:
                    repo.delete_last_user_message(memory_id)
                except Exception:
                    pass
                # Remove continuation messages from a partially-streamed answer.
                for _cid, _mid in (msg_ids or [])[1:]:
                    try:
                        bot_light.delete_message(_cid, _mid)
                    except Exception:
                        pass
                try:
                    bot_light.edit_message_text(
                        "⏹ متوقف شد.", chat_id=message.chat.id, message_id=placeholder.message_id
                    )
                except Exception:
                    pass
                return

            if status_err is not None:
                METRICS["llm_errors"] += 1
                # Same cleanup as the L bot: an unanswered turn shouldn't
                # linger in memory shaping later replies.
                if user_message_saved:
                    try:
                        repo.delete_last_user_message(memory_id)
                    except Exception:
                        pass
                if status_err == 429:
                    txt = "سرور تحت فشار است."
                elif status_err == "network_error":
                    txt = "اتصال با سرور قطع شد. لطفاً دوباره تلاش کنید."
                else:
                    txt = "خطایی در ارتباط با سرور رخ داد."
                try:
                    bot_light.edit_message_text(txt, chat_id=message.chat.id, message_id=placeholder.message_id)
                except Exception:
                    bot_light.send_message(message.chat.id, txt)
                return

            if not full_text or not full_text.strip():
                full_text = "داده‌ای برای پاسخ یافت نشد."

            finalize_message_generic(bot_light, message, placeholder.message_id, full_text, msg_ids)
            ps.save_memory_guarded(memory_id, "assistant", full_text, message.from_user.id, turn_born)
            maybe_summarize(memory_id)
            METRICS["handled"] += 1

        except Exception:
            log.error("LIGHT CHAT ERROR", exc_info=True)
            METRICS["llm_errors"] += 1
            admin_notifier.notify("light_chat_error", traceback.format_exc(limit=5))
            try:
                _safe_reply_to(bot_light, message, "خطایی در پردازش رخ داد.")
            except Exception:
                pass
