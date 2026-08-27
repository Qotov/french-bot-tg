"""Pre-deploy features: timezone, account erasure, deck management, alerting,
and a restore that has actually been exercised.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, select

from frbot.bot.alerts import LLM_FAILURE_THRESHOLD, AdminAlerter
from frbot.bot.handlers import deck
from frbot.bot.handlers.settings import _validate, on_timezone_chosen
from frbot.bot.handlers.system import cmd_delete_me, handle_delete_confirmation
from frbot.db import repo
from frbot.db.models import Card, Review, User, Writing
from tests.fakes import (
    ALLOWED_USER_ID,
    add_vocab_card,
    make_callback_query,
    make_message,
)


def state_for(bot) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID),
    )


# ------------------------------------------------------------------ timezone


async def test_timezone_is_per_user_and_drives_the_day_boundary(
    fake_bot, session_factory, settings, user
):
    state = state_for(fake_bot)
    await on_timezone_chosen(
        make_callback_query("tz:Asia/Tbilisi", bot=fake_bot),
        state,
        user,
        session_factory,
        settings,
    )
    async with session_factory() as session:
        cfg = await repo.get_effective_config(session, settings, user_id=user.id)
        row = await repo.get_user(session, user.id)
    assert cfg.tz == "Asia/Tbilisi"
    assert repo.user_tz(row, settings) == "Asia/Tbilisi"


def test_invalid_timezone_is_rejected():
    assert _validate("TZ", "Europe/Paris") == "Europe/Paris"
    assert _validate("TZ", "Mars/Olympus") is None
    assert _validate("TZ", "") is None


async def test_unset_timezone_falls_back_to_the_server_default(session_factory, settings, user):
    async with session_factory() as session:
        cfg = await repo.get_effective_config(session, settings, user_id=user.id)
    assert cfg.tz == settings.tz


async def test_a_corrupt_timezone_does_not_break_delivery(session_factory, settings):
    async with session_factory() as session:
        session.add(User(id=777, chat_id=777, tz="Nowhere/Fake"))
        await session.commit()
        cfg = await repo.get_effective_config(session, settings, user_id=777)
    assert cfg.tz == settings.tz  # falls back rather than raising in the tick


# ------------------------------------------------------------- account erasure


async def test_delete_me_erases_everything_after_confirmation(
    fake_bot, session_factory, settings, user
):
    await add_vocab_card(session_factory, "maison")
    async with session_factory() as session:
        session.add(Writing(user_id=user.id, prompt="Décris ta journée."))
        session.add(
            Review(
                user_id=user.id,
                card_id=1,
                rating=3,
                reviewed_at=datetime.now(UTC),
                elapsed_days=0.0,
            )
        )
        await session.commit()

    state = state_for(fake_bot)
    await cmd_delete_me(make_message("/delete_me", bot=fake_bot), state)
    assert "УДАЛИТЬ" in fake_bot.session.sent_messages[-1].text

    await handle_delete_confirmation(
        make_message("УДАЛИТЬ", bot=fake_bot), state, user, session_factory
    )
    async with session_factory() as session:
        assert (await session.execute(select(func.count(Card.id)))).scalar_one() == 0
        assert (await session.execute(select(func.count(Review.id)))).scalar_one() == 0
        assert (await session.execute(select(func.count(Writing.id)))).scalar_one() == 0
        assert await repo.get_user(session, user.id) is None
    assert "удалено" in fake_bot.session.sent_messages[-1].text


async def test_delete_me_needs_the_exact_word(fake_bot, session_factory, user):
    await add_vocab_card(session_factory, "maison")
    state = state_for(fake_bot)
    await cmd_delete_me(make_message("/delete_me", bot=fake_bot), state)
    await handle_delete_confirmation(make_message("да", bot=fake_bot), state, user, session_factory)
    async with session_factory() as session:
        assert await repo.count_cards(session, user_id=user.id) == 1
        assert await repo.get_user(session, user.id) is not None
    assert "Отменил" in fake_bot.session.sent_messages[-1].text


async def test_delete_me_only_touches_the_asking_user(fake_bot, session_factory, user):
    async with session_factory() as session:
        session.add(User(id=999, chat_id=999))
        await session.commit()
    await add_vocab_card(session_factory, "mien", user_id=user.id)
    await add_vocab_card(session_factory, "sien", user_id=999)

    state = state_for(fake_bot)
    await cmd_delete_me(make_message("/delete_me", bot=fake_bot), state)
    await handle_delete_confirmation(
        make_message("УДАЛИТЬ", bot=fake_bot), state, user, session_factory
    )
    async with session_factory() as session:
        assert await repo.count_cards(session, user_id=999) == 1
        assert await repo.get_user(session, 999) is not None


# ------------------------------------------------------------------ /cards


async def test_cards_lists_the_deck_with_controls(fake_bot, session_factory, user):
    for lemma in ("maison", "voiture", "jardin"):
        await add_vocab_card(session_factory, lemma)
    await deck.cmd_cards(make_message("/cards", bot=fake_bot), user, session_factory)
    sent = fake_bot.session.sent_messages[-1]
    assert "Твоя колода" in sent.text
    assert "3 карточек" in sent.text
    labels = [b.text for row in sent.reply_markup.inline_keyboard for b in row]
    assert any("maison" in lbl for lbl in labels)


async def test_cards_on_an_empty_deck_points_at_the_next_action(fake_bot, session_factory, user):
    await deck.cmd_cards(make_message("/cards", bot=fake_bot), user, session_factory)
    text = fake_bot.session.sent_messages[-1].text
    assert "/topic" in text


async def test_suspending_a_card_removes_it_from_the_review_queue(
    fake_bot, session_factory, user, settings
):
    from frbot.srs.queue import build_queue

    card_id = await add_vocab_card(
        session_factory, "gênant", reviewed_days_ago=2, due=datetime.now(UTC) - timedelta(hours=1)
    )
    await deck.on_toggle(
        make_callback_query(f"deck:toggle:{card_id}:0", bot=fake_bot), user, session_factory
    )
    async with session_factory() as session:
        card = await repo.get_card(session, card_id, user_id=user.id)
        queue = await build_queue(
            session,
            user_id=user.id,
            now=datetime.now(UTC),
            tz=settings.tz,
            session_max=30,
            daily_new_limit=15,
        )
    assert card.suspended is True
    assert card_id not in queue.card_ids  # paused, but the card and history remain

    await deck.on_toggle(
        make_callback_query(f"deck:toggle:{card_id}:0", bot=fake_bot), user, session_factory
    )
    async with session_factory() as session:
        assert (await repo.get_card(session, card_id, user_id=user.id)).suspended is False


async def test_cannot_suspend_another_users_card(fake_bot, session_factory, user):
    async with session_factory() as session:
        session.add(User(id=888, chat_id=888))
        await session.commit()
    other = await add_vocab_card(session_factory, "leur", user_id=888)
    await deck.on_toggle(
        make_callback_query(f"deck:toggle:{other}:0", bot=fake_bot), user, session_factory
    )
    async with session_factory() as session:
        assert (await repo.get_card(session, other, user_id=888)).suspended is False


# ----------------------------------------------------------------- alerting


async def test_llm_failures_alert_the_admin_once_they_pile_up(fake_bot):
    alerter = AdminAlerter(ALLOWED_USER_ID)
    for _ in range(LLM_FAILURE_THRESHOLD - 1):
        await alerter.record_llm_failure(fake_bot, "enrich")
    assert fake_bot.session.sent_messages == []  # a single blip is not an incident

    await alerter.record_llm_failure(fake_bot, "enrich")
    assert len(fake_bot.session.sent_messages) == 1
    assert "LLM" in fake_bot.session.sent_messages[0].text


async def test_alerts_of_the_same_kind_are_rate_limited(fake_bot):
    alerter = AdminAlerter(ALLOWED_USER_ID)
    assert await alerter.send(fake_bot, "boom", "first") is True
    assert await alerter.send(fake_bot, "boom", "second") is False  # inside cooldown
    assert await alerter.send(fake_bot, "other", "different kind") is True
    assert len(fake_bot.session.sent_messages) == 2


async def test_a_failing_admin_chat_does_not_raise(session_factory):
    class Broken:
        async def send_message(self, *a, **k):
            raise RuntimeError("admin blocked the bot")

    alerter = AdminAlerter(ALLOWED_USER_ID)
    assert await alerter.send(Broken(), "kind", "text") is False  # swallowed


async def test_heartbeat_reports_the_pilot_numbers(fake_bot, session_factory, settings):
    from frbot.jobs.reminders import send_heartbeat

    async with session_factory() as session:
        session.add(User(id=ALLOWED_USER_ID, chat_id=ALLOWED_USER_ID))
        session.add(User(id=321, chat_id=321))
        session.add(
            Review(
                user_id=ALLOWED_USER_ID,
                card_id=1,
                rating=3,
                reviewed_at=datetime.now(UTC),
                elapsed_days=0.0,
            )
        )
        await session.commit()

    await send_heartbeat(fake_bot, session_factory, settings, AdminAlerter(ALLOWED_USER_ID))
    text = fake_bot.session.sent_messages[-1].text
    assert "Бот жив" in text
    assert "Участников: 2" in text
    assert "Активны за 7 дней: 1" in text


# ------------------------------------------------------------------ restore


async def test_backup_can_actually_be_restored(tmp_path: Path, settings):
    """An untested backup is not a backup: take one, destroy the original,
    put the snapshot back, and read the data out again."""
    from frbot.jobs.reminders import backup_database

    db_path = tmp_path / "frbot.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY, lemma TEXT)")
    conn.executemany("INSERT INTO cards (lemma) VALUES (?)", [("maison",), ("voiture",)])
    conn.commit()
    conn.close()

    await backup_database(settings.model_copy(update={"db_url": f"sqlite+aiosqlite:///{db_path}"}))
    snapshots = sorted((tmp_path / "backups").glob("frbot-*.db"))
    assert len(snapshots) == 1

    db_path.unlink()  # the disaster
    import shutil

    shutil.copy2(snapshots[0], db_path)  # the documented restore procedure

    conn = sqlite3.connect(db_path)
    rows = [r[0] for r in conn.execute("SELECT lemma FROM cards ORDER BY id")]
    conn.close()
    assert rows == ["maison", "voiture"]


async def test_all_jobs_including_heartbeat_are_registered(fake_bot, session_factory, settings):
    from types import SimpleNamespace

    from aiogram.fsm.storage.memory import MemoryStorage

    from frbot.jobs.reminders import (
        BACKUP_JOB_ID,
        CLEANUP_JOB_ID,
        HEARTBEAT_JOB_ID,
        TICK_JOB_ID,
        WEEKLY_JOB_ID,
        create_scheduler,
        setup_jobs,
    )

    scheduler = create_scheduler(settings.tz)
    setup_jobs(
        scheduler,
        fake_bot,
        SimpleNamespace(storage=MemoryStorage()),
        session_factory,
        settings,
        AdminAlerter(ALLOWED_USER_ID),
    )
    scheduler.start(paused=True)
    try:
        for job_id in (TICK_JOB_ID, WEEKLY_JOB_ID, BACKUP_JOB_ID, CLEANUP_JOB_ID, HEARTBEAT_JOB_ID):
            assert scheduler.get_job(job_id) is not None, job_id
    finally:
        scheduler.shutdown(wait=False)


async def test_users_in_different_timezones_are_not_renotified_every_tick(
    fake_bot, session_factory, settings
):
    """Participants can legitimately be on different calendar dates at the same
    instant. The delivered-today set must not let them purge each other's
    records — that re-sent everyone's reminder on every single tick."""
    from zoneinfo import ZoneInfo

    from frbot.jobs.reminders import _sent_today, drain_deliveries, minute_tick
    from tests.test_jobs import fake_dispatcher

    _sent_today.clear()
    east, west = "Pacific/Kiritimati", "Pacific/Midway"  # ~26 hours apart

    def local_hh_mm(tz: str) -> str:
        return datetime.now(UTC).astimezone(ZoneInfo(tz)).strftime("%H:%M")

    async with session_factory() as session:
        session.add(User(id=111, chat_id=111, tz=east, reminder_time=local_hh_mm(east)))
        session.add(User(id=222, chat_id=222, tz=west, reminder_time=local_hh_mm(west)))
        await session.commit()

    for _ in range(3):
        await minute_tick(fake_bot, fake_dispatcher(), session_factory, settings)
        await drain_deliveries()

    counts: dict[int, int] = {}
    for message in fake_bot.session.sent_messages:
        counts[message.chat_id] = counts.get(message.chat_id, 0) + 1
    assert counts == {111: 1, 222: 1}


async def test_delivered_set_does_not_grow_without_bound(fake_bot, session_factory, settings):
    from frbot.jobs.reminders import _mark_sent, _sent_today

    _sent_today.clear()
    base = datetime(2026, 5, 1, tzinfo=UTC).date()
    for offset in range(10):
        assert _mark_sent(1, "reminder", base + timedelta(days=offset)) is True
    # Only the recent window is retained.
    assert len(_sent_today) <= 3


# --------------------------------------- onboarding must never dead-end


async def test_starter_deck_survives_a_non_llm_transport_error(
    fake_bot, session_factory, settings, user, usage, alerter
):
    """The transport can raise things that are not LLMError. Onboarding must
    still reach the first-steps message — it carries the privacy notice and is
    the only place the learner is told what to do next."""
    from frbot.bot.handlers.system import on_level_chosen
    from frbot.srs.scheduler import SrsScheduler
    from tests.fakes import FakeLLM

    llm = FakeLLM(topic_results=[RuntimeError("aiohttp: payload truncated")])
    await on_level_chosen(
        make_callback_query("level:B1", bot=fake_bot),
        state_for(fake_bot),
        user,
        session_factory,
        llm,
        SrsScheduler(0.9),
        settings,
        usage,
        alerter,
    )
    texts = [m.text or "" for m in fake_bot.session.sent_messages]
    assert any("Не получилось собрать" in t for t in texts)
    assert any("delete_me" in t for t in texts)  # first-steps + privacy delivered


async def test_starter_deck_survives_a_failing_enrichment(
    fake_bot, session_factory, settings, user, usage, alerter
):
    from frbot.bot.handlers.topic import build_starter_deck
    from frbot.llm.schemas import Enrichment, TopicWordList
    from frbot.srs.scheduler import SrsScheduler
    from tests.fakes import FakeLLM, enrichment_dict

    llm = FakeLLM(
        topic_results=[
            TopicWordList.model_validate(
                {"words": [{"lemma": w, "translation_ru": "…"} for w in ("un", "deux")]}
            )
        ],
        enrich_results=[
            RuntimeError("connection reset"),
            Enrichment.model_validate(enrichment_dict("deux")),
        ],
    )
    created = await build_starter_deck(
        user, session_factory, llm, SrsScheduler(0.9), settings, usage, alerter, fake_bot
    )
    assert created == ["deux"]  # the good one still lands


# ------------------------------------------- alerts must actually arrive


async def test_alert_with_markup_in_the_text_still_reaches_the_admin(fake_bot):
    """Exception strings contain <, > and &. An alert that Telegram rejects as
    bad HTML is worse than no alerting, because the failure is silent."""
    from aiogram.methods import SendMessage

    from frbot.bot.alerts import AdminAlerter

    original = fake_bot.session._result_for

    def reject_html(method):
        if isinstance(method, SendMessage) and method.parse_mode is not None:
            raise RuntimeError("Bad Request: can't parse entities")
        return original(method)

    fake_bot.session._result_for = reject_html
    alerter = AdminAlerter(ALLOWED_USER_ID)
    delivered = await alerter.send(
        fake_bot, "kind", "<b>Ошибка</b>\n<code>KeyError: <unclosed & broken</code>"
    )
    assert delivered is True
    sent = fake_bot.session.sent("SendMessage")[-1]
    assert sent.parse_mode is None  # fell back to plain text
    assert "KeyError" in sent.text


def test_alert_escaping_neutralises_markup():
    from frbot.bot.alerts import esc

    assert esc("<script>&") == "&lt;script&gt;&amp;"


# --------------------------------------------- deck paging and delete UX


async def test_deleting_the_last_card_of_a_page_does_not_strand_the_user(
    fake_bot, session_factory, user
):
    from frbot.bot.handlers import deck as deck_mod

    ids = [await add_vocab_card(session_factory, f"mot-{i}") for i in range(9)]
    # Page 2 holds exactly one card; delete it and the view must fall back.
    await deck_mod.on_delete(
        make_callback_query(f"deck:del:{ids[0]}:8", bot=fake_bot), user, session_factory
    )
    text = fake_bot.session.sent("EditMessageText")[-1].text
    assert "Твоя колода" in text
    assert "8 карточек" in text


async def test_stale_delete_confirmation_does_not_erase_the_account(
    fake_bot, session_factory, user
):
    from frbot.bot.handlers.system import (
        DeleteStates,
        handle_delete_confirmation,
    )

    await add_vocab_card(session_factory, "maison")
    state = state_for(fake_bot)
    await state.set_state(DeleteStates.confirming)
    await state.update_data(delete_armed_at=0)  # armed long ago

    await handle_delete_confirmation(
        make_message("УДАЛИТЬ", bot=fake_bot), state, user, session_factory
    )
    async with session_factory() as session:
        assert await repo.get_user(session, user.id) is not None
        assert await repo.count_cards(session, user_id=user.id) == 1


async def test_timezone_tap_does_not_cancel_an_unrelated_session(
    fake_bot, session_factory, settings, user
):
    from frbot.bot.handlers.review import ReviewStates
    from frbot.bot.handlers.settings import on_timezone_chosen

    state = state_for(fake_bot)
    await state.set_state(ReviewStates.reviewing)
    await state.set_data({"queue": [1], "index": 0, "total": 1, "reviewed": 0, "again": 0})

    await on_timezone_chosen(
        make_callback_query("tz:Europe/Berlin", bot=fake_bot),
        state,
        user,
        session_factory,
        settings,
    )
    assert await state.get_state() == ReviewStates.reviewing.state
    async with session_factory() as session:
        assert (await repo.get_user(session, user.id)).tz == "Europe/Berlin"
