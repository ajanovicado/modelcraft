import importlib.util
import json
import sys
from pathlib import Path

import pytest

from ai_powerbi_analyst.paths import normalize_profile_config, runtime_guard_root


def payload_builder():
    path = Path(__file__).resolve().parents[2] / "scripts" / "build_store_payload.py"
    spec = importlib.util.spec_from_file_location("payload_builder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("native", [True, False])
def test_payload_preserves_server_tree_and_pins(tmp_path, native):
    builder = payload_builder()
    install = tmp_path / "Ollama"
    server = install / ("lib/ollama/llama-server.exe" if native else "ollama.exe")
    model = tmp_path / "models/blobs/model"
    corpus = tmp_path / "corpus.jsonl"
    source = tmp_path / "backend.py"
    files = [install / "ollama.exe", server, install / "lib/ollama/cpu.dll", model, corpus, source]
    for file in files:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(file.name.encode())
    cfg = {
        "backend": "managed-llama-text" if native else "managed-ollama",
        "server": str(server),
        "model": str(model),
        "model_store": str(tmp_path / "models"),
        "corpus": str(corpus),
        "hashes": {str(file): builder.sha256(file) for file in files},
    }
    config = tmp_path / "profile.json"
    config.write_text(json.dumps(cfg))
    out = tmp_path / "bundle/runtime"
    builder.build_profile(config, out, "standard", ("engine.exe", "sealed-engine"))
    profile = out / "profiles/standard.json"
    result = normalize_profile_config(json.loads(profile.read_text()), profile)
    assert Path(result["server"]).read_bytes() == server.name.encode()
    assert result["server"] in result["hashes"]
    assert (out / "ollama/lib/ollama/cpu.dll").is_file()
    assert str(source) not in result["hashes"]
    assert result["hashes"][str((out.parent / "engine.exe").resolve())] == "sealed-engine"
    source.write_text("changed after seal")
    with pytest.raises(SystemExit, match="source seal mismatch"):
        builder.build_profile(config, tmp_path / "bad", "standard", ("engine.exe", "seal"))


def test_sealed_engine_data_inputs_fold_into_the_engine_pin(tmp_path):
    # The Qwen 4B seal also pins report catalogs and uv.lock, which the engine freezes.
    builder = payload_builder()
    server = tmp_path / "Ollama/lib/ollama/llama-server.exe"
    model = tmp_path / "models/blobs/model"
    corpus = tmp_path / "corpus.jsonl"
    catalog = tmp_path / "src/report_visuals.json"
    lock = tmp_path / "uv.lock"
    files = [server, model, corpus, catalog, lock]
    for file in files:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(file.name.encode())
    cfg = {
        "backend": "managed-llama-text",
        "server": str(server),
        "model": str(model),
        "model_store": str(tmp_path / "models"),
        "corpus": str(corpus),
        "hashes": {str(file): builder.sha256(file) for file in files},
    }
    config = tmp_path / "profile.json"
    config.write_text(json.dumps(cfg))
    with pytest.raises(SystemExit, match="unclassified sealed file"):
        builder.build_profile(config, tmp_path / "unbound", "standard", ("engine.exe", "seal"))
    inputs = frozenset(str(file).lower() for file in (catalog, lock))
    out = tmp_path / "bundle/runtime"
    builder.build_profile(config, out, "standard", ("engine.exe", "seal"), inputs)
    pins = json.loads((out / "profiles/standard.json").read_text())["hashes"]
    assert sorted(pins) == [
        "..\\engine.exe",
        "blobs\\model",
        "corpus\\corpus.jsonl",
        "ollama\\lib\\ollama\\llama-server.exe",
    ]


def test_payload_ships_only_the_model_pin_and_its_download_address(tmp_path, monkeypatch):
    # Installers cannot carry the 3.4 GB model: its pin and registry address ship instead.
    builder = payload_builder()
    server = tmp_path / "Ollama/lib/ollama/llama-server.exe"
    model = tmp_path / "models/blobs/sha256-abc"
    corpus = tmp_path / "corpus.jsonl"
    for file in (server, model, corpus):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(file.name.encode())
    cfg = {
        "backend": "managed-llama-text",
        "tag": "qwen3.5:4b-q4_K_M",
        "server": str(server),
        "model": str(model),
        "model_store": str(tmp_path / "models"),
        "corpus": str(corpus),
        "hashes": {str(file): builder.sha256(file) for file in (server, model, corpus)},
    }
    config = tmp_path / "profile.json"
    config.write_text(json.dumps(cfg))
    out = tmp_path / "bundle/runtime"
    builder.build_profile(config, out, "standard", ("engine.exe", "seal"))
    assert not (out / "models").exists()
    shipped = json.loads((out / "profiles/standard.json").read_text())
    digest = builder.sha256(model)
    assert shipped["model_location"] == "user" and shipped["model"] == "blobs\\sha256-abc"
    assert shipped["model_download"] == {
        "url": f"https://registry.ollama.ai/v2/library/qwen3.5/blobs/sha256:{digest}",
        "bytes": model.stat().st_size,
    }
    monkeypatch.setenv("AI_POWERBI_MODEL_ROOT", str(tmp_path / "user"))
    profile = out / "profiles/standard.json"
    resolved = normalize_profile_config(shipped, profile)
    expected = str((tmp_path / "user/blobs/sha256-abc").resolve())
    assert resolved["model"] == expected and resolved["hashes"][expected] == digest


def test_repository_catalog_serves_only_qwen_4b():
    runtime = Path(__file__).resolve().parents[2] / "runtime"
    catalog = json.loads((runtime / "catalog.json").read_text(encoding="utf-8"))
    assert list(catalog["profiles"]) == ["standard"]
    assert [path.name for path in (runtime / "profiles").glob("*.json")] == ["standard.json"]
    assert "coder" in catalog["rejected_profiles"]


def test_frozen_runtime_lock_uses_writable_shared_user_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "user-data"))
    first = runtime_guard_root(tmp_path / "read-only-install/runtime/profiles/standard.json")
    second = runtime_guard_root(tmp_path / "other-install/runtime/profiles/standard.json")
    assert first == second
    assert first.is_dir() and first.is_relative_to(tmp_path / "user-data")


def test_frozen_text_runner_requires_engine_pin(monkeypatch, tmp_path):
    from ai_powerbi_analyst.llm import text_only_backend as backend

    engine = tmp_path / "engine.exe"
    server = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    for file in (engine, server, model):
        file.write_bytes(b"fixture")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(engine))
    runner = object.__new__(backend.TextOnlyServer)
    runner.config = {
        "server": str(server),
        "model": str(model),
        "hashes": {str(f): backend.sha(f) for f in (engine, server, model)},
    }
    runner._verify_files()
    engine.write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        runner._verify_files()
    del runner.config["hashes"][str(engine)]
    with pytest.raises(ValueError, match="not pinned"):
        runner._verify_files()


@pytest.mark.parametrize("change", ["source", "added", "deleted", "engine"])
def test_engine_build_receipt_rejects_stale_inputs(monkeypatch, tmp_path, change):
    path = Path(__file__).resolve().parents[2] / "scripts/sidecar_manifest.py"
    spec = importlib.util.spec_from_file_location("sidecar_manifest", path)
    manifest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manifest)
    source = tmp_path / "src"
    source.mkdir()
    module = source / "app.py"
    module.write_text("original")
    engine = tmp_path / "engine.exe"
    engine.write_bytes(b"original engine")
    monkeypatch.setattr(manifest, "source_files", lambda root: sorted(source.glob("*.py")))
    receipt = {"sources": manifest.capture(tmp_path), "engine_sha256": manifest.digest(engine)}
    engine.with_suffix(".build.json").write_text(json.dumps(receipt))
    manifest.verify_engine(tmp_path, engine)
    if change == "source":
        module.write_text("changed")
    elif change == "added":
        (source / "new.py").write_text("new")
    elif change == "deleted":
        module.unlink()
    else:
        engine.write_bytes(b"different engine")
    with pytest.raises(ValueError, match="Engine"):
        manifest.verify_engine(tmp_path, engine)
