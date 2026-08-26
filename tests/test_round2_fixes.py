"""Regression tests for the second-round review fixes."""

from datetime import UTC, datetime

from sqlalchemy import select

from frbot.bot import render
from frbot.bot.handlers.capture import handle_capture, on_regenerate
from frbot.db.models import Card
from frbot.llm.schemas import Enrichment, WritingCorrection, WritingError
from frbot.srs.scheduler import SrsScheduler
from frbot.timeutil import day_end_utc, day_start_utc
from tests.fakes import (
    ALLOWED_USER_ID,
    FakeLLM,
    load_fixture_json,
    make_callback_query,
    make_message,
)

# ------------------------------------------------------------- DST boundaries


def test_day_end_is_next_local_midnight_on_fallback_day():
    # 2026-10-25: Europe/Paris falls back (25-hour day). Next local midnight
    # (Oct 26 00:00 CET) is 23:00 UTC, not "midnight + 24h" = 22:00 UTC.
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    assert day_end_utc(now, "Europe/Paris") == datetime(2026, 10, 25, 23, 0, tzinfo=UTC)


def test_day_end_is_next_local_midnight_on_springforward_day():
    # 2026-03-29: Europe/Paris springs forward (23-hour day). Next local
    # midnight (Mar 30 00:00 CEST) is 22:00 UTC.
    now = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
    assert day_end_utc(now, "Europe/Paris") == datetime(2026, 3, 29, 22, 0, tzinfo=UTC)


def test_day_start_on_regular_day():
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    # Paris is UTC+2 in August: local midnight = 22:00 UTC the day before.
    assert day_start_utc(now, "Europe/Paris") == datetime(2026, 8, 25, 22, 0, tzinfo=UTC)


# ------------------------------------------------------ regen lemma collision


async def test_regenerate_rejected_when_lemma_belongs_to_other_card(
    fake_bot, session_factory, settings, user, usage
):
    first = Enrichment.model_validate(load_fixture_json("enrichment_valid.json"))
    second = first.model_copy(update={"lemma": "quotidien"})
    srs = SrsScheduler(desired_retention=0.9)
    llm = FakeLLM(enrich_results=[first, second, first])
    await handle_capture(
        make_message("au fur et à mesure", bot=fake_bot),
        user,
        session_factory,
        llm,
        srs,
        settings,
        usage,
    )
    await handle_capture(
        make_message("quotidien", bot=fake_bot), user, session_factory, llm, srs, settings, usage
    )
    async with session_factory() as session:
        cards = list((await session.execute(select(Card).order_by(Card.id))).scalars())
    assert [c.lemma for c in cards] == ["au fur et à mesure", "quotidien"]

    # Regenerating card 2 returns card 1's lemma -> rejected, card untouched.
    query = make_callback_query(f"card:regen:{cards[1].id}", bot=fake_bot)
    await on_regenerate(query, user, session_factory, llm, settings, usage)

    async with session_factory() as session:
        card = await session.get(Card, cards[1].id)
    assert card.lemma == "quotidien"
    assert card.enrichment["lemma"] == "quotidien"
    assert "дубликат" in fake_bot.session.sent_messages[-1].text


# ------------------------------------------------------- message length guard


def test_correction_message_never_exceeds_telegram_limit():
    errors = [
        WritingError(
            original="x" * 300,
            corrected="y" * 300,
            type="other",
            explanation_ru="объяснение " * 30,
        )
        for _ in range(8)
    ]
    correction = WritingCorrection(
        corrected_text="mot " * 1500,  # 6000 chars
        errors=errors,
        comment_ru="комментарий " * 40,
    )
    text = render.correction_message(correction, created_cards=5)
    assert len(text) <= 4096
    # Dropping whole lines keeps tags balanced.
    assert text.count("<i>") == text.count("</i>")
    assert text.count("<b>") == text.count("</b>")


# ------------------------------------------------------------ fsm janitor job


async def test_cleanup_prunes_stray_fsm_entries(fake_bot, settings, session_factory, user):
    from aiogram.fsm.storage.base import StorageKey

    from frbot.jobs.reminders import cleanup_stray_fsm_entries
    from tests.test_jobs import fake_dispatcher

    dispatcher = fake_dispatcher()
    storage = dispatcher.storage
    stranger = StorageKey(bot_id=42, chat_id=999, user_id=999)
    ours = StorageKey(bot_id=42, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID)
    await storage.set_state(stranger, "some:state")
    await storage.set_state(ours, "review:reviewing")

    await cleanup_stray_fsm_entries(dispatcher, session_factory)

    assert stranger not in storage.storage
    assert ours in storage.storage


async def test_writing_job_with_real_dispatcher_isolation(
    fake_bot, session_factory, settings, user
):
    """The job path that takes the real per-user isolation lock."""
    from frbot.__main__ import build_dispatcher
    from frbot.jobs.reminders import send_writing_prompt

    dp = build_dispatcher(settings, session_factory, llm=FakeLLM(), srs=SrsScheduler(0.9))
    await send_writing_prompt(fake_bot, dp, user, session_factory, settings)
    assert any("Задание" in (m.text or "") for m in fake_bot.session.sent_messages)
