"""Entry point: python -m frbot"""

import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import BotCommand
from sqlalchemy.ext.asyncio import create_async_engine

from frbot.bot.handlers import admin, capture, drill, review, stats, system, talk, topic, write
from frbot.bot.handlers import settings as settings_handlers
from frbot.bot.middleware import AuthMiddleware
from frbot.config import Settings
from frbot.db import repo
from frbot.db.models import Base
from frbot.db.session import SessionFactory, create_engine_and_factory
from frbot.jobs import reminders
from frbot.llm.client import LLMClient
from frbot.srs.scheduler import SrsScheduler
from frbot.usage import UsageLimiter

logger = logging.getLogger(__name__)


def build_dispatcher(
    settings: Settings,
    session_factory: SessionFactory,
    llm: LLMClient | None = None,
    srs: SrsScheduler | None = None,
    usage: UsageLimiter | None = None,
) -> Dispatcher:
    # SimpleEventIsolation serializes update handling per chat/user, so a
    # double-tap on an inline button cannot run two handlers concurrently
    # (double-graded reviews, duplicate capture cards, ...).
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dp.update.outer_middleware(AuthMiddleware(settings, session_factory))
    # Feedback first: while its flag is set, the next plain message is feedback
    # no matter which session is running.
    dp.include_router(system.create_feedback_router())
    dp.include_router(system.create_router())
    dp.include_router(admin.create_router())
    dp.include_router(review.create_router())
    dp.include_router(stats.create_router())
    dp.include_router(write.create_router())
    dp.include_router(drill.create_router())
    dp.include_router(settings_handlers.create_router())
    dp.include_router(talk.create_router())
    dp.include_router(topic.create_router())
    dp.include_router(capture.create_router())  # must stay last: catch-all text/voice
    dp["settings"] = settings
    dp["session_factory"] = session_factory
    dp["llm"] = llm or LLMClient(settings.gemini_api_key)
    dp["srs"] = srs or SrsScheduler(settings.desired_retention)
    dp["usage"] = usage or UsageLimiter(settings.daily_llm_actions, settings.tz)
    return dp


def build_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


BOT_COMMANDS = [
    BotCommand(command="review", description="Повторение карточек"),
    BotCommand(command="write", description="Письменное задание"),
    BotCommand(command="talk", description="Диалог с исправлениями"),
    BotCommand(command="topic", description="Подборка слов по теме"),
    BotCommand(command="drill", description="Грамматика недели"),
    BotCommand(command="stats", description="Мой прогресс"),
    BotCommand(command="level", description="Уровень (A2/B1/B2)"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="feedback", description="Написать автору"),
    BotCommand(command="stop", description="Прервать сессию"),
    BotCommand(command="help", description="Справка"),
]


def _alembic_upgrade() -> bool:
    if not Path("alembic.ini").exists():
        return False
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.attributes["configure_logger"] = False  # keep the app's logging setup
    command.upgrade(cfg, "head")
    return True


async def run_migrations(settings: Settings) -> None:
    try:
        if await asyncio.to_thread(_alembic_upgrade):
            logger.info("migrations applied")
            return
    except Exception:
        logger.exception("alembic upgrade failed; falling back to create_all")
    engine = create_async_engine(settings.db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    logger.info("schema ensured via create_all")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings()
    await run_migrations(settings)
    _engine, session_factory = create_engine_and_factory(settings.db_url)
    async with session_factory() as session:
        await repo.ensure_drill_topics_seeded(session)
        await session.commit()
        user_count = await repo.count_users(session)
    logger.info("pilot: %d/%d users registered", user_count, settings.max_users)

    dp = build_dispatcher(settings, session_factory)
    bot = build_bot(settings)

    scheduler = reminders.create_scheduler(settings.tz)
    reminders.setup_jobs(scheduler, bot, dp, session_factory, settings)
    scheduler.start()
    dp["scheduler"] = scheduler

    logger.info("starting long polling")
    try:
        await bot.set_my_commands(BOT_COMMANDS)
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
