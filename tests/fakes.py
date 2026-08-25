"""Fake aiogram bot: a session that records outgoing API calls instead of
performing HTTP requests, plus builders for incoming Telegram objects.
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import CallbackQuery, Chat, Message, Update, User

ALLOWED_USER_ID = 111_111
OTHER_USER_ID = 999_999


class RecordingSession(BaseSession):
    """Records every TelegramMethod and returns a plausible result."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self._message_id = 1000

    async def close(self) -> None:
        pass

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109 - aiogram BaseSession interface
    ) -> TelegramType:
        self.requests.append(method)
        return self._result_for(method)

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 - aiogram BaseSession interface
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        yield b""

    def _result_for(self, method: TelegramMethod[Any]) -> Any:
        name = type(method).__name__
        if name in {"SendMessage", "EditMessageText", "EditMessageReplyMarkup"}:
            self._message_id += 1
            chat_id = getattr(method, "chat_id", None) or ALLOWED_USER_ID
            return Message.model_construct(
                message_id=self._message_id,
                date=datetime.now(UTC),
                chat=Chat.model_construct(id=int(chat_id), type="private"),
                text=getattr(method, "text", None),
            )
        return True

    # -- helpers for assertions ------------------------------------------------

    def sent(self, method_name: str) -> list[TelegramMethod[Any]]:
        return [m for m in self.requests if type(m).__name__ == method_name]

    @property
    def sent_messages(self) -> list[TelegramMethod[Any]]:
        return self.sent("SendMessage")


def make_bot() -> Bot:
    return Bot(
        token="42:TEST-TOKEN",
        session=RecordingSession(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def make_user(user_id: int = ALLOWED_USER_ID) -> User:
    return User(id=user_id, is_bot=False, first_name="Test")


def make_message(
    text: str,
    user_id: int = ALLOWED_USER_ID,
    bot: Bot | None = None,
    message_id: int = 1,
) -> Message:
    message = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=make_user(user_id),
        text=text,
    )
    if bot is not None:
        message = message.as_(bot)
    return message


def make_callback_query(
    data: str,
    user_id: int = ALLOWED_USER_ID,
    bot: Bot | None = None,
    message: Message | None = None,
) -> CallbackQuery:
    if message is None:
        message = make_message("front", user_id=user_id, bot=bot, message_id=500)
    query = CallbackQuery(
        id="cbq-1",
        from_user=make_user(user_id),
        chat_instance="ci-1",
        message=message,
        data=data,
    )
    if bot is not None:
        query = query.as_(bot)
    return query


def make_update_with_message(text: str, user_id: int = ALLOWED_USER_ID) -> Update:
    return Update(update_id=1, message=make_message(text, user_id=user_id))
