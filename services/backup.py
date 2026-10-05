import datetime
import gzip
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import traceback

from bots import admin_notifier, bot
from config import (
    ADMIN_ID,
    BACKUP_HOUR_IRAN,
    BACKUP_KEEP_DAILY,
    DATA_DIR,
    DB_FILE,
    DB_RETENTION_DAYS,
    DB_SIZE_WARN_MB,
)
from db import repo
from metrics import METRICS
from timeutil import iran_now

log = logging.getLogger("L-bot")


def _backup_dir():
    path = os.path.join(DATA_DIR, "backups")
    os.makedirs(path, exist_ok=True)
    return path


def _prune_backup_snapshots(backup_dir=None, keep=None):
    """Keep only the newest `keep` snapshot archives. Never raises."""
    try:
        backup_dir = backup_dir or _backup_dir()
        keep = BACKUP_KEEP_DAILY if keep is None else int(keep)
        snaps = sorted(
            f for f in os.listdir(backup_dir)
            if f.startswith("chat_history_") and f.endswith(".db.gz")
        )
        for stale in snaps[: max(0, len(snaps) - max(0, keep))]:
            try:
                os.remove(os.path.join(backup_dir, stale))
            except OSError:
                pass
    except Exception:
        log.warning("Backup snapshot pruning failed", exc_info=True)


def _run_daily_maintenance():
    """Prune old history, reclaim space, and warn on DB size. Runs from the
    daily backup job BEFORE the snapshot, so backups stay small too. Returns
    a small report dict; never raises."""
    report = {"purged": 0, "vacuumed": False, "db_size_bytes": 0, "size_ok": True}
    try:
        report["purged"] = repo.purge_old_messages(DB_RETENTION_DAYS)
        if report["purged"]:
            log.info("Retention: purged %d messages older than %d days.",
                     report["purged"], DB_RETENTION_DAYS)
    except Exception:
        log.warning("Retention purge failed", exc_info=True)
    try:
        report["vacuumed"] = repo.maybe_vacuum()
        if report["vacuumed"]:
            log.info("VACUUM completed (weekly space reclaim).")
    except Exception:
        log.warning("VACUUM failed", exc_info=True)
    try:
        report["db_size_bytes"] = os.path.getsize(DB_FILE)
        limit = int(DB_SIZE_WARN_MB) * 1024 * 1024
        if limit > 0 and report["db_size_bytes"] > limit:
            report["size_ok"] = False
            log.warning("DB size %.1f MB exceeds warn threshold %d MB.",
                        report["db_size_bytes"] / 1024 / 1024, DB_SIZE_WARN_MB)
            admin_notifier.notify(
                "db_size_warning",
                f"chat_history.db is {report['db_size_bytes'] / 1024 / 1024:.1f} MB "
                f"(warn at {DB_SIZE_WARN_MB} MB). Consider lowering "
                f"DB_RETENTION_DAYS (now {DB_RETENTION_DAYS}).",
            )
    except Exception:
        log.warning("DB size check failed", exc_info=True)
    return report


# -------------------------
# Daily DB backup (gzipped snapshot -> admin DM)
# -------------------------
def iran_next_backup_epoch() -> float:
    """Epoch for the next BACKUP_HOUR_IRAN:00 Iran time (today if still ahead)."""
    now_iran = iran_now()
    target = now_iran.replace(hour=BACKUP_HOUR_IRAN, minute=0, second=0, microsecond=0)
    if target <= now_iran:
        target += datetime.timedelta(days=1)
    return target.timestamp()

def build_backup_archive(dest_dir):
    """Snapshot the SQLite DB with the backup API (safe against concurrent
    writers, unlike copying the file) and gzip it. Returns (path, size_bytes)."""
    os.makedirs(dest_dir, exist_ok=True)
    stamp = iran_now().strftime("%Y-%m-%d_%H%M")
    raw_path = os.path.join(dest_dir, f"chat_history_{stamp}.db")
    gz_path = raw_path + ".gz"

    src = sqlite3.connect(DB_FILE)
    try:
        dst = sqlite3.connect(raw_path)
        try:
            src.backup(dst)  # page-consistent snapshot, WAL-safe
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            dst.close()
    finally:
        src.close()

    with open(raw_path, "rb") as fin, gzip.open(gz_path, "wb", compresslevel=9) as fout:
        shutil.copyfileobj(fin, fout)
    os.remove(raw_path)
    return gz_path, os.path.getsize(gz_path)

def send_daily_backup(admin_id=None):
    """Build the snapshot, rotate local copies, and send the newest to an
    admin's DM. Best-effort and heavily guarded — a backup problem must
    never take the bot down. `admin_id` targets the send (Stage 3B run-now
    sends to the requesting owner); None keeps the legacy ADMIN_ID target.
    NOTE: no retention purge or VACUUM here — those live in
    _run_daily_maintenance, which only the scheduled run calls."""
    target = admin_id or ADMIN_ID
    if not target:
        log.info("Daily backup skipped: no admin target.")
        return False
    try:
        path, size = build_backup_archive(_backup_dir())
    except Exception:
        log.error("Daily backup: snapshot failed", exc_info=True)
        admin_notifier.notify("backup_snapshot_failed", traceback.format_exc(limit=5))
        return False
    _prune_backup_snapshots(os.path.dirname(path))

    try:
        with open(path, "rb") as fh:
            bot.send_document(
                target, fh,
                visible_file_name=os.path.basename(path),
                caption=(
                    f"💾 بکاپ روزانه دیتابیس — {iran_now().strftime('%Y-%m-%d %H:%M')} "
                    f"(به وقت ایران)\n"
                    f"حجم فشرده: {size / 1024:.0f} KB"
                ),
            )
        METRICS["backups_sent"] = METRICS.get("backups_sent", 0) + 1
        log.info("Daily DB backup sent to admin (%.0f KB).", size / 1024)
        return True
    except Exception:
        log.error("Daily backup: send to admin failed", exc_info=True)
        admin_notifier.notify("backup_send_failed", traceback.format_exc(limit=5))
        return False


# Single-flight with the scheduled run: a double tap or an overlap never
# runs two backups at once (run-now takes it non-blocking and refuses).
_backup_lock = threading.Lock()


def run_backup_now(admin_id):
    """Owner run-now entry: snapshot + send ONLY (never purge/VACUUM).
    Synchronous — the panel runs it on a background thread. Returns
    (ok, message)."""
    from services.panel_jobs import job_mark_error, job_mark_start, job_mark_success

    if not _backup_lock.acquire(blocking=False):
        return False, "a backup is already running"
    job_mark_start("backup")
    try:
        ok = send_daily_backup(admin_id)
        if ok:
            job_mark_success("backup")
        return ok, ("backup sent" if ok else "backup failed — see logs")
    except Exception:
        job_mark_error("backup", traceback.format_exc(limit=3))
        return False, "backup crashed — see logs"
    finally:
        _backup_lock.release()

def _backup_scheduler_loop():
    """Backup counterpart of the digest loop: sleep until 04:00 Iran time,
    snapshot + send, reschedule. Never dies."""
    log.info("Backup scheduler thread started (target=%02d:00 Iran time).", BACKUP_HOUR_IRAN)
    while True:
        sleep_for = max(1.0, iran_next_backup_epoch() - time.time())
        time.sleep(sleep_for)
        try:
            from services.panel_jobs import (
                job_mark_error,
                job_mark_start,
                job_mark_success,
                job_paused_until,
            )

            job_mark_start("backup")
            paused, _label = job_paused_until("backup")
            while paused:
                time.sleep(60)
                paused, _label = job_paused_until("backup")
            with _backup_lock:
                _run_daily_maintenance()
                send_daily_backup()
            job_mark_success("backup")
        except Exception:
            log.error("Scheduled backup failed", exc_info=True)
            try:
                job_mark_error("backup", traceback.format_exc(limit=3))
            except Exception:
                pass
            admin_notifier.notify("backup_scheduler_error", traceback.format_exc(limit=5))
        time.sleep(60)  # small buffer so a fast loop can't double-fire
