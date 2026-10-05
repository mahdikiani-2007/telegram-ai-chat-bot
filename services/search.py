import datetime
import json
import logging
import re
import threading
import time

from ai import agnes
from config import TAVILY_API_KEY, TAVILY_DAILY_LIMIT, TAVILY_MAX_RESULTS, TAVILY_SEARCH_URL, UTC_TZ
from infra import HTTP
from metrics import METRICS

log = logging.getLogger("L-bot")


if not TAVILY_API_KEY:
    log.warning("TAVILY_API_KEY not set — live web search is disabled; bot answers from the model alone.")
else:
    log.info("Web search enabled via Tavily (daily cap=%d, max_results=%d).", TAVILY_DAILY_LIMIT, TAVILY_MAX_RESULTS)

# =========================
# Web Search (Tavily) — decide → budget-check → search → inject as context
# =========================
class DailyBudget:
    """Thread-safe daily counter so one busy day can't burn through a whole
    month's worth of Tavily credits. Resets at UTC midnight. This is a
    coarse safety cap, not real billing/accounting."""

    def __init__(self, daily_limit):
        self._lock = threading.Lock()
        self._limit = daily_limit
        self._day = None
        self._count = 0

    def set_limit(self, daily_limit):
        """Panel override (Stage 2B): clamped 0–1000 upstream; applied from
        the DB at startup and after every panel write."""
        with self._lock:
            self._limit = max(0, int(daily_limit))

    def try_consume(self):
        if self._limit <= 0:
            return True  # 0 or negative in env = no cap configured
        today = datetime.datetime.now(tz=UTC_TZ).date()
        with self._lock:
            if self._day != today:
                self._day = today
                self._count = 0
            if self._count >= self._limit:
                return False
            self._count += 1
            return True

tavily_budget = DailyBudget(TAVILY_DAILY_LIMIT)


def _tavily_max_results():
    """Panel-overrideable max results (Stage 2B)."""
    try:
        from services.panel_settings import setting_value

        return setting_value("tavily.max_results")
    except Exception:
        return TAVILY_MAX_RESULTS

SEARCH_DECISION_PROMPT = (
    "You decide whether answering the user's latest message requires a live "
    "web search for current / up-to-date information — news, prices, "
    "scores, recent events, who currently holds some position, or any fact "
    "that could have changed recently or that you might not know "
    "confidently. Timeless facts, greetings, opinions, coding help, "
    "creative writing, and ordinary conversation do NOT need a search.\n\n"
    "Respond with ONLY a single JSON object, nothing else, no code fences, "
    "in this exact shape: "
    '{"search": true or false, "query": "a short effective web search query '
    'in the same language as the user\'s message, or an empty string if '
    'search is false"}'
)

def decide_web_search(user_text):
    """One-shot AI call: does this need a web search, and with what query?
    Returns (should_search: bool, query: str). Fails closed (False) on any
    error or unparseable response so a flaky decision call never burns a
    Tavily credit."""
    if not TAVILY_API_KEY or not user_text or not user_text.strip():
        return False, ""
    try:
        raw = agnes.generate([
            {"role": "system", "content": SEARCH_DECISION_PROMPT},
            {"role": "user", "content": user_text[:1000]},
        ])
        raw = (raw or "").strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        should = bool(data.get("search"))
        query = str(data.get("query") or "").strip()
        if not should or not query:
            return False, ""
        return True, query[:300]
    except Exception:
        log.info("decide_web_search: could not parse decision response, skipping search")
        return False, ""

def tavily_web_search(query):
    """Call Tavily's search API and return a compact plain-text block of
    results, or '' on any failure. Uses the shared HTTP session (pooling +
    retries already configured on it)."""
    if not TAVILY_API_KEY:
        return ""
    try:
        r = HTTP.post(
            TAVILY_SEARCH_URL,
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "max_results": _tavily_max_results(),
                "search_depth": "basic",
                "include_answer": False,
            },
            timeout=15,
        )
        if r.status_code != 200:
            log.warning("Tavily search non-200: %d — %s", r.status_code, r.text[:300])
            return ""
        r.encoding = "utf-8"
        data = r.json()
        results = data.get("results") or []
        if not results:
            return ""
        lines = []
        for item in results[:_tavily_max_results()]:
            title = (item.get("title") or "").strip()
            content = (item.get("content") or "").strip()
            if len(content) > 400:
                content = content[:400].rstrip() + "..."
            if title or content:
                lines.append(f"- {title}: {content}")
        return "\n".join(lines)
    except Exception:
        log.warning("Tavily search request failed", exc_info=True)
        return ""

def maybe_web_search(user_text):
    """Full pipeline: decide -> budget check -> search. Returns a ready-to-
    inject context string, or '' if search wasn't needed/available/over
    budget. Never raises — any failure here just means no search context
    for this turn, the bot still answers normally."""
    should, query = decide_web_search(user_text)
    if not should:
        return ""
    if not tavily_budget.try_consume():
        log.info("Tavily daily search budget exhausted; skipping search for '%s'", query[:80])
        return ""
    results = tavily_web_search(query)
    if not results:
        return ""
    METRICS["web_searches"] += 1
    return results

def maybe_web_search_with_status(bot_instance, chat_id, placeholder_id, user_text):
    """Same pipeline as maybe_web_search, but — once we know a search is
    actually going to happen — edits `placeholder_id` to a short status line
    so the user sees something during the extra decision+search round trip
    instead of a silent gap that looks like the bot is stuck. Streaming
    picks up and overwrites that placeholder normally right after, so no
    cleanup is needed here."""
    should, query = decide_web_search(user_text)
    if not should:
        return ""
    if not tavily_budget.try_consume():
        log.info("Tavily daily search budget exhausted; skipping search for '%s'", query[:80])
        return ""
    try:
        bot_instance.edit_message_text(
            "🔎 در حال جستجو در وب...", chat_id=chat_id, message_id=placeholder_id,
        )
    except Exception:
        pass  # placeholder may already be gone; the search itself still runs
    results = tavily_web_search(query)
    if not results:
        return ""
    METRICS["web_searches"] += 1
    return results

SEARCH_CONTEXT_PREFIX = (
    "[نتایج جستجوی وب — فقط پشت‌صحنه برای کمک به پاسخ‌دهی؛ لازم نیست به "
    "کاربر بگی سرچ کردی مگر خودش بپرسه، و لینک/URL توی پاسخ ننویس]:\n"
)

def inject_search_context(text, search_context):
    """Prefix `text` with the search results block, for the outgoing API
    call only — callers must keep saving the original `text` (without this
    block) to the chat history/repo, so the DB and rolling summary never
    get bloated with search dumps."""
    if not search_context:
        return text
    return f"{SEARCH_CONTEXT_PREFIX}{search_context}\n\n{text}"
