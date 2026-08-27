"""Regression tests for the Phase C review findings."""

from datetime import UTC, datetime, timedelta

import pytest

from frbot import tracks
from frbot.bot import render
from frbot.db import repo
from frbot.db.models import User
from tests.fakes import ALLOWED_USER_ID, FakeLLM, make_callback_query, make_message


def state_for(bot):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID),
    )


# ------------------------------------------------- TTS must not bill blindly


async def test_no_paid_synthesis_when_the_host_cannot_encode_audio(
    monkeypatch, fake_bot, session_factory, settings, user, usage
):
    """The capability check is free and local; the synthesis is billed. Doing
    them in the wrong order charged for audio that could never be delivered."""
    from frbot.bot import pronounce
    from tests.fakes import add_vocab_card

    monkeypatch.setattr(pronounce, "audio_supported", lambda: False)
    card_id = await add_vocab_card(session_factory, "boulangerie")
    llm = FakeLLM()

    from frbot.bot.audio import VoiceCache

    await pronounce.on_speak(
        make_callback_query(f"say:word:{card_id}", bot=fake_bot),
        user,
        session_factory,
        llm,
        settings,
        usage,
        VoiceCache(tmp_cache_dir()),
    )
    assert usage.used_today(user.id) == 0  # no quota burned
    answers = fake_bot.session.sent("AnswerCallbackQuery")
    assert any("недоступна" in (a.text or "") for a in answers)


def tmp_cache_dir():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp())


def test_audio_buttons_disappear_when_tts_is_off(settings):
    from frbot.bot.keyboards import card_preview_kb

    labels_on = [b.text for r in card_preview_kb(1, with_audio=True).inline_keyboard for b in r]
    labels_off = [b.text for r in card_preview_kb(1, with_audio=False).inline_keyboard for b in r]
    assert any("🔊" in x for x in labels_on)
    assert not any("🔊" in x for x in labels_off)


def test_voice_caption_is_escaped():
    assert render.esc("l'été & <b>") == "l'été &amp; &lt;b&gt;"


# --------------------------------------- the output budget follows the input


def test_correction_budget_grows_with_the_essay():
    from frbot.llm.client import MAX_TOKENS, MAX_TOKENS_CEILING, correction_token_budget

    assert correction_token_budget("court") == MAX_TOKENS
    big = correction_token_budget("x" * 4000)
    assert big > MAX_TOKENS  # a DELF essay cannot be corrected in the default budget
    assert correction_token_budget("x" * 100_000) <= MAX_TOKENS_CEILING


def test_truncated_output_is_reported_as_a_budget_error_not_bad_json():
    """Retrying a truncation with the same ceiling truncates identically, so it
    must not masquerade as malformed JSON."""
    from types import SimpleNamespace

    from frbot.llm.client import _hit_token_ceiling

    hit = SimpleNamespace(candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")])
    ok = SimpleNamespace(candidates=[SimpleNamespace(finish_reason="STOP")])
    assert _hit_token_ceiling(hit) is True
    assert _hit_token_ceiling(ok) is False


# ------------------------------------- error cards stay a single-point drill


def test_error_cards_keep_only_the_sentence_carrying_the_error():
    essay = (
        "Je pense que la ville doit changer. " * 12
        + "Hier je suis allé au marché. "
        + "Ensuite nous avons discuté longuement. " * 12
    )
    picked = render.sentence_around(essay, "suis allé")
    assert "suis allé" in picked
    assert len(picked) <= render.ERROR_SENTENCE_MAX


def test_review_of_an_essay_error_card_fits_in_a_telegram_message():
    from frbot.db.models import Card, CardKind

    long_text = "Une phrase assez longue pour poser problème. " * 60
    sentence = render.sentence_around(long_text, "problème")
    card = Card(
        text=sentence,
        lemma="x",
        kind=CardKind.error.value,
        error_meta={
            "type": "vocab",
            "original": "problème" * 40,
            "corrected": "souci",
            "explanation_ru": "объяснение " * 60,
            "front": render.make_gapped(sentence, "problème"),
        },
    )
    shown = f"{render.card_front(card)}\n—\n{render.card_back(card)}"
    assert len(shown) < 4096


# ------------------------------------------------- tracks and word counts


def test_task_word_ranges_match_the_task_text():
    for track in tracks.TRACKS.values():
        for text, low, high in track.tasks:
            assert low < high
            # The range is stated once, by the bot, from the task's own numbers.
            assert "mots" not in text


def test_tcf_tasks_carry_three_different_official_lengths():
    ranges = {(low, high) for _t, low, high in tracks.get("tcf").tasks}
    assert ranges == {(60, 120), (120, 150), (120, 180)}


def test_task_word_range_falls_back_to_the_track():
    track = tracks.get("general")
    assert tracks.task_word_range(track, None) == track.word_target
    assert tracks.task_word_range(track, ("t", 10, 20)) == (10, 20)


async def test_exam_prompt_states_the_task_s_own_length(fake_bot, session_factory, settings, user):
    from frbot.bot.handlers.write import cmd_write

    async with session_factory() as session:
        (await repo.get_user(session, user.id)).track = "tcf"
        await session.commit()
        user = await repo.get_user(session, user.id)

    seen = set()
    for _ in range(12):
        fake_bot.session.requests.clear()
        await cmd_write(
            make_message("/write", bot=fake_bot),
            state_for(fake_bot),
            user,
            session_factory,
            settings,
        )
        text = fake_bot.session.sent_messages[-1].text
        band = next(b for b in ("60–120", "120–150", "120–180") if b in text)
        # Whatever the task, the stated length is that task's own.
        seen.add(band)
    assert len(seen) >= 2


# ------------------------------------- the weekly topic must match the drill


async def test_weekly_summary_announces_each_learner_s_own_topic(
    fake_bot, session_factory, settings
):
    from frbot.jobs.reminders import send_weekly_summary

    async with session_factory() as session:
        session.add(User(id=111, chat_id=111, track="general"))
        session.add(User(id=222, chat_id=222, track="delf_b2"))
        await repo.ensure_drill_topics_seeded(session)
        await session.commit()

    await send_weekly_summary(fake_bot, session_factory, settings)
    announced = {
        m.chat_id: m.text
        for m in fake_bot.session.sent_messages
        if "Тема следующей недели" in (m.text or "")
    }
    assert set(announced) == {111, 222}

    # The exam learner's announcement matches what /drill will actually serve.
    now = datetime.now(UTC).astimezone().date() + timedelta(days=1)
    async with session_factory() as session:
        exam_topic = await repo.get_topic_for_week(session, today=now, track="delf_b2")
    assert exam_topic.title_fr in announced[222]


# ------------------------------------------ placement must not dead-end


async def test_abandoning_the_test_still_completes_onboarding(
    fake_bot, session_factory, settings, user, usage, alerter
):
    from frbot.bot.handlers.placement import cmd_placement, cmd_stop_placement
    from frbot.llm.schemas import Enrichment, TopicWordList
    from frbot.srs.scheduler import SrsScheduler
    from tests.fakes import enrichment_dict

    llm = FakeLLM(
        topic_results=[
            TopicWordList.model_validate({"words": [{"lemma": "la maison", "translation_ru": "…"}]})
        ],
        enrich_results=[Enrichment.model_validate(enrichment_dict("la maison"))],
    )
    state = state_for(fake_bot)
    await cmd_placement(make_message("/placement", bot=fake_bot), state, onboarding=True)
    await cmd_stop_placement(
        make_message("/stop", bot=fake_bot),
        state,
        user,
        session_factory,
        llm,
        SrsScheduler(0.9),
        settings,
        usage,
        alerter,
    )

    texts = [m.text or "" for m in fake_bot.session.sent_messages]
    assert any("delete_me" in t for t in texts)  # data notice delivered
    async with session_factory() as session:
        assert await repo.count_cards(session, user_id=user.id) == 1  # deck built
        assert (await repo.get_user(session, user.id)).level  # a level remains
    assert await state.get_state() is None


async def test_typing_during_the_test_does_not_create_a_card(fake_bot):
    from frbot.bot.handlers.placement import handle_typed_answer

    await handle_typed_answer(make_message("suis", bot=fake_bot))
    assert "кнопкой" in fake_bot.session.sent_messages[-1].text


# ------------------------------------------------------ the cache is pruned


async def test_nightly_cleanup_prunes_the_voice_cache(
    tmp_path, fake_bot, session_factory, settings
):
    from types import SimpleNamespace

    from aiogram.fsm.storage.memory import MemoryStorage

    from frbot.bot.audio import VoiceCache
    from frbot.jobs.reminders import cleanup_stray_fsm_entries

    db = tmp_path / "frbot.db"
    tuned = settings.model_copy(
        update={"db_url": f"sqlite+aiosqlite:///{db}", "tts_cache_max_files": 2}
    )
    cache = VoiceCache(tmp_path / "tts", max_files=2)
    for i in range(5):
        await cache.put(f"mot-{i}", "Kore", b"OggS-fake")

    await cleanup_stray_fsm_entries(
        SimpleNamespace(storage=MemoryStorage()), session_factory, tuned
    )
    assert len(list((tmp_path / "tts").glob("*.ogg"))) <= 2


@pytest.mark.parametrize("text", ["", "   "])
def test_sentence_around_handles_empty_input(text):
    assert render.sentence_around(text, "x") == ""
