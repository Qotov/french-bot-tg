"""Voice capture (outside sessions) and voice answers to /write."""

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, select

from frbot.bot.handlers.capture import (
    VOICE_NO_WORDS_TEXT,
    handle_voice_capture,
)
from frbot.bot.handlers.write import WriteStates, cmd_write, handle_voice_answer
from frbot.bot.telegram_utils import VOICE_MAX_DURATION
from frbot.db.models import Card
from frbot.llm.client import LLMError
from frbot.llm.schemas import Enrichment, Transcript, VoiceWords, WritingCorrection
from frbot.srs.scheduler import SrsScheduler
from tests.fakes import (
    ALLOWED_USER_ID,
    FakeLLM,
    enrichment_dict,
    load_fixture_json,
    make_voice_message,
)


def make_state(bot) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID),
    )


def srs() -> SrsScheduler:
    return SrsScheduler(desired_retention=0.9)


def enrichment_for(lemma: str) -> Enrichment:
    return Enrichment.model_validate(enrichment_dict(lemma))


async def card_count(session_factory, kind: str = "vocab") -> int:
    async with session_factory() as session:
        stmt = select(func.count(Card.id)).where(Card.kind == kind)
        return (await session.execute(stmt)).scalar_one()


# ------------------------------------------------------------- voice capture


async def test_voice_capture_creates_cards(fake_bot, session_factory, settings, user, usage):
    llm = FakeLLM(
        voice_words_results=[VoiceWords(words=["boulangerie", "au fur et à mesure"])],
        enrich_results=[enrichment_for("boulangerie"), enrichment_for("au fur et à mesure")],
    )
    await handle_voice_capture(
        make_voice_message(bot=fake_bot), user, session_factory, llm, srs(), settings, usage
    )
    assert llm.voice_words_calls == ["audio/ogg"]
    assert await card_count(session_factory) == 2
    previews = [m.text for m in fake_bot.session.sent_messages]
    assert any("boulangerie" in t for t in previews)
    assert any("au fur et à mesure" in t for t in previews)


async def test_voice_capture_no_words(fake_bot, session_factory, settings, user, usage):
    llm = FakeLLM(voice_words_results=[VoiceWords(words=[])])
    await handle_voice_capture(
        make_voice_message(bot=fake_bot), user, session_factory, llm, srs(), settings, usage
    )
    assert fake_bot.session.sent_messages[-1].text == VOICE_NO_WORDS_TEXT
    assert await card_count(session_factory) == 0


async def test_voice_capture_too_long(fake_bot, session_factory, settings, user, usage):
    llm = FakeLLM()
    await handle_voice_capture(
        make_voice_message(duration=VOICE_MAX_DURATION + 1, bot=fake_bot),
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
    )
    assert llm.voice_words_calls == []
    assert "короче" in fake_bot.session.sent_messages[-1].text


async def test_voice_capture_llm_failure(fake_bot, session_factory, settings, user, usage):
    llm = FakeLLM(voice_words_results=[LLMError("down")])
    await handle_voice_capture(
        make_voice_message(bot=fake_bot), user, session_factory, llm, srs(), settings, usage
    )
    assert "голосовое" in fake_bot.session.sent_messages[-1].text
    assert await card_count(session_factory) == 0


async def test_voice_capture_dedupes_existing(fake_bot, session_factory, settings, user, usage):
    from tests.fakes import add_vocab_card

    await add_vocab_card(session_factory, "boulangerie")
    llm = FakeLLM(voice_words_results=[VoiceWords(words=["boulangerie"])])
    await handle_voice_capture(
        make_voice_message(bot=fake_bot), user, session_factory, llm, srs(), settings, usage
    )
    assert llm.enrich_calls == []  # lemma pre-check hit
    assert await card_count(session_factory) == 1
    assert "уже есть" in fake_bot.session.sent_messages[-1].text


# -------------------------------------------------------------- voice /write


async def test_voice_answer_transcribed_and_corrected(
    fake_bot, session_factory, settings, user, usage
):
    correction = WritingCorrection.model_validate(load_fixture_json("correction_valid.json"))
    llm = FakeLLM(
        transcribe_results=[Transcript(transcript="je suis allé au marché depuis hier")],
        correct_results=[correction],
    )
    state = make_state(fake_bot)
    from tests.fakes import make_message

    await cmd_write(make_message("/write", bot=fake_bot), state, user, session_factory, settings)
    await handle_voice_answer(
        make_voice_message(bot=fake_bot), state, user, session_factory, llm, srs(), settings, usage
    )

    _, answer = llm.correct_calls[0]
    assert answer == "je suis allé au marché depuis hier"
    texts = [m.text for m in fake_bot.session.sent_messages]
    assert any("🎙" in t for t in texts)  # transcript echoed
    assert any("Исправлено" in t for t in texts)
    assert await card_count(session_factory, kind="error") == 2
    assert await state.get_state() is None


async def test_voice_answer_empty_transcript_keeps_state(
    fake_bot, session_factory, settings, user, usage
):
    llm = FakeLLM(transcribe_results=[Transcript(transcript="  ")])
    state = make_state(fake_bot)
    from tests.fakes import make_message

    await cmd_write(make_message("/write", bot=fake_bot), state, user, session_factory, settings)
    await handle_voice_answer(
        make_voice_message(bot=fake_bot), state, user, session_factory, llm, srs(), settings, usage
    )
    assert "Не расслышал" in fake_bot.session.sent_messages[-1].text
    assert await state.get_state() == WriteStates.awaiting_answer.state


async def test_voice_capture_skips_overlong_extracted_phrase(
    fake_bot, session_factory, settings, user, usage
):
    from frbot.bot.handlers.capture import CAPTURE_MAX_LEN

    long_phrase = "mot " * (CAPTURE_MAX_LEN // 3)
    llm = FakeLLM(
        voice_words_results=[VoiceWords(words=[long_phrase, "boulangerie"])],
        enrich_results=[enrichment_for("boulangerie")],
    )
    await handle_voice_capture(
        make_voice_message(bot=fake_bot), user, session_factory, llm, srs(), settings, usage
    )
    assert llm.enrich_calls == ["boulangerie"]  # the utterance-sized item skipped
    assert await card_count(session_factory) == 1
    assert any("слишком длинно" in (m.text or "").lower() for m in fake_bot.session.sent_messages)


async def test_voice_capture_stops_after_first_llm_failure(
    fake_bot, session_factory, settings, user, usage
):
    llm = FakeLLM(
        voice_words_results=[VoiceWords(words=["premier", "deuxième", "troisième"])],
        enrich_results=[LLMError("down"), enrichment_for("deuxième"), enrichment_for("troisième")],
    )
    await handle_voice_capture(
        make_voice_message(bot=fake_bot), user, session_factory, llm, srs(), settings, usage
    )
    assert llm.enrich_calls == ["premier"]  # no retries for the rest
    assert await card_count(session_factory) == 0
    assert any("не добавлены" in (m.text or "") for m in fake_bot.session.sent_messages)


async def test_voice_while_awaiting_setting_asks_for_text(fake_bot):
    from frbot.bot.handlers.settings import on_voice_while_awaiting

    await on_voice_while_awaiting(make_voice_message(bot=fake_bot))
    assert "текстом" in fake_bot.session.sent_messages[-1].text
