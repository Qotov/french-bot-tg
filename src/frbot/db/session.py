"""Async engine and session factory."""

from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

SessionFactory = async_sessionmaker[AsyncSession]


def create_engine_and_factory(db_url: str) -> tuple[AsyncEngine, SessionFactory]:
    if db_url.startswith("sqlite+aiosqlite:///") and ":memory:" not in db_url:
        db_path = Path(db_url.removeprefix("sqlite+aiosqlite:///"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory
