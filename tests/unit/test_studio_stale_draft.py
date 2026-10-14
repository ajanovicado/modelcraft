from __future__ import annotations

from pathlib import Path

import pytest

from ai_powerbi_analyst.studio.contracts import (
    ApproveDraftRequest,
    BuildRequest,
    CreateWorkspaceRequest,
    DashboardDraft,
    DatasetSnapshot,
    DraftRequest,
    IngestRequest,
    LocalWorkspace,
)
from ai_powerbi_analyst.studio.planner import StudioPlanningService
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH, StateConflictError


def _workspace_with_superseded_draft(
    tmp_path: Path,
) -> tuple[StudioService, LocalWorkspace, DatasetSnapshot, DashboardDraft]:
    source_root = tmp_path / "selected"
    source_root.mkdir()
    source = source_root / "orders.csv"
    source.write_text(
        "Order Date,Region,Sales\n2025-01-01,East,100.00\n2025-02-01,West,250.00\n",
        encoding="utf-8",
    )

    repository = Path(__file__).resolve().parents[2]
    service = StudioService(repository, tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Stale draft safety",
            selected_folder=str(source_root),
            idempotency_key="create-stale-draft-workspace",
            expected_base_hash=EMPTY_HASH,
        )
    )
    first_snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="initial-stale-draft-ingestion",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=first_snapshot.id,
            prompt="Show sales trends by region",
            idempotency_key="create-draft-before-reingestion",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)

    source.write_text(
        "Order Date,Region,Sales\n"
        "2025-01-01,East,100.00\n"
        "2025-02-01,West,250.00\n"
        "2025-03-01,North,175.00\n",
        encoding="utf-8",
    )
    replacement_snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="replacement-stale-draft-ingestion",
            expected_base_hash=workspace.base_hash,
        )
    )
    return service, service.workspace(workspace.id), replacement_snapshot, draft


def test_reingestion_clears_the_active_draft(tmp_path: Path) -> None:
    _service, workspace, replacement_snapshot, _draft = _workspace_with_superseded_draft(tmp_path)

    assert workspace.active_snapshot_id == replacement_snapshot.id
    assert workspace.active_draft_id is None


def test_superseded_draft_cannot_be_approved_or_built(tmp_path: Path) -> None:
    service, workspace, _replacement_snapshot, draft = _workspace_with_superseded_draft(tmp_path)

    with pytest.raises(StateConflictError, match="regenerate.*before approval"):
        service.approve_draft(
            draft.id,
            ApproveDraftRequest(
                workspace_id=workspace.id,
                approved_relationship_ids=[],
                idempotency_key="approve-superseded-draft",
                expected_base_hash=draft.base_hash,
            ),
        )

    with pytest.raises(StateConflictError, match="regenerate.*before build"):
        service.build(
            BuildRequest(
                workspace_id=workspace.id,
                draft_id=draft.id,
                project_name="Stale draft safety",
                idempotency_key="build-superseded-draft",
                expected_base_hash=workspace.base_hash,
            )
        )

    assert service.draft(draft.id).status == "draft"
    assert service.builds(workspace.id) == []
