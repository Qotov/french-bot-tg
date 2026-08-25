"""APScheduler jobs: daily reminder, nightly DB backup.

The scheduler runs in the configured timezone (Europe/Paris); job times read
runtime overrides from app_settings at setup, and /settings reschedules jobs
via reschedule_daily_job.
"""

import asyncio
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from frbot.bot.keyboards import start_review_kb
from frbot.config import Settings
from frbot.db import repo
from frbot.db.session import SessionFactory

logger = logging.getLogger(__name__)

REMINDER_JOB_ID = "daily_reminder"
WRITING_JOB_ID = "daily_writing"
WEEKLY_JOB_ID = "weekly_stats"
BACKUP_JOB_ID = "daily_backup"
BACKUP_KEEP = 14
BACKUP_TIME = "03:00"


def create_scheduler(tz: str) -> AsyncIOScheduler:
    return AsyncIOScheduler(timezone=ZoneInfo(tz))


def _daily(hh_mm: str) -> CronTrigger:
    hour, minute = hh_mm.split(":")
    return CronTrigger(hour=int(hour), minute=int(minute))


def reschedule_daily_job(scheduler: AsyncIOScheduler, job_id: str, hh_mm: str) -> None:
    scheduler.reschedule_job(job_id, trigger=_daily(hh_mm))
    logger.info("job %s rescheduled to %s", job_id, hh_mm)


async def _stored_chat_id(session_factory: SessionFactory) -> int | None:
    async with session_factory() as session:
        raw = await repo.get_setting(session, repo.CHAT_ID_KEY)
    if raw is None:
        logger.warning("no stored chat id yet (send /start first); job skipped")
        return None
    return int(raw)


async def send_reminder(bot: Bot, session_factory: SessionFactory) -> None:
    chat_id = await _stored_chat_id(session_factory)
    if chat_id is None:
        return
    now = datetime.now(UTC)
    async with session_factory() as session:
        due = await repo.count_due(session, until=now)
    logger.info("reminder job: %d due", due)
    if due > 0:
        await bot.send_message(
            chat_id,
            f"⏰ Сегодня к повторению: <b>{due}</b>",
            reply_markup=start_review_kb(),
        )
    else:
        await bot.send_message(chat_id, "⏰ Сегодня нечего повторять 🎉")


def _copy_and_prune(src: Path, today: str) -> tuple[Path | None, int]:
    if not src.exists():
        return None, 0
    backups_dir = src.parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    dst = backups_dir / f"frbot-{today}.db"
    shutil.copy2(src, dst)
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
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    async with session_factory() as session:
        cfg = await repo.get_effective_config(session, settings)
    scheduler.add_job(
        send_reminder,
        _daily(cfg.reminder_time),
        id=REMINDER_JOB_ID,
        args=[bot, session_factory],
        replace_existing=True,
    )
    scheduler.add_job(
        backup_database,
        _daily(BACKUP_TIME),
        id=BACKUP_JOB_ID,
        args=[settings],
        replace_existing=True,
    )
    logger.info("jobs scheduled: reminder %s, backup %s", cfg.reminder_time, BACKUP_TIME)
