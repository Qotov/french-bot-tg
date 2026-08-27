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
from frbot.bot.alerts import AdminAlerter
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
from frbot.llm.schemas import TalkTurn
from frbot.srs.scheduler import SrsScheduler
from frbot.timeutil import day_start_utc
from frbot.usage import OVER_LIMIT_TEXT, UsageLimiter

logger = logging.getLogger(__name__)

HISTORY_MAX = 12  # kept lines ("Élève: …" / "Tuteur: …")
ERRORS_SHOWN_MAX = 5
TURN_MAX_LEN = 1000  # keeps the LLM's structured reply inside its token budget
TRANSCRIPT_SHOWN_MAX = 600
FAIL_TEXT = "⚠️ Не расслышал / не получилось ответить. Скажи ещё раз?"
OPEN_FAIL_TEXT = "⚠️ Не получилось начать диалог. Попробуй /talk ещё раз через минуту."
VOICE_FAIL_TEXT = "⚠️ Не получилось скачать голосовое. Попробуй ещё раз."
STOPPED_TEXT = "👋 Диалог завершён. Начать новый: /talk"
NOT_TALKING_TEXT = "Сейчас нет активной сессии. Начать диалог: /talk"
FLOW_STOPPED_TEXT = "👌 Текущая сессия прервана."
TOO_LONG_TEXT = f"Слишком длинно для одной реплики (до {TURN_MAX_LEN} символов) — разбей на части."


class TalkStates(StatesGroup):
    talking = State()


async def cmd_talk(
    message: Message,
    state: FSMContext,
    user: User,
    session_factory: SessionFactory,
    llm: LLMClient,
    settings: Settings,
    usage: UsageLimiter,
    alerter: AdminAlerter,
) -> None:
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    async with session_factory() as session:
        lemmas = await repo.get_recent_lemmas(session, user_id=user.id, limit=10)
    if not usage.check_and_count(user.id):
        await message.answer(OVER_LIMIT_TEXT)
        return
    try:
        turn = await llm.talk_open(lemmas, model=settings.model_smart, level=user.level)
    except LLMError:
        logger.exception("talk opener failed")
        await alerter.record_llm_failure(message.bot, "talk open")
        await message.answer(OPEN_FAIL_TEXT)
        return
    await state.set_state(TalkStates.talking)
    await state.set_data({"history": [f"Tuteur: {turn.reply_fr}"]})
    logger.info("talk session started")
    await message.answer(
        "💬 Диалог начат — отвечай текстом или голосом. Закончить: /stop\n\n"
        f"🇫🇷 {render.esc(_clip(turn.reply_fr, 3000))}"
    )


async def cmd_stop(message: Message, state: FSMContext) -> None:
    """Cancels whatever session is active: /talk, but also a pending /write
    answer, /topic input, or /settings value — the state must never stay armed
    after the user said stop."""
    current = await state.get_state()
    if current is None:
        await message.answer(NOT_TALKING_TEXT)
        return
    await state.clear()
    if current == TalkStates.talking.state:
        await message.answer(STOPPED_TEXT)
    else:
        await message.answer(FLOW_STOPPED_TEXT)


async def handle_text_turn(
    message: Message,
    state: FSMContext,
    user: User,
    session_factory: SessionFactory,
    llm: LLMClient,
    srs: SrsScheduler,
    settings: Settings,
    usage: UsageLimiter,
    alerter: AdminAlerter,
) -> None:
    text = (message.text or "").strip()
    if not text:
        return
    if len(text) > TURN_MAX_LEN:
        await message.answer(TOO_LONG_TEXT)
        return
    await _turn(
        message,
        state,
        user,
        session_factory,
        llm,
        srs,
        settings,
        usage,
        alerter=alerter,
        text=text,
    )


async def handle_voice_turn(
    message: Message,
    state: FSMContext,
    user: User,
    session_factory: SessionFactory,
    llm: LLMClient,
    srs: SrsScheduler,
    settings: Settings,
    usage: UsageLimiter,
    alerter: AdminAlerter,
) -> None:
    if message.voice.duration > VOICE_MAX_DURATION:
        await message.answer(VOICE_TOO_LONG_TEXT)
        return
    audio = await download_voice(message)
    if audio is None:
        await message.answer(VOICE_FAIL_TEXT)
        return
    await _turn(
        message,
        state,
        user,
        session_factory,
        llm,
        srs,
        settings,
        usage,
        alerter=alerter,
        audio=audio,
    )


async def _create_error_cards(
    session_factory: SessionFactory,
    srs: SrsScheduler,
    settings: Settings,
    turn: TalkTurn,
    said: str,
    user: User,
) -> int:
    sentence = turn.corrected_fr.strip() or said
    created = 0
    now = datetime.now(UTC)
    async with session_factory() as session:
        cap_left = repo.ERROR_CARDS_DAILY_CAP - await repo.count_error_cards_created_since(
            session, user_id=user.id, since=day_start_utc(now, settings.tz)
        )
        for error in turn.errors:
            if cap_left <= 0:
                break
            card = await repo.create_error_card(
                session,
                srs,
                user_id=user.id,
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


def _clip(value: str, limit: int) -> str:
    value = value.strip()
    return value if len(value) <= limit else value[:limit] + "…"


def _turn_reply(turn: TalkTurn, created: int, *, from_voice: bool) -> str:
    lines = []
    if from_voice and turn.transcript.strip():
        lines.append(f"🎙 <i>{render.esc(_clip(turn.transcript, TRANSCRIPT_SHOWN_MAX))}</i>")
    for error in turn.errors[:ERRORS_SHOWN_MAX]:
        lines.append(
            f"✏️ ❌ {render.esc(_clip(error.original, 150))}"
            f" → ✅ <b>{render.esc(_clip(error.corrected, 150))}</b>"
            f" — {render.esc(_clip(error.explanation_ru, 200))}"
        )
    hidden = len(turn.errors) - ERRORS_SHOWN_MAX
    if hidden > 0:
        lines.append(f"✏️ … и ещё {hidden}.")
    if created:
        lines.append(f"➕ Карточек из ошибок: {created}")
    if lines:
        lines.append("")
    lines.append(f"🇫🇷 {render.esc(turn.reply_fr)}")
    return render.fit_lines(lines)


async def _turn(
    message: Message,
    state: FSMContext,
    user: User,
    session_factory: SessionFactory,
    llm: LLMClient,
    srs: SrsScheduler,
    settings: Settings,
    usage: UsageLimiter,
    *,
    text: str | None = None,
    audio: tuple[bytes, str] | None = None,
    alerter: AdminAlerter,
) -> None:
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    data = await state.get_data()
    history: list[str] = data.get("history", [])
    if not usage.check_and_count(user.id):
        await message.answer(OVER_LIMIT_TEXT)
        return
    try:
        turn = await llm.talk_turn(
            "\n".join(history),
            model=settings.model_smart,
            level=user.level,
            text=text,
            audio=audio,
        )
    except LLMError:
        logger.exception("talk turn failed")
        await alerter.record_llm_failure(message.bot, "talk turn")
        await message.answer(FAIL_TEXT)  # state kept: the user can just retry
        return

    said = (text or turn.transcript).strip()
    created = 0
    if turn.errors and said:
        created = await _create_error_cards(session_factory, srs, settings, turn, said, user)

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
