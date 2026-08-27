from datetime import UTC, datetime, timedelta

from frbot.db import repo
from frbot.srs.queue import build_queue
from frbot.srs.scheduler import ReviewResult
from tests.fakes import ALLOWED_USER_ID, add_vocab_card

TZ = "Europe/Paris"


def now() -> datetime:
    return datetime.now(UTC)


async def test_due_cards_come_first_ordered_by_due(session_factory):
    later = await add_vocab_card(
        session_factory, "plus-tard", reviewed_days_ago=2, due=now() - timedelta(hours=1)
    )
    earlier = await add_vocab_card(
        session_factory, "plus-tôt", reviewed_days_ago=5, due=now() - timedelta(days=2)
    )
    new = await add_vocab_card(session_factory, "nouveau")

    async with session_factory() as session:
        queue = await build_queue(
            session, user_id=ALLOWED_USER_ID, now=now(), tz=TZ, session_max=30, daily_new_limit=15
        )
    assert queue.card_ids == [earlier, later, new]
    assert queue.due_count == 2
    assert queue.new_count == 1


async def test_future_due_cards_excluded(session_factory):
    await add_vocab_card(
        session_factory, "futur", reviewed_days_ago=1, due=now() + timedelta(days=3)
    )
    async with session_factory() as session:
        queue = await build_queue(
            session, user_id=ALLOWED_USER_ID, now=now(), tz=TZ, session_max=30, daily_new_limit=15
        )
    assert queue.total == 0


async def test_suspended_cards_excluded(session_factory):
    await add_vocab_card(
        session_factory,
        "suspendu",
        reviewed_days_ago=2,
        due=now() - timedelta(hours=1),
        suspended=True,
    )
    await add_vocab_card(session_factory, "nouveau-suspendu", suspended=True)
    async with session_factory() as session:
        queue = await build_queue(
            session, user_id=ALLOWED_USER_ID, now=now(), tz=TZ, session_max=30, daily_new_limit=15
        )
    assert queue.total == 0


async def test_session_max_caps_due_cards(session_factory):
    for i in range(5):
        await add_vocab_card(
            session_factory,
            f"mot-{i}",
            reviewed_days_ago=2,
            due=now() - timedelta(hours=5 - i),
        )
    async with session_factory() as session:
        queue = await build_queue(
            session, user_id=ALLOWED_USER_ID, now=now(), tz=TZ, session_max=3, daily_new_limit=15
        )
    assert queue.total == 3
    assert queue.due_count == 3
    assert queue.new_count == 0


async def test_new_cards_fill_remaining_slots_up_to_daily_limit(session_factory):
    for i in range(2):
        await add_vocab_card(
            session_factory, f"dû-{i}", reviewed_days_ago=2, due=now() - timedelta(hours=1)
        )
    for i in range(10):
        await add_vocab_card(session_factory, f"nouveau-{i}")

    async with session_factory() as session:
        queue = await build_queue(
            session, user_id=ALLOWED_USER_ID, now=now(), tz=TZ, session_max=30, daily_new_limit=4
        )
    assert queue.due_count == 2
    assert queue.new_count == 4  # daily_new_limit wins over remaining slots

    async with session_factory() as session:
        queue = await build_queue(
            session, user_id=ALLOWED_USER_ID, now=now(), tz=TZ, session_max=5, daily_new_limit=15
        )
    assert queue.due_count == 2
    assert queue.new_count == 3  # session_max wins


async def test_daily_new_limit_counts_cards_introduced_today(session_factory):
    # Fixed clock: noon UTC = 14:00 in Paris, safely inside one Paris day.
    fixed_now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    # Three cards got their first review earlier the same Paris day.
    for i in range(3):
        card_id = await add_vocab_card(
            session_factory, f"intro-{i}", reviewed_days_ago=2, due=fixed_now - timedelta(hours=1)
        )
        async with session_factory() as session:
            card = await repo.get_card(session, card_id, user_id=ALLOWED_USER_ID)
            result = ReviewResult(fsrs=card.fsrs, due=card.due, state=card.state, elapsed_days=0.0)
            await repo.apply_review(
                session,
                card,
                result,
                user_id=ALLOWED_USER_ID,
                rating=3,
                now=fixed_now - timedelta(hours=2),
            )
            await session.commit()
    for i in range(5):
        await add_vocab_card(session_factory, f"nouveau-{i}")

    async with session_factory() as session:
        queue = await build_queue(
            session,
            user_id=ALLOWED_USER_ID,
            now=fixed_now,
            tz=TZ,
            session_max=30,
            daily_new_limit=4,
        )
    # 3 of the 4 allowed new introductions are used up.
    assert queue.new_count == 1


async def test_yesterdays_introductions_do_not_count(session_factory):
    card_id = await add_vocab_card(session_factory, "hier", reviewed_days_ago=2)
    async with session_factory() as session:
        card = await repo.get_card(session, card_id, user_id=ALLOWED_USER_ID)
        result = ReviewResult(fsrs=card.fsrs, due=card.due, state=card.state, elapsed_days=0.0)
        await repo.apply_review(
            session, card, result, user_id=ALLOWED_USER_ID, rating=3, now=now() - timedelta(days=2)
        )
        await session.commit()
    for i in range(5):
        await add_vocab_card(session_factory, f"nouveau-{i}")

    async with session_factory() as session:
        queue = await build_queue(
            session, user_id=ALLOWED_USER_ID, now=now(), tz=TZ, session_max=30, daily_new_limit=3
        )
    assert queue.new_count == 3
