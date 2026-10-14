import json
from datetime import date
from pathlib import Path
from threading import Event

import pytest
from pydantic import ValidationError
from test_studio_governed_editor import _service_with_sources

from ai_powerbi_analyst.contracts import VisualDateFilter
from ai_powerbi_analyst.powerbi.semantic_model import SemanticProjectBuilder
from ai_powerbi_analyst.powerbi.visual_filters import date_filter_config
from ai_powerbi_analyst.studio.contracts import (
    ApproveDraftRequest,
    ChatRequest,
    ConversationContext,
    UpdateDashboardRequest,
)
from ai_powerbi_analyst.studio.planner import StudioPlanningError, compile_powerbi_plan
from ai_powerbi_analyst.studio.report_actions import filter_command, report_filter_action


def test_typo_request_opens_selected_chart_filter_without_model_or_draft_mutation(
    tmp_path, monkeypatch
):
    service, workspace, _, draft = _service_with_sources(tmp_path)
    visual = next(v for v in draft.pages[0].visuals if v.recipe == "trend")

    def unexpected(*args, **kwargs):
        pytest.fail("A date-filter UI action must not generate a new visual")

    monkeypatch.setattr(service, "revise_draft", unexpected)
    monkeypatch.setattr(service, "chat", unexpected)
    request = ChatRequest(
        workspace_id=workspace.id,
        message="add date slice filer only for this chart",
        context=ConversationContext(page_id=draft.pages[0].id, visual_id=visual.id),
        idempotency_key="chart-date-command",
        expected_base_hash=draft.base_hash,
    )
    events = list(service.chat_events(request, Event()))
    assert events[0] == {
        "event": "report_action",
        "data": {
            "kind": "visual_date_filter",
            "page_id": draft.pages[0].id,
            "visual_id": visual.id,
        },
    }
    assert events == list(service.chat_events(request, Event()))
    assert service.draft(draft.id) == draft
    assert len(service.store.messages(workspace.id)) == 2


@pytest.mark.parametrize(
    "message",
    ["add date slice filer only for this chart", "filter this chart by date", "show date filter"],
)
def test_date_commands_are_detected(message):
    assert filter_command(message)


@pytest.mark.parametrize(
    "message",
    [
        "Do not add a date filter",
        "How do date filters work?",
        "Build an LOD calculation fixed by Region and ignore other filters",
        "Add a line chart over time",
    ],
)
def test_non_filter_commands_keep_their_route(message):
    assert not filter_command(message)


def test_unselected_chart_is_not_guessed_and_named_chart_wins(tmp_path):
    _, _, _, draft = _service_with_sources(tmp_path)
    text, action = report_filter_action("add date filter only for this chart", None, draft)
    assert action == {"kind": "visual_date_filter"}
    assert "Choose the chart" in text
    page = draft.pages[0]
    target = next(v for v in page.visuals if v.recipe == "trend")
    _, action = report_filter_action(
        f"add date filter for {target.title}",
        ConversationContext(page_id=page.id, visual_id=page.visuals[0].id),
        draft,
    )
    assert action["visual_id"] == target.id
    _, action = report_filter_action("add a date slicer for the entire page", None, draft)
    assert action is None


@pytest.mark.parametrize(
    "start,end",
    [(None, None), ("2025-03-01", "2025-02-01"), ("2025-02-30", None), (None, "9999-12-31")],
)
def test_invalid_ranges_rejected(start, end):
    with pytest.raises(ValidationError):
        VisualDateFilter(table_id="orders", column_id="order_date", start=start, end=end)


def test_date_range_filters_only_target_and_survives_native_compilation(tmp_path):
    service, workspace, snapshot, draft = _service_with_sources(
        tmp_path,
        orders_content=(
            "Order ID,Order Date,Region,Sales,Profit\n"
            "A-1,2025-01-31 23:59:59,East,100,20\n"
            "A-2,2025-02-01 00:00:00,West,250,40\n"
            "A-3,2025-02-01 23:59:59,East,150,30\n"
            "A-4,2025-02-02 00:00:00,West,900,90\n"
        ),
    )
    pages = [p.model_copy(deep=True) for p in draft.pages]
    target = next(v for v in pages[0].visuals if v.recipe == "trend")
    target = target.model_copy(
        update={
            "date_filter": VisualDateFilter(
                table_id=target.table_id,
                column_id=target.category_column_id,
                start=date(2025, 2, 1),
                end=date(2025, 2, 1),
            )
        }
    )
    pages[0] = pages[0].model_copy(
        update={"visuals": [target if v.id == target.id else v for v in pages[0].visuals]}
    )
    updated = service.update_dashboard(
        draft.id,
        UpdateDashboardRequest(
            workspace_id=workspace.id,
            pages=pages,
            idempotency_key="save-chart-date-filter",
            expected_base_hash=draft.base_hash,
        ),
    )
    assert (
        sum(r["value"] for p in updated.previews if p.visual_id == target.id for r in p.rows) == 400
    )
    assert draft.base_hash != updated.base_hash
    for before in draft.previews:
        if before.visual_id != target.id:
            assert (
                next(p for p in updated.previews if p.visual_id == before.visual_id).rows
                == before.rows
            )
    for visual in updated.pages[0].visuals:
        if visual.id != target.id:
            assert visual == next(v for v in draft.pages[0].visuals if v.id == visual.id)
    approved = service.approve_draft(
        updated.id,
        ApproveDraftRequest(
            workspace_id=workspace.id,
            approved_relationship_ids=[],
            idempotency_key="approve-chart-date-filter",
            expected_base_hash=updated.base_hash,
        ),
    )
    manifest = service.ingestion.load_manifest(snapshot)
    plan = compile_powerbi_plan(approved, manifest)
    native_visual = next(v for p in plan.pages for v in p.visuals if v.id == target.id)
    assert native_visual.date_filter == target.date_filter
    builder = SemanticProjectBuilder(
        tmp_path / "build",
        Path(__file__).resolve().parents[2] / "fixtures/powerbi/client-wow-template",
    )
    report = tmp_path / "filter-report"
    builder._write_report(report, manifest, plan)
    emitted = [json.loads(p.read_text(encoding="utf-8")) for p in report.rglob("visual.json")]
    filtered_visuals = [v for v in emitted if "filterConfig" in v]
    assert len(filtered_visuals) == 1
    assert filtered_visuals[0]["visual"]["visualType"] == target.visual_type
    assert not any("filterConfig" in json.loads(p.read_text()) for p in report.rglob("page.json"))
    table = next(t for t in manifest.tables if t.id == target.table_id)
    config = date_filter_config(
        native_visual.date_filter, visual_id=target.id, table=table, semantic_name="Renamed Orders"
    )
    container = config["filters"][0]
    assert container["field"]["Column"]["Expression"]["SourceRef"] == {"Entity": "Renamed Orders"}
    condition = container["filter"]["Where"][0]["Condition"]["And"]
    assert condition["Left"]["Comparison"]["ComparisonKind"] == 2
    assert condition["Right"]["Comparison"]["ComparisonKind"] == 3
    assert (
        condition["Right"]["Comparison"]["Right"]["Literal"]["Value"]
        == "datetime'2025-02-02T00:00:00'"
    )
    # Clearing restores the unfiltered result, and cannot retain stale filtered evidence.
    clear_pages = [p.model_copy(deep=True) for p in approved.pages]
    clear_pages[0] = clear_pages[0].model_copy(
        update={
            "visuals": [
                v.model_copy(update={"date_filter": None}) if v.id == target.id else v
                for v in clear_pages[0].visuals
            ]
        }
    )
    cleared = service.update_dashboard(
        approved.id,
        UpdateDashboardRequest(
            workspace_id=workspace.id,
            pages=clear_pages,
            idempotency_key="clear-chart-date-filter",
            expected_base_hash=approved.base_hash,
        ),
    )
    assert (
        sum(r["value"] for p in cleared.previews if p.visual_id == target.id for r in p.rows)
        == 1400
    )


def test_non_date_field_rejected_without_changing_draft(tmp_path):
    service, workspace, _, draft = _service_with_sources(tmp_path)
    pages = [p.model_copy(deep=True) for p in draft.pages]
    target = pages[0].visuals[0]
    target = target.model_copy(
        update={
            "date_filter": VisualDateFilter(
                table_id=target.table_id, column_id="sales", start="2025-02-01"
            )
        }
    )
    pages[0] = pages[0].model_copy(
        update={"visuals": [target if v.id == target.id else v for v in pages[0].visuals]}
    )
    with pytest.raises(StudioPlanningError, match="reviewed date field"):
        service.update_dashboard(
            draft.id,
            UpdateDashboardRequest(
                workspace_id=workspace.id,
                pages=pages,
                idempotency_key="invalid-chart-date-filter",
                expected_base_hash=draft.base_hash,
            ),
        )
    assert service.draft(draft.id) == draft
