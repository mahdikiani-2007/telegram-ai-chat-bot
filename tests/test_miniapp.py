"""Mini App games: config URL derivation, score store, HTTP server E2E,
initData auth, /play handler. Hermetic: real repo on the temp DATA_DIR, real
HTTP server on an ephemeral loopback port, initData signed in-test with the
same HMAC scheme Telegram uses (mirrors telebot.util.validate_web_app_data).
"""
import hashlib
import hmac
import importlib
import json
import os
import sys
import threading
import types as std_types
from urllib.parse import urlencode

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_mod
from db import repo

BOT_TOKEN = "900111:MiniAppTestToken-abcdef"
_MU = 900000  # user-id block owned by this suite


def _ns(**kwargs):
    obj = std_types.SimpleNamespace()
    for k, v in kwargs.items():
        setattr(obj, k, v)
    return obj


def _user(uid):
    return _ns(id=uid, is_bot=False, first_name="تست")


def _make_init_data(uid, name="بازیکن", start_param=None, chat_type="private",
                    token=BOT_TOKEN, tamper=False):
    """Sign initData exactly like Telegram (Bot API 7.x WebAppData scheme)."""
    fields = {
        "auth_date": "1730000000",
        "chat_type": chat_type,
        "query_id": "AAF38F88AAAD7777",
        "user": json.dumps({"id": uid, "first_name": name}, ensure_ascii=False),
    }
    if start_param:
        fields["start_param"] = start_param
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    sig = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if tamper:
        sig = ("0" if sig[0] != "0" else "1") + sig[1:]
    return urlencode({**fields, "hash": sig})


@pytest.fixture
def server():
    import miniapp_server

    srv = miniapp_server.MiniAppHTTPServer(("127.0.0.1", 0), BOT_TOKEN)
    thread = threading.Thread(target=srv.serve_forever, daemon=True, name="t-miniapp")
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base
    srv.shutdown()
    srv.server_close()


def _cleanup_scopes(user_ids=(), scopes=()):
    with repo._write_lock:
        conn = repo._connect()
        for uid in user_ids:
            conn.execute("DELETE FROM miniapp_scores WHERE user_id = ?", (uid,))
        for scope in scopes:
            conn.execute("DELETE FROM miniapp_scores WHERE scope_key = ?", (scope,))
        conn.execute("DELETE FROM known_chats WHERE chat_id IN ('-991001', '991001')")
        conn.commit()


# =========================
# Config: public URL derivation
# =========================
def test_miniapp_public_url_from_railway_domain(monkeypatch):
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "bot-prod.up.railway.app")
    monkeypatch.delenv("MINIAPP_PUBLIC_URL", raising=False)
    importlib.reload(config_mod)
    try:
        assert config_mod.MINIAPP_PUBLIC_URL == "https://bot-prod.up.railway.app"
    finally:
        monkeypatch.undo()
        importlib.reload(config_mod)


def test_miniapp_public_url_override_wins_and_absent_disables(monkeypatch):
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "auto.up.railway.app")
    monkeypatch.setenv("MINIAPP_PUBLIC_URL", "https://games.example.com")
    importlib.reload(config_mod)
    try:
        assert config_mod.MINIAPP_PUBLIC_URL == "https://games.example.com"
        monkeypatch.delenv("MINIAPP_PUBLIC_URL")
        monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN")
        importlib.reload(config_mod)
        assert config_mod.MINIAPP_PUBLIC_URL == ""
    finally:
        monkeypatch.undo()
        importlib.reload(config_mod)


# =========================
# DB: score store semantics
# =========================
def test_db_upsert_is_best_only_and_counts_plays():
    uid = _MU + 1
    try:
        best, new = repo.upsert_miniapp_score("global", uid, "snake_neo", 100, "الف")
        assert (best, new) == (100, True)
        best, new = repo.upsert_miniapp_score("global", uid, "snake_neo", 40, "الف")
        assert (best, new) == (100, False), "lower score must not lower the best"
        best, new = repo.upsert_miniapp_score("global", uid, "snake_neo", 250, "الف")
        assert (best, new) == (250, True)
        row = repo._connect().execute(
            "SELECT plays, last_name FROM miniapp_scores WHERE scope_key='global' "
            "AND user_id=? AND game='snake_neo'", (uid,)).fetchone()
        assert row[0] == 3 and row[1] == "الف"
        assert repo.miniapp_user_rank("global", uid, "snake_neo")[1] == 250
        assert repo.miniapp_user_rank("global", uid, "snake_rush") == (None, 0)
    finally:
        _cleanup_scopes(user_ids=(uid,))


def test_db_leaderboard_orders_and_scope_title():
    uid_a, uid_b = _MU + 2, _MU + 3
    try:
        repo.upsert_known_chat("-991001", "supergroup", None, "گروه تست")
        repo.upsert_miniapp_score("g-991001", uid_a, "snake_neo", 300, "الف")
        repo.upsert_miniapp_score("g-991001", uid_b, "snake_neo", 500, "ب")
        repo.upsert_miniapp_score("global", uid_a, "snake_neo", 999, "الف")
        rows = repo.miniapp_leaderboard("g-991001", "snake_neo", 10)
        assert [r[0] for r in rows] == [uid_b, uid_a]
        assert [r[2] for r in rows] == [500, 300]
        assert repo.miniapp_leaderboard("g-991001", "snake_rush") == []
        assert repo.miniapp_user_rank("g-991001", uid_a, "snake_neo") == (2, 300)
        assert repo.miniapp_user_rank("g-991001", uid_b, "snake_neo") == (1, 500)
        assert repo.miniapp_scope_title("g-991001") == "گروه تست"
        assert repo.miniapp_scope_title("global") == ""
    finally:
        _cleanup_scopes(user_ids=(uid_a, uid_b), scopes=("g-991001",))


# =========================
# HTTP server E2E
# =========================
def test_server_serves_static_index(server):
    r = requests.get(f"{server}/", timeout=5)
    assert r.status_code == 200
    assert "text/html" in r.headers["Content-Type"]
    assert "telegram-web-app.js" in r.text


def test_server_blocks_unknown_paths(server):
    for path in ("/../config.py", "/%2e%2e/config.py", "/api/unknown", "/nope.html"):
        r = requests.get(f"{server}{path}", timeout=5)
        assert r.status_code == 404, path


def test_submit_happy_path_best_only(server):
    uid = _MU + 10
    try:
        r1 = requests.post(f"{server}/api/submit", timeout=5, json={
            "initData": _make_init_data(uid, name="بازیکن یک"),
            "game": "snake_neo", "score": 100})
        assert r1.status_code == 200 and r1.json()["ok"] is True
        body = r1.json()
        assert (body["best"], body["new_best"], body["rank"]) == (100, True, 1)

        r2 = requests.post(f"{server}/api/submit", timeout=5, json={
            "initData": _make_init_data(uid, name="بازیکن یک"),
            "game": "snake_neo", "score": 40})
        assert r2.json()["best"] == 100 and r2.json()["new_best"] is False

        r3 = requests.post(f"{server}/api/submit", timeout=5, json={
            "initData": _make_init_data(uid, name="بازیکن یک"),
            "game": "snake_neo", "score": 500})
        assert r3.json()["best"] == 500 and r3.json()["rank"] == 1
    finally:
        _cleanup_scopes(user_ids=(uid,))


def test_submit_rejects_bad_signature_and_missing_data(server):
    uid = _MU + 11
    try:
        r = requests.post(f"{server}/api/submit", timeout=5, json={
            "initData": _make_init_data(uid, tamper=True),
            "game": "snake_neo", "score": 10})
        assert r.status_code == 401

        r = requests.post(f"{server}/api/submit", timeout=5, json={
            "game": "snake_neo", "score": 10})
        assert r.status_code == 401

        # signed with a DIFFERENT token (foreign bot) → must fail
        r = requests.post(f"{server}/api/submit", timeout=5, json={
            "initData": _make_init_data(uid, token="999999:OtherToken"),
            "game": "snake_neo", "score": 10})
        assert r.status_code == 401
    finally:
        _cleanup_scopes(user_ids=(uid,))


def test_submit_rejects_malformed_scores_and_games(server):
    uid = _MU + 12
    bad_payloads = [
        {"game": "snake_neo", "score": "abc"},
        {"game": "snake_neo", "score": True},
        {"game": "snake_neo", "score": 10_000_001},
        {"game": "snake_neo", "score": -5},
        {"score": 10},
        {"game": "DROP TABLE miniapp_scores", "score": 10},
    ]
    try:
        for payload in bad_payloads:
            r = requests.post(f"{server}/api/submit", timeout=5, json={
                "initData": _make_init_data(uid), **payload})
            assert r.status_code == 400, payload
        assert repo._connect().execute(
            "SELECT COUNT(*) FROM miniapp_scores WHERE user_id = ?", (uid,)).fetchone()[0] == 0
    finally:
        _cleanup_scopes(user_ids=(uid,))


def test_submit_rejects_oversized_body(server):
    r = requests.post(f"{server}/api/submit", timeout=5, json={
        "initData": "x", "game": "snake_neo", "score": 1,
        "junk": "A" * 20000})
    assert r.status_code == 413


def test_group_scope_via_start_param_is_isolated(server):
    uid_a, uid_b = _MU + 20, _MU + 21
    try:
        requests.post(f"{server}/api/submit", timeout=5, json={
            "initData": _make_init_data(uid_a, name="الف",
                                        start_param="g-991002", chat_type="supergroup"),
            "game": "snake_neo", "score": 300})
        requests.post(f"{server}/api/submit", timeout=5, json={
            "initData": _make_init_data(uid_b, name="ب",
                                        start_param="g-991002", chat_type="supergroup"),
            "game": "snake_neo", "score": 700})
        requests.post(f"{server}/api/submit", timeout=5, json={
            "initData": _make_init_data(uid_b, name="ب"),
            "game": "snake_neo", "score": 999})

        lb = requests.post(f"{server}/api/leaderboard", timeout=5, json={
            "initData": _make_init_data(uid_a, start_param="g-991002",
                                        chat_type="supergroup"),
            "game": "snake_neo"}).json()
        assert lb["ok"] is True and lb["scope"] == "g-991002"
        assert [row["uid"] for row in lb["rows"]] == [uid_b, uid_a]
        assert lb["you"]["rank"] == 2 and lb["you"]["best"] == 300

        g_lb = requests.post(f"{server}/api/leaderboard", timeout=5, json={
            "initData": _make_init_data(uid_b), "game": "snake_neo"}).json()
        assert g_lb["scope"] == "global"
        assert [row["uid"] for row in g_lb["rows"]] == [uid_b]
        assert g_lb["you"]["rank"] == 1 and g_lb["you"]["best"] == 999
    finally:
        _cleanup_scopes(user_ids=(uid_a, uid_b), scopes=("g-991002",))


# =========================
# /play handler
# =========================
def test_play_private_gives_web_app_button(monkeypatch):
    import handlers.miniapp as miniapp_mod
    from telebot import types as tb_types

    sent = []

    class _Bot:
        def reply_to(self, message, text, **kwargs):
            sent.append((message.chat.id, text, kwargs))
            return _ns(message_id=1)

    monkeypatch.setattr(miniapp_mod, "bot", _Bot())
    monkeypatch.setattr(miniapp_mod.ps, "feature_gate_message", lambda *a, **k: True)
    monkeypatch.setattr(miniapp_mod, "MINIAPP_PUBLIC_URL", "https://games.example.com")

    msg = _ns(from_user=_user(930), chat=_ns(type="private", id=930),
              text="/play", message_id=1, content_type="text")
    miniapp_mod.handle_play(msg)
    assert sent, "private /play must answer"
    markup = sent[0][2]["reply_markup"]
    btn = markup.keyboard[0][0]
    assert isinstance(btn, tb_types.InlineKeyboardButton)
    assert btn.web_app is not None and btn.web_app.url == "https://games.example.com"


def test_play_group_gives_startapp_url_button(monkeypatch):
    import handlers.miniapp as miniapp_mod

    sent = []

    class _Bot:
        def reply_to(self, message, text, **kwargs):
            sent.append((message.chat.id, text, kwargs))
            return _ns(message_id=1)

    monkeypatch.setattr(miniapp_mod, "bot", _Bot())
    monkeypatch.setattr(miniapp_mod.ps, "feature_gate_message", lambda *a, **k: True)
    monkeypatch.setattr(miniapp_mod, "MINIAPP_PUBLIC_URL", "https://games.example.com")
    monkeypatch.setattr(miniapp_mod, "get_bot_identity",
                        lambda: {"username": "LDetectiveBot", "id": 900111})

    msg = _ns(from_user=_user(931), chat=_ns(type="supergroup", id=-100991003),
              text="/play", message_id=2, content_type="text")
    miniapp_mod.handle_play(msg)
    assert sent
    btn = sent[0][2]["reply_markup"].keyboard[0][0]
    assert btn.url == "https://t.me/LDetectiveBot?startapp=g-100991003"
    assert btn.web_app is None


def test_play_silent_without_public_url_and_when_disabled(monkeypatch):
    import handlers.miniapp as miniapp_mod

    sent = []

    class _Bot:
        def reply_to(self, message, text, **kwargs):
            sent.append((message.chat.id, text, kwargs))
            return _ns(message_id=1)

    monkeypatch.setattr(miniapp_mod, "bot", _Bot())
    monkeypatch.setattr(miniapp_mod.ps, "feature_gate_message", lambda *a, **k: True)
    monkeypatch.setattr(miniapp_mod, "MINIAPP_PUBLIC_URL", "")
    miniapp_mod.handle_play(_ns(from_user=_user(932), chat=_ns(type="private", id=932),
                                text="/play", message_id=3, content_type="text"))
    assert sent, "configured-off feature must still answer with a hint (explicit invocation)"

    sent.clear()
    monkeypatch.setattr(miniapp_mod, "MINIAPP_PUBLIC_URL", "https://games.example.com")
    monkeypatch.setattr(miniapp_mod.ps, "feature_gate_message", lambda *a, **k: False)
    miniapp_mod.handle_play(_ns(from_user=_user(932), chat=_ns(type="private", id=932),
                                text="/play", message_id=4, content_type="text"))
    assert not sent, "feature gate must silence the handler"
