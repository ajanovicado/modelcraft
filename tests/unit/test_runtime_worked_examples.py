"""stream_chat(worked_examples=False) must reach the prompt through every runtime layer."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from ai_powerbi_analyst.llm.adaptive import AdaptiveQwenRuntime
from ai_powerbi_analyst.llm.contract_ollama import ContractOllamaModelRuntime
from ai_powerbi_analyst.settings import ModelSettings


def _runtime(monkeypatch, tmp_path):
    runtime = ContractOllamaModelRuntime(ModelSettings())
    seen: dict[str, list] = {"messages": [], "references": [], "budgets": []}

    class Server:
        def complete(self, messages, **kwargs):
            seen["messages"].append([dict(message) for message in messages])
            seen["budgets"].append(kwargs["max_tokens"])
            on_text = kwargs.get("on_text")
            if on_text:
                on_text("Plain answer.")
            return dict(
                raw="Plain answer.",
                output="Plain answer.",
                complete=True,
                error=None,
                finish_reason="stop",
                tokens=2,
                prompt_tokens=3,
                seconds=0.1,
                native_timings={},
            )

        def stop(self):
            return None

    @contextmanager
    def session():
        yield Server(), tmp_path, tmp_path

    def references(_root, text):
        seen["references"].append(text)
        return "\n\nWorked examples with DIFFERENT data. Chores(ChoreId integer)", ["R-1"]

    monkeypatch.setattr(runtime, "_session", session)
    monkeypatch.setattr(runtime, "_references", references)
    return runtime, seen


def test_chat_without_worked_examples_never_retrieves_them(monkeypatch, tmp_path):
    runtime, seen = _runtime(monkeypatch, tmp_path)
    text = "".join(runtime.stream_chat("Explain it", system_prompt="s", worked_examples=False))
    assert text == "Plain answer."
    assert seen["references"] == []
    assert "Worked examples" not in seen["messages"][-1][-1]["content"]


def test_chat_keeps_worked_examples_by_default(monkeypatch, tmp_path):
    runtime, seen = _runtime(monkeypatch, tmp_path)
    assert "".join(runtime.stream_chat("Explain it", system_prompt="s")) == "Plain answer."
    assert seen["references"] == ["Explain it"]


def test_buffered_artifact_path_also_honours_the_flag(monkeypatch, tmp_path):
    runtime, _seen = _runtime(monkeypatch, tmp_path)
    captured = {}

    def generate(_system, _message, response_model, **_kwargs):
        captured["model"] = response_model
        return response_model(artifact="Total = SUM ( T[Amount] )")

    monkeypatch.setattr(runtime, "generate", generate)
    request = "Write a DAX measure named Total summing T[Amount]."
    list(runtime.stream_chat(request, system_prompt="s", worked_examples=False))
    assert captured["model"].worked_examples is False
    list(runtime.stream_chat(request, system_prompt="s"))
    assert getattr(captured["model"], "worked_examples", True) is True


def test_adaptive_runtime_forwards_the_flag(monkeypatch):
    received = {}

    class Child:
        _active = None

        def stream_chat(self, *args, **kwargs):
            received.update(kwargs)
            yield "ok"

    runtime = AdaptiveQwenRuntime.__new__(AdaptiveQwenRuntime)
    import threading

    runtime._lock = threading.Lock()
    runtime.busy_wait_seconds = 1
    monkeypatch.setattr(runtime, "_pick", lambda: Child(), raising=False)
    monkeypatch.setattr(runtime, "_record_selection", lambda _model: None, raising=False)
    assert list(runtime.stream_chat("m", system_prompt="s", worked_examples=False)) == ["ok"]
    assert received["worked_examples"] is False


def test_legacy_ollama_runtime_accepts_the_flag():
    from inspect import signature

    from ai_powerbi_analyst.llm.runtime import OllamaModelRuntime

    assert signature(OllamaModelRuntime.stream_chat).parameters["worked_examples"].default is True


def test_code_output_budget_reaches_guarded_runtime_without_mutating_settings(
    monkeypatch,
    tmp_path,
):
    runtime, seen = _runtime(monkeypatch, tmp_path)
    original = runtime.settings.chat_output_tokens
    list(
        runtime.stream_chat(
            "Create a Python function",
            system_prompt="s",
            worked_examples=False,
            max_output_tokens=768,
        )
    )
    assert seen["budgets"] == [768]
    assert runtime.settings.chat_output_tokens == original


@pytest.mark.parametrize("budget", [0, 63, 1025, True, 768.0])
def test_invalid_chat_budget_is_rejected_before_inference(monkeypatch, tmp_path, budget):
    runtime, seen = _runtime(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="budget"):
        list(runtime.stream_chat("Explain", system_prompt="s", max_output_tokens=budget))
    assert seen["messages"] == []
