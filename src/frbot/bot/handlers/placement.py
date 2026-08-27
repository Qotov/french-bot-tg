"""/placement: an 18-question test that sets the learner's level.

Self-declared level is a guess, and a wrong guess miscalibrates every prompt
the bot will ever send. The test takes about three minutes and can be taken
again whenever the learner feels it is wrong.
"""

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from frbot import placement
from frbot.bot import render
from frbot.bot.alerts import AdminAlerter
from frbot.bot.handlers.topic import build_starter_deck
from frbot.bot.onboarding import (
    BUILDING_DECK_TEXT as SHARED_BUILDING_TEXT,
)
from frbot.bot.onboarding import (
    DECK_FAILED_TEXT,
    FIRST_STEPS_TEXT,
)
from frbot.bot.telegram_utils import safe_answer, safe_edit_text
from frbot.config import Settings
from frbot.db import repo
from frbot.db.models import User
from frbot.db.session import SessionFactory
from frbot.llm.client import LLMClient
from frbot.srs.scheduler import SrsScheduler
from frbot.usage import UsageLimiter

BUILDING_DECK_TEXT = SHARED_BUILDING_TEXT
STARTER_DECK_TIMEOUT = 60

logger = logging.getLogger(__name__)

INTRO_TEXT = (
    "🎯 <b>Тест на уровень</b> — 18 коротких вопросов, минуты три.\n\n"
    "Отвечай как есть, не подглядывая: смысл в том, чтобы бот подстроился под "
    "твой настоящий уровень. Не знаешь — выбирай, что кажется вероятнее.\n\n"
    "Прервать можно в любой момент: /stop — тогда оставлю уровень B1, "
    "поменять всегда можно через /level."
)
USE_BUTTONS_TEXT = "Выбери один из вариантов кнопкой ниже 🙂"

SKILL_LABELS = {
    "auxiliaire": "вспомогательные глаголы (être/avoir)",
    "genre": "род существительных",
    "négation": "отрицание и артикли",
    "préposition": "предлоги",
    "accord": "согласование причастия",
    "présent": "спряжение в présent",
    "imparfait vs pc": "imparfait против passé composé",
    "pronom en": "местоимения y / en",
    "relatif": "относительные местоимения",
    "temps": "выражения времени",
    "si-clause": "условные предложения",
    "ordre des pronoms": "порядок местоимений",
    "subjonctif": "сослагательное наклонение",
    "concordance": "согласование времён",
    "accord du participe": "согласование причастия с COD",
    "connecteur": "связки и уступки",
    "subjonctif passé": "subjonctif passé",
    "registre": "формальный регистр",
}

LEVEL_VERDICT = {
    "A2": (
        "Базой владеешь, но настоящее и прошедшее пока требуют внимания. "
        "Бот будет давать короткие фразы и разбирать всё подробно."
    ),
    "B1": (
        "Уверенный B1 — на бытовые темы говоришь свободно. "
        "Бот будет подтягивать тебя к B2: нюансы времён, местоимения, связки."
    ),
    "B2": (
        "Сильный уровень: контролируешь сложные структуры. "
        "Бот перестанет упрощать и будет придираться к регистру и точности."
    ),
}


class PlacementStates(StatesGroup):
    running = State()


def _kb(index: int, options: tuple[str, ...]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=opt, callback_data=f"place:{index}:{i}")
                for i, opt in enumerate(options)
            ]
        ]
    )


def _question_text(index: int, total: int, item: placement.Item) -> str:
    return (
        f"<b>{index + 1}/{total}</b> · {render.esc(item.level)}\n\n"
        f"{render.esc(item.sentence)}"
    )


async def _ask(answer, index: int, state: FSMContext) -> None:
    items = placement.items_in_order()
    item = items[index]
    await state.update_data(index=index)
    await answer(_question_text(index, len(items), item), reply_markup=_kb(index, item.options))


async def cmd_placement(
    message: Message, state: FSMContext, *, onboarding: bool = False
) -> None:
    await state.set_state(PlacementStates.running)
    await state.set_data({"index": 0, "answers": [], "onboarding": onboarding})
    await message.answer(INTRO_TEXT)
    await _ask(message.answer, 0, state)


async def handle_typed_answer(message: Message) -> None:
    """A typed message during the test must not become a vocab card."""
    await message.answer(USE_BUTTONS_TEXT)


async def cmd_stop_placement(
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
    """Abandoning the test must not abandon onboarding.

    Eighteen taps is a long funnel for a brand-new user, so stopping partway is
    expected — and they must still leave with a level, a deck, and the
    first-steps message that carries the data notice.
    """
    onboarding = bool((await state.get_data()).get("onboarding"))
    await state.clear()
    await message.answer(
        f"Хорошо, остановились. Оставляю уровень <b>{render.esc(user.level)}</b> — "
        f"поменять можно через /level, пройти тест заново — /placement."
    )
    if onboarding:
        await _ensure_starter_deck(
            message, user, session_factory, llm, srs, settings, usage, alerter
        )
        await message.answer(FIRST_STEPS_TEXT)


async def _ensure_starter_deck(
    message: Message,
    user: User,
    session_factory: SessionFactory,
    llm: LLMClient,
    srs: SrsScheduler,
    settings: Settings,
    usage: UsageLimiter,
    alerter: AdminAlerter,
) -> None:
    async with session_factory() as session:
        if await repo.count_cards(session, user_id=user.id):
            return
    await message.answer(BUILDING_DECK_TEXT)
    try:
        async with asyncio.timeout(STARTER_DECK_TIMEOUT):
            created = await build_starter_deck(
                user, session_factory, llm, srs, settings, usage, alerter, message.bot
            )
    except TimeoutError:
        logger.warning("starter deck timed out for user %d", user.id)
        created = []
    if created:
        preview = ", ".join(render.esc(lemma) for lemma in created[:6])
        await message.answer(
            f"🎁 Готово — {len(created)} карточек: {preview}…\n\nПопробуй: /review"
        )
    else:
        await message.answer(DECK_FAILED_TEXT)


async def on_answer(
    query: CallbackQuery,
    state: FSMContext,
    user: User,
    session_factory: SessionFactory,
    llm: LLMClient,
    srs: SrsScheduler,
    settings: Settings,
    usage: UsageLimiter,
    alerter: AdminAlerter,
) -> None:
    if await state.get_state() != PlacementStates.running.state:
        await safe_answer(query, "Тест не активен — начать заново: /placement")
        return
    _, index_raw, option_raw = query.data.split(":")
    index, option = int(index_raw), int(option_raw)

    data = await state.get_data()
    if index != data.get("index"):
        await safe_answer(query, "Этот вопрос уже пройден.")
        return

    items = placement.items_in_order()
    item = items[index]
    chosen = item.options[option]
    correct = chosen == item.correct
    answers = [*data.get("answers", []), [item.level, item.skill, correct]]
    await state.update_data(answers=answers)

    # No per-question feedback: it would turn a measurement into a lesson and
    # let the learner recalibrate mid-test.
    if isinstance(query.message, Message):
        await safe_edit_text(
            query.message, f"{_question_text(index, len(items), item)}\n\n✔️ {render.esc(chosen)}"
        )
    await safe_answer(query)

    if index + 1 < len(items):
        if isinstance(query.message, Message):
            await _ask(query.message.answer, index + 1, state)
        return

    await _finish(
        query, state, user, session_factory, answers, llm, srs, settings, usage, alerter
    )


async def _finish(
    query: CallbackQuery,
    state: FSMContext,
    user: User,
    session_factory: SessionFactory,
    answers: list[list],
    llm: LLMClient,
    srs: SrsScheduler,
    settings: Settings,
    usage: UsageLimiter,
    alerter: AdminAlerter,
) -> None:
    onboarding = bool((await state.get_data()).get("onboarding"))
    await state.clear()
    detail = [(level, skill, bool(ok)) for level, skill, ok in answers]
    level = placement.level_from_answers([(lvl, ok) for lvl, _s, ok in detail])
    counts = placement.score_by_band([(lvl, ok) for lvl, _s, ok in detail])

    async with session_factory() as session:
        await repo.set_user_level(session, user.id, level)
        await session.commit()
        user = await repo.get_user(session, user.id)
        deck_size = await repo.count_cards(session, user_id=user.id)
    logger.info("placement for user %d: %s %s", user.id, level, counts)

    lines = [
        f"🎯 <b>Твой уровень: {render.esc(level)}</b>",
        "",
        *(
            f"{band}: {counts[band]}/{placement.PER_BAND}"
            for band in placement.LEVELS_ORDER
        ),
        "",
        LEVEL_VERDICT[level],
    ]
    weak = placement.weakest_skills(detail)
    if weak:
        lines.append("")
        lines.append("Над чем стоит поработать:")
        lines.extend(f"• {render.esc(SKILL_LABELS.get(s, s))}" for s in weak)
    lines.append("")
    lines.append("Уровень можно поменять вручную: /level")

    if not isinstance(query.message, Message):
        return
    await query.message.answer(render.fit_lines(lines))

    # Someone who took the test instead of self-declaring must still end up
    # with a deck — otherwise the honest answer is punished with an empty bot.
    if deck_size == 0:
        await _ensure_starter_deck(
            query.message, user, session_factory, llm, srs, settings, usage, alerter
        )

    if onboarding:
        # Everyone who joins must see this once: it is the only place the
        # data-retention notice and the "what do I do now" steps appear.
        await query.message.answer(FIRST_STEPS_TEXT)


def create_router() -> Router:
    router = Router(name="placement")
    router.message.register(cmd_placement, Command("placement"))
    # Registered before the talk router's global /stop so an abandoned test is
    # handled by the flow that owns it.
    router.message.register(cmd_stop_placement, PlacementStates.running, Command("stop"))
    router.message.register(
        handle_typed_answer, PlacementStates.running, F.text, ~F.text.startswith("/")
    )
    router.callback_query.register(on_answer, F.data.startswith("place:"))
    return router
