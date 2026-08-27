"""Regression tests for the pilot-review findings.

Each test pins one confirmed defect so it cannot come back.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update

from frbot.bot.handlers.system import cmd_feedback, cmd_start
from frbot.bot.middleware import AuthMiddleware
from frbot.db import repo
from frbot.db.models import Review, User
from frbot.jobs.reminders import _minutes_skipped, minute_tick
from tests.fakes import ALLOWED_USER_ID, make_user

GROUP_ID = -1007778889


def make_group_message(text: str, bot=None, user_id: int = ALLOWED_USER_ID) -> Message:
    message = Message(
        message_id=5,
        date=datetime.now(UTC),
        chat=Chat(id=GROUP_ID, type="supergroup"),
        from_user=make_user(user_id),
        text=text,
    )
    return message.as_(bot) if bot is not None else message


def command_obj(args: str | None):
    return SimpleNamespace(args=args)


# ------------------------------------------------- group chats leak private data


async def test_start_in_a_group_does_not_repoint_delivery_chat(
    fake_bot, session_factory, settings, user
):
    """The leak: /start in a shared chat used to move a participant's reminders,
    writing prompts and stats into that group for everyone to read."""
    await cmd_start(
        make_group_message("/start@frbot", bot=fake_bot),
        command_obj(None),
        user,
        session_factory,
        settings,
    )
    async with session_factory() as session:
        row = await repo.get_user(session, ALLOWED_USER_ID)
    assert row.chat_id == ALLOWED_USER_ID  # still the private chat


async def test_middleware_ignores_group_updates(settings, session_factory, user):
    called = False

    async def handler(event, data):
        nonlocal called
        called = True

    mw = AuthMiddleware(settings, session_factory)
    event = Update(update_id=1, message=make_group_message("/review"))
    result = await mw(handler, event, {"event_from_user": make_user(ALLOWED_USER_ID)})
    assert not called
    assert result is None


async def test_middleware_still_allows_private_updates(settings, session_factory, user):
    from tests.fakes import make_update_with_message

    called = False

    async def handler(event, data):
        nonlocal called
        called = True

    mw = AuthMiddleware(settings, session_factory)
    event = make_update_with_message("/review")
    await mw(handler, event, {"event_from_user": make_user(ALLOWED_USER_ID)})
    assert called


# --------------------------------------------- one busy user must not starve the rest


async def test_tick_is_not_stalled_by_a_user_holding_their_lock(
    fake_bot, session_factory, settings
):
    """A user mid-LLM-call holds their FSM lock; the tick must still deliver to
    everyone else within the minute instead of blocking on them."""
    from frbot.jobs.reminders import _sent_today

    _sent_today.clear()
    hh_mm = datetime.now(ZoneInfo(settings.tz)).strftime("%H:%M")
    async with session_factory() as session:
        session.add(User(id=ALLOWED_USER_ID, chat_id=ALLOWED_USER_ID, writing_time=hh_mm))
        session.add(User(id=222, chat_id=222, reminder_time=hh_mm))
        await session.commit()

    from aiogram.fsm.storage.memory import SimpleEventIsolation

    isolation = SimpleEventIsolation()
    dispatcher = SimpleNamespace(
        storage=MemoryStorage(), fsm=SimpleNamespace(events_isolation=isolation)
    )
    busy_key = StorageKey(bot_id=fake_bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID)

    async with isolation.lock(busy_key):  # the blocked user, lock held throughout
        await minute_tick(fake_bot, dispatcher, session_factory, settings)
        # The tick returned without waiting; the free user is served.
        for _ in range(50):
            if any(m.chat_id == 222 for m in fake_bot.session.sent_messages):
                break
            await _yield()
    assert any(m.chat_id == 222 for m in fake_bot.session.sent_messages)


async def _yield() -> None:
    import asyncio

    await asyncio.sleep(0.01)


# ------------------------------------------- writing prompt must not trap the user


async def test_writing_state_is_not_armed_when_the_prompt_fails_to_send(
    fake_bot, session_factory, settings, user
):
    from frbot.bot.handlers.write import WriteStates, start_writing

    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=fake_bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID),
    )

    async def failing_answer(text: str, **kwargs):
        raise RuntimeError("blocked by the user")

    with pytest.raises(RuntimeError):
        await start_writing(failing_answer, state, user, session_factory, settings)
    # Not left waiting for an answer to a prompt that never arrived.
    assert await state.get_state() != WriteStates.awaiting_answer.state


# --------------------------------------------- feedback must not swallow a flow's reply


async def test_feedback_refuses_while_another_flow_is_waiting(fake_bot, session_factory, user):
    from frbot.bot.handlers.topic import TopicStates
    from tests.fakes import make_message

    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=fake_bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID),
    )
    await state.set_state(TopicStates.choosing)
    await cmd_feedback(make_message("/feedback", bot=fake_bot), state)

    assert (await state.get_data()).get("awaiting_feedback") is not True
    assert "сессия" in fake_bot.session.sent_messages[-1].text.lower()


# ------------------------------------------------------------- retention metric


async def test_active_days_are_counted_in_the_configured_timezone(session_factory):
    """Two reviews on the same Paris day must count as one day, even though
    they straddle midnight UTC."""
    async with session_factory() as session:
        session.add(User(id=ALLOWED_USER_ID, chat_id=ALLOWED_USER_ID))
        # 00:30 and 23:00 Paris on 2026-07-06 -> 22:30Z on the 5th and 21:00Z on the 6th
        session.add(
            Review(
                user_id=ALLOWED_USER_ID,
                card_id=1,
                rating=3,
                reviewed_at=datetime(2026, 7, 5, 22, 30, tzinfo=UTC),
                elapsed_days=0.0,
            )
        )
        session.add(
            Review(
                user_id=ALLOWED_USER_ID,
                card_id=1,
                rating=3,
                reviewed_at=datetime(2026, 7, 6, 21, 0, tzinfo=UTC),
                elapsed_days=0.0,
            )
        )
        await session.commit()
        days = await repo.count_active_days(
            session,
            user_id=ALLOWED_USER_ID,
            since=datetime(2026, 7, 1, tzinfo=UTC),
            tz="Europe/Paris",
        )
    assert days == 1


# ------------------------------------------------------------------------- DST


def test_minutes_skipped_covers_the_spring_forward_gap():
    paris = ZoneInfo("Europe/Paris")
    before = datetime(2026, 3, 29, 1, 59, tzinfo=paris)
    after = datetime(2026, 3, 29, 3, 0, tzinfo=paris)  # 02:00-02:59 never happens
    skipped = _minutes_skipped(before, after)
    assert "02:30" in skipped
    assert "02:00" in skipped


def test_minutes_skipped_ignores_a_normal_tick_and_a_long_outage():
    paris = ZoneInfo("Europe/Paris")
    base = datetime(2026, 5, 1, 10, 0, tzinfo=paris)
    assert _minutes_skipped(base, base + timedelta(minutes=1)) == set()
    assert _minutes_skipped(base, base + timedelta(hours=8)) == set()
    assert _minutes_skipped(None, base) == set()
