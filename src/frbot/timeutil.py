"""Day-boundary helpers. Storage is UTC; the learning day follows the
configured timezone (Europe/Paris by default).

Boundaries are computed as actual local midnights (not "midnight + 24h"),
so they stay correct across DST transitions (23h and 25h days).
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo


def _local_midnight_utc(now: datetime, tz: str, days_ahead: int) -> datetime:
    zone = ZoneInfo(tz)
    local_date = now.astimezone(zone).date() + timedelta(days=days_ahead)
    return datetime.combine(local_date, time(), tzinfo=zone).astimezone(UTC)


def day_start_utc(now: datetime, tz: str) -> datetime:
    """Start of the current local day, as a UTC datetime."""
    return _local_midnight_utc(now, tz, 0)


def day_end_utc(now: datetime, tz: str) -> datetime:
    """End (exclusive) of the current local day, as a UTC datetime."""
    return _local_midnight_utc(now, tz, 1)


def tomorrow_end_utc(now: datetime, tz: str) -> datetime:
    """End (exclusive) of tomorrow's local day, as a UTC datetime."""
    return _local_midnight_utc(now, tz, 2)
