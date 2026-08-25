"""Query functions. Every function takes an AsyncSession and commits nothing;
callers own the transaction (commit at the end of a handler/job).
"""

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from frbot.config import TIME_RE, Settings
from frbot.db.models import AppSetting, Card, CardState, Review
from frbot.srs.scheduler import ReviewResult

logger = logging.getLogger(__name__)

CHAT_ID_KEY = "chat_id"


async def get_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.get(AppSetting, key)
    return row.value if row else None


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


async def get_all_settings(session: AsyncSession) -> dict[str, str]:
    rows = (await session.execute(select(AppSetting))).scalars().all()
    return {r.key: r.value for r in rows}


# -- cards --------------------------------------------------------------------


async def get_card(session: AsyncSession, card_id: int) -> Card | None:
    return await session.get(Card, card_id)


async def find_card_by_lemma(session: AsyncSession, lemma: str) -> Card | None:
    stmt = (
        select(Card)
        .where(func.lower(Card.lemma) == lemma.strip().lower())
        .order_by(Card.id)
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def add_card(session: AsyncSession, card: Card) -> Card:
    session.add(card)
    await session.flush()
    return card


async def delete_card(session: AsyncSession, card_id: int) -> bool:
    card = await session.get(Card, card_id)
    if card is None:
        return False
    await session.delete(card)
    return True


async def get_due_cards(session: AsyncSession, *, now: datetime, limit: int) -> list[Card]:
    stmt = (
        select(Card)
        .where(
            Card.suspended.is_(False),
            Card.state != CardState.new.value,
            Card.due <= now,
        )
        .order_by(Card.due)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_new_cards(session: AsyncSession, *, limit: int) -> list[Card]:
    stmt = (
        select(Card)
        .where(Card.suspended.is_(False), Card.state == CardState.new.value)
        .order_by(Card.id)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def count_due(session: AsyncSession, *, until: datetime) -> int:
    stmt = select(func.count(Card.id)).where(
        Card.suspended.is_(False),
        Card.state != CardState.new.value,
        Card.due <= until,
    )
    return (await session.execute(stmt)).scalar_one()


async def count_new_introduced_since(session: AsyncSession, *, since: datetime) -> int:
    """Cards whose FIRST review happened at or after `since`."""
    first_reviews = (
        select(Review.card_id, func.min(Review.reviewed_at).label("first_at"))
        .group_by(Review.card_id)
        .subquery()
    )
    stmt = select(func.count()).select_from(first_reviews).where(first_reviews.c.first_at >= since)
    return (await session.execute(stmt)).scalar_one()


async def apply_review(
    session: AsyncSession,
    card: Card,
    result: ReviewResult,
    *,
    rating: int,
    now: datetime,
) -> None:
    card.fsrs = result.fsrs
    card.due = result.due
    card.state = result.state
    session.add(
        Review(
            card_id=card.id,
            rating=rating,
            reviewed_at=now,
            elapsed_days=result.elapsed_days,
        )
    )


# -- stats --------------------------------------------------------------------


@dataclass(frozen=True)
class Stats:
    due_today: int
    reviews_7d: int
    correct_rate_7d: float | None  # None when there were no reviews
    new_cards_7d: int
    top_error_types_30d: list[tuple[str, int]]


async def gather_stats(
    session: AsyncSession,
    *,
    due_until: datetime,
    week_ago: datetime,
    month_ago: datetime,
) -> Stats:
    due_today = await count_due(session, until=due_until)

    ratings = (
        (await session.execute(select(Review.rating).where(Review.reviewed_at >= week_ago)))
        .scalars()
        .all()
    )
    reviews_7d = len(ratings)
    correct_rate_7d = sum(1 for r in ratings if r >= 3) / reviews_7d if reviews_7d else None

    new_cards_7d = (
        await session.execute(select(func.count(Card.id)).where(Card.created_at >= week_ago))
    ).scalar_one()

    error_metas = (
        (
            await session.execute(
                select(Card.error_meta).where(Card.kind != "vocab", Card.created_at >= month_ago)
            )
        )
        .scalars()
        .all()
    )
    counts = Counter(meta.get("type", "other") for meta in error_metas if isinstance(meta, dict))
    return Stats(
        due_today=due_today,
        reviews_7d=reviews_7d,
        correct_rate_7d=correct_rate_7d,
        new_cards_7d=new_cards_7d,
        top_error_types_30d=counts.most_common(5),
    )


# -- runtime-overridable configuration ---------------------------------------


@dataclass(frozen=True)
class EffectiveConfig:
    reminder_time: str
    writing_time: str
    daily_new_limit: int
    session_max: int


RUNTIME_KEYS = ("REMINDER_TIME", "WRITING_TIME", "DAILY_NEW_LIMIT", "SESSION_MAX")


def _time_or(value: str | None, default: str) -> str:
    if value and TIME_RE.match(value):
        return value
    if value:
        logger.warning("invalid time override %r, using %s", value, default)
    return default


def _int_or(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        logger.warning("invalid int override %r, using %s", value, default)
        return default
    return parsed if parsed > 0 else default


async def get_effective_config(session: AsyncSession, settings: Settings) -> EffectiveConfig:
    """Env values overridden by /settings values stored in app_settings."""
    overrides = await get_all_settings(session)
    return EffectiveConfig(
        reminder_time=_time_or(overrides.get("REMINDER_TIME"), settings.reminder_time),
        writing_time=_time_or(overrides.get("WRITING_TIME"), settings.writing_time),
        daily_new_limit=_int_or(overrides.get("DAILY_NEW_LIMIT"), settings.daily_new_limit),
        session_max=_int_or(overrides.get("SESSION_MAX"), settings.session_max),
    )
