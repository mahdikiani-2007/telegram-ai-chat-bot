# =========================
# Metrics (in-process counters; used by /stats)
# =========================
import time

METRICS = {"handled": 0, "llm_errors": 0, "ratelimited": 0, "summaries": 0, "web_searches": 0, "digests_sent": 0, "instagram_downloads": 0, "youtube_downloads": 0}

# Process-start timestamp for uptime reporting (/stats). Lives here (not
# bot.py) so handler modules never need to import the entrypoint module.
_START_TIME = time.time()
