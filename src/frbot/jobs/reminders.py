"""APScheduler jobs: daily reminder, daily writing prompt, nightly DB backup.

The scheduler runs in the configured timezone (Europe/Paris); job times read
runtime overrides from app_settings at setup, and /settings reschedules jobs
via reschedule_daily_job.
"""

import asyncio
import logging
import sqlite3
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from frbot.bot import render
from frbot.bot.handlers.write import start_writing
from frbot.bot.keyboards import start_review_kb
from frbot.config import Settings
from frbot.db import repo
from frbot.db.session import SessionFactory
from frbot.timeutil import day_end_utc


class HasStorage(Protocol):
    storage: BaseStorage


logger = logging.getLogger(__name__)

REMINDER_JOB_ID = "daily_reminder"
WRITING_JOB_ID = "daily_writing"
WEEKLY_JOB_ID = "weekly_stats"
BACKUP_JOB_ID = "daily_backup"
CLEANUP_JOB_ID = "daily_cleanup"
BACKUP_KEEP = 14
BACKUP_TIME = "03:00"
CLEANUP_TIME = "04:00"


def create_scheduler(tz: str) -> AsyncIOScheduler:
    return AsyncIOScheduler(timezone=ZoneInfo(tz))


def _daily(hh_mm: str, tz: str) -> CronTrigger:
    # APScheduler 3 applies the scheduler timezone only to triggers given as
    # strings; a CronTrigger instance without an explicit timezone falls back
    # to the host OS timezone. Always pass the configured tz explicitly.
    hour, minute = hh_mm.split(":")
    return CronTrigger(hour=int(hour), minute=int(minute), timezone=ZoneInfo(tz))


def reschedule_daily_job(scheduler: AsyncIOScheduler, job_id: str, hh_mm: str, tz: str) -> None:
    scheduler.reschedule_job(job_id, trigger=_daily(hh_mm, tz))
    logger.info("job %s rescheduled to %s (%s)", job_id, hh_mm, tz)


async def _stored_chat_id(session_factory: SessionFactory) -> int | None:
    async with session_factory() as session:
        raw = await repo.get_setting(session, repo.CHAT_ID_KEY)
    if raw is None:
        logger.warning("no stored chat id yet (send /start first); job skipped")
        return None
    return int(raw)


async def send_reminder(bot: Bot, session_factory: SessionFactory, settings: Settings) -> None:
    chat_id = await _stored_chat_id(session_factory)
    if chat_id is None:
        return
    now = datetime.now(UTC)
    async with session_factory() as session:
        # Count what the Start-review button will actually serve (due <= now),
        # so the number and the queue behind the button always agree.
        due = await repo.count_due(session, until=now)
    logger.info("reminder job: %d due", due)
    if due > 0:
        await bot.send_message(
            chat_id,
            f"⏰ К повторению: <b>{due}</b>",
            reply_markup=start_review_kb(),
        )
    else:
        await bot.send_message(chat_id, "⏰ Сейчас нечего повторять 🎉")


async def send_writing_prompt(
    bot: Bot,
    dispatcher: HasStorage,
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    chat_id = await _stored_chat_id(session_factory)
    if chat_id is None:
        return
    key = StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=chat_id)
    state = FSMContext(storage=dispatcher.storage, key=key)

    # Serialize with the user's handlers (same per-user isolation lock the
    # dispatcher uses), so the state check and start_writing are atomic.
    isolation = getattr(getattr(dispatcher, "fsm", None), "events_isolation", None)
    lock = isolation.lock(key) if isolation is not None else nullcontext()

    async with lock:
        current = await state.get_state()
        if current is not None:
            # Don't stomp an in-progress review/drill/settings/write session.
            logger.info("writing prompt job skipped: user is in state %s", current)
            await bot.send_message(
                chat_id,
                "⏰ Время письма! Заверши текущую сессию и набери /write.",
            )
            return

        async def answer(text: str, **kwargs: object) -> object:
            return await bot.send_message(chat_id, text, **kwargs)

        logger.info("writing prompt job fired")
        await start_writing(answer, state, session_factory, settings)


async def cleanup_stray_fsm_entries(dispatcher: HasStorage, allowed_user_id: int) -> None:
    """Every update from a non-whitelisted user leaves a per-user lock (and
    possibly a storage record) behind before the whitelist drops it; prune
    them daily so a public bot username can't slowly grow memory.
    """
    removed = 0
    storage_dict = getattr(dispatcher.storage, "storage", None)
    if isinstance(storage_dict, dict):
        for key in list(storage_dict):
            if getattr(key, "user_id", allowed_user_id) != allowed_user_id:
                del storage_dict[key]
                removed += 1
    isolation = getattr(getattr(dispatcher, "fsm", None), "events_isolation", None)
    locks = getattr(isolation, "_locks", None)
    if isinstance(locks, dict):
        for key in list(locks):
            if getattr(key, "user_id", allowed_user_id) != allowed_user_id:
                del locks[key]
                removed += 1
    logger.info("fsm cleanup: %d stray entries removed", removed)


async def send_weekly_summary(
    bot: Bot, session_factory: SessionFactory, settings: Settings
) -> None:
    """Sunday evening: stats summary + rotation to the next drill topic."""
    chat_id = await _stored_chat_id(session_factory)
    if chat_id is None:
        return
    now = datetime.now(UTC)
    async with session_factory() as session:
        stats = await repo.gather_stats(
            session,
            due_until=day_end_utc(now, settings.tz),
            week_ago=now - timedelta(days=7),
            month_ago=now - timedelta(days=30),
        )
        await repo.ensure_drill_topics_seeded(session)
        today = now.astimezone(ZoneInfo(settings.tz)).date()
        topic = await repo.rotate_drill_topic(session, week=today)
        await session.commit()
        topic_title = topic.title_fr
    logger.info("weekly summary job: new topic %s", topic_title)
    await bot.send_message(chat_id, render.stats_message(stats))
    await bot.send_message(
        chat_id,
        f"📅 Новая тема недели: <b>{render.esc(topic_title)}</b> — /drill",
    )


def _copy_and_prune(src: Path, today: str) -> tuple[Path | None, int]:
    if not src.exists():
        return None, 0
    backups_dir = src.parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    dst = backups_dir / f"frbot-{today}.db"
    # SQLite online-backup API: consistent even while the bot is writing.
    # (sqlite3's context manager only commits; close explicitly.)
    source = sqlite3.connect(src)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    stale = sorted(backups_dir.glob("frbot-*.db"))[:-BACKUP_KEEP]
    for path in stale:
        path.unlink()
    return dst, len(stale)


async def backup_database(settings: Settings) -> None:
    prefix = "sqlite+aiosqlite:///"
    if not settings.db_url.startswith(prefix) or ":memory:" in settings.db_url:
        return
    src = Path(settings.db_url.removeprefix(prefix))
    today = datetime.now(ZoneInfo(settings.tz)).date().isoformat()
    dst, pruned = await asyncio.to_thread(_copy_and_prune, src, today)
    if dst is None:
        logger.warning("backup skipped: %s does not exist", src)
        return
    logger.info("backup written to %s (%d pruned)", dst, pruned)


async def setup_jobs(
    scheduler: AsyncIOScheduler,
    bot: Bot,
    dispatcher: HasStorage,
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    async with session_factory() as session:
        cfg = await repo.get_effective_config(session, settings)
    scheduler.add_job(
        send_reminder,
        _daily(cfg.reminder_time, settings.tz),
        id=REMINDER_JOB_ID,
        args=[bot, session_factory, settings],
        replace_existing=True,
    )
    scheduler.add_job(
        send_writing_prompt,
        _daily(cfg.writing_time, settings.tz),
        id=WRITING_JOB_ID,
        args=[bot, dispatcher, session_factory, settings],
        replace_existing=True,
    )
    scheduler.add_job(
        send_weekly_summary,
        CronTrigger(day_of_week="sun", hour=18, minute=0, timezone=ZoneInfo(settings.tz)),
        id=WEEKLY_JOB_ID,
        args=[bot, session_factory, settings],
        replace_existing=True,
    )
    scheduler.add_job(
        backup_database,
        _daily(BACKUP_TIME, settings.tz),
        id=BACKUP_JOB_ID,
        args=[settings],
        replace_existing=True,
    )
    scheduler.add_job(
        cleanup_stray_fsm_entries,
        _daily(CLEANUP_TIME, settings.tz),
        id=CLEANUP_JOB_ID,
        args=[dispatcher, settings.allowed_user_id],
        replace_existing=True,
    )
    logger.info(
        "jobs scheduled: reminder %s, writing %s, weekly sun 18:00, backup %s",
        cfg.reminder_time,
        cfg.writing_time,
        BACKUP_TIME,
    )
