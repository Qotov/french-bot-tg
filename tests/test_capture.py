from sqlalchemy import func, select

from frbot.bot.handlers.capture import (
    CAPTURE_MAX_LEN,
    FAIL_TEXT,
    handle_capture,
    on_delete,
    on_regenerate,
)
from frbot.db import repo
from frbot.db.models import Card, CardState
from frbot.llm.client import LLMError
from frbot.llm.schemas import Enrichment
from frbot.srs.scheduler import SrsScheduler
from tests.fakes import (
    ALLOWED_USER_ID,
    FakeLLM,
    load_fixture_json,
    make_callback_query,
    make_message,
)


def enrichment() -> Enrichment:
    return Enrichment.model_validate(load_fixture_json("enrichment_valid.json"))


def srs() -> SrsScheduler:
    return SrsScheduler(desired_retention=0.9)


async def card_count(session_factory) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count(Card.id)))).scalar_one()


async def test_capture_creates_card_with_preview(fake_bot, session_factory, settings, user, usage):
    llm = FakeLLM(enrich_results=[enrichment()])
    message = make_message("au fur et à mesure", bot=fake_bot)
    await handle_capture(message, user, session_factory, llm, srs(), settings, usage)

    async with session_factory() as session:
        card = await repo.find_card_by_lemma(session, "au fur et à mesure", user_id=ALLOWED_USER_ID)
    assert card is not None
    assert card.kind == "vocab"
    assert card.state == CardState.new.value
    assert card.enrichment["translation_ru"] == "по мере того как; постепенно"
    assert card.fsrs["card_id"]

    assert len(fake_bot.session.sent("SendChatAction")) == 1
    sent = fake_bot.session.sent_messages
    assert len(sent) == 1
    assert "au fur et à mesure" in sent[0].text
    assert "fyʁ" in sent[0].text
    buttons = [b.text for row in sent[0].reply_markup.inline_keyboard for b in row]
    assert any("Delete" in b for b in buttons)
    assert any("Regenerate" in b for b in buttons)


async def test_capture_same_input_returns_existing_card(
    fake_bot, session_factory, settings, user, usage
):
    llm = FakeLLM(enrich_results=[enrichment()])
    await handle_capture(
        make_message("au fur et à mesure", bot=fake_bot),
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
    )
    await handle_capture(
        make_message("Au fur et à mesure ", bot=fake_bot),
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
    )

    assert await card_count(session_factory) == 1
    assert len(llm.enrich_calls) == 1  # second capture hit the lemma pre-check
    second_reply = fake_bot.session.sent_messages[1]
    assert "уже есть" in second_reply.text


async def test_capture_dedupes_when_enrichment_returns_known_lemma(
    fake_bot, session_factory, settings, user, usage
):
    llm = FakeLLM(enrich_results=[enrichment()])
    await handle_capture(
        make_message("au fur et à mesure", bot=fake_bot),
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
    )
    # Different raw input, same lemma from the LLM.
    await handle_capture(
        make_message("au fur et a mesure", bot=fake_bot),
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
    )

    assert await card_count(session_factory) == 1
    assert len(llm.enrich_calls) == 2
    assert "уже есть" in fake_bot.session.sent_messages[1].text


async def test_capture_llm_failure_reports_and_stores_nothing(
    fake_bot, session_factory, settings, user, usage
):
    llm = FakeLLM(enrich_results=[LLMError("down")])
    await handle_capture(
        make_message("au fur et à mesure", bot=fake_bot),
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
    )
    assert await card_count(session_factory) == 0
    assert fake_bot.session.sent_messages[0].text == FAIL_TEXT


async def test_capture_too_long_is_rejected_without_llm_call(
    fake_bot, session_factory, settings, user, usage
):
    llm = FakeLLM()
    await handle_capture(
        make_message("x" * (CAPTURE_MAX_LEN + 1), bot=fake_bot),
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
    )
    assert llm.enrich_calls == []
    assert await card_count(session_factory) == 0
    assert "Слишком длинно" in fake_bot.session.sent_messages[0].text


async def test_delete_callback_removes_card(fake_bot, session_factory, settings, user, usage):
    llm = FakeLLM(enrich_results=[enrichment()])
    await handle_capture(
        make_message("au fur et à mesure", bot=fake_bot),
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
    )
    async with session_factory() as session:
        card = await repo.find_card_by_lemma(session, "au fur et à mesure", user_id=ALLOWED_USER_ID)

    query = make_callback_query(f"card:delete:{card.id}", bot=fake_bot)
    await on_delete(query, user, session_factory)

    assert await card_count(session_factory) == 0
    edits = fake_bot.session.sent("EditMessageText")
    assert len(edits) == 1
    assert "удалена" in edits[0].text
    assert len(fake_bot.session.sent("AnswerCallbackQuery")) == 1


async def test_regenerate_callback_updates_enrichment(
    fake_bot, session_factory, settings, user, usage
):
    regenerated = enrichment().model_copy(update={"translation_ru": "постепенно (обновлено)"})
    llm = FakeLLM(enrich_results=[enrichment(), regenerated])
    await handle_capture(
        make_message("au fur et à mesure", bot=fake_bot),
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
    )
    async with session_factory() as session:
        card = await repo.find_card_by_lemma(session, "au fur et à mesure", user_id=ALLOWED_USER_ID)

    query = make_callback_query(f"card:regen:{card.id}", bot=fake_bot)
    await on_regenerate(query, user, session_factory, llm, settings, usage)

    async with session_factory() as session:
        updated = await repo.get_card(session, card.id, user_id=ALLOWED_USER_ID)
    assert updated.enrichment["translation_ru"] == "постепенно (обновлено)"
    edits = fake_bot.session.sent("EditMessageText")
    assert len(edits) == 1
    assert "обновлено" in edits[0].text
