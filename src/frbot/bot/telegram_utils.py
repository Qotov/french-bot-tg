"""Small Telegram helpers.

Editing a message can fail for benign reasons: identical content ("message is
not modified") or the message being too old to edit. Neither should crash a
handler flow, so safe_edit_text falls back to sending a new message.
"""

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)


async def safe_edit_text(
    message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        logger.warning("edit_text failed (%s); sending a new message", exc)
        await message.answer(text, reply_markup=reply_markup)


async def safe_clear_markup(message: Message) -> None:
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest as exc:
        logger.info("clearing reply markup failed: %s", exc)
