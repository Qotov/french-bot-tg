"""/start and /help."""

import logging

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from frbot.db import repo
from frbot.db.session import SessionFactory

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "🇫🇷 <b>frbot</b> — французский, B1 → B2\n\n"
    "Пришли слово или фразу (текстом или голосом 🎙) — я сделаю карточку.\n\n"
    "/review — повторение карточек (FSRS)\n"
    "/write — письменное задание дня (можно отвечать голосом)\n"
    "/talk — диалог: я отвечаю по-французски и исправляю ошибки\n"
    "/topic — подборка слов по любой теме (например: /topic ресторан 10)\n"
    "/drill — грамматическая тема недели\n"
    "/stats — статистика\n"
    "/settings — настройки времени и лимитов\n"
    "/stop — завершить диалог\n"
    "/help — эта справка"
)


async def cmd_start(message: Message, session_factory: SessionFactory) -> None:
    async with session_factory() as session:
        await repo.set_setting(session, repo.CHAT_ID_KEY, str(message.chat.id))
        await session.commit()
    logger.info("stored chat id %s", message.chat.id)
    await message.answer(HELP_TEXT)


async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


def create_router() -> Router:
    router = Router(name="system")
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_help, Command("help"))
    return router
