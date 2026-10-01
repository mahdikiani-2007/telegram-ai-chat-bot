"""Tests for Phase 4.2: pending Telegram updates survive redeploys and
poller crash-restarts by default; DROP_PENDING_UPDATES=1 skips them."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wiring
from wiring import shutdown_event


@pytest.fixture
def clean_shutdown_event():
    shutdown_event.clear()
    yield
    shutdown_event.clear()


def test_default_preserves_queue():
    assert wiring.DROP_PENDING_UPDATES is False


def _fake_light(delete_calls):
    light = type("B", (), {})()
    light.delete_webhook = (
        lambda drop_pending_updates: delete_calls.append(drop_pending_updates))

    def fake_poll(*a, **k):
        shutdown_event.set()

    light.infinity_polling = fake_poll
    return light


def _stub_real_bot(monkeypatch, delete_calls):
    monkeypatch.setattr(wiring.bot, "delete_webhook",
                        lambda drop_pending_updates: delete_calls.append(drop_pending_updates))

    def fake_poll(*a, **k):
        shutdown_event.set()

    monkeypatch.setattr(wiring.bot, "infinity_polling", fake_poll)


def test_run_bot_l_preserves_queue(clean_shutdown_event, monkeypatch):
    calls = []
    _stub_real_bot(monkeypatch, calls)
    wiring.run_bot_l()  # must return after one poll, not loop
    assert calls == [False]


def test_run_bot_l_honors_drop_flag(clean_shutdown_event, monkeypatch):
    calls = []
    _stub_real_bot(monkeypatch, calls)
    monkeypatch.setattr(wiring, "DROP_PENDING_UPDATES", True)
    wiring.run_bot_l()
    assert calls == [True]


def test_run_bot_light_preserves_queue(clean_shutdown_event, monkeypatch):
    calls = []
    monkeypatch.setattr(wiring, "bot_light", _fake_light(calls))
    wiring.run_bot_light()
    assert calls == [False]


def test_run_bot_light_honors_drop_flag(clean_shutdown_event, monkeypatch):
    calls = []
    monkeypatch.setattr(wiring, "bot_light", _fake_light(calls))
    monkeypatch.setattr(wiring, "DROP_PENDING_UPDATES", True)
    wiring.run_bot_light()
    assert calls == [True]


def test_ensure_polling_mode_passes_flag(monkeypatch):
    for flag, want in ((False, False), (True, True)):
        calls = []
        inst = type("B", (), {})()
        inst.delete_webhook = lambda drop_pending_updates: calls.append(drop_pending_updates)
        monkeypatch.setattr(wiring, "DROP_PENDING_UPDATES", flag)
        wiring._ensure_polling_mode(inst)
        assert calls == [want]
