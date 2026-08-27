"""Admin-only commands for running the pilot: /invite, /users, /broadcast.

Every handler is gated on user.is_admin; non-admins get no reply at all, so the
commands are invisible to participants.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from frbot.bot import render
from frbot.config import Settings
from frbot.db import repo
from frbot.db.models import User
from frbot.db.session import SessionFactory

logger = logging.getLogger(__name__)

BROADCAST_RATE = 20  # messages per second, under Telegram's ~30/s cap


async def cmd_invite(
    message: Message,
    command: CommandObject,
    user: User,
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    if not user.is_admin:
        return
    args = (command.args or "").split()
    count = 1
    max_uses = 1
    if args and args[0].isdigit():
        count = min(int(args[0]), 20)
    if len(args) > 1 and args[1].isdigit():
        max_uses = min(int(args[1]), 50)

    bot_info = await message.bot.me()
    async with session_factory() as session:
        taken = await repo.count_users(session)
        invites = [
            await repo.create_invite(session, created_by=user.id, max_uses=max_uses)
            for _ in range(count)
        ]
        await session.commit()
        codes = [inv.code for inv in invites]

    lines = [f"🎟 Коды ({max_uses} использ. каждый), мест занято {taken}/{settings.max_users}:", ""]
    lines += [f"<code>https://t.me/{bot_info.username}?start={code}</code>" for code in codes]
    await message.answer("\n".join(lines))


async def cmd_users(
    message: Message,
    user: User,
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    if not user.is_admin:
        return
    now = datetime.now(UTC)
    week_ago = now - timedelta(days=7)
    async with session_factory() as session:
        users = await repo.list_users(session)
        rows = []
        active_7d = 0
        for u in users:
            days = await repo.count_active_days(
                session, user_id=u.id, since=week_ago, tz=settings.tz
            )
            cards = await repo.count_cards(session, user_id=u.id)
            last = await repo.last_activity_at(session, user_id=u.id)
            if days:
                active_7d += 1
            rows.append((u, days, cards, last))

    if not rows:
        await message.answer("Пока никто не зарегистрирован.")
        return

    lines = [
        f"👥 <b>{len(rows)}</b> из {settings.max_users} · активны на этой неделе: "
        f"<b>{active_7d}</b> ({round(100 * active_7d / len(rows))}%)",
        "",
    ]
    for u, days, cards, last in rows:
        who = f"@{u.username}" if u.username else (u.first_name or str(u.id))
        ago = "—" if last is None else f"{(now - last).days}д назад"
        flags = " 👑" if u.is_admin else ""
        lines.append(
            f"• {render.esc(who)}{flags} · {render.esc(u.level)} · "
            f"{cards} карт · {days}/7 дней · {ago}"
        )
    await message.answer(render.fit_lines(lines))


async def cmd_broadcast(
    message: Message,
    command: CommandObject,
    user: User,
    session_factory: SessionFactory,
) -> None:
    if not user.is_admin:
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("Использование: <code>/broadcast текст сообщения</code>")
        return
    async with session_factory() as session:
        users = await repo.list_active_users(session)

    sent = failed = 0
    for target in users:
        chat_id = target.chat_id or target.id
        try:
            await message.bot.send_message(chat_id, text)
            sent += 1
        except Exception:
            logger.warning("broadcast to %s failed", target.id)
            failed += 1
        await asyncio.sleep(1 / BROADCAST_RATE)
    await message.answer(f"📣 Отправлено: {sent}, не доставлено: {failed}")


def create_router() -> Router:
    router = Router(name="admin")
    router.message.register(cmd_invite, Command("invite"))
    router.message.register(cmd_users, Command("users"))
    router.message.register(cmd_broadcast, Command("broadcast"), F.text)
    return router
