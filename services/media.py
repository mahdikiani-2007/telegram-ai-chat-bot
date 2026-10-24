import asyncio
import html
import http.cookiejar
import logging
import os
import re
import shutil
import threading
import time
import uuid

import yt_dlp

from config import (
    INSTAGRAM_COOKIES_FILE,
    INSTAGRAM_DL_DIR,
    SESSION_TTL_SECONDS,
    TELEGRAM_UPLOAD_MAX_BYTES,
    TG_API_HASH,
    TG_API_ID,
    TG_SESSION_DIR,
    TG_SESSION_NAME,
    YOUTUBE_COOKIES_FILE,
    YOUTUBE_DL_DIR,
    YOUTUBE_MAX_HEIGHT,
)
from infra import HTTP
from sessions import TTLSessionStore, _register_late_session_store

log = logging.getLogger("L-bot")


# =========================
# Instagram Downloader (yt-dlp based) — auto-detects post/reel/IGTV/carousel,
# story/highlight, AND share-shortlink (/s/...) links pasted anywhere in a
# message, and re-uploads the media + caption directly into the chat.
# Stories/highlights always require a logged-in session (Instagram platform
# rule, not a limitation of this code) — see INSTAGRAM_COOKIES_FILE.
# =========================
INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:"
    r"(?:[^/?#\s]+/)?(?:p|tv|reels?)/[^/?#\s]+"   # posts / reels / IGTV, optional /username/ prefix
    r"|stories/[^/?#\s]+(?:/\d+)?"                # stories AND highlights (/stories/highlights/<id>/)
    r"|s/[^/?#\s]+"                                # share shortlinks (e.g. "Copy Link" on a highlight)
    r")\S*",
    re.IGNORECASE,
)

def extract_instagram_urls(text):
    if not text:
        return []
    return INSTAGRAM_URL_RE.findall(text)

def _pick_media_url(entry):
    """Best-effort extraction of a direct, fetchable media URL from a
    yt-dlp info entry — handling video entries (formats list) and photo
    entries (flat url/thumbnail) the same way, by resolving to a URL and
    fetching it ourselves with `requests` instead of letting yt-dlp's own
    downloader touch it. Returns (url, is_video) or (None, False).

    Format selection detail (this is what was causing silent video sends):
    verified in yt-dlp's InstagramIE._extract_product_media source — every
    Instagram video's `formats` list starts with its `video_versions`
    entries, which are already pre-muxed (single file, video+audio
    together). But if the post also has a `video_dash_manifest`, yt-dlp
    EXTENDS that same list with DASH-parsed formats, which — like YouTube —
    are split into separate video-only and audio-only streams meant to be
    merged with ffmpeg. Blindly taking formats[-1] ("last = best") can
    silently land on one of those video-only DASH streams once a post has
    a DASH manifest, which plays back with no sound. We don't want to
    depend on ffmpeg being available on the host, so we explicitly prefer a
    pre-muxed, non-DASH format (identifiable by yt-dlp tagging DASH-parsed
    formats' format_id with the "dash" mpd_id) and only fall back to DASH
    formats if literally nothing else is available."""
    formats = entry.get("formats") or []
    if formats:
        def has_declared_audio(f):
            # acodec is None ("unspecified") for Instagram's pre-muxed
            # video_versions formats — that does NOT mean no audio, it
            # means yt-dlp didn't get an explicit per-format signal (the
            # post-level has_audio flag only gets attached when it's
            # explicitly False). Only the literal string "none" (set by
            # yt-dlp's DASH/MPD parser for a genuine video-only stream)
            # means "confirmed no audio".
            return f.get("acodec") != "none"

        def is_dash(f):
            return str(f.get("format_id") or "").lower().startswith("dash")

        premuxed_with_audio = [f for f in formats if not is_dash(f) and has_declared_audio(f)]
        candidates = premuxed_with_audio or [f for f in formats if has_declared_audio(f)] or formats

        best = max(candidates, key=lambda f: ((f.get("height") or 0), (f.get("width") or 0), (f.get("tbr") or 0)))
        if best.get("url"):
            return best["url"], True
    if entry.get("url"):
        is_video = (entry.get("vcodec") or "none") not in (None, "none") or bool(formats)
        return entry["url"], is_video
    if entry.get("thumbnail"):
        return entry["thumbnail"], False
    return None, False

_INSTAGRAM_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://www.instagram.com/",
    "Accept-Language": "en-US,en;q=0.9",
}

_OG_VIDEO_RE = re.compile(r'property=["\']og:video(?::secure_url)?["\']\s+content=["\']([^"\']+)["\']', re.IGNORECASE)

_OG_IMAGE_RE = re.compile(r'property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', re.IGNORECASE)

_OG_DESC_RE = re.compile(r'property=["\']og:description["\']\s+content=["\']([^"\']+)["\']', re.IGNORECASE)

_instagram_cookiejar_cache = {"loaded": False, "jar": None}

def _get_instagram_cookiejar():
    """Lazily load INSTAGRAM_COOKIES_FILE (Netscape format) into a stdlib
    CookieJar, cached after the first successful/failed attempt. Used for
    the raw OG-tag fallback fetch below — previously that fetch went out
    completely anonymous even when a logged-in cookies file was configured
    for yt-dlp, which is a likely reason it was finding nothing (Instagram
    login-walls most anonymous page loads)."""
    if not _instagram_cookiejar_cache["loaded"]:
        _instagram_cookiejar_cache["loaded"] = True
        if INSTAGRAM_COOKIES_FILE and os.path.isfile(INSTAGRAM_COOKIES_FILE):
            try:
                jar = http.cookiejar.MozillaCookieJar(INSTAGRAM_COOKIES_FILE)
                jar.load(ignore_discard=True, ignore_expires=True)
                _instagram_cookiejar_cache["jar"] = jar
                log.info("OG-tag fallback: loaded %d Instagram cookies from %s", len(list(jar)), INSTAGRAM_COOKIES_FILE)
            except Exception:
                log.warning("OG-tag fallback: failed to load Instagram cookies file", exc_info=True)
    return _instagram_cookiejar_cache["jar"]

def _fetch_instagram_via_og_tags(url):
    """Last-resort fallback for whenever yt-dlp's structured extraction
    fails or doesn't apply.

    HONESTY NOTE: this isn't covering a bug in our code — it's yt-dlp's
    Instagram extractor being video-first BY DESIGN. Verified directly in
    its source (including the latest nightly build, checked the same day
    this was written): a single (non-carousel) photo post always has an
    empty `formats` list — video and photo data live in completely
    different fields internally (`video_versions` vs `image_versions2`) —
    and InstagramIE._real_extract deliberately raises "There is no video in
    this post" whenever a non-playlist entry has no formats. Two upstream
    issues about exactly this (yt-dlp#7569, yt-dlp#12439) were closed by
    the maintainers as intended behavior, not bugs. So this will not get
    fixed by updating yt-dlp — a different approach is needed for photos.

    This fetches the public post page ourselves (WITH the same logged-in
    cookies used for yt-dlp, if configured) and reads the Open Graph
    video/image + description meta tags, which Instagram server-renders
    for link-preview crawlers (Facebook/Twitter cards) independent of
    yt-dlp entirely. Also the best bet for share-shortlinks
    (instagram.com/s/...) since no yt-dlp extractor claims that URL shape
    at all. Public content only — Instagram won't render these tags on a
    login-walled page.
    """
    try:
        r = HTTP.get(
            url, headers=_INSTAGRAM_FETCH_HEADERS, cookies=_get_instagram_cookiejar(),
            timeout=20, allow_redirects=True,
        )
        if r.status_code != 200:
            log.info("OG-tag fallback: non-200 (%d) for %s (final url after redirects: %s)", r.status_code, url, r.url)
            return None

        page = r.text
        desc_match = _OG_DESC_RE.search(page)
        caption = html.unescape(desc_match.group(1)) if desc_match else ""

        video_match = _OG_VIDEO_RE.search(page)
        if video_match:
            return {"media_url": html.unescape(video_match.group(1)), "is_video": True, "caption": caption}

        image_match = _OG_IMAGE_RE.search(page)
        if image_match:
            return {"media_url": html.unescape(image_match.group(1)), "is_video": False, "caption": caption}

        title_match = re.search(r"<title[^>]*>([^<]*)</title>", page, re.IGNORECASE)
        log.info(
            "OG-tag fallback: page loaded (200, final url=%s, %d bytes) but no og:video/og:image found — "
            "page <title>: %r. Likely a login-wall or bot-challenge page rather than the real post.",
            r.url, len(page), (title_match.group(1).strip() if title_match else None),
        )
        return None
    except Exception:
        log.warning("OG-tag fallback failed for %s", url, exc_info=True)
        return None

def _save_remote_media(media_url, is_video, dest_path):
    """Fetch `media_url` and save it to `dest_path`. Returns True/False."""
    try:
        r = HTTP.get(media_url, headers=_INSTAGRAM_FETCH_HEADERS, timeout=30, stream=True)
        if r.status_code != 200:
            return False
        with open(dest_path, "wb") as fh:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if chunk:
                    fh.write(chunk)
        return True
    except Exception:
        log.warning("Instagram media fetch failed for %s", media_url, exc_info=True)
        return False

def download_instagram_post(url):
    """Download an Instagram post/reel/IGTV/carousel/story/highlight/share-
    link. Returns {"files": [{"path", "is_video"}, ...], "caption": str,
    "outdir": str} on success, or None on any failure (private/deleted
    content, no login for a story, Instagram-side throttling, network
    error, etc.) — every downloaded file lives under its own request-scoped
    temp dir so cleanup is a single rmtree.

    Strategy: try yt-dlp's structured extractor first (best for videos,
    reels, and carousels — gets proper per-item resolution). If that fails
    for ANY reason — including the by-design photo-post gap described in
    _fetch_instagram_via_og_tags — fall back to scraping Open Graph tags
    directly, which also covers share-shortlinks yt-dlp doesn't recognize
    at all."""
    request_dir = os.path.join(INSTAGRAM_DL_DIR, uuid.uuid4().hex[:12])
    os.makedirs(request_dir, exist_ok=True)

    files = []
    caption = ""

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,  # carousels/highlights come back as multi-entry "playlists"
        "socket_timeout": 30,
        "retries": 2,
    }
    if INSTAGRAM_COOKIES_FILE and os.path.isfile(INSTAGRAM_COOKIES_FILE):
        ydl_opts["cookiefile"] = INSTAGRAM_COOKIES_FILE

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # download=False: extraction/resolution only — we always fetch
            # the actual bytes ourselves (see _pick_media_url / _save_remote_media).
            info = ydl.extract_info(url, download=False)
        if info is None:
            raise ValueError("yt-dlp returned no info")

        entries = info.get("entries") if info.get("_type") == "playlist" else [info]
        caption = (info.get("description") or info.get("title") or "").strip()

        for idx, entry in enumerate(entries):
            if not entry:
                continue
            media_url, is_video = _pick_media_url(entry)
            if not media_url:
                continue
            dest = os.path.join(request_dir, f"{idx}.{'mp4' if is_video else 'jpg'}")
            if _save_remote_media(media_url, is_video, dest):
                files.append({"path": dest, "is_video": is_video})
    except Exception as e:
        log.info("yt-dlp extraction failed for %s (%s) — trying OG-tag fallback", url, str(e)[:200])

    if not files:
        fallback = _fetch_instagram_via_og_tags(url)
        if fallback and fallback.get("media_url"):
            dest = os.path.join(request_dir, f"0.{'mp4' if fallback['is_video'] else 'jpg'}")
            if _save_remote_media(fallback["media_url"], fallback["is_video"], dest):
                files.append({"path": dest, "is_video": fallback["is_video"]})
                if not caption:
                    caption = fallback.get("caption", "")

    if not files:
        shutil.rmtree(request_dir, ignore_errors=True)
        return None

    return {"files": files, "caption": caption, "outdir": request_dir}

def _is_instagram_link_message(message):
    return bool(message.text) and bool(INSTAGRAM_URL_RE.search(message.text))

# =========================
# Large-file delivery via a dedicated userbot (Pyrogram/MTProto)
# =========================
# Bypasses the Bot API's 50MB upload cap by sending through a REAL Telegram
# account instead of the bot token — up to 2GB (4GB with Premium). This is
# deliberately NOT wired through the main bot's file_id: file_ids are only
# valid for the bot that issued them, so a file uploaded by the userbot
# account cannot be handed to `bot.send_video(file_id=...)` — it has to
# send the file into the chat directly. That means the userbot account
# must actually be a MEMBER of any group where this is needed.
#
# Bridges sync (telebot/threading) and async (Pyrogram) code: a dedicated
# thread owns its own asyncio event loop and the Pyrogram client for the
# whole process lifetime; sync code calls into it via
# asyncio.run_coroutine_threadsafe(...).result(timeout=...), which safely
# blocks the calling (telebot worker) thread until the async send finishes.
_userbot_loop = None

_userbot_client = None

_userbot_ready = threading.Event()

def _userbot_thread_main():
    """Runs for the whole process lifetime in its own daemon thread. Does
    nothing (feature stays disabled, rest of the bot unaffected) unless
    TG_API_ID/TG_API_HASH are set AND a valid .session file already exists
    — first-time interactive phone/SMS-code login can't happen on a
    non-interactive server process, so that must be done locally first."""
    global _userbot_loop, _userbot_client

    if not (TG_API_ID and TG_API_HASH):
        log.info("Userbot large-file delivery disabled (TG_API_ID/TG_API_HASH not set).")
        return

    session_path = os.path.join(TG_SESSION_DIR, f"{TG_SESSION_NAME}.session")
    if not os.path.isfile(session_path):
        log.warning(
            "Userbot large-file delivery disabled: no session file at %s. "
            "Authenticate locally first and upload the .session file (see setup notes).",
            session_path,
        )
        return

    try:
        from pyrogram import Client
    except ImportError:
        log.warning("Userbot large-file delivery disabled: pyrogram not installed.")
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = Client(TG_SESSION_NAME, api_id=TG_API_ID, api_hash=TG_API_HASH, workdir=TG_SESSION_DIR)

    async def _startup():
        await client.start()
        me = await client.get_me()
        log.info("Userbot connected as @%s (id=%s) — large-file delivery enabled.", me.username, me.id)

    try:
        loop.run_until_complete(_startup())
    except Exception:
        log.error("Userbot failed to start — large-file delivery disabled.", exc_info=True)
        return

    _userbot_loop = loop
    _userbot_client = client
    _userbot_ready.set()
    loop.run_forever()

def userbot_available():
    return _userbot_ready.is_set() and _userbot_loop is not None and _userbot_client is not None

def send_large_file_via_userbot(chat_id, file_path, caption=None, is_video=True, reply_to_message_id=None, timeout=180):
    """Send a local file into `chat_id` directly as the userbot account —
    NOT via the main bot. Returns the sent message's id on success, or None
    on any failure (userbot not configured/started, not a member of that
    chat, network error, timeout, etc.) — this never raises, so callers can
    treat None as "delivery unavailable" and fall back to the normal
    size-limit message."""
    if not userbot_available():
        return None
    try:
        async def _send():
            kwargs = {"caption": caption}
            if reply_to_message_id:
                kwargs["reply_to_message_id"] = reply_to_message_id
            if is_video:
                return await _userbot_client.send_video(chat_id, file_path, supports_streaming=True, **kwargs)
            return await _userbot_client.send_audio(chat_id, file_path, **kwargs)

        future = asyncio.run_coroutine_threadsafe(_send(), _userbot_loop)
        sent = future.result(timeout=timeout)
        return sent.id if sent else None
    except Exception:
        log.warning("Userbot large-file send failed for chat %s", chat_id, exc_info=True)
        return None

# =========================
# YouTube Downloader (yt-dlp based) — auto-detects links, lets the person
# pick a quality via inline buttons, then downloads+sends that exact format.
# =========================
# No ffmpeg on this host (confirmed — no sudo access to install it), so
# only PROGRESSIVE formats are offered: video+audio already muxed into one
# file by YouTube itself, no merge step needed. This caps out at whatever
# progressive quality YouTube still serves for a given video — historically
# up to 720p for many videos, but YouTube has been reducing progressive
# availability over time, so some videos may only offer 360p. We show
# exactly what's actually available, never a fake/upscaled option.
#
# The "audio only" option sends the raw best audio stream as-is (typically
# .m4a or .webm/opus) — NOT converted to literal .mp3, since that
# conversion needs ffmpeg too. It's a normal playable audio file either
# way; Telegram's audio player handles both fine.
YOUTUBE_URL_RE = re.compile(
    r"https?://(?:(?:www|m|music)\.)?youtube\.com/(?:watch\?v=|shorts/|embed/|live/|clip/)[\w-]+\S*"
    r"|https?://(?:www\.)?youtube-nocookie\.com/embed/[\w-]+\S*"
    r"|https?://youtu\.be/[\w-]+\S*",
    re.IGNORECASE,
)

_youtube_quality_sessions = TTLSessionStore(SESSION_TTL_SECONDS)  # token -> {url, title, options, user_id}


def clear_user_yt_sessions(user_id):
    """Drop YT picker sessions belonging to this user (Stage 2C reset).
    Never raises."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return
    _youtube_quality_sessions.discard_where(
        value_pred=lambda v: isinstance(v, dict) and v.get("user_id") == uid)

_ffmpeg_path_cache = {"checked": False, "path": None}

def _ffmpeg_location():
    """Locate an ffmpeg binary. Prefers a real system-installed ffmpeg on
    PATH (e.g. apt-installed in the Railway/Docker image, where root access
    makes that possible) — that's a properly linked, battle-tested build.
    Falls back to the portable binary from `pip install imageio-ffmpeg`
    (no root needed) for hosts without one, e.g. the old no-root Alwaysdata
    setup. That portable binary is a reasonable fallback but has shown
    real "Postprocessing: Conversion failed!" errors merging YouTube's
    separate video/audio streams in at least one container environment, so
    it's deliberately not the first choice anymore. Cached after first
    check since it does a bit of I/O. Returns a path, or None if neither is
    available — callers must fall back to progressive-only formats in that
    case, never assume it exists."""
    if not _ffmpeg_path_cache["checked"]:
        _ffmpeg_path_cache["checked"] = True

        system_ffmpeg = shutil.which("ffmpeg")
        if system_ffmpeg:
            _ffmpeg_path_cache["path"] = system_ffmpeg
            log.info("YouTube downloader: using system ffmpeg (%s) — HD/merged qualities enabled.", system_ffmpeg)
        else:
            try:
                import imageio_ffmpeg
                _ffmpeg_path_cache["path"] = imageio_ffmpeg.get_ffmpeg_exe()
                log.info(
                    "YouTube downloader: no system ffmpeg found — using portable imageio-ffmpeg (%s) — "
                    "HD/merged qualities enabled, but see chat notes on this fallback's known issues.",
                    _ffmpeg_path_cache["path"],
                )
            except Exception:
                log.info("YouTube downloader: no ffmpeg available at all — progressive-only qualities.")
    return _ffmpeg_path_cache["path"]

def extract_youtube_urls(text):
    if not text:
        return []
    return YOUTUBE_URL_RE.findall(text)

def _is_youtube_link_message(message):
    return bool(message.text) and bool(YOUTUBE_URL_RE.search(message.text))

# Remembers which extraction mode (with/without cookies) and player client
# last succeeded, so a request that hits YouTube's anti-bot gate doesn't pay
# the failing attempts' latency on every single link. The android player
# client doesn't face the web client's JS-challenge/sign-in gate, so once it
# proves working it becomes the first attempt until the web path recovers.
_yt_last_good_mode = {"use_cookies": None, "player_client": "default"}

def _youtube_gate_error(exc):
    """True if `exc` looks like one of YouTube's anti-bot/stale-session gates
    ("The page needs to be reloaded", "Sign in to confirm you're not a bot",
    cookie/consent errors) rather than an ordinary video-unavailable error —
    those are exactly the cases where retrying without the (stale) cookie
    jar succeeds where the cookie'd attempt failed."""
    msg = str(exc).lower()
    return any(
        s in msg
        for s in (
            "needs to be reloaded",
            "sign in",
            "not a bot",
            "confirm you're not",
            "cookies",
            "consent",
        )
    )

def _youtube_base_ydl_opts(use_cookies=True, player_client="default"):
    """Base yt-dlp options shared by every YouTube extraction/download call.
    Cookies are attached only when requested AND the file actually exists —
    a stale/flagged cookie jar served from a datacenter IP actively triggers
    YouTube's reload/bot gate, so callers fall back to anonymous requests
    when the cookie'd attempt failed that way (see _youtube_gate_error).
    player_client selects the YouTube player API client: "default" is the
    web client; "android" bypasses the web client's JS-challenge/sign-in
    gate (at most ~1080p, which already covers this bot's ceiling)."""
    opts = {
        "quiet": True, "no_warnings": True, "socket_timeout": 30, "retries": 2,
        "js_runtimes": {"deno": {}},  # EJS challenge solving — required since yt-dlp 2026.x
    }
    if use_cookies and YOUTUBE_COOKIES_FILE and os.path.isfile(YOUTUBE_COOKIES_FILE):
        opts["cookiefile"] = YOUTUBE_COOKIES_FILE
    if player_client != "default":
        opts["extractor_args"] = {"youtube": {"player_client": [player_client]}}
    return opts

def _extract_youtube_qualities(url):
    """Extract video info and build the set of downloadable options.

    Without ffmpeg: only progressive formats (video+audio already muxed by
    YouTube) are offered, capped at YOUTUBE_MAX_HEIGHT — whatever heights
    YouTube actually still serves progressively for this specific video
    (can be as low as just 360p; that's a YouTube-side reduction over the
    years, not something we can work around without merging).

    With ffmpeg (imageio-ffmpeg): ANY available height up to
    YOUTUBE_MAX_HEIGHT is offered — for heights with no progressive format,
    the best video-only + best audio-only streams get merged into one file
    at download time via yt-dlp's own ffmpeg-backed merger.

    Returns (title, options, reason) — options_dict maps a short key ("v360",
    "v1080", "audio") to {"height", "is_audio", "label", "filesize",
    "needs_merge"}. Returns (None, None, reason) on failure, where reason is
    "gate" (YouTube demanded sign-in on every path — operational: refresh
    cookies / check IP standing) or "unavailable" (private/deleted/etc.).
    No raw URLs are stored here — download time re-resolves via a fresh
    yt-dlp call using a format-selector string, since extraction-time URLs
    can be short-lived and HD extraction/download takes longer."""
    ffmpeg_path = _ffmpeg_location()

    # Attempt order: last-known-good combo first; otherwise cookies first
    # (fresh cookies unlock age-gated/HD content) with an anonymous retry —
    # a stale cookie jar replayed from a datacenter IP is exactly what
    # triggers YouTube's "The page needs to be reloaded" / "Sign in to
    # confirm you're not a bot" gate, and a cookie-less attempt gets past it.
    # If every web-client attempt hits the gate, one last attempt goes out
    # over the android player client, which doesn't face the web gate.
    if _yt_last_good_mode["use_cookies"] is not None:
        first = _yt_last_good_mode["use_cookies"]
        modes = [first]
        if first and YOUTUBE_COOKIES_FILE:
            modes.append(False)  # stale-cookie gate fallback (symmetric)
        elif not first and YOUTUBE_COOKIES_FILE:
            modes.append(True)
    elif YOUTUBE_COOKIES_FILE:
        modes = [True, False]
    else:
        modes = [False]

    remembered_client = _yt_last_good_mode.get("player_client", "default")
    attempts = []
    for i, use_cookies in enumerate(modes):
        attempts.append((use_cookies, remembered_client if i == 0 else "default"))

    info = None
    last_exc = None
    for use_cookies, player_client in attempts:
        mode_name = ("cookies" if use_cookies else "anonymous") + (
            f"+{player_client}" if player_client != "default" else "")
        try:
            with yt_dlp.YoutubeDL(_youtube_base_ydl_opts(use_cookies=use_cookies,
                                                          player_client=player_client)) as ydl:
                info = ydl.extract_info(url, download=False)
            _yt_last_good_mode["use_cookies"] = use_cookies
            _yt_last_good_mode["player_client"] = player_client
            log.info("YouTube extraction OK for %s (mode=%s)", url, mode_name)
            break
        except Exception as e:
            last_exc = e
            log.warning("YouTube extraction failed for %s (mode=%s): %s", url, mode_name, str(e)[:200])
            if not _youtube_gate_error(e):
                break  # ordinary failure (private/deleted video etc.) — switching modes won't help
            if use_cookies:
                log.info("YouTube anti-bot gate hit with cookies — retrying anonymously for %s", url)
    if not info and last_exc is not None and _youtube_gate_error(last_exc) and all(
            c != "android" for _, c in attempts):
        # Web player clients are all gated from here — last resort: the
        # android player client, which doesn't face the web sign-in gate.
        try:
            with yt_dlp.YoutubeDL(_youtube_base_ydl_opts(use_cookies=False,
                                                          player_client="android")) as ydl:
                info = ydl.extract_info(url, download=False)
            _yt_last_good_mode["use_cookies"] = False
            _yt_last_good_mode["player_client"] = "android"
            log.info("YouTube extraction OK for %s (mode=anonymous+android fallback)", url)
        except Exception as e:
            last_exc = e
            log.warning("YouTube extraction failed for %s (mode=anonymous+android): %s", url, str(e)[:200])
    if not info:
        if last_exc is not None:
            log.warning("YouTube extraction failed for %s (all modes)", url, exc_info=last_exc)
        reason = "gate" if last_exc is not None and _youtube_gate_error(last_exc) else "unavailable"
        return None, None, reason

    title = (info.get("title") or "").strip()
    all_formats = info.get("formats") or []

    audio_candidates = [
        f for f in all_formats
        if (f.get("vcodec") or "none") == "none" and (f.get("acodec") or "none") != "none" and f.get("url")
    ]
    best_audio = max(audio_candidates, key=lambda f: f.get("abr") or 0) if audio_candidates else None
    best_audio_size = (best_audio.get("filesize") or best_audio.get("filesize_approx") or 0) if best_audio else 0

    available_heights = sorted({
        f.get("height") for f in all_formats
        if f.get("height") and (f.get("vcodec") or "none") != "none" and f.get("url")
        and 0 < f["height"] <= YOUTUBE_MAX_HEIGHT
    })

    options = {}
    for h in available_heights:
        candidates_at_h = [f for f in all_formats if f.get("height") == h and (f.get("vcodec") or "none") != "none" and f.get("url")]
        if not candidates_at_h:
            continue
        best_video = max(candidates_at_h, key=lambda f: f.get("tbr") or 0)
        is_progressive = (best_video.get("acodec") or "none") != "none"

        if not is_progressive and not ffmpeg_path:
            continue  # would need a merge we can't do — don't offer it

        est_size = best_video.get("filesize") or best_video.get("filesize_approx") or 0
        if not is_progressive:
            est_size += best_audio_size

        options[f"v{h}"] = {
            "height": h, "is_audio": False, "label": f"🎬 {h}p",
            "filesize": est_size or None, "needs_merge": not is_progressive,
        }

    if best_audio:
        if ffmpeg_path:
            options["audio"] = {
                "height": None, "is_audio": True, "label": "🎵 فقط صدا (MP3)",
                "filesize": best_audio_size or None, "needs_merge": False,
            }
        else:
            # No ffmpeg -> can't transcode to mp3, send the raw best audio
            # stream as-is (still a normal playable file, just not literally .mp3).
            options["audio"] = {
                "height": None, "is_audio": True, "label": "🎵 فقط صدا",
                "filesize": best_audio_size or None, "needs_merge": False,
            }

    return title, options, None

def _download_youtube_quality(url, opt, dest_dir):
    """Download (and merge via ffmpeg if needed) the chosen quality using
    yt-dlp's own downloader — required whenever opt['needs_merge'] is True,
    since that's a separate video+audio stream pair only yt-dlp+ffmpeg can
    combine. Returns the output file path, or None on failure. Tries the
    exact requested quality first; if that fails for any reason, retries
    once with a looser selector (still respecting the height ceiling)
    before giving up — a specific format can occasionally disappear or
    fail to merge even when a nearby one would have worked fine. Within
    each selector attempt, an anti-bot/stale-cookie gate failure is retried
    once without cookies (same logic as extraction-time fallback), since a
    flagged cookie jar served from a datacenter IP breaks downloads too,
    and an mp4 postprocess/merge failure is retried as matroska (mkv
    accepts every codec pair YouTube serves, while forced-mp4 muxing can
    legitimately fail on VP9/AV1+Opus input)."""
    ffmpeg_path = _ffmpeg_location()
    outtmpl = os.path.join(dest_dir, "file.%(ext)s")

    if opt["is_audio"]:
        format_attempts = ["bestaudio/best"]
    else:
        h = opt["height"]
        format_attempts = [
            f"best[height={h}][vcodec!=none][acodec!=none]/bestvideo[height={h}]+bestaudio/best[height<={h}]",
            f"best[height<={h}]/bestvideo[height<={h}]+bestaudio/best",  # looser fallback
        ]

    cookies_usable = bool(YOUTUBE_COOKIES_FILE and os.path.isfile(YOUTUBE_COOKIES_FILE))
    preferred = _yt_last_good_mode["use_cookies"]  # extraction usually just succeeded with this mode

    for attempt_num, fmt in enumerate(format_attempts, 1):
        # Clear out anything left behind by any failed earlier attempt
        # (partial merges, orphaned video-only/audio-only fragments) so
        # the directory-scan fallback below can't pick up stale output.
        for fn in os.listdir(dest_dir):
            try:
                os.remove(os.path.join(dest_dir, fn))
            except OSError:
                pass

        # Merging needs room for the downloaded streams PLUS the merged
        # output (~2x total); bail out early with a clear reason instead of
        # failing cryptically inside ffmpeg when the volume is nearly full.
        est_bytes = opt.get("filesize") or 0
        need_mb = max(200, (est_bytes * 2) // (1024 * 1024) + 50)
        try:
            free_mb = shutil.disk_usage(dest_dir).free // (1024 * 1024)
            if free_mb < need_mb:
                log.error(
                    "YouTube download aborted for %s (%s): only %dMB free on %s, need ~%dMB "
                    "(volume nearly full? old botapi/upload caches may need pruning).",
                    url, opt.get("label"), free_mb, dest_dir, need_mb,
                )
                return None
        except OSError:
            pass

        cookie_modes = [preferred, not preferred] if preferred is not None else [True, False]
        cookie_modes = cookie_modes if cookies_usable else [False]

        # Try the last-known-good (cookies, player-client) combo first, then
        # the standard fallbacks — at most one extra attempt, and usually the
        # first one succeeds since extraction just proved the combo.
        remembered_client = _yt_last_good_mode.get("player_client", "default")
        pairs = []
        if preferred is not None:
            pairs.append((preferred, remembered_client))
        for c in cookie_modes:
            if (c, "default") not in pairs:
                pairs.append((c, "default"))

        next_selector = False  # generic failures skip remaining variants entirely (original behaviour)

        for use_cookies, player_client in pairs:
            if next_selector:
                break
            mode_name = ("cookies" if use_cookies else "anonymous") + (
                f"+{player_client}" if player_client != "default" else "")
            # Telegram-friendliest container first; fall back to mkv only if
            # ffmpeg's mp4 muxing actually fails (it accepts any codecs).
            merge_exts = ["mp4", "matroska"] if not opt["is_audio"] else [None]

            for merge_ext in merge_exts:
                ydl_opts = _youtube_base_ydl_opts(use_cookies=use_cookies,
                                                  player_client=player_client)
                ydl_opts["format"] = fmt
                ydl_opts["outtmpl"] = outtmpl
                if merge_ext:
                    ydl_opts["merge_output_format"] = merge_ext
                if ffmpeg_path:
                    ydl_opts["ffmpeg_location"] = ffmpeg_path
                    if opt["is_audio"]:
                        ydl_opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]

                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                    requested = (info.get("requested_downloads") or [None])[0] if info else None
                    if requested and requested.get("filepath") and os.path.isfile(requested["filepath"]):
                        _yt_last_good_mode["use_cookies"] = use_cookies
                        _yt_last_good_mode["player_client"] = player_client
                        return requested["filepath"]
                    for fn in os.listdir(dest_dir):
                        fp = os.path.join(dest_dir, fn)
                        if os.path.isfile(fp):
                            _yt_last_good_mode["use_cookies"] = use_cookies
                            _yt_last_good_mode["player_client"] = player_client
                            return fp
                    next_selector = True  # yt-dlp succeeded but no file appeared — no variant will change that
                    break
                except Exception as e:
                    err_str = str(e)[:800]
                    log.warning(
                        "YouTube quality download attempt %d/%d failed for %s (%s, format=%r, mode=%s, merge=%s): %s",
                        attempt_num, len(format_attempts), url, opt.get("label"), fmt,
                        mode_name, merge_ext or "-", err_str,
                    )
                    lowered = err_str.lower()
                    if _youtube_gate_error(e) and use_cookies:
                        log.info("YouTube anti-bot gate hit during download — retrying same quality anonymously")
                        break  # switch cookie mode (restarts at the mp4 container there)
                    if "postprocessing" in lowered and merge_ext == "mp4":
                        log.info("ffmpeg could not produce mp4 — retrying same quality as matroska")
                        continue
                    next_selector = True  # anything else — next selector attempt
                    break

    return None
