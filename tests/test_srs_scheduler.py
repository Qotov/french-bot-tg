from datetime import UTC, datetime, timedelta

from fsrs import Card as FsrsCard

from frbot.db.models import CardState
from frbot.srs.scheduler import SrsScheduler

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def test_new_card_is_new_and_due_now():
    srs = SrsScheduler(desired_retention=0.9)
    new = srs.new_card()
    assert new.state == CardState.new.value
    assert new.fsrs["last_review"] is None
    assert new.due <= datetime.now(UTC)


def test_round_trip_through_dict():
    srs = SrsScheduler(desired_retention=0.9)
    new = srs.new_card()
    result = srs.review(new.fsrs, 3, now=NOW)
    restored = FsrsCard.from_dict(result.fsrs)
    assert restored.due == result.due
    assert restored.last_review == NOW


def test_rating_moves_due_forward():
    srs = SrsScheduler(desired_retention=0.9)
    new = srs.new_card()
    result = srs.review(new.fsrs, 3, now=NOW)
    assert result.due > NOW
    later = srs.review(result.fsrs, 3, now=NOW + timedelta(days=1))
    assert later.due > result.due


def test_good_gets_later_due_than_again():
    srs = SrsScheduler(desired_retention=0.9)
    new = srs.new_card()
    good = srs.review(new.fsrs, 3, now=NOW)
    again = srs.review(new.fsrs, 1, now=NOW)
    assert good.due > again.due


def test_easy_gets_later_due_than_good():
    srs = SrsScheduler(desired_retention=0.9)
    new = srs.new_card()
    good = srs.review(new.fsrs, 3, now=NOW)
    easy = srs.review(new.fsrs, 4, now=NOW)
    assert easy.due > good.due


def test_state_leaves_new_after_first_review():
    srs = SrsScheduler(desired_retention=0.9)
    new = srs.new_card()
    result = srs.review(new.fsrs, 3, now=NOW)
    assert result.state in (
        CardState.learning.value,
        CardState.review.value,
    )
    assert result.state != CardState.new.value


def test_elapsed_days_computed_from_last_review():
    srs = SrsScheduler(desired_retention=0.9)
    new = srs.new_card()
    first = srs.review(new.fsrs, 3, now=NOW)
    assert first.elapsed_days == 0.0
    second = srs.review(first.fsrs, 3, now=NOW + timedelta(days=3))
    assert abs(second.elapsed_days - 3.0) < 0.01
