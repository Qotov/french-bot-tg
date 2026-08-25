"""Whitelist middleware: drop every update that is not from the allowed user."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update, User

logger = logging.getLogger(__name__)


def _extract_user(event: TelegramObject) -> User | None:
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


class WhitelistMiddleware(BaseMiddleware):
    def __init__(self, allowed_user_id: int) -> None:
        self.allowed_user_id = allowed_user_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user") or _extract_user(event)
        if user is None or user.id != self.allowed_user_id:
            logger.info("dropped update from user %s", user.id if user else "<unknown>")
            return None
        return await handler(event, data)
