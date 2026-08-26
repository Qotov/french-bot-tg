"""/settings: runtime editing of REMINDER_TIME, WRITING_TIME, DAILY_NEW_LIMIT,
SESSION_MAX. Values persist in app_settings and override .env; time changes
reschedule the corresponding APScheduler job.
"""

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from frbot.bot.keyboards import settings_kb
from frbot.config import TIME_RE, Settings
from frbot.db import repo
from frbot.db.models import User
from frbot.db.session import SessionFactory

logger = logging.getLogger(__name__)

TIME_KEYS = ("REMINDER_TIME", "WRITING_TIME")
INT_KEYS = ("DAILY_NEW_LIMIT", "SESSION_MAX")
EDITABLE_KEYS = (*TIME_KEYS, *INT_KEYS)

PROMPTS = {
    "REMINDER_TIME": "Пришли время напоминания в формате ЧЧ:ММ (например, 08:30).",
    "WRITING_TIME": "Пришли время письменного задания в формате ЧЧ:ММ (например, 19:00).",
    "DAILY_NEW_LIMIT": "Пришли максимум новых карточек в день (число, например 15).",
    "SESSION_MAX": "Пришли максимум карточек за сессию (число, например 30).",
}


class SettingsStates(StatesGroup):
    awaiting_value = State()


async def _current_values(
    session_factory: SessionFactory, settings: Settings, user_id: int
) -> dict[str, str]:
    async with session_factory() as session:
        cfg = await repo.get_effective_config(session, settings, user_id=user_id)
    return {
        "REMINDER_TIME": cfg.reminder_time,
        "WRITING_TIME": cfg.writing_time,
        "DAILY_NEW_LIMIT": str(cfg.daily_new_limit),
        "SESSION_MAX": str(cfg.session_max),
    }


async def cmd_settings(
    message: Message,
    state: FSMContext,
    user: User,
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    await state.clear()
    values = await _current_values(session_factory, settings, user.id)
    await message.answer(
        "⚙️ <b>Настройки</b> — нажми, чтобы изменить:", reply_markup=settings_kb(values)
    )


async def on_edit(query: CallbackQuery, state: FSMContext) -> None:
    key = query.data.split(":")[2]
    if key not in EDITABLE_KEYS:
        await query.answer("Неизвестная настройка.")
        return
    await state.set_state(SettingsStates.awaiting_value)
    await state.set_data({"key": key})
    if isinstance(query.message, Message):
        await query.message.answer(PROMPTS[key])
    await query.answer()


def _validate(key: str, raw: str) -> str | None:
    """Returns the normalized value, or None if invalid."""
    raw = raw.strip()
    if key in TIME_KEYS:
        return raw if TIME_RE.match(raw) else None
    try:
        number = int(raw)
    except ValueError:
        return None
    return str(number) if 1 <= number <= 500 else None


async def handle_value(
    message: Message,
    state: FSMContext,
    user: User,
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    data = await state.get_data()
    key = data.get("key")
    if key not in EDITABLE_KEYS:
        await state.clear()
        return
    value = _validate(key, message.text or "")
    if value is None:
        await message.answer(f"Не похоже на корректное значение. {PROMPTS[key]}")
        return

    async with session_factory() as session:
        await repo.set_user_setting(session, user_id=user.id, key=key, value=value)
        await session.commit()
    await state.clear()
    logger.info("user %d changed %s to %s", user.id, key, value)

    # Per-user delivery times are read by the minute-tick job on every run,
    # so a changed time takes effect immediately — nothing to reschedule.
    values = await _current_values(session_factory, settings, user.id)
    await message.answer(f"✅ {key} = {value}", reply_markup=settings_kb(values))


async def on_voice_while_awaiting(message: Message) -> None:
    await message.answer("Пришли значение текстом, пожалуйста (или /stop, чтобы выйти).")


def create_router() -> Router:
    router = Router(name="settings")
    router.message.register(cmd_settings, Command("settings"))
    router.callback_query.register(on_edit, F.data.startswith("settings:edit:"))
    router.message.register(
        handle_value, SettingsStates.awaiting_value, F.text, ~F.text.startswith("/")
    )
    router.message.register(on_voice_while_awaiting, SettingsStates.awaiting_value, F.voice)
    return router
