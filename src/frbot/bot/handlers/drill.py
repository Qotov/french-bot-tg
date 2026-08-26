"""/drill: 5 cloze items for the weekly grammar topic.

FSM data: items (ClozeItem dumps), index, correct, wrong, topic_slug, topic_title.
Callback data: drill:answer:<item_index>:<option_index>
"""

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from frbot.bot import render
from frbot.bot.keyboards import drill_options_kb
from frbot.bot.telegram_utils import safe_edit_text
from frbot.config import Settings
from frbot.db import repo
from frbot.db.models import CardKind
from frbot.db.session import SessionFactory
from frbot.llm.client import LLMClient, LLMError
from frbot.srs.scheduler import SrsScheduler

logger = logging.getLogger(__name__)

FAIL_TEXT = "⚠️ Не получилось сгенерировать упражнения. Попробуй /drill ещё раз через минуту."
NOT_ACTIVE_TEXT = "Сессия не активна — начни заново: /drill"


class DrillStates(StatesGroup):
    drilling = State()


def _item_text(item: dict, index: int, total: int) -> str:
    return f"<b>{index + 1}/{total}</b>\n\n{render.esc(item['sentence_with_gap'])}"


async def cmd_drill(
    message: Message,
    state: FSMContext,
    session_factory: SessionFactory,
    llm: LLMClient,
    settings: Settings,
) -> None:
    today = datetime.now(UTC).astimezone(ZoneInfo(settings.tz)).date()
    async with session_factory() as session:
        await repo.ensure_drill_topics_seeded(session)
        topic = await repo.get_active_drill_topic(session)
        if topic is None:
            topic = await repo.rotate_drill_topic(session, week=today)
        lemmas = await repo.get_recent_lemmas(session)
        await session.commit()
        topic_slug, topic_title = topic.slug, topic.title_fr

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        cloze = await llm.cloze(topic_title, lemmas, model=settings.model_fast)
    except LLMError:
        logger.exception("cloze generation failed for %s", topic_slug)
        await message.answer(FAIL_TEXT)
        return

    items = [item.model_dump() for item in cloze.items]
    await state.set_state(DrillStates.drilling)
    await state.set_data(
        {
            "items": items,
            "index": 0,
            "correct": 0,
            "wrong": 0,
            "topic_slug": topic_slug,
            "topic_title": topic_title,
        }
    )
    logger.info("drill session started: %s, %d items", topic_slug, len(items))
    await message.answer(f"📚 Тема недели: <b>{render.esc(topic_title)}</b>")
    await message.answer(
        _item_text(items[0], 0, len(items)),
        reply_markup=drill_options_kb(0, items[0]["options"]),
    )


async def on_answer(
    query: CallbackQuery,
    state: FSMContext,
    session_factory: SessionFactory,
    srs: SrsScheduler,
) -> None:
    if await state.get_state() != DrillStates.drilling.state:
        await query.answer(NOT_ACTIVE_TEXT, show_alert=True)
        return
    _, _, item_index_raw, opt_index_raw = query.data.split(":")
    item_index, opt_index = int(item_index_raw), int(opt_index_raw)

    data = await state.get_data()
    items: list[dict] = data["items"]
    if item_index != data["index"] or item_index >= len(items):
        await query.answer("Этот вопрос уже пройден.")
        return
    item = items[item_index]
    if not 0 <= opt_index < len(item["options"]):
        await query.answer("Некорректный вариант.")
        return

    chosen = item["options"][opt_index]
    correct = item["correct"]
    is_correct = chosen == correct
    filled = render.esc(item["sentence_with_gap"]).replace(
        "___", f"<b>{render.esc(correct)}</b>", 1
    )

    lines = [f"<b>{item_index + 1}/{len(items)}</b>", "", filled]
    if is_correct:
        lines.append("✅ Верно!")
    else:
        lines.append(f"❌ Неверно, ты выбрал «{render.esc(chosen)}».")
    lines.append(f"💡 {render.esc(item['explanation_ru'])}")

    if not is_correct:
        async with session_factory() as session:
            card = await repo.create_error_card(
                session,
                srs,
                kind=CardKind.drill_error.value,
                sentence=item["sentence_with_gap"].replace("___", correct, 1),
                original=chosen,
                corrected=correct,
                err_type=data["topic_slug"],
                explanation_ru=item["explanation_ru"],
                front=item["sentence_with_gap"],
            )
            await session.commit()
        if card is not None:
            logger.info("drill error card %d created", card.id)

    if isinstance(query.message, Message):
        await safe_edit_text(query.message, "\n".join(lines))

    index = item_index + 1
    await state.update_data(
        index=index,
        correct=data["correct"] + (1 if is_correct else 0),
        wrong=data["wrong"] + (0 if is_correct else 1),
    )

    if index >= len(items):
        data = await state.get_data()
        await state.clear()
        summary = [f"🏁 Готово: {data['correct']}/{len(items)} верно."]
        if data["wrong"]:
            summary.append("Ошибки станут карточками и придут в /review.")
        if isinstance(query.message, Message):
            await query.message.answer("\n".join(summary))
    elif isinstance(query.message, Message):
        await query.message.answer(
            _item_text(items[index], index, len(items)),
            reply_markup=drill_options_kb(index, items[index]["options"]),
        )
    await query.answer()


def create_router() -> Router:
    router = Router(name="drill")
    router.message.register(cmd_drill, Command("drill"))
    router.callback_query.register(on_answer, F.data.startswith("drill:answer:"))
    return router
