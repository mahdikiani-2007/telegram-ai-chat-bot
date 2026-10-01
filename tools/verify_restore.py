"""Verify (and optionally restore) a gzipped SQLite snapshot.

Standalone stdlib-only script — works on any machine holding an archive,
no bot imports or credentials needed:

  python3 tools/verify_restore.py --archive chat_history_2026-10-01_0400.db.gz
  python3 tools/verify_restore.py --archive X.db.gz --restore /data/chat_history.db

Exit 0 = archive healthy (and restored, if asked). Anything else = DO NOT
deploy that archive. See RESTORE.md for the full procedure.
"""
import argparse
import gzip
import os
import shutil
import sqlite3
import sys
import tempfile

# Core tables a healthy backup must contain (subset on purpose: extra
# tables from future migrations must not fail verification).
CORE_TABLES = {
    "messages", "summaries", "todos", "reminders", "known_chats",
    "sessions", "user_facts",
}


def verify_archive(path):
    """Gunzip to temp, integrity-check, report tables/rows. Returns a
    report dict; raises on any problem."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"archive not found: {path}")
    report = {"archive": path, "size_bytes": os.path.getsize(path)}
    tmpdir = tempfile.mkdtemp(prefix="lbot_verify_")
    raw = os.path.join(tmpdir, "check.db")
    try:
        with gzip.open(path, "rb") as fin, open(raw, "wb") as fout:
            shutil.copyfileobj(fin, fout)
    except OSError as e:
        raise ValueError(f"not a valid gzip archive: {e}")
    try:
        con = sqlite3.connect(raw)
        try:
            ok = con.execute("PRAGMA integrity_check").fetchone()
            report["integrity"] = ok[0] if ok else "?"
            if report["integrity"] != "ok":
                raise ValueError(f"integrity_check failed: {report['integrity']}")
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            report["tables"] = sorted(tables)
            missing = CORE_TABLES - tables
            if missing:
                raise ValueError(f"core tables missing: {sorted(missing)}")
            report["rows"] = {
                t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                for t in sorted(tables) if not t.startswith("sqlite_")
            }
        finally:
            con.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return report


def restore_archive(archive, dest_db):
    """Verify, then place the archive as dest_db. Removes WAL sidecars so
    SQLite can't resurrect stale pages next to the restored file."""
    verify_archive(archive)
    with gzip.open(archive, "rb") as fin, open(dest_db, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            os.remove(dest_db + suffix)
        except OSError:
            pass
    # Re-verify the placed file itself, not just the archive.
    con = sqlite3.connect(dest_db)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()
    if ok != "ok":
        raise ValueError(f"restored file failed integrity_check: {ok}")
    return dest_db


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", required=True)
    ap.add_argument("--restore", default=None,
                    help="destination .db path (verifies first)")
    args = ap.parse_args()
    try:
        if args.restore:
            restore_archive(args.archive, args.restore)
            print(f"RESTORED {args.archive} -> {args.restore}")
        else:
            rep = verify_archive(args.archive)
            print(f"OK integrity=ok tables={len(rep['tables'])} "
                  f"messages={rep['rows'].get('messages', '?')} "
                  f"size={rep['size_bytes']} bytes")
    except Exception as e:
        print(f"VERIFY FAILED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
