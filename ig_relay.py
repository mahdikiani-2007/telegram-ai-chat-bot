"""Instagram -> Telegram relay (standalone, optional feature).

Reads posts shared into a configured Instagram group DM (via the unofficial
instagrapi private API — the official Meta API does NOT support group DMs)
and republishes them into a configured Telegram group:

    👤 Mehdi (@mehdi123) این پست رو فرستاد:
    <media, captioned with the original Instagram caption>

Integration surface with bot.py (two additive hooks only):
  * ig_relay.register(bot, admin_id, get_deps)
        Registers the /relay admin command. MUST be called at module level
        BEFORE bot.py's catch-all handle_all_messages handler, otherwise
        telebot dispatches /relay to the catch-all first and the command
        never reaches us.
  * ig_relay.start(get_deps)
        Launches the polling daemon thread. Call from __main__.

Everything else (env config, SQLite store, login/session handling, poll
loop, message parsing, upload, /relay subcommands) lives in this file.

Required env vars (feature stays disabled without them):
  IG_RELAY_USERNAME / IG_RELAY_PASSWORD   dedicated (non-personal!) IG account
Optional env vars:
  IG_RELAY_TARGET_CHAT    default Telegram target chat id (admin can override)
  IG_RELAY_THREADS        comma-separated Instagram thread ids
  IG_RELAY_POLL_SECONDS   poll interval, default 20 (min 5)
  IG_RELAY_MAX_ATTEMPTS   download/send retries per post, default 3
  IG_RELAY_LOGIN_BACKOFF  seconds between login retries after a failure, default 300
  IG_RELAY_CHALLENGE_BACKOFF  seconds to go silent after an IG checkpoint, default 900
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import traceback

log = logging.getLogger("ig-relay")

IG_RELAY_USERNAME = os.environ.get("IG_RELAY_USERNAME", "").strip()
IG_RELAY_PASSWORD = os.environ.get("IG_RELAY_PASSWORD", "")
IG_RELAY_TARGET_CHAT = os.environ.get("IG_RELAY_TARGET_CHAT", "").strip()
IG_RELAY_THREADS_ENV = os.environ.get("IG_RELAY_THREADS", "").strip()
IG_RELAY_POLL_SECONDS = max(5, int(os.environ.get("IG_RELAY_POLL_SECONDS", "20")))
IG_RELAY_MAX_ATTEMPTS = max(1, int(os.environ.get("IG_RELAY_MAX_ATTEMPTS", "3")))
IG_RELAY_LOGIN_BACKOFF = max(30, int(os.environ.get("IG_RELAY_LOGIN_BACKOFF", "300")))
# Instagram hands out "native_flow" checkpoints on direct_v2/threads/ when it
# decides our polling looks like scraping. They are NOT resolvable by code
# (instagrapi raises before any challenge_code_handler is consulted) and they do
# NOT clear by themselves — only manual confirmation in the official app does.
# While one is pending we must go silent: polling a challenged account every
# poll_seconds keeps the flag alive and walks the account toward a hard ban.
IG_RELAY_CHALLENGE_BACKOFF = max(300, int(os.environ.get("IG_RELAY_CHALLENGE_BACKOFF", "900")))

_DATA_DIR = os.environ.get(
    "DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
)
IG_RELAY_SESSION_FILE = os.path.join(_DATA_DIR, "ig_relay_session.json")

CAPTION_MAX = 1024
MEDIA_GROUP_CAP = 10
SEEN_KEEP = 2000
ALERT_THROTTLE_SECONDS = 120

_POST_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_DEEP_CODE_RE = re.compile(r"[?&]code=([A-Za-z0-9_-]+)", re.IGNORECASE)


def _permalink(code):
    return "https://www.instagram.com/p/{}/".format(code)


def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _dump(obj):
    if isinstance(obj, dict):
        return json.dumps(obj, default=str)
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        try:
            return json.dumps(model_dump(), default=str)
        except Exception:
            return ""
    return ""


def _find_code(node, depth=0):
    if depth > 4 or node is None:
        return None
    if isinstance(node, dict):
        code = node.get("code")
        if isinstance(code, str) and code:
            return code
        for value in node.values():
            found = _find_code(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_code(value, depth + 1)
            if found:
                return found
    return None


def extract_post_urls(msg, text_extractor=None):
    """Collect unique Instagram post/reel permalinks referenced by a DM
    message: plain-text links, media_share/clip (older share formats),
    xma_share/generic_xma (current share format), link messages, and — as a
    last resort — a regex sweep over the serialized message. Returns
    normalized https://www.instagram.com/p/<code>/ URLs."""
    urls = []
    seen = set()

    def add(raw):
        if not raw:
            return
        raw = str(raw).strip()
        match = _POST_URL_RE.search(raw)
        if match:
            url = _permalink(match.group(1))
        elif raw.lower().startswith("instagram://"):
            match = _DEEP_CODE_RE.search(raw)
            if not match:
                return
            url = _permalink(match.group(1))
        else:
            return
        if url not in seen:
            seen.add(url)
            urls.append(url)

    def add_code(code):
        if isinstance(code, str) and code:
            add(_permalink(code))

    text = _get(msg, "text") or ""
    if text:
        if text_extractor:
            try:
                for url in text_extractor(text) or []:
                    add(url)
            except Exception:
                log.warning("text_url_extractor failed", exc_info=True)
        for match in _POST_URL_RE.finditer(text):
            add(_permalink(match.group(1)))

    media_share = _get(msg, "media_share")
    if media_share:
        add_code(_get(media_share, "code"))

    clip = _get(msg, "clip")
    if clip:
        inner = _get(clip, "clip") or clip
        add_code(_get(inner, "code"))

    for key in ("xma_share", "generic_xma"):
        node = _get(msg, key)
        if not node:
            continue
        for item in (node if isinstance(node, list) else [node]):
            for field in ("video_url", "target_url", "link_url"):
                add(_get(item, field))

    link = _get(msg, "link")
    if link:
        context = _get(link, "link_context")
        before = len(urls)
        add(_get(context, "link_url"))
        if len(urls) == before:
            add(_get(link, "text"))

    reel_share = _get(msg, "reel_share")
    if isinstance(reel_share, (dict, list)):
        add_code(_find_code(reel_share))

    if not urls:
        for match in _POST_URL_RE.finditer(_dump(msg)):
            add(_permalink(match.group(1)))

    return urls


def sender_names(thread, user_id):
    """(full_name, username) of the message author, resolved against the
    thread participant list; empty strings when unknown."""
    target = str(user_id or "")
    if not target:
        return "", ""
    for user in _get(thread, "users") or []:
        pk = _get(user, "pk")
        uid = _get(user, "id")
        if target in (str(pk or ""), str(uid or "")):
            return (_get(user, "full_name") or "").strip(), (_get(user, "username") or "").strip()
    return "", ""


def format_attribution(full_name, username):
    full_name = (full_name or "").strip()
    username = (username or "").strip().lstrip("@")
    name = full_name or username or "کاربر ناشناس"
    if username and username != name:
        return "👤 {} (@{}) این پست رو فرستاد:".format(name, username)
    return "👤 {} این پست رو فرستاد:".format(name)


class RelayStore:
    """SQLite state for the relay: runtime config + dedupe/retry ledger.
    Uses its own connection to the same chat_history.db file, so bot.py's
    ChatRepository needs no changes."""

    def __init__(self, db_file):
        self.db_file = db_file
        self._lock = threading.Lock()
        self._conn = None
        self._init_schema()

    def _connect(self):
        if self._conn is None:
            parent = os.path.dirname(os.path.abspath(self.db_file))
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._conn = sqlite3.connect(self.db_file, timeout=30, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def _init_schema(self):
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ig_relay_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ig_relay_seen (
                    message_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    done_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ig_relay_seen_thread ON ig_relay_seen(thread_id)"
            )
            conn.commit()

    def get_config(self, key, default=""):
        with self._lock:
            row = self._connect().execute(
                "SELECT value FROM ig_relay_config WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else default

    def set_config(self, key, value):
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO ig_relay_config(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            conn.commit()

    def is_enabled(self):
        return self.get_config("enabled", "1") == "1"

    def get_target(self):
        target = self.get_config("target_chat") or IG_RELAY_TARGET_CHAT
        return target.strip()

    def get_threads(self):
        raw = self.get_config("threads")
        if raw:
            try:
                threads = json.loads(raw)
                return [str(t) for t in threads if str(t).strip()]
            except Exception:
                log.warning("ig_relay threads config is not valid JSON: %r", raw)
        if IG_RELAY_THREADS_ENV:
            return [t.strip() for t in IG_RELAY_THREADS_ENV.split(",") if t.strip()]
        return []

    def set_threads(self, threads):
        self.set_config("threads", json.dumps([str(t) for t in threads]))

    def get_seen(self, message_id):
        with self._lock:
            row = self._connect().execute(
                "SELECT attempts, done_at FROM ig_relay_seen WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def has_thread_seen(self, thread_id):
        with self._lock:
            row = self._connect().execute(
                "SELECT 1 FROM ig_relay_seen WHERE thread_id = ? LIMIT 1", (thread_id,)
            ).fetchone()
        return row is not None

    def seed_thread(self, thread_id, message_ids):
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            conn.executemany(
                "INSERT OR IGNORE INTO ig_relay_seen(message_id, thread_id, attempts, done_at) "
                "VALUES(?, ?, 0, ?)",
                [(str(mid), str(thread_id), now) for mid in message_ids],
            )
            conn.commit()

    def record_attempt(self, message_id, thread_id):
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO ig_relay_seen(message_id, thread_id, attempts) VALUES(?, ?, 1) "
                "ON CONFLICT(message_id) DO UPDATE SET attempts = attempts + 1",
                (str(message_id), str(thread_id)),
            )
            conn.commit()

    def mark_done(self, message_id, thread_id):
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO ig_relay_seen(message_id, thread_id, attempts, done_at) "
                "VALUES(?, ?, 1, ?) "
                "ON CONFLICT(message_id) DO UPDATE SET attempts = excluded.attempts, "
                "done_at = excluded.done_at",
                (str(message_id), str(thread_id), now),
            )
            conn.commit()

    def prune(self):
        with self._lock:
            conn = self._connect()
            conn.execute(
                "DELETE FROM ig_relay_seen WHERE message_id NOT IN "
                "(SELECT message_id FROM ig_relay_seen ORDER BY rowid DESC LIMIT ?)",
                (SEEN_KEEP,),
            )
            conn.commit()


class InstagramRelay:
    """Polls configured Instagram group DM threads and republishes shared
    posts into the configured Telegram target chat."""

    def __init__(self, deps):
        self.bot = deps["bot"]
        self.admin_id = deps.get("admin_id") or 0
        self.metrics = deps.get("metrics") or {}
        self.admin_notifier = deps.get("admin_notifier")
        self.mark_bot_media = deps.get("mark_bot_media")
        self.download_post = deps["download_post"]
        self.text_url_extractor = deps.get("text_url_extractor")
        self.upload_max_bytes = deps.get("upload_max_bytes") or 49 * 1024 * 1024
        self.store = RelayStore(deps["db_file"])
        self.username = IG_RELAY_USERNAME
        self.password = IG_RELAY_PASSWORD
        self.session_file = IG_RELAY_SESSION_FILE
        self.poll_seconds = IG_RELAY_POLL_SECONDS
        self.max_attempts = IG_RELAY_MAX_ATTEMPTS
        self.challenge_backoff = IG_RELAY_CHALLENGE_BACKOFF

        self._client = None
        self._client_lock = threading.RLock()
        self._alert_lock = threading.Lock()
        self._alerts = {}
        self._challenge_until = 0.0
        self.metrics.setdefault("ig_relay_relayed", 0)
        self.metrics.setdefault("ig_relay_errors", 0)
        self.state = {
            "logged_in": False,
            "fatal": "",
            "challenge": "",
            "last_error": "",
            "last_poll_at": "",
        }

    # ------------------------------------------------------------------
    # admin notifications
    # ------------------------------------------------------------------
    def alert(self, text, key="generic"):
        now = time.time()
        with self._alert_lock:
            last = self._alerts.get(key, 0)
            if now - last < ALERT_THROTTLE_SECONDS:
                return
            self._alerts[key] = now
        if not self.admin_id:
            return
        try:
            self.bot.send_message(self.admin_id, text)
        except Exception:
            log.warning("ig-relay: failed to alert admin", exc_info=True)

    # ------------------------------------------------------------------
    # login / session
    # ------------------------------------------------------------------
    def _build_client(self):
        from instagrapi import Client

        if self._client is None:
            client = Client()
            if os.path.isfile(self.session_file):
                try:
                    client.load_settings(self.session_file)
                    log.info("ig-relay: loaded saved session from %s", self.session_file)
                except Exception:
                    log.warning("ig-relay: could not load saved session", exc_info=True)
            self._client = client
        return self._client

    def _save_session(self, client):
        try:
            os.makedirs(os.path.dirname(self.session_file), exist_ok=True)
            client.dump_settings(self.session_file)
        except Exception:
            log.warning("ig-relay: could not persist session", exc_info=True)

    def try_login(self, verification_code=""):
        """Attempt (or retry) an Instagram login. Returns True when logged in.
        Failures are classified and surfaced to the admin instead of raising."""
        if not self.username or not self.password:
            self.state["fatal"] = "IG_RELAY_USERNAME/IG_RELAY_PASSWORD تنظیم نشده"
            return False
        with self._client_lock:
            try:
                client = self._build_client()
            except ImportError:
                self.state["fatal"] = "پکیج instagrapi نصب نیست (requirements.txt)"
                self.alert("🔁 ریلی اینستاگرام: پکیج instagrapi نصب نیست.", "import")
                return False
            try:
                client.login(self.username, self.password, verification_code=verification_code or "")
                self._save_session(client)
                previous_error = self.state.get("last_error") or self.state.get("challenge")
                self.state["logged_in"] = True
                self.state["fatal"] = ""
                self.state["challenge"] = ""
                self.state["last_error"] = ""
                log.info("ig-relay: login OK as @%s", self.username)
                if previous_error:
                    self.alert("✅ ریلی اینستاگرام: لاگین دوباره با موفقیت انجام شد (@{}).".format(self.username), "login_ok")
                return True
            except Exception as exc:
                name = type(exc).__name__
                detail = str(exc)
                self.state["logged_in"] = False
                self.state["last_error"] = "{}: {}".format(name, detail[:200])
                if name == "TwoFactorRequired":
                    self.state["challenge"] = "2FA"
                    self.alert(
                        "🔁 ریلی اینستاگرام: احراز هویت دومرحله‌ای فعال است.\n"
                        "کد پیامک/اپ را با دستور زیر وارد کنید:\n"
                        "/relay verify <کد>",
                        "2fa",
                    )
                elif name == "ChallengeRequired":
                    url = self._challenge_url(client)
                    self.state["challenge"] = "checkpoint"
                    self.alert(
                        "🔁 ریلی اینستاگرام: اینستاگرام checkpoint خواست.\n"
                        "یک بار با اپ رسمی وارد اکانت {} شوید و تأیید را کامل کنید، "
                        "بعد ربات را ری‌استارت کنید.{}\nجزئیات: {}".format(
                            self.username,
                            ("\nلینک: " + url) if url else "",
                            detail[:300],
                        ),
                        "challenge",
                    )
                elif name == "BadPassword":
                    self.state["fatal"] = "رمز عبور اشتباه است"
                    self.alert("🔁 ریلی اینستاگرام: IG_RELAY_PASSWORD اشتباه است.", "badpass")
                else:
                    self.alert(
                        "🔁 ریلی اینستاگرام: خطا در لاگین اینستاگرام\n{}".format(self.state["last_error"]),
                        "login_error",
                    )
                log.error("ig-relay: login failed (%s): %s", name, detail)
                if self.admin_notifier:
                    self.admin_notifier.notify("ig_relay_login_error", traceback.format_exc(limit=5))
                return False

    def _challenge_url(self, client):
        try:
            last = getattr(client, "last_json", None) or {}
            challenge = last.get("challenge") or {}
            if isinstance(challenge, dict):
                return str(challenge.get("url") or "")
        except Exception:
            pass
        return ""

    def ensure_logged_in(self):
        if self.state.get("fatal"):
            return False
        if self.state["logged_in"]:
            return True
        return self.try_login()

    # ------------------------------------------------------------------
    # polling
    # ------------------------------------------------------------------
    def ready(self):
        if not self.username or not self.password:
            return False
        if not self.store.is_enabled():
            return False
        return bool(self.store.get_target() and self.store.get_threads())

    def challenge_pending(self):
        return time.time() < self._challenge_until

    def resume(self):
        """Clear a pending checkpoint pause. Called by /relay resume after the
        admin has confirmed the account in the official Instagram app."""
        self._challenge_until = 0.0
        self.state["challenge"] = ""
        self.state["last_error"] = ""

    def run_forever(self):
        log.info(
            "ig-relay: thread started (creds=%s, enabled=%s, target=%r, threads=%s, poll=%ss)",
            "set" if (self.username and self.password) else "MISSING",
            self.store.is_enabled(),
            self.store.get_target() or "-",
            ",".join(self.store.get_threads()) or "-",
            self.poll_seconds,
        )
        idle_logged = False
        while True:
            try:
                if not self.ready():
                    if not idle_logged:
                        log.info(
                            "ig-relay: idle (waiting for creds/enabled/target/threads) — checked every 5s"
                        )
                        idle_logged = True
                    time.sleep(5)
                    continue
                idle_logged = False
                if self.challenge_pending():
                    # Go silent while a checkpoint is unresolved. Re-polling a
                    # challenged account is what escalates it; the only fix is
                    # manual confirmation in the official app (or /relay resume).
                    log.info(
                        "ig-relay: paused for %.0fs after an Instagram checkpoint — "
                        "no requests will be sent",
                        self._challenge_until - time.time(),
                    )
                    time.sleep(min(30, max(1, self._challenge_until - time.time())))
                    continue
                if not self.ensure_logged_in():
                    time.sleep(IG_RELAY_LOGIN_BACKOFF)
                    continue
                try:
                    from services.panel_jobs import (
                        job_mark_start,
                        job_mark_success,
                        job_paused_until,
                    )

                    paused, _label = job_paused_until("relay")
                    if paused:
                        time.sleep(60)
                        continue
                    job_mark_start("relay")
                except Exception:
                    pass
                self.poll_once()
                try:
                    job_mark_success("relay")
                except Exception:
                    pass
                time.sleep(self.poll_seconds)
            except Exception:
                log.error("ig-relay: poll loop crashed; backing off", exc_info=True)
                self.state["last_error"] = "poll loop: " + traceback.format_exc(limit=2).splitlines()[-1][:200]
                self.metrics["ig_relay_errors"] += 1
                try:
                    from services.panel_jobs import job_mark_error

                    job_mark_error("relay", traceback.format_exc(limit=3))
                except Exception:
                    pass
                if self.admin_notifier:
                    self.admin_notifier.notify("ig_relay_loop_error", traceback.format_exc(limit=5))
                time.sleep(IG_RELAY_LOGIN_BACKOFF)

    def fetch_thread(self, thread_id):
        with self._client_lock:
            thread = self._client.direct_thread(int(thread_id), amount=20)
        return thread

    def poll_once(self):
        try:
            from services.panel_settings import feature_enabled

            if not feature_enabled("relay"):
                return
        except Exception:
            pass  # fail open: panel must never break the relay loop
        target = self.store.get_target()
        if not target:
            return
        for thread_id in self.store.get_threads():
            try:
                thread = self.fetch_thread(thread_id)
            except Exception as exc:
                name = type(exc).__name__
                if name in ("LoginRequired", "ClientLoginRequired", "ReloginAttemptExceeded"):
                    self.state["logged_in"] = False
                    self.state["last_error"] = name
                    log.warning("ig-relay: session rejected (%s), will re-login", name)
                    return
                if name == "DirectThreadNotFound":
                    self.state["last_error"] = "thread {} پیدا نشد".format(thread_id)
                    self.alert(
                        "🔁 ریلی اینستاگرام: thread با شناسهٔ {} پیدا نشد. با /relay threads لیست را ببینید.".format(
                            thread_id
                        ),
                        "thread_missing",
                    )
                    continue
                if name in ("ChallengeRequired", "ChallengeError", "ChallengeRedirection"):
                    # Account-level, not thread-level: every remaining thread in
                    # this cycle will 400 too, and the session is still valid —
                    # this is a checkpoint on our polling behaviour, not a login
                    # problem. Bail out of the whole cycle and go quiet.
                    self.state["challenge"] = "checkpoint"
                    self.state["last_error"] = "Instagram checkpoint: {}".format(str(exc)[:150])
                    self._challenge_until = time.time() + self.challenge_backoff
                    self.metrics["ig_relay_errors"] += 1
                    log.error(
                        "ig-relay: Instagram checkpoint — pausing all polling for %ss",
                        self.challenge_backoff,
                    )
                    self.alert(
                        "🔁 ریلی اینستاگرام: اینستاگرام به اکانت checkpoint داد و "
                        "نظرسنجی تا {} ثانیه متوقف شد.\n"
                        "این خطا با /relay verify حل نمی‌شود. یک بار با اپ رسمی اینستاگرام "
                        "وارد اکانت {} شوید و تأیید را کامل کنید، بعد /relay resume بزنید."
                        .format(int(self.challenge_backoff), self.username),
                        "challenge_poll",
                    )
                    if self.admin_notifier:
                        self.admin_notifier.notify("ig_relay_challenge", str(exc)[:400])
                    return
                self.metrics["ig_relay_errors"] += 1
                self.state["last_error"] = "{}: {}".format(name, str(exc)[:150])
                log.error("ig-relay: fetch thread %s failed", thread_id, exc_info=True)
                if self.admin_notifier:
                    self.admin_notifier.notify("ig_relay_fetch_error", traceback.format_exc(limit=5))
                continue
            if self.state.get("challenge") == "checkpoint":
                # A fetch came back clean, so the checkpoint cleared on its own
                # (usually the admin finished it in the app). Don't require a
                # manual /relay resume just to drop the pause.
                self.state["challenge"] = ""
                self._challenge_until = 0.0
                self.alert("✅ ریلی اینستاگرام: checkpoint برطرف شد و ریلی ادامه یافت.", "challenge_cleared")
            try:
                self.process_thread(thread, target)
            except Exception:
                log.error("ig-relay: process thread %s failed", thread_id, exc_info=True)
                if self.admin_notifier:
                    self.admin_notifier.notify("ig_relay_process_error", traceback.format_exc(limit=5))
        self.store.prune()
        self.state["last_poll_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    def process_thread(self, thread, target):
        thread_id = str(_get(thread, "id") or _get(thread, "pk") or "")
        if not thread_id:
            return
        messages = list(_get(thread, "messages") or [])
        messages.sort(key=lambda m: (_get(m, "timestamp"), str(_get(m, "id") or "")))
        if not self.store.has_thread_seen(thread_id):
            self.store.seed_thread(thread_id, [str(_get(m, "id")) for m in messages])
            log.info(
                "ig-relay: baseline seeded for thread %s (%d existing messages skipped)",
                thread_id,
                len(messages),
            )
            return
        for message in messages:
            try:
                self.relay_message(message, thread, target)
            except Exception:
                log.error("ig-relay: relay of message %s failed", _get(message, "id"), exc_info=True)
                if self.admin_notifier:
                    self.admin_notifier.notify("ig_relay_message_error", traceback.format_exc(limit=5))

    def relay_message(self, message, thread, target):
        message_id = str(_get(message, "id") or "")
        if not message_id:
            return
        row = self.store.get_seen(message_id)
        if row and row[1]:
            return
        attempts = row[0] if row else 0
        if attempts >= self.max_attempts:
            return
        urls = extract_post_urls(message, self.text_url_extractor)
        if not urls:
            self.store.mark_done(message_id, str(_get(thread, "id") or ""))
            return
        full_name, username = sender_names(thread, _get(message, "user_id"))
        attribution = format_attribution(full_name, username)
        self.store.record_attempt(message_id, str(_get(thread, "id") or ""))
        log.info("ig-relay: relaying %s (from %s)", urls[0], attribution)
        result = None
        try:
            sent_ids = [self.bot.send_message(target, attribution).message_id]
            result = self.download_post(urls[0])
            if not result:
                raise RuntimeError("دانلود پست ناموفق بود: " + urls[0])
            sent_ids.extend(self.send_media(target, result))
            if self.mark_bot_media:
                for sent_id in sent_ids:
                    try:
                        self.mark_bot_media(target, sent_id, kind="instagram")
                    except Exception:
                        log.warning("ig-relay: mark_bot_media failed", exc_info=True)
            self.store.mark_done(message_id, str(_get(thread, "id") or ""))
            self.metrics["ig_relay_relayed"] += 1
        except Exception:
            self.metrics["ig_relay_errors"] += 1
            self.state["last_error"] = traceback.format_exc(limit=1).splitlines()[-1][:200]
            log.error("ig-relay: failed to relay %s", urls[0], exc_info=True)
            if self.admin_notifier:
                self.admin_notifier.notify("ig_relay_post_error", traceback.format_exc(limit=5))
            try:
                self.bot.send_message(target, "❌ این پست دانلود یا ارسال نشد.")
            except Exception:
                pass
        finally:
            if result:
                shutil.rmtree(result.get("outdir", ""), ignore_errors=True)

    def send_media(self, target, result):
        """Upload downloaded files to the target chat; returns sent message ids.
        Mirrors bot.py's Instagram sender: single file -> photo/video with the
        original caption, multiple files -> media group (caption on the first),
        oversize files skipped with a warning."""
        caption = ((result.get("caption") or "").strip())[:CAPTION_MAX] or None
        files = [
            f for f in result.get("files", [])
            if os.path.getsize(f["path"]) <= self.upload_max_bytes
        ]
        oversized = len(result.get("files", [])) - len(files)
        sent_ids = []
        handles = []
        try:
            from telebot import types as tb_types

            if len(files) == 1:
                single = files[0]
                with open(single["path"], "rb") as fh:
                    if single["is_video"]:
                        sent = self.bot.send_video(target, fh, caption=caption, supports_streaming=True)
                    else:
                        sent = self.bot.send_photo(target, fh, caption=caption)
                sent_ids.append(sent.message_id)
            elif len(files) > 1:
                media = []
                for index, f in enumerate(files[:MEDIA_GROUP_CAP]):
                    fh = open(f["path"], "rb")
                    handles.append(fh)
                    kwargs = {"caption": caption} if index == 0 and caption else {}
                    if f["is_video"]:
                        media.append(tb_types.InputMediaVideo(fh, **kwargs))
                    else:
                        media.append(tb_types.InputMediaPhoto(fh, **kwargs))
                if media:
                    for sent in self.bot.send_media_group(target, media):
                        sent_ids.append(sent.message_id)
            else:
                raise RuntimeError("حجم فایل بیشتر از سقف مجاز تلگرام است")
        finally:
            for fh in handles:
                try:
                    fh.close()
                except Exception:
                    pass
        if oversized:
            try:
                self.bot.send_message(
                    target, "⚠️ {} مورد از این پست به‌خاطر حجم بالا ارسال نشد.".format(oversized)
                )
            except Exception:
                pass
        return sent_ids

    # ------------------------------------------------------------------
    # /relay command surface
    # ------------------------------------------------------------------
    def handle_command(self, message):
        parts = (message.text or "").strip().split()
        sub = parts[1].lower() if len(parts) > 1 else ""
        args = parts[2:]
        if sub not in ("", "help", "status"):
            try:
                from services.panel_settings import feature_enabled

                if not feature_enabled("relay"):
                    self.reply(message, "⏸ Relay is temporarily disabled by the admins.")
                    return
            except Exception:
                pass
        if not sub or sub == "help":
            self.reply(message, self.help_text())
        elif sub == "status":
            self.reply(message, self.status_text())
        elif sub == "on":
            self.store.set_config("enabled", "1")
            self.reply(message, "✅ ریلی اینستاگرام فعال شد.")
        elif sub == "off":
            self.store.set_config("enabled", "0")
            self.reply(message, "⛔ ریلی اینستاگرام غیرفعال شد.")
        elif sub == "target":
            self.cmd_target(message, args)
        elif sub == "threads":
            self.cmd_list_threads(message)
        elif sub == "thread":
            self.cmd_thread(message, args)
        elif sub == "test":
            self.cmd_test(message, args)
        elif sub == "verify":
            self.cmd_verify(message, args)
        elif sub == "resume":
            self.cmd_resume(message)
        else:
            self.reply(message, "❓ زیردستور نامعتبر است.\n\n" + self.help_text())

    def reply(self, message, text):
        try:
            self.bot.reply_to(message, text)
        except Exception:
            log.warning("ig-relay: reply failed", exc_info=True)

    def help_text(self):
        return (
            "🔁 ریلی اینستاگرام → تلگرام\n\n"
            "/relay status — وضعیت فعلی\n"
            "/relay on — فعال‌سازی ریلی\n"
            "/relay off — توقف ریلی\n"
            "/relay target — با ریپلای روی پیام گروه مقصد (یا /relay target <chat_id>)\n"
            "/relay threads — لیست گروه‌های چت اکانت اینستاگرام\n"
            "/relay thread <id> — افزودن thread به لیست مانیتور\n"
            "/relay thread remove <id> — حذف thread\n"
            "/relay test <لینک پست> — ارسال آزمایشی به مقصد\n"
            "/relay verify <کد> — کد تأیید دومرحله‌ای\n"
            "/relay resume — از سرگیری پس از تأیید checkpoint در اپ اینستاگرام"
        )

    def status_text(self):
        creds = "✅ set (@{})".format(self.username) if self.username and self.password else "❌ تنظیم نشده"
        enabled = "✅ فعال" if self.store.is_enabled() else "⛔ غیرفعال"
        target = self.store.get_target() or "❌ تعیین نشده"
        threads = self.store.get_threads()
        threads_text = ", ".join(threads) if threads else "❌ تعیین نشده"
        if not self.username or not self.password:
            login = "—"
        elif self.challenge_pending():
            # Checked before logged_in on purpose: the session is still valid, so
            # showing "logged in" would hide the only thing that needs action.
            login = "⏸️ checkpoint اینستاگرام — تعلیق تا {} ثانیه دیگر (پس از تأیید در اپ: /relay resume)".format(
                int(self._challenge_until - time.time())
            )
        elif self.state["logged_in"]:
            login = "✅ موفق"
        elif self.state["challenge"] == "2fa":
            login = "⏳ در انتظار /relay verify <کد>"
        elif self.state["challenge"] == "checkpoint":
            login = "⏳ در انتظار تأیید checkpoint در اپ اینستاگرام"
        elif self.state.get("fatal"):
            login = "❌ " + self.state["fatal"]
        else:
            login = "❌ " + (self.state["last_error"] or "ناموفق")
        ready = "✅ آماده" if self.ready() else "⏳ در انتظار تنظیمات (enabled + مقصد + thread)"
        if not (self.username and self.password):
            ready = "⏳ در انتظار تنظیم IG_RELAY_USERNAME / IG_RELAY_PASSWORD"
        elif self.challenge_pending():
            ready = "⏸️ تعلیق به‌خاطر checkpoint اینستاگرام"
        return (
            "🔁 وضعیت ریلی اینستاگرام\n\n"
            "اکانت اینستاگرام: {}\n"
            "لاگین: {}\n"
            "کلید کلی: {}\n"
            "گروه مقصد تلگرام: {}\n"
            "thread های مانیتور: {}\n"
            "وضعیت: {}\n"
            "آخرین نظرسنجی: {}\n"
            "آخرین خطا: {}\n"
            "پست‌های ارسال‌شده: {} | خطاها: {}\n"
            "بازهٔ نظرسنجی: {} ثانیه"
        ).format(
            creds,
            login,
            enabled,
            target,
            threads_text,
            ready,
            self.state["last_poll_at"] or "—",
            self.state["last_error"] or "—",
            self.metrics.get("ig_relay_relayed", 0),
            self.metrics.get("ig_relay_errors", 0),
            self.poll_seconds,
        )

    def cmd_target(self, message, args):
        if args:
            try:
                chat_id = int(args[0])
            except ValueError:
                self.reply(message, "❓ شناسهٔ گروه باید عدد باشد. مثال: /relay target -1001234567890")
                return
        elif message.reply_to_message:
            chat_id = message.reply_to_message.chat.id
        else:
            self.reply(message, "❓ روی پیامی در گروه مقصد ریپلای کنید یا شناسه را بدهید:\n/relay target <chat_id>")
            return
        self.store.set_config("target_chat", str(chat_id))
        self.reply(message, "✅ گروه مقصد روی `{}` تنظیم شد.".format(chat_id))

    def cmd_list_threads(self, message):
        if not self.ensure_logged_in():
            self.reply(
                message,
                "❌ هنوز به اینستاگرام لاگین نشده.\nوضعیت: /relay status",
            )
            return
        try:
            with self._client_lock:
                threads = self._client.direct_threads(amount=20)
        except Exception:
            log.error("ig-relay: direct_threads failed", exc_info=True)
            self.reply(message, "❌ خطا در دریافت لیست گروه‌ها. دوباره تلاش کنید.")
            return
        if not threads:
            self.reply(message, "هیچ چتی برای این اکانت پیدا نشد.")
            return
        lines = ["📋 گروه‌های چت اینستاگرام:"]
        configured = set(self.store.get_threads())
        for thread in threads[:20]:
            title = (_get(thread, "thread_title") or "").strip() or "(بدون عنوان)"
            users = _get(thread, "users") or []
            names = ", ".join(
                (_get(u, "full_name") or _get(u, "username") or "?") for u in users[:4]
            )
            marker = "✅" if str(_get(thread, "id")) in configured else "•"
            kind = "گروه" if _get(thread, "is_group") else "خصوصی"
            lines.append(
                "{} {} | id={}\n   {} | {}".format(marker, title, _get(thread, "id"), kind, names)
            )
        lines.append("\nافزودن: /relay thread <id>")
        self.reply(message, "\n".join(lines))

    def cmd_thread(self, message, args):
        threads = self.store.get_threads()
        if args and args[0].lower() == "remove":
            if len(args) < 2:
                self.reply(message, "❓ شناسهٔ thread را بدهید:\n/relay thread remove <id>")
                return
            victim = args[1]
            if victim not in threads:
                self.reply(message, "❓ thread با شناسهٔ `{}` در لیست نیست.".format(victim))
                return
            self.store.set_threads([t for t in threads if t != victim])
            self.reply(message, "✅ thread `{}` از لیست حذف شد.".format(victim))
            return
        if not args:
            self.reply(message, "❓ شناسهٔ thread را بدهید:\n/relay thread <id>\nلیست: /relay threads")
            return
        thread_id = args[0]
        if not thread_id.isdigit():
            self.reply(message, "❓ شناسهٔ thread باید عدد باشد.")
            return
        if thread_id in threads:
            self.reply(message, "ℹ️ این thread از قبل در لیست است.")
            return
        threads.append(thread_id)
        self.store.set_threads(threads)
        self.reply(
            message,
            "✅ thread `{}` اضافه شد. نظرسنجی بعدی از پیام‌های قبلی عبور می‌کند "
            "(فقط پست‌های تازه ریلی می‌شوند).".format(thread_id),
        )

    def cmd_test(self, message, args):
        target = self.store.get_target()
        if not target:
            self.reply(message, "❌ اول گروه مقصد را مشخص کنید:\n/relay target")
            return
        if not args:
            self.reply(message, "❓ یک لینک پست بدهید:\n/relay test <لینک>")
            return
        url = args[0]
        sent_ids = []
        result = None
        try:
            sent_ids.append(
                self.bot.send_message(target, format_attribution("تست ریلی", "")).message_id
            )
            result = self.download_post(url)
            if not result:
                raise RuntimeError("دانلود ناموفق")
            sent_ids.extend(self.send_media(target, result))
            if self.mark_bot_media:
                for sent_id in sent_ids:
                    self.mark_bot_media(target, sent_id, kind="instagram")
            self.reply(message, "✅ پیام آزمایشی با موفقیت به `{}` ارسال شد.".format(target))
        except Exception:
            log.error("ig-relay: test send failed", exc_info=True)
            self.reply(message, "❌ ارسال آزمایشی ناموفق بود. جزئیات در لاگ ربات.")
        finally:
            if result:
                shutil.rmtree(result.get("outdir", ""), ignore_errors=True)

    def cmd_verify(self, message, args):
        if not args:
            self.reply(message, "❓ کد تأیید را بدهید:\n/relay verify <کد>")
            return
        if self.try_login(verification_code=args[0]):
            self.reply(message, "✅ لاگین با موفقیت انجام شد.")
        else:
            self.reply(message, "❌ لاگین ناموفق بود. وضعیت: /relay status")

    def cmd_resume(self, message):
        if not self.challenge_pending() and self.state.get("challenge") != "checkpoint":
            self.reply(message, "ℹ️ ریلی در حالت عادی است و نیازی به resume ندارد.")
            return
        self.resume()
        self.reply(
            message,
            "✅ ریلی اینستاگرام از سرگیری شد.\n"
            "اگر هنوز checkpoint در اپ تأیید نشده، نظرسنجی بعدی دوباره خطا می‌دهد "
            "و این بار خودکار متوقف می‌شود.",
        )


_relay = None
_relay_lock = threading.Lock()
_registered = False
_ctx = {"bot": None, "admin_id": 0, "get_deps": None}


def _ensure_relay():
    global _relay
    if _ctx["get_deps"] is None:
        raise RuntimeError("ig_relay.register() must be called before use")
    with _relay_lock:
        if _relay is None:
            deps = _ctx["get_deps"]()
            deps["bot"] = _ctx["bot"]
            deps["admin_id"] = _ctx["admin_id"]
            _relay = InstagramRelay(deps)
        return _relay


def register(bot, admin_id, get_deps):
    """Register the /relay admin command. Must be called from bot.py at module
    level BEFORE the catch-all handle_all_messages handler — telebot stops at
    the first handler whose filters match, and the catch-all claims every
    text message."""
    global _registered
    _ctx.update(bot=bot, admin_id=admin_id, get_deps=get_deps)
    if _registered:
        return
    _registered = True

    @bot.message_handler(commands=["relay"])
    def _handle_relay_command(message):
        try:
            if message.from_user.id != admin_id:
                try:
                    bot.reply_to(message, "⛔ You are not authorized to use this command.")
                except Exception:
                    pass
                return
            _ensure_relay().handle_command(message)
        except Exception:
            log.error("ig-relay: /relay command failed", exc_info=True)
            try:
                bot.reply_to(message, "❌ خطایی در دستور /relay رخ داد.")
            except Exception:
                pass


def start():
    """Launch the polling daemon thread (no-op when credentials are missing).
    Call this once from bot.py's __main__ block, after register()."""
    relay = _ensure_relay()
    if not relay.username or not relay.password:
        log.info("ig-relay: IG_RELAY_USERNAME/IG_RELAY_PASSWORD not set — feature disabled.")
        return None
    thread = threading.Thread(target=relay.run_forever, daemon=True, name="ig-relay-polling")
    thread.start()
    log.info("ig-relay: polling daemon thread launched.")
    return relay
