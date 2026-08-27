"""/track: choose an exam goal (DELF B1, DELF B2, TCF) or stay general.

A dated exam is the strongest motivation a learner brings, and it changes what
practice should look like: the writing task takes the exam's format and length,
the correction weighs the exam's criteria, and the weekly grammar order follows
what that exam actually tests.
"""

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from frbot import tracks
from frbot.bot import render
from frbot.bot.telegram_utils import safe_answer, safe_edit_text
from frbot.db import repo
from frbot.db.models import User
from frbot.db.session import SessionFactory

logger = logging.getLogger(__name__)


def track_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t.title, callback_data=f"track:{t.slug}")]
            for t in tracks.TRACKS.values()
        ]
    )


def _describe(track: tracks.Track) -> str:
    lines = [f"🎓 <b>{render.esc(track.title)}</b>", render.esc(track.blurb)]
    if tracks.is_exam(track.slug):
        low, high = track.word_target
        lines.append("")
        lines.append(f"Письменные задания: {low}–{high} слов, формат экзамена.")
        lines.append("Грамматика недели идёт в порядке, который спрашивают на экзамене.")
    return "\n".join(lines)


async def cmd_track(message: Message, user: User) -> None:
    current = tracks.get(user.track)
    await message.answer(
        f"Сейчас: <b>{render.esc(current.title)}</b>\n\nВыбери цель:",
        reply_markup=track_kb(),
    )


async def on_track_chosen(
    query: CallbackQuery,
    user: User,
    session_factory: SessionFactory,
) -> None:
    slug = query.data.split(":", 1)[1]
    if slug not in tracks.TRACKS:
        await safe_answer(query, "Неизвестный трек.")
        return
    track = tracks.get(slug)
    async with session_factory() as session:
        await repo.set_user_track(session, user.id, slug)
        # An exam track implies the level it is pitched at; a learner preparing
        # for DELF B2 should not be getting A2 example sentences.
        if tracks.is_exam(slug) and (user.level or "B1") < track.level:
            await repo.set_user_level(session, user.id, track.level)
        await session.commit()
    logger.info("user %d chose track %s", user.id, slug)
    if isinstance(query.message, Message):
        await safe_edit_text(query.message, _describe(track))
        if tracks.is_exam(slug):
            await query.message.answer(
                "Попробуй прямо сейчас: /write — пришлю задание в формате экзамена."
            )
    await safe_answer(query)


def create_router() -> Router:
    router = Router(name="track")
    router.message.register(cmd_track, Command("track"))
    router.callback_query.register(on_track_chosen, F.data.startswith("track:"))
    return router
