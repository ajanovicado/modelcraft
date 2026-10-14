from __future__ import annotations

import pytest

from ai_powerbi_analyst.contracts import RouteDecision, RouteIntent
from ai_powerbi_analyst.llm.ollama import (
    InvalidModelResponse,
    OllamaSettings,
    OllamaStructuredClient,
)


class FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeResponse:
    def __init__(self, content: str) -> None:
        self.message = FakeMessage(content)


class FakeOllamaClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    def chat(self, **kwargs: object) -> FakeResponse:
        self.calls.append(kwargs)
        return FakeResponse(self.content)


def test_settings_reject_non_loopback_ollama_hosts() -> None:
    with pytest.raises(ValueError, match="loopback"):
        OllamaSettings(endpoint="http://192.168.1.20:11434")


def test_structured_client_sends_closed_json_schema_and_parses_response() -> None:
    fake = FakeOllamaClient(
        '{"intent":"ANALYZE","confidence":0.91,"requested_object_ids":[],'
        '"missing_information":[],"reason":"Numerical question"}'
    )
    client = OllamaStructuredClient(
        settings=OllamaSettings(name="test-model", max_retries=0),
        client=fake,
    )

    result = client.generate(
        system_prompt="Classify intent.",
        user_prompt="Why did revenue fall?",
        response_model=RouteDecision,
    )

    assert result.intent is RouteIntent.ANALYZE
    call = fake.calls[0]
    assert call["model"] == "test-model"
    assert isinstance(call["format"], dict)
    assert call["options"] == {"temperature": 0, "num_ctx": 4096, "num_predict": 512}


def test_invalid_model_json_fails_closed() -> None:
    client = OllamaStructuredClient(
        settings=OllamaSettings(name="test-model", max_retries=0),
        client=FakeOllamaClient("not-json"),
    )

    with pytest.raises(InvalidModelResponse):
        client.generate("Classify.", "Maybe this", RouteDecision)
