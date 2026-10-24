"""Tests for Phase 4.1 graceful shutdown (signal handling, stoppable
polling loops, shutdown sequence, WAL checkpoint)."""
import os
import signal
import sqlite3
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wiring
from wiring import shutdown_event


@pytest.fixture
def clean_shutdown_event():
    shutdown_event.clear()
    yield
    shutdown_event.clear()


def test_request_shutdown_sets_event_once(clean_shutdown_event):
    wiring._request_shutdown(signal.SIGTERM, None)
    assert shutdown_event.is_set()


def test_second_signal_forces_exit(clean_shutdown_event):
    wiring._request_shutdown(signal.SIGTERM, None)
    with pytest.raises(SystemExit):
        wiring._request_shutdown(signal.SIGTERM, None)


def test_run_bot_l_exits_when_shutdown_requested(clean_shutdown_event, monkeypatch):
    calls = []
    monkeypatch.setattr(wiring.bot, "delete_webhook",
                        lambda *a, **k: calls.append("delete_webhook"))

    def fake_poll(*a, **k):
        calls.append("poll")
        shutdown_event.set()
        return None

    monkeypatch.setattr(wiring.bot, "infinity_polling", fake_poll)
    wiring.run_bot_l()  # must return, not loop forever
    assert calls[0] == "delete_webhook"
    assert calls.count("poll") == 1


def test_run_bot_light_no_token_returns_immediately(clean_shutdown_event, monkeypatch):
    monkeypatch.setattr(wiring, "bot_light", None)
    wiring.run_bot_light()  # must return at once


class _RecordingThread:
    """Duck-typed stand-in: join/is_alive/name without real threads."""

    def __init__(self, name, joined):
        self.name = name
        self._joined = joined

    def join(self, timeout=None):
        self._joined.append((self.name, timeout))

    def is_alive(self):
        return False


def _fake_thread(name, joined):
    return _RecordingThread(name, joined)


def test_shutdown_sequence_order_and_containment(clean_shutdown_event, monkeypatch):
    calls = []
    monkeypatch.setattr(wiring.bot, "stop_polling",
                        lambda: calls.append("stop_L"))

    def boom():
        calls.append("stop_Light")
        raise RuntimeError("light down")

    monkeypatch.setattr(wiring, "bot_light",
                        type("B", (), {"stop_polling": staticmethod(boom)})())
    monkeypatch.setattr(wiring.repo, "checkpoint_wal",
                        lambda: calls.append("checkpoint") or (0, 0, 0))
    joined = []
    threads = [_fake_thread("p1", joined), _fake_thread("p2", joined)]
    wiring._shutdown_sequence(threads, grace_seconds=0)
    # stop both bots before checkpoint; a raising stop must not skip the rest
    assert calls.index("stop_L") < calls.index("checkpoint")
    assert "stop_Light" in calls and "checkpoint" in calls
    assert [n for n, _ in joined] == ["p1", "p2"]


def test_shutdown_sequence_survives_checkpoint_failure(clean_shutdown_event, monkeypatch):
    monkeypatch.setattr(wiring.bot, "stop_polling", lambda: None)
    monkeypatch.setattr(wiring, "bot_light", None)

    def boom():
        raise RuntimeError("wal busy")

    monkeypatch.setattr(wiring.repo, "checkpoint_wal", boom)
    wiring._shutdown_sequence([], grace_seconds=0)  # must not raise


def test_checkpoint_wal_returns_triple():
    import db as db_mod
    repo = db_mod.repo
    repo.save_message("shutdown_test_chat", "user", "checkpoint probe")
    res = repo.checkpoint_wal()
    assert isinstance(res, tuple) and len(res) == 3
    assert all(isinstance(v, int) for v in res)
    repo.clear_chat("shutdown_test_chat")


def test_live_sigterm_probe():
    """End-to-end: stubbed polling + real signal handlers + real sequence.
    Asserts the process exits 0 promptly with the shutdown markers."""
    probe = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "tools", "shutdown_probe.py")
    proc = subprocess.run(
        [sys.executable, "-u", probe],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"probe failed:\n{proc.stdout}\n{proc.stderr}"
    for marker in ("probe: handlers installed",
                   "probe: polling entered",
                   "Shutdown signal received",
                   "polling stopped",
                   "Shutdown sequence complete",
                   "probe: clean exit"):
        assert marker in proc.stdout + proc.stderr, f"missing: {marker}"
