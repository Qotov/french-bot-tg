from frbot.bot.middleware import WhitelistMiddleware
from tests.fakes import ALLOWED_USER_ID, OTHER_USER_ID, make_update_with_message, make_user


class SpyHandler:
    def __init__(self) -> None:
        self.called = False

    async def __call__(self, event, data):
        self.called = True
        return "handled"


async def test_allows_whitelisted_user():
    mw = WhitelistMiddleware(ALLOWED_USER_ID)
    handler = SpyHandler()
    event = make_update_with_message("hello")
    result = await mw(handler, event, {"event_from_user": make_user(ALLOWED_USER_ID)})
    assert handler.called
    assert result == "handled"


async def test_drops_other_user():
    mw = WhitelistMiddleware(ALLOWED_USER_ID)
    handler = SpyHandler()
    event = make_update_with_message("hello", user_id=OTHER_USER_ID)
    result = await mw(handler, event, {"event_from_user": make_user(OTHER_USER_ID)})
    assert not handler.called
    assert result is None


async def test_drops_when_no_user_in_data_and_extracts_from_update():
    mw = WhitelistMiddleware(ALLOWED_USER_ID)
    handler = SpyHandler()
    event = make_update_with_message("hello", user_id=OTHER_USER_ID)
    result = await mw(handler, event, {})
    assert not handler.called
    assert result is None

    allowed_event = make_update_with_message("hello", user_id=ALLOWED_USER_ID)
    result = await mw(handler, allowed_event, {})
    assert handler.called
    assert result == "handled"
