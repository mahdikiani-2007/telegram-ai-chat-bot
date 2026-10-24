import logging
import threading
import time

import telebot

import infra  # noqa: F401 — side-effect import: configures apihelper
# (CUSTOM_REQUEST_SENDER, ENABLE_MIDDLEWARE, Local Bot API URLs) BEFORE
# the TeleBot instances below are constructed. Never remove or move this
# below the TeleBot() calls: importing bots first (e.g. handlers.guard)
# must still see middleware enabled.

from config import (
    ADMIN_ERROR_THROTTLE_SECONDS,
    OWNER_ID,
    TELEGRAM_TOKEN,
    TELEGRAM_TOKEN_LIGHT,
)

log = logging.getLogger("L-bot")


# =========================
# Bot + identity
# =========================
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True, num_threads=8)

_bot_identity = {"username": None, "id": None}
_identity_lock = threading.Lock()


def get_bot_identity():
    if _bot_identity["id"] is None:
        with _identity_lock:
            if _bot_identity["id"] is None:
                try:
                    me = bot.get_me()
                    _bot_identity["username"] = (me.username or "").lower()
                    _bot_identity["id"] = me.id
                except Exception:
                    log.error("get_me failed; will retry next message", exc_info=True)
    return _bot_identity


# =========================
# Bot Light (Yagami) + identity
# =========================
bot_light = None
if TELEGRAM_TOKEN_LIGHT:
    bot_light = telebot.TeleBot(TELEGRAM_TOKEN_LIGHT, threaded=True, num_threads=8)

_bot_light_identity = {"username": None, "id": None}
_identity_light_lock = threading.Lock()


def get_bot_light_identity():
    if _bot_light_identity["id"] is None:
        with _identity_light_lock:
            if _bot_light_identity["id"] is None:
                try:
                    me = bot_light.get_me()
                    _bot_light_identity["username"] = (me.username or "").lower()
                    _bot_light_identity["id"] = me.id
                except Exception:
                    log.error("get_bot_light_identity failed", exc_info=True)
    return _bot_light_identity


# Token lookup for the raw Rich Message HTTP calls below (pyTelegramBotAPI
# 4.26.0 has no typed sendRichMessage support yet, so we hit the Bot API
# directly and need each bot instance's token).
_BOT_TOKENS = {id(bot): TELEGRAM_TOKEN}
if bot_light:
    _BOT_TOKENS[id(bot_light)] = TELEGRAM_TOKEN_LIGHT

# =========================
# Feature 3: Throttled admin error notifier
# =========================
class AdminNotifier:
    """Sends a short alert to ADMIN_ID for high-level runtime errors, throttled
    per error-type so a crash loop can't spam the admin's DMs."""

    def __init__(self, bot_instance, admin_id, throttle_seconds=60):
        self.bot = bot_instance
        self.admin_id = admin_id
        self.throttle = throttle_seconds
        self._last_sent = {}
        self._lock = threading.Lock()

    def notify(self, error_type, detail=""):
        if not self.admin_id:
            return
        now = time.time()
        with self._lock:
            last = self._last_sent.get(error_type, 0)
            if now - last < self.throttle:
                return
            self._last_sent[error_type] = now
        try:
            text = f"🚨 **خطای سیستمی**\n\nنوع: `{error_type}`\n\n```\n{detail[:3500]}\n```"
            self.bot.send_message(self.admin_id, text, parse_mode="Markdown")
        except Exception:
            # Markdown with unbalanced backticks/underscores in `detail` makes
            # sendMessage return 400 'Can't parse entities' — retry as plain
            # text so the admin actually hears about the error.
            log.warning("AdminNotifier: markdown notify failed; retrying as plain text", exc_info=True)
            try:
                self.bot.send_message(
                    self.admin_id,
                    f"🚨 خطای سیستمی\n\nنوع: {error_type}\n\n{detail[:3500]}",
                )
            except Exception:
                log.warning("AdminNotifier: failed to notify admin", exc_info=True)


# The notifier targets OWNER_ID (first ADMIN_IDS entry, else legacy ADMIN_ID —
# config guarantees OWNER_ID == ADMIN_ID in the legacy-only case). Reading the
# legacy ADMIN_ID directly used to mute every error alert whenever the deploy
# only configured ADMIN_IDS (its default is 0 → notify() silently returned).
admin_notifier = AdminNotifier(bot, OWNER_ID, ADMIN_ERROR_THROTTLE_SECONDS)
