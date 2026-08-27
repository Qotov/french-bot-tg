"""SQLAlchemy models. All timestamps are stored as UTC.

SQLite has no timezone-aware datetime storage, so UTCDateTime strips tzinfo on
write (after converting to UTC) and re-attaches UTC on read. Everything above
the DB layer works with aware datetimes.
"""

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, BigInteger, Date, DateTime, ForeignKey, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class CardKind(StrEnum):
    vocab = "vocab"
    error = "error"
    drill_error = "drill_error"


class CardState(StrEnum):
    new = "New"
    learning = "Learning"
    review = "Review"
    relearning = "Relearning"


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


class Card(Base):
    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    text: Mapped[str]
    lemma: Mapped[str] = mapped_column(index=True)
    kind: Mapped[str]  # CardKind
    enrichment: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    fsrs: Mapped[dict] = mapped_column(JSON)
    due: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    state: Mapped[str]  # CardState
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    suspended: Mapped[bool] = mapped_column(default=False)

    reviews: Mapped[list["Review"]] = relationship(
        back_populates="card", cascade="all, delete-orphan"
    )


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id", ondelete="CASCADE"), index=True)
    rating: Mapped[int]  # 1-4
    reviewed_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    elapsed_days: Mapped[float]

    card: Mapped[Card] = relationship(back_populates="reviews")


class Writing(Base):
    __tablename__ = "writings"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    prompt: Mapped[str]
    answer: Mapped[str | None] = mapped_column(nullable=True)
    corrections: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DrillTopic(Base):
    __tablename__ = "drill_topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(unique=True)
    title_fr: Mapped[str]
    position: Mapped[int]
    active_week: Mapped[date | None] = mapped_column(Date, nullable=True)


LEVELS = ("A2", "B1", "B2")


class User(Base):
    """A pilot participant. id is the Telegram user id. The nullable settings
    columns override the .env defaults when set (see repo.config_for_user)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(nullable=True)
    first_name: Mapped[str | None] = mapped_column(nullable=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    level: Mapped[str] = mapped_column(default="B1")  # one of LEVELS
    invite_code: Mapped[str | None] = mapped_column(nullable=True)
    is_admin: Mapped[bool] = mapped_column(default=False)
    active: Mapped[bool] = mapped_column(default=True)
    tz: Mapped[str | None] = mapped_column(nullable=True)  # IANA name
    reminder_time: Mapped[str | None] = mapped_column(nullable=True)
    writing_time: Mapped[str | None] = mapped_column(nullable=True)
    daily_new_limit: Mapped[int | None] = mapped_column(nullable=True)
    session_max: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Invite(Base):
    __tablename__ = "invites"

    code: Mapped[str] = mapped_column(primary_key=True)
    created_by: Mapped[int] = mapped_column(BigInteger)
    max_uses: Mapped[int] = mapped_column(default=1)
    used_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str]
