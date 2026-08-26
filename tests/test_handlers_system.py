"""/start registration by invite, /help, /level, /feedback."""

from types import SimpleNamespace

from sqlalchemy import select

from frbot.__main__ import build_dispatcher
from frbot.bot.handlers.system import (
    BAD_INVITE_TEXT,
    FULL_TEXT,
    NEED_INVITE_TEXT,
    cmd_feedback,
    cmd_help,
    cmd_level,
    cmd_start,
    handle_feedback,
    on_level_chosen,
)
from frbot.db import repo
from frbot.db.models import Invite, User
from tests.fakes import (
    ALLOWED_USER_ID,
    OTHER_USER_ID,
    make_callback_query,
    make_message,
    make_update_with_message,
)


def command_obj(args: str | None) -> SimpleNamespace:
    return SimpleNamespace(args=args)


async def make_invite(session_factory, max_uses: int = 1) -> str:
    async with session_factory() as session:
        invite = await repo.create_invite(session, created_by=ALLOWED_USER_ID, max_uses=max_uses)
        await session.commit()
        return invite.code


# ------------------------------------------------------------- registration


async def test_start_without_code_asks_for_one(fake_bot, session_factory, settings):
    message = make_message("/start", user_id=OTHER_USER_ID, bot=fake_bot)
    await cmd_start(message, command_obj(None), None, session_factory, settings)
    assert fake_bot.session.sent_messages[0].text == NEED_INVITE_TEXT
    async with session_factory() as session:
        assert await repo.count_users(session) == 0


async def test_start_with_valid_code_registers_and_asks_level(fake_bot, session_factory, settings):
    code = await make_invite(session_factory)
    message = make_message(f"/start {code}", user_id=OTHER_USER_ID, bot=fake_bot)
    await cmd_start(message, command_obj(code), None, session_factory, settings)

    async with session_factory() as session:
        row = await repo.get_user(session, OTHER_USER_ID)
        invite = await session.get(Invite, code)
    assert row is not None
    assert row.invite_code == code
    assert row.is_admin is False
    assert invite.used_count == 1

    sent = fake_bot.session.sent_messages[0]
    assert "Добро пожаловать" in sent.text
    labels = [b.text for r in sent.reply_markup.inline_keyboard for b in r]
    assert any(label.startswith("A2") for label in labels)
    assert any(label.startswith("B2") for label in labels)


async def test_invite_cannot_be_reused_beyond_its_limit(fake_bot, session_factory, settings):
    code = await make_invite(session_factory, max_uses=1)
    await cmd_start(
        make_message(f"/start {code}", user_id=OTHER_USER_ID, bot=fake_bot),
        command_obj(code),
        None,
        session_factory,
        settings,
    )
    await cmd_start(
        make_message(f"/start {code}", user_id=777_001, bot=fake_bot),
        command_obj(code),
        None,
        session_factory,
        settings,
    )
    assert fake_bot.session.sent_messages[-1].text == BAD_INVITE_TEXT
    async with session_factory() as session:
        assert await repo.count_users(session) == 1


async def test_multi_use_invite_admits_several_people(fake_bot, session_factory, settings):
    code = await make_invite(session_factory, max_uses=3)
    for uid in (OTHER_USER_ID, 777_002, 777_003):
        await cmd_start(
            make_message(f"/start {code}", user_id=uid, bot=fake_bot),
            command_obj(code),
            None,
            session_factory,
            settings,
        )
    async with session_factory() as session:
        assert await repo.count_users(session) == 3


async def test_registration_stops_at_max_users(fake_bot, session_factory, settings):
    small = settings.model_copy(update={"max_users": 1})
    async with session_factory() as session:
        session.add(User(id=ALLOWED_USER_ID, chat_id=ALLOWED_USER_ID))
        await session.commit()
    code = await make_invite(session_factory)
    await cmd_start(
        make_message(f"/start {code}", user_id=OTHER_USER_ID, bot=fake_bot),
        command_obj(code),
        None,
        session_factory,
        small,
    )
    assert fake_bot.session.sent_messages[-1].text == FULL_TEXT
    async with session_factory() as session:
        assert await repo.count_users(session) == 1


async def test_start_for_existing_user_shows_help_and_refreshes_chat_id(
    fake_bot, session_factory, settings, user
):
    message = make_message("/start", bot=fake_bot)
    await cmd_start(message, command_obj(None), user, session_factory, settings)
    assert "frbot" in fake_bot.session.sent_messages[0].text
    assert "/review" in fake_bot.session.sent_messages[0].text


# ------------------------------------------------------------------- level


async def test_level_choice_is_saved_and_first_steps_shown(
    fake_bot, session_factory, settings, user
):
    query = make_callback_query("level:B2", bot=fake_bot)
    await on_level_chosen(query, user, session_factory)
    async with session_factory() as session:
        row = await repo.get_user(session, user.id)
    assert row.level == "B2"
    texts = [m.text for m in fake_bot.session.sent_messages]
    assert any("Три шага" in t for t in texts)


async def test_level_command_offers_the_keyboard(fake_bot, user):
    await cmd_level(make_message("/level", bot=fake_bot), user)
    sent = fake_bot.session.sent_messages[0]
    assert "B1" in sent.text
    assert sent.reply_markup is not None


# ---------------------------------------------------------------- feedback


async def test_feedback_is_forwarded_to_the_admin(fake_bot, session_factory, settings, user):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=fake_bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID),
    )
    await cmd_feedback(make_message("/feedback", bot=fake_bot), state)
    assert (await state.get_data())["awaiting_feedback"] is True

    await handle_feedback(
        make_message("голосовые бы подлиннее", bot=fake_bot), state, user, settings
    )
    assert (await state.get_data())["awaiting_feedback"] is False

    forwarded = [m for m in fake_bot.session.sent_messages if m.chat_id == settings.admin_user_id]
    assert any("подлиннее" in (m.text or "") for m in forwarded)
    assert "Спасибо" in fake_bot.session.sent_messages[-1].text


# ------------------------------------------------------------------ routing


async def test_help_replies_with_usage(fake_bot):
    await cmd_help(make_message("/help", bot=fake_bot))
    assert "/write" in fake_bot.session.sent_messages[0].text


async def test_dispatcher_routes_start_for_registered_user(
    fake_bot, session_factory, settings, user
):
    dp = build_dispatcher(settings, session_factory)
    await dp.feed_update(fake_bot, make_update_with_message("/start"))
    assert len(fake_bot.session.sent_messages) == 1


async def test_dispatcher_ignores_stranger_who_is_not_starting(fake_bot, session_factory, settings):
    dp = build_dispatcher(settings, session_factory)
    await dp.feed_update(fake_bot, make_update_with_message("bonjour", user_id=OTHER_USER_ID))
    assert fake_bot.session.sent_messages == []
    async with session_factory() as session:
        assert (await session.execute(select(User))).scalars().all() == []
