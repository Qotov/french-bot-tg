"""/cards: browse your deck, suspend what you don't want, delete mistakes.

Suspending rather than deleting is the pedagogically right default: a card that
is annoying today (a word you already know, a bad generation) should leave the
review queue without losing the history behind it.
"""

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from frbot.bot import render
from frbot.bot.keyboards import deck_kb
from frbot.bot.telegram_utils import safe_answer, safe_edit_text
from frbot.db import repo
from frbot.db.models import CardKind, User
from frbot.db.session import SessionFactory

logger = logging.getLogger(__name__)

EMPTY_TEXT = (
    "В колоде пока пусто. Пришли слово или набери /topic с темой — и здесь появятся карточки."
)

KIND_ICONS = {
    CardKind.vocab.value: "📇",
    CardKind.error.value: "✍️",
    CardKind.drill_error.value: "📚",
}


async def _page(session_factory: SessionFactory, user: User, offset: int) -> tuple[str, object]:
    async with session_factory() as session:
        total = await repo.count_cards(session, user_id=user.id)
        if total == 0:
            return EMPTY_TEXT, None
        # Deleting the last card of the last page would otherwise leave the
        # user staring at an empty list with no way back.
        last_page_offset = ((total - 1) // repo.DECK_PAGE_SIZE) * repo.DECK_PAGE_SIZE
        offset = min(max(offset, 0), last_page_offset)
        cards = await repo.list_cards_page(session, user_id=user.id, offset=offset)

    shown_to = min(offset + len(cards), total)
    lines = [f"📚 <b>Твоя колода</b> — {total} карточек ({offset + 1}–{shown_to})", ""]
    for card in cards:
        icon = KIND_ICONS.get(card.kind, "📇")
        state = " · ⏸ на паузе" if card.suspended else ""
        lines.append(f"{icon} <b>{render.esc(card.lemma)}</b>{state}")
    lines.append("")
    lines.append("<i>⏸ — убрать из повторений, ▶️ — вернуть, 🗑 — удалить</i>")
    return render.fit_lines(lines), deck_kb(cards, offset, total, repo.DECK_PAGE_SIZE)


async def cmd_cards(message: Message, user: User, session_factory: SessionFactory) -> None:
    text, keyboard = await _page(session_factory, user, 0)
    await message.answer(text, reply_markup=keyboard)


async def on_page(query: CallbackQuery, user: User, session_factory: SessionFactory) -> None:
    offset = int(query.data.split(":")[2])
    text, keyboard = await _page(session_factory, user, offset)
    if isinstance(query.message, Message):
        await safe_edit_text(query.message, text, reply_markup=keyboard)
    await safe_answer(query)


async def on_toggle(query: CallbackQuery, user: User, session_factory: SessionFactory) -> None:
    _, _, card_id_raw, offset_raw = query.data.split(":")
    async with session_factory() as session:
        card = await repo.get_card(session, int(card_id_raw), user_id=user.id)
        if card is None:
            await safe_answer(query, "Карточка не найдена.")
            return
        updated = await repo.set_card_suspended(
            session, card.id, user_id=user.id, suspended=not card.suspended
        )
        await session.commit()
        paused = updated.suspended
    await safe_answer(query, "На паузе" if paused else "Вернул в повторения")
    text, keyboard = await _page(session_factory, user, int(offset_raw))
    if isinstance(query.message, Message):
        await safe_edit_text(query.message, text, reply_markup=keyboard)


async def on_delete(query: CallbackQuery, user: User, session_factory: SessionFactory) -> None:
    _, _, card_id_raw, offset_raw = query.data.split(":")
    async with session_factory() as session:
        deleted = await repo.delete_card(session, int(card_id_raw), user_id=user.id)
        await session.commit()
    await safe_answer(query, "Удалено" if deleted else "Уже удалено")
    text, keyboard = await _page(session_factory, user, int(offset_raw))
    if isinstance(query.message, Message):
        await safe_edit_text(query.message, text, reply_markup=keyboard)


def create_router() -> Router:
    router = Router(name="deck")
    router.message.register(cmd_cards, Command("cards"))
    router.callback_query.register(on_page, F.data.startswith("deck:page:"))
    router.callback_query.register(on_toggle, F.data.startswith("deck:toggle:"))
    router.callback_query.register(on_delete, F.data.startswith("deck:del:"))
    return router
