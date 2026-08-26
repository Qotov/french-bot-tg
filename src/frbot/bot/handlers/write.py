"""/write: one short daily writing prompt, corrected by MODEL_SMART.

Flow: a situation prompt plus 3 words to use -> the user replies with 2-3
sentences -> correction with per-error explanations in Russian -> each error
becomes an error card (daily cap, dedupe on type + corrected span).

The WRITING_TIME job triggers the same start_writing via jobs/reminders.py.
"""

import logging
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from frbot.bot import render
from frbot.bot.telegram_utils import (
    VOICE_MAX_DURATION,
    VOICE_TOO_LONG_TEXT,
    download_voice,
)
from frbot.config import Settings
from frbot.db import repo
from frbot.db.models import CardKind, User
from frbot.db.session import SessionFactory
from frbot.llm.client import LLMClient, LLMError
from frbot.srs.scheduler import SrsScheduler
from frbot.timeutil import day_end_utc, day_start_utc
from frbot.usage import OVER_LIMIT_TEXT, UsageLimiter

logger = logging.getLogger(__name__)

FAIL_TEXT = "⚠️ Не получилось проверить текст. Пришли его ещё раз через минуту."
VOICE_FAIL_TEXT = "⚠️ Не получилось разобрать голосовое. Скажи ещё раз или напиши текстом."
ANSWER_MAX_LEN = 1500

WRITING_SITUATIONS = [
    "Raconte ce que tu as fait hier soir.",
    "Décris ton petit déjeuner idéal.",
    "Tu écris un message à un ami pour proposer une sortie ce week-end.",
    "Décris ton trajet préféré dans ta ville.",
    "Raconte un petit problème que tu as eu cette semaine.",
    "Tu recommandes un film ou une série à un collègue.",
    "Décris ce que tu vois par ta fenêtre en ce moment.",
    "Raconte ton dernier voyage, même court.",
    "Tu expliques à un ami pourquoi tu apprends le français.",
    "Décris ton plat préféré et comment on le prépare.",
    "Raconte ce que tu feras le week-end prochain.",
    "Tu laisses un avis sur un restaurant où tu as mangé récemment.",
    "Décris une personne de ta famille.",
    "Raconte un souvenir d'école.",
    "Tu écris à ton propriétaire pour signaler un problème dans l'appartement.",
]


class WriteStates(StatesGroup):
    awaiting_answer = State()


def _build_prompt(situation: str, words: list[str]) -> str:
    if words:
        return f"{situation} Utilise les mots : {', '.join(words)}."
    return situation


async def start_writing(
    answer: Callable[..., Awaitable[Message]],
    state: FSMContext,
    user: User,
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        words = await repo.pick_writing_words(
            session, user_id=user.id, due_until=day_end_utc(now, settings.tz)
        )
        situation = random.choice(WRITING_SITUATIONS)
        prompt = _build_prompt(situation, words)
        writing = await repo.create_writing(session, prompt, user_id=user.id)
        await session.commit()
        writing_id = writing.id

    await state.set_state(WriteStates.awaiting_answer)
    await state.set_data({"writing_id": writing_id, "prompt": prompt})
    logger.info("writing prompt %d sent: %s", writing_id, situation)

    lines = [f"✍️ <b>Задание:</b> {render.esc(situation)}"]
    if words:
        pretty = ", ".join(f"<b>{render.esc(w)}</b>" for w in words)
        lines.append(f"Используй слова: {pretty}")
    lines.append("Напиши 2–3 предложения по-французски.")
    await answer("\n".join(lines))


async def cmd_write(
    message: Message,
    state: FSMContext,
    user: User,
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    await start_writing(message.answer, state, user, session_factory, settings)


async def handle_answer(
    message: Message,
    state: FSMContext,
    user: User,
    session_factory: SessionFactory,
    llm: LLMClient,
    srs: SrsScheduler,
    settings: Settings,
    usage: UsageLimiter,
) -> None:
    answer_text = (message.text or "").strip()
    if not answer_text:
        return
    await process_answer(
        message, answer_text, state, user, session_factory, llm, srs, settings, usage
    )


async def handle_voice_answer(
    message: Message,
    state: FSMContext,
    user: User,
    session_factory: SessionFactory,
    llm: LLMClient,
    srs: SrsScheduler,
    settings: Settings,
    usage: UsageLimiter,
) -> None:
    """A spoken answer to the writing prompt: transcribe, show, then correct."""
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
        transcript = await llm.transcribe(data, mime_type, model=settings.model_fast)
    except LLMError:
        logger.exception("voice answer transcription failed")
        await message.answer(VOICE_FAIL_TEXT)
        return
    answer_text = transcript.transcript.strip()
    if not answer_text:
        await message.answer("🤔 Не расслышал ответа — скажи ещё раз или напиши текстом.")
        return
    shown = answer_text if len(answer_text) <= 1000 else answer_text[:1000] + "…"
    await message.answer(f"🎙 <i>{render.esc(shown)}</i>")
    await process_answer(
        message, answer_text, state, user, session_factory, llm, srs, settings, usage
    )


async def process_answer(
    message: Message,
    answer_text: str,
    state: FSMContext,
    user: User,
    session_factory: SessionFactory,
    llm: LLMClient,
    srs: SrsScheduler,
    settings: Settings,
    usage: UsageLimiter,
) -> None:
    if len(answer_text) > ANSWER_MAX_LEN:
        await message.answer(
            f"Слишком длинно — нужно всего 2–3 предложения (до {ANSWER_MAX_LEN} символов). "
            f"Сократи и пришли ещё раз."
        )
        return
    data = await state.get_data()
    prompt: str = data.get("prompt", "")

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    if not usage.check_and_count(user.id):
        await message.answer(OVER_LIMIT_TEXT)
        return
    try:
        correction = await llm.correct(
            prompt, answer_text, model=settings.model_smart, level=user.level
        )
    except LLMError:
        logger.exception("correction failed")
        await message.answer(FAIL_TEXT)  # state is kept so the user can resend
        return

    now = datetime.now(UTC)
    created = 0
    async with session_factory() as session:
        writing_id = data.get("writing_id")
        writing = (
            await repo.get_writing(session, writing_id, user_id=user.id) if writing_id else None
        )
        if writing is not None:
            writing.answer = answer_text
            writing.corrections = correction.model_dump()

        cap_left = repo.ERROR_CARDS_DAILY_CAP - await repo.count_error_cards_created_since(
            session, user_id=user.id, since=day_start_utc(now, settings.tz)
        )
        for error in correction.errors:
            if cap_left <= 0:
                break
            card = await repo.create_error_card(
                session,
                srs,
                user_id=user.id,
                kind=CardKind.error.value,
                sentence=correction.corrected_text,
                original=error.original,
                corrected=error.corrected,
                err_type=error.type,
                explanation_ru=error.explanation_ru,
                front=render.make_gapped(correction.corrected_text, error.corrected),
            )
            if card is not None:
                created += 1
                cap_left -= 1
        await session.commit()

    await state.clear()
    logger.info("correction: %d errors, %d new cards", len(correction.errors), created)
    await message.answer(render.correction_message(correction, created))


def create_router() -> Router:
    router = Router(name="write")
    router.message.register(cmd_write, Command("write"))
    router.message.register(
        handle_answer, WriteStates.awaiting_answer, F.text, ~F.text.startswith("/")
    )
    router.message.register(handle_voice_answer, WriteStates.awaiting_answer, F.voice)
    return router
