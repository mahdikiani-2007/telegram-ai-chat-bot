"""Tests for Phase 4.3 crash-safe dispatch: DB claims, conditional
finalize, slot-anchored reschedule, error classification, persistent
retries, and the rewritten _fire paths (network stubbed throughout)."""
import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as db_mod
from services import scheduling as sched


def _repo():
    return db_mod.repo


@pytest.fixture
def stubbed_sender(monkeypatch):
    """Replace sched.bot/admin_notifier with recorders. Returns (sent, notified)."""
    sent, notified = [], []

    class Sender:
        def send_message(self, *a, **k):
            sent.append((a, k))

    class Notifier:
        def notify(self, *a, **k):
            notified.append((a, k))

    monkeypatch.setattr(sched, "bot", Sender())
    monkeypatch.setattr(sched, "admin_notifier", Notifier())
    return sent, notified


def _add_due_reminder(text, recurrence="none", overdue_by=10):
    repo = _repo()
    rid = repo.add_reminder(424243, 1, text, "me",
                            recurrence, time.time() - overdue_by)
    return rid


def _add_due_todo(text, recurrence="none", overdue_by=10, deadline=None):
    repo = _repo()
    tid = repo.add_todo(424243, 1, text, "Normal", "General",
                        deadline if deadline is not None else time.time() - overdue_by,
                        recurrence=recurrence)
    return tid


def test_claim_excludes_concurrent_and_refires_stale():
    repo = _repo()
    r1 = _add_due_reminder("claim-a")
    r2 = _add_due_reminder("claim-b")
    try:
        got_a = repo.claim_due_reminders(time.time(), "token-A")
        ids_a = {r[0] for r in got_a}
        assert {r1, r2} <= ids_a
        # second batch sees nothing (claimed, fresh)
        got_b = repo.claim_due_reminders(time.time(), "token-B")
        assert r1 not in {r[0] for r in got_b}
        assert r2 not in {r[0] for r in got_b}
        # stale claims refire
        repo._connect().execute(
            "UPDATE reminders SET claimed_at = ? WHERE id IN (?, ?)",
            (time.time() - 400, r1, r2))
        repo._connect().commit()
        got_c = repo.claim_due_reminders(time.time(), "token-C")
        assert {r1, r2} <= {r[0] for r in got_c}
    finally:
        repo.dismiss_reminder(r1)
        repo.dismiss_reminder(r2)


def test_conditional_reschedule_accepts_and_rejects():
    repo = _repo()
    rid = _add_due_reminder("cas-reminder")
    try:
        row = repo.get_reminder_by_id(rid)
        old_trigger = row[6]
        assert repo.reschedule_reminder_if_expected(rid, old_trigger, old_trigger + 60) == 1
        # stale expectation no longer matches
        assert repo.reschedule_reminder_if_expected(rid, old_trigger, old_trigger + 120) == 0
        assert repo.get_reminder_by_id(rid)[6] == old_trigger + 60
    finally:
        repo.dismiss_reminder(rid)


def test_conditional_todo_deadline_accepts_and_rejects():
    repo = _repo()
    tid = _add_due_todo("cas-todo")
    try:
        row = repo.get_todo_by_id(tid)
        old_deadline = row[7]
        assert repo.update_todo_deadline(tid, old_deadline, old_deadline + 60) == 1
        assert repo.update_todo_deadline(tid, old_deadline, old_deadline + 120) == 0
        assert repo.get_todo_by_id(tid)[7] == old_deadline + 60
    finally:
        repo.delete_todo(tid)


def test_next_trigger_anchored_to_slot():
    now = time.time()
    old = now - 100
    nxt = sched._next_trigger(old, 86400, now)
    assert nxt == old + 86400 and nxt > now
    # multi-day outage rolls whole slots forward to the next future one
    old2 = now - 3 * 86400 - 10
    nxt2 = sched._next_trigger(old2, 86400, now)
    assert nxt2 > now and (nxt2 - old2) % 86400 == 0
    # absurd staleness still terminates on a future slot
    nxt3 = sched._next_trigger(now - 10 * 86400, 60, now)
    assert nxt3 > now


def _api_error(code):
    from telebot.apihelper import ApiTelegramException
    return ApiTelegramException(
        "sendMessage", {"ok": False},
        {"ok": False, "error_code": code, "description": f"code {code}"})


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_classifier_permanent_codes(code):
    assert sched._classify_dispatch_error(_api_error(code)) == "permanent"


@pytest.mark.parametrize("code", [429, 500, 502, 503])
def test_classifier_transient_codes(code):
    assert sched._classify_dispatch_error(_api_error(code)) == "transient"


def test_classifier_transient_signals_and_safe_default():
    import requests
    assert sched._classify_dispatch_error(requests.exceptions.ConnectionError("boom")) == "transient"
    assert sched._classify_dispatch_error(TimeoutError("timed out")) == "transient"
    assert sched._classify_dispatch_error(ValueError("weird")) == "transient"
    # polling-conflict duplicates must never kill reminders
    assert sched._classify_dispatch_error(_api_error(409)) == "transient"


def test_fire_onetime_success_dismisses(stubbed_sender):
    sent, _ = stubbed_sender
    repo = _repo()
    rid = _add_due_reminder("fire-onetime")
    row = repo.get_reminder_by_id(rid)
    sched._fire_reminder(rid, row[6])
    assert len(sent) == 1
    assert repo.get_reminder_by_id(rid) is None


def test_fire_recurring_rolls_from_missed_slot(stubbed_sender):
    sent, _ = stubbed_sender
    repo = _repo()
    old = time.time() - 100
    rid = repo.add_reminder(424243, 1, "fire-daily", "me", "daily", old)
    sched._fire_reminder(rid, old)
    assert len(sent) == 1
    new_trigger = repo.get_reminder_by_id(rid)[6]
    assert new_trigger == old + 86400  # anchored, not now()+86400
    repo.dismiss_reminder(rid)


def test_fire_transient_releases_without_counting(stubbed_sender, monkeypatch):
    sent, _ = stubbed_sender
    repo = _repo()
    rid = _add_due_reminder("fire-transient")
    row = repo.get_reminder_by_id(rid)
    try:
        import requests

        calls = []

        def flaky_send(*a, **k):
            calls.append(1)
            raise requests.exceptions.ConnectionError("down")

        monkeypatch.setattr(sched.bot, "send_message", flaky_send)
        sched._fire_reminder(rid, row[6])
        assert len(calls) == 1 and len(sent) == 0
        state = repo._connect().execute(
            "SELECT trigger_time, attempts, claimed_at FROM reminders WHERE id = ?",
            (rid,)).fetchone()
        assert state is not None
        assert state[0] == row[6]  # trigger untouched
        assert state[1] == 0  # uncounted
        assert state[2] is None  # claim released for next cycle
    finally:
        repo.dismiss_reminder(rid)


def test_fire_permanent_force_dismisses_at_max(stubbed_sender, monkeypatch):
    sent, notified = stubbed_sender
    repo = _repo()
    monkeypatch.setattr(sched, "MAX_DISPATCH_RETRIES", 2)
    rid = _add_due_reminder("fire-permanent")
    row = repo.get_reminder_by_id(rid)
    try:
        def blocked(*a, **k):
            raise _api_error(403)

        monkeypatch.setattr(sched.bot, "send_message", blocked)
        sched._fire_reminder(rid, row[6])
        assert repo.get_reminder_by_id(rid) is not None  # attempt 1: still pending
        sched._fire_reminder(rid, repo.get_reminder_by_id(rid)[6])
        assert repo.get_reminder_by_id(rid) is None  # attempt 2: dismissed
        assert notified and notified[0][0][0] == "reminder_dispatch_failed"
        assert len(sent) == 0
    finally:
        if repo.get_reminder_by_id(rid) is not None:
            repo.dismiss_reminder(rid)


def test_fire_skips_row_deleted_midflight(stubbed_sender):
    sent, _ = stubbed_sender
    repo = _repo()
    rid = _add_due_reminder("fire-deleted")
    row = repo.get_reminder_by_id(rid)
    repo.dismiss_reminder(rid)  # user dismissed between claim and fire
    sched._fire_reminder(rid, row[6])  # must not raise, must not send
    assert len(sent) == 0


def test_fire_todo_expiry_marks_expired(stubbed_sender):
    sent, _ = stubbed_sender
    repo = _repo()
    tid = _add_due_todo("fire-todo")
    row = repo.get_todo_by_id(tid)
    try:
        sched._fire_todo_expiry(tid, row[7])
        assert len(sent) == 1
        assert repo.get_todo_by_id(tid)[6] == 2  # status expired
    finally:
        repo.delete_todo(tid)


def test_todo_claim_excludes_concurrent():
    repo = _repo()
    t1 = _add_due_todo("claim-t1")
    try:
        got = repo.claim_due_todos(time.time(), "tok-1")
        assert t1 in {r[0] for r in got}
        assert t1 not in {r[0] for r in repo.claim_due_todos(time.time(), "tok-2")}
    finally:
        repo.delete_todo(t1)


def test_crash_between_send_and_finalize_refires_cleanly(stubbed_sender, monkeypatch):
    """Kill lands after a confirmed send but before finalize: the row must
    refire once stale, and the trigger must advance exactly once (the
    duplicate send itself is irreducible — Telegram has no idempotent send)."""
    sent, _ = stubbed_sender
    repo = _repo()
    old = time.time() - 100
    rid = repo.add_reminder(424243, 1, "crash-sim", "me", "daily", old)
    try:
        real_resched = repo.reschedule_reminder_if_expected
        calls = {"n": 0}

        def boom_once(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("simulated crash")
            return real_resched(*a, **k)

        monkeypatch.setattr(repo, "reschedule_reminder_if_expected", boom_once)
        claimed = repo.claim_due_reminders(time.time(), "tok-claim")
        assert rid in {r[0] for r in claimed}
        sched._fire_reminder(rid, old)
        assert len(sent) == 1
        assert repo.get_reminder_by_id(rid)[6] == old  # not advanced
        # held claim: immediate reclaim finds nothing...
        assert rid not in {r[0] for r in repo.claim_due_reminders(time.time(), "tok-x")}
        # ...but goes stale and refires, finalizing exactly once
        repo._connect().execute(
            "UPDATE reminders SET claimed_at = ? WHERE id = ?",
            (time.time() - 400, rid))
        repo._connect().commit()
        got = repo.claim_due_reminders(time.time(), "tok-y")
        assert rid in {r[0] for r in got}
        sched._fire_reminder(rid, old)
        assert len(sent) == 2
        assert repo.get_reminder_by_id(rid)[6] == old + 86400
    finally:
        repo.dismiss_reminder(rid)


def test_snooze_clears_claim():
    repo = _repo()
    rid = _add_due_reminder("snooze-claim")
    try:
        claimed = repo.claim_due_reminders(time.time(), "tok-s")
        assert rid in {r[0] for r in claimed}
        repo.snooze_reminder(rid, time.time() + 3600)
        row = repo._connect().execute(
            "SELECT claimed_at FROM reminders WHERE id = ?", (rid,)).fetchone()
        assert row[0] is None
    finally:
        repo.dismiss_reminder(rid)


def test_pending_reminders_list_and_delete_authorization():
    repo = _repo()
    rid = _add_due_reminder("pending-a")
    try:
        pending = {r[0]: r for r in repo.get_pending_reminders(424243)}
        assert rid in pending
        assert pending[rid][2] == 1  # creator_id
        assert "pending-a" in pending[rid][3]  # text

        from handlers import tasks as tasks_mod

        class _User:
            def __init__(self, uid):
                self.id = uid

        class _Chat:
            type = "private"
            id = 424243

        class _Msg:
            chat = _Chat()

        class _Call:
            def __init__(self, uid):
                self.message = _Msg()
                self.from_user = _User(uid)

        # creator may delete
        assert tasks_mod._user_is_authorized_for_reminder_delete(pending[rid], _Call(1))[0]
        # a different private-chat user may not
        ok, reason = tasks_mod._user_is_authorized_for_reminder_delete(pending[rid], _Call(999))
        assert not ok and "سازنده" in reason
    finally:
        repo.dismiss_reminder(rid)
    assert repo.get_reminder_by_id(rid) is None
