from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_powerbi_analyst.llm import model_install
from ai_powerbi_analyst.llm.model_install import ModelInstaller, ModelTarget
from ai_powerbi_analyst.paths import MODEL_ROOT_ENV, normalize_profile_config, user_model_root

BLOB = bytes(range(256)) * 4096  # 1 MiB of deterministic "model" bytes
DIGEST = hashlib.sha256(BLOB).hexdigest()


class _Blob(BaseHTTPRequestHandler):
    honour_range = True
    slow = False

    def do_GET(self):  # noqa: N802 - http.server API
        start = 0
        header = self.headers.get("Range")
        if header and self.honour_range:
            start = int(header.split("=")[1].split("-")[0])
            self.send_response(206)
        else:
            self.send_response(200)
        body = BLOB[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        for offset in range(0, len(body), 65536):
            if self.slow:
                threading.Event().wait(0.05)
            try:
                self.wfile.write(body[offset : offset + 65536])
            except (ConnectionError, OSError):
                return

    def log_message(self, *_args):
        pass


@pytest.fixture
def server():
    handler = type("Handler", (_Blob,), {})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield handler, f"http://127.0.0.1:{httpd.server_address[1]}/blob"
    httpd.shutdown()


def _target(tmp_path: Path, url: str | None = None) -> ModelTarget:
    return ModelTarget(
        path=tmp_path / "models" / "blobs" / f"sha256-{DIGEST}",
        sha256=DIGEST,
        size=len(BLOB),
        url=url,
        label="Standard · Qwen 4B · text only",
    )


@pytest.fixture(autouse=True)
def no_local_ollama(tmp_path, monkeypatch):
    # Never pick up a real Ollama store on the test machine.
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "no-ollama"))


def test_packaged_profile_keeps_its_model_and_pin_in_the_user_store(tmp_path, monkeypatch):
    monkeypatch.setenv(MODEL_ROOT_ENV, str(tmp_path / "user-models"))
    config = tmp_path / "install" / "runtime" / "profiles" / "standard.json"
    cfg = {
        "model_location": "user",
        "model": "blobs\\sha256-abc",
        "model_store": ".",
        "server": "ollama\\lib\\ollama\\llama-server.exe",
        "hashes": {"blobs\\sha256-abc": "abc", "ollama\\lib\\ollama\\llama-server.exe": "srv"},
    }
    result = normalize_profile_config(cfg, config)
    model = str((tmp_path / "user-models" / "blobs" / "sha256-abc").resolve())
    assert result["model"] == model and result["hashes"][model] == "abc"
    assert result["model_store"] == str((tmp_path / "user-models").resolve())
    runtime = tmp_path / "install" / "runtime"
    server = str((runtime / "ollama/lib/ollama/llama-server.exe").resolve())
    assert result["server"] == server and result["hashes"][server] == "srv"
    # Without the marker a relative model stays in the install payload (older bundles).
    legacy = normalize_profile_config({**cfg, "model_location": None}, config)
    assert legacy["model"].startswith(str((tmp_path / "install" / "runtime").resolve()))


def test_user_model_root_defaults_to_local_app_data(monkeypatch, tmp_path):
    monkeypatch.delenv(MODEL_ROOT_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert user_model_root() == tmp_path / "AI Power BI Analyst" / "models"


def test_missing_model_reports_install_needed(tmp_path):
    status = ModelInstaller(_target(tmp_path, "https://example.invalid")).status()
    assert status["state"] == "missing" and status["installed"] is False
    assert status["download_available"] is True and status["bytes_total"] == len(BLOB)


def test_duplicate_start_returns_current_install_without_deadlock(tmp_path, monkeypatch):
    source = tmp_path / "model.gguf"
    source.write_bytes(BLOB)
    installer = ModelInstaller(_target(tmp_path))
    entered, release = threading.Event(), threading.Event()
    copy = installer._copy

    def delayed_copy(path, kind):
        entered.set()
        assert release.wait(5)
        copy(path, kind)

    monkeypatch.setattr(installer, "_copy", delayed_copy)
    installer.start("file", str(source))
    assert entered.wait(2)
    worker = installer._thread
    replies = []
    duplicate = threading.Thread(
        target=lambda: replies.append(installer.start("file", str(source))), daemon=True
    )
    try:
        duplicate.start()
        duplicate.join(1)
        assert not duplicate.is_alive(), "a repeated start must not acquire the lock twice"
        assert replies[0]["running"] is True and installer._thread is worker
    finally:
        release.set()
    assert installer.wait(5)["installed"] is True


def test_existing_model_is_checked_once_in_background_and_changes_invalidate_it(
    tmp_path, monkeypatch
):
    target = _target(tmp_path)
    target.path.parent.mkdir(parents=True)
    target.path.write_bytes(BLOB)
    installer = ModelInstaller(target)
    entered, release = threading.Event(), threading.Event()
    calls = []
    hash_file = installer._hash_file

    def delayed_hash(path, limit=None):
        calls.append(path)
        entered.set()
        assert release.wait(5)
        return hash_file(path, limit)

    monkeypatch.setattr(installer, "_hash_file", delayed_hash)
    status = installer.status()
    assert entered.wait(2)
    assert status["state"] == "verifying" and status["installed"] is False
    assert status["source"] == "existing" and status["running"] is True
    for _ in range(20):
        assert installer.status()["installed"] is False
    assert len(calls) == 1
    release.set()
    assert installer.wait(5)["installed"] is True
    for _ in range(20):
        assert installer.status()["installed"] is True
    assert len(calls) == 1
    target.path.write_bytes(bytes(len(BLOB)))  # same size cannot establish trust
    installer.status()
    status = installer.wait(5)
    assert status["state"] == "damaged" and status["installed"] is False
    assert "fingerprint" in status["error"] and "Delete" not in status["error"]
    for _ in range(20):
        assert installer.status()["state"] == "damaged"
    assert len(calls) == 2


@pytest.mark.parametrize("changed_after_cancel", [False, True])
def test_cancelled_existing_model_check_waits_for_retry_or_a_changed_file(
    tmp_path, monkeypatch, changed_after_cancel
):
    target = _target(tmp_path)
    target.path.parent.mkdir(parents=True)
    target.path.write_bytes(BLOB)
    installer = ModelInstaller(target)
    entered = threading.Event()
    calls = []
    hash_file = installer._hash_file

    def cancellable_hash(path, limit=None):
        calls.append(path)
        if len(calls) == 1:
            entered.set()
            assert installer._cancel.wait(5)
        return hash_file(path, limit)

    monkeypatch.setattr(installer, "_hash_file", cancellable_hash)
    installer.status()
    assert entered.wait(2)
    assert installer.cancel()["state"] == "cancelled"
    for _ in range(20):
        status = installer.status()
        assert status["state"] == "cancelled" and status["installed"] is False
    assert len(calls) == 1 and target.path.read_bytes() == BLOB
    if changed_after_cancel:
        target.path.write_bytes(bytes(len(BLOB)))
        installer.status()
        status = installer.wait(5)
        assert status["state"] == "damaged" and status["installed"] is False
        assert len(calls) == 2
        return
    installer.start("auto")
    status = installer.wait(5)
    assert status["installed"] is True and status["source"] == "existing"
    assert len(calls) == 2


def test_file_changed_during_existing_check_is_never_marked_installed(tmp_path, monkeypatch):
    target = _target(tmp_path)
    target.path.parent.mkdir(parents=True)
    target.path.write_bytes(BLOB)
    installer = ModelInstaller(target)
    hash_file = installer._hash_file
    calls = []

    def changing_hash(path, limit=None):
        digest = hash_file(path, limit)
        calls.append(path)
        if len(calls) == 1:
            target.path.write_bytes(bytes(len(BLOB)))
        return digest

    monkeypatch.setattr(installer, "_hash_file", changing_hash)
    installer.status()
    status = installer.wait(5)
    assert status["installed"] is False
    assert installer.wait(5)["state"] == "damaged"


def test_installed_status_waits_for_the_active_operation_to_finish(tmp_path, monkeypatch):
    target = _target(tmp_path)
    source = tmp_path / "good.gguf"
    source.write_bytes(BLOB)
    installer = ModelInstaller(target)
    entered, release = threading.Event(), threading.Event()
    update = installer._update

    def delayed_completion(**values):
        if values.get("state") == "installed":
            entered.set()
            assert release.wait(5)
        update(**values)

    monkeypatch.setattr(installer, "_update", delayed_completion)
    installer.start("file", str(source))
    assert entered.wait(2)
    try:
        status = installer.status()
        assert status["running"] is True and status["installed"] is False
    finally:
        release.set()
    assert installer.wait(5)["installed"] is True


def test_a_failed_replacement_preserves_the_existing_damaged_model(tmp_path):
    target = _target(tmp_path)
    original = bytes(len(BLOB))
    target.path.parent.mkdir(parents=True)
    target.path.write_bytes(original)
    replacement = tmp_path / "wrong.gguf"
    replacement.write_bytes(b"x" * len(BLOB))
    installer = ModelInstaller(target)
    installer.status()
    assert installer.wait(5)["state"] == "damaged"
    installer.start("file", str(replacement))
    status = installer.wait(5)
    assert status["state"] == "failed" and status["installed"] is False
    assert target.path.read_bytes() == original
    assert not target.path.with_name(target.path.name + ".partial").exists()
    replacement.write_bytes(BLOB)
    installer.start("file", str(replacement))
    assert installer.wait(5)["installed"] is True
    assert target.path.read_bytes() == BLOB


def test_repair_replaces_existing_file_only_after_the_replacement_is_verified(
    tmp_path, server, monkeypatch
):
    _handler, url = server
    target = _target(tmp_path, url)
    original = bytes(len(BLOB))
    target.path.parent.mkdir(parents=True)
    target.path.write_bytes(original)
    installer = ModelInstaller(target)
    installer.status()
    assert installer.wait(5)["state"] == "damaged"
    replace = model_install.os.replace
    promotions = []

    def checked_replace(partial, destination):
        assert target.path.read_bytes() == original
        assert hashlib.sha256(Path(partial).read_bytes()).hexdigest() == DIGEST
        promotions.append(destination)
        replace(partial, destination)

    monkeypatch.setattr(model_install.os, "replace", checked_replace)
    installer.start("download")
    assert installer.wait(5)["installed"] is True
    assert promotions == [target.path] and target.path.read_bytes() == BLOB


def test_tampered_partial_file_does_not_replace_the_original(tmp_path, monkeypatch):
    target = _target(tmp_path)
    original = bytes(len(BLOB))
    target.path.parent.mkdir(parents=True)
    target.path.write_bytes(original)
    source = tmp_path / "good.gguf"
    source.write_bytes(BLOB)
    installer = ModelInstaller(target)
    installer.status()
    assert installer.wait(5)["state"] == "damaged"
    finish = installer._finish

    def tampered_finish(partial, digest):
        Path(partial).write_bytes(b"x" * len(BLOB))
        finish(partial, digest)

    monkeypatch.setattr(installer, "_finish", tampered_finish)
    installer.start("file", str(source))
    status = installer.wait(5)
    assert status["state"] == "failed" and status["installed"] is False
    assert target.path.read_bytes() == original


def test_file_changed_at_promotion_is_not_cached_as_installed(tmp_path, monkeypatch):
    target = _target(tmp_path)
    source = tmp_path / "good.gguf"
    source.write_bytes(BLOB)
    installer = ModelInstaller(target)
    replace = model_install.os.replace

    def changed_replace(partial, destination):
        replace(partial, destination)
        target.path.write_bytes(bytes(len(BLOB)))

    monkeypatch.setattr(model_install.os, "replace", changed_replace)
    installer.start("file", str(source))
    status = installer.wait(5)
    assert status["installed"] is False
    assert installer.wait(5)["state"] == "damaged"


def test_a_verified_existing_file_is_not_replaced_by_an_invalid_selected_file(tmp_path):
    target = _target(tmp_path)
    target.path.parent.mkdir(parents=True)
    target.path.write_bytes(BLOB)
    wrong = tmp_path / "wrong.gguf"
    wrong.write_bytes(bytes(len(BLOB)))
    installer = ModelInstaller(target)
    installer.status()
    assert installer.wait(5)["installed"] is True
    installer.start("file", str(wrong))
    assert installer.wait(5)["installed"] is True
    assert target.path.read_bytes() == BLOB


def test_auto_repair_downloads_when_an_existing_ollama_copy_is_corrupt(
    tmp_path, server, monkeypatch
):
    _handler, url = server
    target = _target(tmp_path, url)
    store = tmp_path / "ollama"
    (store / "blobs").mkdir(parents=True)
    (store / "blobs" / f"sha256-{DIGEST}").write_bytes(bytes(len(BLOB)))
    monkeypatch.setenv("OLLAMA_MODELS", str(store))
    installer = ModelInstaller(target)
    installer.start("auto")
    status = installer.wait(5)
    assert status["installed"] is True and status["source"] == "download"
    assert target.path.read_bytes() == BLOB


def test_cancelled_final_download_check_keeps_original_and_retries_without_network(
    tmp_path, server, monkeypatch
):
    _handler, url = server
    target = _target(tmp_path, url)
    original = bytes(len(BLOB))
    target.path.parent.mkdir(parents=True)
    target.path.write_bytes(original)
    installer = ModelInstaller(target)
    installer.status()
    assert installer.wait(5)["state"] == "damaged"
    entered = threading.Event()
    finish = installer._finish
    calls = []

    def cancellable_finish(partial, digest):
        calls.append(partial)
        if len(calls) == 1:
            entered.set()
            assert installer._cancel.wait(5)
        finish(partial, digest)

    monkeypatch.setattr(installer, "_finish", cancellable_finish)
    installer.start("download")
    assert entered.wait(3)
    assert installer.cancel()["state"] == "cancelled"
    assert target.path.read_bytes() == original
    assert target.path.with_name(target.path.name + ".partial").read_bytes() == BLOB

    def no_network(*_args, **_kwargs):
        raise AssertionError("a complete partial file should be checked and promoted locally")

    monkeypatch.setattr(model_install.urllib.request, "urlopen", no_network)
    installer.start("download")
    assert installer.wait(5)["installed"] is True
    assert len(calls) == 2 and target.path.read_bytes() == BLOB


def test_local_ollama_copy_installs_without_download(tmp_path, monkeypatch):
    store = tmp_path / "ollama"
    (store / "blobs").mkdir(parents=True)
    (store / "blobs" / f"sha256-{DIGEST}").write_bytes(BLOB)
    monkeypatch.setenv("OLLAMA_MODELS", str(store))
    installer = ModelInstaller(_target(tmp_path, url=None))
    assert installer.status()["local_copy"]
    status = installer.start("auto")
    status = installer.wait(10)
    assert status["state"] == "installed" and status["source"] == "ollama"
    assert installer.target.path.read_bytes() == BLOB


def test_a_file_with_the_wrong_fingerprint_is_never_installed(tmp_path):
    wrong = tmp_path / "wrong.gguf"
    wrong.write_bytes(bytes(len(BLOB)))  # right size, wrong bytes
    installer = ModelInstaller(_target(tmp_path))
    installer.start("file", str(wrong))
    status = installer.wait(10)
    assert status["state"] == "failed" and "fingerprint" in status["error"]
    assert not installer.target.path.exists()
    assert not list(installer.target.path.parent.glob("*.partial"))


def test_a_file_of_the_wrong_size_is_rejected_before_copying(tmp_path):
    short = tmp_path / "short.gguf"
    short.write_bytes(b"too small")
    installer = ModelInstaller(_target(tmp_path))
    installer.start("file", str(short))
    status = installer.wait(10)
    assert status["state"] == "failed" and "bytes" in status["error"]


def test_download_installs_exactly_the_pinned_bytes(tmp_path, server):
    _handler, url = server
    installer = ModelInstaller(_target(tmp_path, url))
    installer.start("download")
    status = installer.wait(20)
    assert status["state"] == "installed", status
    assert hashlib.sha256(installer.target.path.read_bytes()).hexdigest() == DIGEST


@pytest.mark.parametrize("honour_range", [True, False])
def test_download_resumes_or_restarts_cleanly(tmp_path, server, honour_range):
    handler, url = server
    handler.honour_range = honour_range
    target = _target(tmp_path, url)
    target.path.parent.mkdir(parents=True)
    Path(str(target.path) + ".partial").write_bytes(BLOB[: len(BLOB) // 3])
    installer = ModelInstaller(target)
    installer.start("download")
    status = installer.wait(20)
    assert status["state"] == "installed", status
    assert target.path.read_bytes() == BLOB


def test_cancel_keeps_the_partial_download_for_a_later_resume(tmp_path, server, monkeypatch):
    handler, url = server
    handler.slow = True
    monkeypatch.setattr(model_install, "_CHUNK", 65536)  # progress per slow server write
    installer = ModelInstaller(_target(tmp_path, url))
    installer.start("download")
    for _ in range(200):
        if installer.status().get("bytes_done"):
            break
        threading.Event().wait(0.02)
    status = installer.cancel()
    assert status["state"] == "cancelled" and status["installed"] is False
    assert Path(str(installer.target.path) + ".partial").exists()


def test_not_enough_disk_space_fails_with_a_clear_message(tmp_path, server, monkeypatch):
    _handler, url = server

    class Usage:
        free = 10

    monkeypatch.setattr(model_install.shutil, "disk_usage", lambda _path: Usage)
    installer = ModelInstaller(_target(tmp_path, url))
    installer.start("download")
    status = installer.wait(10)
    assert status["state"] == "failed" and "disk space" in status["error"]


def test_install_api_reports_and_runs_the_first_launch_install(tmp_path, monkeypatch, server):
    from ai_powerbi_analyst.studio.api import create_app
    from ai_powerbi_analyst.studio.service import StudioService

    _handler, url = server
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    target = _target(tmp_path, url)
    monkeypatch.setattr(service.model, "model_target", lambda: target, raising=False)
    headers = {"Authorization": "Bearer install-api-test-token"}
    with TestClient(create_app(service, "install-api-test-token")) as client:
        assert client.get("/v1/model/install").status_code == 401
        before = client.get("/v1/model/install", headers=headers).json()
        assert before["state"] == "missing" and before["installed"] is False
        refused = client.post("/v1/model/install", headers=headers, json={"source": "file"})
        assert refused.status_code == 409 and "Choose" in refused.json()["detail"]
        started = client.post("/v1/model/install", headers=headers, json={"source": "download"})
        assert started.status_code == 200
        for _ in range(400):
            status = client.get("/v1/model/install", headers=headers).json()
            if not status["running"]:
                break
            threading.Event().wait(0.05)
        assert status["state"] == "installed", json.dumps(status)
    assert target.path.read_bytes() == BLOB
