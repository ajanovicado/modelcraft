"""Rename and delete coverage for local workspaces.

The rules under test: both mutations are hash-guarded and idempotent, deleting
requires the stored name back, only app-owned state disappears, and generated
Power BI project folders are never removed outside the engine's state root.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_powerbi_analyst.studio.api import create_app
from ai_powerbi_analyst.studio.contracts import (
    BuildJob,
    CreateWorkspaceRequest,
    IngestRequest,
    StudioBuildState,
)
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH

TOKEN = "isolated-workspace-admin-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def service(tmp_path: Path) -> StudioService:
    return StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")


def create_workspace(service: StudioService, tmp_path: Path, name: str) -> tuple[str, Path]:
    source = tmp_path / "sources"
    source.mkdir(exist_ok=True)
    orders = source / "orders.csv"
    orders.write_text("Region,Sales\nEast,100\nWest,200\n", encoding="utf-8")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name=name,
            selected_folder=str(source),
            idempotency_key=f"create-{name.replace(' ', '-').lower()}",
            expected_base_hash=EMPTY_HASH,
        )
    )
    return workspace.id, orders


def test_rename_is_validated_trimmed_recorded_and_replayable(
    service: StudioService,
    tmp_path: Path,
) -> None:
    workspace_id, _orders = create_workspace(service, tmp_path, "Rename me")
    workspace = service.workspace(workspace_id)
    body = {
        "workspace_id": workspace_id,
        "name": "  Quarterly review  ",
        "idempotency_key": "rename-quarterly",
        "expected_base_hash": workspace.base_hash,
    }
    with TestClient(create_app(service, TOKEN)) as client:
        mismatch = client.post(
            f"/v1/workspaces/{workspace_id}/name",
            headers=HEADERS,
            json={**body, "workspace_id": "workspace-other"},
        )
        assert mismatch.status_code == 422
        blank = client.post(
            f"/v1/workspaces/{workspace_id}/name",
            headers=HEADERS,
            json={**body, "name": "   ", "idempotency_key": "rename-blank"},
        )
        assert blank.status_code == 422
        stale = client.post(
            f"/v1/workspaces/{workspace_id}/name",
            headers=HEADERS,
            json={**body, "idempotency_key": "rename-stale", "expected_base_hash": EMPTY_HASH},
        )
        assert stale.status_code == 409
        unknown = client.post(
            "/v1/workspaces/workspace-missing/name",
            headers=HEADERS,
            json={**body, "workspace_id": "workspace-missing"},
        )
        assert unknown.status_code == 404
        response = client.post(f"/v1/workspaces/{workspace_id}/name", headers=HEADERS, json=body)
        assert response.status_code == 200
        renamed = response.json()
        assert renamed["name"] == "Quarterly review"
        assert renamed["selected_folder"] == workspace.selected_folder
        assert renamed["base_hash"] != workspace.base_hash
        replay = client.post(f"/v1/workspaces/{workspace_id}/name", headers=HEADERS, json=body)
        assert replay.status_code == 200
        assert replay.json() == renamed
        events = client.get(f"/v1/workspaces/{workspace_id}/events", headers=HEADERS).json()
        renames = [event for event in events if event["type"] == "workspace.renamed"]
        assert len(renames) == 1
        assert renames[0]["payload"]["previous_name"] == "Rename me"
        listed = client.get("/v1/workspaces", headers=HEADERS).json()
        assert [item["name"] for item in listed] == ["Quarterly review"]
        # Renaming to the current name is a no-op that records nothing new.
        same = client.post(
            f"/v1/workspaces/{workspace_id}/name",
            headers=HEADERS,
            json={
                **body,
                "idempotency_key": "rename-noop",
                "expected_base_hash": renamed["base_hash"],
            },
        )
        assert same.status_code == 200
        assert same.json() == renamed
        events = client.get(f"/v1/workspaces/{workspace_id}/events", headers=HEADERS).json()
        assert [event["type"] for event in events].count("workspace.renamed") == 1


def test_delete_requires_the_exact_name_and_removes_only_app_state(
    service: StudioService,
    tmp_path: Path,
) -> None:
    workspace_id, orders = create_workspace(service, tmp_path, "Delete me")
    workspace = service.workspace(workspace_id)
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace_id,
            item_paths=[str(orders)],
            idempotency_key="delete-ingest",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace_id)
    service.store.add_message(workspace_id, "user", "hello")
    body = {
        "workspace_id": workspace_id,
        "confirm_name": "Delete me",
        "idempotency_key": "delete-workspace",
        "expected_base_hash": workspace.base_hash,
    }
    with TestClient(create_app(service, TOKEN)) as client:
        wrong_name = client.post(
            f"/v1/workspaces/{workspace_id}/delete",
            headers=HEADERS,
            json={**body, "confirm_name": "delete me", "idempotency_key": "delete-wrong-name"},
        )
        assert wrong_name.status_code == 422
        stale = client.post(
            f"/v1/workspaces/{workspace_id}/delete",
            headers=HEADERS,
            json={**body, "idempotency_key": "delete-stale", "expected_base_hash": EMPTY_HASH},
        )
        assert stale.status_code == 409
        assert [item["id"] for item in client.get("/v1/workspaces", headers=HEADERS).json()] == [
            workspace_id
        ]
        response = client.post(f"/v1/workspaces/{workspace_id}/delete", headers=HEADERS, json=body)
        assert response.status_code == 200
        receipt = response.json()
        assert receipt["id"] == workspace_id
        assert receipt["name"] == "Delete me"
        assert receipt["removed_snapshots"] == 1
        assert receipt["removed_messages"] == 1
        assert receipt["removed_events"] >= 1
        assert receipt["files_untouched"] is True
        assert client.get("/v1/workspaces", headers=HEADERS).json() == []
        assert client.get(f"/v1/workspaces/{workspace_id}", headers=HEADERS).status_code == 404
        assert client.get(f"/v1/snapshots/{snapshot.id}", headers=HEADERS).status_code == 404
        assert client.get(f"/v1/workspaces/{workspace_id}/events", headers=HEADERS).json() == []
        assert client.get(f"/v1/workspaces/{workspace_id}/messages", headers=HEADERS).json() == []
        replay = client.post(f"/v1/workspaces/{workspace_id}/delete", headers=HEADERS, json=body)
        assert replay.status_code == 200
        assert replay.json() == receipt
        reused = client.post(
            f"/v1/workspaces/{workspace_id}/delete",
            headers=HEADERS,
            json={**body, "confirm_name": "Something else"},
        )
        assert reused.status_code == 409
        rename_after = client.post(
            f"/v1/workspaces/{workspace_id}/name",
            headers=HEADERS,
            json={
                "workspace_id": workspace_id,
                "name": "Zombie",
                "idempotency_key": "rename-after-delete",
                "expected_base_hash": workspace.base_hash,
            },
        )
        assert rename_after.status_code == 404
    # The user's own files were never part of the deletion.
    assert orders.exists()
    assert orders.read_text(encoding="utf-8").startswith("Region,Sales")


def test_delete_leaves_other_workspaces_untouched(
    service: StudioService,
    tmp_path: Path,
) -> None:
    keep_id, _orders = create_workspace(service, tmp_path, "Keep me")
    drop_id, _orders = create_workspace(service, tmp_path, "Drop me")
    with TestClient(create_app(service, TOKEN)) as client:
        response = client.post(
            f"/v1/workspaces/{drop_id}/delete",
            headers=HEADERS,
            json={
                "workspace_id": drop_id,
                "confirm_name": "Drop me",
                "idempotency_key": "delete-drop",
                "expected_base_hash": service.workspace(drop_id).base_hash,
            },
        )
        assert response.status_code == 200
        remaining = client.get("/v1/workspaces", headers=HEADERS).json()
        assert [item["id"] for item in remaining] == [keep_id]
        assert remaining[0]["selected_folder"] == service.workspace(keep_id).selected_folder


def test_generated_project_cleanup_never_reaches_outside_the_state_root(
    service: StudioService,
    tmp_path: Path,
) -> None:
    generated = service.state_root / "projects" / "workspace-demo"
    generated.mkdir(parents=True)
    (generated / "workspace-demo.pbip").write_text("{}", encoding="utf-8")
    evidence = service.state_root / "projects" / ".render-evidence" / "build-demo"
    evidence.mkdir(parents=True)
    exported = tmp_path / "exported" / "workspace-demo"
    exported.mkdir(parents=True)
    (exported / "workspace-demo.pbip").write_text("{}", encoding="utf-8")
    now = datetime.now(UTC)
    builds = [
        BuildJob(
            id="build-demo",
            workspace_id="workspace-demo",
            draft_id="draft-demo",
            state=StudioBuildState.COMMITTED,
            project_path=str(generated / "workspace-demo.pbip"),
            created_at=now,
            updated_at=now,
        ),
        BuildJob(
            id="build-exported",
            workspace_id="workspace-demo",
            draft_id="draft-demo",
            state=StudioBuildState.COMMITTED,
            project_path=str(exported / "workspace-demo.pbip"),
            created_at=now,
            updated_at=now,
        ),
    ]
    assert service._remove_generated_projects(builds) == 1  # noqa: SLF001
    assert not generated.exists()
    assert not evidence.exists()
    assert exported.is_dir()
    assert (exported / "workspace-demo.pbip").is_file()
