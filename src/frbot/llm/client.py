"""Anthropic wrapper: JSON-only completions parsed into pydantic schemas.

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

import anthropic
from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

from frbot.llm import prompts
from frbot.llm.schemas import ClozeSet, Enrichment, WritingCorrection

logger = logging.getLogger(__name__)

MAX_TOKENS = 1500
TEMPERATURE_ENRICH = 0.2
TEMPERATURE_DRILL = 0.2
TEMPERATURE_CORRECTION = 0.0
DEFAULT_BACKOFF: Sequence[float] = (1.0, 2.0, 4.0)
# The SDK default is 600s per attempt; while an LLM call runs, the per-user
# isolation lock is held, so keep attempts short.
REQUEST_TIMEOUT = 30.0


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
        # The SDK's own retries are disabled; retry policy lives here.
        self._client = client or AsyncAnthropic(
            api_key=api_key, max_retries=0, timeout=REQUEST_TIMEOUT
        )
        self._backoff = backoff

    # -- public API ----------------------------------------------------------

    async def enrich(self, text: str, *, model: str) -> Enrichment:
        return await self.complete_json(
            model=model,
            system=prompts.ENRICH_SYSTEM,
            prompt=prompts.ENRICH_USER.format(text=text),
            schema=Enrichment,
            temperature=TEMPERATURE_ENRICH,
        )

    async def correct(self, prompt: str, answer: str, *, model: str) -> WritingCorrection:
        return await self.complete_json(
            model=model,
            system=prompts.CORRECTION_SYSTEM,
            prompt=prompts.CORRECTION_USER.format(prompt=prompt, answer=answer),
            schema=WritingCorrection,
            temperature=TEMPERATURE_CORRECTION,
        )

    async def cloze(self, topic: str, lemmas: Sequence[str], *, model: str) -> ClozeSet:
        lemma_list = ", ".join(lemmas) if lemmas else "(none yet)"
        return await self.complete_json(
            model=model,
            system=prompts.CLOZE_SYSTEM,
            prompt=prompts.CLOZE_USER.format(topic=topic, lemmas=lemma_list),
            schema=ClozeSet,
            temperature=TEMPERATURE_DRILL,
        )

    # -- core ----------------------------------------------------------------

    async def complete_json[T: BaseModel](
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        schema: type[T],
        temperature: float,
    ) -> T:
        text = await self._call(model, system, prompt, temperature)
        try:
            return _parse(text, schema)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning("llm output invalid for %s, retrying once: %s", schema.__name__, exc)
            retry_prompt = (
                f"{prompt}\n\n"
                f"Your previous response was invalid:\n{exc}\n\n"
                f"Return only the corrected JSON object."
            )
            text = await self._call(model, system, retry_prompt, temperature)
            try:
                return _parse(text, schema)
            except (json.JSONDecodeError, ValidationError) as exc2:
                logger.error("llm output invalid twice for %s: %s", schema.__name__, exc2)
                raise LLMOutputError(f"invalid {schema.__name__} output") from exc2

    async def _call(self, model: str, system: str, prompt: str, temperature: float) -> str:
        last_exc: Exception | None = None
        for attempt in range(len(self._backoff) + 1):
            if attempt > 0:
                await asyncio.sleep(self._backoff[attempt - 1])
            try:
                response = await self._client.messages.create(
                    model=model,
                    max_tokens=MAX_TOKENS,
                    temperature=temperature,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                )
            except anthropic.APIConnectionError as exc:
                logger.warning("llm connection error (attempt %d): %s", attempt + 1, exc)
                last_exc = exc
                continue
            except anthropic.APIStatusError as exc:
                if exc.status_code == 429 or exc.status_code >= 500:
                    logger.warning(
                        "llm status %s (attempt %d): %s", exc.status_code, attempt + 1, exc
                    )
                    last_exc = exc
                    continue
                logger.error("llm non-retryable status %s: %s", exc.status_code, exc)
                raise LLMError(f"Anthropic API error {exc.status_code}") from exc

            usage = getattr(response, "usage", None)
            logger.info(
                "llm call model=%s input_tokens=%s output_tokens=%s",
                model,
                getattr(usage, "input_tokens", "?"),
                getattr(usage, "output_tokens", "?"),
            )
            return "".join(
                block.text for block in response.content if getattr(block, "type", "") == "text"
            )
        logger.error("llm call failed after %d attempts", len(self._backoff) + 1)
        raise LLMError("Anthropic API unavailable") from last_exc


def _parse[T: BaseModel](text: str, schema: type[T]) -> T:
    cleaned = text.strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise json.JSONDecodeError("no JSON object found", cleaned or " ", 0)
    data = json.loads(cleaned[start : end + 1])
    return schema.model_validate(data)
