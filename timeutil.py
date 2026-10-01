import datetime
import re
import time

from config import DIGEST_HOUR_IRAN, DIGEST_MINUTE_IRAN, IRAN_TZ


def iran_now_epoch() -> float:
    """Return current epoch time (UTC-based). All epoch storage uses UTC."""
    return time.time()


def iran_now() -> datetime.datetime:
    """Current time as a timezone-aware Iran-local datetime."""
    return datetime.datetime.now(tz=IRAN_TZ)


def iran_tomorrow_9am_epoch() -> float:
    """Calculate the epoch for tomorrow at 09:00 Iran Standard Time."""
    now_iran = iran_now()
    tomorrow_9am_iran = now_iran.replace(
        hour=9, minute=0, second=0, microsecond=0
    ) + datetime.timedelta(days=1)
    return tomorrow_9am_iran.timestamp()


def iran_next_digest_epoch() -> float:
    """Epoch for the next occurrence of DIGEST_HOUR_IRAN:DIGEST_MINUTE_IRAN
    Iran time — today if that time hasn't passed yet, otherwise tomorrow."""
    now_iran = iran_now()
    target_iran = now_iran.replace(
        hour=DIGEST_HOUR_IRAN, minute=DIGEST_MINUTE_IRAN, second=0, microsecond=0
    )
    if target_iran <= now_iran:
        target_iran += datetime.timedelta(days=1)
    return target_iran.timestamp()


def format_trigger_time_iran(epoch_ts: float) -> str:
    """Format an epoch timestamp as Iran-local human-readable string."""
    dt = datetime.datetime.fromtimestamp(epoch_ts, tz=IRAN_TZ)
    return dt.strftime("%Y-%m-%d %H:%M")


def format_deadline_display(epoch_ts: float) -> str:
    """Format a deadline epoch as a smart Iran-local string for todo display."""
    dt = datetime.datetime.fromtimestamp(epoch_ts, tz=IRAN_TZ)
    now_iran = iran_now()
    if dt.date() == now_iran.date():
        return f"🕒 Expiring: Today {dt.strftime('%H:%M')}"
    elif dt.date() == (now_iran + datetime.timedelta(days=1)).date():
        return f"🕒 Expiring: Tomorrow {dt.strftime('%H:%M')}"
    else:
        return f"🕒 Expiring: {dt.strftime('%Y-%m-%d %H:%M')}"


def parse_custom_time_input(user_input: str) -> float | None:
    """Parse user-entered time string into a UTC epoch timestamp.

    Supported formats:
      - Relative: 45m, 2h, 3d (from now)
      - Absolute clock: 18:30 (today Iran time, or tomorrow if passed)
      - Full datetime: 2026-07-15 10:00 (Iran time)

    Returns UTC epoch or None if unparseable.
    """
    text = user_input.strip().lower()

    m = re.fullmatch(r"(\d+)\s*(m|min|mins|minute|minutes)", text)
    if m:
        return time.time() + int(m.group(1)) * 60
    m = re.fullmatch(r"(\d+)\s*(h|hr|hrs|hour|hours)", text)
    if m:
        return time.time() + int(m.group(1)) * 3600
    m = re.fullmatch(r"(\d+)\s*(d|day|days)", text)
    if m:
        return time.time() + int(m.group(1)) * 86400

    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            now_iran = iran_now()
            target_iran = now_iran.replace(hour=h, minute=mi, second=0, microsecond=0)
            if target_iran <= now_iran:
                target_iran += datetime.timedelta(days=1)
            return target_iran.timestamp()
        return None

    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", text)
    if m:
        try:
            yr, mo, dy = int(m.group(1)), int(m.group(2)), int(m.group(3))
            h, mi = int(m.group(4)), int(m.group(5))
            if not (0 <= h <= 23 and 0 <= mi <= 59):
                return None
            target_iran = datetime.datetime(yr, mo, dy, h, mi, 0, tzinfo=IRAN_TZ)
            if target_iran <= iran_now():
                return None  # past datetime — firing immediately is never what a reminder/todo wants
            return target_iran.timestamp()
        except (ValueError, OverflowError):
            return None

    return None


def _parse_custom_interval_seconds(interval_str: str) -> int | None:
    """Parse a custom recurrence interval string into seconds.

    Supported: 12h, 3d, 30m, etc.
    Returns seconds or None if unparseable.
    """
    text = interval_str.strip().lower()
    m = re.fullmatch(r"(\d+)\s*(m|min|mins|minute|minutes)", text)
    if m:
        return int(m.group(1)) * 60
    m = re.fullmatch(r"(\d+)\s*(h|hr|hrs|hour|hours)", text)
    if m:
        return int(m.group(1)) * 3600
    m = re.fullmatch(r"(\d+)\s*(d|day|days)", text)
    if m:
        return int(m.group(1)) * 86400
    return None
