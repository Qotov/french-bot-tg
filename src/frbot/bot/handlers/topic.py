"""/topic: generate a pack of B2-level words on any topic and add the selected
ones as cards.

Flow: /topic <тема> [N]  ->  Gemini proposes N words (excluding known lemmas)
-> the user toggles words in an inline keyboard -> selected words are enriched
concurrently and saved with the usual dedupe.
"""

import asyncio
import logging

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from frbot.bot import render
from frbot.bot.keyboards import topic_select_kb
from frbot.bot.telegram_utils import safe_answer, safe_edit_text
from frbot.config import Settings
from frbot.db import repo
from frbot.db.session import SessionFactory
from frbot.llm.client import LLMClient, LLMError
from frbot.llm.schemas import TOPIC_WORDS_MAX, Enrichment
from frbot.srs.scheduler import SrsScheduler

logger = logging.getLogger(__name__)

DEFAULT_COUNT = 10
MIN_COUNT = 3
ENRICH_CONCURRENCY = 5
FAIL_TEXT = "⚠️ Не получилось собрать слова по теме. Попробуй ещё раз через минуту."
ASK_TOPIC_TEXT = "На какую тему собрать слова? Можно указать количество, например: «ресторан 10»."


class TopicStates(StatesGroup):
    choosing = State()
    selecting = State()


COUNT_ARG_MAX = 30  # an edge number above this is part of the topic ("les années 80")


def parse_topic_args(raw: str) -> tuple[str, int] | None:
    """ "ресторан 15" / "15 ресторан" / "ресторан" -> (topic, count).

    Only a leading or trailing number in a plausible range counts as the word
    count; interior or large numbers stay in the topic itself.
    """
    parts = raw.split()
    if not parts:
        return None
    count = DEFAULT_COUNT
    if len(parts) >= 2 and parts[-1].isdigit() and 1 <= int(parts[-1]) <= COUNT_ARG_MAX:
        count = int(parts.pop())
    elif len(parts) >= 2 and parts[0].isdigit() and 1 <= int(parts[0]) <= COUNT_ARG_MAX:
        count = int(parts.pop(0))
    topic = " ".join(parts).strip(" ,.")
    if not topic or topic.isdigit():
        return None
    return topic, max(MIN_COUNT, min(count, TOPIC_WORDS_MAX))


async def cmd_topic(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    session_factory: SessionFactory,
    llm: LLMClient,
    settings: Settings,
) -> None:
    parsed = parse_topic_args(command.args or "")
    if parsed is None:
        await state.set_state(TopicStates.choosing)
        await message.answer(ASK_TOPIC_TEXT)
        return
    await _generate(message, state, parsed, session_factory, llm, settings)


async def handle_topic_input(
    message: Message,
    state: FSMContext,
    session_factory: SessionFactory,
    llm: LLMClient,
    settings: Settings,
) -> None:
    parsed = parse_topic_args(message.text or "")
    if parsed is None:
        await message.answer(ASK_TOPIC_TEXT)
        return
    await _generate(message, state, parsed, session_factory, llm, settings)


def _selection_text(topic: str, words: list[dict]) -> str:
    lines = [f"📚 Тема: <b>{render.esc(topic)}</b> — выбери слова для карточек:"]
    lines.extend(
        f"• <b>{render.esc(w['lemma'])}</b> — {render.esc(w['translation_ru'])}" for w in words
    )
    return "\n".join(lines)


async def _generate(
    message: Message,
    state: FSMContext,
    parsed: tuple[str, int],
    session_factory: SessionFactory,
    llm: LLMClient,
    settings: Settings,
) -> None:
    topic, count = parsed
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    async with session_factory() as session:
        known = await repo.get_recent_lemmas(session, limit=100)
    try:
        word_list = await llm.topic_words(topic, count, known, model=settings.model_fast)
    except LLMError:
        logger.exception("topic word generation failed for %r", topic)
        await state.clear()
        await message.answer(FAIL_TEXT)
        return

    # Drop anything already in the deck.
    fresh = []
    async with session_factory() as session:
        for word in word_list.words:
            if await repo.find_card_by_lemma(session, word.lemma) is None:
                fresh.append(word.model_dump())
    if not fresh:
        await state.clear()
        await message.answer("🎉 Все предложенные слова по этой теме уже в колоде.")
        return

    selected = list(range(len(fresh)))
    await state.set_state(TopicStates.selecting)
    await state.set_data({"topic": topic, "words": fresh, "selected": selected})
    logger.info("topic %r: %d candidate words", topic, len(fresh))
    sent = await message.answer(
        _selection_text(topic, fresh),
        reply_markup=topic_select_kb([w["lemma"] for w in fresh], set(selected)),
    )
    # Bind the pack to its message so a superseded pack's keyboard goes inert.
    await state.update_data(message_id=sent.message_id)


async def _is_stale_pack(query: CallbackQuery, state: FSMContext) -> bool:
    """A keyboard from a superseded /topic pack must not drive the new pack."""
    data = await state.get_data()
    bound_id = data.get("message_id")
    if (
        bound_id is not None
        and isinstance(query.message, Message)
        and query.message.message_id != bound_id
    ):
        await safe_edit_text(query.message, "Эта подборка устарела.")
        await safe_answer(query, "Эта подборка уже не активна.")
        return True
    return False


async def on_toggle(query: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() != TopicStates.selecting.state:
        await safe_answer(query, "Подборка уже не активна — /topic")
        return
    if await _is_stale_pack(query, state):
        return
    index = int(query.data.split(":")[2])
    data = await state.get_data()
    words: list[dict] = data["words"]
    if not 0 <= index < len(words):
        await safe_answer(query)
        return
    selected = set(data["selected"])
    if index in selected:
        selected.discard(index)
    else:
        selected.add(index)
    await state.update_data(selected=sorted(selected))
    if isinstance(query.message, Message):
        await safe_edit_text(
            query.message,
            _selection_text(data["topic"], words),
            reply_markup=topic_select_kb([w["lemma"] for w in words], selected),
        )
    await safe_answer(query)


async def on_cancel(query: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() == TopicStates.selecting.state:
        if await _is_stale_pack(query, state):
            return
        await state.clear()
    if isinstance(query.message, Message):
        await safe_edit_text(query.message, "Подборка отменена.")
    await safe_answer(query)


async def on_save(
    query: CallbackQuery,
    state: FSMContext,
    session_factory: SessionFactory,
    llm: LLMClient,
    srs: SrsScheduler,
    settings: Settings,
) -> None:
    if await state.get_state() != TopicStates.selecting.state:
        await safe_answer(query, "Подборка уже не активна — /topic")
        return
    if await _is_stale_pack(query, state):
        return
    data = await state.get_data()
    words: list[dict] = data["words"]
    chosen = [words[i] for i in data["selected"] if 0 <= i < len(words)]
    await state.clear()
    if not chosen:
        if isinstance(query.message, Message):
            await safe_edit_text(query.message, "Ничего не выбрано — подборка закрыта.")
        await safe_answer(query)
        return

    await safe_answer(query, f"Создаю {len(chosen)} карточек…")
    if isinstance(query.message, Message):
        await safe_edit_text(
            query.message, _selection_text(data["topic"], words) + "\n\n⏳ Создаю карточки…"
        )

    semaphore = asyncio.Semaphore(ENRICH_CONCURRENCY)

    async def enrich_one(word: dict) -> Enrichment | Exception:
        async with semaphore:
            try:
                return await llm.enrich(word["lemma"], model=settings.model_fast)
            except LLMError as exc:
                return exc

    results = await asyncio.gather(*(enrich_one(w) for w in chosen))

    created: list[str] = []
    skipped = 0
    failed = 0
    async with session_factory() as session:
        for word, result in zip(chosen, results, strict=True):
            if isinstance(result, Exception):
                logger.warning("topic enrich failed for %r", word["lemma"])
                failed += 1
                continue
            if await repo.find_card_by_lemma(session, result.lemma) is not None:
                skipped += 1
                continue
            await repo.create_vocab_card(
                session, srs, text=word["lemma"], enrichment=result.model_dump()
            )
            created.append(result.lemma)
        await session.commit()

    lines = [f"✅ Добавлено карточек: {len(created)}"]
    if created:
        lines.append(", ".join(render.esc(lemma) for lemma in created))
    if skipped:
        lines.append(f"Уже были в колоде: {skipped}")
    if failed:
        lines.append(f"Не получилось: {failed} — попробуй добавить их отдельно.")
    logger.info("topic save: %d created, %d skipped, %d failed", len(created), skipped, failed)
    if isinstance(query.message, Message):
        await query.message.answer("\n".join(lines))


async def on_voice_while_choosing(message: Message) -> None:
    await message.answer("Пришли тему текстом, пожалуйста (или /stop, чтобы выйти).")


def create_router() -> Router:
    router = Router(name="topic")
    router.message.register(cmd_topic, Command("topic"))
    router.message.register(
        handle_topic_input, TopicStates.choosing, F.text, ~F.text.startswith("/")
    )
    router.message.register(on_voice_while_choosing, TopicStates.choosing, F.voice)
    router.callback_query.register(on_toggle, F.data.startswith("topic:toggle:"))
    router.callback_query.register(on_save, F.data == "topic:save")
    router.callback_query.register(on_cancel, F.data == "topic:cancel")
    return router
