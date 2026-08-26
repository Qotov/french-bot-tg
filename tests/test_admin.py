"""Admin commands for running the pilot: /invite, /users, /broadcast."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from frbot.bot.handlers.admin import cmd_broadcast, cmd_invite, cmd_users
from frbot.db import repo
from frbot.db.models import User
from frbot.srs.scheduler import SrsScheduler
from tests.fakes import ALLOWED_USER_ID, add_vocab_card, make_message

PARTICIPANT = 444_001


def command_obj(args: str | None) -> SimpleNamespace:
    return SimpleNamespace(args=args)


@pytest.fixture
async def participant(session_factory) -> User:
    row = User(id=PARTICIPANT, chat_id=PARTICIPANT, username="eleve", level="A2")
    async with session_factory() as session:
        session.add(row)
        await session.commit()
    return row


async def test_invite_creates_codes_with_deep_links(fake_bot, session_factory, settings, user):
    await cmd_invite(
        make_message("/invite 3", bot=fake_bot),
        command_obj("3"),
        user,
        session_factory,
        settings,
    )
    text = fake_bot.session.sent_messages[0].text
    async with session_factory() as session:
        invites = await repo.list_invites(session)
    assert len(invites) == 3
    for invite in invites:
        assert invite.code in text
        assert invite.max_uses == 1
    assert "?start=" in text


async def test_invite_supports_multi_use_codes(fake_bot, session_factory, settings, user):
    await cmd_invite(
        make_message("/invite 1 10", bot=fake_bot),
        command_obj("1 10"),
        user,
        session_factory,
        settings,
    )
    async with session_factory() as session:
        invites = await repo.list_invites(session)
    assert invites[0].max_uses == 10


async def test_invite_is_silent_for_non_admins(fake_bot, session_factory, settings, participant):
    await cmd_invite(
        make_message("/invite", bot=fake_bot),
        command_obj(None),
        participant,
        session_factory,
        settings,
    )
    assert fake_bot.session.sent_messages == []
    async with session_factory() as session:
        assert await repo.list_invites(session) == []


async def test_users_reports_the_cohort_with_activity(
    fake_bot, session_factory, settings, user, participant
):
    # The participant reviewed one card today; the admin has none.
    card_id = await add_vocab_card(session_factory, "mot", user_id=PARTICIPANT, reviewed_days_ago=2)
    async with session_factory() as session:
        card = await repo.get_card(session, card_id, user_id=PARTICIPANT)
        result = SrsScheduler(0.9).review(card.fsrs, 3, datetime.now(UTC))
        await repo.apply_review(
            session, card, result, user_id=PARTICIPANT, rating=3, now=datetime.now(UTC)
        )
        await session.commit()

    await cmd_users(make_message("/users", bot=fake_bot), user, session_factory, settings)
    text = fake_bot.session.sent_messages[0].text
    assert "2" in text  # two registered
    assert "@eleve" in text
    assert "A2" in text
    assert "1/7" in text  # active one day this week
    assert "👑" in text  # admin marked


async def test_users_is_silent_for_non_admins(fake_bot, session_factory, settings, participant):
    await cmd_users(make_message("/users", bot=fake_bot), participant, session_factory, settings)
    assert fake_bot.session.sent_messages == []


async def test_broadcast_reaches_every_active_user(
    fake_bot, session_factory, settings, user, participant
):
    async with session_factory() as session:
        session.add(User(id=444_002, chat_id=444_002, active=False))
        await session.commit()

    await cmd_broadcast(
        make_message("/broadcast Завтра новая тема!", bot=fake_bot),
        command_obj("Завтра новая тема!"),
        user,
        session_factory,
    )
    sent = fake_bot.session.sent_messages
    recipients = {m.chat_id for m in sent if "Завтра" in (m.text or "")}
    assert recipients == {ALLOWED_USER_ID, PARTICIPANT}  # the inactive user is skipped
    assert "Отправлено: 2" in sent[-1].text


async def test_broadcast_requires_text(fake_bot, session_factory, settings, user):
    await cmd_broadcast(
        make_message("/broadcast", bot=fake_bot), command_obj(None), user, session_factory
    )
    assert "Использование" in fake_bot.session.sent_messages[0].text


async def test_broadcast_is_silent_for_non_admins(fake_bot, session_factory, settings, participant):
    await cmd_broadcast(
        make_message("/broadcast hi", bot=fake_bot),
        command_obj("hi"),
        participant,
        session_factory,
    )
    assert fake_bot.session.sent_messages == []


async def test_last_activity_and_active_days(session_factory, participant):
    week_ago = datetime.now(UTC) - timedelta(days=7)
    async with session_factory() as session:
        assert await repo.last_activity_at(session, user_id=PARTICIPANT) is None
        assert await repo.count_active_days(session, user_id=PARTICIPANT, since=week_ago) == 0

    card_id = await add_vocab_card(session_factory, "mot", user_id=PARTICIPANT, reviewed_days_ago=2)
    async with session_factory() as session:
        card = await repo.get_card(session, card_id, user_id=PARTICIPANT)
        result = SrsScheduler(0.9).review(card.fsrs, 3, datetime.now(UTC))
        # Two reviews on the same day count as one active day.
        for _ in range(2):
            await repo.apply_review(
                session, card, result, user_id=PARTICIPANT, rating=3, now=datetime.now(UTC)
            )
        await session.commit()
        assert await repo.last_activity_at(session, user_id=PARTICIPANT) is not None
        assert await repo.count_active_days(session, user_id=PARTICIPANT, since=week_ago) == 1
