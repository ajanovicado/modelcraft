from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from ai_powerbi_analyst.studio.api import create_app
from ai_powerbi_analyst.studio.contracts import CreateWorkspaceRequest, IngestRequest
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH, StateConflictError

TOKEN = "isolated-release-test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def test_ingestion_retry_replays_without_new_files_or_events(
    service: StudioService,
    tmp_path: Path,
) -> None:
    source = tmp_path / "orders.csv"
    source.write_text("Region,Sales\nEast,100\nWest,200\n", encoding="utf-8")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Retry QA",
            selected_folder=str(tmp_path),
            idempotency_key="create-retry-test",
            expected_base_hash=EMPTY_HASH,
        )
    )
    request = IngestRequest(
        workspace_id=workspace.id,
        item_paths=[str(source)],
        idempotency_key="import-retry-test",
        expected_base_hash=workspace.base_hash,
    )
    snapshot = service.ingest(request)
    events_before = service.events(workspace.id)
    files_before = sorted(str(path) for path in service.state_root.rglob("*"))
    assert service.ingest(request) == snapshot
    assert service.workspace(workspace.id).active_snapshot_id == snapshot.id
    assert service.events(workspace.id) == events_before
    assert sorted(str(path) for path in service.state_root.rglob("*")) == files_before
    with pytest.raises(StateConflictError, match="another request"):
        service.ingest(request.model_copy(update={"item_paths": [str(tmp_path / "other.csv")]}))


def test_production_rejects_development_origins_unless_explicitly_enabled(
    service: StudioService,
) -> None:
    preflight = {"Origin": "http://127.0.0.1:5173", "Access-Control-Request-Method": "GET"}
    with TestClient(create_app(service, TOKEN)) as client:
        assert client.options("/v1/ping", headers=preflight).status_code == 400
    with TestClient(
        create_app(service, TOKEN, dev_browser_origins=[preflight["Origin"]])
    ) as client:
        assert client.options("/v1/ping", headers=preflight).status_code == 200
    with pytest.raises(ValueError, match="loopback"):
        create_app(service, TOKEN, dev_browser_origins=["http://untrusted.example"])


@pytest.fixture
def service(tmp_path: Path) -> StudioService:
    return StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")


def test_model_recovery_health_is_fresh_authenticated_and_skips_tool_probes(
    service: StudioService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_powerbi_analyst.llm.runtime import ModelHealth

    readings = iter([False, True])

    def model_health() -> ModelHealth:
        ready = next(readings)
        return ModelHealth(
            service_available=ready,
            model_available=ready,
            endpoint="local",
            available_memory_gb=3.7 if ready else 2.98,
        )

    def full_diagnostics() -> None:
        raise AssertionError("Recovery must not launch full tool discovery")

    monkeypatch.setattr(service.model, "health", model_health)
    monkeypatch.setattr(service, "health", full_diagnostics)
    with TestClient(create_app(service, TOKEN)) as client:
        assert client.get("/v1/model-health").status_code == 401
        paused = client.get("/v1/model-health", headers=HEADERS).json()
        recovered = client.get("/v1/model-health", headers=HEADERS).json()
    assert paused["model_available"] is False
    assert recovered["model_available"] is True
    assert recovered["available_memory_gb"] == 3.7


def test_api_auth_and_unknown_fields_fail_closed(service: StudioService) -> None:
    with TestClient(create_app(service, TOKEN)) as client:
        assert client.get("/v1/ping").status_code == 401
        assert client.get("/v1/ping", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/v1/ping", headers={"Authorization": "Bearer x"}).status_code == 401
        assert (
            client.get(
                "/v1/ping",
                headers={"Authorization": f"Bearer {TOKEN}extra"},
            ).status_code
            == 401
        )
        ping = client.get("/v1/ping", headers=HEADERS).json()
        assert ping["ready"] is True
        assert {"chat-context", "draft-restore"}.issubset(ping["features"])
        response = client.post(
            "/v1/workspaces",
            headers=HEADERS,
            json={
                "name": "QA",
                "expected_base_hash": EMPTY_HASH,
                "idempotency_key": "release-test-unknown",
                "execute_shell": "never",
            },
        )
        assert response.status_code == 422
        assert service.list_workspaces() == []
        assert client.get("/openapi.json", headers=HEADERS).status_code == 404


@pytest.mark.parametrize("model_failure", [False, True])
def test_chat_stream_hides_unexpected_exception_details(
    service: StudioService, model_failure: bool
) -> None:
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Stream QA",
            idempotency_key="stream-error-workspace",
            expected_base_hash=EMPTY_HASH,
        )
    )

    def boom(_request: object, _cancel: Event) -> object:
        if model_failure:
            from ai_powerbi_analyst.llm.runtime import LocalModelError

            raise LocalModelError("model internal path C:\\secret\\spill")
        raise RuntimeError("duckdb internal path C:\\secret\\spill")

    service.chat_events = boom  # type: ignore[method-assign]
    with (
        TestClient(create_app(service, TOKEN)) as client,
        client.stream(
            "POST",
            "/v1/chat/stream",
            headers=HEADERS,
            json={"workspace_id": workspace.id, "message": "hello"},
        ) as response,
    ):
        assert response.status_code == 200
        body = "".join(response.iter_text())
    assert "event: error" in body
    assert "duckdb" not in body.lower()
    assert "secret" not in body.lower()
    assert "could not complete this" in body


def test_chat_stream_never_blames_memory_and_hides_diagnostics(
    service: StudioService,
) -> None:
    from ai_powerbi_analyst.llm.runtime import LocalModelError

    def blocked(_request: object, _cancel: Event) -> object:
        raise LocalModelError(
            'Resource guard (8 GiB RAM/disk, AC required): {"available": 7516192768, '
            '"disk_free": 17179869184, "ac": true, "private_path": "secret"}'
        )

    service.chat_events = blocked  # type: ignore[method-assign]
    with TestClient(create_app(service, TOKEN)) as client:
        response = client.post(
            "/v1/chat/stream", headers=HEADERS, json={"workspace_id": "test", "message": "hello"}
        )
    assert "event: error" in response.text
    assert "Check model diagnostics in Settings" in response.text
    assert "paused" not in response.text
    assert "GiB" not in response.text
    assert "secret" not in response.text


# Memory, disk, power and paging never pause Qwen 4B, so no message blames them.
@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        (
            'Resource guard (8 GiB RAM/disk, AC required): {"available": 17179869184, '
            '"disk_free": 17179869184, "ac": false}',
            "Check model diagnostics",
        ),
        (
            'Resource guard (3 GiB RAM/disk, AC required): {"available": 17179869184, '
            '"disk_free": 1073741824, "ac": true}',
            "Check model diagnostics",
        ),
        ("New paging exceeded 512 MiB cap: native_swapouts", "Check model diagnostics"),
        ("Resource guard (8 GiB RAM/disk, AC required): secret", "Check model diagnostics"),
        ("Context budget exceeded; shorten the conversation or formula", "too large"),
        ("Local model is busy; only one guarded request is allowed", "already handling"),
    ],
)
def test_runtime_errors_have_safe_messages_that_never_blame_resources(detail, expected):
    from ai_powerbi_analyst.llm.runtime import LocalModelError
    from ai_powerbi_analyst.studio.api import _client_error_message

    message = _client_error_message(LocalModelError(detail))
    assert expected in message
    assert "secret" not in message
    for claim in ("paused", "gib", "plug in", "paging"):
        assert claim not in message.lower()


@pytest.mark.parametrize("origin", ["http://tauri.localhost", "https://tauri.localhost"])
def test_native_webview_origin_can_authenticate(service: StudioService, origin: str) -> None:
    with TestClient(create_app(service, TOKEN)) as client:
        response = client.options(
            "/v1/ping",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        blocked = client.options(
            "/v1/ping",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert blocked.status_code == 400
        assert "access-control-allow-origin" not in blocked.headers


def test_workspace_folder_selection_is_validated_and_idempotent(
    service: StudioService,
    tmp_path: Path,
) -> None:
    data_folder = tmp_path / "data folder"
    data_folder.mkdir()
    (data_folder / "orders.csv").write_text("Region,Sales\nEast,100\n", encoding="utf-8")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="New local workspace",
            idempotency_key="folder-pick-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    body = {
        "workspace_id": workspace.id,
        "folder": str(data_folder),
        "name": "data folder",
        "idempotency_key": "folder-pick-set",
        "expected_base_hash": workspace.base_hash,
    }
    with TestClient(create_app(service, TOKEN)) as client:
        missing = client.post(
            f"/v1/workspaces/{workspace.id}/folder",
            headers=HEADERS,
            json={
                **body,
                "folder": str(tmp_path / "missing"),
                "idempotency_key": "folder-pick-missing",
            },
        )
        assert missing.status_code == 422
        mismatch = client.post(
            f"/v1/workspaces/{workspace.id}/folder",
            headers=HEADERS,
            json={
                **body,
                "workspace_id": "workspace-other",
                "idempotency_key": "folder-pick-mismatch",
            },
        )
        assert mismatch.status_code == 422
        stale = client.post(
            f"/v1/workspaces/{workspace.id}/folder",
            headers=HEADERS,
            json={
                **body,
                "idempotency_key": "folder-pick-stale",
                "expected_base_hash": EMPTY_HASH,
            },
        )
        assert stale.status_code == 409
        response = client.post(f"/v1/workspaces/{workspace.id}/folder", headers=HEADERS, json=body)
        assert response.status_code == 200
        updated = response.json()
        assert updated["selected_folder"] == str(data_folder)
        assert updated["name"] == "data folder"
        assert updated["base_hash"] != workspace.base_hash
        replay = client.post(f"/v1/workspaces/{workspace.id}/folder", headers=HEADERS, json=body)
        assert replay.status_code == 200
        assert replay.json() == updated
        events = client.get(f"/v1/workspaces/{workspace.id}/events", headers=HEADERS).json()
        assert [event["type"] for event in events].count("workspace.folder_selected") == 1
        file_pick = client.post(
            f"/v1/workspaces/{workspace.id}/folder",
            headers=HEADERS,
            json={
                **body,
                "folder": str(data_folder / "orders.csv"),
                "name": "orders",
                "idempotency_key": "folder-pick-file",
                "expected_base_hash": updated["base_hash"],
            },
        )
        assert file_pick.status_code == 200
        picked = file_pick.json()
        assert picked["selected_folder"] == str(data_folder)
        assert picked["name"] == "orders"


def test_browse_lists_directories_and_marks_data_files_selectable(
    service: StudioService,
    tmp_path: Path,
) -> None:
    root = tmp_path / "browse root"
    root.mkdir()
    (root / "nested").mkdir()
    (root / "orders.csv").write_text("Region,Sales\nEast,100\n", encoding="utf-8")
    (root / "notes.txt").write_text("not data", encoding="utf-8")
    (root / ".hidden.csv").write_text("x", encoding="utf-8")
    with TestClient(create_app(service, TOKEN)) as client:
        roots = client.get("/v1/browse", headers=HEADERS)
        assert roots.status_code == 200
        top = roots.json()
        assert top["path"] == ""
        assert all(entry["kind"] == "dir" for entry in top["entries"])
        assert any(entry["name"] == "Home" for entry in top["entries"])
        listing = client.get("/v1/browse", params={"path": str(root)}, headers=HEADERS)
        assert listing.status_code == 200
        result = listing.json()
        assert Path(result["path"]) == root.resolve()
        assert result["parent"] == str(tmp_path.resolve())
        by_name = {entry["name"]: entry for entry in result["entries"]}
        assert by_name["nested"]["kind"] == "dir"
        assert by_name["orders.csv"]["selectable"] is True
        assert by_name["notes.txt"]["selectable"] is False
        assert ".hidden.csv" not in by_name
        via_file = client.get(
            "/v1/browse",
            params={"path": str(root / "orders.csv")},
            headers=HEADERS,
        )
        assert via_file.status_code == 200
        assert Path(via_file.json()["path"]) == root.resolve()
        missing = client.get(
            "/v1/browse",
            params={"path": str(tmp_path / "missing")},
            headers=HEADERS,
        )
        assert missing.status_code == 422


def test_missing_folder_returns_actionable_json_not_an_internal_error(
    service: StudioService,
    tmp_path: Path,
) -> None:
    with TestClient(create_app(service, TOKEN)) as client:
        response = client.post(
            "/v1/inventory", headers=HEADERS, json={"folder": str(tmp_path / "missing")}
        )
        assert response.status_code == 404
        assert "rescan" in response.json()["detail"]


def test_slow_health_does_not_block_ping_or_other_requests(
    service: StudioService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = Event(), Event()

    def slow_health() -> dict[str, bool]:
        entered.set()
        assert release.wait(5)
        return {"ready": True}

    monkeypatch.setattr(service, "health", slow_health)
    with TestClient(create_app(service, TOKEN)) as client, ThreadPoolExecutor() as pool:
        slow = pool.submit(client.get, "/v1/health", headers=HEADERS)
        try:
            assert entered.wait(2)
            ping = pool.submit(client.get, "/v1/ping", headers=HEADERS)
            assert ping.result(timeout=2).status_code == 200
        finally:
            release.set()
        assert slow.result(timeout=3).status_code == 200
