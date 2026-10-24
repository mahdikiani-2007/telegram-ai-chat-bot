"""Mini App host: serves the games frontend + a small JSON API from the bot
process itself. Stdlib http.server only — no new dependencies (Telegram opens
this URL as a Mini App via /play or the chat menu button).

Auth model: every API call carries Telegram's signed `initData`; it is
validated with the Bot API 7.x HMAC scheme via telebot.util.parse_web_app_data
(secret = HMAC-SHA256(b"WebAppData", bot_token)). No signature → 401. Nothing
about the user is ever trusted from the plain JSON body.

Group scoping: the group's /play message uses a t.me URL button with
`?startapp=g<chat_id>`; Telegram signs start_param into initData, so the
server can trust `g<chat_id>` as the leaderboard scope. Opens without it are
private/global.

Routes (fixed whitelist — no path traversal surface):
  GET  /               → mini app page
  GET  /<static file>  → whitelisted assets only
  POST /api/submit     → {initData, game, score} → best-only upsert
  POST /api/leaderboard→ {initData, game} → top-20 + caller's rank
"""
import json
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl

from config import MINIAPP_PORT, MINIAPP_PUBLIC_URL, TELEGRAM_TOKEN
from db import repo

log = logging.getLogger("L-bot")

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "miniapp", "static")
MAX_BODY_BYTES = 16 * 1024
MAX_SCORE = 10_000_000
MAX_LEADERBOARD_ROWS = 20
_GAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")

_STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/style.css": "style.css",
    "/app.js": "app.js",
    "/games/snake.js": "games/snake.js",
}
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def _scope_from(start_param):
    """Trusted leaderboard scope from the SIGNED start_param: 'g<chat_id>' for
    group opens (g + optional minus + digits), else the global board."""
    s = str(start_param or "")
    if s.startswith("g") and len(s) > 1 and s[1:].lstrip("-").isdigit():
        return s
    return "global"


def _validate_init_data(token, raw_init_data):
    """Return a trusted context dict, or None. Mirrors Telegram's WebAppData
    HMAC scheme through telebot.util; nothing untrusted survives this point."""
    if not raw_init_data or not token:
        return None
    from telebot import util

    parsed = util.parse_web_app_data(str(token), str(raw_init_data))
    if not parsed:
        return None
    user = parsed.get("user")
    if not isinstance(user, dict):
        return None
    try:
        uid = int(user.get("id"))
    except (TypeError, ValueError):
        return None
    if uid <= 0:
        return None
    name = str(user.get("first_name") or "").strip()[:64] or "کاربر"
    return {
        "user_id": uid,
        "name": name,
        "start_param": str(parsed.get("start_param") or ""),
    }


class _MiniAppHandler(BaseHTTPRequestHandler):
    server_version = "LMiniApp/1.0"

    def log_message(self, fmt, *args):  # route through the bot's logger
        log.debug("miniapp http: " + fmt, *args)

    # -- helpers ----------------------------------------------------------
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    # -- GET: static whitelist -------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        fname = _STATIC_FILES.get(path)
        if fname is None:
            return self._json(404, {"ok": False, "error": "not found"})
        # fname comes from the fixed whitelist above (never from the request),
        # so there is no traversal surface; missing files are a deploy bug.
        fp = os.path.join(_STATIC_DIR, fname)
        try:
            with open(fp, "rb") as fh:
                body = fh.read()
        except OSError:
            log.error("MiniApp static file missing: %s", fname)
            return self._json(500, {"ok": False, "error": "static missing"})
        ext = os.path.splitext(fname)[1]
        self._send(200, body, _CONTENT_TYPES.get(ext, "application/octet-stream"))

    # -- POST: JSON API ----------------------------------------------------
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/api/submit", "/api/leaderboard"):
            return self._json(404, {"ok": False, "error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"ok": False, "error": "bad length"})
        if length <= 0 or length > MAX_BODY_BYTES:
            self.close_connection = True
            return self._json(413, {"ok": False, "error": "body too large"})
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except Exception:
            return self._json(400, {"ok": False, "error": "bad json"})

        user = _validate_init_data(self.server.token, data.get("initData"))
        if not user:
            return self._json(401, {"ok": False, "error": "unauthorized"})

        game = str(data.get("game") or "")
        if not _GAME_RE.fullmatch(game):
            return self._json(400, {"ok": False, "error": "bad game"})

        scope = _scope_from(user["start_param"])
        if path == "/api/submit":
            return self._handle_submit(user, scope, game, data)
        return self._handle_leaderboard(user, scope, game)

    def _handle_submit(self, user, scope, game, data):
        score = data.get("score")
        if (not isinstance(score, int) or isinstance(score, bool)
                or not 0 <= score <= MAX_SCORE):
            return self._json(400, {"ok": False, "error": "bad score"})
        try:
            best, is_new_best = repo.upsert_miniapp_score(
                scope, user["user_id"], game, score, user["name"])
            rank, _stored = repo.miniapp_user_rank(scope, user["user_id"], game)
        except Exception:
            log.error("MiniApp submit failed for user %s", user["user_id"], exc_info=True)
            return self._json(500, {"ok": False, "error": "storage"})
        return self._json(200, {
            "ok": True,
            "game": game,
            "scope": scope,
            "title": repo.miniapp_scope_title(scope),
            "best": best,
            "new_best": is_new_best,
            "rank": rank,
        })

    def _handle_leaderboard(self, user, scope, game):
        try:
            rows = repo.miniapp_leaderboard(scope, game, MAX_LEADERBOARD_ROWS)
            rank, best = repo.miniapp_user_rank(scope, user["user_id"], game)
        except Exception:
            log.error("MiniApp leaderboard failed for scope %s", scope, exc_info=True)
            return self._json(500, {"ok": False, "error": "storage"})
        return self._json(200, {
            "ok": True,
            "game": game,
            "scope": scope,
            "title": repo.miniapp_scope_title(scope) or (
                "رتبه‌بندی سراسری" if scope == "global" else "جدول این گروه"),
            "rows": [
                {"uid": r[0], "name": r[1], "best": r[2], "plays": r[3]}
                for r in rows
            ],
            "you": {"rank": rank, "best": best},
        })


class MiniAppHTTPServer(ThreadingHTTPServer):
    """Threaded stdlib server; the bot token travels on the instance so the
    handler can validate initData against the right bot."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, token):
        self.token = token
        super().__init__(addr, _MiniAppHandler)


def start_miniapp_server(port=None, token=None):
    """Bind + serve on a daemon thread. Returns the running server, or None
    when the port is taken (feature keeps working, just not served)."""
    port = int(port or MINIAPP_PORT)
    try:
        srv = MiniAppHTTPServer(("0.0.0.0", port), token or TELEGRAM_TOKEN)
    except OSError as exc:
        log.error("MiniApp server could not bind :%d (%s) — games web app is down.", port, exc)
        return None
    thread = threading.Thread(
        target=srv.serve_forever, daemon=True, name="miniapp-http")
    thread.start()
    log.info("MiniApp server listening on :%d (public: %s)",
             port, MINIAPP_PUBLIC_URL or "NOT CONFIGURED — /play will say so")
    return srv
