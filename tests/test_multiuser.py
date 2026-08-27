"""The pilot's load-bearing guarantees: data isolation, invites, admin tools,
per-user level, and the LLM cost guardrail.

Cross-user leakage is the one bug class this refactor must not have, so the
isolation tests here exercise every read path a handler can reach.
"""

from datetime import UTC, datetime, timedelta

import pytest

from frbot.db import repo
from frbot.db.models import User
from frbot.srs.queue import build_queue
from frbot.srs.scheduler import SrsScheduler
from frbot.usage import UsageLimiter
from tests.fakes import ALLOWED_USER_ID, add_vocab_card, make_message

ALICE = ALLOWED_USER_ID
BOB = 555_001


def srs() -> SrsScheduler:
    return SrsScheduler(desired_retention=0.9)


def now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
async def two_users(session_factory):
    async with session_factory() as session:
        session.add(User(id=ALICE, chat_id=ALICE, level="B1"))
        session.add(User(id=BOB, chat_id=BOB, level="B2"))
        await session.commit()


# ------------------------------------------------------------- isolation


async def test_cards_are_not_visible_across_users(session_factory, two_users):
    await add_vocab_card(session_factory, "maison", user_id=ALICE)
    bob_card = await add_vocab_card(session_factory, "voiture", user_id=BOB)

    async with session_factory() as session:
        assert await repo.count_cards(session, user_id=ALICE) == 1
        assert await repo.count_cards(session, user_id=BOB) == 1
        # Alice cannot fetch Bob's card by id or by lemma.
        assert await repo.get_card(session, bob_card, user_id=ALICE) is None
        assert await repo.find_card_by_lemma(session, "voiture", user_id=ALICE) is None
        assert await repo.find_card_by_lemma(session, "voiture", user_id=BOB) is not None


async def test_same_lemma_can_exist_for_both_users(session_factory, two_users):
    """Dedupe is per user: Bob learning a word Alice already has is not a duplicate."""
    await add_vocab_card(session_factory, "maison", user_id=ALICE)
    await add_vocab_card(session_factory, "maison", user_id=BOB)
    async with session_factory() as session:
        assert await repo.count_cards(session, user_id=ALICE) == 1
        assert await repo.count_cards(session, user_id=BOB) == 1


async def test_review_queue_only_contains_own_cards(session_factory, two_users):
    alice_due = await add_vocab_card(
        session_factory, "dû-a", user_id=ALICE, reviewed_days_ago=2, due=now() - timedelta(hours=1)
    )
    await add_vocab_card(
        session_factory, "dû-b", user_id=BOB, reviewed_days_ago=2, due=now() - timedelta(hours=1)
    )
    await add_vocab_card(session_factory, "neuf-b", user_id=BOB)

    async with session_factory() as session:
        queue = await build_queue(
            session,
            user_id=ALICE,
            now=now(),
            tz="Europe/Paris",
            session_max=30,
            daily_new_limit=15,
        )
    assert queue.card_ids == [alice_due]


async def test_stats_and_due_counts_are_per_user(session_factory, two_users):
    for i in range(3):
        await add_vocab_card(
            session_factory,
            f"a-{i}",
            user_id=ALICE,
            reviewed_days_ago=2,
            due=now() - timedelta(hours=1),
        )
    await add_vocab_card(
        session_factory, "b-0", user_id=BOB, reviewed_days_ago=2, due=now() - timedelta(hours=1)
    )

    async with session_factory() as session:
        alice = await repo.gather_stats(
            session,
            user_id=ALICE,
            due_until=now(),
            week_ago=now() - timedelta(days=7),
            month_ago=now() - timedelta(days=30),
        )
        bob = await repo.gather_stats(
            session,
            user_id=BOB,
            due_until=now(),
            week_ago=now() - timedelta(days=7),
            month_ago=now() - timedelta(days=30),
        )
    assert alice.due_today == 3
    assert bob.due_today == 1
    assert alice.total_cards == 3
    assert bob.total_cards == 1


async def test_error_card_dedupe_is_per_user(session_factory, two_users):
    """Bob making the same mistake as Alice must still get his own card."""
    async with session_factory() as session:
        for uid in (ALICE, BOB):
            card = await repo.create_error_card(
                session,
                srs(),
                user_id=uid,
                kind="error",
                sentence="Je suis allé au marché.",
                original="j'ai allé",
                corrected="je suis allé",
                err_type="auxiliary",
                explanation_ru="aller спрягается с être.",
            )
            assert card is not None
        await session.commit()
        assert await repo.count_cards(session, user_id=ALICE) == 1
        assert await repo.count_cards(session, user_id=BOB) == 1


async def test_writings_are_scoped(session_factory, two_users):
    async with session_factory() as session:
        writing = await repo.create_writing(session, "Décris ta journée.", user_id=ALICE)
        await session.commit()
        wid = writing.id
        assert await repo.get_writing(session, wid, user_id=ALICE) is not None
        assert await repo.get_writing(session, wid, user_id=BOB) is None


async def test_writing_word_picking_uses_only_own_vocabulary(session_factory, two_users):
    await add_vocab_card(session_factory, "alice-mot", user_id=ALICE)
    await add_vocab_card(session_factory, "bob-mot", user_id=BOB)
    async with session_factory() as session:
        words = await repo.pick_writing_words(
            session, user_id=ALICE, due_until=now() + timedelta(days=1)
        )
        lemmas = await repo.get_recent_lemmas(session, user_id=ALICE)
    assert "bob-mot" not in words
    assert "bob-mot" not in lemmas


async def test_delete_cannot_touch_another_users_card(session_factory, two_users):
    bob_card = await add_vocab_card(session_factory, "voiture", user_id=BOB)
    async with session_factory() as session:
        deleted = await repo.delete_card(session, bob_card, user_id=ALICE)
        await session.commit()
    assert deleted is False
    async with session_factory() as session:
        assert await repo.count_cards(session, user_id=BOB) == 1


async def test_daily_new_limit_is_counted_per_user(session_factory, two_users):
    """Alice hitting her daily new-card limit must not throttle Bob."""
    fixed = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    for i in range(3):
        card_id = await add_vocab_card(
            session_factory,
            f"a-intro-{i}",
            user_id=ALICE,
            reviewed_days_ago=2,
            due=fixed - timedelta(hours=1),
        )
        async with session_factory() as session:
            card = await repo.get_card(session, card_id, user_id=ALICE)
            result = srs().review(card.fsrs, 3, fixed - timedelta(hours=2))
            await repo.apply_review(
                session, card, result, user_id=ALICE, rating=3, now=fixed - timedelta(hours=2)
            )
            await session.commit()
    for i in range(5):
        await add_vocab_card(session_factory, f"a-new-{i}", user_id=ALICE)
        await add_vocab_card(session_factory, f"b-new-{i}", user_id=BOB)

    async with session_factory() as session:
        alice_q = await build_queue(
            session, user_id=ALICE, now=fixed, tz="Europe/Paris", session_max=30, daily_new_limit=4
        )
        bob_q = await build_queue(
            session, user_id=BOB, now=fixed, tz="Europe/Paris", session_max=30, daily_new_limit=4
        )
    assert alice_q.new_count == 1  # 3 of 4 already introduced today
    assert bob_q.new_count == 4  # Bob's allowance is untouched


# ---------------------------------------------------------------- invites


async def test_invite_codes_are_unique_and_redeemable_once(session_factory):
    async with session_factory() as session:
        a = await repo.create_invite(session, created_by=ALICE)
        b = await repo.create_invite(session, created_by=ALICE)
        await session.commit()
        assert a.code != b.code
        assert len(a.code) == 8

        assert await repo.redeem_invite(session, a.code) is not None
        assert await repo.redeem_invite(session, a.code) is None  # exhausted
        assert await repo.redeem_invite(session, "NOSUCH01") is None


async def test_invite_code_is_case_insensitive(session_factory):
    async with session_factory() as session:
        invite = await repo.create_invite(session, created_by=ALICE)
        await session.commit()
        assert await repo.redeem_invite(session, invite.code.lower()) is not None


async def test_multi_use_invite_counts_down(session_factory):
    async with session_factory() as session:
        invite = await repo.create_invite(session, created_by=ALICE, max_uses=3)
        await session.commit()
        for _ in range(3):
            assert await repo.redeem_invite(session, invite.code) is not None
        assert await repo.redeem_invite(session, invite.code) is None


# ------------------------------------------------------------------ level


async def test_level_is_per_user_and_validated(session_factory, two_users, settings):
    async with session_factory() as session:
        assert await repo.set_user_level(session, ALICE, "A2") is True
        assert await repo.set_user_level(session, ALICE, "C2") is False  # not offered
        await session.commit()
        alice = await repo.get_effective_config(session, settings, user_id=ALICE)
        bob = await repo.get_effective_config(session, settings, user_id=BOB)
    assert alice.level == "A2"
    assert bob.level == "B2"


def test_level_clause_changes_the_system_prompt():
    from frbot.llm import prompts

    a2 = prompts.with_level(prompts.ENRICH_SYSTEM, "A2")
    b2 = prompts.with_level(prompts.ENRICH_SYSTEM, "B2")
    assert "A2" in a2 and "under 10 words" in a2
    assert "B2" in b2 and "idiomatic" in b2
    # An unknown level must not crash — it falls back to B1.
    assert prompts.LEVEL_CLAUSES["B1"] in prompts.with_level(prompts.ENRICH_SYSTEM, "Z9")


# ------------------------------------------------------------ usage limiter


def test_usage_limiter_caps_per_user():
    limiter = UsageLimiter(daily_limit=3, tz="Europe/Paris")
    assert all(limiter.check_and_count(ALICE) for _ in range(3))
    assert limiter.check_and_count(ALICE) is False
    # Bob is unaffected by Alice hitting her cap.
    assert limiter.check_and_count(BOB) is True
    assert limiter.used_today(ALICE) == 3


def test_usage_limiter_resets_on_the_next_day():
    limiter = UsageLimiter(daily_limit=1, tz="Europe/Paris")
    today = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    assert limiter.check_and_count(ALICE, today) is True
    assert limiter.check_and_count(ALICE, today) is False
    tomorrow = today + timedelta(days=1)
    assert limiter.check_and_count(ALICE, tomorrow) is True


# ------------------------------------------- the cap as handlers see it


async def test_capture_refuses_once_the_daily_cap_is_reached(
    fake_bot, session_factory, settings, user
):
    """The guardrail must stop the LLM call, not just log it."""
    from frbot.bot.handlers.capture import handle_capture
    from frbot.llm.schemas import Enrichment
    from tests.fakes import FakeLLM, enrichment_dict, make_message

    limiter = UsageLimiter(daily_limit=1, tz=settings.tz)
    llm = FakeLLM(enrich_results=[Enrichment.model_validate(enrichment_dict("maison"))])

    await handle_capture(
        make_message("maison", bot=fake_bot), user, session_factory, llm, srs(), settings, limiter
    )
    assert len(llm.enrich_calls) == 1

    await handle_capture(
        make_message("voiture", bot=fake_bot), user, session_factory, llm, srs(), settings, limiter
    )
    assert len(llm.enrich_calls) == 1  # blocked before reaching Gemini
    assert "лимит" in fake_bot.session.sent_messages[-1].text
    async with session_factory() as session:
        assert await repo.count_cards(session, user_id=user.id) == 1


async def test_cap_does_not_block_reviews(fake_bot, session_factory, settings, user):
    """Reviewing costs nothing, so it must keep working after the cap."""
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from frbot.bot.handlers.review import cmd_review

    await add_vocab_card(
        session_factory, "dû", user_id=user.id, reviewed_days_ago=2, due=now() - timedelta(hours=1)
    )
    limiter = UsageLimiter(daily_limit=0, tz=settings.tz)
    assert limiter.check_and_count(user.id) is False

    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=fake_bot.id, chat_id=user.id, user_id=user.id),
    )
    await cmd_review(make_message("/review", bot=fake_bot), state, user, session_factory, settings)
    assert any("1/1" in (m.text or "") for m in fake_bot.session.sent_messages)


# ---------------------------------------------------- end-to-end isolation

# Card ids are sequential integers, so a participant can trivially guess another
# person's card id. These tests drive the REAL dispatcher (auth middleware, DI,
# routers) to prove the guess is useless — repo-level scoping is not enough on
# its own if a handler ever forgets to pass the owner.


def _dispatcher(settings, session_factory, llm):
    from frbot.__main__ import build_dispatcher
    from frbot.srs.scheduler import SrsScheduler

    return build_dispatcher(
        settings, session_factory, llm=llm, srs=SrsScheduler(settings.desired_retention)
    )


async def test_other_user_cannot_delete_my_card_via_callback(
    fake_bot, session_factory, settings, two_users
):
    """The classic attack: send card:delete:<id> for a card you do not own."""
    from aiogram.types import Update

    from frbot.db import repo
    from tests.fakes import FakeLLM, make_callback_query, make_message

    alice, bob = ALICE, BOB
    card_id = await add_vocab_card(session_factory, "secret", user_id=alice)

    dp = _dispatcher(settings, session_factory, FakeLLM())
    # Bob presses delete on Alice's card id.
    query = make_callback_query(
        f"card:delete:{card_id}",
        user_id=bob,
        message=make_message("preview", user_id=bob, message_id=77),
    )
    await dp.feed_update(fake_bot, Update(update_id=900, callback_query=query))

    async with session_factory() as session:
        assert await repo.get_card(session, card_id, user_id=alice) is not None
    texts = [m.text or "" for m in fake_bot.session.sent("EditMessageText")]
    assert not any("удалена" in t and "уже" not in t for t in texts)


async def test_review_via_dispatcher_serves_only_own_cards(
    fake_bot, session_factory, settings, two_users
):
    from aiogram.types import Update

    from tests.fakes import FakeLLM, make_message

    alice, bob = ALICE, BOB
    now = datetime.now(UTC)
    await add_vocab_card(
        session_factory,
        "alice-mot",
        user_id=alice,
        reviewed_days_ago=2,
        due=now - timedelta(hours=1),
    )
    await add_vocab_card(
        session_factory,
        "bob-mot",
        user_id=bob,
        reviewed_days_ago=2,
        due=now - timedelta(hours=1),
    )

    dp = _dispatcher(settings, session_factory, FakeLLM())
    await dp.feed_update(
        fake_bot, Update(update_id=901, message=make_message("/review", user_id=bob))
    )
    shown = " ".join(m.text or "" for m in fake_bot.session.sent_messages)
    assert "bob-mot" in shown
    assert "alice-mot" not in shown


async def test_unregistered_stranger_reaches_nothing(
    fake_bot, session_factory, settings, two_users
):
    from aiogram.types import Update

    from tests.fakes import FakeLLM, make_message

    llm = FakeLLM()
    dp = _dispatcher(settings, session_factory, llm)
    stranger = 424242
    for i, text in enumerate(["/review", "/stats", "bonjour", "/topic ресторан"]):
        await dp.feed_update(
            fake_bot, Update(update_id=910 + i, message=make_message(text, user_id=stranger))
        )
    assert fake_bot.session.sent_messages == []
    assert llm.enrich_calls == []
