"""Regression tests for the 2026-10-23 full-review bug fixes.

Each test encodes ONE reviewed bug and fails against the pre-fix code:
  1. Behavior profiler read UTC stat_date so the nightly run (03:30 Iran ==
     00:00 UTC) analyzed the just-started, empty day instead of yesterday.
  2. AdminNotifier + scheduled daily backup targeted legacy ADMIN_ID (0 when
     only ADMIN_IDS is configured) and silently disabled themselves.
  3. Broadcast confirm held _broadcast_lock forever when the progress message
     send failed (worker never started, lock never released).
  4. /stop and stream-error cleanup deleted the NEWEST user row of the chat
     scope (possibly another user's message in busy groups) instead of the
     aborted turn's own row.
  5. Digest sends had no plain-text fallback, so one unbalanced-Markdown LLM
     output failed delivery to every group.
  6. /pic echoed the raw prompt inside a Markdown code span without
     sanitize_backtick_span, so a backtick in the prompt failed the send.

Hermetic like the other suites: conftest stub env + temp DATA_DIR, no
network (bot/agnes/HTTP monkeypatched per test).
"""
import importlib
import os
import sys
import time
import types as std_types
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_mod
import handlers.shared as shared
from db import repo
from services import panel_settings as ps
from telebot.apihelper import ApiTelegramException


def _ns(**kwargs):
    obj = std_types.SimpleNamespace()
    for k, v in kwargs.items():
        setattr(obj, k, v)
    return obj


def _user(uid):
    return _ns(id=uid)


def _pcall(uid, data, chat_type="private", chat_id=100):
    return _ns(id="cbfix", data=data, from_user=_user(uid),
               message=_ns(chat=_ns(type=chat_type, id=chat_id), message_id=7))


def _cleanup_behavior_rows():
    with repo._write_lock:
        conn = repo._connect()
        conn.execute("DELETE FROM behavior_stats WHERE chat_id LIKE 'tst-fix-%'")
        conn.commit()


# =========================
# Fix 1: behavior profiler dates (Iran day, not UTC day)
# =========================
def test_record_behavior_stamps_iran_date(monkeypatch):
    """record_behavior must stamp the IRAN date. The mocked clock sits far in
    the future (and past Iran-midnight, so its UTC date differs) — a UTC-stamp
    implementation can never produce it, whatever day the suite runs on."""
    import db as db_mod

    fixed = datetime(2099, 3, 15, 1, 0, tzinfo=config_mod.IRAN_TZ)  # UTC 2099-03-14 21:30
    monkeypatch.setattr(db_mod, "iran_now", lambda: fixed, raising=False)
    try:
        repo.record_behavior("tst-fix-iran", 55, 10, False, False, False, False, "s", 1)
        with repo._write_lock:
            conn = repo._connect()
            row = conn.execute(
                "SELECT stat_date FROM behavior_stats WHERE chat_id = 'tst-fix-iran' AND user_id = 55"
            ).fetchone()
        assert row is not None
        assert row[0] == "2099-03-15"
    finally:
        _cleanup_behavior_rows()


def test_day_behavior_rows_default_targets_yesterday_iran(monkeypatch):
    """The no-arg call (what the nightly profiler makes at 03:30 Iran) must
    return the Iran day that just ended, not the UTC day that just started."""
    import db as db_mod

    fixed = datetime(2026, 10, 23, 23, 0, tzinfo=config_mod.IRAN_TZ)  # UTC 2026-10-23 19:30
    monkeypatch.setattr(db_mod, "iran_now", lambda: fixed, raising=False)
    try:
        with repo._write_lock:
            conn = repo._connect()
            conn.execute(
                "INSERT INTO behavior_stats (chat_id, user_id, stat_date, msg_count) "
                "VALUES ('tst-fix-yd', 56, '2026-10-22', 12), ('tst-fix-yd', 57, '2026-10-23', 12)"
            )
            conn.commit()
        uids = {r[1] for r in repo.get_day_behavior_rows()}
        assert 56 in uids, "yesterday-Iran row must be analyzed"
        assert 57 not in uids, "today-Iran row must NOT be analyzed by the nightly run"
    finally:
        _cleanup_behavior_rows()


# =========================
# Fix 2: OWNER_ID as the notify/backup target
# =========================
def test_admin_notifier_targets_owner_when_only_admin_ids_set(monkeypatch):
    """ADMIN_IDS-only config must still route error notifications to the
    owner — previously AdminNotifier read legacy ADMIN_ID (0) and went mute."""
    import bots as bots_mod

    # Handler modules registered against THESE instances at collection time;
    # put them back afterwards so later suites still see the same objects.
    orig_bot = bots_mod.bot
    orig_light = bots_mod.bot_light
    orig_notifier = bots_mod.admin_notifier

    monkeypatch.setenv("ADMIN_IDS", "4242")
    monkeypatch.delenv("ADMIN_ID", raising=False)
    importlib.reload(config_mod)  # re-derive OWNER_ID from the new env
    importlib.reload(bots_mod)
    try:
        assert config_mod.OWNER_ID == 4242
        assert bots_mod.admin_notifier.admin_id == 4242
    finally:
        monkeypatch.undo()
        importlib.reload(config_mod)
        bots_mod.bot = orig_bot
        bots_mod.bot_light = orig_light
        bots_mod.admin_notifier = orig_notifier


def test_scheduled_daily_backup_targets_owner(monkeypatch, tmp_path):
    """send_daily_backup(None) (the scheduler path) must fall back to OWNER_ID
    — previously legacy ADMIN_ID (0) made the run skip with 'no admin target'."""
    import services.backup as backup_mod

    monkeypatch.setattr(backup_mod, "OWNER_ID", 777, raising=False)
    monkeypatch.setattr(backup_mod, "ADMIN_ID", 0, raising=False)
    archive = tmp_path / "chat_history_test.db.gz"
    archive.write_bytes(b"gz")
    monkeypatch.setattr(backup_mod, "build_backup_archive", lambda d: (str(archive), 2))
    sent = {}

    class _FakeBackupBot:
        def send_document(self, target, fh, **kwargs):
            sent["target"] = target
            return _ns(message_id=1)

    monkeypatch.setattr(backup_mod, "bot", _FakeBackupBot())
    assert backup_mod.send_daily_backup(None) is True
    assert sent["target"] == 777


# =========================
# Fix 3: broadcast confirm must not leak _broadcast_lock
# =========================
def test_broadcast_confirm_progress_failure_releases_lock(monkeypatch):
    """If the '📡 Starting…' progress send fails, the confirm must restore the
    draft, close the broadcast row, and RELEASE the lock — previously the
    lock stayed held until restart, blocking every future broadcast."""
    import handlers.panel as panel

    monkeypatch.setattr(shared, "OWNER_ID", 1, raising=False)
    monkeypatch.setattr(shared, "ADMIN_IDS", (1,), raising=False)
    monkeypatch.setattr(config_mod, "OWNER_ID", 1, raising=False)

    class _FailProgressBot(_PanelFakeBotShim):
        def send_message(self, chat_id, text, **kwargs):
            raise RuntimeError("progress send exploded")

    fb = _FailProgressBot()
    monkeypatch.setattr(panel, "bot", fb)

    token = "progressfail1"
    draft = {"owner": 1, "kind": "users", "label": "test", "recipients": [301],
             "skipped_banned": 0, "source_chat": 100, "source_msg": 50,
             "content_type": "text"}
    panel._panel_broadcast_drafts.set(token, draft)
    try:
        panel.handle_panel_broadcast_confirm(_pcall(1, f"pnl_bc_ok_{token}"))
        deadline = time.time() + 5
        while panel._broadcast_lock.locked() and time.time() < deadline:
            time.sleep(0.02)
        assert not panel._broadcast_lock.locked(), "lock must be released on progress-send failure"
        assert panel._broadcast_run == {}, "run registry must be cleared"
        assert token in panel._panel_broadcast_drafts._data, "draft must be restored for retry"
        rows = repo.list_broadcasts(limit=1)
        assert rows and rows[0][9] != "running", "broadcast row must not stay 'running'"
    finally:
        monkeypatch.undo()
        for tok in list(panel._panel_broadcast_drafts._data):
            panel._panel_broadcast_drafts.discard(tok)
        if panel._broadcast_lock.locked():
            try:
                panel._broadcast_lock.release()
            except RuntimeError:
                pass
        with repo._write_lock:
            conn = repo._connect()
            conn.execute("DELETE FROM panel_broadcasts")
            conn.execute("DELETE FROM panel_audit WHERE action LIKE 'broadcast%'")
            conn.commit()


class _PanelFakeBotShim:
    """Minimal recorder matching test_panel._PanelFakeBot surface."""

    def __init__(self):
        self.acked = []
        self.edits = []

    def answer_callback_query(self, qid, text=None, **kwargs):
        self.acked.append((qid, text))

    def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        self.edits.append((chat_id, message_id, text, kwargs))

    def edit_message_reply_markup(self, chat_id=None, message_id=None, reply_markup=None, **kwargs):
        return True


# =========================
# Fix 4: abort cleanup deletes the aborted turn's OWN row
# =========================
def test_save_message_returns_rowid_and_delete_by_id_is_surgical():
    """save_message must return the new row id, and delete_message_by_id must
    remove exactly that row — even when a newer user row (another group
    member's turn) landed in the same chat scope afterwards."""
    rid1 = repo.save_message("tst-fix-rowid", "user", "first ask")
    rid2 = repo.save_message("tst-fix-rowid", "user", "second user ask")
    try:
        assert isinstance(rid1, int) and rid1 > 0
        assert isinstance(rid2, int) and rid2 > rid1
        assert repo.delete_message_by_id(rid1) is True
        history = [h["content"] for h in repo.get_history("tst-fix-rowid")]
        assert history == ["second user ask"], "the newer row must survive"
        assert repo.delete_message_by_id(99999999) is False
    finally:
        repo.clear_chat("tst-fix-rowid")


def test_save_memory_guarded_returns_rowid_or_none():
    rid = ps.save_memory_guarded("tst-fix-guarded", "user", "hello there", 424242, time.time())
    try:
        # type() is deliberate: bool subclasses int, and the pre-fix code
        # returned True — which must not count as a row id.
        assert type(rid) is int and rid > 0
    finally:
        repo.clear_chat("tst-fix-guarded")


def test_save_memory_guarded_tombstone_returns_none(monkeypatch):
    ps.note_reset(424243)
    rid = ps.save_memory_guarded("tst-fix-tomb", "user", "post-reset", 424243, time.time() - 10)
    try:
        assert rid is None
        assert repo.count_messages("tst-fix-tomb") == 0
    finally:
        repo.clear_chat("tst-fix-tomb")


# =========================
# Fix 5: digest Markdown parse failure falls back to plain text
# =========================
class _ParseFailBot:
    """send_message fails ONLY for Markdown parse_mode with a 400
    can't-parse-entities error, like an unbalanced LLM output would."""

    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, **kwargs):
        if kwargs.get("parse_mode") == "Markdown":
            raise ApiTelegramException("sendMessage", None, {
                "ok": False, "error_code": 400,
                "description": "Bad Request: can't parse entities: "
                               "Character '*' is reserved and must be escaped"})
        self.sent.append((chat_id, text))
        return _ns(message_id=1)


def test_digest_single_send_falls_back_to_plain_on_parse_error():
    import services.digest as digest_mod

    fb = _ParseFailBot()
    orig_bot = digest_mod.bot
    digest_mod.bot = fb
    try:
        outcome, detail = digest_mod._send_digest_one(-999001, "heading", "- خط خبری")
    finally:
        digest_mod.bot = orig_bot
    assert outcome == "sent", f"parse failure must fall back to plain text, got {outcome}: {detail}"
    assert detail == ""
    assert fb.sent and fb.sent[0][0] == -999001


def test_digest_profile_chunks_fall_back_to_plain_on_parse_error():
    import services.digest as digest_mod

    fb = _ParseFailBot()
    orig_bot = digest_mod.bot
    digest_mod.bot = fb
    try:
        outcome, detail = digest_mod._send_profile_chunks(-999002, "heading", "- خط خبری")
    finally:
        digest_mod.bot = orig_bot
    assert outcome == "sent", f"chunked profile send must fall back to plain text, got {outcome}: {detail}"
    assert fb.sent and fb.sent[0][0] == -999002


# =========================
# Fix 6: /pic caption backticks are sanitized
# =========================
def test_pic_caption_sanitizes_backticks(monkeypatch):
    import handlers.basics as basics

    class _PicBot:
        def __init__(self):
            self.captions = []

        def send_chat_action(self, *a, **k):
            return True

        def reply_to(self, message, text, **kwargs):
            return _ns(message_id=5)

        def edit_message_text(self, *a, **k):
            return True

        def send_photo(self, chat_id, photo, reply_to_message_id=None,
                       caption=None, parse_mode=None, **kwargs):
            self.captions.append(caption)
            return _ns(message_id=6)

    class _PicHTTP:
        @staticmethod
        def get(url, timeout=None):
            return _ns(status_code=200, content=b"\x89PNG fakebytes")

    class _PicAgnes:
        @staticmethod
        def generate_image(prompt):
            return _ns(status_code=200, text="ok",
                       json=lambda: {"data": [{"url": "https://img.example/x.jpg"}]})

    fb = _PicBot()
    monkeypatch.setattr(basics, "bot", fb)
    monkeypatch.setattr(basics, "agnes", _PicAgnes())
    monkeypatch.setattr(basics, "HTTP", _PicHTTP())
    monkeypatch.setattr(basics.ps, "feature_gate_message", lambda *a, **k: True)

    msg = _ns(from_user=_user(424244), chat=_ns(type="private", id=424244),
              text="/pic use `code` here", message_id=9, content_type="text")
    basics.handle_image_generation(msg)
    assert fb.captions, "photo with caption expected"
    assert fb.captions[0].count("`") == 2, (
        "only the wrapping code-span backticks may remain — raw user backticks "
        "must be sanitized or Telegram rejects the caption"
    )
