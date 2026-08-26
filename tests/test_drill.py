from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

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


async def test_weekly_topic_is_deterministic_and_shared(session_factory):
    """Everyone in the cohort drills the same topic in the same week, and the
    mapping cannot drift after a restart or a missed job."""
    async with session_factory() as session:
        await repo.ensure_drill_topics_seeded(session)
        await session.commit()
        monday = date(2026, 9, 7)
        same_week = await repo.get_topic_for_week(session, today=monday + timedelta(days=3))
        this_week = await repo.get_topic_for_week(session, today=monday)
        next_week = await repo.get_topic_for_week(session, today=monday + timedelta(days=7))
        ten_weeks = await repo.get_topic_for_week(session, today=monday + timedelta(weeks=10))
    assert this_week.slug == same_week.slug
    assert this_week.slug != next_week.slug
    assert this_week.slug == ten_weeks.slug  # 10 topics -> wraps after 10 weeks


# ---------------------------------------------------------------- /drill flow


async def test_drill_serves_five_items_for_active_topic(
    fake_bot, session_factory, settings, user, usage
):
    await add_vocab_card(session_factory, "marché")
    llm = FakeLLM(cloze_results=[cloze()])
    state = make_state(fake_bot)
    await cmd_drill(
        make_message("/drill", bot=fake_bot), state, user, session_factory, llm, settings, usage
    )

    # The topic is this ISO week's cohort topic.
    async with session_factory() as session:
        expected = await repo.get_topic_for_week(
            session, today=datetime.now(ZoneInfo("Europe/Paris")).date()
        )
    topic, lemmas = llm.cloze_calls[0]
    assert topic == expected.title_fr
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


async def test_correct_answer_gives_feedback_and_no_card(
    fake_bot, session_factory, settings, user, usage
):
    llm = FakeLLM(cloze_results=[cloze()])
    state = make_state(fake_bot)
    await cmd_drill(
        make_message("/drill", bot=fake_bot), state, user, session_factory, llm, settings, usage
    )

    # Item 0: correct option is "suis" at index 0.
    await on_answer(
        make_callback_query("drill:answer:0:0", bot=fake_bot), state, user, session_factory, srs()
    )
    feedback = fake_bot.session.sent("EditMessageText")[-1]
    assert "✅ Верно" in feedback.text
    assert "être" in feedback.text  # explanation shown
    assert await drill_error_count(session_factory) == 0
    next_item = fake_bot.session.sent_messages[-1]
    assert "2/5" in next_item.text


async def test_wrong_answer_creates_drill_error_card(
    fake_bot, session_factory, settings, user, usage
):
    llm = FakeLLM(cloze_results=[cloze()])
    state = make_state(fake_bot)
    await cmd_drill(
        make_message("/drill", bot=fake_bot), state, user, session_factory, llm, settings, usage
    )

    # Item 0: wrong option "ai" at index 1.
    await on_answer(
        make_callback_query("drill:answer:0:1", bot=fake_bot), state, user, session_factory, srs()
    )
    feedback = fake_bot.session.sent("EditMessageText")[-1]
    assert "❌" in feedback.text
    assert "«ai»" in feedback.text

    assert await drill_error_count(session_factory) == 1
    async with session_factory() as session:
        card = (
            (await session.execute(select(Card).where(Card.kind == "drill_error"))).scalars().one()
        )
    async with session_factory() as session:
        expected = await repo.get_topic_for_week(
            session, today=datetime.now(ZoneInfo("Europe/Paris")).date()
        )
    assert card.error_meta["type"] == expected.slug
    assert card.error_meta["corrected"] == "suis"
    assert card.error_meta["original"] == "ai"
    assert "suis" in card.text  # sentence stored with the gap filled


async def test_full_drill_run_summary_and_counts(fake_bot, session_factory, settings, user, usage):
    llm = FakeLLM(cloze_results=[cloze()])
    state = make_state(fake_bot)
    await cmd_drill(
        make_message("/drill", bot=fake_bot), state, user, session_factory, llm, settings, usage
    )

    items = (await state.get_data())["items"]
    # Answer all: first wrong, rest correct.
    for index, item in enumerate(items):
        correct_idx = item["options"].index(item["correct"])
        chosen = correct_idx if index > 0 else (correct_idx + 1) % 3
        await on_answer(
            make_callback_query(f"drill:answer:{index}:{chosen}", bot=fake_bot),
            state,
            user,
            session_factory,
            srs(),
        )

    assert await state.get_state() is None
    summary = fake_bot.session.sent_messages[-1].text
    assert "4/5 верно" in summary
    assert "карточками" in summary
    assert await drill_error_count(session_factory) == 1


async def test_stale_answer_ignored(fake_bot, session_factory, settings, user, usage):
    llm = FakeLLM(cloze_results=[cloze()])
    state = make_state(fake_bot)
    await cmd_drill(
        make_message("/drill", bot=fake_bot), state, user, session_factory, llm, settings, usage
    )
    await on_answer(
        make_callback_query("drill:answer:0:0", bot=fake_bot), state, user, session_factory, srs()
    )
    # Pressing item 0 again must not advance or crash.
    await on_answer(
        make_callback_query("drill:answer:0:1", bot=fake_bot), state, user, session_factory, srs()
    )
    assert (await state.get_data())["index"] == 1
    assert await drill_error_count(session_factory) == 0


async def test_drill_llm_failure(fake_bot, session_factory, settings, user, usage):
    llm = FakeLLM(cloze_results=[LLMError("down")])
    state = make_state(fake_bot)
    await cmd_drill(
        make_message("/drill", bot=fake_bot), state, user, session_factory, llm, settings, usage
    )
    assert fake_bot.session.sent_messages[-1].text == FAIL_TEXT
    assert await state.get_state() is None


async def test_drill_errors_are_scoped_to_the_user(
    fake_bot, session_factory, settings, user, usage
):
    """A wrong answer creates a card for the answering user only."""
    from frbot.db.models import User

    other = User(id=999_002, chat_id=999_002)
    async with session_factory() as session:
        session.add(other)
        await session.commit()

    llm = FakeLLM(cloze_results=[cloze()])
    state = make_state(fake_bot)
    await cmd_drill(
        make_message("/drill", bot=fake_bot), state, user, session_factory, llm, settings, usage
    )
    await on_answer(
        make_callback_query("drill:answer:0:1", bot=fake_bot), state, user, session_factory, srs()
    )

    async with session_factory() as session:
        mine = await repo.count_cards(session, user_id=user.id)
        theirs = await repo.count_cards(session, user_id=other.id)
    assert mine == 1
    assert theirs == 0
