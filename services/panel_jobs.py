import logging
import threading
import time

log = logging.getLogger("L-bot")

# =========================
# Job status registry (Stage 3B): name -> counters + last outcomes.
# Loops report via mark_* (try/finally-adjacent one-liners that never alter
# existing exception handling or the "loop never dies" structure).
# Thread liveness is read from threading.enumerate() by the stable thread
# names set in main.py (all loops have one — verified, none missing).
# =========================
_JOB_DEFS = (
    # name, thread name (None = view-only, alive check only), pausable, max pause hours (None = +indefinite)
    ("scheduler", "reminder-scheduler", True, 24),
    ("backup", "backup-scheduler", True, 24),
    ("profiler", "behavior-profiler", True, None),
    ("relay", "ig-relay-polling", True, None),
    ("session-gc", "session-gc", False, None),
    ("fact-extractor", "fact-extractor", False, None),
)

_registry = {}
_registry_lock = threading.Lock()


def _entry(name):
    with _registry_lock:
        return _registry.setdefault(name, {
            "runs": 0, "failures": 0,
            "last_start": 0.0, "last_success": 0.0,
            "last_error": "",
        })


def job_mark_start(name):
    try:
        e = _entry(name)
        with _registry_lock:
            e["runs"] += 1
            e["last_start"] = time.time()
    except Exception:
        pass


def job_mark_success(name):
    try:
        e = _entry(name)
        with _registry_lock:
            e["last_success"] = time.time()
    except Exception:
        pass


def job_mark_error(name, err):
    try:
        from handlers.panel import _redact  # reuse broadcast redaction

        text = _redact(err)
    except Exception:
        try:
            text = str(err)[:200]
        except Exception:
            text = "?"
    try:
        e = _entry(name)
        with _registry_lock:
            e["failures"] += 1
            e["last_error"] = text
    except Exception:
        pass


def _thread_alive(thread_name):
    try:
        return any(t.name == thread_name and t.is_alive()
                   for t in threading.enumerate())
    except Exception:
        return False


def job_snapshot():
    """[(name, thread_name, alive, paused_label_or_None, runs, failures,
    last_success_age, last_error)] — no DB reads except the pause check
    (cached settings, fail open)."""
    from services import panel_settings as ps

    out = []
    for name, thread_name, _pausable, _maxh in _JOB_DEFS:
        paused, label = job_paused_state(name, ps=ps)
        with _registry_lock:
            e = dict(_registry.get(name, {"runs": 0, "failures": 0,
                                          "last_start": 0.0, "last_success": 0.0,
                                          "last_error": ""}))
        alive = _thread_alive(thread_name) if thread_name else None
        age = time.time() - e["last_success"] if e["last_success"] else None
        out.append((name, thread_name, alive, paused, label, e["runs"],
                    e["failures"], age, e["last_error"]))
    return out


# =========================
# Pause / resume: panel_settings key job.<name>.paused_until = epoch float
# or "never". Checked once per loop cycle (cached, fail open = not paused).
# =========================
def _pause_key(name):
    return f"job.{name}.paused_until"


def job_paused_until(name, ps=None):
    """(paused: bool, label: str). DB error → (False, '') — fail open."""
    try:
        if ps is None:
            from services import panel_settings as ps
        raw = ps.get_setting(_pause_key(name), "0")
        if raw == "never":
            return True, "paused indefinitely"
        until = float(str(raw).strip())
        now = time.time()
        if until > now:
            left = until - now
            h, rem = divmod(int(left), 3600)
            label = f"paused {h}h{rem // 60:02d}m left" if h else f"paused {rem // 60}m left"
            return True, label
        return False, ""
    except Exception:
        return False, ""


def job_paused_state(name, ps=None):
    return job_paused_until(name, ps=ps)


def job_pause(name, hours, ps=None):
    """hours in (1, 6, 24) or 0 = indefinite (relay/profiler only).
    Returns (ok, message)."""
    try:
        if ps is None:
            from services import panel_settings as ps
        allowed = {d[0]: d[3] for d in _JOB_DEFS}
        if name not in allowed:
            return False, "unknown job"
        maxh = allowed[name]
        if hours == 0:
            if maxh is not None:
                return False, "indefinite pause is only allowed for relay/profiler"
            ps.set_setting(_pause_key(name), "never")
            return True, "paused indefinitely"
        if hours not in (1, 6, 24):
            return False, "allowed durations: 1h, 6h, 24h"
        if maxh is not None and hours > maxh:
            return False, f"this job can be paused at most {maxh}h"
        until = time.time() + hours * 3600
        ps.set_setting(_pause_key(name), str(until))
        return True, f"paused for {hours}h"
    except Exception:
        log.warning("job pause failed for %s", name, exc_info=True)
        return False, "DB error"


def job_resume(name, ps=None):
    try:
        if ps is None:
            from services import panel_settings as ps
        if name not in {d[0] for d in _JOB_DEFS}:
            return False, "unknown job"
        ps.delete_setting(_pause_key(name))
        return True, "resumed"
    except Exception:
        log.warning("job resume failed for %s", name, exc_info=True)
        return False, "DB error"
