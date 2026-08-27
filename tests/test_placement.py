"""The placement test: the bank's integrity, the scoring rules, and the flow."""

from collections import Counter

import pytest

from frbot import placement
from frbot.bot.handlers.placement import (
    PlacementStates,
    cmd_placement,
    on_answer,
)
from frbot.db import repo
from frbot.srs.scheduler import SrsScheduler
from tests.fakes import (
    ALLOWED_USER_ID,
    FakeLLM,
    enrichment_dict,
    make_callback_query,
    make_message,
)


def state_for(bot):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID),
    )


def starter_llm() -> FakeLLM:
    from frbot.llm.schemas import Enrichment, TopicWordList

    words = ["la maison", "le métro"]
    return FakeLLM(
        topic_results=[
            TopicWordList.model_validate(
                {"words": [{"lemma": w, "translation_ru": "…"} for w in words]}
            )
        ],
        enrich_results=[Enrichment.model_validate(enrichment_dict(w)) for w in words],
    )


# ------------------------------------------------------------------- the bank


def test_every_band_is_equally_weighted():
    counts = Counter(item.level for item in placement.BANK)
    assert set(counts) == set(placement.LEVELS_ORDER)
    assert all(n == placement.PER_BAND for n in counts.values())


def test_items_are_well_formed():
    for item in placement.BANK:
        assert "___" in item.sentence, item.sentence
        assert len(item.options) == 3
        assert len(set(item.options)) == 3, item.sentence
        assert item.correct in item.options, item.sentence
        assert item.explanation_ru
        assert item.skill


def test_items_are_presented_easiest_band_first():
    levels = [item.level for item in placement.items_in_order()]
    assert levels == sorted(levels, key=placement.LEVELS_ORDER.index)


# ---------------------------------------------------------------- the scoring


@pytest.mark.parametrize(
    ("correct_bands", "expected"),
    [
        (set(), "A2"),
        ({"A2"}, "A2"),
        ({"A2", "B1"}, "B1"),
        ({"A2", "B1", "B2"}, "B2"),
        ({"B2"}, "A2"),  # cannot skip a band you do not control
        ({"A2", "B2"}, "A2"),  # a B1 gap stops the ladder
    ],
)
def test_level_is_the_highest_band_actually_controlled(correct_bands, expected):
    answers = [(item.level, item.level in correct_bands) for item in placement.items_in_order()]
    assert placement.level_from_answers(answers) == expected


def test_a_band_scraped_through_does_not_count():
    """3/6 is not control of a level."""
    answers = []
    seen: Counter = Counter()
    for item in placement.items_in_order():
        seen[item.level] += 1
        ok = item.level == "A2" or (item.level == "B1" and seen["B1"] <= 3)
        answers.append((item.level, ok))
    assert placement.level_from_answers(answers) == "A2"


def test_weakest_skills_are_reported_without_duplicates():
    detail = [
        ("A2", "genre", False),
        ("A2", "genre", False),
        ("B1", "relatif", False),
        ("B1", "temps", True),
    ]
    assert placement.weakest_skills(detail) == ["genre", "relatif"]


# ------------------------------------------------------------------- the flow


async def test_full_run_sets_the_level_and_builds_a_deck(
    fake_bot, session_factory, settings, user, usage, alerter
):
    state = state_for(fake_bot)
    await cmd_placement(make_message("/placement", bot=fake_bot), state)
    assert await state.get_state() == PlacementStates.running.state

    items = placement.items_in_order()
    llm = starter_llm()
    for index, item in enumerate(items):
        await on_answer(
            make_callback_query(f"place:{index}:{item.options.index(item.correct)}", bot=fake_bot),
            state,
            user,
            session_factory,
            llm,
            SrsScheduler(0.9),
            settings,
            usage,
            alerter,
        )

    async with session_factory() as session:
        row = await repo.get_user(session, user.id)
        cards = await repo.count_cards(session, user_id=user.id)
    assert row.level == "B2"  # every answer correct
    assert cards == 2  # the honest answer still gets a starter deck
    assert await state.get_state() is None

    texts = [m.text or "" for m in fake_bot.session.sent_messages]
    assert any("Твой уровень: B2" in t for t in texts)


async def test_wrong_answers_place_the_learner_at_a2(
    fake_bot, session_factory, settings, user, usage, alerter
):
    state = state_for(fake_bot)
    await cmd_placement(make_message("/placement", bot=fake_bot), state)
    items = placement.items_in_order()
    llm = starter_llm()
    for index, item in enumerate(items):
        wrong = next(i for i, o in enumerate(item.options) if o != item.correct)
        await on_answer(
            make_callback_query(f"place:{index}:{wrong}", bot=fake_bot),
            state,
            user,
            session_factory,
            llm,
            SrsScheduler(0.9),
            settings,
            usage,
            alerter,
        )
    async with session_factory() as session:
        assert (await repo.get_user(session, user.id)).level == "A2"
    texts = [m.text or "" for m in fake_bot.session.sent_messages]
    assert any("поработать" in t for t in texts)  # weak skills reported


async def test_the_test_gives_no_answer_away_mid_run(
    fake_bot, session_factory, settings, user, usage, alerter
):
    """Per-question feedback would let the learner recalibrate and would turn a
    measurement into a lesson."""
    state = state_for(fake_bot)
    await cmd_placement(make_message("/placement", bot=fake_bot), state)
    item = placement.items_in_order()[0]
    wrong = next(i for i, o in enumerate(item.options) if o != item.correct)
    await on_answer(
        make_callback_query(f"place:0:{wrong}", bot=fake_bot),
        state,
        user,
        session_factory,
        starter_llm(),
        SrsScheduler(0.9),
        settings,
        usage,
        alerter,
    )
    shown = " ".join(m.text or "" for m in fake_bot.session.sent("EditMessageText"))
    assert "Верно" not in shown
    assert item.explanation_ru not in shown


async def test_stale_question_taps_are_ignored(
    fake_bot, session_factory, settings, user, usage, alerter
):
    state = state_for(fake_bot)
    await cmd_placement(make_message("/placement", bot=fake_bot), state)
    llm = starter_llm()
    for _ in range(2):  # answer question 0 twice
        await on_answer(
            make_callback_query("place:0:0", bot=fake_bot),
            state,
            user,
            session_factory,
            llm,
            SrsScheduler(0.9),
            settings,
            usage,
            alerter,
        )
    assert len((await state.get_data())["answers"]) == 1


async def test_answering_without_a_running_test_is_refused(
    fake_bot, session_factory, settings, user, usage, alerter
):
    state = state_for(fake_bot)
    await on_answer(
        make_callback_query("place:0:0", bot=fake_bot),
        state,
        user,
        session_factory,
        starter_llm(),
        SrsScheduler(0.9),
        settings,
        usage,
        alerter,
    )
    answers = fake_bot.session.sent("AnswerCallbackQuery")
    assert any("не активен" in (a.text or "") for a in answers)
