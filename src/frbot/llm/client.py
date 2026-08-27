"""Gemini wrapper: JSON-only completions parsed into pydantic schemas.

Two retry layers, per the spec:
- transport: 3 retries with exponential backoff on 429/5xx/connection errors;
- output: one retry with the validation error appended to the prompt, then
  LLMOutputError (the caller shows a user-visible failure message).
"""

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import Any

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, ValidationError

from frbot.llm import prompts
from frbot.llm.schemas import (
    ClozeSet,
    Enrichment,
    TalkTurn,
    TopicWordList,
    Transcript,
    VoiceWords,
    WritingCorrection,
)

logger = logging.getLogger(__name__)

MAX_TOKENS = 1500
TEMPERATURE_ENRICH = 0.2
TEMPERATURE_DRILL = 0.2
TEMPERATURE_CORRECTION = 0.0
TEMPERATURE_TALK = 0.7  # conversation should not be robotic
DEFAULT_BACKOFF: Sequence[float] = (1.0, 2.0, 4.0)
# While an LLM call runs, the per-user isolation lock is held, so keep
# attempts short. google-genai HttpOptions.timeout is in milliseconds.
REQUEST_TIMEOUT_MS = 30_000


class LLMError(Exception):
    """The LLM call failed for good; the caller should tell the user."""


class LLMOutputError(LLMError):
    """The LLM answered, but the output never validated."""


class LLMClient:
    def __init__(
        self,
        api_key: str,
        *,
        client: Any | None = None,
        backoff: Sequence[float] = DEFAULT_BACKOFF,
    ) -> None:
        # No SDK-internal retries are configured; retry policy lives here.
        self._client = client or genai.Client(
            api_key=api_key,
            http_options=genai_types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
        )
        self._backoff = backoff

    # -- public API ----------------------------------------------------------

    async def enrich(self, text: str, *, model: str, level: str = "B1") -> Enrichment:
        return await self.complete_json(
            model=model,
            system=prompts.with_level(prompts.ENRICH_SYSTEM, level),
            contents=prompts.ENRICH_USER.format(text=text),
            schema=Enrichment,
            temperature=TEMPERATURE_ENRICH,
        )

    async def correct(
        self, prompt: str, answer: str, *, model: str, level: str = "B1"
    ) -> WritingCorrection:
        return await self.complete_json(
            model=model,
            system=prompts.with_level(prompts.CORRECTION_SYSTEM, level),
            contents=prompts.CORRECTION_USER.format(prompt=prompt, answer=answer),
            schema=WritingCorrection,
            temperature=TEMPERATURE_CORRECTION,
        )

    async def cloze(
        self, topic: str, lemmas: Sequence[str], *, model: str, level: str = "B1"
    ) -> ClozeSet:
        lemma_list = ", ".join(lemmas) if lemmas else "(none yet)"
        return await self.complete_json(
            model=model,
            system=prompts.with_level(prompts.CLOZE_SYSTEM, level),
            contents=prompts.CLOZE_USER.format(topic=topic, lemmas=lemma_list),
            schema=ClozeSet,
            temperature=TEMPERATURE_DRILL,
        )

    async def synthesize(self, text: str, *, model: str, voice: str) -> bytes:
        """French pronunciation as raw 24 kHz mono PCM.

        Uses the same transport retry ladder as the text calls but skips the
        JSON layer entirely — the payload is audio, and there is nothing to
        validate or re-prompt.
        """
        last_exc: Exception | None = None
        for attempt in range(len(self._backoff) + 1):
            if attempt > 0:
                await asyncio.sleep(self._backoff[attempt - 1])
            try:
                response = await self._client.aio.models.generate_content(
                    model=model,
                    contents=(
                        "Prononce ce mot ou cette expression en français, "
                        f"clairement et à vitesse normale : {text}"
                    ),
                    config=genai_types.GenerateContentConfig(
                        response_modalities=["AUDIO"],
                        speech_config=genai_types.SpeechConfig(
                            language_code="fr-FR",
                            voice_config=genai_types.VoiceConfig(
                                prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                                    voice_name=voice
                                )
                            ),
                        ),
                    ),
                )
            except genai_errors.APIError as exc:
                code = exc.code or 0
                if code == 429 or code >= 500:
                    logger.warning("tts status %s (attempt %d)", code, attempt + 1)
                    last_exc = exc
                    continue
                raise LLMError(f"Gemini TTS error {code}") from exc
            except (httpx.HTTPError, ConnectionError, TimeoutError) as exc:
                logger.warning("tts connection error (attempt %d): %s", attempt + 1, exc)
                last_exc = exc
                continue

            audio = _extract_audio(response)
            if audio is None:
                raise LLMError("Gemini returned no audio")
            logger.info("tts model=%s bytes=%d text=%r", model, len(audio), text[:40])
            return audio
        raise LLMError("Gemini TTS unavailable") from last_exc

    async def topic_words(
        self,
        topic: str,
        count: int,
        known_lemmas: Sequence[str],
        *,
        model: str,
        level: str = "B1",
    ) -> TopicWordList:
        known = ", ".join(known_lemmas) if known_lemmas else "(nothing yet)"
        return await self.complete_json(
            model=model,
            system=prompts.with_level(prompts.TOPIC_SYSTEM, level),
            contents=prompts.TOPIC_USER.format(topic=topic, count=count, known=known),
            schema=TopicWordList,
            temperature=TEMPERATURE_ENRICH,
        )

    async def extract_voice_words(self, audio: bytes, mime_type: str, *, model: str) -> VoiceWords:
        return await self.complete_json(
            model=model,
            system=prompts.VOICE_CAPTURE_SYSTEM,
            contents=[_audio_part(audio, mime_type), prompts.VOICE_CAPTURE_USER],
            schema=VoiceWords,
            temperature=TEMPERATURE_ENRICH,
        )

    async def transcribe(self, audio: bytes, mime_type: str, *, model: str) -> Transcript:
        return await self.complete_json(
            model=model,
            system=prompts.TRANSCRIBE_SYSTEM,
            contents=[_audio_part(audio, mime_type), prompts.TRANSCRIBE_USER],
            schema=Transcript,
            temperature=TEMPERATURE_CORRECTION,
        )

    async def talk_open(self, lemmas: Sequence[str], *, model: str, level: str = "B1") -> TalkTurn:
        lemma_list = ", ".join(lemmas) if lemmas else "(none yet)"
        return await self.complete_json(
            model=model,
            system=prompts.with_level(prompts.TALK_SYSTEM, level),
            contents=prompts.TALK_OPENER_USER.format(lemmas=lemma_list),
            schema=TalkTurn,
            temperature=TEMPERATURE_TALK,
        )

    async def talk_turn(
        self,
        history: str,
        *,
        model: str,
        level: str = "B1",
        text: str | None = None,
        audio: tuple[bytes, str] | None = None,
    ) -> TalkTurn:
        if (text is None) == (audio is None):
            raise ValueError("talk_turn needs exactly one of text or audio")
        preamble = prompts.TALK_TURN_USER.format(history=history or "(beginning)")
        contents: str | list[Any]
        if text is not None:
            contents = f"{preamble}\n\nLearner (text): {text}"
        else:
            data, mime_type = audio
            contents = [preamble, _audio_part(data, mime_type)]
        return await self.complete_json(
            model=model,
            system=prompts.with_level(prompts.TALK_SYSTEM, level),
            contents=contents,
            schema=TalkTurn,
            temperature=TEMPERATURE_TALK,
        )

    # -- core ----------------------------------------------------------------

    async def complete_json[T: BaseModel](
        self,
        *,
        model: str,
        system: str,
        contents: str | list[Any],
        schema: type[T],
        temperature: float,
    ) -> T:
        text = await self._call(model, system, contents, temperature)
        try:
            return _parse(text, schema)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning("llm output invalid for %s, retrying once: %s", schema.__name__, exc)
            retry_note = (
                f"Your previous response was invalid:\n{exc}\n\n"
                f"Return only the corrected JSON object."
            )
            retry_contents: str | list[Any]
            if isinstance(contents, str):
                retry_contents = f"{contents}\n\n{retry_note}"
            else:
                retry_contents = [*contents, retry_note]
            text = await self._call(model, system, retry_contents, temperature)
            try:
                return _parse(text, schema)
            except (json.JSONDecodeError, ValidationError) as exc2:
                logger.error("llm output invalid twice for %s: %s", schema.__name__, exc2)
                raise LLMOutputError(f"invalid {schema.__name__} output") from exc2

    async def _call(
        self, model: str, system: str, contents: str | list[Any], temperature: float
    ) -> str:
        last_exc: Exception | None = None
        for attempt in range(len(self._backoff) + 1):
            if attempt > 0:
                await asyncio.sleep(self._backoff[attempt - 1])
            try:
                response = await self._client.aio.models.generate_content(
                    model=model,
                    contents=contents,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=system,
                        temperature=temperature,
                        max_output_tokens=MAX_TOKENS,
                    ),
                )
            except genai_errors.APIError as exc:
                code = exc.code or 0
                if code == 429 or code >= 500:
                    logger.warning("llm status %s (attempt %d): %s", code, attempt + 1, exc)
                    last_exc = exc
                    continue
                logger.error("llm non-retryable status %s: %s", code, exc)
                raise LLMError(f"Gemini API error {code}") from exc
            except (httpx.HTTPError, ConnectionError, TimeoutError) as exc:
                logger.warning("llm connection error (attempt %d): %s", attempt + 1, exc)
                last_exc = exc
                continue

            usage = getattr(response, "usage_metadata", None)
            logger.info(
                "llm call model=%s input_tokens=%s output_tokens=%s",
                model,
                getattr(usage, "prompt_token_count", "?"),
                getattr(usage, "candidates_token_count", "?"),
            )
            return response.text or ""
        logger.error("llm call failed after %d attempts", len(self._backoff) + 1)
        raise LLMError("Gemini API unavailable") from last_exc


def _extract_audio(response: Any) -> bytes | None:
    """Pull inline audio bytes out of a generate_content response."""
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None)
            if data:
                return data
    return None


def _audio_part(data: bytes, mime_type: str) -> genai_types.Part:
    return genai_types.Part.from_bytes(data=data, mime_type=mime_type)


def _parse[T: BaseModel](text: str, schema: type[T]) -> T:
    cleaned = text.strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise json.JSONDecodeError("no JSON object found", cleaned or " ", 0)
    data = json.loads(cleaned[start : end + 1])
    return schema.model_validate(data)
