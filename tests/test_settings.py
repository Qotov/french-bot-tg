"""/settings — now per user, stored on the user row."""

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from frbot.bot.handlers.settings import (
    SettingsStates,
    cmd_settings,
    handle_value,
    on_edit,
)
from frbot.db import repo
from frbot.db.models import User
from tests.fakes import ALLOWED_USER_ID, make_callback_query, make_message


def make_state(bot) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID),
    )


async def test_settings_shows_current_values(fake_bot, session_factory, settings, user):
    state = make_state(fake_bot)
    await cmd_settings(
        make_message("/settings", bot=fake_bot), state, user, session_factory, settings
    )
    sent = fake_bot.session.sent_messages[0]
    assert "Настройки" in sent.text
    labels = [b.text for row in sent.reply_markup.inline_keyboard for b in row]
    assert "REMINDER_TIME: 08:30" in labels
    assert "WRITING_TIME: 19:00" in labels
    assert "DAILY_NEW_LIMIT: 15" in labels
    assert "SESSION_MAX: 30" in labels


async def test_edit_time_persists_on_the_user_row(fake_bot, session_factory, settings, user):
    state = make_state(fake_bot)
    await on_edit(make_callback_query("settings:edit:REMINDER_TIME", bot=fake_bot), state)
    assert await state.get_state() == SettingsStates.awaiting_value.state
    assert "ЧЧ:ММ" in fake_bot.session.sent_messages[-1].text

    await handle_value(make_message("07:00", bot=fake_bot), state, user, session_factory, settings)
    async with session_factory() as session:
        row = await repo.get_user(session, user.id)
        cfg = await repo.get_effective_config(session, settings, user_id=user.id)
    assert row.reminder_time == "07:00"
    assert cfg.reminder_time == "07:00"  # picked up by the next minute tick
    assert await state.get_state() is None
    assert "REMINDER_TIME = 07:00" in fake_bot.session.sent_messages[-1].text


async def test_settings_are_isolated_between_users(fake_bot, session_factory, settings, user):
    """One participant's limits must never leak into another's."""
    other = User(id=999_001, chat_id=999_001)
    async with session_factory() as session:
        session.add(other)
        await session.commit()

    state = make_state(fake_bot)
    await on_edit(make_callback_query("settings:edit:SESSION_MAX", bot=fake_bot), state)
    await handle_value(make_message("12", bot=fake_bot), state, user, session_factory, settings)

    async with session_factory() as session:
        mine = await repo.get_effective_config(session, settings, user_id=user.id)
        theirs = await repo.get_effective_config(session, settings, user_id=other.id)
    assert mine.session_max == 12
    assert theirs.session_max == settings.session_max  # untouched default


async def test_invalid_value_keeps_state(fake_bot, session_factory, settings, user):
    state = make_state(fake_bot)
    await on_edit(make_callback_query("settings:edit:REMINDER_TIME", bot=fake_bot), state)
    await handle_value(make_message("25:99", bot=fake_bot), state, user, session_factory, settings)
    assert await state.get_state() == SettingsStates.awaiting_value.state
    assert "Не похоже" in fake_bot.session.sent_messages[-1].text
    async with session_factory() as session:
        row = await repo.get_user(session, user.id)
    assert row.reminder_time is None


async def test_unknown_key_rejected(fake_bot, session_factory, settings, user):
    state = make_state(fake_bot)
    await on_edit(make_callback_query("settings:edit:HACK", bot=fake_bot), state)
    assert await state.get_state() is None
    answers = fake_bot.session.sent("AnswerCallbackQuery")
    assert any("Неизвестная" in (a.text or "") for a in answers)
