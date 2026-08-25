"""Query functions. Every function takes an AsyncSession and commits nothing;
callers own the transaction (commit at the end of a handler/job).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frbot.db.models import AppSetting

CHAT_ID_KEY = "chat_id"


async def get_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.get(AppSetting, key)
    return row.value if row else None


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


async def get_all_settings(session: AsyncSession) -> dict[str, str]:
    rows = (await session.execute(select(AppSetting))).scalars().all()
    return {r.key: r.value for r in rows}
