"""Tests for the data-safety phase: retention, VACUUM, rotation,
maintenance report, and archive build+verify roundtrip.

Shares the hermetic test DB (see test_smoke.py): unique chat ids per test
plus cleanup, since both modules run in one pytest process.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import db as db_mod
import verify_restore
from services import backup as backup_mod


def _repo():
    return db_mod.repo


def test_purge_old_messages():
    repo = _repo()
    chat = "backup_test_purge_chat"
    repo.clear_chat(chat)
    repo.save_message(chat, "user", "fresh message")
    repo.save_message(chat, "user", "old message 1")
    repo.save_message(chat, "user", "old message 2")
    repo._connect().execute(
        "UPDATE messages SET timestamp = datetime('now', '-100 days') "
        "WHERE chat_id = ? AND text LIKE 'old message%'", (chat,))
    repo._connect().commit()
    assert repo.count_messages(chat) == 3
    deleted = repo.purge_old_messages(keep_days=90)
    assert deleted == 2
    assert repo.count_messages(chat) == 1
    assert repo.purge_old_messages(keep_days=90) == 0  # idempotent
    repo.clear_chat(chat)


def test_maybe_vacuum_runs_once_per_interval():
    repo = _repo()
    assert repo.maybe_vacuum(interval_days=7) is True
    assert repo.maybe_vacuum(interval_days=7) is False
    assert repo.get_maintenance_value("last_vacuum") is not None


def test_prune_backup_snapshots_keeps_newest():
    tmp = tempfile.mkdtemp(prefix="lbot_prune_")
    for i in range(10):
        name = f"chat_history_2026-09-{i + 1:02d}_0400.db.gz"
        with open(os.path.join(tmp, name), "wb") as f:
            f.write(b"x")
    with open(os.path.join(tmp, "unrelated.txt"), "w") as f:
        f.write("keep me")
    backup_mod._prune_backup_snapshots(tmp, keep=7)
    remaining = sorted(os.listdir(tmp))
    assert len([f for f in remaining if f.endswith(".db.gz")]) == 7
    assert "chat_history_2026-09-10_0400.db.gz" in remaining  # newest kept
    assert "chat_history_2026-09-01_0400.db.gz" not in remaining
    assert "unrelated.txt" in remaining


def test_daily_maintenance_report():
    rep = backup_mod._run_daily_maintenance()
    assert set(rep) == {"purged", "vacuumed", "db_size_bytes", "size_ok"}
    assert rep["purged"] >= 0
    assert isinstance(rep["vacuumed"], bool)
    assert rep["db_size_bytes"] > 0
    assert rep["size_ok"] is True  # tiny test DB under the warn threshold


def test_build_and_verify_roundtrip():
    repo = _repo()
    chat = "backup_test_roundtrip_chat"
    repo.clear_chat(chat)
    repo.save_message(chat, "user", "roundtrip probe")
    tmp = tempfile.mkdtemp(prefix="lbot_roundtrip_")
    path, size = backup_mod.build_backup_archive(tmp)
    assert os.path.isfile(path) and size > 0
    rep = verify_restore.verify_archive(path)
    assert rep["integrity"] == "ok"
    assert "messages" in rep["tables"]
    assert rep["rows"]["messages"] >= 1
    restored = os.path.join(tmp, "restored.db")
    verify_restore.restore_archive(path, restored)
    import sqlite3
    con = sqlite3.connect(restored)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        n = con.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat,)).fetchone()[0]
        assert n == 1
    finally:
        con.close()
    repo.clear_chat(chat)
