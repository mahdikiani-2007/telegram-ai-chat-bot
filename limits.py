import threading
import time


# =========================
# Thread-safe Rate Limiter (sliding window + global ceiling)
# =========================
class RateLimiter:
    """Per-user sliding-window limiter with a global ceiling. Thread-safe.

    Separate buckets per "category" (chat / pic / vision) so an expensive
    image call has a tighter budget than a normal chat message. Defaults are
    tuned for a modest community-scale deployment (a handful of active
    groups), not a high-traffic public bot.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._users = {}  # (category, user_id) -> [timestamps]
        self._global = {}  # category -> [timestamps]
        self._max_keys = 20_000
        # category -> (per_user_per_min, global_per_min)
        self._limits = {
            "chat": (10, 200),
            "pic": (3, 40),
            "vision": (5, 60),
            "inline": (6, 100),
            "instagram": (5, 40),
            "youtube": (5, 40),
        }

    def _prune(self, lst, cutoff):
        return [t for t in lst if t > cutoff]

    def _gc(self):
        if len(self._users) <= self._max_keys:
            return
        now = time.time()
        self._users = {k: v for k, v in self._users.items() if v and now - v[-1] < 300}

    def allow(self, category, user_id):
        """Return (allowed: bool, scope: str|None)."""
        now = time.time()
        cutoff = now - 60
        per_user, per_global = self._limits.get(category, (10, 200))
        with self._lock:
            g = self._prune(self._global.get(category, []), cutoff)
            if len(g) >= per_global:
                self._global[category] = g
                return False, "global"

            ukey = (category, user_id)
            u = self._prune(self._users.get(ukey, []), cutoff)
            if len(u) >= per_user:
                self._users[ukey] = u
                return False, "user"

            u.append(now)
            g.append(now)
            self._users[ukey] = u
            self._global[category] = g
            self._gc()
            return True, None


rate_limiter = RateLimiter()


# =========================
# Anti-Spam Tracker (thread-safe)
# =========================
class SpamTracker:
    def __init__(self, max_keys=10_000):
        self._lock = threading.Lock()
        self._data = {}
        self._max = max_keys

    def _gc(self):
        if len(self._data) <= self._max:
            return
        now = time.time()
        self._data = {k: v for k, v in self._data.items() if now - v["last_time"] < 300}

    def check(self, key):
        """Return (action, value).

        action == "muted"   -> value = remaining seconds
        action == "warn"    -> value = None (just hit the mute threshold)
        action == "ok"      -> value = None
        """
        now = time.time()
        with self._lock:
            entry = self._data.get(key)
            if entry:
                if now < entry["muted_until"]:
                    return "muted", int(entry["muted_until"] - now)
                if now - entry["last_time"] < 15:
                    entry["count"] += 1
                else:
                    entry["count"] = 1
                entry["last_time"] = now
            else:
                entry = {"count": 1, "muted_until": 0.0, "last_time": now}
                self._data[key] = entry

            if entry["count"] >= 3:
                entry["muted_until"] = now + 60
                entry["count"] = 0
                self._gc()
                return "warn", None

            self._gc()
            return "ok", None


spam_tracker = SpamTracker()
