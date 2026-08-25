"""/review: FSRS review session driven by aiogram FSM.

FSM data: queue (card ids), index, total, reviewed, again.
Callback data: review:start | review:show:<card_id> | review:grade:<card_id>:<rating>
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from frbot.bot import render
from frbot.bot.keyboards import grade_kb, show_answer_kb
from frbot.config import Settings
from frbot.db import repo
from frbot.db.session import SessionFactory
from frbot.srs.queue import build_queue
from frbot.srs.scheduler import SrsScheduler
from frbot.timeutil import tomorrow_end_utc

logger = logging.getLogger(__name__)

NOT_ACTIVE_TEXT = "Сессия не активна — начни заново: /review"
EMPTY_TEXT = "🎉 Сегодня нечего повторять."


class ReviewStates(StatesGroup):
    reviewing = State()


def _front_text(card, index: int, total: int) -> str:
    return f"<b>{index + 1}/{total}</b>\n\n{render.card_front(card)}"


def _full_text(card, index: int, total: int) -> str:
    return f"<b>{index + 1}/{total}</b>\n\n{render.card_front(card)}\n—\n{render.card_back(card)}"


async def start_session(
    answer: Callable[..., Awaitable[Message]],
    state: FSMContext,
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        cfg = await repo.get_effective_config(session, settings)
        queue = await build_queue(
            session,
            now=now,
            tz=settings.tz,
            session_max=cfg.session_max,
            daily_new_limit=cfg.daily_new_limit,
        )
        if queue.total == 0:
            await state.clear()
            await answer(EMPTY_TEXT)
            return
        first_card = await repo.get_card(session, queue.card_ids[0])

    await state.set_state(ReviewStates.reviewing)
    await state.set_data(
        {
            "queue": queue.card_ids,
            "index": 0,
            "total": queue.total,
            "reviewed": 0,
            "again": 0,
        }
    )
    logger.info("review session: %d due, %d new", queue.due_count, queue.new_count)
    await answer(f"📚 Повторение: {queue.due_count} по расписанию, {queue.new_count} новых.")
    await answer(
        _front_text(first_card, 0, queue.total),
        reply_markup=show_answer_kb(first_card.id),
    )


async def cmd_review(
    message: Message,
    state: FSMContext,
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    await start_session(message.answer, state, session_factory, settings)


async def on_start_callback(
    query: CallbackQuery,
    state: FSMContext,
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    await query.answer()
    if isinstance(query.message, Message):
        await start_session(query.message.answer, state, session_factory, settings)


async def _current_card_id(state: FSMContext) -> int | None:
    data = await state.get_data()
    queue: list[int] = data.get("queue", [])
    index: int = data.get("index", 0)
    if index >= len(queue):
        return None
    return queue[index]


async def on_show(
    query: CallbackQuery,
    state: FSMContext,
    session_factory: SessionFactory,
) -> None:
    if await state.get_state() != ReviewStates.reviewing.state:
        await query.answer(NOT_ACTIVE_TEXT, show_alert=True)
        return
    card_id = int(query.data.split(":")[2])
    if card_id != await _current_card_id(state):
        await query.answer("Эта карточка уже пройдена.")
        return

    data = await state.get_data()
    async with session_factory() as session:
        card = await repo.get_card(session, card_id)
    if card is None:
        await query.answer("Карточка была удалена.")
        await _advance(query, state, session_factory)
        return

    if isinstance(query.message, Message):
        await query.message.edit_text(
            _full_text(card, data["index"], data["total"]),
            reply_markup=grade_kb(card.id),
        )
    await query.answer()


async def on_grade(
    query: CallbackQuery,
    state: FSMContext,
    session_factory: SessionFactory,
    srs: SrsScheduler,
    settings: Settings,
) -> None:
    if await state.get_state() != ReviewStates.reviewing.state:
        await query.answer(NOT_ACTIVE_TEXT, show_alert=True)
        return
    _, _, card_id_raw, rating_raw = query.data.split(":")
    card_id, rating = int(card_id_raw), int(rating_raw)
    if rating not in (1, 2, 3, 4):
        await query.answer("Некорректная оценка.")
        return
    if card_id != await _current_card_id(state):
        await query.answer("Эта карточка уже пройдена.")
        return

    now = datetime.now(UTC)
    async with session_factory() as session:
        card = await repo.get_card(session, card_id)
        if card is not None:
            result = srs.review(card.fsrs, rating, now)
            await repo.apply_review(session, card, result, rating=rating, now=now)
            await session.commit()
            logger.info("graded card %d rating=%d next due %s", card_id, rating, result.due)

    if card is not None:
        data = await state.get_data()
        await state.update_data(
            reviewed=data["reviewed"] + 1,
            again=data["again"] + (1 if rating == 1 else 0),
        )
    if isinstance(query.message, Message):
        await query.message.edit_reply_markup(reply_markup=None)
    await _advance(query, state, session_factory, settings=settings)


async def _advance(
    query: CallbackQuery,
    state: FSMContext,
    session_factory: SessionFactory,
    settings: Settings | None = None,
) -> None:
    data = await state.get_data()
    index = data["index"] + 1
    await state.update_data(index=index)

    if index >= data["total"]:
        await _finish(query, state, session_factory, settings)
        return

    async with session_factory() as session:
        card = await repo.get_card(session, data["queue"][index])
    if card is None:
        await _advance(query, state, session_factory, settings)
        return
    if isinstance(query.message, Message):
        await query.message.answer(
            _front_text(card, index, data["total"]), reply_markup=show_answer_kb(card.id)
        )
    await query.answer()


async def _finish(
    query: CallbackQuery,
    state: FSMContext,
    session_factory: SessionFactory,
    settings: Settings | None,
) -> None:
    data = await state.get_data()
    await state.clear()

    tomorrow_due = 0
    if settings is not None:
        now = datetime.now(UTC)
        async with session_factory() as session:
            tomorrow_due = await repo.count_due(session, until=tomorrow_end_utc(now, settings.tz))

    summary = (
        f"✅ Готово!\n"
        f"Повторено: {data['reviewed']}\n"
        f"Again: {data['again']}\n"
        f"Завтра к повторению: {tomorrow_due}"
    )
    if isinstance(query.message, Message):
        await query.message.answer(summary)
    await query.answer()


def create_router() -> Router:
    router = Router(name="review")
    router.message.register(cmd_review, Command("review"))
    router.callback_query.register(on_start_callback, F.data == "review:start")
    router.callback_query.register(on_show, F.data.startswith("review:show:"))
    router.callback_query.register(on_grade, F.data.startswith("review:grade:"))
    return router
