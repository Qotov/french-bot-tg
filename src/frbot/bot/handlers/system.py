"""/start (registration by invite), /help, /level, /feedback."""

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from frbot.bot import render
from frbot.bot.telegram_utils import safe_answer, safe_edit_text
from frbot.config import Settings
from frbot.db import repo
from frbot.db.models import LEVELS, User
from frbot.db.session import SessionFactory

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "🇫🇷 <b>frbot</b> — французский, каждый день понемногу\n\n"
    "Пришли слово или фразу (текстом или голосом 🎙) — я сделаю карточку.\n\n"
    "/review — повторение карточек\n"
    "/write — письменное задание дня (можно отвечать голосом)\n"
    "/talk — диалог: отвечаю по-французски и исправляю ошибки\n"
    "/topic — подборка слов по любой теме (например: /topic ресторан 10)\n"
    "/drill — грамматическая тема недели\n"
    "/stats — твой прогресс\n"
    "/level — уровень (A2 / B1 / B2)\n"
    "/settings — время напоминаний и лимиты\n"
    "/feedback — написать автору (я читаю всё)\n"
    "/stop — прервать текущую сессию\n"
    "/help — эта справка"
)

WELCOME_TEXT = (
    "🇫🇷 Добро пожаловать в бету!\n\n"
    "Как это работает: ты присылаешь слова, которые встретил, — я делаю из них "
    "карточки и напоминаю повторить в нужный момент. Вечером присылаю короткое "
    "задание на письмо, разбираю ошибки — и каждая ошибка тоже становится карточкой.\n\n"
    "Сначала выбери свой уровень:"
)

LEVEL_HINTS = {
    "A2": "простые фразы, настоящее и прошедшее время",
    "B1": "свободно на бытовые темы, читаю новости со словарём",
    "B2": "уверенно говорю, хочу точности и нюансов",
}

FIRST_STEPS_TEXT = (
    "Отлично! Три шага, чтобы начать прямо сейчас:\n\n"
    "1️⃣ Пришли мне любое французское слово, которое хочешь запомнить "
    "(или скажи голосом).\n"
    "2️⃣ Набери /topic и тему — соберу подборку слов, выберешь нужные.\n"
    "3️⃣ Вечером пришлю задание на письмо. Отвечай текстом или голосом.\n\n"
    "Дальше просто: 10–15 минут в день. /help — если что-то забудешь."
)

NEED_INVITE_TEXT = (
    "Это закрытая бета. Чтобы войти, пришли код приглашения:\n"
    "<code>/start ТВОЙКОД</code>"
)
BAD_INVITE_TEXT = "Код не подошёл — возможно, он уже использован. Проверь ещё раз."
FULL_TEXT = "Сейчас все места в бете заняты. Напиши автору, чтобы попасть в следующий набор."
FEEDBACK_ASK = (
    "Напиши, что работает, что мешает, чего не хватает — одним сообщением. "
    "Я читаю всё лично."
)
FEEDBACK_THANKS = "Спасибо! Записал и прочитаю сегодня же."


def level_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{lvl} — {LEVEL_HINTS[lvl]}", callback_data=f"level:{lvl}")]
            for lvl in LEVELS
        ]
    )


async def cmd_start(
    message: Message,
    command: CommandObject,
    user: User | None,
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    if user is not None:
        async with session_factory() as session:
            row = await repo.get_user(session, user.id)
            if row is not None and row.chat_id != message.chat.id:
                row.chat_id = message.chat.id
                await session.commit()
        await message.answer(HELP_TEXT)
        return

    code = (command.args or "").strip()
    if not code:
        await message.answer(NEED_INVITE_TEXT)
        return

    async with session_factory() as session:
        if await repo.count_users(session) >= settings.max_users:
            await message.answer(FULL_TEXT)
            return
        invite = await repo.redeem_invite(session, code)
        if invite is None:
            await message.answer(BAD_INVITE_TEXT)
            return
        await repo.create_user(
            session,
            user_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            chat_id=message.chat.id,
            invite_code=invite.code,
        )
        await session.commit()

    logger.info("user %s joined with invite %s", message.from_user.id, code)
    await message.answer(WELCOME_TEXT, reply_markup=level_kb())


async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


async def cmd_level(message: Message, user: User) -> None:
    await message.answer(
        f"Твой уровень: <b>{render.esc(user.level)}</b>. Изменить:",
        reply_markup=level_kb(),
    )


async def on_level_chosen(
    query: CallbackQuery,
    user: User,
    session_factory: SessionFactory,
) -> None:
    level = query.data.split(":")[1]
    async with session_factory() as session:
        ok = await repo.set_user_level(session, user.id, level)
        await session.commit()
    if not ok:
        await safe_answer(query, "Неизвестный уровень.")
        return
    if isinstance(query.message, Message):
        await safe_edit_text(query.message, f"Уровень: <b>{render.esc(level)}</b> ✅")
        await query.message.answer(FIRST_STEPS_TEXT)
    await safe_answer(query)


class FeedbackStates:
    """Feedback uses a plain flag in FSM data rather than its own state group,
    so it can be started from anywhere without clobbering an active session."""


async def cmd_feedback(message: Message, state: FSMContext) -> None:
    await state.update_data(awaiting_feedback=True)
    await message.answer(FEEDBACK_ASK)


async def handle_feedback(
    message: Message,
    state: FSMContext,
    user: User,
    settings: Settings,
) -> None:
    await state.update_data(awaiting_feedback=False)
    who = f"@{user.username}" if user.username else (user.first_name or str(user.id))
    logger.info("feedback from %s: %s", user.id, (message.text or "")[:200])
    try:
        await message.bot.send_message(
            settings.admin_user_id,
            f"📮 <b>Отзыв</b> от {render.esc(who)} (<code>{user.id}</code>, "
            f"{render.esc(user.level)}):\n\n{render.esc(message.text or '')}",
        )
    except Exception:
        logger.exception("failed to forward feedback to admin")
    await message.answer(FEEDBACK_THANKS)


def create_router() -> Router:
    router = Router(name="system")
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_level, Command("level"))
    router.message.register(cmd_feedback, Command("feedback"))
    router.callback_query.register(on_level_chosen, F.data.startswith("level:"))
    return router


def create_feedback_router() -> Router:
    """Registered before every other text handler: while the feedback flag is
    set, the next plain message is the feedback, whatever else is running."""
    router = Router(name="feedback")

    async def _flagged(message: Message, state: FSMContext) -> bool:
        return bool((await state.get_data()).get("awaiting_feedback"))

    router.message.register(handle_feedback, _flagged, F.text, ~F.text.startswith("/"))
    return router
