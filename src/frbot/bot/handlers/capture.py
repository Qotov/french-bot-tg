"""Capture: any non-command text message becomes a vocab card.
Voice notes are transcribed by Gemini and the mentioned words become cards.
"""

import logging

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.types import CallbackQuery, Message

from frbot.bot import render
from frbot.bot.keyboards import card_preview_kb
from frbot.bot.telegram_utils import (
    VOICE_MAX_DURATION,
    VOICE_TOO_LONG_TEXT,
    download_voice,
    safe_answer,
    safe_edit_text,
)
from frbot.config import Settings
from frbot.db import repo
from frbot.db.session import SessionFactory
from frbot.llm.client import LLMClient, LLMError
from frbot.srs.scheduler import SrsScheduler

logger = logging.getLogger(__name__)

CAPTURE_MAX_LEN = 200
FAIL_TEXT = "⚠️ Не получилось обработать. Попробуй ещё раз через минуту."
VOICE_FAIL_TEXT = "⚠️ Не получилось разобрать голосовое. Попробуй ещё раз."
VOICE_NO_WORDS_TEXT = (
    "🤔 Не расслышал ни одного французского слова. Скажи, например: «слово boulangerie»."
)


async def capture_one(
    message: Message,
    raw: str,
    session_factory: SessionFactory,
    llm: LLMClient,
    srs: SrsScheduler,
    settings: Settings,
) -> None:
    """The capture pipeline for one word/phrase: dedupe, enrich, save, preview."""
    # Cheap pre-check: the raw input may already be a stored lemma.
    async with session_factory() as session:
        existing = await repo.find_card_by_lemma(session, raw)
    if existing is not None:
        await message.answer(
            render.card_preview(existing, existing=True),
            reply_markup=card_preview_kb(existing.id),
        )
        return

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
        card = await repo.create_vocab_card(
            session, srs, text=raw, enrichment=enrichment.model_dump()
        )
        await session.commit()
        card_id = card.id

    logger.info("captured card %d: %s", card_id, enrichment.lemma)
    await message.answer(render.card_preview(card), reply_markup=card_preview_kb(card_id))


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
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await capture_one(message, raw, session_factory, llm, srs, settings)


async def handle_voice_capture(
    message: Message,
    session_factory: SessionFactory,
    llm: LLMClient,
    srs: SrsScheduler,
    settings: Settings,
) -> None:
    """A voice note outside any session: extract the words to save from audio."""
    if message.voice.duration > VOICE_MAX_DURATION:
        await message.answer(VOICE_TOO_LONG_TEXT)
        return
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    audio = await download_voice(message)
    if audio is None:
        await message.answer(VOICE_FAIL_TEXT)
        return
    data, mime_type = audio
    try:
        extracted = await llm.extract_voice_words(data, mime_type, model=settings.model_fast)
    except LLMError:
        logger.exception("voice word extraction failed")
        await message.answer(VOICE_FAIL_TEXT)
        return
    if not extracted.words:
        await message.answer(VOICE_NO_WORDS_TEXT)
        return
    logger.info("voice capture: %d words", len(extracted.words))
    for word in extracted.words:
        await capture_one(message, word.strip(), session_factory, llm, srs, settings)


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
    router.message.register(handle_voice_capture, F.voice)
    return router
