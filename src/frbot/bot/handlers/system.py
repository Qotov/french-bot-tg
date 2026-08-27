"""/start (registration by invite), /help, /level, /feedback."""

import asyncio
import logging
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from frbot.bot import render
from frbot.bot.alerts import AdminAlerter
from frbot.bot.handlers.placement import cmd_placement
from frbot.bot.handlers.topic import build_starter_deck
from frbot.bot.onboarding import (
    BUILDING_DECK_TEXT,
    DECK_FAILED_TEXT,
    FIRST_STEPS_TEXT,
    STARTER_DECK_TIMEOUT,
)
from frbot.bot.telegram_utils import safe_answer, safe_edit_text
from frbot.config import Settings
from frbot.db import repo
from frbot.db.models import LEVELS, User
from frbot.db.session import SessionFactory
from frbot.llm.client import LLMClient
from frbot.srs.scheduler import SrsScheduler
from frbot.usage import UsageLimiter

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
    "/cards — моя колода: посмотреть, поставить на паузу, удалить\n"
    "/level — уровень (A2 / B1 / B2)\n"
    "/placement — тест на уровень (3 минуты)\n"
    "/track — цель: DELF B1 / DELF B2 / TCF\n"
    "/settings — время напоминаний и лимиты\n"
    "/feedback — написать автору (я читаю всё)\n"
    "/stop — прервать текущую сессию\n"
    "/delete_me — удалить все свои данные\n"
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


NEED_INVITE_TEXT = (
    "Это закрытая бета. Чтобы войти, пришли код приглашения:\n<code>/start ТВОЙКОД</code>"
)
BAD_INVITE_TEXT = "Код не подошёл — возможно, он уже использован. Проверь ещё раз."
FULL_TEXT = "Сейчас все места в бете заняты. Напиши автору, чтобы попасть в следующий набор."
FEEDBACK_ASK = (
    "Напиши, что работает, что мешает, чего не хватает — одним сообщением. Я читаю всё лично."
)
FEEDBACK_THANKS = "Спасибо! Записал и прочитаю сегодня же."


def level_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{lvl} — {LEVEL_HINTS[lvl]}", callback_data=f"level:{lvl}")]
            for lvl in LEVELS
        ]
        + [[InlineKeyboardButton(text="🎯 Не знаю — пройти тест", callback_data="level:test")]]
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
            # Only ever deliver to the private chat (the middleware already
            # rejects other chat types; this is the second belt).
            private = message.chat.type == "private"
            if row is not None and private and row.chat_id != message.chat.id:
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
    state: FSMContext,
    user: User,
    session_factory: SessionFactory,
    llm: LLMClient,
    srs: SrsScheduler,
    settings: Settings,
    usage: UsageLimiter,
    alerter: AdminAlerter,
) -> None:
    level = query.data.split(":")[1]
    if level == "test":
        # They would rather be measured than guess — the better answer.
        await safe_answer(query)
        if isinstance(query.message, Message):
            await safe_edit_text(query.message, "🎯 Определим уровень тестом.")
            await cmd_placement(query.message, state, onboarding=True)
        return
    async with session_factory() as session:
        ok = await repo.set_user_level(session, user.id, level)
        await session.commit()
        user = await repo.get_user(session, user.id)
    if not ok:
        await safe_answer(query, "Неизвестный уровень.")
        return
    await safe_answer(query)
    if not isinstance(query.message, Message):
        return

    await safe_edit_text(query.message, f"Уровень: <b>{render.esc(level)}</b> ✅")

    # Day one must not be an empty deck — build one before saying anything else.
    async with session_factory() as session:
        already = await repo.count_cards(session, user_id=user.id)
    if already == 0:
        await query.message.answer(BUILDING_DECK_TEXT)
        await query.message.bot.send_chat_action(query.message.chat.id, ChatAction.TYPING)
        # Bounded: this runs while the per-user lock is held, so it must not be
        # able to block the participant's own messages for minutes.
        try:
            async with asyncio.timeout(STARTER_DECK_TIMEOUT):
                created = await build_starter_deck(
                    user,
                    session_factory,
                    llm,
                    srs,
                    settings,
                    usage,
                    alerter,
                    query.message.bot,
                )
        except TimeoutError:
            logger.warning("starter deck timed out for user %d", user.id)
            created = []
        if created:
            preview = ", ".join(render.esc(lemma) for lemma in created[:6])
            await query.message.answer(
                f"🎁 Готово — {len(created)} карточек для начала: {preview}…\n\n"
                f"Попробуй прямо сейчас: /review"
            )
        else:
            await query.message.answer(DECK_FAILED_TEXT)

    await query.message.answer(FIRST_STEPS_TEXT)


BUSY_TEXT = "Сейчас идёт другая сессия. Заверши её или набери /stop, потом /feedback."


async def cmd_feedback(message: Message, state: FSMContext) -> None:
    # Feedback is a flag rather than its own state so it survives anywhere —
    # but it must not be armed while another flow is waiting for a message,
    # or it would swallow that flow's answer.
    if await state.get_state() is not None:
        await message.answer(BUSY_TEXT)
        return
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


DELETE_CONFIRM_TEXT = (
    "⚠️ Это удалит <b>всё</b>: карточки, историю повторений, тексты и сам аккаунт. "
    "Отменить будет нельзя.\n\n"
    "Если уверен(а), пришли одним сообщением: <code>УДАЛИТЬ</code>\n"
    "Передумал(а) — просто напиши что угодно другое или /stop."
)
DELETE_CONFIRM_TTL = 300  # seconds; after that the armed confirmation lapses
DELETE_WORD = "УДАЛИТЬ"
DELETE_DONE_TEXT = (
    "Готово — всё удалено: {cards} карточек, {reviews} повторений, {writings} текстов.\n"
    "Спасибо, что попробовал(а). Вернуться можно по новому приглашению."
)
DELETE_CANCELLED_TEXT = "Отменил — ничего не удалено."


class DeleteStates(StatesGroup):
    confirming = State()


async def cmd_delete_me(message: Message, state: FSMContext) -> None:
    await state.set_state(DeleteStates.confirming)
    await state.update_data(delete_armed_at=datetime.now(UTC).timestamp())
    await message.answer(DELETE_CONFIRM_TEXT)


async def handle_delete_confirmation(
    message: Message,
    state: FSMContext,
    user: User,
    session_factory: SessionFactory,
) -> None:
    armed_at = (await state.get_data()).get("delete_armed_at", 0)
    await state.clear()
    expired = datetime.now(UTC).timestamp() - armed_at > DELETE_CONFIRM_TTL
    if expired or (message.text or "").strip().upper() != DELETE_WORD:
        # Anything that is not the exact word cancels — and a confirmation left
        # armed for minutes must not delete an account because the next message
        # happened to be that word.
        await message.answer(DELETE_CANCELLED_TEXT)
        return
    async with session_factory() as session:
        counts = await repo.delete_user_data(session, user.id)
        await session.commit()
    logger.info("user %d deleted their account", user.id)
    await message.answer(DELETE_DONE_TEXT.format(**counts))


def create_router() -> Router:
    router = Router(name="system")
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_level, Command("level"))
    router.message.register(cmd_feedback, Command("feedback"))
    router.message.register(cmd_delete_me, Command("delete_me"))
    router.message.register(
        handle_delete_confirmation, DeleteStates.confirming, F.text, ~F.text.startswith("/")
    )
    router.callback_query.register(on_level_chosen, F.data.startswith("level:"))
    return router


def create_feedback_router() -> Router:
    """Registered before every other text handler: while the feedback flag is
    set, the next plain message is the feedback, whatever else is running."""
    router = Router(name="feedback")

    async def _flagged(message: Message, state: FSMContext) -> bool:
        # Only when no other flow owns the conversation (belt and braces with
        # the check in cmd_feedback).
        if await state.get_state() is not None:
            return False
        return bool((await state.get_data()).get("awaiting_feedback"))

    router.message.register(handle_feedback, _flagged, F.text, ~F.text.startswith("/"))
    return router
