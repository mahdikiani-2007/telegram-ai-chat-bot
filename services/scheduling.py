import logging
import re
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

from telebot import types

from bots import admin_notifier, bot
from config import MAX_DISPATCH_RETRIES, SCHEDULER_MAX_WORKERS
from db import _build_all_members_mention, repo
from metrics import METRICS
from services.streaming import escape_markdown
from timeutil import _parse_custom_interval_seconds

log = logging.getLogger("L-bot")


# =========================
# Background Reminder + Todo Scheduler (ThreadPoolExecutor dispatch)
# =========================
_scheduler_executor = ThreadPoolExecutor(max_workers=SCHEDULER_MAX_WORKERS, thread_name_prefix="sched-dispatch")

# Dispatch is crash-safe via DB claims (see db.claim_due_*): a batch owns
# its rows through claim_token, so overlapping cycles can't double-submit
# and a restart refires only stale claims. Finalizes are conditional on the
# trigger value the dispatcher saw, so a refire after a crash between send
# and finalize can't double-advance state (the duplicate send itself is
# irreducible — Telegram has no idempotent send).
_scheduler_executor = ThreadPoolExecutor(max_workers=SCHEDULER_MAX_WORKERS, thread_name_prefix="sched-dispatch")

# Claims older than this are treated as died mid-flight and refire.
CLAIM_STALE_SECONDS = 300

# Recurrence roll-forward cap: 2880 daily slots (~8 years) bounds the loop
# for absurdly stale triggers while always landing on a future slot.
_MAX_RECURRENCE_ROLLS = 2880

_TRANSIENT_RE = re.compile(
    r"429|too many|flood|timeout|timed out|timed-out|connection|reset by peer"
    r"|eof|temporary|502|503|504|internal error|bad gateway|unavailable",
    re.IGNORECASE,
)

_PERMANENT_CODES = frozenset((400, 401, 403, 404))


def _classify_dispatch_error(exc):
    """Permanent (count toward force-dismiss) vs transient (release the
    claim, retry next cycle). Unknown errors default to transient — retrying
    is always safer than wrongly killing a user's reminder."""
    code = getattr(exc, "error_code", None)
    if code is None:
        result = getattr(exc, "result", None)
        if isinstance(result, dict):
            code = result.get("error_code")
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    if code == 429 or (code is not None and 500 <= code <= 599):
        return "transient"
    if code in _PERMANENT_CODES:
        return "permanent"
    if _TRANSIENT_RE.search(str(exc)):
        return "transient"
    return "transient"


def _next_trigger(old_trigger, interval_sec, now):
    """Roll the missed slot forward by whole intervals to the first future
    slot (slot-anchored: repeated crashes can't drift the schedule)."""
    nxt = float(old_trigger)
    for _ in range(_MAX_RECURRENCE_ROLLS):
        nxt += float(interval_sec)
        if nxt > now:
            return nxt
    return now + float(interval_sec)

def _build_reminder_alarm(rid, chat_id, creator_id, text, mention_type, assignee_username):
    if assignee_username:
        target_str = f"👥 @{assignee_username}"
    elif mention_type == "all":
        target_str = f"📢 {_build_all_members_mention(str(chat_id))}"
    else:
        target_str = f"👤 [کلیک کنید](tg://user?id={creator_id})"

    alarm_text = f"🔔 **یادآوری!**\n\n📝 {escape_markdown(text)}\n\n{target_str}"

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("💤 Snooze 15m", callback_data=f"remind_snooze_{rid}"),
        types.InlineKeyboardButton("✅ Dismiss", callback_data=f"remind_dismiss_{rid}"),
    )
    return alarm_text, markup

def _fire_reminder(rid, expected_trigger):
    """Dispatched on the scheduler thread pool. Claim-token flow: re-read
    the fresh row (dismissed/snoozed meanwhile -> release + skip), send,
    then conditionally finalize on the trigger value seen here. A crash
    between send and finalize refires at most a duplicate send — state can
    never double-advance."""
    fresh = repo.get_reminder_by_id(rid)
    if fresh is None:
        return  # dismissed/deleted after claiming
    (fid, fchat, fcreator, ftext, fmention, frec,
     ftrigger, fstatus, fassignee, fassuser) = fresh
    if fstatus != 0 or float(ftrigger) != float(expected_trigger):
        repo.release_reminder_claim(rid)
        return  # snoozed/rescheduled/finalized concurrently
    try:
        alarm_text, markup = _build_reminder_alarm(rid, fchat, fcreator, ftext, fmention, fassuser)
        bot.send_message(int(fchat), alarm_text, parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        if _classify_dispatch_error(e) == "permanent":
            attempts = repo.note_reminder_failure(rid)
            log.warning("Reminder %d dispatch failed (attempt %d/%d): %s", rid, attempts, MAX_DISPATCH_RETRIES, e)
            if attempts >= MAX_DISPATCH_RETRIES:
                log.error("Reminder %d exceeded max dispatch retries; force-dismissing", rid)
                try:
                    repo.dismiss_reminder(rid)
                except Exception:
                    pass
                admin_notifier.notify(
                    "reminder_dispatch_failed",
                    f"Reminder {rid} dropped after {MAX_DISPATCH_RETRIES} failed attempts.\n{e}",
                )
        else:
            repo.release_reminder_claim(rid)
            log.warning("Reminder %d transient dispatch failure; claim released for retry: %s", rid, e)
        return
    log.info("Reminder %d fired for chat %s", rid, fchat)

    try:
        if frec == "daily":
            interval_sec = 86400
        elif frec == "weekly":
            interval_sec = 604800
        elif frec and frec != "none":
            interval_sec = _parse_custom_interval_seconds(frec)
            if not interval_sec or interval_sec < 60:
                log.warning("Reminder %d has unparseable recurrence '%s'; dismissing", rid, frec)
                repo.dismiss_reminder(rid)
                return
        else:
            repo.dismiss_reminder(rid)
            return
        claimed = repo.reschedule_reminder_if_expected(rid, ftrigger, _next_trigger(ftrigger, interval_sec, time.time()))
        if claimed:
            log.info("Reminder %d rescheduled with recurrence '%s'", rid, frec)
        else:
            log.info("Reminder %d resolved concurrently; skipping finalize", rid)
    except Exception:
        # DB error after a confirmed send: claim stays held so the row
        # refires once it goes stale (noisy-future avoidance; the send
        # itself is never retried blindly here).
        log.warning("Reminder %d finalize failed; will refire when claim goes stale", rid, exc_info=True)

def _fire_todo_expiry(tid, expected_deadline):
    """Claim-token flow mirroring _fire_reminder: fresh re-read, conditional
    roll-forward from the missed slot, permanent-only retry counting."""
    fresh = repo.get_todo_by_id(tid)
    if fresh is None:
        return  # deleted after claiming
    (fid, fchat, fcreator, ftext, fpriority, fcategory, fstatus,
     fdeadline, frec, fassignee, fassuser) = fresh
    if fstatus != 0 or fdeadline is None or float(fdeadline) != float(expected_deadline):
        repo.release_todo_claim(tid)
        return  # done/edited/finalized concurrently
    try:
        assignee_str = f"\n👥 مسئول: @{fassuser}" if fassuser else ""
        alert_text = (
            f"⚠️ **Deadline Expired!**\n"
            f"The deadline for the following task has ended:\n\n"
            f"📌 {escape_markdown(ftext)}{assignee_str}\n\n"
            f"Created by: [User](tg://user?id={fcreator})"
        )
        bot.send_message(int(fchat), alert_text, parse_mode="Markdown")
    except Exception as e:
        if _classify_dispatch_error(e) == "permanent":
            attempts = repo.note_todo_failure(tid)
            log.warning("Todo %d expiry dispatch failed (attempt %d/%d): %s", tid, attempts, MAX_DISPATCH_RETRIES, e)
            if attempts >= MAX_DISPATCH_RETRIES:
                log.error("Todo %d expiry exceeded max dispatch retries; force-marking expired", tid)
                try:
                    repo.mark_todo_expired(tid)
                except Exception:
                    pass
                admin_notifier.notify(
                    "todo_dispatch_failed",
                    f"Todo {tid} expiry alert dropped after {MAX_DISPATCH_RETRIES} failed attempts.\n{e}",
                )
        else:
            repo.release_todo_claim(tid)
            log.warning("Todo %d transient expiry failure; claim released for retry: %s", tid, e)
        return
    log.info("Todo %d deadline expired for chat %s", tid, fchat)

    try:
        if frec == "daily":
            interval_sec = 86400
        elif frec == "weekly":
            interval_sec = 604800
        elif frec and frec != "none":
            interval_sec = _parse_custom_interval_seconds(frec)
            if not interval_sec or interval_sec < 60:
                repo.mark_todo_expired(tid)
                return
        else:
            repo.mark_todo_expired(tid)
            return
        claimed = repo.update_todo_deadline(tid, fdeadline, _next_trigger(fdeadline, interval_sec, time.time()))
        if not claimed:
            log.info("Todo %d resolved concurrently; skipping finalize", tid)
    except Exception:
        log.warning("Todo %d finalize failed; will refire when claim goes stale", tid, exc_info=True)

def _reminder_scheduler_loop():
    """Background daemon thread that polls for due reminders AND expired
    todo deadlines every 15 seconds, dispatching each one onto a bounded
    ThreadPoolExecutor so a single slow/blocked chat can't stall the whole
    queue (previously a sequential for-loop)."""
    log.info("Reminder & Todo scheduler thread started (pool size=%d).", SCHEDULER_MAX_WORKERS)

    # Restart catch-up (#1): anything that became due while the process was
    # down/redeploying fires now, marked as delayed — a late reminder beats
    # a silently dropped one. Claims from before the kill are all stale, so
    # they refire through the same path (at most a duplicate send on the
    # narrow crash-between-send-and-finalize window).
    try:
        token = uuid.uuid4().hex
        missed = repo.claim_due_reminders(time.time(), token, CLAIM_STALE_SECONDS)
        for rid, expected in missed:
            _scheduler_executor.submit(_fire_reminder, rid, expected)
        if missed:
            log.info("Scheduler catch-up: firing %d reminder(s) that came due while offline.", len(missed))
        missed_todos = repo.claim_due_todos(time.time(), token, CLAIM_STALE_SECONDS)
        for tid, expected in missed_todos:
            _scheduler_executor.submit(_fire_todo_expiry, tid, expected)
        if missed_todos:
            log.info("Scheduler catch-up: firing %d expired todo alert(s) from downtime.", len(missed_todos))
    except Exception:
        log.error("Scheduler catch-up pass failed", exc_info=True)

    while True:
        try:
            token = uuid.uuid4().hex
            for rid, expected in repo.claim_due_reminders(time.time(), token, CLAIM_STALE_SECONDS):
                _scheduler_executor.submit(_fire_reminder, rid, expected)
        except Exception:
            log.error("Reminder polling error", exc_info=True)
            admin_notifier.notify("scheduler_reminder_poll", traceback.format_exc(limit=5))

        try:
            token = uuid.uuid4().hex
            for tid, expected in repo.claim_due_todos(time.time(), token, CLAIM_STALE_SECONDS):
                _scheduler_executor.submit(_fire_todo_expiry, tid, expected)
        except Exception:
            log.error("Todo deadline polling error", exc_info=True)
            admin_notifier.notify("scheduler_todo_poll", traceback.format_exc(limit=5))

        time.sleep(15)
