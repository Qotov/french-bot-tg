"""Scheduled jobs.

Delivery times are per user now, so instead of one cron job per message type
there is a single minute tick: every minute it wakes, computes the local HH:MM,
and sends to the users whose own reminder/writing time matches. A user changing
their time in /settings is picked up on the next tick with nothing to
reschedule — and a missed tick (restart, brief outage) costs one message, not a
corrupted schedule.
"""

import asyncio
import logging
import sqlite3
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
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
from frbot.db.models import User
from frbot.db.session import SessionFactory
from frbot.timeutil import day_end_utc


class HasStorage(Protocol):
    storage: BaseStorage


logger = logging.getLogger(__name__)

TICK_JOB_ID = "minute_tick"
WEEKLY_JOB_ID = "weekly_stats"
BACKUP_JOB_ID = "daily_backup"
CLEANUP_JOB_ID = "daily_cleanup"
BACKUP_KEEP = 14
BACKUP_TIME = "03:00"
CLEANUP_TIME = "04:00"
SEND_RATE = 20  # messages/second, under Telegram's ~30/s cap
LOCK_WAIT_SECONDS = 20  # give up rather than stall the tick on a busy user
DELIVERY_TIMEOUT = 60  # a single delivery may never outlive its minute

# Deliveries run detached from the tick, so one slow user cannot delay anyone
# else. Keep strong references or the event loop may garbage-collect them.
_in_flight: set[asyncio.Task] = set()

# The local wall-clock minute the previous tick saw, to detect a DST jump.
_last_local: datetime | None = None

# (user_id, kind, local date) pairs already delivered — prevents a double send
# if a tick runs twice for the same minute after a restart.
_sent_today: set[tuple[int, str, date]] = set()


def create_scheduler(tz: str) -> AsyncIOScheduler:
    return AsyncIOScheduler(timezone=ZoneInfo(tz))


def _daily(hh_mm: str, tz: str) -> CronTrigger:
    # APScheduler 3 applies the scheduler timezone only to triggers given as
    # strings; a CronTrigger instance without an explicit timezone falls back
    # to the host OS timezone. Always pass the configured tz explicitly.
    hour, minute = hh_mm.split(":")
    return CronTrigger(hour=int(hour), minute=int(minute), timezone=ZoneInfo(tz))


def _mark_sent(user_id: int, kind: str, today: date) -> bool:
    """False when this user already got this message today."""
    key = (user_id, kind, today)
    if key in _sent_today:
        return False
    # Keep the set small: drop entries from previous days.
    for stale in [k for k in _sent_today if k[2] != today]:
        _sent_today.discard(stale)
    _sent_today.add(key)
    return True


async def drain_deliveries(wait_seconds: float = DELIVERY_TIMEOUT + 5) -> None:
    """Wait for detached deliveries to finish (shutdown, and tests)."""
    while _in_flight:
        pending = set(_in_flight)
        done, _ = await asyncio.wait(pending, timeout=wait_seconds)
        if not done:
            return


def _minutes_skipped(previous: datetime | None, current: datetime) -> set[str]:
    """Wall-clock minutes between two ticks that never happened (DST jump)."""
    if previous is None:
        return set()
    gap = (current - previous).total_seconds()
    # A normal tick is ~60s; a spring-forward jump is an hour or so. Anything
    # longer than that is a restart/outage, not a DST change.
    if gap <= 90 or gap > 3 * 3600:
        return set()
    missed = set()
    cursor = previous + timedelta(minutes=1)
    while cursor < current and len(missed) < 180:
        missed.add(cursor.strftime("%H:%M"))
        cursor += timedelta(minutes=1)
    return missed


async def send_due_reminder(bot: Bot, user: User, session_factory: SessionFactory) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        due = await repo.count_due(session, user_id=user.id, until=now)
    chat_id = user.chat_id or user.id
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
    user: User,
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    """Send the daily writing prompt. Raises TimeoutError if the user is busy
    with a long-running update — the caller treats that as "skip today"."""
    chat_id = user.chat_id or user.id
    # aiogram resolves incoming updates with FSMStrategy.USER_IN_CHAT, i.e. the
    # chat the message arrives in — always the user's private chat. Keying on
    # user.id keeps the job's state on that same key even if chat_id drifts.
    key = StorageKey(bot_id=bot.id, chat_id=user.id, user_id=user.id)
    state = FSMContext(storage=dispatcher.storage, key=key)

    # Serialize with the user's handlers (same per-user isolation lock the
    # dispatcher uses), so the state check and start_writing are atomic. The
    # lock is held for the whole of any update the user is currently in —
    # including a slow LLM call — so never wait on it indefinitely.
    isolation = getattr(getattr(dispatcher, "fsm", None), "events_isolation", None)
    lock = isolation.lock(key) if isolation is not None else nullcontext()

    async with asyncio.timeout(LOCK_WAIT_SECONDS), lock:
        current = await state.get_state()
        if current is not None:
            # Don't stomp an in-progress review/drill/settings/write session.
            logger.info("writing prompt skipped for %d: state %s", user.id, current)
            await bot.send_message(
                chat_id, "⏰ Время письма! Заверши текущую сессию и набери /write."
            )
            return

        async def answer(text: str, **kwargs: object) -> object:
            return await bot.send_message(chat_id, text, **kwargs)

        await start_writing(answer, state, user, session_factory, settings)


async def minute_tick(
    bot: Bot,
    dispatcher: HasStorage,
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    """Deliver each user's reminder and writing prompt at their own time."""
    global _last_local
    local = datetime.now(ZoneInfo(settings.tz))
    hh_mm = local.strftime("%H:%M")
    today = local.date()
    # On the spring-forward night a whole hour of wall-clock time never occurs,
    # so an exact HH:MM match would silently skip everyone scheduled inside it.
    # Treat every minute the clock jumped over as also due now.
    skipped = _minutes_skipped(_last_local, local)
    _last_local = local

    async with session_factory() as session:
        users = await repo.list_active_users(session)
        plan = [(u, repo.config_for_user(u, settings)) for u in users]

    matches = {hh_mm, *skipped}
    due: list[tuple[User, str]] = []
    for user, cfg in plan:
        if cfg.reminder_time in matches and _mark_sent(user.id, "reminder", today):
            due.append((user, "reminder"))
        if cfg.writing_time in matches and _mark_sent(user.id, "writing", today):
            due.append((user, "writing"))
    if not due:
        return

    logger.info("tick %s: %d deliveries", hh_mm, len(due))
    for index, (user, kind) in enumerate(due):
        task = asyncio.create_task(
            _deliver(bot, dispatcher, user, kind, session_factory, settings, index)
        )
        _in_flight.add(task)
        task.add_done_callback(_in_flight.discard)


async def _deliver(
    bot: Bot,
    dispatcher: HasStorage,
    user: User,
    kind: str,
    session_factory: SessionFactory,
    settings: Settings,
    index: int,
) -> None:
    """One user's scheduled message, fully isolated from every other user's."""
    # Stagger sends to stay under Telegram's rate limit without serializing the
    # tick itself.
    await asyncio.sleep(index / SEND_RATE)
    try:
        async with asyncio.timeout(DELIVERY_TIMEOUT):
            if kind == "reminder":
                await send_due_reminder(bot, user, session_factory)
            else:
                await send_writing_prompt(bot, dispatcher, user, session_factory, settings)
    except TimeoutError:
        logger.warning("%s for user %d timed out (busy or slow); skipped today", kind, user.id)
    except Exception:
        logger.exception("%s delivery to user %d failed", kind, user.id)


async def cleanup_stray_fsm_entries(
    dispatcher: HasStorage, session_factory: SessionFactory
) -> None:
    """Updates from people who are not pilot participants still allocate a
    per-user lock and storage record before the auth middleware drops them;
    prune everything that does not belong to a registered user."""
    async with session_factory() as session:
        known = {u.id for u in await repo.list_users(session)}

    removed = 0
    storage_dict = getattr(dispatcher.storage, "storage", None)
    if isinstance(storage_dict, dict):
        for key in list(storage_dict):
            if getattr(key, "user_id", None) not in known:
                del storage_dict[key]
                removed += 1
    isolation = getattr(getattr(dispatcher, "fsm", None), "events_isolation", None)
    locks = getattr(isolation, "_locks", None)
    if isinstance(locks, dict):
        for key in list(locks):
            if getattr(key, "user_id", None) not in known:
                del locks[key]
                removed += 1
    logger.info("fsm cleanup: %d stray entries removed", removed)


async def send_weekly_summary(
    bot: Bot, session_factory: SessionFactory, settings: Settings
) -> None:
    """Sunday evening: each participant's own stats plus the cohort's topic
    for the coming week."""
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo(settings.tz)).date()
    next_week = today + timedelta(days=1)  # the topic that starts tomorrow

    async with session_factory() as session:
        await repo.ensure_drill_topics_seeded(session)
        users = await repo.list_active_users(session)
        topic = await repo.get_topic_for_week(session, today=next_week)
        if topic is not None:
            await repo.mark_topic_announced(session, topic, week=next_week)
        await session.commit()
        topic_title = topic.title_fr if topic else None

    for user in users:
        try:
            async with session_factory() as session:
                stats = await repo.gather_stats(
                    session,
                    user_id=user.id,
                    due_until=day_end_utc(now, settings.tz),
                    week_ago=now - timedelta(days=7),
                    month_ago=now - timedelta(days=30),
                    tz=settings.tz,
                )
            chat_id = user.chat_id or user.id
            await bot.send_message(chat_id, render.weekly_summary(stats))
            if topic_title:
                await bot.send_message(
                    chat_id,
                    f"📅 Тема следующей недели: <b>{render.esc(topic_title)}</b> — /drill",
                )
            await asyncio.sleep(1 / SEND_RATE)
        except Exception:
            logger.exception("weekly summary to user %d failed", user.id)
    logger.info("weekly summary sent to %d users", len(users))


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


def setup_jobs(
    scheduler: AsyncIOScheduler,
    bot: Bot,
    dispatcher: HasStorage,
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    scheduler.add_job(
        minute_tick,
        CronTrigger(minute="*", timezone=ZoneInfo(settings.tz)),
        id=TICK_JOB_ID,
        args=[bot, dispatcher, session_factory, settings],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
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
        args=[dispatcher, session_factory],
        replace_existing=True,
    )
    logger.info(
        "jobs scheduled: per-user tick every minute, weekly sun 18:00, backup %s, cleanup %s",
        BACKUP_TIME,
        CLEANUP_TIME,
    )
