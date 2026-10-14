from __future__ import annotations

from pathlib import Path

from ai_powerbi_analyst.studio.contracts import (
    ApproveDraftRequest,
    CreateWorkspaceRequest,
    DraftRequest,
    IngestRequest,
    ReviseDraftRequest,
)
from ai_powerbi_analyst.studio.planner import StudioPlanningService, _wants_single_executive_page
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


def _service(tmp_path: Path, csv: str) -> tuple[StudioService, object, object]:
    source_root = tmp_path / "selected"
    source_root.mkdir()
    source = source_root / "orders.csv"
    source.write_text(csv, encoding="utf-8")
    repository = Path(__file__).resolve().parents[2]
    service = StudioService(repository, tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Governed recipes",
            selected_folder=str(source_root),
            idempotency_key="create-recipe-workspace",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="ingest-recipe-source",
            expected_base_hash=workspace.base_hash,
        )
    )
    return service, service.workspace(workspace.id), snapshot


def test_executive_overview_prompt_creates_one_page(tmp_path: Path) -> None:
    assert _wants_single_executive_page(
        "Create one executive overview page with KPIs, a trend, and a category breakdown"
    )
    assert not _wants_single_executive_page(
        "Create a sales and profit dashboard with an overview, trends and detail page."
    )
    service, workspace, snapshot = _service(
        tmp_path,
        "Order ID,Order Date,Region,Sales,Profit,Quantity\n"
        "A-1,2025-01-01,East,100.00,20.00,2\n"
        "A-2,2025-02-01,West,250.00,40.00,4\n",
    )
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt=(
                "Create one executive overview page with KPIs, a trend, and a category "
                "breakdown from the reviewed data."
            ),
            idempotency_key="executive-overview",
            expected_base_hash=workspace.base_hash,
        )
    )
    assert len(draft.pages) == 1
    assert draft.pages[0].id == "overview"
    assert {visual.recipe for visual in draft.pages[0].visuals} >= {"kpi"}
    preview_ids = {item.visual_id for item in draft.previews}
    assert preview_ids == {visual.id for visual in draft.pages[0].visuals}
    assert all(item.empty_reason is None or item.rows for item in draft.previews)


def test_weighted_average_revision_uses_catalog_ids_not_advanced_dax(tmp_path: Path) -> None:
    service, workspace, snapshot = _service(
        tmp_path,
        "Order ID,Order Date,Region,Sales,Profit,Quantity\n"
        "A-1,2025-01-01,East,100.00,20.00,2\n"
        "A-2,2025-02-01,West,250.00,40.00,4\n",
    )
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Show sales and profit trends by region",
            idempotency_key="weighted-base-draft",
            expected_base_hash=workspace.base_hash,
        )
    )
    approved = service.approve_draft(
        draft.id,
        ApproveDraftRequest(
            workspace_id=workspace.id,
            approved_relationship_ids=[],
            idempotency_key="approve-weighted-base",
            expected_base_hash=draft.base_hash,
        ),
    )
    revised = service.revise_draft(
        approved.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt="Create a weighted average of Sales weighted by Quantity and add a page",
            idempotency_key="revise-weighted",
            expected_base_hash=approved.base_hash,
        ),
    )
    measure = next(
        item
        for item in revised.measure_proposal.measures
        if getattr(item, "operation", None) == "weighted_average"
    )
    assert measure.tier == "governed"
    assert measure.column_id
    assert measure.weight_column_id
    assert measure.column_id != measure.weight_column_id
    assert any(page.title == "Weighted Average" for page in revised.pages)
    assert measure.tier == "governed"
