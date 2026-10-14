from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_powerbi_analyst.studio.api import create_app
from ai_powerbi_analyst.studio.contracts import (
    ApproveDraftRequest,
    BuildRequest,
    CreateWorkspaceRequest,
    DraftRequest,
    IngestRequest,
    RelinkBuildRequest,
    StudioBuildState,
    UndoBuildRequest,
)
from ai_powerbi_analyst.studio.planner import StudioPlanningService
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


def _built_csv_project(tmp_path: Path):
    source_root = tmp_path / "selected"
    source_root.mkdir()
    source = source_root / "orders.csv"
    source.write_text(
        "Order Date,Region,Sales\n2025-01-01,East,100\n2025-02-01,West,200\n2025-03-01,East,100\n",
        encoding="utf-8",
    )
    repository = Path(__file__).resolve().parents[2]
    service = StudioService(repository, tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    service.projects.require_external_validators = False
    # Unit scope: never depend on installed validators or their timing under CPU load.
    service.projects._external_validation = lambda _project: ([], [])  # type: ignore[method-assign]
    service.projects.desktop_closed_check = lambda: None
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Relink test",
            selected_folder=str(source_root),
            idempotency_key="create-relink-workspace",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="ingest-relink-source",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Build a sales dashboard by region",
            idempotency_key="create-relink-draft",
            expected_base_hash=workspace.base_hash,
        )
    )
    approved = service.approve_draft(
        draft.id,
        ApproveDraftRequest(
            workspace_id=workspace.id,
            approved_relationship_ids=[],
            idempotency_key="approve-relink-draft",
            expected_base_hash=draft.base_hash,
        ),
    )
    workspace = service.workspace(workspace.id)
    build_request = BuildRequest(
        workspace_id=workspace.id,
        draft_id=approved.id,
        project_name="Relink Project",
        idempotency_key="build-relink-project",
        expected_base_hash=workspace.base_hash,
    )
    build = service.build(build_request)
    assert build.state is StudioBuildState.COMMITTED, build.message
    assert build.project_path is not None
    assert build.project_hash is not None
    assert service.build(build_request) == build
    return service, workspace, snapshot, source, build


def _only_source_tmdl(pbip_path: str) -> Path:
    project = Path(pbip_path).resolve(strict=True).parent
    candidates = [
        path
        for path in project.glob("*.SemanticModel/definition/tables/*.tmdl")
        if path.stem != "Calendar"
    ]
    assert len(candidates) == 1
    return candidates[0]


def test_relink_rewrites_connector_validates_records_and_remains_undoable(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    # Desktop's limit applies to the full PBIR path, including pytest's test name.
    service, workspace, snapshot, source, build = _built_csv_project(
        tmp_path_factory.mktemp("relink")
    )
    assert build.project_path is not None
    assert build.project_hash is not None
    source_tmdl = _only_source_tmdl(build.project_path)
    managed_path = snapshot.tables[0].normalized_parquet_path
    before = source_tmdl.read_text(encoding="utf-8")
    assert "Parquet.Document" in before
    assert managed_path in before

    request = RelinkBuildRequest(
        workspace_id=workspace.id,
        expected_project_hash=build.project_hash,
        idempotency_key="relink-project-to-originals",
        expected_base_hash=workspace.base_hash,
    )
    receipt = service.relink_build(build.id, request)
    replayed = service.relink_build(build.id, request)

    assert receipt == replayed
    assert receipt.passed
    assert receipt.project_hash is not None
    assert receipt.project_hash != build.project_hash
    after = source_tmdl.read_text(encoding="utf-8")
    assert "Csv.Document" in after
    assert str(source.resolve()) in after
    assert "Parquet.Document" not in after
    evidence = json.loads(
        (source_tmdl.parents[3] / ".ai-powerbi" / "source-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert evidence["sources"][0]["kind"] == "csv"
    assert evidence["sources"][0]["snapshot_path"] == str(source.resolve())
    assert sum(event["type"] == "build.relinked" for event in service.events(workspace.id)) == 1

    undone = service.undo_build(
        build.id,
        UndoBuildRequest(
            workspace_id=workspace.id,
            idempotency_key="undo-relink-project",
            expected_base_hash=workspace.base_hash,
        ),
    )
    assert undone.restored
    restored = source_tmdl.read_text(encoding="utf-8")
    assert "Parquet.Document" in restored
    assert managed_path in restored


def test_compatibility_and_relink_requests_reject_unknown_fields(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    service, workspace, _snapshot, _source, build = _built_csv_project(
        tmp_path_factory.mktemp("relink")
    )
    assert build.project_path is not None
    assert build.project_hash is not None
    token = "strict-relink-api-token"
    client = TestClient(create_app(service, token))
    headers = {"Authorization": f"Bearer {token}"}

    compatibility = client.post(
        "/v1/compatibility",
        json={"pbip_path": build.project_path, "unexpected": True},
        headers=headers,
    )
    relink = client.post(
        f"/v1/builds/{build.id}/relink",
        json={
            "workspace_id": workspace.id,
            "expected_project_hash": build.project_hash,
            "idempotency_key": "strict-relink-request",
            "expected_base_hash": workspace.base_hash,
            "unexpected": True,
        },
        headers=headers,
    )

    assert compatibility.status_code == 422
    assert relink.status_code == 422
