from datetime import UTC, datetime, timedelta

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, select

from frbot.bot.handlers.review import (
    EMPTY_TEXT,
    ReviewStates,
    cmd_review,
    on_grade,
    on_show,
    on_start_callback,
)
from frbot.db import repo
from frbot.db.models import CardState, Review
from frbot.srs.scheduler import SrsScheduler
from tests.fakes import ALLOWED_USER_ID, add_vocab_card, make_callback_query, make_message


def make_state(bot) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID),
    )


def now() -> datetime:
    return datetime.now(UTC)


def srs() -> SrsScheduler:
    return SrsScheduler(desired_retention=0.9)


async def test_review_with_no_cards_says_empty(fake_bot, session_factory, settings):
    state = make_state(fake_bot)
    await cmd_review(make_message("/review", bot=fake_bot), state, session_factory, settings)
    assert fake_bot.session.sent_messages[0].text == EMPTY_TEXT
    assert await state.get_state() is None


async def test_full_session_two_cards(fake_bot, session_factory, settings):
    due_id = await add_vocab_card(
        session_factory, "marché", reviewed_days_ago=2, due=now() - timedelta(hours=1)
    )
    new_id = await add_vocab_card(session_factory, "boulangerie")
    state = make_state(fake_bot)

    await cmd_review(make_message("/review", bot=fake_bot), state, session_factory, settings)
    assert await state.get_state() == ReviewStates.reviewing.state
    intro, first_front = fake_bot.session.sent_messages[:2]
    assert "1 по расписанию" in intro.text
    assert "1 новых" in intro.text
    assert "1/2" in first_front.text
    assert "marché" in first_front.text
    assert "Show answer" in first_front.reply_markup.inline_keyboard[0][0].text

    # Show answer for the due card.
    await on_show(
        make_callback_query(f"review:show:{due_id}", bot=fake_bot),
        state,
        session_factory,
        settings,
    )
    shown = fake_bot.session.sent("EditMessageText")[-1]
    assert "по мере" in shown.text  # back contains the translation
    grade_labels = [b.text for row in shown.reply_markup.inline_keyboard for b in row]
    assert grade_labels == ["Again", "Hard", "Good", "Easy"]

    # Grade Good -> next card arrives.
    await on_grade(
        make_callback_query(f"review:grade:{due_id}:3", bot=fake_bot),
        state,
        session_factory,
        srs(),
        settings,
    )
    second_front = fake_bot.session.sent_messages[-1]
    assert "2/2" in second_front.text
    assert "boulangerie" in second_front.text

    # Show + grade Again on the new card -> summary.
    await on_show(
        make_callback_query(f"review:show:{new_id}", bot=fake_bot),
        state,
        session_factory,
        settings,
    )
    await on_grade(
        make_callback_query(f"review:grade:{new_id}:1", bot=fake_bot),
        state,
        session_factory,
        srs(),
        settings,
    )
    summary = fake_bot.session.sent_messages[-1]
    assert "Повторено: 2" in summary.text
    assert "Again: 1" in summary.text
    assert await state.get_state() is None

    # DB effects: reviews logged, due/state updated.
    async with session_factory() as session:
        count = (await session.execute(select(func.count(Review.id)))).scalar_one()
        due_card = await repo.get_card(session, due_id)
        new_card = await repo.get_card(session, new_id)
    assert count == 2
    assert due_card.due > now() - timedelta(minutes=1)
    assert new_card.state != CardState.new.value
    assert new_card.fsrs["last_review"] is not None


async def test_stale_grade_press_is_ignored(fake_bot, session_factory, settings):
    first_id = await add_vocab_card(
        session_factory, "premier", reviewed_days_ago=2, due=now() - timedelta(hours=2)
    )
    await add_vocab_card(
        session_factory, "deuxième", reviewed_days_ago=2, due=now() - timedelta(hours=1)
    )
    state = make_state(fake_bot)
    await cmd_review(make_message("/review", bot=fake_bot), state, session_factory, settings)
    await on_grade(
        make_callback_query(f"review:grade:{first_id}:3", bot=fake_bot),
        state,
        session_factory,
        srs(),
        settings,
    )
    # Second press on the already-graded card must not double-log.
    await on_grade(
        make_callback_query(f"review:grade:{first_id}:1", bot=fake_bot),
        state,
        session_factory,
        srs(),
        settings,
    )
    async with session_factory() as session:
        count = (await session.execute(select(func.count(Review.id)))).scalar_one()
    assert count == 1


async def test_grade_without_session_asks_to_restart(fake_bot, session_factory, settings):
    card_id = await add_vocab_card(session_factory, "seul")
    state = make_state(fake_bot)
    await on_grade(
        make_callback_query(f"review:grade:{card_id}:3", bot=fake_bot),
        state,
        session_factory,
        srs(),
        settings,
    )
    async with session_factory() as session:
        count = (await session.execute(select(func.count(Review.id)))).scalar_one()
    assert count == 0
    answers = fake_bot.session.sent("AnswerCallbackQuery")
    assert any("не активна" in (a.text or "") for a in answers)


async def test_start_callback_starts_session(fake_bot, session_factory, settings):
    await add_vocab_card(session_factory, "rappel")
    state = make_state(fake_bot)
    await on_start_callback(
        make_callback_query("review:start", bot=fake_bot), state, session_factory, settings
    )
    assert await state.get_state() == ReviewStates.reviewing.state
    assert any("1/1" in (m.text or "") for m in fake_bot.session.sent_messages)
