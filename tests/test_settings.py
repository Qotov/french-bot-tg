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
from frbot.jobs.reminders import (
    REMINDER_JOB_ID,
    create_scheduler,
    setup_jobs,
)
from tests.fakes import ALLOWED_USER_ID, make_callback_query, make_message
from tests.test_jobs import fake_dispatcher


def make_state(bot) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID),
    )


async def test_settings_shows_current_values(fake_bot, session_factory, settings):
    state = make_state(fake_bot)
    await cmd_settings(make_message("/settings", bot=fake_bot), state, session_factory, settings)
    sent = fake_bot.session.sent_messages[0]
    assert "Настройки" in sent.text
    labels = [b.text for row in sent.reply_markup.inline_keyboard for b in row]
    assert "REMINDER_TIME: 08:30" in labels
    assert "WRITING_TIME: 19:00" in labels
    assert "DAILY_NEW_LIMIT: 15" in labels
    assert "SESSION_MAX: 30" in labels


async def test_edit_time_persists_and_reschedules(fake_bot, session_factory, settings):
    scheduler = create_scheduler(settings.tz)
    await setup_jobs(scheduler, fake_bot, fake_dispatcher(), session_factory, settings)
    scheduler.start(paused=True)
    try:
        state = make_state(fake_bot)
        await on_edit(make_callback_query("settings:edit:REMINDER_TIME", bot=fake_bot), state)
        assert await state.get_state() == SettingsStates.awaiting_value.state
        assert "ЧЧ:ММ" in fake_bot.session.sent_messages[-1].text

        await handle_value(
            make_message("07:00", bot=fake_bot),
            state,
            session_factory,
            settings,
            scheduler=scheduler,
        )
        async with session_factory() as session:
            stored = await repo.get_setting(session, "REMINDER_TIME")
        assert stored == "07:00"
        assert str(scheduler.get_job(REMINDER_JOB_ID).trigger) == "cron[hour='7', minute='0']"
        assert await state.get_state() is None
        confirmation = fake_bot.session.sent_messages[-1]
        assert "REMINDER_TIME = 07:00" in confirmation.text
        labels = [b.text for row in confirmation.reply_markup.inline_keyboard for b in row]
        assert "REMINDER_TIME: 07:00" in labels
    finally:
        scheduler.shutdown(wait=False)


async def test_edit_int_value(fake_bot, session_factory, settings):
    state = make_state(fake_bot)
    await on_edit(make_callback_query("settings:edit:SESSION_MAX", bot=fake_bot), state)
    await handle_value(
        make_message("12", bot=fake_bot), state, session_factory, settings, scheduler=None
    )
    async with session_factory() as session:
        cfg = await repo.get_effective_config(session, settings)
    assert cfg.session_max == 12


async def test_invalid_value_keeps_state(fake_bot, session_factory, settings):
    state = make_state(fake_bot)
    await on_edit(make_callback_query("settings:edit:REMINDER_TIME", bot=fake_bot), state)
    await handle_value(
        make_message("25:99", bot=fake_bot), state, session_factory, settings, scheduler=None
    )
    assert await state.get_state() == SettingsStates.awaiting_value.state
    assert "Не похоже" in fake_bot.session.sent_messages[-1].text
    async with session_factory() as session:
        assert await repo.get_setting(session, "REMINDER_TIME") is None


async def test_unknown_key_rejected(fake_bot, session_factory, settings):
    state = make_state(fake_bot)
    await on_edit(make_callback_query("settings:edit:HACK", bot=fake_bot), state)
    assert await state.get_state() is None
    answers = fake_bot.session.sent("AnswerCallbackQuery")
    assert any("Неизвестная" in (a.text or "") for a in answers)
