import base64
import hashlib
import logging
import os
import re
import shutil
import time
import traceback

from telebot import types

from ai import agnes
from bots import admin_notifier, bot, bot_light
from config import (
    ADMIN_ID,
    OWNER_ID,
    TELEGRAM_FILE_BASE_URL,
    TELEGRAM_TOKEN,
    TELEGRAM_UPLOAD_MAX_BYTES,
)
from db import repo
from handlers.shared import extract_text_from_message, is_admin
from infra import HTTP
from limits import rate_limiter
from metrics import METRICS
from services.media import (
    download_instagram_post,
    extract_instagram_urls,
)
from services.memory import (
    build_context,
    build_context_light,
    maybe_summarize,
)
from services.search import inject_search_context, maybe_web_search
from services import panel_settings as ps
from services.streaming import (
    _consume_active_request,
    _register_active_request,
    _safe_reply_to,
    escape_markdown,
    finalize_message,
    finalize_message_generic,
    rate_limited_reply,
    stream_to_telegram,
    stream_to_telegram_generic,
)

log = logging.getLogger("L-bot")


# =========================
# Inline Mode — ask L (or Light) a question from ANY chat, even one the
# bot isn't a member of: @BotUsername your question
# =========================
# Design note: inline queries fire repeatedly as the person types (Telegram
# debounces client-side, but still much more often than one deliberate
# question). Calling the LLM on every keystroke would be slow and wasteful,
# so the FIRST response (inline_query) is instant and cheap — just a single
# placeholder result, no AI call. The real AI call only happens once the
# person actually TAPS that result, which Telegram reports via a separate
# chosen_inline_result update — and only then do we call the model and edit
# the already-sent message in place (editMessageText with inline_message_id)
# once the real answer is ready. No per-keystroke spam, no wasted API calls.
#
# This needs TWO things set up per bot in BotFather before it works at all:
#   1. /setinline           — turns inline mode on
#   2. /setinlinefeedback   — set to 100%, otherwise chosen_inline_result
#      updates (how we learn a question was actually asked) won't reliably
#      arrive, and the placeholder text will just sit there forever.
# The InlineKeyboardMarkup attached to the result below is also required —
# Telegram only includes inline_message_id in chosen_inline_result when the
# result had a keyboard attached; without it we'd have no way to edit it.
#
# Memory: since the bot isn't a member of the group, Telegram deliberately
# doesn't tell it which chat the question came from (privacy). So inline
# conversations are tracked per PERSON (`inline_<user_id>`), not per group,
# and are kept separate from that person's normal private-chat history with
# the bot — a distinct, portable "case file" that follows them across
# whatever group they ask from.
INLINE_ASK_MIN_LEN = 2

INLINE_PLACEHOLDER_TEXT = "🕵️ در حال بررسی سؤال...\n\n«{q}»"

def _inline_ask_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔄 سؤال بعدی", switch_inline_query_current_chat=""))
    return markup

def _feature_allows_surface(bot_instance, query_obj, feature, user_id=None, guest_query_id=None):
    """Feature gate for inline/guest surfaces (Stage 2B). Always answers
    (never hangs the client): disabled → empty inline answer / disabled
    text article for guest. Admins exempt. Returns True to proceed."""
    try:
        if user_id is None:
            user = getattr(query_obj, "from_user", None)
            user_id = getattr(user, "id", 0) or 0
        if is_admin(user_id):
            return True
        if ps.feature_enabled(feature):
            return True
        if feature == "inline":
            try:
                bot_instance.answer_inline_query(query_obj.id, [], cache_time=1, is_personal=True)
            except Exception:
                pass
        else:
            try:
                result = types.InlineQueryResultArticle(
                    id=hashlib.md5(str(guest_query_id).encode("utf-8")).hexdigest(),
                    title="⏸ Disabled",
                    input_message_content=types.InputTextMessageContent(
                        "⏸ This feature is temporarily disabled by the admins."),
                )
                bot_instance.answer_guest_query(guest_query_id, result)
            except Exception:
                pass
        return False
    except Exception:
        log.warning("surface feature gate failed; allowing (fail open)", exc_info=True)
        return True


def _handle_inline_query_generic(bot_instance, inline_query, persona_label):
    """Stage 1 — answer fast with a single placeholder result. No AI call
    happens here; only once the person actually taps it (stage 2)."""
    try:
        query_text = (inline_query.query or "").strip()
        if len(query_text) < INLINE_ASK_MIN_LEN:
            bot_instance.answer_inline_query(inline_query.id, [], cache_time=1, is_personal=True)
            return

        if not _feature_allows_surface(bot_instance, inline_query, "inline"):
            return

        result = types.InlineQueryResultArticle(
            id=hashlib.md5(query_text.encode("utf-8")).hexdigest(),
            title=f"🕵️ بپرس از {persona_label}: {query_text[:60]}",
            description="برای ارسال سؤال و دریافت پاسخ، اینجا رو بزن",
            input_message_content=types.InputTextMessageContent(
                INLINE_PLACEHOLDER_TEXT.format(q=escape_markdown(query_text[:300])),
                parse_mode="Markdown",
            ),
            reply_markup=_inline_ask_keyboard(),
        )
        bot_instance.answer_inline_query(inline_query.id, [result], cache_time=1, is_personal=True)
    except Exception:
        log.warning("Inline query handling failed", exc_info=True)
        try:
            bot_instance.answer_inline_query(inline_query.id, [], cache_time=1)
        except Exception:
            pass

def _handle_chosen_inline_result_generic(bot_instance, chosen, is_light):
    """Stage 2 — the person actually picked the result, so the placeholder
    text is now live in some chat we can't see or identify (Telegram
    withholds the target chat when the bot isn't a member, for privacy). We
    now do the real work — search if needed, ask the model, save to this
    person's inline memory — and edit that message in place via
    inline_message_id once the answer is ready."""
    inline_message_id = getattr(chosen, "inline_message_id", None)
    if not inline_message_id:
        log.info("chosen_inline_result had no inline_message_id — check /setinlinefeedback in BotFather.")
        return
    try:
        user_id = chosen.from_user.id if chosen.from_user else 0
        user_text = (chosen.query or "").strip()
        if not user_text:
            return

        allowed, _scope = rate_limiter.allow("inline", user_id)
        if not allowed:
            try:
                bot_instance.edit_message_text(
                    "🚫 تعداد سؤالات inline شما بیش از حد مجاز است. کمی صبر کنید.",
                    inline_message_id=inline_message_id,
                )
            except Exception:
                pass
            return

        memory_id = f"inline_{'light_' if is_light else ''}{user_id}"
        repo.save_message(memory_id, "user", f"[inline]: {user_text}")

        search_context = maybe_web_search(user_text)

        messages = build_context_light(memory_id) if is_light else build_context(memory_id)
        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"] = inject_search_context(messages[-1]["content"], search_context)

        full_text = agnes.generate(messages)
        if not full_text or not full_text.strip():
            full_text = "دادهای برای تحلیل یافت نشد. دوباره تلاش کنید."

        repo.save_message(memory_id, "assistant", full_text)
        maybe_summarize(memory_id)
        METRICS["handled"] += 1

        try:
            bot_instance.edit_message_text(
                full_text[:4096], inline_message_id=inline_message_id, parse_mode="Markdown",
            )
        except Exception:
            try:
                bot_instance.edit_message_text(full_text[:4096], inline_message_id=inline_message_id)
            except Exception:
                log.warning("Failed to edit inline message with final answer", exc_info=True)
    except Exception:
        log.error("Inline chosen-result handling failed", exc_info=True)
        try:
            bot_instance.edit_message_text(
                "❌ خطایی در پردازش سؤال رخ داد.", inline_message_id=inline_message_id,
            )
        except Exception:
            pass

bot.register_inline_handler(
    lambda q: _handle_inline_query_generic(bot, q, "L"), func=lambda q: True
)
bot.register_chosen_inline_handler(
    lambda r: _handle_chosen_inline_result_generic(bot, r, is_light=False), func=lambda r: True
)

if bot_light:
    bot_light.register_inline_handler(
        lambda q: _handle_inline_query_generic(bot_light, q, "لایت"), func=lambda q: True
    )
    bot_light.register_chosen_inline_handler(
        lambda r: _handle_chosen_inline_result_generic(bot_light, r, is_light=True), func=lambda r: True
    )

# =========================
# Guest Mode (Bot API 10.x) — answer @-mentions in groups the bot is NOT a
# member of. Requires pyTelegramBotAPI >= 4.36.0 (bumped in requirements.txt)
# and "Guest Mode" enabled per-bot in BotFather's MiniApp settings.
# =========================
# Telegram's contract here is different from inline mode: there is no
# follow-up event to defer work to (no chosen_inline_result equivalent) —
# the guest_message handler gets exactly ONE chance to call answerGuestQuery,
# so the whole pipeline (search + AI generation) has to run synchronously
# inside the handler before answering. Telegram also documents this as
# stateless per interaction / no chat history access; the small per-person
# memory below (`guest_<user_id>`) is our own addition on top of that — it
# follows the PERSON across whatever group they ask from, not the group
# itself (which the bot can't see anyway), same design as inline mode's
# memory, but kept in a separate namespace from it.
def _upload_for_guest_reply(bot_instance, file_path, is_video, storage_chat_id):
    """Upload a local file to `storage_chat_id` purely to obtain a Telegram
    file_id, then delete that message — file_ids stay valid for reuse
    independent of the original message once issued. This is how real
    video/photo content gets into a Guest Mode reply: it can only answer
    with something Telegram already has (a file_id, via
    InlineQueryResultCached*) or an externally fetchable URL — never a raw
    local upload — and the file_id route is reliable where a raw CDN URL
    fetched by Telegram itself often isn't (signed/cookie-gated Instagram
    URLs). Returns the file_id, or None on any failure."""
    msg = None
    try:
        with open(file_path, "rb") as fh:
            if is_video:
                msg = bot_instance.send_video(storage_chat_id, fh, supports_streaming=True)
                file_id = msg.video.file_id
            else:
                msg = bot_instance.send_photo(storage_chat_id, fh)
                file_id = msg.photo[-1].file_id
        return file_id
    except Exception:
        log.warning("Failed to upload file for Guest Mode file_id", exc_info=True)
        return None
    finally:
        if msg:
            try:
                bot_instance.delete_message(storage_chat_id, msg.message_id)
            except Exception:
                pass

def _handle_guest_message_generic(bot_instance, guest_msg, is_light):
    log.info(
        "guest_message received: chat_type=%s text=%r guest_query_id=%s",
        getattr(getattr(guest_msg, "chat", None), "type", None),
        (guest_msg.text or guest_msg.caption or "")[:80],
        getattr(guest_msg, "guest_query_id", None),
    )
    guest_query_id = getattr(guest_msg, "guest_query_id", None)
    if not guest_query_id:
        log.warning("guest_message had no guest_query_id — can't answer it, dropping.")
        return

    def _answer(text, use_markdown=True):
        try:
            result = types.InlineQueryResultArticle(
                id=hashlib.md5((guest_query_id + str(use_markdown)).encode("utf-8")).hexdigest(),
                title="پاسخ",
                input_message_content=types.InputTextMessageContent(
                    text[:4096], parse_mode="Markdown" if use_markdown else None,
                ),
            )
            bot_instance.answer_guest_query(guest_query_id, result)
            return True
        except Exception:
            return False

    try:
        user = guest_msg.from_user or getattr(guest_msg, "guest_bot_caller_user", None)
        user_id = user.id if user else 0
        user_text = (guest_msg.text or guest_msg.caption or "").strip()
        if not user_text:
            return

        if not _feature_allows_surface(bot_instance, guest_msg, "guest",
                                       user_id=user_id, guest_query_id=guest_query_id):
            return

        allowed, _scope = rate_limiter.allow("inline", user_id)
        if not allowed:
            _answer("🚫 تعداد سؤالات شما در این حالت بیش از حد مجاز است. کمی صبر کنید.", use_markdown=False)
            return

        # Route: Instagram links. Guest mode only allows ONE reply, and it
        # can only be text or something Telegram already has access to (a
        # file_id, or an externally fetchable URL) — never a raw local
        # upload. So: download the post like normal, pre-upload the result
        # to a storage chat purely to obtain a file_id (see
        # _upload_for_guest_reply), then answer with THAT as a cached
        # video/photo result. Nothing is sent back until the real clip is
        # ready — no placeholder, no intermediate message.
        ig_urls = extract_instagram_urls(user_text)
        if ig_urls:
            # Owner's DM is the throwaway storage chat for the file_id trick.
            # OWNER_ID with ADMIN_ID fallback preserves today's behavior when
            # no owner is configured (both 0 → feature unavailable, same as before).
            storage_chat_id = OWNER_ID or ADMIN_ID
            if not storage_chat_id:
                _answer(
                    "📥 دانلود اینستاگرام توی حالت مهمان فعلاً در دسترس نیست (ADMIN_ID روی سرور تنظیم نشده). "
                    "لینک رو توی پیوی برام بفرست یا منو به یه گروه اضافه کن.",
                    use_markdown=False,
                )
                return

            ig_result = download_instagram_post(ig_urls[0])
            if not ig_result or not ig_result.get("files"):
                _answer(
                    "❌ نتونستم این پست رو دانلود کنم. ممکنه پرایوت باشه، حذف‌شده باشه، یا اینستاگرام محدودش کرده باشه.",
                    use_markdown=False,
                )
                if ig_result:
                    shutil.rmtree(ig_result["outdir"], ignore_errors=True)
                return

            try:
                f = ig_result["files"][0]  # one reply = one media item, even for carousels
                if os.path.getsize(f["path"]) > TELEGRAM_UPLOAD_MAX_BYTES:
                    _answer("⚠️ حجم فایل بیشتر از سقف مجاز تلگرامه (۵۰ مگابایت) و قابل ارسال نیست.", use_markdown=False)
                    return

                file_id = _upload_for_guest_reply(bot_instance, f["path"], f["is_video"], storage_chat_id)
                if not file_id:
                    _answer("❌ خطایی توی آماده‌سازی فایل برای ارسال رخ داد.", use_markdown=False)
                    return

                caption = (ig_result.get("caption") or "").strip()[:1024] or None
                if len(ig_result["files"]) > 1:
                    note = f"(۱ از {len(ig_result['files'])} — فقط اولین آیتم توی حالت مهمان ارسال می‌شه)"
                    caption = f"{caption}\n\n{note}" if caption else note

                result_id = hashlib.md5(guest_query_id.encode("utf-8")).hexdigest()
                if f["is_video"]:
                    media_result = types.InlineQueryResultCachedVideo(
                        id=result_id, video_file_id=file_id, title="ویدیو", caption=caption,
                    )
                else:
                    media_result = types.InlineQueryResultCachedPhoto(
                        id=result_id, photo_file_id=file_id, caption=caption,
                    )
                bot_instance.answer_guest_query(guest_query_id, media_result)
                METRICS["instagram_downloads"] += 1
            finally:
                shutil.rmtree(ig_result["outdir"], ignore_errors=True)
            return

        # Route: /tr — fully supported here since the result is just text.
        if re.match(r"^/tr(?:@\S+)?(?:\s|$)", user_text, re.IGNORECASE):
            target_lang, source_text = _parse_tr_command(user_text)
            if target_lang is None and not source_text:
                _answer(PERSIAN_GUIDE, use_markdown=True)
                return
            if target_lang is None:
                target_lang = "en"
            translated = agnes.generate(_build_translate_messages(target_lang, source_text))
            _answer((translated or "").strip() or "ترجمه‌ای یافت نشد.", use_markdown=True)
            return

        # Route: any other slash-command (/todo, /remind, /pic, /admin, ...)
        # — these are all tied to a persistent chat_id or interactive
        # wizard state that Guest Mode deliberately doesn't expose, so
        # there's nothing meaningful to run. Say so instead of letting it
        # fall through and get "answered" as a literal chat question.
        if user_text.startswith("/"):
            _answer(
                "⚙️ این دستور توی حالت مهمان در دسترس نیست. برای دستورات، توی پیوی بهم پیام بده یا منو به گروه اضافه کن.",
                use_markdown=False,
            )
            return

        memory_id = f"guest_{'light_' if is_light else ''}{user_id}"

        reply_ctx = ""
        replied = getattr(guest_msg, "reply_to_message", None)
        if replied and (replied.text or replied.caption):
            reply_ctx = (replied.text or replied.caption or "")[:500]
        full_user_text = f"[پیامی که ریپلای شده]: {reply_ctx}\n\n{user_text}" if reply_ctx else user_text

        repo.save_message(memory_id, "user", f"[guest]: {full_user_text}")
        search_context = maybe_web_search(user_text)

        messages = build_context_light(memory_id) if is_light else build_context(memory_id)
        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"] = inject_search_context(messages[-1]["content"], search_context)

        answer_text = agnes.generate(messages)
        if not answer_text or not answer_text.strip():
            answer_text = "دادهای برای تحلیل یافت نشد. دوباره تلاش کنید."
        else:
            repo.save_message(memory_id, "assistant", answer_text)
            maybe_summarize(memory_id)
            METRICS["handled"] += 1

        if not _answer(answer_text, use_markdown=True):
            log.warning("answer_guest_query failed with Markdown, retrying plain")
            _answer(answer_text, use_markdown=False)
    except Exception:
        log.error("Guest message handling failed", exc_info=True)
        _answer("❌ خطایی در پردازش سؤال رخ داد.", use_markdown=False)

bot.register_guest_message_handler(
    lambda m: _handle_guest_message_generic(bot, m, is_light=False), func=lambda m: True
)

if bot_light:
    bot_light.register_guest_message_handler(
        lambda m: _handle_guest_message_generic(bot_light, m, is_light=True), func=lambda m: True
    )

# =========================
# Translation (/tr command)
# =========================
TRANSLATOR_SYSTEM_PROMPT = """
You are an elite, professional human translator and expert linguist with native fluency in all major worldwide languages. Your sole task is to translate the user's input text into the target language specified by the user.

Strictly adhere to the following operational rules:
1. CONTEXTUAL & NATURAL TRANSLATION: Do not translate word-for-word. Capture the exact meaning, cultural nuances, and context. The output must sound completely natural to a native speaker of the target language.
2. TONE PRESERVATION & CONSISTENCY: Match the exact tone of the source text. Do not mix formal and colloquial language in the output. For modern tech/business contexts, keep the tone smooth, professional, and contemporary (not overly archaic or strictly textbook).
3. HANDLING OCR IMPERFECTIONS: The input text might occasionally originate from an OCR scan and contain minor typos, broken words, or punctuation errors. Silently correct these errors during the translation process based on the obvious context.
4. FORMAT & STRUCTURE RETENTION: Keep all original emojis, line breaks, bullet points, and formatting exactly where they are. Do not alter formulas or code snippets if present.
5. TECHNICAL TERMS & NO MIXED TOKENS: For technical terms (e.g., API, Middleware, Third-party), either use their widely accepted Persian translations (e.g., شخص ثالث) or write the English word completely in Latin characters (e.g., Middleware). NEVER blend Persian prefixes/suffixes with English letters (e.g., do NOT write "مiddleware" or "APIها"). Keep them strictly separate.
6. PURE TARGET LANGUAGE ONLY: Ensure the final output is written purely in the target language. Do not accidentally generate characters, kanji, or words from unrelated foreign languages (e.g., Chinese characters like 第三方).
7. EMPTY COMMAND HANDLING: If the user's input is exactly "/tr" or contains no text/image content to translate, completely halt the translation protocol. Instead, generate a beautifully formatted, clear, and comprehensive Telegram user guide in Persian (به زبان فارسی). This guide must instruct the user how to use the bot for translating text (e.g., using `/tr en [text]`) and photos (by putting `/tr` or language codes in the photo caption), and list common language codes (such as fa, en, ar, tr, fr, de, ru) using clear emojis and Telegram markdown formatting.
8. STRICT OUTPUT CONSTRAINT: Output ONLY and ONLY the final translated text (or the Persian user guide if Rule 7 is triggered).
   - NEVER include introductory remarks (e.g., "Here is your translation:", "Sure, I can help with that:").
   - NEVER include conversational fluff, explanations, or notes at the end.
   - If the input is already in the target language and Rule 7 is not triggered, simply return the input text as-is without any modification.
"""

PERSIAN_GUIDE = """📖 **راهنمای استفاده از سرویس ترجمه**

━━━━━━━━━━━━━━━━━

🔹 **ترجمه متن:**
`/tr [کد زبان] [متن شما]`

مثال:
• `/tr en سلام دنیا` → ترجمه به انگلیسی
• `/tr fr حالت چطوره؟` → ترجمه به فرانسوی
• `/tr ar خوش آمدید` → ترجمه به عربی

🔹 **ترجمه با شناسایی خودکار زبان مبدأ:**
فقط کد زبان مقصد و متن را وارد کنید:
`/tr de Good morning everyone`

🔹 **ترجمه تصویر:**
عکس خود را ارسال کنید و در کپشن آن بنویسید:
• `/tr en` → ترجمه متن تصویر به انگلیسی
• `/tr fa` → ترجمه متن تصویر به فارسی

━━━━━━━━━━━━━━━━━

🌐 **کدهای رایج زبان:**

`fa` 🇮🇷 فارسی
`en` 🇬🇧 انگلیسی
`ar` 🇸🇦 عربی
`tr` 🇹🇷 ترکی استانبولی
`fr` 🇫🇷 فرانسوی
`de` 🇩🇪 آلمانی
`ru` 🇷🇺 روسی
`es` 🇪🇸 اسپانیایی
`it` 🇮🇹 ایتالیایی
`ja` 🇯🇵 ژاپنی
`ko` 🇰🇷 کرهای
`zh` 🇨🇳 چینی
`pt` 🇧🇷 پرتغالی
`hi` 🇮🇳 هندی
`ur` 🇵🇰 اردو

━━━━━━━━━━━━━━━━━

💡 **نکات:**
• زبان مبدأ به صورت خودکار شناسایی میشود
• میتوانید متنهای طولانی ارسال کنید
• فرمت اصلی متن (ایموجی، خطوط جدید، لیستها) حفظ میشود
"""

LANG_NAMES = {
    "fa": "Persian (فارسی)", "en": "English", "ar": "Arabic (العربية)",
    "tr": "Turkish (Türkçe)", "fr": "French (Français)", "de": "German (Deutsch)",
    "ru": "Russian (Русский)", "es": "Spanish (Español)", "it": "Italian (Italiano)",
    "ja": "Japanese (日本語)", "ko": "Korean (한국어)", "zh": "Chinese (中文)",
    "pt": "Portuguese (Português)", "hi": "Hindi (हिन्दी)", "ur": "Urdu (اردو)",
    "nl": "Dutch (Nederlands)", "sv": "Swedish (Svenska)", "pl": "Polish (Polski)",
    "da": "Danish (Dansk)", "fi": "Finnish (Suomi)", "no": "Norwegian (Norsk)",
    "el": "Greek (Ελληνικά)", "he": "Hebrew (עברית)", "th": "Thai (ไทย)",
    "vi": "Vietnamese (Tiếng Việt)", "id": "Indonesian (Bahasa Indonesia)",
    "ms": "Malay (Bahasa Melayu)", "bn": "Bengali (বাংলা)",
    "uk": "Ukrainian (Українська)", "cs": "Czech (Čeština)",
    "ro": "Romanian (Română)", "hu": "Hungarian (Magyar)",
}

def _parse_tr_command(text):
    """Parse /tr [lang] [text] → (lang_code, content_text)."""
    text = text.strip()
    text = re.sub(r"^/tr(?:@\S+)?", "", text).strip()

    if not text:
        return None, ""

    parts = text.split(None, 1)
    if len(parts) == 1:
        if re.fullmatch(r"[a-zA-Z]{2}", parts[0]):
            return parts[0].lower(), ""
        return None, parts[0]

    candidate = parts[0].lower()
    if re.fullmatch(r"[a-zA-Z]{2}", candidate):
        return candidate, parts[1]
    return None, text

def _build_translate_messages(target_lang, source_text, image_part=None):
    """Build the message list for the translation LLM call."""
    lang_display = LANG_NAMES.get(target_lang, target_lang.upper())
    user_content = (
        f"Translate the following text into **{lang_display}** (language code: `{target_lang}`).\n\n"
        f"Text to translate:\n{source_text}"
    )

    messages = [{"role": "system", "content": TRANSLATOR_SYSTEM_PROMPT}]

    if image_part:
        messages.append({
            "role": "user",
            "content": [image_part, {"type": "text", "text": user_content}],
        })
    else:
        messages.append({"role": "user", "content": user_content})

    return messages

# Photo-with-caption registration MUST come before handle_all_messages
# (registered further down with content_types=["text", "photo"]) — telebot's
# first-match dispatch then routes captioned /tr photos here instead of the
# generic vision pipeline. telebot's `commands=` filter only inspects
# message.text (None for photos), so a second registration with an explicit
# caption func is required for the documented photo-translation flow to work.
@bot.message_handler(
    content_types=["photo"],
    func=lambda m: bool(m.caption) and bool(re.match(r"^/tr(?:@\S+)?(?:\s|$)", m.caption.strip(), re.IGNORECASE)),
)
@bot.message_handler(commands=["tr"])
def handle_translate(message):
    try:
        raw_text = extract_text_from_message(message) or ""
        is_photo = message.content_type == "photo"

        category = "vision" if is_photo else "chat"
        if not rate_limited_reply(message, category):
            return

        if not ps.feature_gate_message(bot, message, "tr", explicit=True):
            return

        target_lang, source_text = _parse_tr_command(raw_text)

        if not is_photo and (target_lang is None and not source_text):
            _safe_reply_to(bot, message, PERSIAN_GUIDE, parse_mode="Markdown")
            return

        if target_lang is None:
            target_lang = "en"

        image_part = None
        if is_photo:
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
                        log.info("Translation photo downloaded: %d bytes", len(raw_content))
                    else:
                        log.error("Translation photo download failed: status=%d", img_response.status_code)
                except Exception as direct_err:
                    log.error("Translation photo download error: %s", direct_err)

                if raw_content is not None:
                    encoded_image = base64.b64encode(raw_content).decode("utf-8")
                    image_part = {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{encoded_image}"},
                    }
                else:
                    log.error("Translation photo download failed")
                    _safe_reply_to(bot, message, "⚠️ قادر به دریافت تصویر برای ترجمه نیستم. لطفاً دوباره تلاش کنید.")
                    return

            except Exception as photo_err:
                log.error("Translation photo exception: %s", photo_err, exc_info=True)
                _safe_reply_to(bot, message, "⚠️ خطا در دریافت تصویر. لطفاً دوباره تلاش کنید.")
                return

        if is_photo and not source_text:
            source_text = (
                "Please OCR and extract all text visible in this image, "
                "then translate it into the target language."
            )

        messages = _build_translate_messages(target_lang, source_text, image_part)

        bot.send_chat_action(message.chat.id, "typing")
        placeholder = _safe_reply_to(bot, message, "⏳ در حال ترجمه...")

        # Register for /stop cancellation, then stream.
        req_entry = _register_active_request(message.chat.id, message.from_user.id, placeholder.message_id)
        full_text, status_err, msg_ids = stream_to_telegram(
            message, messages, placeholder.message_id, request_entry=req_entry
        )
        _consume_active_request(req_entry)  # always consume, cancel or not

        if status_err == "stopped":
            # /stop during translation: remove continuation messages from a
            # partially-streamed answer, then mark the placeholder as stopped.
            for _cid, _mid in (msg_ids or [])[1:]:
                try:
                    bot.delete_message(_cid, _mid)
                except Exception:
                    pass
            try:
                bot.edit_message_text(
                    "⏹ ترجمه متوقف شد.", chat_id=message.chat.id, message_id=placeholder.message_id
                )
            except Exception:
                pass
            return

        if status_err is not None:
            METRICS["llm_errors"] += 1
            if status_err == 429:
                txt = "سرور تحت فشار ترافیکی است. ترجمه متوقف شد."
            elif status_err == "network_error":
                txt = "اتصال با سرور قطع شد. لطفاً دوباره تلاش کنید."
            else:
                txt = f"خطا در ارتباط با سرور. وضعیت: {status_err}"
            try:
                bot.edit_message_text(
                    txt, chat_id=message.chat.id, message_id=placeholder.message_id
                )
            except Exception:
                bot.send_message(message.chat.id, txt)
            return

        if not full_text or not full_text.strip():
            full_text = "ترجمهای یافت نشد."

        finalize_message(message, placeholder.message_id, full_text, msg_ids)
        METRICS["handled"] += 1

    except Exception:
        log.error("TRANSLATE ERROR", exc_info=True)
        METRICS["llm_errors"] += 1
        admin_notifier.notify("translate_error", traceback.format_exc(limit=5))
        try:
            _safe_reply_to(bot, message, "خطایی در پردازش ترجمه رخ داد.")
        except Exception:
            pass
