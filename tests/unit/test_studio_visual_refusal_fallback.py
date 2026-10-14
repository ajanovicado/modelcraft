"""A false visual refusal must not disable an otherwise supported draft addition."""

import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from ai_powerbi_analyst.studio.contracts import (
    ApproveDraftRequest,
    ChatRequest,
    ConversationContext,
    CreateWorkspaceRequest,
    DraftRequest,
    IngestRequest,
    ReviseDraftRequest,
    UpdateDashboardRequest,
)
from ai_powerbi_analyst.studio.planner import (
    StudioPlanningError,
    StudioPlanningService,
    _requested_visual_types,
)
from ai_powerbi_analyst.studio.report_intent import ReportVisualIntent
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH

REQUEST = "on detail page, add appropriate visual blow the table"
SOURCE = (
    "Order ID,Order Date,Region,Sales,Profit\n"
    "A1,2025-01-01,East,100.5,20.25\n"
    "A2,2025-02-01,West,250,40\n"
    "A3,2025-03-01,East,149.5,29.75\n"
)


def prepared_draft(tmp_path):
    source = tmp_path / "Release Golden Data"
    source.mkdir()
    source_file = source / "orders.csv"
    source_file.write_text(SOURCE, encoding="utf-8")
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Release Golden Data",
            selected_folder=str(source),
            expected_base_hash=EMPTY_HASH,
            idempotency_key="visual-refusal-workspace",
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source_file)],
            expected_base_hash=workspace.base_hash,
            idempotency_key="visual-refusal-ingest",
        )
    )
    workspace = service.workspace(workspace.id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Create a sales and profit dashboard with an overview, trends and detail page.",
            expected_base_hash=workspace.base_hash,
            idempotency_key="visual-refusal-draft",
        )
    )
    draft = service.approve_draft(
        draft.id,
        ApproveDraftRequest(
            workspace_id=workspace.id,
            approved_relationship_ids=[],
            expected_base_hash=draft.base_hash,
            idempotency_key="visual-refusal-approve",
        ),
    )
    return service, service.workspace(workspace.id), snapshot, draft, source_file


class RefusalChoices:
    def __init__(self, *, repair_succeeds=False, visual_type="clusteredColumnChart"):
        self.repair_succeeds = repair_succeeds
        self.visual_type = visual_type
        self.requests = []

    def health(self):
        return SimpleNamespace(model_available=True)

    def generate(self, _system, prompt, _response_type, **options):
        payload = json.loads(prompt)
        self.requests.append(payload)
        assert options["repair_attempts"] == 0
        if not self.repair_succeeds or len(self.requests) == 1:
            return ReportVisualIntent(
                visual_type="unsupported",
                reason="The request asks for a visual below a detail table.",
            )
        category_name = (
            "Order Date"
            if self.visual_type in {"lineChart", "areaChart", "stackedAreaChart"}
            else "Region"
        )
        category = next(
            i for i, field in enumerate(payload["tables"][0]["fields"]) if field[0] == category_name
        )
        measure = next(
            i for i, item in enumerate(payload["measures"]) if item["name"] == "Total Sales"
        )
        return ReportVisualIntent(
            visual_type=self.visual_type, table=0, category=category, measure=measure
        )


@pytest.mark.parametrize("repair_succeeds", [False, True])
def test_chat_false_refusal_repairs_or_falls_back_on_requested_detail_page(
    tmp_path, repair_succeeds, caplog
):
    service, workspace, snapshot, before, source_file = prepared_draft(tmp_path)
    assert before.status == "approved" and before.semantic_model.approved
    model = RefusalChoices(repair_succeeds=repair_succeeds)
    service.planner.model = model
    request = ChatRequest(
        workspace_id=workspace.id,
        message=REQUEST,
        context=ConversationContext(page_id="overview"),
        expected_base_hash=before.base_hash,
        idempotency_key="chat-false-visual-refusal",
    )
    events = list(service.chat_events(request, Event()))
    assert any(event["event"] == "draft" for event in events)
    after = service.draft(before.id)
    assert len(model.requests) == 2
    assert "supported catalog visual" in model.requests[1]["repair"]
    assert all(payload["request"].startswith(REQUEST) for payload in model.requests)
    assert all("Page: Overview [overview]" in payload["request"] for payload in model.requests)
    assert after.measure_proposal == before.measure_proposal
    assert len(after.pages) == len(before.pages)
    assert [p for p in after.pages if p.id != "detail"] == [
        p for p in before.pages if p.id != "detail"
    ]
    detail = next(page for page in after.pages if page.id == "detail")
    old_detail = next(page for page in before.pages if page.id == "detail")
    table, chart = detail.visuals
    assert table.model_dump(exclude={"height"}) == old_detail.visuals[0].model_dump(
        exclude={"height"}
    )
    assert table.height >= 6
    assert chart.recipe == "category"
    assert chart.y >= table.y + table.height
    assert chart.y + chart.height <= 12
    source_table = snapshot.tables[0].table
    region = next(field for field in source_table.columns if field.name == "Region")
    sales = next(item for item in before.measure_proposal.measures if item.name == "Total Sales")
    assert (chart.table_id, chart.category_column_id, chart.measure_id) == (
        source_table.id,
        region.id,
        sales.id,
    )
    preview = next(item for item in after.previews if item.visual_id == chart.id)
    assert len(preview.rows) == 2
    assert sum(row["value"] for row in preview.rows) == pytest.approx(500)
    assert preview.query_result_hash
    assert after.status == "draft" and not after.semantic_model.approved
    assert not service.builds(workspace.id)
    assert source_file.read_text(encoding="utf-8") == SOURCE
    if not repair_succeeds:
        assert "catalog-grounded visual addition after two local-model refusals" in caplog.text
    replay = list(service.chat_events(request, Event()))
    assert any(event["event"] == "draft" for event in replay)
    assert service.draft(before.id) == after
    assert len(model.requests) == 2


@pytest.mark.parametrize(
    ("requested_type", "model_type", "repair_succeeds"),
    [
        ("funnel", "clusteredColumnChart", False),
        ("funnel", "clusteredColumnChart", True),
        ("waterfall chart", "clusteredColumnChart", False),
        ("waterfall chart", "clusteredColumnChart", True),
        ("stacked area chart", "lineChart", False),
        ("stacked area chart", "lineChart", True),
        ("stacked bar chart", "clusteredColumnChart", True),
        ("'barChart' visual", "clusteredColumnChart", True),
        ("sales funnel", "clusteredColumnChart", False),
    ],
)
def test_explicit_supported_type_never_silently_substitutes_after_refusal(
    tmp_path, requested_type, model_type, repair_succeeds
):
    service, workspace, _snapshot, before, source_file = prepared_draft(tmp_path)
    model = RefusalChoices(repair_succeeds=repair_succeeds, visual_type=model_type)
    service.planner.model = model
    with pytest.raises(StudioPlanningError, match="exact requested"):
        service.revise_draft(
            before.id,
            ReviseDraftRequest(
                workspace_id=workspace.id,
                prompt=f"Add a {requested_type} to the Detail page",
                expected_base_hash=before.base_hash,
                idempotency_key="explicit-visual-type-refusal",
            ),
        )
    assert len(model.requests) == 2
    assert service.draft(before.id) == before
    assert not service.builds(workspace.id)
    assert source_file.read_text(encoding="utf-8") == SOURCE


@pytest.mark.parametrize(
    ("requested_type", "expected_type"),
    [("monthly line chart", "lineChart"), ("small donut chart", "donutChart")],
)
def test_modified_type_rejects_wrong_model_choice_and_uses_only_matching_fallback(
    tmp_path, requested_type, expected_type
):
    service, workspace, _snapshot, before, source_file = prepared_draft(tmp_path)
    model = RefusalChoices(repair_succeeds=True, visual_type="clusteredColumnChart")
    service.planner.model = model
    after = service.revise_draft(
        before.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt=f"Add a {requested_type} to the Detail page",
            expected_base_hash=before.base_hash,
            idempotency_key="modified-visual-type-refusal",
        ),
    )
    assert len(model.requests) == 2
    assert len(after.pages[2].visuals) == 2
    assert after.pages[2].visuals[-1].visual_type == expected_type
    assert not service.builds(workspace.id)
    assert source_file.read_text(encoding="utf-8") == SOURCE


@pytest.mark.parametrize(
    ("requested_type", "model_type", "repair_succeeds"),
    [
        ("donut chart", "donutChart", False),
        ("bar chart", "clusteredBarChart", False),
        ("donut chart", "donutChart", True),
        ("bar chart", "clusteredBarChart", True),
        ("bar chart", "clusteredColumnChart", True),
        ("funnel", "funnel", True),
        ("waterfall chart", "waterfallChart", True),
        ("'waterfallChart' visual", "waterfallChart", True),
        ("small donut chart", "donutChart", False),
        ("monthly line chart", "lineChart", True),
    ],
)
def test_explicit_visual_type_is_retained_when_repair_or_fallback_can_honor_it(
    tmp_path, requested_type, model_type, repair_succeeds
):
    service, workspace, _snapshot, before, _source_file = prepared_draft(tmp_path)
    model = RefusalChoices(repair_succeeds=repair_succeeds, visual_type=model_type)
    service.planner.model = model
    group = "Order Date" if model_type == "lineChart" else "Region"
    after = service.revise_draft(
        before.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt=f"Add a {requested_type} of Total Sales by orders[{group}] to the Detail page",
            expected_base_hash=before.base_hash,
            idempotency_key="honored-visual-type",
        ),
    )
    assert len(model.requests) == 2
    assert after.pages[2].visuals[-1].visual_type == model_type
    assert len(after.pages[2].visuals) == 2
    assert not service.builds(workspace.id)


@pytest.mark.parametrize(
    "prompt",
    [
        REQUEST,
        "Add an appropriate visual below the table on the Funnel page",
        "Add a visual of Total Sales by orders[Line chart] to the Detail page",
        "Add an appropriate visual showing Waterfall below the table on Detail",
    ],
)
def test_placement_targets_and_field_names_do_not_request_a_visual_type(prompt):
    assert not _requested_visual_types(prompt)


@pytest.mark.parametrize(
    ("prompt", "types"),
    [
        ("Add a stacked bar chart to Detail", {"barChart"}),
        ("Add a clustered column chart to Detail", {"clusteredColumnChart"}),
        ("Add a stacked area chart to Detail", {"stackedAreaChart"}),
        ("Add a 'waterfallChart' to Detail", {"waterfallChart"}),
        ('Add a "barChart" to Detail', {"barChart"}),
        (
            "Add a horizontal bar chart to Detail",
            {"barChart", "clusteredBarChart"},
        ),
        (
            "Add a vertical bar chart to Detail",
            {"columnChart", "clusteredColumnChart"},
        ),
        ("Add a card using 'Funnel'[Sales] to Detail", {"cardVisual"}),
        ("Add a card using Funnel[Sales] to Detail", {"cardVisual"}),
        ('Add a "Funnel Sales" card to Detail', {"cardVisual"}),
        ("Add a 'Funnel Sales' card to Detail", {"cardVisual"}),
        ("Add a monthly line chart to Detail", {"lineChart"}),
        ("Add a small donut chart to Detail", {"donutChart"}),
        ("Add a sales funnel to Detail", {"funnel"}),
        (
            "Add a bar chart using Funnel Sales to Detail",
            {"barChart", "columnChart", "clusteredBarChart", "clusteredColumnChart"},
        ),
        (
            "Add a bar chart to Detail",
            {"barChart", "columnChart", "clusteredBarChart", "clusteredColumnChart"},
        ),
    ],
)
def test_visual_type_aliases_preserve_specific_types_and_generic_bar_family(prompt, types):
    assert _requested_visual_types(prompt) == types


@pytest.mark.parametrize(
    "prompt",
    [
        "Add a matrix below the table on the Detail page",
        "Add a bar chart of Total Sales by orders[Missing] to the Detail page",
        "Add a visual to the Missing page",
        "Add a visual to Overview and Detail pages",
        "Add a visual below the table",
        "Add a visual above the table on the Detail page",
    ],
)
def test_invalid_visual_request_never_uses_refusal_repair_or_mutates_state(tmp_path, prompt):
    service, workspace, _snapshot, before, source_file = prepared_draft(tmp_path)
    model = RefusalChoices()
    service.planner.model = model
    with pytest.raises(StudioPlanningError):
        service.revise_draft(
            before.id,
            ReviseDraftRequest(
                workspace_id=workspace.id,
                prompt=prompt,
                expected_base_hash=before.base_hash,
                idempotency_key="invalid-visual-refusal",
            ),
        )
    assert model.requests == []
    assert service.draft(before.id) == before
    assert service.workspace(workspace.id).active_draft_id == before.id
    assert not service.builds(workspace.id)
    assert source_file.read_text(encoding="utf-8") == SOURCE


@pytest.mark.parametrize("capacity", ["grid", "visual_count"])
def test_repeated_false_refusal_preserves_real_full_page_capacity_error(tmp_path, capacity):
    service, workspace, _snapshot, draft, source_file = prepared_draft(tmp_path)
    chart = (
        draft.pages[0]
        .visuals[-1]
        .model_copy(
            update={"id": "detail_existing_chart", "x": 0, "y": 6, "width": 12, "height": 6}
        )
    )
    if capacity == "grid":
        visuals = [draft.pages[2].visuals[0].model_copy(update={"height": 6}), chart]
        expected_error = "has no room"
    else:
        card = draft.pages[0].visuals[0]
        visuals = [
            card.model_copy(update={"id": f"detail_card_{i}", "x": i % 3 * 4, "y": i // 3 * 2})
            for i in range(12)
        ]
        expected_error = "maximum 12 visuals"
    pages = [
        page.model_copy(update={"visuals": visuals}) if page.id == "detail" else page
        for page in draft.pages
    ]
    before = service.update_dashboard(
        draft.id,
        UpdateDashboardRequest(
            workspace_id=workspace.id,
            pages=pages,
            expected_base_hash=draft.base_hash,
            idempotency_key="fill-detail-page",
        ),
    )
    model = RefusalChoices()
    service.planner.model = model
    with pytest.raises(StudioPlanningError, match=expected_error):
        service.revise_draft(
            before.id,
            ReviseDraftRequest(
                workspace_id=workspace.id,
                prompt=REQUEST,
                expected_base_hash=before.base_hash,
                idempotency_key="full-detail-refusal",
            ),
        )
    assert len(model.requests) == 2
    assert service.draft(before.id) == before
    assert not service.builds(workspace.id)
    assert source_file.read_text(encoding="utf-8") == SOURCE
