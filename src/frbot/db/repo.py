"""Query functions. Every function takes an AsyncSession and commits nothing;
callers own the transaction (commit at the end of a handler/job).
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from frbot.db.models import AppSetting, Card

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


# -- cards --------------------------------------------------------------------


async def get_card(session: AsyncSession, card_id: int) -> Card | None:
    return await session.get(Card, card_id)


async def find_card_by_lemma(session: AsyncSession, lemma: str) -> Card | None:
    stmt = (
        select(Card)
        .where(func.lower(Card.lemma) == lemma.strip().lower())
        .order_by(Card.id)
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def add_card(session: AsyncSession, card: Card) -> Card:
    session.add(card)
    await session.flush()
    return card


async def delete_card(session: AsyncSession, card_id: int) -> bool:
    card = await session.get(Card, card_id)
    if card is None:
        return False
    await session.delete(card)
    return True
