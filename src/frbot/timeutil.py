"""Day-boundary helpers. Storage is UTC; the learning day follows the
configured timezone (Europe/Paris by default).
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


def day_start_utc(now: datetime, tz: str) -> datetime:
    """Start of the current local day, as a UTC datetime."""
    local = now.astimezone(ZoneInfo(tz))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC)


def day_end_utc(now: datetime, tz: str) -> datetime:
    """End (exclusive) of the current local day, as a UTC datetime."""
    return day_start_utc(now, tz) + timedelta(days=1)


def tomorrow_end_utc(now: datetime, tz: str) -> datetime:
    """End (exclusive) of tomorrow's local day, as a UTC datetime."""
    return day_start_utc(now, tz) + timedelta(days=2)
