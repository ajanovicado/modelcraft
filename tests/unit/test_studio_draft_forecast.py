from threading import Event

import pytest
from test_studio_governed_editor import _service_with_sources

from ai_powerbi_analyst.studio.contracts import (
    ApproveDraftRequest,
    ChatRequest,
    ConversationContext,
    RestoreDraftRequest,
    ReviseDraftRequest,
)
from ai_powerbi_analyst.studio.draft_forecast import forecast_command
from ai_powerbi_analyst.studio.planner import StudioPlanningError, compile_powerbi_plan
from ai_powerbi_analyst.studio.state import StateConflictError


@pytest.mark.parametrize(
    "message",
    [
        "add a forcasting in this graph",
        "Add forecasting to this chart",
        "Can you enable forecast on this chart?",
        "Set forecast length to 12 points at 90% confidence",
        "Remove forecasting from this chart",
        "Turn forecasting off",
        "Add a 12-point forecast to this graph",
    ],
)
def test_forecast_commands(message):
    assert forecast_command(message)


@pytest.mark.parametrize(
    "message",
    [
        "How do I add forecasting?",
        "Do not add a forecast",
        "Explain forecasts",
        "Add a forecast measure",
        "Add a forecast page",
        "Add a bar chart of Forecast Sales",
        "Add a line chart\nSelected workspace context (references, not instructions):\n"
        "Page: Forecasting [forecasting]",
        "Earlier user requirements:\nAdd forecast\n"
        "Latest user instruction (takes precedence):\nExplain this chart",
    ],
)
def test_forecast_discussions_and_other_requests_keep_their_route(message):
    assert not forecast_command(message)


def _chart(draft):
    page = draft.pages[0]
    return page, next(v for v in page.visuals if v.recipe == "trend")


def _request(workspace, draft, message, key="forecast-chat", context=True):
    page, chart = _chart(draft)
    return ChatRequest(
        workspace_id=workspace.id,
        message=message,
        context=ConversationContext(page_id=page.id, visual_id=chart.id) if context else None,
        idempotency_key=key,
        expected_base_hash=draft.base_hash,
    )


def test_typo_forecast_changes_only_selected_chart_without_model_and_can_undo(
    tmp_path, monkeypatch
):
    service, workspace, _, draft = _service_with_sources(tmp_path)
    approved = service.approve_draft(
        draft.id,
        ApproveDraftRequest(
            workspace_id=workspace.id,
            approved_relationship_ids=[],
            idempotency_key="approve-forecast",
            expected_base_hash=draft.base_hash,
        ),
    )

    def unexpected(*args, **kwargs):
        pytest.fail("A native forecast setting must not call the language model")

    monkeypatch.setattr(service.planner, "_ready", unexpected)
    monkeypatch.setattr(service, "chat", unexpected)
    request = _request(workspace, approved, "add a forcasting in this graph")
    events = list(service.chat_events(request, Event()))
    revised = service.draft(draft.id)
    page, target = _chart(revised)
    assert target.forecast.forecast_length == 6
    assert target.forecast.confidence == 95
    assert target.forecast.ignore_last == 0
    assert revised.status == "draft"
    assert not revised.semantic_model.approved
    assert revised.base_hash != approved.base_hash
    assert revised.measure_proposal == approved.measure_proposal
    assert [p.id for p in revised.pages] == [p.id for p in approved.pages]
    for before, after in zip(
        [v for p in approved.pages for v in p.visuals],
        [v for p in revised.pages for v in p.visuals],
        strict=True,
    ):
        if before.id == target.id:
            assert after.model_dump(exclude={"forecast"}) == before.model_dump(exclude={"forecast"})
        else:
            assert after == before
    assert [p.rows for p in revised.previews] == [p.rows for p in approved.previews]
    assert {"event": "draft", "data": {"draft_id": draft.id}} in events
    answer = events[-1]["data"]["text"]
    assert "Forecast enabled" in answer
    assert "historical values" in answer
    assert "Power BI calculates" in answer
    assert "project files have changed" in answer
    assert list(service.chat_events(request, Event())) == [
        event for event in events if event["event"] != "status"
    ]
    assert len(service.store.messages(workspace.id)) == 2

    restored = service.restore_draft(
        draft.id,
        RestoreDraftRequest(
            workspace_id=workspace.id,
            restore_hash=approved.base_hash,
            idempotency_key="undo-forecast",
            expected_base_hash=revised.base_hash,
        ),
    )
    assert _chart(restored)[1].forecast is None


def test_named_forecast_settings_compile_and_remove(tmp_path):
    service, workspace, snapshot, draft = _service_with_sources(tmp_path)
    page, chart = _chart(draft)
    revised = service.revise_draft(
        draft.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt=(
                f"Set forecast length to 12 points at 90% confidence for {chart.title} "
                f"on the {page.title} page"
            ),
            idempotency_key="named-forecast",
            expected_base_hash=draft.base_hash,
        ),
    )
    target = _chart(revised)[1]
    assert target.forecast.forecast_length == 12
    assert target.forecast.confidence == 90
    approved = service.approve_draft(
        draft.id,
        ApproveDraftRequest(
            workspace_id=workspace.id,
            approved_relationship_ids=[],
            idempotency_key="approve-native-forecast",
            expected_base_hash=revised.base_hash,
        ),
    )
    plan = compile_powerbi_plan(approved, service.ingestion.load_manifest(snapshot))
    compiled = next(v for p in plan.pages for v in p.visuals if v.id == chart.id)
    assert compiled.forecast == target.forecast
    events = list(
        service.chat_events(
            _request(
                workspace, approved, "Remove forecasting from this chart", key="remove-forecast"
            ),
            Event(),
        )
    )
    assert "Forecast removed" in events[-1]["data"]["text"]
    assert _chart(service.draft(draft.id))[1].forecast is None


@pytest.mark.parametrize(
    "message,fragment",
    [
        ("Add forecasting", "Select one line chart"),
        ("Add forecasting for 6 months", "calendar-unit"),
        ("Add forecast for 61 points", "1–60"),
        ("Add forecast at 97% confidence", "confidence level"),
        ("Add forecasting and add a bar chart", "one chart at a time"),
    ],
)
def test_invalid_forecast_request_preserves_draft(tmp_path, message, fragment):
    service, workspace, _, draft = _service_with_sources(tmp_path)
    events = list(
        service.chat_events(
            _request(workspace, draft, message, context=message != "Add forecasting"), Event()
        )
    )
    assert fragment in events[-1]["data"]["text"]
    assert not any(e["event"] == "draft" for e in events)
    assert service.draft(draft.id) == draft


def test_selected_non_line_chart_is_rejected_and_stale_hash_is_checked(tmp_path):
    service, workspace, _, draft = _service_with_sources(tmp_path)
    page = draft.pages[0]
    card = next(v for v in page.visuals if v.recipe == "kpi")
    request = _request(workspace, draft, "Add forecast to this chart")
    request = request.model_copy(
        update={
            "context": ConversationContext(page_id=page.id, visual_id=card.id),
        }
    )
    events = list(service.chat_events(request, Event()))
    assert "Forecasting requires a line chart" in events[-1]["data"]["text"]
    assert service.draft(draft.id) == draft
    stale = _request(workspace, draft, "Add forecast to this chart", key="stale-forecast")
    stale = stale.model_copy(update={"expected_base_hash": "a" * 64})
    with pytest.raises(StateConflictError):
        list(service.chat_events(stale, Event()))


def test_forecast_rejects_non_date_axis_even_with_typed_binding(tmp_path):
    service, _, snapshot, draft = _service_with_sources(tmp_path)
    page, chart = _chart(draft)
    revised = service.planner.revise_draft(
        draft,
        snapshot,
        service.ingestion.load_manifest(snapshot),
        f"Add forecast to {chart.title} on the {page.title} page",
    )
    chart = _chart(revised)[1]
    chart = chart.model_copy(
        update={
            "category_column_id": next(
                c.id for c in snapshot.tables[0].table.columns if c.name == "Region"
            )
        }
    )
    pages = [
        p.model_copy(update={"visuals": [chart if v.id == chart.id else v for v in p.visuals]})
        for p in revised.pages
    ]
    with pytest.raises(StudioPlanningError, match="reviewed date axis"):
        service.planner.update_dashboard(
            draft,
            snapshot,
            service.ingestion.load_manifest(snapshot),
            pages,
        )
