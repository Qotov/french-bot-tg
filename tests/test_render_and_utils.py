from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import TelegramMethod

from frbot.bot import render
from frbot.bot.telegram_utils import safe_edit_text
from frbot.llm.schemas import WritingCorrection, WritingError
from tests.fakes import RecordingSession, make_bot, make_message

# ---------------------------------------------------------------- make_gapped


def test_make_gapped_respects_word_boundaries():
    assert render.make_gapped("Marie a parlé à son frère.", "a") == "Marie ___ parlé à son frère."


def test_make_gapped_handles_contractions():
    assert render.make_gapped("Hier j'ai acheté du pain.", "ai") == "Hier j'___ acheté du pain."


def test_make_gapped_returns_none_when_absent():
    assert render.make_gapped("Nous sommes partis.", "avons") is None


def test_make_gapped_multiword_span():
    result = render.make_gapped("Hier je suis allée au marché.", "je suis allée")
    assert result == "Hier ___ au marché."


# ---------------------------------------------------------- error card fronts


class FakeCard:
    def __init__(self, text, kind, error_meta):
        self.text = text
        self.kind = kind
        self.error_meta = error_meta


def test_error_card_front_prefers_stored_front():
    card = FakeCard(
        "Marie a parlé à son frère.",
        "drill_error",
        {"corrected": "a", "front": "Marie ___ parlé à son frère.", "original": "est"},
    )
    front = render.error_card_front(card)
    assert "Marie ___ parlé" in front
    assert "M___rie" not in front


def test_error_card_front_falls_back_to_word_boundary_gap():
    card = FakeCard(
        "Marie a parlé à son frère.",
        "drill_error",
        {"corrected": "a", "original": "est"},
    )
    front = render.error_card_front(card)
    assert "Marie ___ parlé" in front


def test_error_card_front_asks_to_correct_when_span_not_found():
    card = FakeCard(
        "Nous sommes partis très tôt.",
        "error",
        {"corrected": "avons quitté", "original": "avons parti"},
    )
    front = render.error_card_front(card)
    assert "Исправь" in front
    assert "avons parti" in front


# ------------------------------------------------------- correction msg caps


def test_correction_message_caps_displayed_errors():
    errors = [
        WritingError(
            original=f"faute{i}", corrected=f"correct{i}", type="other", explanation_ru="п."
        )
        for i in range(12)
    ]
    correction = WritingCorrection(corrected_text="Texte.", errors=errors, comment_ru="ок")
    text = render.correction_message(correction, created_cards=5)
    assert "8. ❌" in text
    assert "9. ❌" not in text
    assert "и ещё 4" in text
    assert len(text) < 4096


# ------------------------------------------------------------- safe_edit_text


class EditFailingSession(RecordingSession):
    def __init__(self, error_text: str) -> None:
        super().__init__()
        self.error_text = error_text

    def _result_for(self, method: TelegramMethod) -> object:
        if type(method).__name__ == "EditMessageText":
            raise TelegramBadRequest(method=method, message=self.error_text)
        return super()._result_for(method)


async def test_safe_edit_swallows_not_modified():
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    session = EditFailingSession("Bad Request: message is not modified")
    bot = Bot(
        token="42:TEST-TOKEN",
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    message = make_message("old", bot=bot)
    await safe_edit_text(message, "old")  # must not raise
    assert session.sent("SendMessage") == []  # no fallback needed
    await bot.session.close()


async def test_safe_edit_falls_back_to_new_message():
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    session = EditFailingSession("Bad Request: message can't be edited")
    bot = Bot(
        token="42:TEST-TOKEN",
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    message = make_message("old", bot=bot)
    await safe_edit_text(message, "new text")
    fallback = session.sent("SendMessage")
    assert len(fallback) == 1
    assert fallback[0].text == "new text"
    await bot.session.close()


async def test_recording_session_still_succeeds_normally(fake_bot):
    message = make_message("hello", bot=fake_bot)
    await safe_edit_text(message, "edited")
    assert len(fake_bot.session.sent("EditMessageText")) == 1


def test_make_bot_helper():
    bot = make_bot()
    assert bot.id == 42
