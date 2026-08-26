"""End-to-end routing tests: real Dispatcher built by build_dispatcher, updates
fed through feed_update, LLM faked. Verifies router order, filters, whitelist,
and that dependency injection provides every handler argument.
"""

from datetime import UTC, datetime, timedelta

import pytest
from aiogram.types import Update
from sqlalchemy import func, select

from frbot.__main__ import build_dispatcher
from frbot.db.models import Card
from frbot.llm.schemas import ClozeSet, Enrichment, WritingCorrection
from frbot.srs.scheduler import SrsScheduler
from tests.fakes import (
    OTHER_USER_ID,
    FakeLLM,
    add_vocab_card,
    load_fixture_json,
    make_callback_query,
    make_message,
)


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM(
        enrich_results=[Enrichment.model_validate(load_fixture_json("enrichment_valid.json"))],
        correct_results=[
            WritingCorrection.model_validate(load_fixture_json("correction_valid.json"))
        ],
        cloze_results=[ClozeSet.model_validate(load_fixture_json("cloze_valid.json"))],
    )


@pytest.fixture
def dp(settings, session_factory, llm):
    return build_dispatcher(
        settings, session_factory, llm=llm, srs=SrsScheduler(settings.desired_retention)
    )


def message_update(text: str, update_id: int = 1, user_id: int | None = None) -> Update:
    kwargs = {"user_id": user_id} if user_id else {}
    return Update(update_id=update_id, message=make_message(text, **kwargs))


def callback_update(data: str, update_id: int = 100) -> Update:
    return Update(update_id=update_id, callback_query=make_callback_query(data))


async def test_capture_routes_to_capture_handler(dp, fake_bot, session_factory, llm):
    await dp.feed_update(fake_bot, message_update("au fur et à mesure"))
    assert llm.enrich_calls == ["au fur et à mesure"]
    assert "au fur et à mesure" in fake_bot.session.sent_messages[-1].text


async def test_commands_are_not_captured(dp, fake_bot, llm):
    for i, cmd in enumerate(["/start", "/help", "/stats", "/settings"]):
        await dp.feed_update(fake_bot, message_update(cmd, update_id=i + 1))
    assert llm.enrich_calls == []
    assert len(fake_bot.session.sent_messages) == 4


async def test_unknown_command_is_ignored(dp, fake_bot, llm):
    await dp.feed_update(fake_bot, message_update("/unknown"))
    assert fake_bot.session.sent_messages == []
    assert llm.enrich_calls == []


async def test_review_full_cycle_via_dispatcher(dp, fake_bot, session_factory):
    card_id = await add_vocab_card(
        session_factory,
        "marché",
        reviewed_days_ago=2,
        due=datetime.now(UTC) - timedelta(hours=1),
    )
    await dp.feed_update(fake_bot, message_update("/review"))
    assert any("1/1" in (m.text or "") for m in fake_bot.session.sent_messages)

    await dp.feed_update(fake_bot, callback_update(f"review:show:{card_id}", update_id=2))
    shown = fake_bot.session.sent("EditMessageText")[-1]
    assert "по мере" in shown.text

    await dp.feed_update(fake_bot, callback_update(f"review:grade:{card_id}:3", update_id=3))
    assert any("Готово" in (m.text or "") for m in fake_bot.session.sent_messages)


async def test_write_flow_via_dispatcher(dp, fake_bot, session_factory, llm):
    await dp.feed_update(fake_bot, message_update("/write"))
    assert any("Задание" in (m.text or "") for m in fake_bot.session.sent_messages)

    await dp.feed_update(
        fake_bot,
        message_update("je suis allé au marché depuis hier", update_id=2),
    )
    # The answer went to the correction handler, not to capture.
    assert llm.correct_calls
    assert llm.enrich_calls == []
    assert any("Исправлено" in (m.text or "") for m in fake_bot.session.sent_messages)
    async with session_factory() as session:
        error_count = (
            await session.execute(select(func.count(Card.id)).where(Card.kind == "error"))
        ).scalar_one()
    assert error_count == 2


async def test_drill_flow_via_dispatcher(dp, fake_bot, session_factory, llm):
    await dp.feed_update(fake_bot, message_update("/drill"))
    assert llm.cloze_calls
    assert any("Тема недели" in (m.text or "") for m in fake_bot.session.sent_messages)

    # Wrong answer on item 0 -> drill_error card via DI-injected srs.
    await dp.feed_update(fake_bot, callback_update("drill:answer:0:1", update_id=2))
    async with session_factory() as session:
        count = (
            await session.execute(select(func.count(Card.id)).where(Card.kind == "drill_error"))
        ).scalar_one()
    assert count == 1


async def test_settings_edit_flow_via_dispatcher(dp, fake_bot, session_factory):
    await dp.feed_update(fake_bot, message_update("/settings"))
    await dp.feed_update(fake_bot, callback_update("settings:edit:SESSION_MAX", update_id=2))
    await dp.feed_update(fake_bot, message_update("12", update_id=3))
    from frbot.db import repo

    async with session_factory() as session:
        assert await repo.get_setting(session, "SESSION_MAX") == "12"
    assert any("SESSION_MAX = 12" in (m.text or "") for m in fake_bot.session.sent_messages)


async def test_whitelist_blocks_everything_via_dispatcher(dp, fake_bot, llm):
    for i, text in enumerate(["/start", "/review", "bonjour"]):
        await dp.feed_update(fake_bot, message_update(text, update_id=i + 1, user_id=OTHER_USER_ID))
    assert fake_bot.session.sent_messages == []
    assert llm.enrich_calls == []


async def test_topic_flow_via_dispatcher(dp, fake_bot, session_factory, llm):
    from frbot.llm.schemas import TopicWordList

    llm.topic_results.append(
        TopicWordList.model_validate(
            {"words": [{"lemma": "commander", "translation_ru": "заказывать"}]}
        )
    )
    await dp.feed_update(fake_bot, message_update("/topic ресторан 5"))
    assert llm.topic_calls
    assert any("ресторан" in (m.text or "") for m in fake_bot.session.sent_messages)

    # The save callback must come from the message the pack is bound to.
    from aiogram.fsm.storage.base import StorageKey

    from tests.fakes import ALLOWED_USER_ID, make_message

    pack_data = await dp.storage.get_data(
        StorageKey(bot_id=fake_bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID)
    )
    save_query = make_callback_query(
        "topic:save",
        message=make_message("selection", message_id=pack_data["message_id"]),
    )
    await dp.feed_update(fake_bot, Update(update_id=2, callback_query=save_query))
    async with session_factory() as session:
        card = (await session.execute(select(Card).where(Card.lemma != ""))).scalars().first()
    assert card is not None


async def test_voice_routes_by_state(dp, fake_bot, session_factory, llm):
    """Idle voice -> capture; /talk voice -> conversation turn."""
    from aiogram.types import Update

    from frbot.llm.schemas import TalkTurn, VoiceWords
    from tests.fakes import make_voice_message

    llm.voice_words_results.append(VoiceWords(words=["boulangerie"]))
    voice_update = Update(update_id=10, message=make_voice_message())
    await dp.feed_update(fake_bot, voice_update)
    assert llm.voice_words_calls == ["audio/ogg"]  # went to capture

    llm.talk_results.extend(
        [
            TalkTurn(reply_fr="Salut ! Ça va ?"),
            TalkTurn(transcript="ça va bien", reply_fr="Super !"),
        ]
    )
    await dp.feed_update(fake_bot, message_update("/talk", update_id=11))
    await dp.feed_update(fake_bot, Update(update_id=12, message=make_voice_message(message_id=3)))
    assert llm.talk_calls[-1]["kind"] == "turn"
    assert llm.talk_calls[-1]["audio"] is True
    assert len(llm.voice_words_calls) == 1  # capture did NOT fire this time


async def test_talk_text_not_captured(dp, fake_bot, session_factory, llm):
    from frbot.llm.schemas import TalkTurn

    llm.talk_results.extend([TalkTurn(reply_fr="Salut !"), TalkTurn(reply_fr="Génial !")])
    await dp.feed_update(fake_bot, message_update("/talk"))
    await dp.feed_update(fake_bot, message_update("je mange une pomme", update_id=2))
    assert llm.talk_calls[-1]["text"] == "je mange une pomme"
    assert llm.enrich_calls == []  # capture stayed out of the conversation
