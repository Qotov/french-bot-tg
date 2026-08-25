"""Builds the daily review queue.

Order: all due cards (due <= now, oldest due first), capped at session_max;
then new cards fill the remaining slots, limited by how many new cards may
still be introduced today (daily_new_limit minus the new cards already
introduced today, i.e. cards whose first review happened today).
"""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from frbot.db import repo
from frbot.timeutil import day_start_utc


@dataclass(frozen=True)
class ReviewQueue:
    card_ids: list[int] = field(default_factory=list)
    due_count: int = 0
    new_count: int = 0

    @property
    def total(self) -> int:
        return len(self.card_ids)


async def build_queue(
    session: AsyncSession,
    *,
    now: datetime,
    tz: str,
    session_max: int,
    daily_new_limit: int,
) -> ReviewQueue:
    due_cards = await repo.get_due_cards(session, now=now, limit=session_max)

    new_cards = []
    remaining = session_max - len(due_cards)
    if remaining > 0:
        introduced_today = await repo.count_new_introduced_since(
            session, since=day_start_utc(now, tz)
        )
        allowance = max(0, daily_new_limit - introduced_today)
        if allowance > 0:
            new_cards = await repo.get_new_cards(session, limit=min(remaining, allowance))

    return ReviewQueue(
        card_ids=[card.id for card in [*due_cards, *new_cards]],
        due_count=len(due_cards),
        new_count=len(new_cards),
    )
