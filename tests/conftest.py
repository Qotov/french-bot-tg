import pytest
import pytest_asyncio
from aiogram import Bot
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from frbot.config import Settings
from frbot.db.models import Base
from frbot.db.session import SessionFactory
from tests.fakes import ALLOWED_USER_ID, make_bot


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        bot_token="42:TEST-TOKEN",
        gemini_api_key="test-key",
        allowed_user_id=ALLOWED_USER_ID,
    )


@pytest_asyncio.fixture
async def session_factory() -> SessionFactory:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def fake_bot() -> Bot:
    bot = make_bot()
    yield bot
    await bot.session.close()
