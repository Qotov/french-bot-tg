"""Exam tracks: definitions, and how they change writing, correction and drills."""

import pytest

from frbot import tracks
from frbot.bot.handlers.track import cmd_track, on_track_chosen
from frbot.db import repo
from frbot.db.models import User
from tests.fakes import ALLOWED_USER_ID, make_callback_query, make_message

# ---------------------------------------------------------------- definitions


def test_every_exam_track_is_usable():
    for track in tracks.TRACKS.values():
        assert track.title and track.blurb and track.criteria_ru
        low, high = track.word_target
        assert 0 < low <= high
        if tracks.is_exam(track.slug):
            assert len(track.tasks) >= 5, track.slug
            assert track.drill_priority, track.slug


def test_exam_word_targets_match_the_real_exams():
    assert tracks.get("delf_b1").word_target == (160, 180)
    assert tracks.get("delf_b2").word_target == (240, 260)
    # TCF spans three task types; the target covers the longer ones.
    assert tracks.get("tcf").word_target[1] >= 150


def test_general_is_the_default_and_is_not_an_exam():
    assert tracks.get(None).slug == "general"
    assert tracks.get("nonsense").slug == "general"
    assert tracks.is_exam(None) is False
    assert tracks.is_exam("delf_b2") is True


def test_drill_priority_puts_exam_grammar_first_without_losing_topics():
    all_slugs = [slug for slug, _ in repo.SEED_TOPICS]
    ordered = tracks.ordered_topic_slugs("delf_b2", all_slugs)
    assert sorted(ordered) == sorted(all_slugs)  # nothing dropped or duplicated
    assert ordered[0] == "subjonctif-present"


def test_general_track_leaves_the_topic_order_alone():
    all_slugs = [slug for slug, _ in repo.SEED_TOPICS]
    assert tracks.ordered_topic_slugs("general", all_slugs) == all_slugs


# ------------------------------------------------------------------- choosing


async def test_choosing_an_exam_track_persists_and_raises_the_level(
    fake_bot, session_factory, user
):
    async with session_factory() as session:
        (await repo.get_user(session, user.id)).level = "A2"
        await session.commit()

    await on_track_chosen(make_callback_query("track:delf_b2", bot=fake_bot), user, session_factory)
    async with session_factory() as session:
        row = await repo.get_user(session, user.id)
    assert row.track == "delf_b2"
    # Someone preparing DELF B2 must not be fed A2 example sentences.
    assert row.level == "B2"


async def test_choosing_the_general_track_does_not_touch_the_level(fake_bot, session_factory, user):
    await on_track_chosen(make_callback_query("track:general", bot=fake_bot), user, session_factory)
    async with session_factory() as session:
        row = await repo.get_user(session, user.id)
    assert row.track == "general"
    assert row.level == "B1"


async def test_unknown_track_is_refused(fake_bot, session_factory, user):
    await on_track_chosen(
        make_callback_query("track:phd_sorbonne", bot=fake_bot), user, session_factory
    )
    async with session_factory() as session:
        assert (await repo.get_user(session, user.id)).track is None


async def test_track_command_shows_the_current_choice(fake_bot, user):
    await cmd_track(make_message("/track", bot=fake_bot), user)
    sent = fake_bot.session.sent_messages[-1]
    assert "Обычные занятия" in sent.text
    labels = [b.text for row in sent.reply_markup.inline_keyboard for b in row]
    assert "DELF B2" in labels


# ------------------------------------------------------- effect on the writing


async def test_exam_track_sets_an_exam_task_and_word_count(
    fake_bot, session_factory, settings, user
):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from frbot.bot.handlers.write import cmd_write

    async with session_factory() as session:
        row = await repo.get_user(session, user.id)
        row.track = "delf_b2"
        await session.commit()
        user = await repo.get_user(session, user.id)

    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=fake_bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID),
    )
    await cmd_write(make_message("/write", bot=fake_bot), state, user, session_factory, settings)
    text = fake_bot.session.sent_messages[-1].text
    assert "DELF B2" in text
    assert "240–260 слов" in text
    assert "2–3 предложения" not in text  # not the casual format


async def test_general_track_keeps_the_short_daily_format(
    fake_bot, session_factory, settings, user
):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from frbot.bot.handlers.write import cmd_write

    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=fake_bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID),
    )
    await cmd_write(make_message("/write", bot=fake_bot), state, user, session_factory, settings)
    assert "2–3 предложения" in fake_bot.session.sent_messages[-1].text


@pytest.mark.parametrize(("track", "expected"), [(None, 1500), ("delf_b2", 4000)])
def test_answer_length_cap_follows_the_task(track, expected):
    from frbot.bot.handlers.write import exam_answer_limit

    assert exam_answer_limit(User(id=1, track=track)) == expected


async def test_correction_is_weighted_by_the_exam_criteria(
    fake_bot, session_factory, settings, user, usage, alerter
):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from frbot.bot.handlers.write import cmd_write, handle_answer
    from frbot.llm.schemas import WritingCorrection
    from frbot.srs.scheduler import SrsScheduler
    from tests.fakes import FakeLLM

    async with session_factory() as session:
        (await repo.get_user(session, user.id)).track = "delf_b1"
        await session.commit()
        user = await repo.get_user(session, user.id)

    llm = FakeLLM(
        correct_results=[WritingCorrection(corrected_text="Bien.", errors=[], comment_ru="ок")]
    )
    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=fake_bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID),
    )
    await cmd_write(make_message("/write", bot=fake_bot), state, user, session_factory, settings)
    await handle_answer(
        make_message("Mon texte en français.", bot=fake_bot),
        state,
        user,
        session_factory,
        llm,
        SrsScheduler(0.9),
        settings,
        usage,
        alerter,
    )
    assert llm.correct_criteria[-1] is not None
    assert "DELF B1" in llm.correct_criteria[-1]


# --------------------------------------------------------- effect on the drill


async def test_weekly_topic_follows_the_track_priority(session_factory):
    from datetime import date

    async with session_factory() as session:
        await repo.ensure_drill_topics_seeded(session)
        await session.commit()
        week = date(2026, 3, 2)
        general = await repo.get_topic_for_week(session, today=week)
        exam = await repo.get_topic_for_week(session, today=week, track="delf_b2")
    # Both are valid topics, but the exam track reorders the rotation.
    assert general is not None and exam is not None
    assert exam.slug in tracks.get("delf_b2").drill_priority


async def test_every_track_still_covers_all_topics_over_time(session_factory):
    from datetime import date, timedelta

    async with session_factory() as session:
        await repo.ensure_drill_topics_seeded(session)
        await session.commit()
        seen = set()
        start = date(2026, 1, 5)
        for week in range(10):
            topic = await repo.get_topic_for_week(
                session, today=start + timedelta(weeks=week), track="tcf"
            )
            seen.add(topic.slug)
    assert len(seen) == len(repo.SEED_TOPICS)  # nothing is unreachable
