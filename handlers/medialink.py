import logging
import os
import shutil
import time
import traceback
import uuid

from telebot import types

from bots import admin_notifier, bot
from config import (
    ADMIN_ID,
    INSTAGRAM_COOKIES_FILE,
    TELEGRAM_UPLOAD_MAX_BYTES,
    YOUTUBE_DL_DIR,
)
from db import repo
from limits import rate_limiter
from metrics import METRICS
from services.media import (
    _download_youtube_quality,
    _extract_youtube_qualities,
    _is_instagram_link_message,
    _is_youtube_link_message,
    _youtube_quality_sessions,
    download_instagram_post,
    extract_instagram_urls,
    extract_youtube_urls,
    send_large_file_via_userbot,
)
from services import panel_settings as ps
from services.streaming import _safe_reply_to, rate_limited_reply
from sessions import _waiting_text_input

log = logging.getLogger("L-bot")


@bot.message_handler(func=_is_instagram_link_message, content_types=["text"])
def handle_instagram_link(message):
    """Registered BEFORE handle_all_messages so any message containing an
    Instagram post/reel/IGTV/carousel/story/highlight link is intercepted
    here instead of falling through to the normal AI chat pipeline — works
    in any chat this bot is a member of, no mention or command needed."""
    result = None
    try:
        urls = extract_instagram_urls(message.text)
        if not urls:
            return
        url = urls[0]  # only the first link per message, bounds cost if someone pastes many
        is_story = "/stories/" in url.lower()

        # Ambient auto-trigger: silent in groups when disabled, rate-limited
        # notice in private; admins exempt. Must precede any status message.
        if not ps.feature_gate_message(bot, message, "instagram", explicit=False):
            return

        allowed, _scope = rate_limiter.allow("instagram", message.from_user.id)
        if not allowed:
            METRICS["ratelimited"] += 1
            _safe_reply_to(bot, message, "درخواست‌های دانلود شما بیش از حد مجاز است. کمی صبر کنید.")
            return

        bot.send_chat_action(message.chat.id, "upload_video")
        status = _safe_reply_to(bot, message, "⏳ در حال دانلود از اینستاگرام...")

        result = download_instagram_post(url)
        if not result:
            fail_text = "❌ نتونستم این پست رو دانلود کنم. ممکنه پرایوت باشه، حذف‌شده باشه، یا اینستاگرام موقتاً محدودش کرده باشه."
            if is_story and not (INSTAGRAM_COOKIES_FILE and os.path.isfile(INSTAGRAM_COOKIES_FILE)):
                fail_text = "❌ نتونستم این استوری/هایلایت رو دانلود کنم. استوری همیشه به یه اکانت لاگین‌شده نیاز داره (INSTAGRAM_COOKIES_FILE تنظیم نشده یا منقضی شده)."
            try:
                bot.edit_message_text(fail_text, chat_id=message.chat.id, message_id=status.message_id)
            except Exception:
                pass
            return

        try:
            bot.delete_message(message.chat.id, status.message_id)
        except Exception:
            pass

        caption = result["caption"]
        caption_display = caption[:1024] if caption else None
        files = [f for f in result["files"] if os.path.getsize(f["path"]) <= TELEGRAM_UPLOAD_MAX_BYTES]
        oversized = len(result["files"]) - len(files)

        open_handles = []
        try:
            if len(files) == 1:
                f = files[0]
                with open(f["path"], "rb") as fh:
                    if f["is_video"]:
                        sent_msg = bot.send_video(
                            message.chat.id, fh, caption=caption_display,
                            reply_to_message_id=message.message_id, supports_streaming=True,
                        )
                    else:
                        sent_msg = bot.send_photo(
                            message.chat.id, fh, caption=caption_display,
                            reply_to_message_id=message.message_id,
                        )
                repo.mark_bot_media_message(message.chat.id, sent_msg.message_id, kind="instagram")
            elif len(files) > 1:
                media_group = []
                for i, f in enumerate(files[:10]):  # Telegram's media-group cap
                    fh = open(f["path"], "rb")
                    open_handles.append(fh)
                    kwargs = {"caption": caption_display} if i == 0 and caption_display else {}
                    if f["is_video"]:
                        media_group.append(types.InputMediaVideo(fh, **kwargs))
                    else:
                        media_group.append(types.InputMediaPhoto(fh, **kwargs))
                if media_group:
                    sent_msgs = bot.send_media_group(message.chat.id, media_group, reply_to_message_id=message.message_id)
                    for sent_msg in sent_msgs:
                        repo.mark_bot_media_message(message.chat.id, sent_msg.message_id, kind="instagram")
            else:
                _safe_reply_to(bot, message, "⚠️ حجم فایل بیشتر از سقف مجاز تلگرامه (۵۰ مگابایت) و قابل ارسال نیست.")
        finally:
            for fh in open_handles:
                try:
                    fh.close()
                except Exception:
                    pass

        if oversized:
            _safe_reply_to(bot, message, f"⚠️ {oversized} مورد از این پست به‌خاطر حجم بالا ارسال نشد.")

        METRICS["instagram_downloads"] += 1
    except Exception:
        log.error("INSTAGRAM DOWNLOAD ERROR", exc_info=True)
        admin_notifier.notify("instagram_download_error", traceback.format_exc(limit=5))
        try:
            _safe_reply_to(bot, message, "❌ خطایی در دانلود پیش اومد.")
        except Exception:
            pass
    finally:
        if result:
            shutil.rmtree(result["outdir"], ignore_errors=True)

@bot.message_handler(func=_is_youtube_link_message, content_types=["text"])
def handle_youtube_link(message):
    """Registered BEFORE handle_all_messages so any message containing a
    YouTube link is intercepted here instead of falling through to the
    normal AI chat pipeline."""
    try:
        urls = extract_youtube_urls(message.text)
        if not urls:
            return
        url = urls[0]

        if not ps.feature_gate_message(bot, message, "youtube", explicit=False):
            return

        allowed, _scope = rate_limiter.allow("youtube", message.from_user.id)
        if not allowed:
            METRICS["ratelimited"] += 1
            _safe_reply_to(bot, message, "درخواست‌های دانلود شما بیش از حد مجاز است. کمی صبر کنید.")
            return

        bot.send_chat_action(message.chat.id, "typing")
        status = _safe_reply_to(bot, message, "⏳ در حال بررسی کیفیت‌های موجود...")

        title, options, reason = _extract_youtube_qualities(url)
        if not options:
            if reason == "gate":
                fail_text = ("❌ یوتیوب جلوی دانلود رو گرفته و ورود می‌خواد (محدودیت آی‌پی یا نشست منقضی‌شده). "
                             "اگه ادامه داشت به ادمین بگو.")
            else:
                fail_text = "❌ نتونستم این ویدیو رو بررسی کنم. ممکنه حذف‌شده، سنی‌محدود، یا خصوصی باشه."
            try:
                bot.edit_message_text(
                    fail_text,
                    chat_id=message.chat.id, message_id=status.message_id,
                )
            except Exception:
                pass
            return

        token = uuid.uuid4().hex[:10]
        _youtube_quality_sessions.set(token, {
            "url": url, "title": title, "options": options, "user_id": message.from_user.id,
        })

        markup = types.InlineKeyboardMarkup(row_width=2)
        buttons = []
        for key, opt in options.items():
            size_str = f" (~{opt['filesize'] // (1024 * 1024)}MB)" if opt.get("filesize") else ""
            buttons.append(types.InlineKeyboardButton(f"{opt['label']}{size_str}", callback_data=f"yt_dl_{token}_{key}"))
        markup.add(*buttons)

        title_display = title[:150] if title else "این ویدیو"
        try:
            bot.edit_message_text(
                f"🎬 **{title_display}**\n\nکیفیت مورد نظر رو انتخاب کن:",
                chat_id=message.chat.id, message_id=status.message_id,
                parse_mode="Markdown", reply_markup=markup,
            )
        except Exception:
            try:
                bot.edit_message_text(
                    f"{title_display}\n\nکیفیت مورد نظر رو انتخاب کن:",
                    chat_id=message.chat.id, message_id=status.message_id, reply_markup=markup,
                )
            except Exception:
                pass
    except Exception:
        log.error("YOUTUBE LINK HANDLER ERROR", exc_info=True)
        admin_notifier.notify("youtube_link_error", traceback.format_exc(limit=5))
        try:
            _safe_reply_to(bot, message, "❌ خطایی در بررسی لینک یوتیوب رخ داد.")
        except Exception:
            pass

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("yt_dl_"))
def handle_youtube_quality_choice(call):
    try:
        if not ps.feature_gate_callback(bot, call, "youtube"):
            return
        parts = call.data.split("_", 3)  # ["yt", "dl", token, key]
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Invalid callback data.")
            return
        token, key = parts[2], parts[3]

        session = _youtube_quality_sessions.pop(token)
        if not session:
            bot.answer_callback_query(call.id, "❌ این درخواست منقضی شده. دوباره لینک رو بفرست.", show_alert=True)
            return
        if call.from_user.id != session["user_id"]:
            bot.answer_callback_query(call.id, "⛔ فقط فردی که لینک رو فرستاده می‌تونه کیفیت رو انتخاب کنه.", show_alert=True)
            return

        # Claim-before-download: pop (atomic get+remove) means a second click
        # (double tap, or a click while the first download is still running on
        # another worker thread) can't start a parallel duplicate download and
        # send the file twice.

        opt = session["options"].get(key)
        if not opt:
            bot.answer_callback_query(call.id, "❌ این گزینه دیگه معتبر نیست.", show_alert=True)
            return

        bot.answer_callback_query(call.id, "⏳ در حال دانلود...")
        try:
            bot.edit_message_text(
                f"⏳ در حال دانلود ({opt['label']})...",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
            )
        except Exception:
            pass

        request_dir = os.path.join(YOUTUBE_DL_DIR, uuid.uuid4().hex[:12])
        os.makedirs(request_dir, exist_ok=True)

        try:
            dest = _download_youtube_quality(session["url"], opt, request_dir)
            if not dest or not os.path.isfile(dest):
                try:
                    bot.edit_message_text(
                        "❌ دانلود ناموفق بود. دوباره امتحان کن.",
                        chat_id=call.message.chat.id, message_id=call.message.message_id,
                    )
                except Exception:
                    pass
                return

            caption = (session.get("title") or "")[:1024] or None

            if os.path.getsize(dest) > TELEGRAM_UPLOAD_MAX_BYTES:
                reply_target = call.message.reply_to_message.message_id if call.message.reply_to_message else None
                sent_id = send_large_file_via_userbot(
                    call.message.chat.id, dest, caption=caption, is_video=not opt["is_audio"],
                    reply_to_message_id=reply_target,
                )
                if sent_id:
                    repo.mark_bot_media_message(call.message.chat.id, sent_id, kind="youtube_userbot")
                    METRICS["youtube_downloads"] += 1
                    try:
                        bot.delete_message(call.message.chat.id, call.message.message_id)
                    except Exception:
                        pass
                else:
                    try:
                        bot.edit_message_text(
                            "⚠️ حجم فایل بیشتر از سقف مجاز تلگرامه (۵۰ مگابایت) و ارسال از طریق حساب کمکی هم ممکن "
                            "نشد (شاید تنظیم نشده، یا اون اکانت عضو این گروه نیست). یه کیفیت پایین‌تر رو امتحان کن.",
                            chat_id=call.message.chat.id, message_id=call.message.message_id,
                        )
                    except Exception:
                        pass
                return

            with open(dest, "rb") as fh:
                if opt["is_audio"]:
                    sent_msg = bot.send_audio(call.message.chat.id, fh, caption=caption, title=session.get("title"))
                else:
                    sent_msg = bot.send_video(call.message.chat.id, fh, caption=caption, supports_streaming=True)

            repo.mark_bot_media_message(call.message.chat.id, sent_msg.message_id, kind="youtube")
            METRICS["youtube_downloads"] += 1
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
        finally:
            shutil.rmtree(request_dir, ignore_errors=True)
    except Exception:
        log.error("YOUTUBE QUALITY CALLBACK ERROR", exc_info=True)
        admin_notifier.notify("youtube_download_error", traceback.format_exc(limit=5))
        try:
            bot.answer_callback_query(call.id, "❌ خطایی در دانلود رخ داد.")
        except Exception:
            pass
