"""Fakes for tests: an aiogram session that records outgoing API calls instead
of performing HTTP requests, builders for incoming Telegram objects, a FakeLLM,
and a stub anthropic client fed with canned responses.
"""

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import CallbackQuery, Chat, File, Message, Update, User, Voice

ALLOWED_USER_ID = 111_111
OTHER_USER_ID = 999_999

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def load_fixture_json(name: str) -> Any:
    return json.loads(load_fixture(name))


class RecordingSession(BaseSession):
    """Records every TelegramMethod and returns a plausible result."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self._message_id = 1000

    async def close(self) -> None:
        pass

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109 - aiogram BaseSession interface
    ) -> TelegramType:
        self.requests.append(method)
        return self._result_for(method)

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 - aiogram BaseSession interface
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        yield b"fake-audio-bytes"

    def _result_for(self, method: TelegramMethod[Any]) -> Any:
        name = type(method).__name__
        if name in {"SendMessage", "EditMessageText", "EditMessageReplyMarkup"}:
            self._message_id += 1
            chat_id = getattr(method, "chat_id", None) or ALLOWED_USER_ID
            return Message.model_construct(
                message_id=self._message_id,
                date=datetime.now(UTC),
                chat=Chat.model_construct(id=int(chat_id), type="private"),
                text=getattr(method, "text", None),
            )
        if name == "GetFile":
            return File(
                file_id=method.file_id,
                file_unique_id=f"unique-{method.file_id}",
                file_size=1024,
                file_path="voice/test.ogg",
            )
        return True

    # -- helpers for assertions ------------------------------------------------

    def sent(self, method_name: str) -> list[TelegramMethod[Any]]:
        return [m for m in self.requests if type(m).__name__ == method_name]

    @property
    def sent_messages(self) -> list[TelegramMethod[Any]]:
        return self.sent("SendMessage")


def make_bot() -> Bot:
    return Bot(
        token="42:TEST-TOKEN",
        session=RecordingSession(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def make_user(user_id: int = ALLOWED_USER_ID) -> User:
    return User(id=user_id, is_bot=False, first_name="Test")


def make_message(
    text: str,
    user_id: int = ALLOWED_USER_ID,
    bot: Bot | None = None,
    message_id: int = 1,
) -> Message:
    message = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=make_user(user_id),
        text=text,
    )
    if bot is not None:
        message = message.as_(bot)
    return message


def make_voice_message(
    duration: int = 5,
    user_id: int = ALLOWED_USER_ID,
    bot: Bot | None = None,
    message_id: int = 2,
) -> Message:
    message = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=make_user(user_id),
        voice=Voice(
            file_id="voice-file-1",
            file_unique_id="voice-unique-1",
            duration=duration,
            mime_type="audio/ogg",
        ),
    )
    if bot is not None:
        message = message.as_(bot)
    return message


def make_callback_query(
    data: str,
    user_id: int = ALLOWED_USER_ID,
    bot: Bot | None = None,
    message: Message | None = None,
) -> CallbackQuery:
    if message is None:
        message = make_message("front", user_id=user_id, bot=bot, message_id=500)
    query = CallbackQuery(
        id="cbq-1",
        from_user=make_user(user_id),
        chat_instance="ci-1",
        message=message,
        data=data,
    )
    if bot is not None:
        query = query.as_(bot)
    return query


def make_update_with_message(text: str, user_id: int = ALLOWED_USER_ID) -> Update:
    return Update(update_id=1, message=make_message(text, user_id=user_id))


# -- card factory --------------------------------------------------------------


def enrichment_dict(lemma: str = "maison") -> dict[str, Any]:
    data = load_fixture_json("enrichment_valid.json")
    data["lemma"] = lemma
    return data


async def add_vocab_card(
    session_factory: Any,
    lemma: str = "maison",
    *,
    reviewed_days_ago: float | None = None,
    due: datetime | None = None,
    suspended: bool = False,
    created_at: datetime | None = None,
) -> int:
    """Insert a vocab card. reviewed_days_ago=None -> a New card;
    otherwise the card was graded Good that many days ago (state Learning,
    due shortly after that review). An explicit `due` overrides the column.
    """
    from frbot.db.models import Card
    from frbot.srs.scheduler import SrsScheduler

    srs = SrsScheduler(desired_retention=0.9)
    new = srs.new_card()
    fsrs_data, card_due, card_state = new.fsrs, new.due, new.state
    if reviewed_days_ago is not None:
        past = datetime.now(UTC) - timedelta(days=reviewed_days_ago)
        result = srs.review(new.fsrs, 3, now=past)
        fsrs_data, card_due, card_state = result.fsrs, result.due, result.state
    card = Card(
        text=lemma,
        lemma=lemma,
        kind="vocab",
        enrichment=enrichment_dict(lemma),
        fsrs=fsrs_data,
        due=due or card_due,
        state=card_state,
        suspended=suspended,
    )
    if created_at is not None:
        card.created_at = created_at
    async with session_factory() as session:
        session.add(card)
        await session.commit()
        return card.id


# -- LLM fakes -----------------------------------------------------------------


class FakeLLM:
    """Duck-typed LLMClient: returns queued results or raises queued exceptions."""

    def __init__(
        self,
        enrich_results: list[Any] | None = None,
        correct_results: list[Any] | None = None,
        cloze_results: list[Any] | None = None,
        topic_results: list[Any] | None = None,
        voice_words_results: list[Any] | None = None,
        transcribe_results: list[Any] | None = None,
        talk_results: list[Any] | None = None,
    ) -> None:
        self.enrich_results = enrich_results or []
        self.correct_results = correct_results or []
        self.cloze_results = cloze_results or []
        self.topic_results = topic_results or []
        self.voice_words_results = voice_words_results or []
        self.transcribe_results = transcribe_results or []
        self.talk_results = talk_results or []
        self.enrich_calls: list[str] = []
        self.correct_calls: list[tuple[str, str]] = []
        self.cloze_calls: list[tuple[str, list[str]]] = []
        self.topic_calls: list[tuple[str, int, list[str]]] = []
        self.voice_words_calls: list[str] = []
        self.transcribe_calls: list[str] = []
        self.talk_calls: list[dict[str, Any]] = []

    @staticmethod
    def _next(queue: list[Any]) -> Any:
        result = queue[0] if len(queue) == 1 else queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def enrich(self, text: str, *, model: str) -> Any:
        self.enrich_calls.append(text)
        return self._next(self.enrich_results)

    async def correct(self, prompt: str, answer: str, *, model: str) -> Any:
        self.correct_calls.append((prompt, answer))
        return self._next(self.correct_results)

    async def cloze(self, topic: str, lemmas: Any, *, model: str) -> Any:
        self.cloze_calls.append((topic, list(lemmas)))
        return self._next(self.cloze_results)

    async def topic_words(self, topic: str, count: int, known_lemmas: Any, *, model: str) -> Any:
        self.topic_calls.append((topic, count, list(known_lemmas)))
        return self._next(self.topic_results)

    async def extract_voice_words(self, audio: bytes, mime_type: str, *, model: str) -> Any:
        self.voice_words_calls.append(mime_type)
        return self._next(self.voice_words_results)

    async def transcribe(self, audio: bytes, mime_type: str, *, model: str) -> Any:
        self.transcribe_calls.append(mime_type)
        return self._next(self.transcribe_results)

    async def talk_open(self, lemmas: Any, *, model: str) -> Any:
        self.talk_calls.append({"kind": "open", "lemmas": list(lemmas)})
        return self._next(self.talk_results)

    async def talk_turn(
        self,
        history: str,
        *,
        model: str,
        text: str | None = None,
        audio: tuple[bytes, str] | None = None,
    ) -> Any:
        self.talk_calls.append(
            {"kind": "turn", "history": history, "text": text, "audio": audio is not None}
        )
        return self._next(self.talk_results)


def text_response(text: str, input_tokens: int = 100, output_tokens: int = 200) -> Any:
    """Shape-compatible stand-in for a google-genai GenerateContentResponse."""
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(
            prompt_token_count=input_tokens, candidates_token_count=output_tokens
        ),
    )


class StubGenaiModels:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class StubGenaiClient:
    """Stands in for genai.Client; outcomes are responses or exceptions."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.aio = SimpleNamespace(models=StubGenaiModels(outcomes))

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.aio.models.calls
