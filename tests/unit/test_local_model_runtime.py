from __future__ import annotations

import io
import json

import pytest

from ai_powerbi_analyst.llm.runtime import LocalModelError, OllamaModelRuntime
from ai_powerbi_analyst.settings import ModelSettings
from ai_powerbi_analyst.studio.contracts import DatasetNarrativePlan


def test_truncated_model_stream_is_not_saved_as_a_completed_answer(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: io.BytesIO(b'{"message":{"content":"partial"},"done":false}\n'),
    )
    runtime = OllamaModelRuntime(ModelSettings(selected_digest=None))
    stream = runtime.stream_chat("hello", system_prompt="Be concise")
    assert next(stream) == "partial"
    with pytest.raises(LocalModelError, match="before completion"):
        next(stream)
    assert runtime.last_generation is None


def test_runtime_rejects_same_tag_with_wrong_digest(monkeypatch) -> None:
    runtime = OllamaModelRuntime(
        ModelSettings(
            name="example:latest",
            selected_digest="sha256:" + "a" * 64,
        )
    )

    monkeypatch.setattr(
        runtime,
        "_request_json",
        lambda *_args, **_kwargs: {
            "models": [
                {
                    "name": "example:latest",
                    "digest": "b" * 64,
                    "size": 123,
                    "modified_at": "2026-08-31T20:00:00-04:00",
                    "details": {
                        "parameter_size": "4B",
                        "quantization_level": "Q4_K_M",
                    },
                }
            ]
        },
    )

    health = runtime.health()

    assert health.service_available
    assert not health.model_available
    assert health.identity is not None
    assert health.identity.digest == "sha256:" + "b" * 64
    assert "digest mismatch" in (health.error or "")


def test_stream_chat_uses_chat_limits(monkeypatch) -> None:
    observed: dict[str, int] = {}
    request_payload: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self):
            payload = {"message": {"content": "Ready"}, "done": True}
            yield (json.dumps(payload) + "\n").encode()

    def fake_urlopen(request: object, *, timeout: int) -> FakeResponse:
        observed["timeout"] = timeout
        data = getattr(request, "data", None)
        assert isinstance(data, bytes)
        request_payload.update(json.loads(data.decode()))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    runtime = OllamaModelRuntime(ModelSettings(selected_digest=None, stream_timeout_seconds=240))

    history = [
        {"role": "user", "content": "Create a margin page"},
        {"role": "assistant", "content": "Review this proposal"},
    ]
    assert list(runtime.stream_chat("hello", system_prompt="be concise", history=history)) == [
        "Ready"
    ]
    assert request_payload["messages"][1:3] == history
    assert observed["timeout"] == 240
    options = request_payload["options"]
    assert isinstance(options, dict)
    assert options["num_predict"] == 384


def test_structured_generation_can_disable_schema_repair_for_a_latency_budget(
    monkeypatch,
) -> None:
    calls = 0
    runtime = OllamaModelRuntime(ModelSettings(selected_digest=None, max_retries=1))

    def invalid_response(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"message": {"content": "{}"}}

    monkeypatch.setattr(runtime, "_request_json", invalid_response)

    with pytest.raises(LocalModelError, match="strict schema"):
        runtime.generate(
            "Return a narrative.",
            "{}",
            DatasetNarrativePlan,
            timeout_seconds=50,
            repair_attempts=0,
        )

    assert calls == 1
