import httpx2
import pytest
from anthropic import BadRequestError, InternalServerError, RateLimitError

from frbot.llm.client import LLMClient, LLMError, LLMOutputError
from frbot.llm.schemas import Enrichment
from tests.fakes import StubAnthropicClient, load_fixture, text_response

NO_BACKOFF = (0.0, 0.0, 0.0)


def make_client(outcomes) -> tuple[LLMClient, StubAnthropicClient]:
    stub = StubAnthropicClient(outcomes)
    return LLMClient("test-key", client=stub, backoff=NO_BACKOFF), stub


def status_error(cls, status: int):
    response = httpx2.Response(
        status, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    return cls("boom", response=response, body=None)


async def test_valid_json_first_try():
    client, stub = make_client([text_response(load_fixture("enrichment_valid.json"))])
    result = await client.enrich("au fur et à mesure", model="test-model")
    assert isinstance(result, Enrichment)
    assert result.lemma == "au fur et à mesure"
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["model"] == "test-model"
    assert call["max_tokens"] == 1500
    assert call["temperature"] == 0.2


async def test_json_wrapped_in_fences_and_preamble_parses():
    wrapped = "Voici la carte:\n```json\n" + load_fixture("enrichment_valid.json") + "\n```"
    client, _ = make_client([text_response(wrapped)])
    result = await client.enrich("au fur et à mesure", model="m")
    assert result.lemma == "au fur et à mesure"


async def test_invalid_then_valid_retries_once_with_error_appended():
    client, stub = make_client(
        [
            text_response(load_fixture("enrichment_two_examples.json")),
            text_response(load_fixture("enrichment_valid.json")),
        ]
    )
    result = await client.enrich("au fur et à mesure", model="m")
    assert result.lemma == "au fur et à mesure"
    assert len(stub.calls) == 2
    retry_prompt = stub.calls[1]["messages"][0]["content"]
    assert "previous response was invalid" in retry_prompt
    assert "3 examples" in retry_prompt


async def test_invalid_twice_raises_output_error():
    client, stub = make_client(
        [
            text_response(load_fixture("not_json.txt")),
            text_response(load_fixture("enrichment_two_examples.json")),
        ]
    )
    with pytest.raises(LLMOutputError):
        await client.enrich("maison", model="m")
    assert len(stub.calls) == 2


async def test_rate_limit_retried_then_succeeds():
    client, stub = make_client(
        [
            status_error(RateLimitError, 429),
            status_error(InternalServerError, 500),
            text_response(load_fixture("enrichment_valid.json")),
        ]
    )
    result = await client.enrich("au fur et à mesure", model="m")
    assert result.lemma == "au fur et à mesure"
    assert len(stub.calls) == 3


async def test_persistent_5xx_exhausts_retries():
    client, stub = make_client([status_error(InternalServerError, 500)] * 4)
    with pytest.raises(LLMError):
        await client.enrich("maison", model="m")
    assert len(stub.calls) == 4  # initial + 3 retries


async def test_bad_request_is_not_retried():
    client, stub = make_client([status_error(BadRequestError, 400)])
    with pytest.raises(LLMError):
        await client.enrich("maison", model="m")
    assert len(stub.calls) == 1


async def test_correction_uses_zero_temperature():
    client, stub = make_client([text_response(load_fixture("correction_valid.json"))])
    await client.correct("Décris ta journée.", "je suis allé au marché depuis hier", model="m")
    assert stub.calls[0]["temperature"] == 0.0


async def test_cloze_passes_lemmas():
    client, stub = make_client([text_response(load_fixture("cloze_valid.json"))])
    result = await client.cloze("avoir vs être", ["maison", "marché"], model="m")
    assert len(result.items) == 5
    assert "maison, marché" in stub.calls[0]["messages"][0]["content"]
