"""Entry point: python -m frbot"""

import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import create_async_engine

from frbot.bot.handlers import capture, drill, review, stats, system, write
from frbot.bot.handlers import settings as settings_handlers
from frbot.bot.middleware import WhitelistMiddleware
from frbot.config import Settings
from frbot.db import repo
from frbot.db.models import Base
from frbot.db.session import SessionFactory, create_engine_and_factory
from frbot.jobs import reminders
from frbot.llm.client import LLMClient
from frbot.srs.scheduler import SrsScheduler

logger = logging.getLogger(__name__)


def build_dispatcher(
    settings: Settings,
    session_factory: SessionFactory,
    llm: LLMClient | None = None,
    srs: SrsScheduler | None = None,
) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(WhitelistMiddleware(settings.allowed_user_id))
    dp.include_router(system.create_router())
    dp.include_router(review.create_router())
    dp.include_router(stats.create_router())
    dp.include_router(write.create_router())
    dp.include_router(drill.create_router())
    dp.include_router(settings_handlers.create_router())
    dp.include_router(capture.create_router())
    dp["settings"] = settings
    dp["session_factory"] = session_factory
    dp["llm"] = llm or LLMClient(settings.anthropic_api_key)
    dp["srs"] = srs or SrsScheduler(settings.desired_retention)
    return dp


def build_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


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
    dp = build_dispatcher(settings, session_factory)
    bot = build_bot(settings)

    scheduler = reminders.create_scheduler(settings.tz)
    await reminders.setup_jobs(scheduler, bot, dp, session_factory, settings)
    scheduler.start()
    dp["scheduler"] = scheduler

    logger.info("starting long polling")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
