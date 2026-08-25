from frbot.__main__ import build_dispatcher
from frbot.bot.handlers.system import cmd_help, cmd_start
from frbot.db import repo
from tests.fakes import OTHER_USER_ID, make_message, make_update_with_message


async def test_start_stores_chat_id_and_replies(fake_bot, session_factory):
    message = make_message("/start", bot=fake_bot)
    await cmd_start(message, session_factory=session_factory)

    async with session_factory() as session:
        chat_id = await repo.get_setting(session, repo.CHAT_ID_KEY)
    assert chat_id == str(message.chat.id)

    sent = fake_bot.session.sent_messages
    assert len(sent) == 1
    assert "frbot" in sent[0].text
    assert "/review" in sent[0].text


async def test_help_replies_with_usage(fake_bot, session_factory):
    message = make_message("/help", bot=fake_bot)
    await cmd_help(message)
    sent = fake_bot.session.sent_messages
    assert len(sent) == 1
    assert "/write" in sent[0].text


async def test_dispatcher_routes_start_for_allowed_user(fake_bot, session_factory, settings):
    dp = build_dispatcher(settings, session_factory)
    await dp.feed_update(fake_bot, make_update_with_message("/start"))
    assert len(fake_bot.session.sent_messages) == 1


async def test_dispatcher_ignores_other_user(fake_bot, session_factory, settings):
    dp = build_dispatcher(settings, session_factory)
    await dp.feed_update(fake_bot, make_update_with_message("/start", user_id=OTHER_USER_ID))
    assert len(fake_bot.session.sent_messages) == 0
