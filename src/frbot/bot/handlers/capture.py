"""Capture: any non-command text message becomes a vocab card.
Voice notes are transcribed by Gemini and the mentioned words become cards.
"""

import logging

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.types import CallbackQuery, Message

from frbot.bot import render
from frbot.bot.alerts import AdminAlerter
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
from frbot.db.models import User
from frbot.db.session import SessionFactory
from frbot.llm.client import LLMClient, LLMError
from frbot.srs.scheduler import SrsScheduler
from frbot.usage import OVER_LIMIT_TEXT, UsageLimiter

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
    user: User,
    session_factory: SessionFactory,
    llm: LLMClient,
    srs: SrsScheduler,
    settings: Settings,
    alerter: AdminAlerter,
    usage: UsageLimiter | None = None,
) -> bool:
    """The capture pipeline for one word/phrase: dedupe, enrich, save, preview.

    Returns False when the LLM failed (callers processing several words should
    stop instead of retrying the whole backoff ladder per word).
    """
    # Cheap pre-check: the raw input may already be a stored lemma.
    async with session_factory() as session:
        existing = await repo.find_card_by_lemma(session, raw, user_id=user.id)
    if existing is not None:
        await message.answer(
            render.card_preview(existing, existing=True),
            reply_markup=card_preview_kb(existing.id),
        )
        return True

    if usage is not None and not usage.check_and_count(user.id):
        await message.answer(OVER_LIMIT_TEXT)
        return False
    try:
        enrichment = await llm.enrich(raw, model=settings.model_fast, level=user.level)
    except LLMError:
        logger.exception("enrichment failed for %r", raw)
        await alerter.record_llm_failure(message.bot, f"enrich: {raw[:60]}")
        await message.answer(FAIL_TEXT)
        return False

    async with session_factory() as session:
        existing = await repo.find_card_by_lemma(session, enrichment.lemma, user_id=user.id)
        if existing is not None:
            await message.answer(
                render.card_preview(existing, existing=True),
                reply_markup=card_preview_kb(existing.id),
            )
            return True
        card = await repo.create_vocab_card(
            session, srs, user_id=user.id, text=raw, enrichment=enrichment.model_dump()
        )
        await session.commit()
        card_id = card.id

    logger.info("captured card %d: %s", card_id, enrichment.lemma)
    await message.answer(render.card_preview(card), reply_markup=card_preview_kb(card_id))
    return True


async def handle_capture(
    message: Message,
    user: User,
    session_factory: SessionFactory,
    llm: LLMClient,
    srs: SrsScheduler,
    settings: Settings,
    usage: UsageLimiter,
    alerter: AdminAlerter,
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
    await capture_one(message, raw, user, session_factory, llm, srs, settings, alerter, usage)


async def handle_voice_capture(
    message: Message,
    user: User,
    session_factory: SessionFactory,
    llm: LLMClient,
    srs: SrsScheduler,
    settings: Settings,
    usage: UsageLimiter,
    alerter: AdminAlerter,
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
    if not usage.check_and_count(user.id):
        await message.answer(OVER_LIMIT_TEXT)
        return
    try:
        extracted = await llm.extract_voice_words(data, mime_type, model=settings.model_fast)
    except LLMError:
        logger.exception("voice word extraction failed")
        await alerter.record_llm_failure(message.bot, "voice extraction")
        await message.answer(VOICE_FAIL_TEXT)
        return
    if not extracted.words:
        await message.answer(VOICE_NO_WORDS_TEXT)
        return
    logger.info("voice capture: %d words", len(extracted.words))
    for index, word in enumerate(extracted.words):
        word = word.strip()
        if len(word) > CAPTURE_MAX_LEN:
            # The extractor returned a whole utterance, not a word/phrase.
            await message.answer(
                f"«{render.esc(word[:60])}…» — слишком длинно для карточки, пропускаю."
            )
            continue
        ok = await capture_one(
            message, word, user, session_factory, llm, srs, settings, alerter, usage
        )
        if not ok:
            remaining = len(extracted.words) - index - 1
            if remaining:
                await message.answer(
                    f"Оставшиеся {remaining} слов(а) не добавлены — пришли их позже."
                )
            break


async def on_delete(query: CallbackQuery, user: User, session_factory: SessionFactory) -> None:
    card_id = int(query.data.split(":")[2])
    async with session_factory() as session:
        deleted = await repo.delete_card(session, card_id, user_id=user.id)
        await session.commit()
    if isinstance(query.message, Message):
        await safe_edit_text(
            query.message, "🗑 Карточка удалена." if deleted else "Карточка уже удалена."
        )
    await query.answer()


async def on_regenerate(
    query: CallbackQuery,
    user: User,
    session_factory: SessionFactory,
    llm: LLMClient,
    settings: Settings,
    usage: UsageLimiter,
    alerter: AdminAlerter,
) -> None:
    card_id = int(query.data.split(":")[2])
    async with session_factory() as session:
        card = await repo.get_card(session, card_id, user_id=user.id)
    if card is None:
        await query.answer("Карточка не найдена.", show_alert=True)
        return

    if not usage.check_and_count(user.id):
        await safe_answer(query, "Дневной лимит запросов исчерпан.", show_alert=True)
        return
    await safe_answer(query, "Генерирую заново…")
    try:
        enrichment = await llm.enrich(card.text, model=settings.model_fast, level=user.level)
    except LLMError:
        logger.exception("regeneration failed for card %d", card_id)
        await alerter.record_llm_failure(query.bot, "regenerate")
        if isinstance(query.message, Message):
            await query.message.answer(FAIL_TEXT)
        return

    async with session_factory() as session:
        card = await repo.get_card(session, card_id, user_id=user.id)
        if card is None:
            return
        new_lemma = enrichment.lemma.strip().lower()
        other = await repo.find_card_by_lemma(session, new_lemma, user_id=user.id)
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
