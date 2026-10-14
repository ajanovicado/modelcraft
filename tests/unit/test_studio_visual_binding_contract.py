"""A catalog-legal visual must still honor the user's exact requested bindings."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_powerbi_analyst.studio.contracts import (
    ApproveDraftRequest,
    CreateWorkspaceRequest,
    DraftRequest,
    IngestRequest,
    ReviseDraftRequest,
    UpdateDashboardRequest,
)
from ai_powerbi_analyst.studio.planner import StudioPlanningError, StudioPlanningService
from ai_powerbi_analyst.studio.report_intent import ReportVisualIntent
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


def prepared_star(tmp_path, *, full_page=False):
    source = tmp_path / "source"
    source.mkdir()
    (source / "FactSales.csv").write_text(
        "CustomerKey,Sales,Profit\n1,100,20\n1,200,40\n2,300,60\n", encoding="utf-8"
    )
    (source / "DimCustomer.csv").write_text(
        "CustomerKey,Region\n1,North\n2,South\n", encoding="utf-8"
    )
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Exact visual bindings",
            selected_folder=str(source),
            expected_base_hash=EMPTY_HASH,
            idempotency_key="workspace",
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(p) for p in sorted(source.glob("*.csv"))],
            expected_base_hash=workspace.base_hash,
            idempotency_key="exact-ingest",
        )
    )
    workspace = service.workspace(workspace.id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Build a sales dashboard by customer region.",
            expected_base_hash=workspace.base_hash,
            idempotency_key="exact-draft",
        )
    )
    approved = service.approve_draft(
        draft.id,
        ApproveDraftRequest(
            workspace_id=workspace.id,
            expected_base_hash=draft.base_hash,
            approved_relationship_ids=[
                rel.id for rel in draft.semantic_model.relationships if not rel.blockers
            ],
            idempotency_key="exact-approve",
        ),
    )
    card = approved.pages[0].visuals[0]
    if full_page:
        card = card.model_copy(update={"height": 12})
    page = approved.pages[0].model_copy(update={"visuals": [card]})
    draft = service.update_dashboard(
        approved.id,
        UpdateDashboardRequest(
            workspace_id=workspace.id,
            expected_base_hash=approved.base_hash,
            pages=[page],
            idempotency_key="prepare-capacity",
        ),
    )
    return service, workspace, snapshot, draft


class BindingChoices:
    def __init__(self, choices):
        self.choices = iter(choices)
        self.requests = []

    def health(self):
        return SimpleNamespace(model_available=True)

    def generate(self, _system, prompt, _response_type, **_options):
        payload = json.loads(prompt)
        self.requests.append(payload)
        choice = next(self.choices)
        if choice == "unsupported":
            return ReportVisualIntent(visual_type="unsupported", reason="Donut is unavailable.")
        table_name = "FactSales" if choice == "wrong-group" else "DimCustomer"
        field_name = "Sales" if choice == "wrong-group" else "Region"
        table = next(i for i, item in enumerate(payload["tables"]) if item["name"] == table_name)
        category = next(
            i
            for i, field in enumerate(payload["tables"][table]["fields"])
            if field[0] == field_name
        )
        measure_name = "Total Profit" if choice == "wrong-measure" else "Total Sales"
        measure = next(
            i for i, item in enumerate(payload["measures"]) if item["name"] == measure_name
        )
        return ReportVisualIntent(
            visual_type="donutChart", table=table, category=category, measure=measure
        )


@pytest.mark.parametrize(
    "choices",
    [
        ["wrong-group", "correct"],
        ["wrong-measure", "correct"],
        ["wrong-group", "wrong-group"],
        ["wrong-group", "unsupported"],
        ["unsupported", "correct"],
        ["unsupported", "unsupported"],
    ],
)
def test_repair_or_grounded_fallback_preserves_exact_region_and_sales(tmp_path, choices):
    service, workspace, snapshot, draft = prepared_star(tmp_path)
    model = BindingChoices(choices)
    service.planner.model = model
    prompt = (
        "Add a donut chart of Total Sales grouped by DimCustomer[Region] "
        f"to {draft.pages[0].title}."
    )
    revised = service.revise_draft(
        draft.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            expected_base_hash=draft.base_hash,
            prompt=prompt,
            idempotency_key="exact-region",
        ),
    )
    visual = revised.pages[0].visuals[-1]
    customer = next(item.table for item in snapshot.tables if item.table.name == "DimCustomer")
    region = next(field for field in customer.columns if field.name == "Region")
    sales = next(
        measure for measure in draft.measure_proposal.measures if measure.name == "Total Sales"
    )
    assert (visual.table_id, visual.category_column_id, visual.measure_id) == (
        customer.id,
        region.id,
        sales.id,
    )
    assert visual.visual_type == "donutChart"
    assert len(model.requests) == 2
    if choices[0] == "unsupported":
        assert "supported catalog visual" in model.requests[1]["repair"]
    else:
        assert "conflicts with the exact requested" in model.requests[1]["repair"]
    assert all(request["request"] == prompt for request in model.requests)


@pytest.mark.parametrize(
    "reference", ["Missing[Region]", "DimCustomer[Missing]", "'Foreign Table'[Region]"]
)
def test_missing_qualified_group_is_refused_before_selection_or_fallback(tmp_path, reference):
    service, workspace, _snapshot, draft = prepared_star(tmp_path)
    model = BindingChoices(["wrong-group", "wrong-group"])
    service.planner.model = model
    with pytest.raises(StudioPlanningError, match="exact requested field.*absent"):
        service.revise_draft(
            draft.id,
            ReviseDraftRequest(
                workspace_id=workspace.id,
                expected_base_hash=draft.base_hash,
                prompt=(
                    f"Add a donut chart of Total Sales by {reference} to {draft.pages[0].title}."
                ),
                idempotency_key="missing-reference",
            ),
        )
    assert model.requests == []
    assert service.draft(draft.id).base_hash == draft.base_hash


def test_supported_visual_capacity_error_survives_hallucinated_unsupported_repair(tmp_path):
    service, workspace, _snapshot, draft = prepared_star(tmp_path, full_page=True)
    model = BindingChoices(["correct", "unsupported"])
    service.planner.model = model
    with pytest.raises(StudioPlanningError, match="has no room"):
        service.revise_draft(
            draft.id,
            ReviseDraftRequest(
                workspace_id=workspace.id,
                expected_base_hash=draft.base_hash,
                prompt=(
                    "Add a donut chart of Total Sales by DimCustomer[Region] "
                    f"to {draft.pages[0].title}."
                ),
                idempotency_key="no-capacity",
            ),
        )
    assert len(model.requests) == 2
    assert service.draft(draft.id).base_hash == draft.base_hash
