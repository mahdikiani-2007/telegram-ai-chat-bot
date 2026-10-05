import logging
import sys
import threading
import traceback

import ig_relay
from telebot import types

from bots import admin_notifier, bot, bot_light
from config import (
    DB_FILE,
    DROP_PENDING_UPDATES,
    OWNER_ID,
    SHUTDOWN_GRACE_SECONDS,
    TELEGRAM_UPLOAD_MAX_BYTES,
)
from db import repo
from metrics import METRICS
from services.media import download_instagram_post, extract_instagram_urls

log = logging.getLogger("L-bot")

# Set by the SIGTERM/SIGINT handler (installed in main()). Polling loops
# check it instead of looping forever; the main thread waits on it instead
# of sleeping so shutdown is prompt (time.sleep is NOT interrupted by
# signal handlers under PEP 475).
shutdown_event = threading.Event()


def _request_shutdown(signum, frame):
    """Signal handler: first signal starts the graceful sequence (run by the
    main thread after its wait returns); a second signal forces fast exit."""
    if shutdown_event.is_set():
        log.warning("Second shutdown signal (%s) — forcing immediate exit.", signum)
        sys.exit(1)
    log.info("Shutdown signal received (%s) — stopping polling.", signum)
    shutdown_event.set()


def register_all():
    # Import handler modules in dispatch order (telebot
    # first-match-wins) and register the relay command. Must run
    # before polling starts; safe to call once.
    import handlers.guard  # noqa: F401  (foreign commands + middleware first)
    ig_relay.register(bot=bot, admin_id=OWNER_ID, get_deps=_ig_relay_deps)
    import handlers.admin  # noqa: F401
    import handlers.panel  # noqa: F401  (panel callbacks + input interceptor before tasks wizards)
    import handlers.tasks  # noqa: F401
    import handlers.basics  # noqa: F401
    import handlers.surfaces  # noqa: F401
    import handlers.debate  # noqa: F401
    import handlers.medialink  # noqa: F401
    import handlers.memory_cmds  # noqa: F401
    import handlers.chat  # noqa: F401  (catch-alls register last)

    # Central ban + maintenance gate on BOTH bot instances (verified single
    # dispatch entry: TeleBot.process_new_updates; middleware can't cancel).
    # Re-apply DB-backed runtime settings (rate limits, Tavily budget) so a
    # restart picks up panel-changed values. Leftover 'running' broadcasts
    # died with the previous process: mark interrupted (never auto-resume —
    # that would duplicate sends).
    from services.panel_settings import install_central_gate, reapply_runtime_settings

    reapply_runtime_settings()
    try:
        interrupted = repo.mark_interrupted_broadcasts()
        if interrupted:
            log.warning("Marked %d leftover broadcast(s) as interrupted.", interrupted)
    except Exception:
        log.warning("interrupted-broadcast marking failed", exc_info=True)
    install_central_gate(bot)
    if bot_light is not None:
        install_central_gate(bot_light)


def _ig_relay_deps():
    return {
        "metrics": METRICS,
        "admin_notifier": admin_notifier,
        "mark_bot_media": repo.mark_bot_media_message,
        "download_post": download_instagram_post,
        "text_url_extractor": extract_instagram_urls,
        "upload_max_bytes": TELEGRAM_UPLOAD_MAX_BYTES,
        "db_file": DB_FILE,
    }

# =========================
# Start Bot
# =========================
# Passing allowed_updates to getUpdates REPLACES Telegram's default set, not
# adds to it — so this list must include everything the bot already relies
# on (messages, callbacks, inline mode) plus the new "guest_message" type
# (Bot API 10.x Guest Mode), which — like chat_member and a few other newer
# update types before it — is NOT delivered unless explicitly requested.
POLLING_ALLOWED_UPDATES = [
    "message", "edited_message", "callback_query",
    "inline_query", "chosen_inline_result", "guest_message",
]

def _sync_bot_commands():
    """Push the command menus to Telegram via setMyCommands so the client UI
    (the '/' menu, command autocomplete, bot description hints) always matches
    the handlers actually registered in this file — no manual BotFather edits
    after adding or renaming a command. Each bot gets its own list, since
    bot_light only registers a subset of handlers."""
    l_commands = [
        types.BotCommand("start", "Initiate the analytical matrix"),
        types.BotCommand("new", "Open a fresh conversation thread (private chats)"),
        types.BotCommand("session", "List, switch, rename, or delete conversation threads"),
        types.BotCommand("stop", "Cancel the current in-flight response"),
        types.BotCommand("reset", "Clear chat history and files (Admin only)"),
        types.BotCommand("pic", "Write your prompt after /pic to generate your photo"),
        types.BotCommand("tr", "Translate text or images. Usage: /tr [lang] [text] (default: Persian)"),
        types.BotCommand("todos", "View the active task list with done/delete buttons"),
        types.BotCommand("todo", "Manage tasks, set recurrence, or delegate using @username"),
        types.BotCommand("remind", "Schedule alerts, recurring reminders, or delegate tasks"),
        types.BotCommand("tldr", "Summarize recent group chat (groups only)"),
        types.BotCommand("myfacts", "View what the bot has durably learned about you"),
        types.BotCommand("forget", "Delete learned facts: /forget [number] or /forget all"),
        types.BotCommand("stats", "View real-time bot metrics and system performance (Admin only)"),
        types.BotCommand("digest", "Trigger the daily news digest on demand (Admin only)"),
        types.BotCommand("admin", "Open the broadcast and management panel (Admin only)"),
        types.BotCommand("relay", "Instagram to Telegram post relay control (Admin only)"),
        types.BotCommand("stopall", "Halt all active debates instantly (Admin only)"),
    ]
    light_commands = [
        types.BotCommand("start", "Initiate the Kira persona"),
        types.BotCommand("stop", "Cancel the current in-flight response"),
        types.BotCommand("stopall", "Halt all active debates instantly (Admin only)"),
    ]

    targets = [(bot, l_commands, "L")]
    if bot_light:
        targets.append((bot_light, light_commands, "Light"))

    for bot_instance, commands, label in targets:
        try:
            bot_instance.set_my_commands(commands)
            log.info("Command menu synced to Telegram for bot %s (%d commands).", label, len(commands))
        except Exception:
            # Menu sync is cosmetic — a failure here must never block startup.
            log.error("setMyCommands failed for bot %s", label, exc_info=True)

def _ensure_polling_mode(bot_instance):
    """Delete any webhook so getUpdates polling receives updates. The
    pending server-side queue is preserved unless DROP_PENDING_UPDATES=1."""
    bot_instance.delete_webhook(drop_pending_updates=DROP_PENDING_UPDATES)


def run_bot_l():
    """Run Bot L (detective) polling in a daemon thread. Supervised: tracks
    restarts so /stats can surface instability instead of failing silently.
    Exits (instead of restarting) once shutdown is requested."""
    while not shutdown_event.is_set():
        try:
            _ensure_polling_mode(bot)
            bot.infinity_polling(timeout=20, long_polling_timeout=20, allowed_updates=POLLING_ALLOWED_UPDATES)
        except Exception:
            METRICS["poller_restarts"] = METRICS.get("poller_restarts", 0) + 1
            log.error("Bot L polling crashed; restarting in 5s (restart #%d)",
                      METRICS["poller_restarts"], exc_info=True)
            admin_notifier.notify("bot_l_polling_crash", traceback.format_exc(limit=5))
            shutdown_event.wait(5)

def run_bot_light():
    """Run Bot Light (Yagami) polling in a daemon thread. Only if token is set.
    Exits (instead of restarting) once shutdown is requested."""
    if not bot_light:
        return
    while not shutdown_event.is_set():
        try:
            _ensure_polling_mode(bot_light)
            bot_light.infinity_polling(timeout=20, long_polling_timeout=20, allowed_updates=POLLING_ALLOWED_UPDATES)
        except Exception:
            METRICS["poller_restarts"] = METRICS.get("poller_restarts", 0) + 1
            log.error("Bot Light polling crashed; restarting in 5s (restart #%d)",
                      METRICS["poller_restarts"], exc_info=True)
            admin_notifier.notify("bot_light_polling_crash", traceback.format_exc(limit=5))
            shutdown_event.wait(5)


def _shutdown_sequence(polling_threads, grace_seconds=None):
    """Graceful stop, run once by the main thread after shutdown_event is
    set: stop accepting updates, let in-flight work finish (bounded by
    grace), join polling threads, checkpoint WAL. Every step is contained
    so one failure can't skip the rest; never raises."""
    grace = SHUTDOWN_GRACE_SECONDS if grace_seconds is None else grace_seconds
    log.info("Shutdown sequence started (grace=%ss).", grace)
    for label, inst in (("L", bot), ("Light", bot_light)):
        if inst is None:
            continue
        try:
            inst.stop_polling()
            log.info("Bot %s polling stopped (no new updates).", label)
        except Exception:
            log.warning("Bot %s stop_polling failed", label, exc_info=True)
    try:
        shutdown_event.wait(grace)
        log.info("Grace period elapsed; joining polling threads.")
    except Exception:
        log.warning("Grace wait failed", exc_info=True)
    for thread in polling_threads:
        try:
            thread.join(timeout=max(1.0, float(grace) + 10))
            if thread.is_alive():
                log.warning("Polling thread %s still alive after join timeout.",
                            thread.name)
        except Exception:
            log.warning("Joining polling thread failed", exc_info=True)
    try:
        busy, log_frames, done = repo.checkpoint_wal()
        log.info("WAL checkpoint done (busy=%s log=%s checkpointed=%s).",
                 busy, log_frames, done)
    except Exception:
        log.warning("WAL checkpoint failed", exc_info=True)
    log.info("Shutdown sequence complete.")
