"""/talk: free conversation with the tutor, text or voice.

The bot opens with a French question; every learner message (typed or spoken)
gets corrections of its mistakes plus a conversational reply. Mistakes become
error cards under the same daily cap as /write. /stop ends the dialogue.
"""

import logging
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
from frbot.db.models import CardKind
from frbot.db.session import SessionFactory
from frbot.llm.client import LLMClient, LLMError
from frbot.llm.schemas import TalkTurn
from frbot.srs.scheduler import SrsScheduler
from frbot.timeutil import day_start_utc

logger = logging.getLogger(__name__)

HISTORY_MAX = 12  # kept lines ("Élève: …" / "Tuteur: …")
ERRORS_SHOWN_MAX = 5
FAIL_TEXT = "⚠️ Не расслышал / не получилось ответить. Скажи ещё раз?"
OPEN_FAIL_TEXT = "⚠️ Не получилось начать диалог. Попробуй /talk ещё раз через минуту."
VOICE_FAIL_TEXT = "⚠️ Не получилось скачать голосовое. Попробуй ещё раз."
STOPPED_TEXT = "👋 Диалог завершён. Начать новый: /talk"
NOT_TALKING_TEXT = "Сейчас нет активного диалога. Начать: /talk"


class TalkStates(StatesGroup):
    talking = State()


async def cmd_talk(
    message: Message,
    state: FSMContext,
    session_factory: SessionFactory,
    llm: LLMClient,
    settings: Settings,
) -> None:
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    async with session_factory() as session:
        lemmas = await repo.get_recent_lemmas(session, limit=10)
    try:
        turn = await llm.talk_open(lemmas, model=settings.model_smart)
    except LLMError:
        logger.exception("talk opener failed")
        await message.answer(OPEN_FAIL_TEXT)
        return
    await state.set_state(TalkStates.talking)
    await state.set_data({"history": [f"Tuteur: {turn.reply_fr}"]})
    logger.info("talk session started")
    await message.answer(
        "💬 Диалог начат — отвечай текстом или голосом. Закончить: /stop\n\n"
        f"🇫🇷 {render.esc(turn.reply_fr)}"
    )


async def cmd_stop(message: Message, state: FSMContext) -> None:
    if await state.get_state() == TalkStates.talking.state:
        await state.clear()
        await message.answer(STOPPED_TEXT)
    else:
        await message.answer(NOT_TALKING_TEXT)


async def handle_text_turn(
    message: Message,
    state: FSMContext,
    session_factory: SessionFactory,
    llm: LLMClient,
    srs: SrsScheduler,
    settings: Settings,
) -> None:
    text = (message.text or "").strip()
    if not text:
        return
    await _turn(message, state, session_factory, llm, srs, settings, text=text)


async def handle_voice_turn(
    message: Message,
    state: FSMContext,
    session_factory: SessionFactory,
    llm: LLMClient,
    srs: SrsScheduler,
    settings: Settings,
) -> None:
    if message.voice.duration > VOICE_MAX_DURATION:
        await message.answer(VOICE_TOO_LONG_TEXT)
        return
    audio = await download_voice(message)
    if audio is None:
        await message.answer(VOICE_FAIL_TEXT)
        return
    await _turn(message, state, session_factory, llm, srs, settings, audio=audio)


async def _create_error_cards(
    session_factory: SessionFactory,
    srs: SrsScheduler,
    settings: Settings,
    turn: TalkTurn,
    said: str,
) -> int:
    sentence = turn.corrected_fr.strip() or said
    created = 0
    now = datetime.now(UTC)
    async with session_factory() as session:
        cap_left = repo.ERROR_CARDS_DAILY_CAP - await repo.count_error_cards_created_since(
            session, since=day_start_utc(now, settings.tz)
        )
        for error in turn.errors:
            if cap_left <= 0:
                break
            card = await repo.create_error_card(
                session,
                srs,
                kind=CardKind.error.value,
                sentence=sentence,
                original=error.original,
                corrected=error.corrected,
                err_type=error.type,
                explanation_ru=error.explanation_ru,
                front=render.make_gapped(sentence, error.corrected),
            )
            if card is not None:
                created += 1
                cap_left -= 1
        await session.commit()
    return created


def _turn_reply(turn: TalkTurn, created: int, *, from_voice: bool) -> str:
    lines = []
    if from_voice and turn.transcript.strip():
        lines.append(f"🎙 <i>{render.esc(turn.transcript.strip())}</i>")
    for error in turn.errors[:ERRORS_SHOWN_MAX]:
        lines.append(
            f"✏️ ❌ {render.esc(error.original)} → ✅ <b>{render.esc(error.corrected)}</b>"
            f" — {render.esc(error.explanation_ru)}"
        )
    hidden = len(turn.errors) - ERRORS_SHOWN_MAX
    if hidden > 0:
        lines.append(f"✏️ … и ещё {hidden}.")
    if created:
        lines.append(f"➕ Карточек из ошибок: {created}")
    if lines:
        lines.append("")
    lines.append(f"🇫🇷 {render.esc(turn.reply_fr)}")
    return "\n".join(lines)


async def _turn(
    message: Message,
    state: FSMContext,
    session_factory: SessionFactory,
    llm: LLMClient,
    srs: SrsScheduler,
    settings: Settings,
    *,
    text: str | None = None,
    audio: tuple[bytes, str] | None = None,
) -> None:
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    data = await state.get_data()
    history: list[str] = data.get("history", [])
    try:
        turn = await llm.talk_turn(
            "\n".join(history), model=settings.model_smart, text=text, audio=audio
        )
    except LLMError:
        logger.exception("talk turn failed")
        await message.answer(FAIL_TEXT)  # state kept: the user can just retry
        return

    said = (text or turn.transcript).strip()
    created = 0
    if turn.errors and said:
        created = await _create_error_cards(session_factory, srs, settings, turn, said)

    history.extend([f"Élève: {said}", f"Tuteur: {turn.reply_fr}"])
    await state.update_data(history=history[-HISTORY_MAX:])
    logger.info("talk turn: %d errors, %d cards", len(turn.errors), created)
    await message.answer(_turn_reply(turn, created, from_voice=audio is not None))


def create_router() -> Router:
    router = Router(name="talk")
    router.message.register(cmd_talk, Command("talk"))
    router.message.register(cmd_stop, Command("stop"))
    router.message.register(handle_text_turn, TalkStates.talking, F.text, ~F.text.startswith("/"))
    router.message.register(handle_voice_turn, TalkStates.talking, F.voice)
    return router
