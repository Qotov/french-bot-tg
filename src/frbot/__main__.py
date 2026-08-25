"""Entry point: python -m frbot"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from frbot.bot.handlers import capture, system
from frbot.bot.middleware import WhitelistMiddleware
from frbot.config import Settings
from frbot.db.session import SessionFactory, create_engine_and_factory
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


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings()
    _engine, session_factory = create_engine_and_factory(settings.db_url)
    dp = build_dispatcher(settings, session_factory)
    bot = build_bot(settings)
    logger.info("starting long polling")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
