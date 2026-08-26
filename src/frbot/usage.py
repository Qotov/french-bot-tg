"""Per-user daily cap on LLM-backed actions.

A cost guardrail, not a billing system: it stops a runaway loop or one
unusually enthusiastic pilot user from running up the Gemini bill. Counters
live in memory and reset at the local day boundary (and on restart) — that is
deliberate for a pilot of known people, where the real risk is a bug, not abuse.
"""

import logging
from collections import defaultdict
from datetime import UTC, datetime

from frbot.timeutil import day_start_utc

logger = logging.getLogger(__name__)

OVER_LIMIT_TEXT = (
    "🛑 На сегодня достигнут дневной лимит запросов. "
    "Карточки и повторения работают как обычно — новые запросы к ИИ будут завтра."
)


class UsageLimiter:
    def __init__(self, daily_limit: int, tz: str) -> None:
        self.daily_limit = daily_limit
        self.tz = tz
        self._counts: dict[int, int] = defaultdict(int)
        self._day_start: datetime | None = None

    def _roll(self, now: datetime) -> None:
        start = day_start_utc(now, self.tz)
        if self._day_start != start:
            self._day_start = start
            self._counts.clear()

    def check_and_count(self, user_id: int, now: datetime | None = None) -> bool:
        """Records one LLM action; False when the user is over the daily limit."""
        now = now or datetime.now(UTC)
        self._roll(now)
        if self._counts[user_id] >= self.daily_limit:
            logger.warning("user %d hit the daily LLM limit (%d)", user_id, self.daily_limit)
            return False
        self._counts[user_id] += 1
        return True

    def used_today(self, user_id: int, now: datetime | None = None) -> int:
        self._roll(now or datetime.now(UTC))
        return self._counts[user_id]
