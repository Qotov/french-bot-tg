from types import SimpleNamespace

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, select

from frbot.bot.handlers.topic import (
    FAIL_TEXT,
    TopicStates,
    cmd_topic,
    handle_topic_input,
    on_cancel,
    on_save,
    on_toggle,
    parse_topic_args,
)
from frbot.db.models import Card
from frbot.llm.client import LLMError
from frbot.llm.schemas import Enrichment, TopicWordList
from frbot.srs.scheduler import SrsScheduler
from tests.fakes import (
    ALLOWED_USER_ID,
    FakeLLM,
    add_vocab_card,
    enrichment_dict,
    load_fixture_json,
    make_callback_query,
    make_message,
)

RESTAURANT_WORDS = TopicWordList.model_validate(
    {
        "words": [
            {"lemma": "l'addition", "translation_ru": "счёт"},
            {"lemma": "commander", "translation_ru": "заказывать"},
            {"lemma": "le pourboire", "translation_ru": "чаевые"},
        ]
    }
)


def make_state(bot) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=bot.id, chat_id=ALLOWED_USER_ID, user_id=ALLOWED_USER_ID),
    )


def command_obj(args: str | None) -> SimpleNamespace:
    return SimpleNamespace(args=args)


async def pack_query(data_str: str, state: FSMContext, bot):
    """Callback bound to the current pack's selection message."""
    mid = (await state.get_data())["message_id"]
    return make_callback_query(
        data_str, bot=bot, message=make_message("selection", bot=bot, message_id=mid)
    )


def enrichment_for(lemma: str) -> Enrichment:
    return Enrichment.model_validate(enrichment_dict(lemma))


def srs() -> SrsScheduler:
    return SrsScheduler(desired_retention=0.9)


async def vocab_count(session_factory) -> int:
    async with session_factory() as session:
        stmt = select(func.count(Card.id)).where(Card.kind == "vocab")
        return (await session.execute(stmt)).scalar_one()


# ----------------------------------------------------------------- arg parse


def test_parse_topic_args():
    assert parse_topic_args("ресторан") == ("ресторан", 10)
    assert parse_topic_args("ресторан 15") == ("ресторан", 15)
    assert parse_topic_args("15 la cuisine française") == ("la cuisine française", 15)
    assert parse_topic_args("ресторан 1") == ("ресторан", 3)  # floored
    assert parse_topic_args("ресторан 25") == ("ресторан", 20)  # capped
    assert parse_topic_args("") is None
    assert parse_topic_args("12") is None  # a number is not a topic
    # Numbers that belong to the topic stay in the topic.
    assert parse_topic_args("les années 80") == ("les années 80", 10)
    assert parse_topic_args("top 10 des verbes") == ("top 10 des verbes", 10)


# --------------------------------------------------------------------- flows


async def test_topic_generates_selection(fake_bot, session_factory, settings, user, usage):
    await add_vocab_card(session_factory, "maison")
    llm = FakeLLM(topic_results=[RESTAURANT_WORDS])
    state = make_state(fake_bot)
    await cmd_topic(
        make_message("/topic ресторан 5", bot=fake_bot),
        command_obj("ресторан 5"),
        state,
        user,
        session_factory,
        llm,
        settings,
        usage,
    )
    topic, count, known = llm.topic_calls[0]
    assert topic == "ресторан"
    assert count == 5
    assert "maison" in known

    assert await state.get_state() == TopicStates.selecting.state
    sent = fake_bot.session.sent_messages[-1]
    assert "ресторан" in sent.text
    assert "commander" in sent.text
    labels = [b.text for row in sent.reply_markup.inline_keyboard for b in row]
    assert any("✅ l'addition" in label for label in labels)
    assert any("Добавить (3)" in label for label in labels)


async def test_topic_without_args_asks_then_generates(
    fake_bot, session_factory, settings, user, usage
):
    llm = FakeLLM(topic_results=[RESTAURANT_WORDS])
    state = make_state(fake_bot)
    await cmd_topic(
        make_message("/topic", bot=fake_bot),
        command_obj(None),
        state,
        user,
        session_factory,
        llm,
        settings,
        usage,
    )
    assert await state.get_state() == TopicStates.choosing.state
    assert "тему" in fake_bot.session.sent_messages[-1].text

    await handle_topic_input(
        make_message("ресторан 4", bot=fake_bot), state, user, session_factory, llm, settings, usage
    )
    assert await state.get_state() == TopicStates.selecting.state


async def test_known_words_dropped_from_selection(fake_bot, session_factory, settings, user, usage):
    await add_vocab_card(session_factory, "commander")
    llm = FakeLLM(topic_results=[RESTAURANT_WORDS])
    state = make_state(fake_bot)
    await cmd_topic(
        make_message("/topic ресторан", bot=fake_bot),
        command_obj("ресторан"),
        state,
        user,
        session_factory,
        llm,
        settings,
        usage,
    )
    data = await state.get_data()
    lemmas = [w["lemma"] for w in data["words"]]
    assert "commander" not in lemmas
    assert len(lemmas) == 2


async def test_toggle_and_save_creates_selected_cards(
    fake_bot, session_factory, settings, user, usage
):
    llm = FakeLLM(
        topic_results=[RESTAURANT_WORDS],
        enrich_results=[enrichment_for("l'addition"), enrichment_for("le pourboire")],
    )
    state = make_state(fake_bot)
    await cmd_topic(
        make_message("/topic ресторан", bot=fake_bot),
        command_obj("ресторан"),
        state,
        user,
        session_factory,
        llm,
        settings,
        usage,
    )
    # Deselect index 1 ("commander").
    await on_toggle(await pack_query("topic:toggle:1", state, fake_bot), state)
    assert (await state.get_data())["selected"] == [0, 2]

    await on_save(
        await pack_query("topic:save", state, fake_bot),
        state,
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
    )
    assert await vocab_count(session_factory) == 2
    assert sorted(llm.enrich_calls) == ["l'addition", "le pourboire"]
    summary = fake_bot.session.sent_messages[-1].text
    assert "Добавлено карточек: 2" in summary
    assert await state.get_state() is None


async def test_save_skips_duplicates_from_enrichment(
    fake_bot, session_factory, settings, user, usage
):
    # Enrichment maps both selected words to the same lemma -> one card.
    llm = FakeLLM(
        topic_results=[RESTAURANT_WORDS],
        enrich_results=[enrichment_for("commander"), enrichment_for("commander")],
    )
    state = make_state(fake_bot)
    await cmd_topic(
        make_message("/topic ресторан", bot=fake_bot),
        command_obj("ресторан"),
        state,
        user,
        session_factory,
        llm,
        settings,
        usage,
    )
    await on_toggle(await pack_query("topic:toggle:2", state, fake_bot), state)  # keep 0, 1
    await on_save(
        await pack_query("topic:save", state, fake_bot),
        state,
        user,
        session_factory,
        llm,
        srs(),
        settings,
        usage,
    )
    assert await vocab_count(session_factory) == 1
    assert "Уже были в колоде: 1" in fake_bot.session.sent_messages[-1].text


async def test_cancel_clears_state(fake_bot, session_factory, settings, user, usage):
    llm = FakeLLM(topic_results=[RESTAURANT_WORDS])
    state = make_state(fake_bot)
    await cmd_topic(
        make_message("/topic ресторан", bot=fake_bot),
        command_obj("ресторан"),
        state,
        user,
        session_factory,
        llm,
        settings,
        usage,
    )
    await on_cancel(await pack_query("topic:cancel", state, fake_bot), state)
    assert await state.get_state() is None
    assert await vocab_count(session_factory) == 0


async def test_topic_llm_failure(fake_bot, session_factory, settings, user, usage):
    llm = FakeLLM(topic_results=[LLMError("down")])
    state = make_state(fake_bot)
    await cmd_topic(
        make_message("/topic ресторан", bot=fake_bot),
        command_obj("ресторан"),
        state,
        user,
        session_factory,
        llm,
        settings,
        usage,
    )
    assert fake_bot.session.sent_messages[-1].text == FAIL_TEXT
    assert await state.get_state() is None


async def test_all_words_already_known(fake_bot, session_factory, settings, user, usage):
    for word in ("l'addition", "commander", "le pourboire"):
        await add_vocab_card(session_factory, word)
    llm = FakeLLM(topic_results=[RESTAURANT_WORDS])
    state = make_state(fake_bot)
    await cmd_topic(
        make_message("/topic ресторан", bot=fake_bot),
        command_obj("ресторан"),
        state,
        user,
        session_factory,
        llm,
        settings,
        usage,
    )
    assert "уже в колоде" in fake_bot.session.sent_messages[-1].text
    assert await state.get_state() is None


def test_fixture_sanity():
    assert load_fixture_json("enrichment_valid.json")["lemma"]


async def test_stale_pack_keyboard_is_inert(fake_bot, session_factory, settings, user, usage):
    """A superseded pack's keyboard must not drive the new pack's state."""
    llm = FakeLLM(topic_results=[RESTAURANT_WORDS, RESTAURANT_WORDS])
    state = make_state(fake_bot)
    await cmd_topic(
        make_message("/topic ресторан", bot=fake_bot),
        command_obj("ресторан"),
        state,
        user,
        session_factory,
        llm,
        settings,
        usage,
    )
    old_mid = (await state.get_data())["message_id"]
    await cmd_topic(
        make_message("/topic voyage", bot=fake_bot),
        command_obj("voyage"),
        state,
        user,
        session_factory,
        llm,
        settings,
        usage,
    )
    # Tap the OLD keyboard: toggle must not change the new selection,
    # cancel must not kill the new pack's state.
    stale = make_callback_query(
        "topic:toggle:0",
        bot=fake_bot,
        message=make_message("old", bot=fake_bot, message_id=old_mid),
    )
    await on_toggle(stale, state)
    assert (await state.get_data())["selected"] == [0, 1, 2]

    stale_cancel = make_callback_query(
        "topic:cancel", bot=fake_bot, message=make_message("old", bot=fake_bot, message_id=old_mid)
    )
    await on_cancel(stale_cancel, state)
    assert await state.get_state() == TopicStates.selecting.state


async def test_voice_while_choosing_asks_for_text(fake_bot):
    from frbot.bot.handlers.topic import on_voice_while_choosing
    from tests.fakes import make_voice_message

    await on_voice_while_choosing(make_voice_message(bot=fake_bot))
    assert "текстом" in fake_bot.session.sent_messages[-1].text
