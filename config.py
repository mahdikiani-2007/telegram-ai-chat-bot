import datetime
import logging
import os

# Process-wide logging setup. Lives here (not bot.py) so it runs no matter
# which module is imported first — mirrors the original L0 basicConfig that
# executed before anything else at bot.py import time.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


# =========================
# Environment Variables
# =========================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
AGNES_API_KEY = os.environ.get("AGNES_API_KEY")
CHAT_API_KEY = os.environ.get("CHAT_API_KEY", AGNES_API_KEY)
IMAGE_API_KEY = os.environ.get("IMAGE_API_KEY", AGNES_API_KEY)
TELEGRAM_TOKEN_LIGHT = os.environ.get("TELEGRAM_TOKEN_LIGHT")

# Local Bot API Server (optional — see Railway/Docker deployment notes).
# When set, ALL Bot API calls get redirected here instead of the default
# https://api.telegram.org — this is what raises the upload cap from 50MB
# to 2000MB. Needs TG_API_ID/TG_API_HASH (same my.telegram.org credentials
# already used by the optional userbot feature below) since the server
# itself needs its own Telegram API application credentials, separate from
# the bot token. Leave unset to keep using the normal cloud API unchanged.
LOCAL_BOT_API_URL = os.environ.get("LOCAL_BOT_API_URL", "").rstrip("/")
TELEGRAM_API_BASE_URL = f"{LOCAL_BOT_API_URL}/bot{{0}}/{{1}}" if LOCAL_BOT_API_URL else "https://api.telegram.org/bot{0}/{1}"
TELEGRAM_FILE_BASE_URL = f"{LOCAL_BOT_API_URL}/file/bot{{0}}/{{1}}" if LOCAL_BOT_API_URL else "https://api.telegram.org/file/bot{0}/{1}"

# Instagram downloader (yt-dlp based). Public posts/reels only.
# INSTAGRAM_COOKIES_FILE is optional but strongly recommended: a Netscape-
# format cookies.txt exported from a browser logged into a DEDICATED
# Instagram account (not your personal one — see chat notes on why).
# Without it, downloads work intermittently since Instagram randomly
# throttles/soft-blocks anonymous scraping — that's an Instagram-side
# limitation, not something this code can fully work around.
INSTAGRAM_COOKIES_FILE = os.environ.get("INSTAGRAM_COOKIES_FILE")
# 50MB via the normal cloud Bot API; ~1950MB (safety margin under the
# documented 2000MB) once a Local Bot API Server is active. Shared by the
# Instagram + YouTube downloaders.
TELEGRAM_UPLOAD_MAX_BYTES = (1950 if LOCAL_BOT_API_URL else 49) * 1024 * 1024

# YouTube downloader (yt-dlp based). Quality is capped at YOUTUBE_MAX_HEIGHT;
# whether qualities above whatever YouTube serves progressively (pre-muxed,
# no merge needed) are actually offered depends on ffmpeg being available —
# see _ffmpeg_location(), which prefers a real system ffmpeg (present on
# this Railway/Docker image) and falls back to the portable imageio-ffmpeg
# package on hosts without one.
YOUTUBE_MAX_HEIGHT = int(os.environ.get("YOUTUBE_MAX_HEIGHT", "1080"))
# Optional but recommended: a Netscape-format cookies.txt exported from a
# browser logged into a DEDICATED YouTube/Google account (not your personal
# one — same reasoning as INSTAGRAM_COOKIES_FILE above). Without it,
# extraction can intermittently fail with "This video is not available"
# even for perfectly public videos — YouTube applies extra scrutiny to
# anonymous requests from datacenter/cloud IP ranges (which is exactly what
# Railway's servers are), and a logged-in session avoids a lot of that.
YOUTUBE_COOKIES_FILE = os.environ.get("YOUTUBE_COOKIES_FILE")

# Large-file delivery via a dedicated userbot (Pyrogram/MTProto) — bypasses
# the Bot API's 50MB upload cap using a REAL Telegram account (up to 2GB,
# 4GB with Premium) instead of the bot token. Optional: unset by default,
# everything else behaves exactly as before if these aren't configured.
# First-time login (phone number + SMS code) MUST be done locally/
# interactively beforehand — a server process can't answer an SMS prompt —
# then the resulting .session file gets uploaded alongside bot.py. See
# chat setup notes for the exact steps.
TG_API_ID = int(os.environ.get("TG_API_ID", "0"))
TG_API_HASH = os.environ.get("TG_API_HASH", "")
TG_SESSION_NAME = os.environ.get("TG_SESSION_NAME", "yt_uploader")
TG_SESSION_DIR = os.environ.get("TG_SESSION_DIR")  # default (DATA_DIR) is backfilled below, once DATA_DIR exists

# Web search (Tavily). Optional — if TAVILY_API_KEY isn't set, the search
# feature quietly disables itself (no crash, bot behaves exactly as before).
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
TAVILY_SEARCH_URL = os.environ.get("TAVILY_SEARCH_URL", "https://api.tavily.com/search")
TAVILY_MAX_RESULTS = int(os.environ.get("TAVILY_MAX_RESULTS", "5"))
# Coarse daily safety cap so one busy/looping day can't burn through the
# whole monthly Tavily credit allowance. 0 or negative = no cap.
TAVILY_DAILY_LIMIT = int(os.environ.get("TAVILY_DAILY_LIMIT", "40"))

# Daily news digest (Iran-time, two sections: Iran/world news + tech/AI news).
DIGEST_HOUR_IRAN = int(os.environ.get("DIGEST_HOUR_IRAN", "9"))
DIGEST_MINUTE_IRAN = int(os.environ.get("DIGEST_MINUTE_IRAN", "0"))

# Daily DB backup (Iran-time). The gzipped SQLite snapshot gets sent to the
# admin's private chat once a day so a wiped Railway volume can never take
# the community's memory with it.
BACKUP_HOUR_IRAN = int(os.environ.get("BACKUP_HOUR_IRAN", "4"))

# =========================
# Admin Configuration
# =========================
_log = logging.getLogger("L-bot")

try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))  # legacy single admin; kept exported so existing imports keep working
except (TypeError, ValueError):
    _log.warning("ADMIN_ID: ignoring invalid value %r (must be a positive integer).", os.environ.get("ADMIN_ID"))
    ADMIN_ID = 0
if ADMIN_ID < 0:
    _log.warning("ADMIN_ID: ignoring invalid value %r (must be a positive integer).", ADMIN_ID)
    ADMIN_ID = 0


def _parse_admin_ids(raw):
    """Parse comma-separated admin IDs. Only positive ints survive; anything
    else is ignored with a warning (fail closed: a typo must never morph
    into admin rights for id 0 or a stranger)."""
    ids = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except (TypeError, ValueError):
            _log.warning("ADMIN_IDS: ignoring invalid entry %r (not an integer).", part)
            continue
        if value <= 0:
            _log.warning("ADMIN_IDS: ignoring invalid entry %r (must be a positive integer).", part)
            continue
        if value not in ids:
            ids.append(value)
    return ids


ADMIN_IDS = tuple(_parse_admin_ids(os.environ.get("ADMIN_IDS", "")))

# Owner = first ID in ADMIN_IDS if set, else legacy ADMIN_ID. On conflict
# ADMIN_IDS[0] wins (warned, since two sources disagreeing is a config bug).
if ADMIN_IDS:
    OWNER_ID = ADMIN_IDS[0]
    if ADMIN_ID and ADMIN_ID != OWNER_ID:
        _log.warning(
            "ADMIN_ID (%d) differs from ADMIN_IDS[0] (%d) — ADMIN_IDS[0] wins as owner.",
            ADMIN_ID, OWNER_ID,
        )
elif ADMIN_ID > 0:
    OWNER_ID = ADMIN_ID
    ADMIN_IDS = (ADMIN_ID,)
else:
    OWNER_ID = 0

if not OWNER_ID:
    _log.warning("No owner configured (set ADMIN_IDS or ADMIN_ID) — the admin panel is disabled.")

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not found")
if not AGNES_API_KEY and not CHAT_API_KEY:
    raise ValueError("API KEY not found")

# Persistent storage. Defaults to a "data" folder next to this script
# (auto-created on first run); override with DATA_DIR if you want the
# SQLite file to live somewhere else (e.g. a separate disk/volume).
DATA_DIR = os.environ.get(
    "DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
)
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "chat_history.db")
TG_SESSION_DIR = TG_SESSION_DIR or DATA_DIR  # backfilled default now that DATA_DIR exists
INSTAGRAM_DL_DIR = os.path.join(DATA_DIR, "ig_downloads")
os.makedirs(INSTAGRAM_DL_DIR, exist_ok=True)
YOUTUBE_DL_DIR = os.path.join(DATA_DIR, "yt_downloads")
os.makedirs(YOUTUBE_DL_DIR, exist_ok=True)

# Model / endpoint configuration (override via env vars, e.g. in a .env / systemd unit).
CHAT_API_BASE = os.environ.get("CHAT_API_BASE", "https://opencode.ai/zen/v1")
AGNES_BASE = os.environ.get("AGNES_BASE", "https://apihub.agnes-ai.com/v1")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "mimo-v2.5-free")
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "agnes-image-2.0-flash")
# Vision (photo understanding) runs on a DEDICATED endpoint, not the chat
# router: the deployed chat router (a proxy whose "model" alias is opaque)
# answers "I can't see" for images even though multimodal providers exist —
# verified live 2026-10-23 (agnes-3.0-flash correctly described a test image,
# the chat router's alias did not receive it). Defaults to the Agnes hub with
# the same key /pic already uses, so photo turns work with zero new env vars.
VISION_MODEL = os.environ.get("VISION_MODEL", "agnes-3.0-flash")
VISION_API_BASE = os.environ.get("VISION_API_BASE", AGNES_BASE)
VISION_API_KEY = os.environ.get("VISION_API_KEY", AGNES_API_KEY)

# =========================
# Mini App (games) — HTML served by the bot process itself over stdlib
# http.server; Telegram opens it as a Mini App via /play (web_app button in
# private, t.me startapp URL button in groups) or the chat menu button.
# =========================
MINIAPP_PORT = int(os.environ.get("MINIAPP_PORT") or os.environ.get("PORT") or "8080")
MINIAPP_PUBLIC_URL = (
    os.environ.get("MINIAPP_PUBLIC_URL")
    or (f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}"
        if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else "")
).rstrip("/")
# CHAT_API_BASE is a free multi-provider router (observed routing to
# Mistral, likely others too); a 400 from one backend provider doesn't mean
# the payload was bad, it means THAT provider is currently flaky — a retry
# often lands on a different, working provider. This is how many chat/generate
# calls get an extra couple of attempts before giving up.
CHAT_RETRY_ATTEMPTS = int(os.environ.get("CHAT_RETRY_ATTEMPTS", "2"))

# Behaviour tuning.
# RECENT_HISTORY / SUMMARIZE_THRESHOLD were widened from the original 8/40 to
# give the bot noticeably deeper working memory. SUMMARIZE_BATCH is sized so
# a compression pass always leaves a comfortable buffer above RECENT_HISTORY
# (60 - 40 = 20 rows remaining right after a pass, never starving get_history).
RECENT_HISTORY = int(os.environ.get("RECENT_HISTORY", "18"))  # last-N turns kept verbatim
SUMMARIZE_THRESHOLD = int(os.environ.get("SUMMARIZE_THRESHOLD", "60"))  # rows before compression
SUMMARIZE_BATCH = int(os.environ.get("SUMMARIZE_BATCH", "40"))  # oldest rows compressed per pass
STREAM_EDIT_INTERVAL = float(os.environ.get("STREAM_EDIT_INTERVAL", "1.2"))  # seconds

# Scheduler / concurrency tuning (modest community-scale defaults).
SCHEDULER_MAX_WORKERS = int(os.environ.get("SCHEDULER_MAX_WORKERS", "5"))
MAX_DISPATCH_RETRIES = int(os.environ.get("MAX_DISPATCH_RETRIES", "5"))

# Session TTL / garbage-collection tuning for the in-memory wizard state.
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "900"))  # 15 min
SESSION_GC_INTERVAL = int(os.environ.get("SESSION_GC_INTERVAL", "300"))  # 5 min

# Throttled admin crash/error notifications.
ADMIN_ERROR_THROTTLE_SECONDS = int(os.environ.get("ADMIN_ERROR_THROTTLE_SECONDS", "60"))

# Pending-update handling on (re)start. Default False = preserve Telegram's
# server-side queue across redeploys and poller crash-restarts so no user
# message is silently dropped. Set to 1/true/yes ONLY as a one-time escape
# hatch to skip a stale backlog after a long outage (then unset it).
DROP_PENDING_UPDATES = os.environ.get("DROP_PENDING_UPDATES", "false").strip().lower() in ("1", "true", "yes")

# Graceful shutdown: how long the shutdown sequence waits for in-flight
# polling/handlers to finish before checkpointing WAL and exiting. Must fit
# under the platform's SIGTERM->SIGKILL grace period.
SHUTDOWN_GRACE_SECONDS = int(os.environ.get("SHUTDOWN_GRACE_SECONDS", "20"))

# On-disk data retention (data-safety phase). Purged by the daily backup job,
# before the snapshot is taken, so backups stay small too.
DB_RETENTION_DAYS = int(os.environ.get("DB_RETENTION_DAYS", "90"))
# Warn the admin when chat_history.db grows past this (MB). 0/negative = off.
DB_SIZE_WARN_MB = int(os.environ.get("DB_SIZE_WARN_MB", "500"))
# Local snapshot rotation: how many newest daily archives to keep in
# DATA_DIR/backups (independently of the copy sent to the admin's DM).
BACKUP_KEEP_DAILY = int(os.environ.get("BACKUP_KEEP_DAILY", "7"))

# =========================
# Iran Timezone Constants (UTC+3:30) — Python 3.12-safe, tz-aware throughout.
# =========================
IRAN_OFFSET_SECONDS = 3 * 3600 + 30 * 60  # UTC+3:30 = 12600 seconds
IRAN_TZ = datetime.timezone(datetime.timedelta(seconds=IRAN_OFFSET_SECONDS))
UTC_TZ = datetime.timezone.utc


# Retention window (days) for digest_history bullet lines.
# Moved here from the digest section: ChatRepository.save_digest_lines also
# uses it, and a single owner here avoids a db <-> digest import cycle.
DIGEST_MEMORY_DAYS = 7
