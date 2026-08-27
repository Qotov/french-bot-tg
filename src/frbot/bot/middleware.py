"""Access control for the pilot.

Registered, active users pass through with their `User` row injected into the
handler data. Everyone else is stopped here — except /start, which is the one
entry point that can register someone holding a valid invite code.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject, Update
from aiogram.types import User as TgUser

from frbot.config import Settings
from frbot.db import repo
from frbot.db.session import SessionFactory

logger = logging.getLogger(__name__)

NOT_INVITED_TEXT = (
    "Это закрытая бета французского бота. Вход по приглашению:\n<code>/start КОД</code>"
)


def _extract_user(event: TelegramObject) -> TgUser | None:
    if isinstance(event, Update):
        nested = (
            event.message
            or event.edited_message
            or event.callback_query
            or event.inline_query
            or event.my_chat_member
            or event.chat_member
        )
        return getattr(nested, "from_user", None)
    return getattr(event, "from_user", None)


def _is_start_command(event: TelegramObject) -> bool:
    message = event.message if isinstance(event, Update) else None
    text = (getattr(message, "text", None) or "").strip()
    return text.startswith("/start")


def _is_private(event: TelegramObject) -> bool:
    """Everything the bot does is one-to-one tutoring.

    A participant's deliveries are addressed to a stored chat id, so allowing a
    flow to be driven from a group would let one /start there redirect that
    person's cards, prompts and stats into a room other people can read.
    """
    if isinstance(event, Update):
        message = event.message or event.edited_message
        if message is not None:
            return message.chat.type == "private"
        query = event.callback_query
        if query is not None and query.message is not None:
            return query.message.chat.type == "private"
        # Inline queries and chat-member updates carry no chat to act on.
        return event.callback_query is None
    chat = getattr(event, "chat", None)
    return chat is None or chat.type == "private"


class AuthMiddleware(BaseMiddleware):
    """Resolves the sender to a pilot participant.

    The bot owner (settings.admin_user_id) is auto-registered on first contact,
    so a fresh deployment is usable without an invite.
    """

    def __init__(self, settings: Settings, session_factory: SessionFactory) -> None:
        self.settings = settings
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user") or _extract_user(event)
        if tg_user is None or tg_user.is_bot:
            return None
        if not _is_private(event):
            logger.info("ignored non-private update from user %s", tg_user.id)
            return None

        async with self.session_factory() as session:
            user = await repo.get_user(session, tg_user.id)
            if user is None and tg_user.id == self.settings.admin_user_id:
                user = await repo.create_user(
                    session,
                    user_id=tg_user.id,
                    username=tg_user.username,
                    first_name=tg_user.first_name,
                    chat_id=tg_user.id,
                    invite_code=None,
                    is_admin=True,
                )
                await session.commit()

        if user is None:
            # Unregistered: only /start can let them in (with a valid code).
            if _is_start_command(event):
                data["user"] = None
                return await handler(event, data)
            logger.info("blocked update from unregistered user %s", tg_user.id)
            return None

        if not user.active:
            logger.info("blocked update from deactivated user %s", tg_user.id)
            return None

        data["user"] = user
        data["user_id"] = user.id
        return await handler(event, data)


async def reply_not_invited(event: TelegramObject) -> None:
    message = event.message if isinstance(event, Update) else event
    if isinstance(message, Message):
        await message.answer(NOT_INVITED_TEXT)
