"""Scheduled jobs: the per-user minute tick, weekly summary, backup, cleanup."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from aiogram.fsm.storage.memory import MemoryStorage

from frbot.config import Settings
from frbot.db import repo
from frbot.db.models import User
from frbot.jobs.reminders import (
    BACKUP_JOB_ID,
    CLEANUP_JOB_ID,
    TICK_JOB_ID,
    WEEKLY_JOB_ID,
    _sent_today,
    backup_database,
    cleanup_stray_fsm_entries,
    create_scheduler,
    minute_tick,
    send_due_reminder,
    send_weekly_summary,
    send_writing_prompt,
    setup_jobs,
)
from tests.fakes import ALLOWED_USER_ID, add_vocab_card


def now() -> datetime:
    return datetime.now(UTC)


def fake_dispatcher() -> SimpleNamespace:
    return SimpleNamespace(storage=MemoryStorage())


async def add_user(
    session_factory,
    user_id: int,
    *,
    reminder_time: str | None = None,
    writing_time: str | None = None,
) -> User:
    row = User(
        id=user_id,
        chat_id=user_id,
        reminder_time=reminder_time,
        writing_time=writing_time,
    )
    async with session_factory() as session:
        session.add(row)
        await session.commit()
    return row


def local_hh_mm(settings: Settings, offset_minutes: int = 0) -> str:
    stamp = datetime.now(ZoneInfo(settings.tz)) + timedelta(minutes=offset_minutes)
    return stamp.strftime("%H:%M")


# ------------------------------------------------------------------ reminders


async def test_reminder_reports_due_count_with_button(fake_bot, session_factory, settings):
    user = await add_user(session_factory, ALLOWED_USER_ID)
    for i in range(2):
        await add_vocab_card(
            session_factory, f"dû-{i}", reviewed_days_ago=2, due=now() - timedelta(hours=1)
        )
    await add_vocab_card(session_factory, "nouveau")  # New cards don't count as due

    await send_due_reminder(fake_bot, user, session_factory)
    sent = fake_bot.session.sent_messages
    assert len(sent) == 1
    assert sent[0].chat_id == ALLOWED_USER_ID
    assert "2" in sent[0].text
    assert "Start review" in sent[0].reply_markup.inline_keyboard[0][0].text


async def test_reminder_without_due_cards_has_no_button(fake_bot, session_factory, settings):
    user = await add_user(session_factory, ALLOWED_USER_ID)
    await send_due_reminder(fake_bot, user, session_factory)
    assert fake_bot.session.sent_messages[0].reply_markup is None


# ------------------------------------------------------------------ the tick


async def test_tick_delivers_only_at_the_users_own_time(fake_bot, session_factory, settings):
    _sent_today.clear()
    await add_user(session_factory, ALLOWED_USER_ID, reminder_time=local_hh_mm(settings))
    await add_user(session_factory, 222, reminder_time=local_hh_mm(settings, 30))

    await minute_tick(fake_bot, fake_dispatcher(), session_factory, settings)

    sent = fake_bot.session.sent_messages
    assert len(sent) == 1
    assert sent[0].chat_id == ALLOWED_USER_ID


async def test_tick_does_not_send_twice_in_one_day(fake_bot, session_factory, settings):
    _sent_today.clear()
    await add_user(session_factory, ALLOWED_USER_ID, reminder_time=local_hh_mm(settings))

    await minute_tick(fake_bot, fake_dispatcher(), session_factory, settings)
    await minute_tick(fake_bot, fake_dispatcher(), session_factory, settings)

    assert len(fake_bot.session.sent_messages) == 1


async def test_tick_sends_writing_prompt_at_writing_time(fake_bot, session_factory, settings):
    _sent_today.clear()
    await add_user(
        session_factory, ALLOWED_USER_ID, reminder_time="03:03", writing_time=local_hh_mm(settings)
    )
    await minute_tick(fake_bot, fake_dispatcher(), session_factory, settings)
    assert any("Задание" in (m.text or "") for m in fake_bot.session.sent_messages)


async def test_tick_skips_inactive_users(fake_bot, session_factory, settings):
    _sent_today.clear()
    async with session_factory() as session:
        session.add(
            User(
                id=ALLOWED_USER_ID,
                chat_id=ALLOWED_USER_ID,
                reminder_time=local_hh_mm(settings),
                active=False,
            )
        )
        await session.commit()
    await minute_tick(fake_bot, fake_dispatcher(), session_factory, settings)
    assert fake_bot.session.sent_messages == []


async def test_tick_survives_a_failing_user(fake_bot, session_factory, settings):
    """One user's delivery blowing up must not stop the others."""
    _sent_today.clear()
    hh_mm = local_hh_mm(settings)
    await add_user(session_factory, ALLOWED_USER_ID, reminder_time=hh_mm)
    await add_user(session_factory, 333, reminder_time=hh_mm)

    original = fake_bot.session._result_for
    calls = {"n": 0}

    def flaky(method):
        calls["n"] += 1
        if calls["n"] == 1 and type(method).__name__ == "SendMessage":
            raise RuntimeError("telegram is unhappy")
        return original(method)

    fake_bot.session._result_for = flaky
    await minute_tick(fake_bot, fake_dispatcher(), session_factory, settings)
    # The second user still got their reminder.
    assert len(fake_bot.session.sent_messages) == 2


# --------------------------------------------------------------- writing job


async def test_writing_prompt_job_does_not_stomp_active_session(
    fake_bot, session_factory, settings
):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey

    from frbot.bot.handlers.review import ReviewStates

    user = await add_user(session_factory, ALLOWED_USER_ID)
    dispatcher = fake_dispatcher()
    state = FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=fake_bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID),
    )
    await state.set_state(ReviewStates.reviewing)
    await state.set_data({"queue": [1], "index": 0, "total": 1, "reviewed": 0, "again": 0})

    await send_writing_prompt(fake_bot, dispatcher, user, session_factory, settings)

    assert await state.get_state() == ReviewStates.reviewing.state
    assert (await state.get_data())["queue"] == [1]
    assert "Заверши текущую" in fake_bot.session.sent_messages[-1].text


# ------------------------------------------------------------------- weekly


async def test_weekly_summary_reaches_every_user(fake_bot, session_factory, settings):
    await add_user(session_factory, ALLOWED_USER_ID)
    await add_user(session_factory, 222)
    await send_weekly_summary(fake_bot, session_factory, settings)

    sent = fake_bot.session.sent_messages
    # Two messages each: stats + next week's topic.
    assert len(sent) == 4
    assert any("Итоги недели" in (m.text or "") for m in sent)
    assert any("Тема следующей недели" in (m.text or "") for m in sent)
    recipients = {m.chat_id for m in sent}
    assert recipients == {ALLOWED_USER_ID, 222}


async def test_weekly_topic_is_deterministic_by_week(session_factory):
    from datetime import date

    async with session_factory() as session:
        await repo.ensure_drill_topics_seeded(session)
        await session.commit()
        # Same ISO week -> same topic; 10 weeks later -> same topic again (10 topics).
        a = await repo.get_topic_for_week(session, today=date(2026, 9, 1))
        b = await repo.get_topic_for_week(session, today=date(2026, 9, 3))
        c = await repo.get_topic_for_week(session, today=date(2026, 9, 8))
        d = await repo.get_topic_for_week(session, today=date(2026, 11, 10))
    assert a.slug == b.slug
    assert a.slug != c.slug
    assert a.slug == d.slug


# ------------------------------------------------------------------- backup


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
    for i in range(16):
        (backups / f"frbot-2026-07-{i + 1:02d}.db").write_bytes(b"old")

    test_settings = settings.model_copy(update={"db_url": f"sqlite+aiosqlite:///{db_file}"})
    await backup_database(test_settings)

    remaining = sorted(backups.glob("frbot-*.db"))
    assert len(remaining) == 14
    check = sqlite3.connect(remaining[-1])
    assert check.execute("SELECT x FROM t").fetchone() == (42,)
    check.close()


async def test_backup_skips_memory_url(settings):
    test_settings = settings.model_copy(update={"db_url": "sqlite+aiosqlite:///:memory:"})
    await backup_database(test_settings)  # must not raise


# ------------------------------------------------------------------ wiring


async def test_setup_registers_all_jobs_with_configured_tz(fake_bot, session_factory, settings):
    scheduler = create_scheduler(settings.tz)
    setup_jobs(scheduler, fake_bot, fake_dispatcher(), session_factory, settings)
    scheduler.start(paused=True)
    try:
        for job_id in (TICK_JOB_ID, WEEKLY_JOB_ID, BACKUP_JOB_ID, CLEANUP_JOB_ID):
            job = scheduler.get_job(job_id)
            assert job is not None, job_id
            assert job.trigger.timezone == ZoneInfo(settings.tz)
        assert "minute='*'" in str(scheduler.get_job(TICK_JOB_ID).trigger)
    finally:
        scheduler.shutdown(wait=False)


async def test_cleanup_prunes_entries_of_non_participants(fake_bot, session_factory, settings):
    from aiogram.fsm.storage.base import StorageKey

    await add_user(session_factory, ALLOWED_USER_ID)
    dispatcher = fake_dispatcher()
    storage = dispatcher.storage
    stranger = StorageKey(bot_id=42, chat_id=999, user_id=999)
    ours = StorageKey(bot_id=42, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID)
    await storage.set_state(stranger, "some:state")
    await storage.set_state(ours, "review:reviewing")

    await cleanup_stray_fsm_entries(dispatcher, session_factory)

    assert stranger not in storage.storage
    assert ours in storage.storage
