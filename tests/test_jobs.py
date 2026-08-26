from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from aiogram.fsm.storage.memory import MemoryStorage

from frbot.config import Settings
from frbot.db import repo
from frbot.jobs.reminders import (
    BACKUP_JOB_ID,
    REMINDER_JOB_ID,
    WEEKLY_JOB_ID,
    WRITING_JOB_ID,
    backup_database,
    create_scheduler,
    reschedule_daily_job,
    send_reminder,
    send_weekly_summary,
    send_writing_prompt,
    setup_jobs,
)
from tests.fakes import ALLOWED_USER_ID, add_vocab_card


def now() -> datetime:
    return datetime.now(UTC)


def fake_dispatcher() -> SimpleNamespace:
    return SimpleNamespace(storage=MemoryStorage())


async def store_chat_id(session_factory) -> None:
    async with session_factory() as session:
        await repo.set_setting(session, repo.CHAT_ID_KEY, str(ALLOWED_USER_ID))
        await session.commit()


async def test_reminder_sends_due_count_with_button(fake_bot, session_factory, settings):
    await store_chat_id(session_factory)
    for i in range(2):
        await add_vocab_card(
            session_factory, f"dû-{i}", reviewed_days_ago=2, due=now() - timedelta(hours=1)
        )
    await add_vocab_card(session_factory, "nouveau")  # New cards don't count as due

    await send_reminder(fake_bot, session_factory, settings)
    sent = fake_bot.session.sent_messages
    assert len(sent) == 1
    assert sent[0].chat_id == ALLOWED_USER_ID
    assert "2" in sent[0].text
    assert "Start review" in sent[0].reply_markup.inline_keyboard[0][0].text


async def test_reminder_without_due_cards_has_no_button(fake_bot, session_factory, settings):
    await store_chat_id(session_factory)
    await send_reminder(fake_bot, session_factory, settings)
    sent = fake_bot.session.sent_messages
    assert len(sent) == 1
    assert sent[0].reply_markup is None


async def test_reminder_skipped_without_chat_id(fake_bot, session_factory, settings):
    await send_reminder(fake_bot, session_factory, settings)
    assert fake_bot.session.sent_messages == []


async def test_backup_copies_and_prunes(tmp_path: Path, settings):
    import sqlite3

    db_file = tmp_path / "frbot.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()
    conn.close()
    backups = tmp_path / "backups"
    backups.mkdir()
    # 16 pre-existing backups with older dates.
    for i in range(16):
        (backups / f"frbot-2026-07-{i + 1:02d}.db").write_bytes(b"old")

    test_settings = settings.model_copy(update={"db_url": f"sqlite+aiosqlite:///{db_file}"})
    await backup_database(test_settings)

    remaining = sorted(backups.glob("frbot-*.db"))
    assert len(remaining) == 14
    # The newest backup is a valid sqlite copy of today's DB.
    newest = remaining[-1]
    check = sqlite3.connect(newest)
    assert check.execute("SELECT x FROM t").fetchone() == (42,)
    check.close()


async def test_backup_skips_memory_url(settings):
    test_settings = settings.model_copy(update={"db_url": "sqlite+aiosqlite:///:memory:"})
    await backup_database(test_settings)  # must not raise


async def test_setup_and_reschedule_jobs(fake_bot, session_factory, settings: Settings):
    scheduler = create_scheduler(settings.tz)
    await setup_jobs(scheduler, fake_bot, fake_dispatcher(), session_factory, settings)
    scheduler.start(paused=True)
    try:
        reminder = scheduler.get_job(REMINDER_JOB_ID)
        writing = scheduler.get_job(WRITING_JOB_ID)
        backup = scheduler.get_job(BACKUP_JOB_ID)
        assert reminder is not None
        assert writing is not None
        assert backup is not None
        assert str(reminder.trigger) == "cron[hour='8', minute='30']"
        assert str(writing.trigger) == "cron[hour='19', minute='0']"
        # Triggers must carry the configured tz, not the host OS timezone.
        from zoneinfo import ZoneInfo

        for job in (reminder, writing, backup, scheduler.get_job(WEEKLY_JOB_ID)):
            assert job.trigger.timezone == ZoneInfo(settings.tz)

        reschedule_daily_job(scheduler, REMINDER_JOB_ID, "10:45", settings.tz)
        reminder = scheduler.get_job(REMINDER_JOB_ID)
        assert str(reminder.trigger) == "cron[hour='10', minute='45']"
        assert reminder.trigger.timezone == ZoneInfo(settings.tz)
    finally:
        scheduler.shutdown(wait=False)


async def test_setup_jobs_respects_runtime_override(fake_bot, session_factory, settings):
    async with session_factory() as session:
        await repo.set_setting(session, "REMINDER_TIME", "06:05")
        await session.commit()
    scheduler = create_scheduler(settings.tz)
    await setup_jobs(scheduler, fake_bot, fake_dispatcher(), session_factory, settings)
    scheduler.start(paused=True)
    try:
        assert str(scheduler.get_job(REMINDER_JOB_ID).trigger) == "cron[hour='6', minute='5']"
    finally:
        scheduler.shutdown(wait=False)


async def test_writing_prompt_job_sends_and_sets_state(fake_bot, session_factory, settings):
    from frbot.bot.handlers.write import WriteStates

    await store_chat_id(session_factory)
    dispatcher = fake_dispatcher()
    await send_writing_prompt(fake_bot, dispatcher, session_factory, settings)

    sent = fake_bot.session.sent_messages
    assert len(sent) == 1
    assert "Задание" in sent[0].text

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey

    state = FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=fake_bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID),
    )
    assert await state.get_state() == WriteStates.awaiting_answer.state


async def test_writing_prompt_job_skipped_without_chat_id(fake_bot, session_factory, settings):
    await send_writing_prompt(fake_bot, fake_dispatcher(), session_factory, settings)
    assert fake_bot.session.sent_messages == []


async def test_writing_prompt_job_does_not_stomp_active_session(
    fake_bot, session_factory, settings
):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey

    from frbot.bot.handlers.review import ReviewStates

    await store_chat_id(session_factory)
    dispatcher = fake_dispatcher()
    state = FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=fake_bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID),
    )
    await state.set_state(ReviewStates.reviewing)
    await state.set_data({"queue": [1], "index": 0, "total": 1, "reviewed": 0, "again": 0})

    await send_writing_prompt(fake_bot, dispatcher, session_factory, settings)

    # The review session is untouched; the user got a nudge instead of a prompt.
    assert await state.get_state() == ReviewStates.reviewing.state
    assert (await state.get_data())["queue"] == [1]
    sent = fake_bot.session.sent_messages
    assert len(sent) == 1
    assert "Заверши текущую" in sent[0].text


async def test_weekly_summary_sends_stats_and_rotates_topic(fake_bot, session_factory, settings):
    await store_chat_id(session_factory)
    await send_weekly_summary(fake_bot, session_factory, settings)

    sent = fake_bot.session.sent_messages
    assert len(sent) == 2
    assert "Статистика" in sent[0].text
    assert "тема недели" in sent[1].text
    assert "Avoir ou être" in sent[1].text  # first rotation activates position 1

    await send_weekly_summary(fake_bot, session_factory, settings)
    assert "genre des noms" in fake_bot.session.sent_messages[-1].text

    async with session_factory() as session:
        active = await repo.get_active_drill_topic(session)
    assert active.slug == "genre-des-noms"


async def test_weekly_job_scheduled_on_sunday(fake_bot, session_factory, settings):
    scheduler = create_scheduler(settings.tz)
    await setup_jobs(scheduler, fake_bot, fake_dispatcher(), session_factory, settings)
    scheduler.start(paused=True)
    try:
        weekly = scheduler.get_job(WEEKLY_JOB_ID)
        assert weekly is not None
        assert "day_of_week='sun'" in str(weekly.trigger)
        assert "hour='18'" in str(weekly.trigger)
    finally:
        scheduler.shutdown(wait=False)
