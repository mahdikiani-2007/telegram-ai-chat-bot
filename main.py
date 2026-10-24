import logging
import os
import shutil
import signal
import threading
import time

import ig_relay
import yt_dlp

from bots import (
    bot,
    bot_light,
    get_bot_identity,
    get_bot_light_identity,
)
from config import (
    BACKUP_HOUR_IRAN,
    DB_FILE,
    INSTAGRAM_COOKIES_FILE,
    YOUTUBE_COOKIES_FILE,
)
from services.backup import _backup_scheduler_loop
from services.digest import _digest_scheduler_loop
from services.media import _userbot_thread_main
from services.memory import (
    BEHAVIOR_PROFILE_HOUR,
    _behavior_profiler_loop,
    fact_extractor,
)
from services.scheduling import _reminder_scheduler_loop
from sessions import _session_gc_loop
from wiring import (
    _ensure_polling_mode,
    _request_shutdown,
    _shutdown_sequence,
    _sync_bot_commands,
    register_all,
    run_bot_l,
    run_bot_light,
    shutdown_event,
)

log = logging.getLogger("L-bot")


def main():
    """Application entrypoint (moved verbatim from bot.py's __main__ block):
    diagnostics, handler registration, daemon workers, polling threads."""
    log.info("Clearing webhook hooks and starting fresh instance...")
    log.info("Using DB at: %s", DB_FILE)

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    # Handler registration first: in the monolith this happened at import
    # time (decorators), i.e. before every diagnostic below. Registering
    # here preserves that order — notably the guest-handler count logged
    # next — while keeping import side effects out of handler modules.
    register_all()

    # Guest Mode diagnostics — this is the exact deployment gotcha that
    # silently breaks guest_message support: requirements.txt says 4.36.0,
    # but if the running process is still on an old venv/cached install, none
    # of this fails loudly, it just never receives guest_message updates.
    try:
        import importlib.metadata as _ilm
        _ptba_version = _ilm.version("pyTelegramBotAPI")
    except Exception:
        _ptba_version = "unknown"
    _has_guest_support = hasattr(bot, "register_guest_message_handler") and hasattr(bot, "answer_guest_query")
    log.info(
        "pyTelegramBotAPI version=%s | guest-mode API present=%s | guest handlers registered on bot=%d%s",
        _ptba_version, _has_guest_support, len(getattr(bot, "guest_message_handlers", [])),
        f", on bot_light={len(getattr(bot_light, 'guest_message_handlers', []))}" if bot_light else "",
    )
    if not _has_guest_support:
        log.error(
            "pyTelegramBotAPI %s has NO Guest Mode support (need >=4.36.0). "
            "Guest Mode messages will silently never reach this bot until the venv is upgraded "
            "(pip install -r requirements.txt in the ACTUAL venv this process runs from, then restart).",
            _ptba_version,
        )

    # Instagram downloader diagnostics.
    try:
        _ytdlp_version = yt_dlp.version.__version__
    except Exception:
        _ytdlp_version = "unknown"
    if INSTAGRAM_COOKIES_FILE:
        _cookies_ok = os.path.isfile(INSTAGRAM_COOKIES_FILE)
        log.info(
            "Instagram downloader: yt-dlp=%s, cookies file=%s (%s)",
            _ytdlp_version, INSTAGRAM_COOKIES_FILE, "found" if _cookies_ok else "MISSING",
        )
        if not _cookies_ok:
            log.warning(
                "INSTAGRAM_COOKIES_FILE is set but the file doesn't exist at that path — "
                "downloads will run anonymously and fail more often (Instagram rate-limits/blocks that)."
            )
    else:
        log.info(
            "Instagram downloader: yt-dlp=%s, no cookies file configured — running anonymously "
            "(works, but Instagram will intermittently reject requests without a logged-in session).",
            _ytdlp_version,
        )

    # YouTube downloader diagnostics.
    _deno_path = shutil.which("deno")
    if _deno_path:
        log.info(
            "YouTube downloader: yt-dlp=%s, JS runtime deno=%s (EJS challenge solving available).",
            _ytdlp_version, _deno_path,
        )
    else:
        log.warning(
            "YouTube downloader: yt-dlp=%s, NO deno JS runtime on PATH — YouTube's JS challenges "
            "can't be solved, expect \"The page needs to be reloaded\" errors.",
            _ytdlp_version,
        )
    if YOUTUBE_COOKIES_FILE:
        _yt_cookies_ok = os.path.isfile(YOUTUBE_COOKIES_FILE)
        log.info(
            "YouTube downloader: cookies file=%s (%s)",
            YOUTUBE_COOKIES_FILE, "found" if _yt_cookies_ok else "MISSING",
        )
        if not _yt_cookies_ok:
            log.warning(
                "YOUTUBE_COOKIES_FILE is set but the file doesn't exist at that path — "
                "extraction will run anonymously and may intermittently fail with "
                "\"This video is not available\" (YouTube scrutinizes datacenter IPs harder)."
            )
    else:
        log.info(
            "YouTube downloader: no cookies file configured — running anonymously "
            "(works, but may intermittently fail with \"This video is not available\" from a cloud-hosted IP)."
        )

    _ensure_polling_mode(bot)
    time.sleep(1)

    _sync_bot_commands()

    get_bot_identity()
    if bot_light:
        try:
            get_bot_light_identity()
        except Exception:
            log.warning("Could not warm Light identity cache", exc_info=True)

    scheduler_thread = threading.Thread(
        target=_reminder_scheduler_loop, daemon=True, name="reminder-scheduler"
    )
    scheduler_thread.start()
    log.info("Reminder scheduler daemon thread launched.")

    gc_thread = threading.Thread(target=_session_gc_loop, daemon=True, name="session-gc")
    gc_thread.start()
    log.info("Session GC daemon thread launched.")

    digest_thread = threading.Thread(target=_digest_scheduler_loop, daemon=True, name="digest-scheduler")
    digest_thread.start()
    log.info("Digest scheduler daemon thread launched.")

    backup_thread = threading.Thread(target=_backup_scheduler_loop, daemon=True, name="backup-scheduler")
    backup_thread.start()
    log.info("Daily backup scheduler daemon thread launched (target=%02d:00 Iran).", BACKUP_HOUR_IRAN)

    facts_thread = threading.Thread(target=fact_extractor.run_forever, daemon=True, name="fact-extractor")
    facts_thread.start()
    log.info("Fact-extractor worker thread launched (long-term user memory).")

    behavior_thread = threading.Thread(target=_behavior_profiler_loop, daemon=True, name="behavior-profiler")
    behavior_thread.start()
    log.info("Behavior profiler daemon thread launched (target=%02d:30 Iran).", BEHAVIOR_PROFILE_HOUR)

    userbot_thread = threading.Thread(target=_userbot_thread_main, daemon=True, name="userbot-mtproto")
    userbot_thread.start()
    log.info("Userbot thread launched (large-file delivery — no-op if TG_API_ID/TG_API_HASH unset).")

    ig_relay.start()

    polling_threads = []
    if not shutdown_event.is_set():
        l_thread = threading.Thread(target=run_bot_l, daemon=True, name="bot-l-polling")
        l_thread.start()
        polling_threads.append(l_thread)
        log.info("Bot L polling thread launched.")

        if bot_light:
            light_thread = threading.Thread(target=run_bot_light, daemon=True, name="bot-light-polling")
            light_thread.start()
            polling_threads.append(light_thread)
            log.info("Bot Light polling thread launched.")
        else:
            log.info("TELEGRAM_TOKEN_LIGHT not set — Bot Light disabled.")
    else:
        log.info("Shutdown requested during startup — polling threads not launched.")

    while not shutdown_event.is_set():
        shutdown_event.wait(60)

    _shutdown_sequence(polling_threads)


if __name__ == "__main__":
    main()
