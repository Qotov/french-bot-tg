"""Query functions. Every function takes an AsyncSession and commits nothing;
callers own the transaction (commit at the end of a handler/job).

Multi-user rule: every function that touches cards, reviews, or writings takes
a REQUIRED keyword-only `user_id`. Forgetting it is a TypeError at import/call
time rather than a silent cross-user data leak, which is the one bug class this
layer must never have.
"""

import logging
import secrets
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from frbot.config import TIME_RE, Settings
from frbot.db.models import (
    LEVELS,
    AppSetting,
    Card,
    CardKind,
    CardState,
    DrillTopic,
    Invite,
    Review,
    User,
    Writing,
)
from frbot.srs.scheduler import ReviewResult, SrsScheduler

logger = logging.getLogger(__name__)

CHAT_ID_KEY = "chat_id"  # legacy single-user key, kept for old databases


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


# -- users --------------------------------------------------------------------


async def get_user(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def count_users(session: AsyncSession) -> int:
    return (await session.execute(select(func.count(User.id)))).scalar_one()


async def list_users(session: AsyncSession) -> list[User]:
    stmt = select(User).order_by(User.created_at)
    return list((await session.execute(stmt)).scalars().all())


async def list_active_users(session: AsyncSession) -> list[User]:
    stmt = select(User).where(User.active.is_(True)).order_by(User.id)
    return list((await session.execute(stmt)).scalars().all())


async def create_user(
    session: AsyncSession,
    *,
    user_id: int,
    username: str | None,
    first_name: str | None,
    chat_id: int | None,
    invite_code: str | None,
    is_admin: bool = False,
) -> User:
    user = User(
        id=user_id,
        username=username,
        first_name=first_name,
        chat_id=chat_id,
        invite_code=invite_code,
        is_admin=is_admin,
    )
    session.add(user)
    await session.flush()
    logger.info("user %d registered (admin=%s, invite=%s)", user_id, is_admin, invite_code)
    return user


async def set_user_level(session: AsyncSession, user_id: int, level: str) -> bool:
    if level not in LEVELS:
        return False
    user = await session.get(User, user_id)
    if user is None:
        return False
    user.level = level
    return True


# -- invites ------------------------------------------------------------------


def _new_code() -> str:
    # Short, unambiguous, safe inside a Telegram deep link.
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


async def create_invite(
    session: AsyncSession, *, created_by: int, max_uses: int = 1
) -> Invite:
    invite = Invite(code=_new_code(), created_by=created_by, max_uses=max(1, max_uses))
    session.add(invite)
    await session.flush()
    return invite


async def list_invites(session: AsyncSession) -> list[Invite]:
    stmt = select(Invite).order_by(Invite.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def redeem_invite(session: AsyncSession, code: str) -> Invite | None:
    """Consume one use of a valid invite; None when unknown or exhausted."""
    invite = await session.get(Invite, code.strip().upper())
    if invite is None or invite.used_count >= invite.max_uses:
        return None
    invite.used_count += 1
    await session.flush()
    return invite


# -- cards --------------------------------------------------------------------


async def get_card(session: AsyncSession, card_id: int, *, user_id: int) -> Card | None:
    card = await session.get(Card, card_id)
    if card is None or card.user_id != user_id:
        return None
    return card


async def find_card_by_lemma(
    session: AsyncSession, lemma: str, *, user_id: int
) -> Card | None:
    """Vocab-card dedupe lookup (error cards keep synthetic lemmas and are skipped)."""
    stmt = (
        select(Card)
        .where(
            Card.user_id == user_id,
            Card.kind == CardKind.vocab.value,
            func.lower(Card.lemma) == lemma.strip().lower(),
        )
        .order_by(Card.id)
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def add_card(session: AsyncSession, card: Card) -> Card:
    session.add(card)
    await session.flush()
    return card


async def create_vocab_card(
    session: AsyncSession,
    srs: SrsScheduler,
    *,
    user_id: int,
    text: str,
    enrichment: dict,
) -> Card:
    new = srs.new_card()
    card = Card(
        user_id=user_id,
        text=text,
        lemma=str(enrichment["lemma"]).strip().lower(),
        kind=CardKind.vocab.value,
        enrichment=enrichment,
        fsrs=new.fsrs,
        due=new.due,
        state=new.state,
    )
    session.add(card)
    await session.flush()
    return card


async def delete_card(session: AsyncSession, card_id: int, *, user_id: int) -> bool:
    card = await get_card(session, card_id, user_id=user_id)
    if card is None:
        return False
    await session.delete(card)
    return True


async def get_due_cards(
    session: AsyncSession, *, user_id: int, now: datetime, limit: int
) -> list[Card]:
    stmt = (
        select(Card)
        .where(
            Card.user_id == user_id,
            Card.suspended.is_(False),
            Card.state != CardState.new.value,
            Card.due <= now,
        )
        .order_by(Card.due)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_new_cards(session: AsyncSession, *, user_id: int, limit: int) -> list[Card]:
    stmt = (
        select(Card)
        .where(
            Card.user_id == user_id,
            Card.suspended.is_(False),
            Card.state == CardState.new.value,
        )
        .order_by(Card.id)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def count_due(session: AsyncSession, *, user_id: int, until: datetime) -> int:
    stmt = select(func.count(Card.id)).where(
        Card.user_id == user_id,
        Card.suspended.is_(False),
        Card.state != CardState.new.value,
        Card.due <= until,
    )
    return (await session.execute(stmt)).scalar_one()


async def count_cards(session: AsyncSession, *, user_id: int) -> int:
    stmt = select(func.count(Card.id)).where(Card.user_id == user_id)
    return (await session.execute(stmt)).scalar_one()


async def count_new_introduced_since(
    session: AsyncSession, *, user_id: int, since: datetime
) -> int:
    """Cards whose FIRST review happened at or after `since`."""
    first_reviews = (
        select(Review.card_id, func.min(Review.reviewed_at).label("first_at"))
        .where(Review.user_id == user_id)
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
    user_id: int,
    rating: int,
    now: datetime,
) -> None:
    card.fsrs = result.fsrs
    card.due = result.due
    card.state = result.state
    session.add(
        Review(
            user_id=user_id,
            card_id=card.id,
            rating=rating,
            reviewed_at=now,
            elapsed_days=result.elapsed_days,
        )
    )


async def last_activity_at(session: AsyncSession, *, user_id: int) -> datetime | None:
    """Most recent review — the pilot's activity signal."""
    stmt = select(func.max(Review.reviewed_at)).where(Review.user_id == user_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def count_active_days(
    session: AsyncSession, *, user_id: int, since: datetime
) -> int:
    """Distinct days with at least one review — the retention metric."""
    day = func.date(Review.reviewed_at)
    stmt = (
        select(func.count(func.distinct(day)))
        .where(Review.user_id == user_id, Review.reviewed_at >= since)
    )
    return (await session.execute(stmt)).scalar_one()


# -- writing ------------------------------------------------------------------


async def pick_writing_words(
    session: AsyncSession, *, user_id: int, due_until: datetime, limit: int = 3
) -> list[str]:
    """Prefer vocab cards due today; fill up with the most recent captures."""
    due_stmt = (
        select(Card.lemma)
        .where(
            Card.user_id == user_id,
            Card.kind == CardKind.vocab.value,
            Card.suspended.is_(False),
            Card.due <= due_until,
        )
        .order_by(Card.due)
        .limit(limit)
    )
    words = list((await session.execute(due_stmt)).scalars().all())
    if len(words) < limit:
        recent_stmt = (
            select(Card.lemma)
            .where(
                Card.user_id == user_id,
                Card.kind == CardKind.vocab.value,
                Card.suspended.is_(False),
                Card.lemma.not_in(words) if words else Card.lemma.is_not(None),
            )
            .order_by(Card.id.desc())
            .limit(limit - len(words))
        )
        words.extend((await session.execute(recent_stmt)).scalars().all())
    return words


async def create_writing(session: AsyncSession, prompt: str, *, user_id: int) -> Writing:
    writing = Writing(user_id=user_id, prompt=prompt)
    session.add(writing)
    await session.flush()
    return writing


async def get_writing(
    session: AsyncSession, writing_id: int, *, user_id: int
) -> Writing | None:
    writing = await session.get(Writing, writing_id)
    if writing is None or writing.user_id != user_id:
        return None
    return writing


# -- error cards --------------------------------------------------------------

ERROR_CARDS_DAILY_CAP = 5


async def count_error_cards_created_since(
    session: AsyncSession, *, user_id: int, since: datetime
) -> int:
    stmt = select(func.count(Card.id)).where(
        Card.user_id == user_id,
        Card.kind == CardKind.error.value,
        Card.created_at >= since,
    )
    return (await session.execute(stmt)).scalar_one()


async def find_error_card(
    session: AsyncSession, *, user_id: int, kind: str, err_type: str, corrected: str
) -> Card | None:
    """Dedupe on type + corrected span."""
    stmt = (
        select(Card)
        .where(
            Card.user_id == user_id,
            Card.kind == kind,
            func.lower(Card.lemma) == f"{err_type}:{corrected.strip().lower()}",
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_error_card(
    session: AsyncSession,
    srs: SrsScheduler,
    *,
    user_id: int,
    kind: str,
    sentence: str,
    original: str,
    corrected: str,
    err_type: str,
    explanation_ru: str,
    front: str | None = None,
) -> Card | None:
    """Create an error/drill_error card unless an equal one exists. No cap check here.

    `front` is the pre-gapped sentence shown at review time; computing it at
    creation time avoids fragile substring replacement later (a short span like
    "a" occurs inside other words).
    """
    existing = await find_error_card(
        session, user_id=user_id, kind=kind, err_type=err_type, corrected=corrected
    )
    if existing is not None:
        return None
    new = srs.new_card()
    card = Card(
        user_id=user_id,
        text=sentence,
        lemma=f"{err_type}:{corrected.strip().lower()}",
        kind=kind,
        error_meta={
            "type": err_type,
            "original": original,
            "corrected": corrected,
            "explanation_ru": explanation_ru,
            "front": front,
        },
        fsrs=new.fsrs,
        due=new.due,
        state=new.state,
    )
    session.add(card)
    await session.flush()
    return card


# -- drill topics -------------------------------------------------------------

SEED_TOPICS: tuple[tuple[str, str], ...] = (
    ("aux-passe-compose", "Avoir ou être au passé composé"),
    ("genre-des-noms", "Le genre des noms"),
    ("depuis-pendant-il-y-a", "Depuis, pendant, il y a"),
    ("de-apres-negation", "De après la négation (pas de, plus de)"),
    ("si-clauses", "La concordance des temps dans les phrases avec si"),
    ("subjonctif-present", "Le subjonctif présent après il faut que, vouloir que"),
    ("pronoms-y-en", "Les pronoms y et en"),
    ("ordre-des-pronoms", "L'ordre des pronoms compléments (COD / COI)"),
    ("relatifs-qui-que-dont", "Les pronoms relatifs qui, que, dont"),
    ("futur-vs-conditionnel", "Futur simple ou conditionnel"),
)


async def ensure_drill_topics_seeded(session: AsyncSession) -> None:
    count = (await session.execute(select(func.count(DrillTopic.id)))).scalar_one()
    if count:
        return
    for position, (slug, title_fr) in enumerate(SEED_TOPICS, start=1):
        session.add(DrillTopic(slug=slug, title_fr=title_fr, position=position))
    await session.flush()
    logger.info("seeded %d drill topics", len(SEED_TOPICS))


async def get_topic_for_week(session: AsyncSession, *, today: date) -> DrillTopic | None:
    """The cohort's grammar topic for the ISO week containing `today`.

    Deterministic (no stored rotation pointer), so everyone in the pilot drills
    the same topic in the same week — the cheapest community feature there is,
    and it cannot drift out of sync after a restart or a missed weekly job.
    """
    topics = list(
        (await session.execute(select(DrillTopic).order_by(DrillTopic.position))).scalars()
    )
    if not topics:
        return None
    return topics[today.isocalendar().week % len(topics)]


async def mark_topic_announced(session: AsyncSession, topic: DrillTopic, *, week: date) -> None:
    topic.active_week = week
    await session.flush()


async def get_recent_lemmas(
    session: AsyncSession, *, user_id: int, limit: int = 20
) -> list[str]:
    stmt = (
        select(Card.lemma)
        .where(
            Card.user_id == user_id,
            Card.kind == CardKind.vocab.value,
            Card.suspended.is_(False),
        )
        .order_by(Card.id.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


# -- stats --------------------------------------------------------------------


@dataclass(frozen=True)
class Stats:
    due_today: int
    reviews_7d: int
    correct_rate_7d: float | None  # None when there were no reviews
    new_cards_7d: int
    top_error_types_30d: list[tuple[str, int]]
    total_cards: int = 0
    active_days_7d: int = 0


async def gather_stats(
    session: AsyncSession,
    *,
    user_id: int,
    due_until: datetime,
    week_ago: datetime,
    month_ago: datetime,
) -> Stats:
    due_today = await count_due(session, user_id=user_id, until=due_until)

    ratings = (
        (
            await session.execute(
                select(Review.rating).where(
                    Review.user_id == user_id, Review.reviewed_at >= week_ago
                )
            )
        )
        .scalars()
        .all()
    )
    reviews_7d = len(ratings)
    correct_rate_7d = sum(1 for r in ratings if r >= 3) / reviews_7d if reviews_7d else None

    new_cards_7d = (
        await session.execute(
            select(func.count(Card.id)).where(
                Card.user_id == user_id, Card.created_at >= week_ago
            )
        )
    ).scalar_one()

    error_metas = (
        (
            await session.execute(
                select(Card.error_meta).where(
                    Card.user_id == user_id,
                    Card.kind != "vocab",
                    Card.created_at >= month_ago,
                )
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
        total_cards=await count_cards(session, user_id=user_id),
        active_days_7d=await count_active_days(session, user_id=user_id, since=week_ago),
    )


# -- per-user configuration ---------------------------------------------------


@dataclass(frozen=True)
class EffectiveConfig:
    reminder_time: str
    writing_time: str
    daily_new_limit: int
    session_max: int
    level: str = "B1"


RUNTIME_KEYS = ("REMINDER_TIME", "WRITING_TIME", "DAILY_NEW_LIMIT", "SESSION_MAX")


def _time_or(value: str | None, default: str) -> str:
    if value and TIME_RE.match(value):
        return value
    if value:
        logger.warning("invalid time override %r, using %s", value, default)
    return default


def _int_or(value: int | None, default: int) -> int:
    if value is None:
        return default
    return value if value > 0 else default


def config_for_user(user: User | None, settings: Settings) -> EffectiveConfig:
    """Env defaults overridden by the user's own /settings values."""
    if user is None:
        return EffectiveConfig(
            reminder_time=settings.reminder_time,
            writing_time=settings.writing_time,
            daily_new_limit=settings.daily_new_limit,
            session_max=settings.session_max,
        )
    return EffectiveConfig(
        reminder_time=_time_or(user.reminder_time, settings.reminder_time),
        writing_time=_time_or(user.writing_time, settings.writing_time),
        daily_new_limit=_int_or(user.daily_new_limit, settings.daily_new_limit),
        session_max=_int_or(user.session_max, settings.session_max),
        level=user.level or "B1",
    )


async def get_effective_config(
    session: AsyncSession, settings: Settings, *, user_id: int
) -> EffectiveConfig:
    return config_for_user(await get_user(session, user_id), settings)


async def set_user_setting(
    session: AsyncSession, *, user_id: int, key: str, value: str
) -> bool:
    user = await session.get(User, user_id)
    if user is None:
        return False
    if key == "REMINDER_TIME":
        user.reminder_time = value
    elif key == "WRITING_TIME":
        user.writing_time = value
    elif key == "DAILY_NEW_LIMIT":
        user.daily_new_limit = int(value)
    elif key == "SESSION_MAX":
        user.session_max = int(value)
    else:
        return False
    return True
