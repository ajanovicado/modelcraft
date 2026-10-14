import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from ai_powerbi_analyst.llm.factory import create_model_runtime
from ai_powerbi_analyst.llm.managed_ollama import ManagedOllamaModelRuntime
from ai_powerbi_analyst.llm.managed_ollama_backend import OllamaServer
from ai_powerbi_analyst.llm.runtime import LocalModelError
from ai_powerbi_analyst.settings import ModelSettings


class Reply(BaseModel):
    artifact: str


def test_guarded_factory_and_package_identity(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(dict(tag="qwen", package_sha256="abc", context_tokens=4096)))
    model = create_model_runtime(
        ModelSettings(name="qwen", selected_digest="sha256:abc", runtime_config_path=str(path))
    )
    assert isinstance(model, ManagedOllamaModelRuntime)
    assert model._configuration()[1]["tag"] == "qwen"
    model.settings = model.settings.model_copy(update={"selected_digest": "sha256:wrong"})
    with pytest.raises(LocalModelError, match="digest"):
        model._configuration()


def test_semantic_failure_repairs_once_and_never_escapes(monkeypatch, tmp_path):
    model = ManagedOllamaModelRuntime(ModelSettings(), evidence_root=tmp_path)
    calls = []

    class Server:
        def complete(self, messages, **kwargs):
            calls.append(messages.copy())
            output = json.dumps({"artifact": "Total = SUMX(T)"})
            return dict(
                output=output,
                complete=True,
                error=None,
                tokens=9,
                prompt_tokens=50,
                seconds=0.1,
                native_timings={},
            )

    @contextmanager
    def session():
        yield Server(), tmp_path, tmp_path

    monkeypatch.setattr(model, "_session", session)
    monkeypatch.setattr(model, "_references", lambda *_: ("", []))
    with pytest.raises(LocalModelError, match="SUMX requires 2 arguments"):
        model.generate("system", "Write a DAX measure total", Reply)
    assert len(calls) == 2
    assert (tmp_path / "attempt-0.json").exists() and (tmp_path / "attempt-1.json").exists()


def test_zero_repairs_remains_zero(monkeypatch, tmp_path):
    model = ManagedOllamaModelRuntime(ModelSettings(), evidence_root=tmp_path)
    calls = []

    class Server:
        def complete(self, *a, **k):
            calls.append(1)
            return dict(
                output='{"artifact":"SUMX(T)"}',
                complete=True,
                error=None,
                tokens=1,
                prompt_tokens=1,
                seconds=0.1,
                native_timings={},
            )

    @contextmanager
    def session():
        yield Server(), tmp_path, tmp_path

    monkeypatch.setattr(model, "_session", session)
    monkeypatch.setattr(model, "_references", lambda *_: ("", []))
    with pytest.raises(LocalModelError):
        model.generate("system", "Write DAX", Reply, repair_attempts=0)
    assert len(calls) == 1


def test_stream_callback_and_fixed_cpu_budget(monkeypatch, tmp_path):
    server = OllamaServer(
        {"tag": "qwen", "context_tokens": 4096, "threads": 4}, lambda: {}, tmp_path
    )
    server.endpoint = "http://127.0.0.1:12345"
    requests = []

    class Response:
        fp = SimpleNamespace(raw=SimpleNamespace(_sock=None))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def __iter__(self):
            for row in [
                dict(message={"content": "hello"}),
                dict(
                    message={"content": " world"},
                    done=True,
                    done_reason="stop",
                    prompt_eval_count=20,
                    eval_count=2,
                    eval_duration=1000000,
                ),
            ]:
                yield json.dumps(row).encode()

    def open_response(request, **kwargs):
        requests.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", open_response)
    chunks = []
    result = server.complete([dict(role="user", content="hello")], on_text=chunks.append)
    assert chunks == ["hello", " world"] and result["complete"]
    assert requests[0]["options"]["num_gpu"] == 0
    assert requests[0]["options"]["num_ctx"] == 4096
    assert requests[0]["think"] is False
    with pytest.raises(ValueError, match="context bound"):
        server.complete([dict(role="user", content="x" * 4000)])
    assert len(requests) == 1
    server.stop()
    with pytest.raises(RuntimeError, match="closed"):
        server.complete([dict(role="user", content="hello")])
