"""Access control: only registered, active pilot users reach handlers."""

from frbot.bot.middleware import AuthMiddleware
from frbot.db import repo
from frbot.db.models import User
from tests.fakes import ALLOWED_USER_ID, OTHER_USER_ID, make_update_with_message, make_user


class SpyHandler:
    def __init__(self) -> None:
        self.called = False
        self.data: dict | None = None

    async def __call__(self, event, data):
        self.called = True
        self.data = data
        return "handled"


def mw(settings, session_factory) -> AuthMiddleware:
    return AuthMiddleware(settings, session_factory)


async def add_user(session_factory, user_id: int, *, active: bool = True) -> None:
    async with session_factory() as session:
        session.add(User(id=user_id, chat_id=user_id, active=active))
        await session.commit()


async def test_registered_user_passes_with_user_injected(settings, session_factory):
    await add_user(session_factory, ALLOWED_USER_ID)
    handler = SpyHandler()
    event = make_update_with_message("bonjour")
    result = await mw(settings, session_factory)(
        handler, event, {"event_from_user": make_user(ALLOWED_USER_ID)}
    )
    assert handler.called
    assert result == "handled"
    assert handler.data["user"].id == ALLOWED_USER_ID
    assert handler.data["user_id"] == ALLOWED_USER_ID


async def test_unregistered_user_is_dropped(settings, session_factory):
    handler = SpyHandler()
    event = make_update_with_message("bonjour", user_id=OTHER_USER_ID)
    result = await mw(settings, session_factory)(
        handler, event, {"event_from_user": make_user(OTHER_USER_ID)}
    )
    assert not handler.called
    assert result is None


async def test_unregistered_user_may_reach_start(settings, session_factory):
    """/start is the one door in — it validates the invite itself."""
    handler = SpyHandler()
    event = make_update_with_message("/start ABC123", user_id=OTHER_USER_ID)
    result = await mw(settings, session_factory)(
        handler, event, {"event_from_user": make_user(OTHER_USER_ID)}
    )
    assert handler.called
    assert result == "handled"
    assert handler.data["user"] is None


async def test_deactivated_user_is_dropped(settings, session_factory):
    await add_user(session_factory, ALLOWED_USER_ID, active=False)
    handler = SpyHandler()
    event = make_update_with_message("/start", user_id=ALLOWED_USER_ID)
    result = await mw(settings, session_factory)(
        handler, event, {"event_from_user": make_user(ALLOWED_USER_ID)}
    )
    assert not handler.called
    assert result is None


async def test_admin_is_auto_registered_on_first_contact(settings, session_factory):
    handler = SpyHandler()
    event = make_update_with_message("/start", user_id=ALLOWED_USER_ID)
    await mw(settings, session_factory)(
        handler, event, {"event_from_user": make_user(ALLOWED_USER_ID)}
    )
    assert handler.called
    async with session_factory() as session:
        row = await repo.get_user(session, ALLOWED_USER_ID)
    assert row is not None
    assert row.is_admin


async def test_other_users_are_not_auto_registered(settings, session_factory):
    handler = SpyHandler()
    event = make_update_with_message("bonjour", user_id=OTHER_USER_ID)
    await mw(settings, session_factory)(
        handler, event, {"event_from_user": make_user(OTHER_USER_ID)}
    )
    async with session_factory() as session:
        assert await repo.get_user(session, OTHER_USER_ID) is None
        assert await repo.count_users(session) == 0
