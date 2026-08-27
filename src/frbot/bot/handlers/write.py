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

from frbot import tracks
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
from frbot.srs.scheduler import SrsScheduler
from frbot.timeutil import day_end_utc, day_start_utc
from frbot.usage import OVER_LIMIT_TEXT, UsageLimiter

logger = logging.getLogger(__name__)

FAIL_TEXT = "⚠️ Не получилось проверить текст. Пришли его ещё раз через минуту."
VOICE_FAIL_TEXT = "⚠️ Не получилось разобрать голосовое. Скажи ещё раз или напиши текстом."
ANSWER_MAX_LEN = 1500
# An exam essay is far longer than a three-sentence exercise; the cap has to
# follow the task or the bot would reject exactly the work it asked for.
EXAM_ANSWER_MAX_LEN = 4000


def exam_answer_limit(user: User) -> int:
    return EXAM_ANSWER_MAX_LEN if tracks.is_exam(user.track) else ANSWER_MAX_LEN

WRITING_SITUATIONS = [
    "Raconte ce que tu as fait hier soir.",
    "Décris ton petit déjeuner idéal.",
    "Décris ton trajet préféré dans ta ville.",
    "Raconte un petit problème que tu as eu cette semaine.",
    "Décris ce que tu vois par ta fenêtre en ce moment.",
    "Raconte ton dernier voyage, même court.",
    "Décris ton plat préféré et comment on le prépare.",
    "Raconte ce que tu feras le week-end prochain.",
    "Décris une personne de ta famille.",
    "Raconte un souvenir d'école.",
    "Raconte comment s'est passée ta journée d'hier, du matin au soir.",
    "Décris ta pièce préférée dans ton logement.",
    "Raconte la dernière fois que tu as bien ri.",
    "Décris ce que tu fais quand tu ne peux pas dormir.",
    "Raconte une habitude que tu aimerais changer.",
    "Décris ton dimanche typique.",
    "Raconte une fois où tu t'es perdu(e) quelque part.",
    "Décris le temps qu'il fait aujourd'hui et ce que ça change pour toi.",
    "Tu écris un message à un ami pour proposer une sortie ce week-end.",
    "Tu recommandes un film ou une série à un collègue.",
    "Tu laisses un avis sur un restaurant où tu as mangé récemment.",
    "Tu écris à ton propriétaire pour signaler un problème dans l'appartement.",
    "Tu annules un rendez-vous et proposes une autre date.",
    "Tu demandes un renseignement à la mairie pour un document.",
    "Tu écris au service client parce qu'une commande n'est pas arrivée.",
    "Tu laisses un mot à ton voisin pour lui demander un service.",
    "Tu réponds à une invitation que tu dois refuser poliment.",
    "Tu expliques à un livreur comment trouver ton immeuble.",
    "Tu prends rendez-vous chez le médecin en expliquant ton problème.",
    "Tu écris une petite annonce pour vendre un objet dont tu n'as plus besoin.",
    "Tu demandes à ton banquier des explications sur des frais.",
    "Tu écris à une école pour demander des informations sur un cours.",
    "Tu expliques à un ami pourquoi tu apprends le français.",
    "Donne ton avis : vaut-il mieux vivre en ville ou à la campagne ?",
    "Explique pourquoi tu aimes (ou pas) les réseaux sociaux.",
    "Raconte un livre ou un article qui t'a marqué(e) récemment.",
    "Explique ce qui te motive le matin.",
    "Donne ton avis sur le télétravail.",
    "Explique ce que tu changerais dans ta ville si tu étais maire.",
    "Raconte ce que tu ferais avec une semaine entièrement libre.",
    "Explique à quelqu'un pourquoi ton métier est intéressant.",
    "Donne un conseil à quelqu'un qui commence à apprendre ta langue maternelle.",
    "Explique une tradition de ton pays à un ami français.",
    "Raconte ce qui te manque le plus quand tu voyages.",
    "Tu te présentes brièvement à une nouvelle équipe.",
    "Tu expliques à un collègue ce sur quoi tu travailles en ce moment.",
    "Tu écris un court message pour féliciter quelqu'un.",
    "Tu racontes à un ami une bonne nouvelle que tu viens d'apprendre.",
    "Tu demandes de l'aide à quelqu'un pour un déménagement.",
    "Tu expliques pourquoi tu seras en retard et proposes une solution.",
]


class WriteStates(StatesGroup):
    awaiting_answer = State()


def _build_prompt(situation: str, words: list[str]) -> str:
    if words:
        return f"{situation} Utilise les mots : {', '.join(words)}."
    return situation


def _pick_task(track: tracks.Track, words: list[str]) -> tuple[str, str]:
    """Returns (situation shown to the learner, prompt sent to the corrector)."""
    if tracks.is_exam(track.slug) and track.tasks:
        situation = random.choice(track.tasks)
        # Exam tasks are self-contained; forcing vocabulary into them would
        # break the format the learner is being marked on.
        return situation, situation
    situation = random.choice(WRITING_SITUATIONS)
    return situation, _build_prompt(situation, words)


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
            session, user_id=user.id, due_until=day_end_utc(now, repo.user_tz(user, settings))
        )
        track = tracks.get(user.track)
        situation, prompt = _pick_task(track, words)
        writing = await repo.create_writing(session, prompt, user_id=user.id)
        await session.commit()
        writing_id = writing.id

    exam = tracks.is_exam(track.slug)
    header = f"✍️ <b>{render.esc(track.title)}</b>" if exam else "✍️ <b>Задание:</b>"
    lines = [f"{header} {render.esc(situation)}" if not exam else header]
    if exam:
        lines.append("")
        lines.append(render.esc(situation))
    if words and not exam:
        pretty = ", ".join(f"<b>{render.esc(w)}</b>" for w in words)
        lines.append(f"Используй слова: {pretty}")
    low, high = track.word_target
    lines.append("")
    lines.append(
        f"Объём: {low}–{high} слов." if exam else "Напиши 2–3 предложения по-французски."
    )

    # Send BEFORE arming the state: if the send fails (blocked bot, network),
    # the user must not be left waiting to answer a prompt they never saw.
    await answer("\n".join(lines))

    await state.set_state(WriteStates.awaiting_answer)
    await state.set_data({"writing_id": writing_id, "prompt": prompt})
    logger.info("writing prompt %d sent: %s", writing_id, situation)


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
    alerter: AdminAlerter,
) -> None:
    answer_text = (message.text or "").strip()
    if not answer_text:
        return
    await process_answer(
        message, answer_text, state, user, session_factory, llm, srs, settings, usage, alerter
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
    alerter: AdminAlerter,
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
        await alerter.record_llm_failure(message.bot, "transcribe")
        await message.answer(VOICE_FAIL_TEXT)
        return
    answer_text = transcript.transcript.strip()
    if not answer_text:
        await message.answer("🤔 Не расслышал ответа — скажи ещё раз или напиши текстом.")
        return
    shown = answer_text if len(answer_text) <= 1000 else answer_text[:1000] + "…"
    await message.answer(f"🎙 <i>{render.esc(shown)}</i>")
    await process_answer(
        message, answer_text, state, user, session_factory, llm, srs, settings, usage, alerter
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
    alerter: AdminAlerter,
) -> None:
    limit = exam_answer_limit(user)
    if len(answer_text) > limit:
        await message.answer(
            f"Слишком длинно (до {limit} символов). Сократи и пришли ещё раз."
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
            prompt,
            answer_text,
            model=settings.model_smart,
            level=user.level,
            criteria=tracks.get(user.track).criteria_ru,
        )
    except LLMError:
        logger.exception("correction failed")
        await alerter.record_llm_failure(message.bot, "correction")
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
            session, user_id=user.id, since=day_start_utc(now, repo.user_tz(user, settings))
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
