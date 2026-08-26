"""Capture: any non-command text message becomes a vocab card."""

import logging

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.types import CallbackQuery, Message

from frbot.bot import render
from frbot.bot.keyboards import card_preview_kb
from frbot.bot.telegram_utils import safe_answer, safe_edit_text
from frbot.config import Settings
from frbot.db import repo
from frbot.db.models import Card, CardKind
from frbot.db.session import SessionFactory
from frbot.llm.client import LLMClient, LLMError
from frbot.srs.scheduler import SrsScheduler

logger = logging.getLogger(__name__)

CAPTURE_MAX_LEN = 200
FAIL_TEXT = "⚠️ Не получилось обработать. Попробуй ещё раз через минуту."


async def handle_capture(
    message: Message,
    session_factory: SessionFactory,
    llm: LLMClient,
    srs: SrsScheduler,
    settings: Settings,
) -> None:
    raw = (message.text or "").strip()
    if not raw:
        return
    if len(raw) > CAPTURE_MAX_LEN:
        await message.answer(
            f"Слишком длинно для карточки (максимум {CAPTURE_MAX_LEN} символов). "
            f"Для текстов есть /write."
        )
        return

    # Cheap pre-check: the raw input may already be a stored lemma.
    async with session_factory() as session:
        existing = await repo.find_card_by_lemma(session, raw)
    if existing is not None:
        await message.answer(
            render.card_preview(existing, existing=True),
            reply_markup=card_preview_kb(existing.id),
        )
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        enrichment = await llm.enrich(raw, model=settings.model_fast)
    except LLMError:
        logger.exception("enrichment failed for %r", raw)
        await message.answer(FAIL_TEXT)
        return

    async with session_factory() as session:
        existing = await repo.find_card_by_lemma(session, enrichment.lemma)
        if existing is not None:
            await message.answer(
                render.card_preview(existing, existing=True),
                reply_markup=card_preview_kb(existing.id),
            )
            return
        new = srs.new_card()
        card = Card(
            text=raw,
            lemma=enrichment.lemma.strip().lower(),
            kind=CardKind.vocab.value,
            enrichment=enrichment.model_dump(),
            fsrs=new.fsrs,
            due=new.due,
            state=new.state,
        )
        await repo.add_card(session, card)
        await session.commit()
        card_id = card.id

    logger.info("captured card %d: %s", card_id, enrichment.lemma)
    await message.answer(render.card_preview(card), reply_markup=card_preview_kb(card_id))


async def on_delete(query: CallbackQuery, session_factory: SessionFactory) -> None:
    card_id = int(query.data.split(":")[2])
    async with session_factory() as session:
        deleted = await repo.delete_card(session, card_id)
        await session.commit()
    if isinstance(query.message, Message):
        await safe_edit_text(
            query.message, "🗑 Карточка удалена." if deleted else "Карточка уже удалена."
        )
    await query.answer()


async def on_regenerate(
    query: CallbackQuery,
    session_factory: SessionFactory,
    llm: LLMClient,
    settings: Settings,
) -> None:
    card_id = int(query.data.split(":")[2])
    async with session_factory() as session:
        card = await repo.get_card(session, card_id)
    if card is None:
        await query.answer("Карточка не найдена.", show_alert=True)
        return

    await safe_answer(query, "Генерирую заново…")
    try:
        enrichment = await llm.enrich(card.text, model=settings.model_fast)
    except LLMError:
        logger.exception("regeneration failed for card %d", card_id)
        if isinstance(query.message, Message):
            await query.message.answer(FAIL_TEXT)
        return

    async with session_factory() as session:
        card = await repo.get_card(session, card_id)
        if card is None:
            return
        new_lemma = enrichment.lemma.strip().lower()
        other = await repo.find_card_by_lemma(session, new_lemma)
        if other is not None and other.id != card_id:
            # Overwriting would duplicate the other card's content while this
            # card keeps its own dedupe key; leave the card untouched.
            logger.info(
                "regen rejected for card %d: lemma %r belongs to card %d",
                card_id,
                new_lemma,
                other.id,
            )
            if isinstance(query.message, Message):
                await query.message.answer(
                    f"⚠️ Получается дубликат карточки «{render.esc(new_lemma)}» — оставил как было."
                )
            return
        card.enrichment = enrichment.model_dump()
        card.lemma = new_lemma
        await session.commit()

    if isinstance(query.message, Message):
        await safe_edit_text(
            query.message, render.card_preview(card), reply_markup=card_preview_kb(card_id)
        )


def create_router() -> Router:
    router = Router(name="capture")
    router.callback_query.register(on_delete, F.data.startswith("card:delete:"))
    router.callback_query.register(on_regenerate, F.data.startswith("card:regen:"))
    router.message.register(handle_capture, F.text, ~F.text.startswith("/"))
    return router
