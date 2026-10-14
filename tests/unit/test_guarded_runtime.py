import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from ai_powerbi_analyst.llm.managed_ollama import ManagedOllamaModelRuntime
from ai_powerbi_analyst.llm.runtime import LocalModelError
from ai_powerbi_analyst.settings import ModelSettings


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: int


def fake_runtime(monkeypatch, tmp_path, outputs):
    runtime = ManagedOllamaModelRuntime(ModelSettings())
    calls = []

    class Server:
        def complete(self, messages, **kwargs):
            calls.append((list(messages), kwargs))
            return dict(
                raw=outputs[len(calls) - 1],
                output=outputs[len(calls) - 1],
                complete=True,
                error=None,
                tokens=2,
                prompt_tokens=3,
                seconds=0.1,
                native_timings={},
            )

    @contextmanager
    def session():
        yield Server(), tmp_path, tmp_path

    monkeypatch.setattr(runtime, "_session", session)
    monkeypatch.setattr(runtime, "_references", lambda *_: ("\nReference example", ["C-1"]))
    return runtime, calls


def test_schema_failure_is_repaired_once_with_evidence(monkeypatch, tmp_path):
    runtime, calls = fake_runtime(monkeypatch, tmp_path, ['{"answer":"wrong"}', '{"answer":7}'])
    assert runtime.generate("system", "question", Answer).answer == 7
    assert len(calls) == 2 and "validation error" in calls[1][0][-1]["content"]
    assert calls[0][1]["thinking"] is False
    assert calls[0][1]["seconds"] == runtime.settings.request_timeout_seconds
    assert json.loads((tmp_path / "attempt-0.json").read_text())["retrieved"] == ["C-1"]
    assert (tmp_path / "attempt-1.json").exists()


def test_invalid_artifact_never_escapes_schema_or_unbounded_retry(monkeypatch, tmp_path):
    runtime, calls = fake_runtime(monkeypatch, tmp_path, ["{}", "{}"])
    with pytest.raises(LocalModelError, match="strict schema"):
        runtime.generate("system", "question", Answer)
    assert len(calls) == 2


def test_existing_receipt_is_not_overwritten(tmp_path):
    path = tmp_path / "receipt.json"
    ManagedOllamaModelRuntime._save(path, {"first": True})
    with pytest.raises(FileExistsError):
        ManagedOllamaModelRuntime._save(path, {"first": False})
    assert json.loads(path.read_text()) == {"first": True}


def test_removed_provider_is_rejected():
    with pytest.raises(ValueError, match="provider"):
        ModelSettings(provider="llama.cpp")


def test_startup_guard_produces_failure_receipt(monkeypatch, tmp_path):
    import powerbi_rag_runtime.guards as guards

    cfg_path = tmp_path / "runtime" / "config.json"
    cfg_path.parent.mkdir()
    (tmp_path / "training").mkdir()
    runtime = ManagedOllamaModelRuntime(ModelSettings(), tmp_path / "evidence")
    monkeypatch.setattr(runtime, "_configuration", lambda: (cfg_path, {}))
    monkeypatch.setattr(
        guards,
        "resources",
        lambda _: dict(
            available=7 * 1024**3, disk_free=20 * 1024**3, ac=True, swap_used=0, native_swapouts=0
        ),
    )
    with pytest.raises(LocalModelError, match="8 GiB"):
        runtime.generate("system", "question", Answer)
    failure = next((tmp_path / "evidence").rglob("failure.json"))
    assert "8 GiB" in failure.read_text()
    assert runtime._active is None


def test_corpus_relocation_preserves_pinned_synthetic_retrieval_inputs(tmp_path):
    from ai_powerbi_analyst.llm.contract_ollama import ContractOllamaModelRuntime
    from ai_powerbi_analyst.settings import load_settings

    root = Path(__file__).resolve().parents[2]
    settings = load_settings(root / "config/defaults.yaml").model
    assert settings.selected_digest is not None
    examples = json.loads((root / "src/ai_powerbi_analyst/llm/artifact_examples.json").read_text())
    corpus_bytes = "".join(
        json.dumps({**row, "family": row["domain"] + " " + row["keywords"]}) + "\n"
        for row in examples
    ).encode()
    prompts = [row["brief"] for row in examples[:5]]
    observed = []
    for name in ("original", "relocated"):
        directory = tmp_path / name
        directory.mkdir()
        corpus = directory / "worked-examples.jsonl"
        corpus.write_bytes(corpus_bytes)
        cfg = directory / "profile.json"
        cfg.write_text(
            json.dumps(
                {
                    "tag": settings.name,
                    "package_sha256": settings.selected_digest.removeprefix("sha256:"),
                    "context_tokens": 4096,
                    "corpus": str(corpus),
                    "hashes": {str(corpus): hashlib.sha256(corpus_bytes).hexdigest()},
                }
            )
        )
        model = ContractOllamaModelRuntime(
            settings.model_copy(update={"runtime_config_path": str(cfg)})
        )
        observed.append([model._references(root, prompt) for prompt in prompts])
    assert all(refs and ids for refs, ids in observed[0])
    assert observed[0] == observed[1]


def test_changed_or_unpinned_corpus_is_rejected(monkeypatch, tmp_path):
    corpus = tmp_path / "worked-examples.jsonl"
    corpus.write_text("{}\n")
    model = ManagedOllamaModelRuntime(ModelSettings())
    cfg = {"corpus": str(corpus), "hashes": {}}
    monkeypatch.setattr(model, "_configuration", lambda: (tmp_path / "config.json", cfg))
    with pytest.raises(LocalModelError, match="not pinned or changed"):
        model._references(tmp_path, "request")
    cfg["hashes"][str(corpus)] = hashlib.sha256(b"original bytes").hexdigest()
    with pytest.raises(LocalModelError, match="not pinned or changed"):
        model._references(tmp_path, "request")
