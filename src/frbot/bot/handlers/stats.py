"""/stats"""

from datetime import UTC, datetime, timedelta

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from frbot.bot import render
from frbot.config import Settings
from frbot.db import repo
from frbot.db.models import User
from frbot.db.session import SessionFactory
from frbot.timeutil import day_end_utc


async def cmd_stats(
    message: Message,
    user: User,
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        stats = await repo.gather_stats(
            session,
            user_id=user.id,
            due_until=day_end_utc(now, settings.tz),
            week_ago=now - timedelta(days=7),
            month_ago=now - timedelta(days=30),
        )
    await message.answer(render.stats_message(stats))


def create_router() -> Router:
    router = Router(name="stats")
    router.message.register(cmd_stats, Command("stats"))
    return router
