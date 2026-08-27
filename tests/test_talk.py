from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, select

from frbot.bot.handlers.talk import (
    FAIL_TEXT,
    HISTORY_MAX,
    TalkStates,
    cmd_stop,
    cmd_talk,
    handle_text_turn,
    handle_voice_turn,
)
from frbot.bot.telegram_utils import VOICE_MAX_DURATION
from frbot.db.models import Card
from frbot.llm.client import LLMError
from frbot.llm.schemas import TalkTurn
from frbot.srs.scheduler import SrsScheduler
from tests.fakes import (
    ALLOWED_USER_ID,
    FakeLLM,
    add_vocab_card,
    make_message,
    make_voice_message,
)

OPENER = TalkTurn(reply_fr="Salut ! Qu'est-ce que tu as mangé ce matin ?")

TURN_WITH_ERROR = TalkTurn(
    corrected_fr="Je suis allé au marché hier.",
    errors=[
        {
            "original": "j'ai allé",
            "corrected": "je suis allé",
            "type": "auxiliary",
            "explanation_ru": "aller спрягается с être.",
        }
    ],
    reply_fr="Ah, le marché ! Qu'est-ce que tu y as acheté ?",
)

CLEAN_TURN = TalkTurn(reply_fr="Très bien ! Et demain, quels sont tes plans ?")

VOICE_TURN = TalkTurn(
    transcript="je vais au boulangerie",
    corrected_fr="Je vais à la boulangerie.",
    errors=[
        {
            "original": "au boulangerie",
            "corrected": "à la boulangerie",
            "type": "gender",
            "explanation_ru": "boulangerie — женский род.",
        }
    ],
    reply_fr="Miam ! Qu'est-ce que tu vas y acheter ?",
)


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


async def start_talk(fake_bot, session_factory, settings, llm, user, usage, alerter):
    state = make_state(fake_bot)
    await cmd_talk(
        make_message("/talk", bot=fake_bot),
        state,
        user,
        session_factory,
        llm,
        settings,
        usage,
        alerter,
    )
    return state


async def test_talk_opens_with_french_question(
    fake_bot, session_factory, settings, user, usage, alerter
):
    await add_vocab_card(session_factory, "marché")
    llm = FakeLLM(talk_results=[OPENER])
    state = await start_talk(fake_bot, session_factory, settings, llm, user, usage, alerter)

    assert llm.talk_calls[0]["kind"] == "open"
    assert "marché" in llm.talk_calls[0]["lemmas"]
    assert await state.get_state() == TalkStates.talking.state
    sent = fake_bot.session.sent_messages[-1].text
    assert "Диалог начат" in sent
    assert "Qu'est-ce que tu as mangé" in sent


async def test_text_turn_corrects_and_replies(
    fake_bot, session_factory, settings, user, usage, alerter
):
    llm = FakeLLM(talk_results=[OPENER, TURN_WITH_ERROR])
    state = await start_talk(fake_bot, session_factory, settings, llm, user, usage, alerter)

    await handle_text_turn(
        make_message("hier j'ai allé au marché", bot=fake_bot),
        state,
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
        alerter,
    )
    turn_call = llm.talk_calls[-1]
    assert turn_call["text"] == "hier j'ai allé au marché"
    assert "Tuteur:" in turn_call["history"]

    reply = fake_bot.session.sent_messages[-1].text
    assert "❌ j'ai allé" in reply
    assert "✅ <b>je suis allé</b>" in reply
    assert "спрягается" in reply
    assert "Qu'est-ce que tu y as acheté" in reply
    assert "Карточек из ошибок: 1" in reply

    assert await error_card_count(session_factory) == 1
    async with session_factory() as session:
        card = (await session.execute(select(Card).where(Card.kind == "error"))).scalars().one()
    assert card.text == "Je suis allé au marché hier."
    assert card.error_meta["front"]  # gapped front stored

    history = (await state.get_data())["history"]
    assert history[-2] == "Élève: hier j'ai allé au marché"
    assert history[-1].startswith("Tuteur: Ah, le marché")


async def test_clean_turn_has_no_corrections_block(
    fake_bot, session_factory, settings, user, usage, alerter
):
    llm = FakeLLM(talk_results=[OPENER, CLEAN_TURN])
    state = await start_talk(fake_bot, session_factory, settings, llm, user, usage, alerter)
    await handle_text_turn(
        make_message("Je vais très bien, merci.", bot=fake_bot),
        state,
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
        alerter,
    )
    reply = fake_bot.session.sent_messages[-1].text
    assert "❌" not in reply
    assert "quels sont tes plans" in reply
    assert await error_card_count(session_factory) == 0


async def test_voice_turn_shows_transcript_and_corrects(
    fake_bot, session_factory, settings, user, usage, alerter
):
    llm = FakeLLM(talk_results=[OPENER, VOICE_TURN])
    state = await start_talk(fake_bot, session_factory, settings, llm, user, usage, alerter)
    await handle_voice_turn(
        make_voice_message(bot=fake_bot),
        state,
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
        alerter,
    )
    assert llm.talk_calls[-1]["audio"] is True
    assert llm.talk_calls[-1]["text"] is None
    reply = fake_bot.session.sent_messages[-1].text
    assert "🎙" in reply
    assert "je vais au boulangerie" in reply
    assert "à la boulangerie" in reply
    assert await error_card_count(session_factory) == 1
    history = (await state.get_data())["history"]
    assert history[-2] == "Élève: je vais au boulangerie"


async def test_too_long_voice_rejected(fake_bot, session_factory, settings, user, usage, alerter):
    llm = FakeLLM(talk_results=[OPENER])
    state = await start_talk(fake_bot, session_factory, settings, llm, user, usage, alerter)
    await handle_voice_turn(
        make_voice_message(duration=VOICE_MAX_DURATION + 1, bot=fake_bot),
        state,
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
        alerter,
    )
    assert "короче" in fake_bot.session.sent_messages[-1].text
    assert len(llm.talk_calls) == 1  # only the opener


async def test_history_is_trimmed(fake_bot, session_factory, settings, user, usage, alerter):
    llm = FakeLLM(talk_results=[OPENER] + [CLEAN_TURN] * 20)
    state = await start_talk(fake_bot, session_factory, settings, llm, user, usage, alerter)
    for i in range(10):
        await handle_text_turn(
            make_message(f"Message numéro {i}.", bot=fake_bot),
            state,
            user,
            session_factory,
            llm,
            srs(),
            settings,
            usage,
            alerter,
        )
    history = (await state.get_data())["history"]
    assert len(history) == HISTORY_MAX


async def test_turn_failure_keeps_session(
    fake_bot, session_factory, settings, user, usage, alerter
):
    llm = FakeLLM(talk_results=[OPENER, LLMError("down"), CLEAN_TURN])
    state = await start_talk(fake_bot, session_factory, settings, llm, user, usage, alerter)
    await handle_text_turn(
        make_message("Bonjour", bot=fake_bot),
        state,
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
        alerter,
    )
    assert fake_bot.session.sent_messages[-1].text == FAIL_TEXT
    assert await state.get_state() == TalkStates.talking.state
    # Retry works.
    await handle_text_turn(
        make_message("Bonjour", bot=fake_bot),
        state,
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
        alerter,
    )
    assert "quels sont tes plans" in fake_bot.session.sent_messages[-1].text


async def test_stop_ends_dialogue(fake_bot, session_factory, settings, user, usage, alerter):
    llm = FakeLLM(talk_results=[OPENER])
    state = await start_talk(fake_bot, session_factory, settings, llm, user, usage, alerter)
    await cmd_stop(make_message("/stop", bot=fake_bot), state)
    assert await state.get_state() is None
    assert "завершён" in fake_bot.session.sent_messages[-1].text


async def test_stop_outside_dialogue(fake_bot):
    state = make_state(fake_bot)
    await cmd_stop(make_message("/stop", bot=fake_bot), state)
    assert "нет активной сессии" in fake_bot.session.sent_messages[-1].text


async def test_stop_cancels_any_active_flow(fake_bot):
    from frbot.bot.handlers.topic import TopicStates

    state = make_state(fake_bot)
    await state.set_state(TopicStates.choosing)
    await cmd_stop(make_message("/stop", bot=fake_bot), state)
    assert await state.get_state() is None
    assert "прервана" in fake_bot.session.sent_messages[-1].text


async def test_overlong_text_turn_rejected(
    fake_bot, session_factory, settings, user, usage, alerter
):
    from frbot.bot.handlers.talk import TURN_MAX_LEN

    llm = FakeLLM(talk_results=[OPENER])
    state = await start_talk(fake_bot, session_factory, settings, llm, user, usage, alerter)
    await handle_text_turn(
        make_message("x" * (TURN_MAX_LEN + 1), bot=fake_bot),
        state,
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
        alerter,
    )
    assert "Слишком длинно" in fake_bot.session.sent_messages[-1].text
    assert len(llm.talk_calls) == 1  # only the opener
    assert await state.get_state() == TalkStates.talking.state


async def test_turn_reply_never_exceeds_telegram_limit(
    fake_bot, session_factory, settings, user, usage, alerter
):
    monster = TalkTurn(
        transcript="mot " * 500,
        corrected_fr="Phrase corrigée.",
        errors=[
            {
                "original": "x" * 400,
                "corrected": "y" * 400,
                "type": "other",
                "explanation_ru": "объяснение " * 40,
            }
        ]
        * 5,
        reply_fr="réponse " * 500,
    )
    llm = FakeLLM(talk_results=[OPENER, monster])
    state = await start_talk(fake_bot, session_factory, settings, llm, user, usage, alerter)
    await handle_text_turn(
        make_message("Bonjour", bot=fake_bot),
        state,
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
        alerter,
    )
    reply = fake_bot.session.sent_messages[-1].text
    assert len(reply) <= 4096
    assert reply.count("<i>") == reply.count("</i>")
    assert reply.count("<b>") == reply.count("</b>")
