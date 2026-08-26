from datetime import date, timedelta

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, select

from frbot.bot.handlers.drill import FAIL_TEXT, DrillStates, cmd_drill, on_answer
from frbot.db import repo
from frbot.db.models import Card
from frbot.llm.client import LLMError
from frbot.llm.schemas import ClozeSet
from frbot.srs.scheduler import SrsScheduler
from tests.fakes import (
    ALLOWED_USER_ID,
    FakeLLM,
    add_vocab_card,
    load_fixture_json,
    make_callback_query,
    make_message,
)


def cloze() -> ClozeSet:
    return ClozeSet.model_validate(load_fixture_json("cloze_valid.json"))


def make_state(bot) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID),
    )


def srs() -> SrsScheduler:
    return SrsScheduler(desired_retention=0.9)


async def drill_error_count(session_factory) -> int:
    async with session_factory() as session:
        stmt = select(func.count(Card.id)).where(Card.kind == "drill_error")
        return (await session.execute(stmt)).scalar_one()


# ---------------------------------------------------------------- seed + rotation


async def test_seed_is_idempotent_and_ordered(session_factory):
    async with session_factory() as session:
        await repo.ensure_drill_topics_seeded(session)
        await repo.ensure_drill_topics_seeded(session)
        await session.commit()
        topics = list(
            (
                await session.execute(select(repo.DrillTopic).order_by(repo.DrillTopic.position))
            ).scalars()
        )
    assert len(topics) == 10
    assert topics[0].slug == "aux-passe-compose"
    assert topics[-1].slug == "futur-vs-conditionnel"
    assert [t.position for t in topics] == list(range(1, 11))


async def test_rotation_walks_positions_and_wraps(session_factory):
    week = date(2026, 8, 23)
    async with session_factory() as session:
        await repo.ensure_drill_topics_seeded(session)
        first = await repo.rotate_drill_topic(session, week=week)
        assert first.position == 1
        for i in range(9):
            topic = await repo.rotate_drill_topic(session, week=week + timedelta(weeks=i + 1))
        assert topic.position == 10
        wrapped = await repo.rotate_drill_topic(session, week=week + timedelta(weeks=10))
        assert wrapped.position == 1


# ---------------------------------------------------------------- /drill flow


async def test_drill_serves_five_items_for_active_topic(fake_bot, session_factory, settings):
    await add_vocab_card(session_factory, "marché")
    llm = FakeLLM(cloze_results=[cloze()])
    state = make_state(fake_bot)
    await cmd_drill(make_message("/drill", bot=fake_bot), state, session_factory, llm, settings)

    # Topic auto-activated (first ever run -> position 1).
    topic, lemmas = llm.cloze_calls[0]
    assert topic == "Avoir ou être au passé composé"
    assert "marché" in lemmas

    assert await state.get_state() == DrillStates.drilling.state
    data = await state.get_data()
    assert len(data["items"]) == 5

    intro, first = fake_bot.session.sent_messages[:2]
    assert "Тема недели" in intro.text
    assert "1/5" in first.text
    assert "___" in first.text
    options = [b.text for row in first.reply_markup.inline_keyboard for b in row]
    assert sorted(options) == sorted(["suis", "ai", "es"])


async def test_correct_answer_gives_feedback_and_no_card(fake_bot, session_factory, settings):
    llm = FakeLLM(cloze_results=[cloze()])
    state = make_state(fake_bot)
    await cmd_drill(make_message("/drill", bot=fake_bot), state, session_factory, llm, settings)

    # Item 0: correct option is "suis" at index 0.
    await on_answer(
        make_callback_query("drill:answer:0:0", bot=fake_bot), state, session_factory, srs()
    )
    feedback = fake_bot.session.sent("EditMessageText")[-1]
    assert "✅ Верно" in feedback.text
    assert "être" in feedback.text  # explanation shown
    assert await drill_error_count(session_factory) == 0
    next_item = fake_bot.session.sent_messages[-1]
    assert "2/5" in next_item.text


async def test_wrong_answer_creates_drill_error_card(fake_bot, session_factory, settings):
    llm = FakeLLM(cloze_results=[cloze()])
    state = make_state(fake_bot)
    await cmd_drill(make_message("/drill", bot=fake_bot), state, session_factory, llm, settings)

    # Item 0: wrong option "ai" at index 1.
    await on_answer(
        make_callback_query("drill:answer:0:1", bot=fake_bot), state, session_factory, srs()
    )
    feedback = fake_bot.session.sent("EditMessageText")[-1]
    assert "❌" in feedback.text
    assert "«ai»" in feedback.text

    assert await drill_error_count(session_factory) == 1
    async with session_factory() as session:
        card = (
            (await session.execute(select(Card).where(Card.kind == "drill_error"))).scalars().one()
        )
    assert card.error_meta["type"] == "aux-passe-compose"
    assert card.error_meta["corrected"] == "suis"
    assert card.error_meta["original"] == "ai"
    assert "suis" in card.text  # sentence stored with the gap filled


async def test_full_drill_run_summary_and_counts(fake_bot, session_factory, settings):
    llm = FakeLLM(cloze_results=[cloze()])
    state = make_state(fake_bot)
    await cmd_drill(make_message("/drill", bot=fake_bot), state, session_factory, llm, settings)

    items = (await state.get_data())["items"]
    # Answer all: first wrong, rest correct.
    for index, item in enumerate(items):
        correct_idx = item["options"].index(item["correct"])
        chosen = correct_idx if index > 0 else (correct_idx + 1) % 3
        await on_answer(
            make_callback_query(f"drill:answer:{index}:{chosen}", bot=fake_bot),
            state,
            session_factory,
            srs(),
        )

    assert await state.get_state() is None
    summary = fake_bot.session.sent_messages[-1].text
    assert "4/5 верно" in summary
    assert "карточками" in summary
    assert await drill_error_count(session_factory) == 1


async def test_stale_answer_ignored(fake_bot, session_factory, settings):
    llm = FakeLLM(cloze_results=[cloze()])
    state = make_state(fake_bot)
    await cmd_drill(make_message("/drill", bot=fake_bot), state, session_factory, llm, settings)
    await on_answer(
        make_callback_query("drill:answer:0:0", bot=fake_bot), state, session_factory, srs()
    )
    # Pressing item 0 again must not advance or crash.
    await on_answer(
        make_callback_query("drill:answer:0:1", bot=fake_bot), state, session_factory, srs()
    )
    assert (await state.get_data())["index"] == 1
    assert await drill_error_count(session_factory) == 0


async def test_drill_llm_failure(fake_bot, session_factory, settings):
    llm = FakeLLM(cloze_results=[LLMError("down")])
    state = make_state(fake_bot)
    await cmd_drill(make_message("/drill", bot=fake_bot), state, session_factory, llm, settings)
    assert fake_bot.session.sent_messages[-1].text == FAIL_TEXT
    assert await state.get_state() is None


async def test_drill_uses_already_active_topic(fake_bot, session_factory, settings):
    async with session_factory() as session:
        await repo.ensure_drill_topics_seeded(session)
        await repo.rotate_drill_topic(session, week=date(2026, 8, 16))
        second = await repo.rotate_drill_topic(session, week=date(2026, 8, 23))
        await session.commit()
        assert second.slug == "genre-des-noms"

    llm = FakeLLM(cloze_results=[cloze()])
    state = make_state(fake_bot)
    await cmd_drill(make_message("/drill", bot=fake_bot), state, session_factory, llm, settings)
    topic, _ = llm.cloze_calls[0]
    assert topic == "Le genre des noms"
