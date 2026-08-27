from datetime import UTC, datetime, timedelta

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, select

from frbot.bot import render
from frbot.bot.handlers.write import FAIL_TEXT, WriteStates, cmd_write, handle_answer
from frbot.db import repo
from frbot.db.models import Card, CardState, Writing
from frbot.llm.client import LLMError
from frbot.llm.schemas import WritingCorrection
from frbot.srs.queue import build_queue
from frbot.srs.scheduler import SrsScheduler
from tests.fakes import ALLOWED_USER_ID, add_vocab_card, load_fixture_json, make_message

ANSWER = "je suis allé au marché depuis hier. j'ai acheté les pommes."


def correction() -> WritingCorrection:
    return WritingCorrection.model_validate(load_fixture_json("correction_valid.json"))


def make_state(bot) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID),
    )


def srs() -> SrsScheduler:
    return SrsScheduler(desired_retention=0.9)


async def error_card_count(session_factory) -> int:
    async with session_factory() as session:
        stmt = select(func.count(Card.id)).where(Card.kind == "error")
        return (await session.execute(stmt)).scalar_one()


async def run_write_and_answer(fake_bot, session_factory, settings, llm, user, usage) -> FSMContext:
    state = make_state(fake_bot)
    await cmd_write(make_message("/write", bot=fake_bot), state, user, session_factory, settings)
    await handle_answer(
        make_message(ANSWER, bot=fake_bot),
        state,
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
    )
    return state


async def test_cmd_write_prefers_due_words(fake_bot, session_factory, settings, user):
    now = datetime.now(UTC)
    for lemma in ("marché", "boulangerie", "quotidien"):
        await add_vocab_card(
            session_factory, lemma, reviewed_days_ago=2, due=now - timedelta(hours=1)
        )
    await add_vocab_card(
        session_factory, "lointain", reviewed_days_ago=1, due=now + timedelta(days=30)
    )

    state = make_state(fake_bot)
    await cmd_write(make_message("/write", bot=fake_bot), state, user, session_factory, settings)

    sent = fake_bot.session.sent_messages[0].text
    assert "Задание" in sent
    for lemma in ("marché", "boulangerie", "quotidien"):
        assert lemma in sent
    assert "lointain" not in sent
    assert await state.get_state() == WriteStates.awaiting_answer.state

    async with session_factory() as session:
        writing = (await session.execute(select(Writing))).scalar_one()
    assert writing.answer is None
    assert "marché" in writing.prompt


async def test_cmd_write_falls_back_to_recent_captures(fake_bot, session_factory, settings, user):
    now = datetime.now(UTC)
    for lemma in ("ancien", "récent"):
        await add_vocab_card(
            session_factory, lemma, reviewed_days_ago=1, due=now + timedelta(days=30)
        )
    state = make_state(fake_bot)
    await cmd_write(make_message("/write", bot=fake_bot), state, user, session_factory, settings)
    sent = fake_bot.session.sent_messages[0].text
    assert "récent" in sent
    assert "ancien" in sent


async def test_answer_renders_correction_and_creates_error_cards(
    fake_bot, session_factory, settings, user, usage
):
    from tests.fakes import FakeLLM

    llm = FakeLLM(correct_results=[correction()])
    state = await run_write_and_answer(fake_bot, session_factory, settings, llm, user, usage)

    # The correction call received the prompt and the answer.
    prompt, answer = llm.correct_calls[0]
    assert answer == ANSWER
    assert prompt  # the stored writing prompt

    reply = fake_bot.session.sent_messages[-1].text
    assert "Исправлено" in reply
    assert "Je suis allé au marché hier" in reply
    assert "1. ❌ depuis hier → ✅ <b>hier</b>" in reply
    assert "«depuis»" in reply  # RU explanation present
    assert "Новых карточек из ошибок: 2" in reply

    assert await error_card_count(session_factory) == 2
    async with session_factory() as session:
        card = (
            (await session.execute(select(Card).where(Card.kind == "error").order_by(Card.id)))
            .scalars()
            .first()
        )
        writing = (await session.execute(select(Writing))).scalar_one()
    assert card.state == CardState.new.value
    assert card.error_meta["type"] == "preposition"
    assert card.error_meta["explanation_ru"]
    assert writing.answer == ANSWER
    assert writing.corrections["corrected_text"].startswith("Je suis allé")
    assert await state.get_state() is None


async def test_error_cards_deduped_across_runs(fake_bot, session_factory, settings, user, usage):
    from tests.fakes import FakeLLM

    llm = FakeLLM(correct_results=[correction(), correction()])
    await run_write_and_answer(fake_bot, session_factory, settings, llm, user, usage)
    await run_write_and_answer(fake_bot, session_factory, settings, llm, user, usage)

    assert await error_card_count(session_factory) == 2  # no duplicates
    second_reply = fake_bot.session.sent_messages[-1].text
    assert "Новых карточек" not in second_reply


async def test_daily_cap_limits_error_cards(fake_bot, session_factory, settings, user, usage):
    from tests.fakes import FakeLLM

    seven = WritingCorrection.model_validate(load_fixture_json("correction_seven_errors.json"))
    llm = FakeLLM(correct_results=[seven])
    await run_write_and_answer(fake_bot, session_factory, settings, llm, user, usage)

    assert await error_card_count(session_factory) == repo.ERROR_CARDS_DAILY_CAP
    reply = fake_bot.session.sent_messages[-1].text
    assert f"Новых карточек из ошибок: {repo.ERROR_CARDS_DAILY_CAP}" in reply


async def test_llm_failure_keeps_state_for_retry(fake_bot, session_factory, settings, user, usage):
    from tests.fakes import FakeLLM

    llm = FakeLLM(correct_results=[LLMError("down")])
    state = make_state(fake_bot)
    await cmd_write(make_message("/write", bot=fake_bot), state, user, session_factory, settings)
    await handle_answer(
        make_message(ANSWER, bot=fake_bot),
        state,
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
    )

    assert fake_bot.session.sent_messages[-1].text == FAIL_TEXT
    assert await state.get_state() == WriteStates.awaiting_answer.state
    assert await error_card_count(session_factory) == 0
    async with session_factory() as session:
        writing = (await session.execute(select(Writing))).scalar_one()
    assert writing.answer is None


async def test_overlong_answer_rejected_without_llm_call(
    fake_bot, session_factory, settings, user, usage
):
    from frbot.bot.handlers.write import ANSWER_MAX_LEN
    from tests.fakes import FakeLLM

    llm = FakeLLM(correct_results=[correction()])
    state = make_state(fake_bot)
    await cmd_write(make_message("/write", bot=fake_bot), state, user, session_factory, settings)
    await handle_answer(
        make_message("x" * (ANSWER_MAX_LEN + 1), bot=fake_bot),
        state,
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
    )
    assert llm.correct_calls == []
    assert "Слишком длинно" in fake_bot.session.sent_messages[-1].text
    assert await state.get_state() == WriteStates.awaiting_answer.state


async def test_no_errors_congratulates(fake_bot, session_factory, settings, user, usage):
    from tests.fakes import FakeLLM

    perfect = WritingCorrection(
        corrected_text="Tout est parfait.", errors=[], comment_ru="Отличная работа."
    )
    llm = FakeLLM(correct_results=[perfect])
    await run_write_and_answer(fake_bot, session_factory, settings, llm, user, usage)
    reply = fake_bot.session.sent_messages[-1].text
    assert "ошибок нет" in reply
    assert await error_card_count(session_factory) == 0


async def test_error_card_appears_in_next_review_queue(
    fake_bot, session_factory, settings, user, usage
):
    from tests.fakes import FakeLLM

    llm = FakeLLM(correct_results=[correction()])
    await run_write_and_answer(fake_bot, session_factory, settings, llm, user, usage)

    async with session_factory() as session:
        queue = await build_queue(
            session,
            user_id=ALLOWED_USER_ID,
            now=datetime.now(UTC),
            tz=settings.tz,
            session_max=30,
            daily_new_limit=15,
        )
        error_ids = (
            (await session.execute(select(Card.id).where(Card.kind == "error"))).scalars().all()
        )
    assert error_ids
    assert set(error_ids) <= set(queue.card_ids)


async def test_error_card_front_gaps_the_corrected_span(
    fake_bot, session_factory, settings, user, usage
):
    from tests.fakes import FakeLLM

    llm = FakeLLM(correct_results=[correction()])
    await run_write_and_answer(fake_bot, session_factory, settings, llm, user, usage)
    async with session_factory() as session:
        card = (
            (await session.execute(select(Card).where(Card.kind == "error").order_by(Card.id)))
            .scalars()
            .first()
        )

    front = render.card_front(card)
    assert "___" in front
    assert "hier" not in front.split("___")[1][:10] if "___" in front else True
    back = render.card_back(card)
    assert "hier" in back
    assert card.error_meta["explanation_ru"][:10] in back
