import logging
import os
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError

from telebot import types

from ai import agnes
from bots import admin_notifier, bot, bot_light
from db import repo
from metrics import METRICS
from services.streaming import _safe_reply_to

log = logging.getLogger("L-bot")


# =========================
# Debate System Constants
# =========================
MAX_DEBATE_ROUNDS = int(os.environ.get("MAX_DEBATE_ROUNDS", "50"))

DEBATE_DELAY = float(os.environ.get("DEBATE_DELAY", "4"))

_active_debates = {}

_debate_lock = threading.Lock()

LIGHT_DEBATE_PROMPT = """
You are Light Yagami (Kira) in a formal debate with L (the detective).
Personality in debates: confident, assertive, intellectually superior to L,
philosophical and moral arguments, dismissive of L's cautious approach,
references your new world vision, charming but dark.
Stay in character as Light Yagami. Maximum 3-4 sentences per response.
Debate topic: {topic}
"""

L_DEBATE_PROMPT = """
You are L (the detective) in a formal debate with Light Yagami (Kira).
Personality in debates: logical, evidence-based, calm, eccentric (eating sweets),
challenges assumptions, scientific methodology, respectful but persistent.
Stay in character as L. Maximum 3-4 sentences per response.
Debate topic: {topic}
"""

def is_debate_trigger(text):
    """Check if a message triggers a debate with Light."""
    if not text:
        return False
    stripped = text.strip()
    name_match = re.match(r'^(L|ال|ل)\s+', stripped)
    if not name_match:
        return False
    text_lower = stripped.lower()
    return "با لایت" in text_lower and "بحث" in text_lower

def extract_debate_topic(text):
    """Extract the debate topic from the trigger message."""
    stripped = text.strip()
    after_name = re.sub(r'^(L|ال|ل)\s+', '', stripped)
    topic = re.sub(r'^در مورد\s+', '', after_name)
    topic = re.sub(r'با لایت\s*یاگامی?\s*بحث\s*کنید?', '', topic)
    topic = re.sub(r'با لایت\s*بحث\s*کن', '', topic)
    topic = re.sub(r'بحث\s*کنید?', '', topic)
    topic = topic.strip()
    return topic if topic else None

def is_debate_active(chat_id_str):
    """Check if a debate is currently active in the given chat."""
    with _debate_lock:
        entry = _active_debates.get(chat_id_str)
        return entry is not None and entry["active"]

def start_debate(message, topic):
    """Start a debate between L and Light Yagami."""
    chat_id_str = str(message.chat.id)

    with _debate_lock:
        if chat_id_str in _active_debates and _active_debates[chat_id_str]["active"]:
            _safe_reply_to(bot, message, "⚠️ یک مناظره در حال انجام است")
            return
        _active_debates[chat_id_str] = {"active": True, "topic": topic, "round": 0}

    log.info("Debate started in chat %s — topic: %s", chat_id_str, topic)

    placeholder = _safe_reply_to(bot, message, "🔥 در حال آماده‌سازی مناظره...")

    l_debate_prompt = L_DEBATE_PROMPT.format(topic=topic)
    opening = agnes.generate([
        {"role": "system", "content": l_debate_prompt},
        {"role": "user", "content": f"موضوع مناظره: {topic}. نظر اولیه‌ات رو مطرح کن."},
    ])

    if not opening:
        bot.edit_message_text(
            "❌ خطا در تولید متن مناظره.",
            chat_id=message.chat.id,
            message_id=placeholder.message_id,
        )
        with _debate_lock:
            _active_debates.pop(chat_id_str, None)
        return

    bot.edit_message_text(
        opening + "\n\n⚡ بذار لایت یاگامی هم نظرش رو بگه... /call@TheLightYagamyBot",
        chat_id=message.chat.id,
        message_id=placeholder.message_id,
    )

    repo.save_message(f"debate_{chat_id_str}", "assistant", f"[L]: {opening}")

    thread = threading.Thread(
        target=_debate_loop,
        args=(chat_id_str, message.chat.id, topic, opening, placeholder.message_id, message.message_id),
        daemon=True,
        name=f"debate-{chat_id_str}",
    )
    thread.start()

DEBATE_GENERATE_TIMEOUT = int(os.environ.get("DEBATE_GENERATE_TIMEOUT", "180"))

DEBATE_GENERATE_ATTEMPTS = int(os.environ.get("DEBATE_GENERATE_ATTEMPTS", "2"))

# Bounded pool backing _generate_with_timeout: a fresh thread per call used to
# leak on timeout (the abandoned thread kept its HTTP connection open until the
# provider gave up). A small pool still lets a timed-out call's worker thread
# keep running in the background, but it caps concurrent leaked work instead of
# accumulating unboundedly.
_DEBATE_GEN_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="debate-gen")

def _generate_with_timeout(prompt_messages, timeout_sec=None):
    """Generate text with a hard timeout to prevent debate threads from hanging.

    The per-attempt timeout must be >= the HTTP timeout inside agnes.generate
    (180s) plus its internal retries' breathing room — a 60s ceiling used to
    fire BEFORE the HTTP layer gave up, so a slow (not even broken) provider
    killed the whole debate on its very first turn. Now: 180s per attempt,
    up to DEBATE_GENERATE_ATTEMPTS attempts (with a short pause between) —
    a transient provider stall costs one slow turn, not the debate."""
    if timeout_sec is None:
        timeout_sec = DEBATE_GENERATE_TIMEOUT

    for attempt in range(1, DEBATE_GENERATE_ATTEMPTS + 1):
        future = _DEBATE_GEN_POOL.submit(agnes.generate, prompt_messages)
        try:
            text = future.result(timeout=timeout_sec) or ""
            if text.strip():
                return text
            log.warning(
                "[DEBATE] agnes.generate() returned empty text (attempt %d/%d)",
                attempt, DEBATE_GENERATE_ATTEMPTS,
            )
        except FutureTimeoutError:
            # cancel() only works on still-QUEUED futures; a running one keeps
            # going in the bounded pool (capped at 4 workers, documented above).
            future.cancel()
            log.error(
                "[DEBATE] agnes.generate() TIMED OUT after %ds (attempt %d/%d)",
                timeout_sec, attempt, DEBATE_GENERATE_ATTEMPTS,
            )
        if attempt < DEBATE_GENERATE_ATTEMPTS:
            time.sleep(2)
    return ""

def _debate_delay():
    """Panel-overrideable turn delay (Stage 2B)."""
    try:
        from services.panel_settings import setting_value

        return setting_value("debate.delay_sec")
    except Exception:
        return DEBATE_DELAY


def _debate_max_rounds():
    """Panel-overrideable round cap (Stage 2B)."""
    try:
        from services.panel_settings import setting_value

        return setting_value("debate.max_rounds")
    except Exception:
        return MAX_DEBATE_ROUNDS


def _debate_loop(chat_id_str, telegram_chat_id, topic, l_opening, l_opening_msg_id=None,
                 user_message_id=None):
    """Background debate loop between L and Light.

    Reply threading (modern Bot API): every debate message goes out as a
    REPLY to the previous turn via types.ReplyParameters — the old
    reply_to_message_id= argument is the legacy pre-6.4 form and silently
    degrades to a plain message when its target is missing. With
    allow_sending_without_reply=True a deleted/missing target never breaks
    the send, and `fallback_chat_id` keeps replies anchored to the debate
    even when replying across bots. The closing line in the finally block
    replies to the last debate message too (that stray plain message was
    exactly the "Light posted a normal message, not a reply" symptom)."""
    log.info("[DEBATE] === Loop started for chat_id=%s topic='%s' bot_light=%s l_opening_msg_id=%s ===",
             chat_id_str, topic[:80], bot_light is not None, l_opening_msg_id)

    if bot_light is None:
        log.error("[DEBATE] bot_light is None — cannot run debate. Aborting for chat %s", chat_id_str)
        with _debate_lock:
            _active_debates.pop(chat_id_str, None)
        try:
            bot.send_message(telegram_chat_id, "⚠️ خطای سیستمی: ربات لایت در دسترس نیست. مناظره لغو شد.")
        except Exception:
            pass
        return

    l_history = [
        {"role": "system", "content": L_DEBATE_PROMPT.format(topic=topic)},
        {"role": "user", "content": f"موضوع مناظره: {topic}. نظر اولیه‌ات رو مطرح کن."},
        {"role": "assistant", "content": l_opening},
    ]
    light_history = [
        {"role": "system", "content": LIGHT_DEBATE_PROMPT.format(topic=topic)},
        {"role": "user", "content": f"[L]: {l_opening}"},
    ]

    last_light_msg_id = None
    last_l_msg_id = l_opening_msg_id
    last_sender = "l" if l_opening_msg_id else None  # who sent the final debate turn

    def _reply_send(bot_instance, text, reply_to_message_id):
        """Send a debate turn as a reply via the modern Bot API 6.4+ form.
        ReplyParameters.chat_id explicitly anchors the reply target's chat,
        and allow_sending_without_reply=True degrades gracefully to a plain
        message if the target was deleted — instead of raising or (worse,
        with the legacy argument) silently unthreading. Returns the sent
        message or raises."""
        return bot_instance.send_message(
            telegram_chat_id,
            text,
            reply_parameters=types.ReplyParameters(
                message_id=reply_to_message_id,
                chat_id=telegram_chat_id,
                allow_sending_without_reply=True,
            ),
        )

    # First real reply target: Light replies to L's opening message; if that
    # placeholder is gone, fall back to the user's trigger message.
    first_reply_target = l_opening_msg_id or user_message_id

    try:
        for round_num in range(1, _debate_max_rounds() + 1):
            with _debate_lock:
                entry = _active_debates.get(chat_id_str)
                if not entry or not entry["active"]:
                    break
                entry["round"] = round_num

            time.sleep(_debate_delay())

            try:
                light_text = _generate_with_timeout(light_history)
            except Exception as gen_err:
                log.error("[DEBATE] Chat %s round %d — generate() EXCEPTION for Light: %s",
                          chat_id_str, round_num, gen_err, exc_info=True)
                break

            if not light_text:
                log.warning("[DEBATE] Chat %s round %d — Light returned empty text. Stopping.",
                            chat_id_str, round_num)
                break

            try:
                light_msg = _reply_send(bot_light, light_text, first_reply_target or last_l_msg_id)
                last_light_msg_id = light_msg.message_id
                last_sender = "light"
            except Exception as e:
                log.warning("[DEBATE] Chat %s round %d — Light send with reply failed: %s — retrying without reply",
                            chat_id_str, round_num, e)
                try:
                    light_msg = bot_light.send_message(telegram_chat_id, light_text)
                    last_light_msg_id = light_msg.message_id
                    last_sender = "light"
                except Exception as e2:
                    log.error("[DEBATE] Chat %s round %d — Light FAILED to send: %s",
                              chat_id_str, round_num, e2, exc_info=True)
                    break

            l_history.append({"role": "user", "content": f"[Light Yagami]: {light_text}"})
            light_history.append({"role": "assistant", "content": light_text})
            repo.save_message(f"debate_{chat_id_str}", "user", f"[Light Yagami]: {light_text}")

            with _debate_lock:
                entry = _active_debates.get(chat_id_str)
                if not entry or not entry["active"]:
                    break

            time.sleep(_debate_delay())

            try:
                l_text = _generate_with_timeout(l_history)
            except Exception as gen_err:
                log.error("[DEBATE] Chat %s round %d — generate() EXCEPTION for L: %s",
                          chat_id_str, round_num, gen_err, exc_info=True)
                break

            if not l_text:
                log.warning("[DEBATE] Chat %s round %d — L returned empty text. Stopping.",
                            chat_id_str, round_num)
                break

            try:
                l_msg = _reply_send(bot, l_text, last_light_msg_id or user_message_id)
                last_l_msg_id = l_msg.message_id
                last_sender = "l"
            except Exception as e:
                log.warning("[DEBATE] Chat %s round %d — L send with reply failed: %s — retrying without reply",
                            chat_id_str, round_num, e)
                try:
                    l_msg = bot.send_message(telegram_chat_id, l_text)
                    last_l_msg_id = l_msg.message_id
                    last_sender = "l"
                except Exception as e2:
                    log.error("[DEBATE] Chat %s round %d — L FAILED to send: %s",
                              chat_id_str, round_num, e2, exc_info=True)
                    break

            light_history.append({"role": "user", "content": f"[L]: {l_text}"})
            l_history.append({"role": "assistant", "content": l_text})
            repo.save_message(f"debate_{chat_id_str}", "assistant", f"[L]: {l_text}")

    except Exception as loop_err:
        log.error("[DEBATE] Chat %s — UNEXPECTED ERROR in debate loop: %s",
                  chat_id_str, loop_err, exc_info=True)
        admin_notifier.notify("debate_loop_error", traceback.format_exc(limit=5))
    finally:
        with _debate_lock:
            _active_debates.pop(chat_id_str, None)

        try:
            # Whoever sent the LAST message gets the closing line, and it
            # goes out as a REPLY to that last message (it used to be a bare
            # send_message — the unthreaded "normal message" symptom).
            if last_sender == "light":
                last_msg_id = last_light_msg_id
                closing_bot, closing_prompt = bot, L_DEBATE_PROMPT.format(topic=topic)
            elif last_sender == "l" and bot_light:
                last_msg_id = last_l_msg_id
                closing_bot, closing_prompt = bot_light, LIGHT_DEBATE_PROMPT.format(topic=topic)
            else:
                last_msg_id = None
                closing_bot, closing_prompt = None, None

            if closing_bot is not None:
                closing = _generate_with_timeout([
                    {"role": "system", "content": closing_prompt},
                    {"role": "user", "content": "مناظره تمام شد. یک جمله پایانی بگو."},
                ])
                if closing:
                    try:
                        _reply_send(closing_bot, f"🏁 {closing}", last_msg_id or user_message_id)
                    except Exception:
                        closing_bot.send_message(telegram_chat_id, f"🏁 {closing}")
        except Exception as close_err:
            log.error("[DEBATE] Chat %s — failed to send closing message: %s",
                      chat_id_str, close_err, exc_info=True)

        log.info("[DEBATE] === Debate fully ended for chat %s ===", chat_id_str)

def stop_all_debates():
    """Stop all active debates. Returns count of stopped debates."""
    count = 0
    with _debate_lock:
        for chat_id_str, entry in _active_debates.items():
            if entry["active"]:
                entry["active"] = False
                count += 1
    return count
