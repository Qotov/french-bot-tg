"""fsrs wrapper: serialization to/from cards.fsrs JSON and review updates.

py-fsrs has no New state (a fresh Card starts in Learning with no last_review),
so "New" is derived: a card that has never been reviewed is New.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from fsrs import Card as FsrsCard
from fsrs import Rating, Scheduler

from frbot.db.models import CardState


def state_name(card: FsrsCard) -> str:
    if card.last_review is None:
        return CardState.new.value
    return card.state.name  # Learning / Review / Relearning


@dataclass(frozen=True)
class NewCard:
    fsrs: dict
    due: datetime
    state: str


@dataclass(frozen=True)
class ReviewResult:
    fsrs: dict
    due: datetime
    state: str
    elapsed_days: float


class SrsScheduler:
    def __init__(self, desired_retention: float) -> None:
        self._scheduler = Scheduler(desired_retention=desired_retention)

    def new_card(self) -> NewCard:
        card = FsrsCard()
        return NewCard(fsrs=card.to_dict(), due=card.due, state=CardState.new.value)

    def review(self, fsrs_data: dict, rating: int, now: datetime | None = None) -> ReviewResult:
        if now is None:
            now = datetime.now(UTC)
        card = FsrsCard.from_dict(fsrs_data)
        elapsed_days = (
            0.0
            if card.last_review is None
            else max(0.0, (now - card.last_review).total_seconds() / 86400)
        )
        updated, _log = self._scheduler.review_card(card, Rating(rating), review_datetime=now)
        return ReviewResult(
            fsrs=updated.to_dict(),
            due=updated.due,
            state=state_name(updated),
            elapsed_days=elapsed_days,
        )
