"""DAX Lab routes shared patterns without inventing execution evidence or fallback DAX."""

from __future__ import annotations

import re
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from ai_powerbi_analyst.studio import dax_assistant
from ai_powerbi_analyst.studio.contracts import (
    CreateWorkspaceRequest,
    DaxAssistRequest,
    IngestRequest,
)
from ai_powerbi_analyst.studio.dax_patterns import MetricSelection
from ai_powerbi_analyst.studio.report_import import ImportedPage, ImportedReport
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "orders.csv"
    source.write_text(
        "Region,Sales,Profit,Qty\nEast,100,20,2\nWest,200,40,4\nEast,50,10,1\n",
        encoding="utf-8",
    )
    model = SimpleNamespace(
        generate=lambda *_args, **_kwargs: pytest.fail("Shared pattern used freeform DAX"),
        stream_chat=lambda *_args, **_kwargs: pytest.fail("Shared pattern used model explanation"),
        unload=lambda: None,
    )
    monkeypatch.setattr(
        "ai_powerbi_analyst.studio.service.create_model_runtime", lambda *_args: model
    )
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Business pattern lab",
            selected_folder=str(tmp_path),
            idempotency_key="pattern-lab-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="pattern-lab-ingest",
            expected_base_hash=workspace.base_hash,
        )
    )
    return service, workspace.id


def request_for(service: StudioService, workspace_id: str, goal: str, **fields):
    return DaxAssistRequest(
        workspace_id=workspace_id,
        snapshot_id=service.workspace(workspace_id).active_snapshot_id,
        request_id="shared-business-pattern",
        goal=goal,
        **fields,
    )


def answer_events(service: StudioService, request: DaxAssistRequest, cancel: Event | None = None):
    events = list(dax_assistant.dax_assistant_events(service, request, cancel or Event()))
    text = "".join(event["data"]["text"] for event in events if event["event"] == "token")
    return events, text


def forbid_fallbacks(service: StudioService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service, "imported_lab_formula", lambda *_args: pytest.fail("Used legacy imported formula")
    )
    monkeypatch.setattr(
        dax_assistant,
        "_time_intelligence_answer",
        lambda *_args: pytest.fail("Used legacy unexecuted time-intelligence explanation"),
    )
    monkeypatch.setattr(
        dax_assistant,
        "_desktop_check",
        lambda *_args: pytest.fail("Shared local result probed Desktop"),
    )
    monkeypatch.setattr(
        service, "evaluate_dax", lambda *_args: pytest.fail("Shared result evaluated a second time")
    )


@pytest.mark.parametrize("mode", ["generate", "auto"])
def test_shared_scalar_result_preserves_actual_numbers_without_native_check(
    lab, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    service, workspace_id = lab
    formula = "DIVIDE(SUM('orders'[Profit]), SUM('orders'[Sales]))"
    reading = (
        "Local preview (all snapshot rows; page slicer selections are not applied): Value = 0.2."
    )
    calls = []
    cancel = Event()

    def shared(workspace: str, goal: str, *, cancel: Event | None = None):
        calls.append((workspace, goal, cancel))
        return formula, "local", reading

    monkeypatch.setattr(service, "business_pattern_formula", shared, raising=False)
    forbid_fallbacks(service, monkeypatch)
    before = service.workspace(workspace_id)
    request = request_for(service, workspace_id, "Calculate Profit divided by Sales", mode=mode)
    events, text = answer_events(service, request, cancel)
    assert calls == [(workspace_id, request.goal, cancel)]
    assert events[1] == {"event": "assistant", "data": {"kind": "formula"}}
    assert text == f"```dax\n{formula}\n```\n\n{reading}"
    assert "Validated by Power BI Desktop" not in text
    assert "Not validated" not in text
    assert "same query chat uses" not in text
    assert service.workspace(workspace_id) == before
    assert service.messages(workspace_id) == []
    audit = next(
        event["payload"]
        for event in service.events(workspace_id)
        if event["type"] == "dax.assistant_answered"
    )
    assert audit["status"] == "completed"
    assert audit["mode"] == "generate"
    assert audit["response"] == text
    assert audit["calculation_request"] is None
    assert not audit["reference_context_supplied"]


def test_shared_top_n_query_keeps_table_sample_and_formula_route(lab, monkeypatch) -> None:
    service, workspace_id = lab
    formula = (
        "EVALUATE TOPN(2, SUMMARIZECOLUMNS('orders'[Region], "
        "\"Value\", SUM('orders'[Sales])), [Value], DESC)"
    )
    reading = (
        "Local preview (all snapshot rows; page slicer selections are not applied): "
        "Region = West, Value = 200; Region = East, Value = 150."
    )
    monkeypatch.setattr(
        service,
        "business_pattern_formula",
        lambda *_args, **_kwargs: (formula, "local", reading),
        raising=False,
    )
    forbid_fallbacks(service, monkeypatch)
    events, text = answer_events(
        service, request_for(service, workspace_id, "Show top 2 regions by Sales")
    )
    assert events[1]["data"]["kind"] == "formula"
    assert f"```dax\n{formula}\n```" in text
    assert reading in text
    assert "Power BI Desktop" not in text


def test_recognized_pattern_clarification_cannot_fall_back_or_emit_formula(lab, monkeypatch):
    service, workspace_id = lab
    clarification = "Choose all dates or the visible date selection for the running total."
    monkeypatch.setattr(
        service,
        "business_pattern_formula",
        lambda *_args, **_kwargs: ("", "clarify", clarification),
        raising=False,
    )
    forbid_fallbacks(service, monkeypatch)
    before = service.workspace(workspace_id)
    events, text = answer_events(
        service, request_for(service, workspace_id, "Calculate the Sales running total")
    )
    assert events[1]["data"]["kind"] == "advice"
    assert text == clarification
    assert "```" not in text
    assert "preview" not in text.casefold()
    assert service.workspace(workspace_id) == before
    audit = next(
        event["payload"]
        for event in service.events(workspace_id)
        if event["type"] == "dax.assistant_answered"
    )
    assert audit["mode"] == "advice"
    assert audit["calculation_request"] is None


def test_fixed_grain_clarification_followup_uses_new_goal_not_prior_formula(lab, monkeypatch):
    service, workspace_id = lab
    forbid_fallbacks(service, monkeypatch)
    before = service.workspace(workspace_id)
    goal = "Create fixed Sales by Region"
    prior_formula = "CALCULATE(SUM('orders'[Profit]), 'orders'[Region] = \"East\")"
    first = request_for(service, workspace_id, goal, expression=prior_formula)
    events, question = answer_events(service, first)
    assert events[1]["data"]["kind"] == "advice"
    assert "Should 'orders'[Sales] ignore other filters while keeping 'orders'[Region]?" in question
    assert "```" not in question
    second = request_for(
        service,
        workspace_id,
        "ignore other filters",
        mode="auto",
        expression=prior_formula,
        discussion=f"User: {goal}\nAssistant: {question}",
    ).model_copy(update={"request_id": "fixed-filter-followup"})
    events, answer = answer_events(service, second)
    assert events[1]["data"]["kind"] == "formula"
    assert "CALCULATE(SUM('orders'[Sales]), ALLEXCEPT('orders', 'orders'[Region]))" in answer
    assert "Value = 350" in answer
    assert "[Profit]" not in answer
    assert service.workspace(workspace_id) == before
    receipts = [
        event["payload"]
        for event in service.events(workspace_id)
        if event["type"] == "dax.assistant_answered"
    ]
    assert [receipt["mode"] for receipt in receipts] == ["advice", "generate"]
    assert all(receipt["status"] == "completed" for receipt in receipts)


def test_fixed_grain_keep_filters_followup_clarifies_instead_of_dropping_filters(lab, monkeypatch):
    service, workspace_id = lab
    forbid_fallbacks(service, monkeypatch)
    goal = "Create fixed Sales by Region"
    question = (
        "Should Sales ignore other filters while keeping Region? "
        "Reply 'ignore other filters', or describe the filters to keep."
    )
    events, answer = answer_events(
        service,
        request_for(
            service,
            workspace_id,
            "keep other filters",
            mode="auto",
            expression="SUM('orders'[Profit])",
            discussion=f"User: {goal}\nAssistant: {question}",
        ),
    )
    assert events[1]["data"]["kind"] == "advice"
    assert "ordinary grouped total" in answer
    assert "```" not in answer


def test_fixed_grain_refinement_cannot_bypass_filter_guard_using_editor_formula(lab, monkeypatch):
    service, workspace_id = lab
    forbid_fallbacks(service, monkeypatch)
    events, answer = answer_events(
        service,
        request_for(
            service,
            workspace_id,
            "Create fixed Sales by Region, keep other filters",
            expression="SUM('orders'[Profit])",
        ),
    )
    assert events[1]["data"]["kind"] == "advice"
    assert "ordinary grouped total" in answer
    assert "```" not in answer


@pytest.mark.parametrize("reply", ["ignore other filters", "yes"])
def test_fixed_grain_followup_cannot_reuse_an_older_request(lab, reply):
    service, workspace_id = lab
    old = (
        "User: Create fixed Sales by Region\nAssistant: Should Sales ignore other filters "
        "while keeping Region? Reply 'ignore other filters'."
    )
    request = request_for(
        service,
        workspace_id,
        reply,
        discussion=old
        + "\n\nUser: Create a Profit running total\nAssistant: Choose all dates or selected dates.",
    )
    assert dax_assistant._fixed_grain_followup(request) is None


def test_open_ended_complex_calculation_asks_purpose_without_model_or_editor_change(
    lab, monkeypatch
):
    service, workspace_id = lab
    forbid_fallbacks(service, monkeypatch)
    monkeypatch.setattr(
        service,
        "business_pattern_formula",
        lambda *_args, **_kwargs: pytest.fail("An unspecified purpose reached pattern selection"),
    )
    before = service.workspace(workspace_id)
    events, answer = answer_events(
        service,
        request_for(
            service,
            workspace_id,
            "Create the most complex DAX you can, you pick everything",
            expression="SUM('orders'[Sales])",
        ),
    )
    assert events[0]["data"]["text"] == "Clarifying your calculation…"
    assert events[1]["data"]["kind"] == "advice"
    assert "What business question" in answer
    assert "Your current formula is unchanged" in answer
    assert "```" not in answer
    assert service.workspace(workspace_id) == before
    receipt = next(
        event["payload"]
        for event in service.events(workspace_id)
        if event["type"] == "dax.assistant_answered"
    )
    assert receipt["status"] == "completed"
    assert receipt["mode"] == "advice"
    assert receipt["calculation_request"] is None


def test_shared_blank_result_does_not_claim_native_validation(lab, monkeypatch) -> None:
    service, workspace_id = lab
    reading = "Local preview returned BLANK; inspect the denominator before using the ratio."
    monkeypatch.setattr(
        service,
        "business_pattern_formula",
        lambda *_args, **_kwargs: ("DIVIDE(0, 0)", "local-blank", reading),
        raising=False,
    )
    forbid_fallbacks(service, monkeypatch)
    _, text = answer_events(service, request_for(service, workspace_id, "Calculate a ratio"))
    assert text == f"```dax\nDIVIDE(0, 0)\n```\n\n{reading}"
    assert "Checked" not in text
    assert "Validated" not in text


def test_unrecognized_csv_goal_keeps_legacy_time_intelligence_route(lab, monkeypatch) -> None:
    service, workspace_id = lab
    monkeypatch.setattr(
        service, "business_pattern_formula", lambda *_args, **_kwargs: None, raising=False
    )
    legacy = "Legacy time-intelligence guidance; the formula was not run."
    monkeypatch.setattr(dax_assistant, "_time_intelligence_answer", lambda *_args: legacy)
    events, text = answer_events(
        service, request_for(service, workspace_id, "Calculate custom YTD")
    )
    assert events[1]["data"]["kind"] == "advice"
    assert text == legacy


def test_unrecognized_imported_goal_keeps_legacy_planned_query(lab, monkeypatch) -> None:
    service, workspace_id = lab
    workspace = service.workspace(workspace_id).model_copy(
        update={"imported_report_id": "report-test"}
    )
    report = ImportedReport(
        id="report-test",
        workspace_id=workspace_id,
        name="Synthetic imported report",
        source_path="report.pbix",
        archived_path="report.pbix",
        source_hash="a" * 64,
        base_hash="a" * 64,
        format="PBIR",
        pages=[ImportedPage(id="overview", title="Overview")],
    )
    monkeypatch.setattr(service, "workspace", lambda _workspace_id: workspace)
    monkeypatch.setattr(service, "imported_report", lambda _workspace_id: report)
    monkeypatch.setattr(
        service, "business_pattern_formula", lambda *_args, **_kwargs: None, raising=False
    )
    calls = []

    def legacy(*args):
        calls.append(args)
        return "SUM('orders'[Sales])", "snapshot", "Legacy imported planner reading."

    monkeypatch.setattr(service, "imported_lab_formula", legacy)
    monkeypatch.setattr(dax_assistant, "_desktop_check", lambda *_args: "closed")
    _, text = answer_events(service, request_for(service, workspace_id, "Show total sales"))
    assert calls == [(workspace_id, "Show total sales")]
    assert "Checked: it ran on the imported data" in text
    assert "Legacy imported planner reading." in text


def test_refining_existing_expression_skips_shared_patterns(lab, monkeypatch) -> None:
    service, workspace_id = lab
    calls = []

    def generate(_system, _message, response_model, **_kwargs):
        calls.append(response_model)
        return response_model(expression="SUM('orders'[Profit])")

    monkeypatch.setattr(service.model, "generate", generate)
    monkeypatch.setattr(
        service,
        "business_pattern_formula",
        lambda *_args, **_kwargs: pytest.fail("Existing formula refinement selected a new pattern"),
        raising=False,
    )
    events, text = answer_events(
        service,
        request_for(
            service,
            workspace_id,
            "Instead use Profit",
            expression="SUM('orders'[Sales])",
            mode="generate",
        ),
    )
    assert len(calls) == 1
    assert events[1]["data"]["kind"] == "formula"
    assert "SUM('orders'[Profit])" in text


def test_real_shared_ratio_executes_snapshot_and_shows_actual_fraction(lab, monkeypatch) -> None:
    service, workspace_id = lab
    monkeypatch.setattr(
        dax_assistant,
        "_desktop_check",
        lambda *_args: pytest.fail("A computed shared ratio should not probe Desktop"),
    )
    before = service.workspace(workspace_id)
    events, text = answer_events(
        service, request_for(service, workspace_id, "Calculate Profit divided by Sales")
    )
    assert events[1]["data"]["kind"] == "formula"
    assert "DIVIDE(SUM('orders'[Profit]), SUM('orders'[Sales]))" in text
    assert "Local preview (all snapshot rows; page slicer selections are not applied)" in text
    assert "Value = 0.2" in text
    assert "Power BI Desktop" not in text
    assert service.workspace(workspace_id) == before


def test_real_shared_top_n_selects_only_slots_and_shows_actual_table_values(
    lab, monkeypatch
) -> None:
    service, workspace_id = lab
    selections = []

    def generate(system, message, response_model, **_kwargs):
        assert "Never write DAX" in system
        assert "expression" not in response_model.model_fields
        selections.append(message)
        sales = re.search(r"(?m)^(\d+) column number orders / Sales$", message)
        region = re.search(r"(?m)^(\d+) column string orders / Region$", message)
        assert sales is not None and region is not None
        return response_model(
            pattern="top_n",
            base=MetricSelection(field=int(sales[1]), aggregation="sum"),
            group=int(region[1]),
            n=2,
            direction="descending",
            scope="all",
        )

    monkeypatch.setattr(service.model, "generate", generate)
    monkeypatch.setattr(
        dax_assistant,
        "_desktop_check",
        lambda *_args: pytest.fail("A computed shared top-N should not probe Desktop"),
    )
    events, text = answer_events(
        service, request_for(service, workspace_id, "Show top 2 Region by Sales across all groups")
    )
    assert len(selections) == 1
    assert events[1]["data"]["kind"] == "formula"
    assert "EVALUATE TOPN(2," in text
    assert "orders[Region] = West, __PatternValue = 200" in text
    assert "orders[Region] = East, __PatternValue = 150" in text
    assert "page slicer selections are not applied" in text
    assert "Validated by Power BI Desktop" not in text


def test_real_recognized_ratio_with_unknown_operand_asks_without_model(lab) -> None:
    service, workspace_id = lab
    before = service.workspace(workspace_id)
    events, text = answer_events(
        service, request_for(service, workspace_id, "Calculate Missing divided by Sales")
    )
    assert events[1]["data"]["kind"] == "advice"
    assert "Name the model field" in text
    assert "No formula or report change was applied" in text
    assert "```" not in text
    assert "Local preview" not in text
    assert service.workspace(workspace_id) == before


@pytest.mark.parametrize(
    "goal",
    [
        "Share of Sales for West large orders",
        "Share of Sales in West with approved customers",
        "Share of Sales for West except canceled orders",
        "Year over year percentage change of Sales for West",
        "YoY share of Sales in West",
        "Moving average of the percentage of Profit to Sales for West",
        "Sales as a percentage of Profit for West in 2024",
        "Sales as a percentage of Profit for West this year",
        "Sales as a percentage of Profit for West during the current month",
    ],
)
def test_reviewed_literal_does_not_bypass_additional_or_new_pattern_scope(lab, goal) -> None:
    service, workspace_id = lab
    before = service.workspace(workspace_id)
    events, text = answer_events(service, request_for(service, workspace_id, goal))
    assert events[1]["data"]["kind"] == "advice"
    assert "No formula or report change was applied" in text
    assert "```" not in text and "Local preview" not in text
    assert service.workspace(workspace_id) == before


def test_progress_arrives_before_pattern_selection_and_cancel_stops_fallback(lab, monkeypatch):
    service, workspace_id = lab
    cancel = Event()
    calls = []

    def shared(*_args, **_kwargs):
        calls.append(True)
        cancel.set()
        return None

    monkeypatch.setattr(service, "business_pattern_formula", shared)
    forbid_fallbacks(service, monkeypatch)
    stream = dax_assistant.dax_assistant_events(
        service, request_for(service, workspace_id, "Calculate a business ratio"), cancel
    )
    assert next(stream) == {"event": "status", "data": {"text": "Understanding your calculation…"}}
    assert calls == []
    assert list(stream) == []
    assert calls == [True]
