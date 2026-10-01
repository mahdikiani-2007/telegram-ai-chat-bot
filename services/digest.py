import logging
import time
import traceback

from ai import agnes
from bots import admin_notifier, bot
from config import DIGEST_HOUR_IRAN, DIGEST_MINUTE_IRAN, TAVILY_API_KEY
from db import fetch_all_group_ids, repo
from metrics import METRICS
from services.search import tavily_budget, tavily_web_search
from timeutil import format_trigger_time_iran, iran_next_digest_epoch, iran_now

log = logging.getLogger("L-bot")


# =========================
# Daily News Digest — two sections (Iran/world news, tech & AI news),
# pushed to every known group at DIGEST_HOUR_IRAN:DIGEST_MINUTE_IRAN, or
# on-demand via /digest (admin, private chat only).
# =========================
DIGEST_QUERIES = [
    ("۱. اخبار ایران و جهان", "مهم‌ترین اخبار امروز ایران و مهم‌ترین خبر بزرگ جهان"),
    ("۲. تکنولوژی و هوش مصنوعی", "مهم‌ترین اخبار بزرگ امروز حوزه تکنولوژی و هوش مصنوعی"),
]

DIGEST_WRITEUP_PROMPT = (
    "You are writing a concise Persian daily news digest for a Telegram "
    "channel from raw web search snippets. You will get two labeled blocks "
    "of search results. For EACH block, write a short section: a bold "
    "section title (exactly as given, in **double asterisks**), followed by "
    "3 to 5 short bullet points (each starting with \"- \"), each 1 "
    "sentence, punchy and concrete — no fluff, no \"در ادامه بخوانید\", no "
    "repeating the query, no source links. If a block has no usable news, "
    "write \"- خبر مهمی یافت نشد.\" for that section instead of inventing "
    "anything. Output ONLY the two sections in order, nothing else — no "
    "intro line, no closing remark, no extra headers beyond the two given."
)

# Restart-safety: remember the last day a digest was actually broadcast so a
# redeploy around DIGEST_HOUR_IRAN doesn't skip (or double-send) the run.
def _get_last_digest_sent_date():
    try:
        row = repo._connect().execute(
            "SELECT digest_date FROM digest_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None

def _digest_due_today():
    """True if the scheduled digest hasn't been sent today (Iran date) yet."""
    today = format_trigger_time_iran(time.time()).split(" ")[0]
    return _get_last_digest_sent_date() != today

DIGEST_REPEAT_GUARD = (
    "\n\nIMPORTANT — DE-DUPLICATION: below is the list of news lines already "
    "reported in the last {days} days. Read it first, then check every "
    "candidate story in the search results against it. SKIP any story that "
    "is the same event, or substantially the same story, as anything on that "
    "list — same story re-reported a day later, minor updates to an "
    "already-covered event, follow-ups with no genuinely new development. "
    "Only write lines about news that has NOT been reported yet. If after "
    "filtering nothing new remains for a section, write \"- خبر مهمی یافت نشد.\" "
    "for that section. Do not mention this list or the filtering in the "
    "output.\n\nPreviously reported lines (do NOT repeat these):\n"
)

def _fetch_digest_section(query):
    """Search + budget-check for one digest section. Returns '' on failure
    or if the daily search budget is exhausted — digest sections share the
    same Tavily budget as the chat search feature on purpose, so one big
    digest run can't itself blow the monthly credit allowance."""
    if not TAVILY_API_KEY:
        return ""
    if not tavily_budget.try_consume():
        log.info("Tavily daily budget exhausted; skipping digest section '%s'", query[:60])
        return ""
    return tavily_web_search(query)

def _extract_digest_lines(digest_text):
    """Pull the bullet lines out of a finished digest for storage in the
    repeat-guard memory (the lines are what gets compared across days)."""
    lines = []
    for raw in (digest_text or "").splitlines():
        stripped = raw.strip()
        if stripped.startswith("- ") and len(stripped) > 4:
            lines.append(stripped[:300])
    return lines

def build_daily_digest_text():
    """Search both digest topics and ask the model to write the two-section
    digest, feeding it the last 7 days of already-reported lines so it
    filters out repeat news. Returns the digest body text, or '' if nothing
    could be built (no Tavily key, budget exhausted, or the AI call failed)
    — callers must treat '' as 'skip this run', not crash."""
    if not TAVILY_API_KEY:
        log.info("Digest skipped: TAVILY_API_KEY not set.")
        return ""

    blocks = []
    for title, query in DIGEST_QUERIES:
        results = _fetch_digest_section(query)
        blocks.append(f"[{title}]\n{results if results else '(نتیجه‌ای یافت نشد)'}")
    raw_context = "\n\n".join(blocks)

    # Repeat-guard: today's lines are excluded so a same-day manual re-run
    # (/digest) regenerates rather than self-censors into an empty digest.
    today = format_trigger_time_iran(time.time()).split(" ")[0]
    prior_lines = repo.get_recent_digest_lines(exclude_date=today)
    if prior_lines:
        raw_context += DIGEST_REPEAT_GUARD + "\n".join(prior_lines[:150])

    try:
        digest = agnes.generate([
            {"role": "system", "content": DIGEST_WRITEUP_PROMPT},
            {"role": "user", "content": raw_context},
        ])
        return (digest or "").strip()
    except Exception:
        log.warning("build_daily_digest_text: generate() failed", exc_info=True)
        return ""

def broadcast_daily_digest(triggered_by=None):
    """Build the digest and push it to every known group as a plain Markdown
    message. On a successful build, the digest's bullet lines are recorded
    in digest_history (7-day rolling window) so future runs skip them.
    Returns (sent_count, failed_count, digest_text); digest_text is '' if
    nothing could be built at all, so callers (e.g. /digest) can report
    that back instead of silently doing nothing."""
    digest_text = build_daily_digest_text()
    if not digest_text:
        return 0, 0, ""

    today_str = format_trigger_time_iran(time.time()).split(" ")[0]
    heading = f"پرونده خبری {today_str}"

    # Record BEFORE sending: even if every group send fails (bot kicked,
    # Telegram down), the news was genuinely covered and re-reporting it
    # tomorrow would be the repeat the memory exists to prevent.
    try:
        repo.save_digest_lines(today_str, _extract_digest_lines(digest_text))
    except Exception:
        log.warning("Failed to record digest history", exc_info=True)

    group_ids = fetch_all_group_ids()
    sent, failed = 0, 0
    for chat_id in group_ids:
        try:
            bot.send_message(chat_id, f"**{heading}**\n\n{digest_text}", parse_mode="Markdown")
            sent += 1
        except Exception as e:
            failed += 1
            log.warning("Digest send failed for chat %s: %s", chat_id, e)
        time.sleep(0.05)  # same pacing as broadcast_message(), respects Telegram flood limits

    METRICS["digests_sent"] += 1
    log.info("Daily digest broadcast: sent=%d failed=%d groups=%d (trigger=%s)",
              sent, failed, len(group_ids), triggered_by or "manual")
    return sent, failed, digest_text

def _digest_scheduler_loop():
    """Background daemon: sleeps until the next DIGEST_HOUR_IRAN:MINUTE Iran
    time, runs the digest, then reschedules for the following day. A crash
    inside one run is caught and logged — the loop still reschedules for
    tomorrow instead of dying."""
    log.info("Digest scheduler thread started (target=%02d:%02d Iran time).", DIGEST_HOUR_IRAN, DIGEST_MINUTE_IRAN)
    # Restart catch-up: if we booted after today's scheduled time and the
    # digest hasn't gone out yet (e.g. a redeploy at 09:05), fire it once
    # now instead of silently skipping until tomorrow.
    try:
        now_iran = iran_now()
        scheduled_today = now_iran.replace(hour=DIGEST_HOUR_IRAN, minute=DIGEST_MINUTE_IRAN, second=0, microsecond=0)
        if now_iran >= scheduled_today and _digest_due_today():
            log.info("Digest catch-up: boot after scheduled time with no send today — running now.")
            broadcast_daily_digest(triggered_by="catchup")
    except Exception:
        log.error("Digest catch-up check failed", exc_info=True)
    while True:
        sleep_for = max(1.0, iran_next_digest_epoch() - time.time())
        time.sleep(sleep_for)
        try:
            sent, failed, digest_text = broadcast_daily_digest(triggered_by="scheduler")
            if not digest_text:
                log.info("Scheduled digest run produced no content (search/AI unavailable); skipped.")
        except Exception:
            log.error("Scheduled digest run failed", exc_info=True)
            admin_notifier.notify("digest_scheduler_error", traceback.format_exc(limit=5))
        time.sleep(5)  # small buffer so a slow run can't immediately re-trigger itself
