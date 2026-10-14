"""The model-revision gate: accept only answers to what the user asked for.

The shrunk revision prompt lets the local model finish in time, so wrong but valid
answers reach _revision_satisfies_request. Model doubles here stand in for the local
runtime; they make contract claims, not inference-quality claims.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple

import pytest

from ai_powerbi_analyst.contracts import SourceManifest
from ai_powerbi_analyst.studio import planner
from ai_powerbi_analyst.studio.contracts import (
    CreateWorkspaceRequest,
    DashboardDraft,
    DatasetSnapshot,
    DraftRequest,
    GovernedMeasureSpec,
    IngestRequest,
    MeasureProposal,
)
from ai_powerbi_analyst.studio.conversation import revision_prompt
from ai_powerbi_analyst.studio.planner import StudioPlanningError, StudioPlanningService
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH

# Superstore-shaped: Order ID is a key; Ship Mode, Segment, Region and Sub-Category repeat.
ORDERS = (
    "Order ID,Order Date,Ship Mode,Segment,Region,Category,Sub-Category,"
    "Sales,Quantity,Discount,Profit\n"
    "A-1,2025-01-03,Standard Class,Consumer,East,Furniture,Chairs,100.00,2,0.10,20.00\n"
    "A-2,2025-02-05,Second Class,Corporate,West,Technology,Phones,250.00,3,0.00,40.00\n"
    "A-3,2025-03-07,Standard Class,Consumer,East,Furniture,Tables,150.00,1,0.20,30.00\n"
    "A-4,2025-03-09,First Class,Home Office,West,Technology,Phones,120.00,5,0.10,10.00\n"
    "A-5,2025-04-11,Standard Class,Consumer,East,Furniture,Chairs,90.00,2,0.00,15.00\n"
    "A-6,2025-04-13,Second Class,Corporate,West,Furniture,Tables,80.00,4,0.30,5.00\n"
)
GATE_REJECTED = "did not satisfy the request"
UNSUPPORTED = "This visual is not yet authorable in Studio. The report has not changed."
R3 = "Add a new page comparing average discount and total quantity by ship mode"
# (recipe, visual type, title, measure name, category column)
Spec = tuple[str, str, str, str, str | None]


def _chart(title: str, measure: str, category: str, visual_type: str = "barChart") -> Spec:
    return ("category", visual_type, title, measure, category)


def _card(measure: str) -> Spec:
    return ("kpi", "cardVisual", measure, measure, None)


class _Model:
    """Returns scripted answers, validated as a runtime would, and records every call."""

    def __init__(self, *answers: dict[str, Any]) -> None:
        self.answers = list(answers)
        self.calls: list[str] = []

    def health(self) -> SimpleNamespace:
        return SimpleNamespace(model_available=True)

    def generate(self, _system: str, prompt: str, response_model: Any, **_options: Any) -> Any:
        self.calls.append(json.loads(prompt)["request"])
        return response_model.model_validate(self.answers.pop(0))


class _Studio(NamedTuple):
    service: StudioService
    snapshot: DatasetSnapshot
    manifest: SourceManifest
    draft: DashboardDraft

    def table(self, name: str) -> str:
        return next(table.id for table in self.manifest.tables if table.name == name)

    def measure(self, name: str) -> str:
        return next(item.id for item in self.draft.measure_proposal.measures if item.name == name)

    def revise(
        self, model: _Model, prompt: str, draft: DashboardDraft | None = None
    ) -> DashboardDraft:
        self.service.planner.model = model  # type: ignore[assignment]
        return self.service.planner.revise_draft(
            draft or self.draft, self.snapshot, self.manifest, prompt
        )

    def with_measures(self, *extra: GovernedMeasureSpec) -> DashboardDraft:
        return self.draft.model_copy(
            update={
                "measure_proposal": MeasureProposal(
                    measures=[*self.draft.measure_proposal.measures, *extra]
                )
            }
        )


@pytest.fixture(scope="module")
def studio(tmp_path_factory: pytest.TempPathFactory) -> _Studio:
    # Revisions return a new draft without storing it, so one workspace serves every test.
    root = tmp_path_factory.mktemp("revision-gate")
    source = root / "selected" / "orders.csv"
    source.parent.mkdir()
    source.write_text(ORDERS, encoding="utf-8")
    service = StudioService(Path(__file__).resolve().parents[2], root / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Revision gate",
            selected_folder=str(source.parent),
            idempotency_key="create-revision-gate",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="ingest-revision-gate",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Build a sales dashboard",
            idempotency_key="draft-revision-gate",
            expected_base_hash=workspace.base_hash,
        )
    )
    return _Studio(service, snapshot, service.ingestion.load_manifest(snapshot), draft)


def _page(title: str, *visuals: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"pages": [{"title": title, "visuals": list(visuals)}], **extra}


def _visual(recipe: str, visual_type: str, title: str, **binding: Any) -> dict[str, Any]:
    return {"recipe": recipe, "visual_type": visual_type, "title": title, **binding}


def _answer(studio: _Studio, *specs: Spec, draft: DashboardDraft | None = None) -> dict[str, Any]:
    """A "Model Answer" page whose visuals name draft measures by their names."""
    ids = {item.name: item.id for item in (draft or studio.draft).measure_proposal.measures}
    return _page(
        "Model Answer",
        *(
            _visual(recipe, visual_type, title, measure_id=ids[measure])
            | ({"category_column_id": category} if category else {})
            for recipe, visual_type, title, measure, category in specs
        ),
    )


def _change(studio: _Studio, name: str, period: str | None, base: str = "Total Sales") -> dict:
    measure = {
        "id": "model_change",
        "name": name,
        "operation": "period_change",
        "table_id": studio.table("orders"),
        "base_measure_id": studio.measure(base),
        "date_column_id": "order_date",
    }
    if period is not None:
        measure["period"] = period
    return measure


def _gate(
    studio: _Studio,
    caplog: pytest.LogCaptureFixture,
    request: str,
    answer: dict[str, Any],
    draft: DashboardDraft | None = None,
) -> tuple[bool, DashboardDraft | None]:
    """(the gate accepted the model's answer, the revised draft or None if refused)."""
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=planner.__name__):
        try:
            revised = studio.revise(_Model(answer), request, draft)
        except StudioPlanningError:
            revised = None
    assert "candidate rejected" not in caplog.text, "the answer must be valid; only the gate"
    return GATE_REJECTED not in caplog.text, revised


def _added_measures(studio: _Studio, revised: DashboardDraft) -> list[GovernedMeasureSpec]:
    existing = {item.id for item in studio.draft.measure_proposal.measures}
    return [
        item
        for item in revised.measure_proposal.measures
        if item.id not in existing and isinstance(item, GovernedMeasureSpec)
    ]


# --- H1 / M2: the period the user asked for is the one saved ---------------------------


@pytest.mark.parametrize(
    ("request_text", "model_period", "expected"),
    [
        # The generic "growth" rule used to turn each of these correct answers into YoY.
        ("Add a month-over-month growth measure for sales", "month", "month"),
        ("Add a quarter-over-quarter growth measure for sales", "quarter", "quarter"),
        ("Add a month on month sales growth measure", "month", "month"),
        ("Add a sales growth vs last month measure", "month", "month"),
        ("Add a monthly sales growth measure", "month", "month"),
        # The original fix: an explicit YoY request outranks the model's month.
        ("Add a YoY growth measure for sales", "month", "year"),
        ("Add a month over month growth measure for sales", "year", "month"),
        # Generic growth never overrides a period the model chose...
        ("Add a sales growth measure", "quarter", "quarter"),
        # ...but fills one the model omitted, instead of the schema's "month".
        ("Add a sales growth measure", None, "year"),
        ("Add a year-over-year sales measure", None, "year"),
        ("Add a year on year sales change measure", None, "year"),
        ("Create a measure for year over year sales growth %", None, "year"),
    ],
)
def test_the_saved_period_is_the_one_the_user_asked_for(
    studio: _Studio,
    caplog: pytest.LogCaptureFixture,
    request_text: str,
    model_period: str | None,
    expected: str,
) -> None:
    accepted, revised = _gate(
        studio, caplog, request_text, {"measures": [_change(studio, "Model Change", model_period)]}
    )

    assert accepted and revised is not None
    (added,) = _added_measures(studio, revised)
    assert (added.id, added.period) == ("model_change", expected)


@pytest.mark.parametrize("model_period", ["month", "year", None])
def test_a_follow_up_period_outranks_the_earlier_unapplied_request(
    studio: _Studio, caplog: pytest.LogCaptureFixture, model_period: str | None
) -> None:
    # The first ask failed (no field named); "it" makes the follow-up carry it along.
    history = [
        {"role": "user", "content": "Add a YoY growth measure"},
        {"role": "assistant", "content": "I couldn't apply that change to the draft."},
    ]
    prompt = revision_prompt("make it monthly instead, for sales", history)
    assert "Latest user instruction (takes precedence):" in prompt

    accepted, revised = _gate(
        studio, caplog, prompt, {"measures": [_change(studio, "Sales Change", model_period)]}
    )

    assert accepted and revised is not None
    (added,) = _added_measures(studio, revised)
    assert added.period == "month"


def test_a_follow_up_to_an_applied_change_is_an_edit_that_is_refused(
    studio: _Studio,
) -> None:
    # After "Draft updated", "make it monthly" can only mean editing the saved measure.
    history = [
        {"role": "user", "content": "Add a YoY growth measure for sales"},
        {"role": "assistant", "content": "Draft updated: 1 pages and 6 measures are ready."},
    ]
    prompt = revision_prompt("make it monthly instead, for sales", history)
    model = _Model({"measures": [_change(studio, "Sales Change", "month")]})

    with pytest.raises(StudioPlanningError) as refused:
        studio.revise(model, prompt)

    assert str(refused.value) == planner._EDIT_REQUEST_MESSAGE
    assert model.calls == []


def test_period_patterns_are_shared_with_dax_lab_and_ignore_a_bare_monthly_trend() -> None:
    def first(text: str) -> tuple[str, str] | None:
        return next(
            (
                (operation, period)
                for pattern, operation, period, _label in planner._TIME_INTELLIGENCE
                if re.search(pattern, text.casefold())
            ),
            None,
        )

    assert first("Sales month-over-month") == ("period_change", "month")
    assert first("Profit compared with the previous quarter") == ("period_change", "quarter")
    assert first("Sales year-to-date") == ("period_to_date", "year")
    assert first("A running-total of profit") == ("running_total", "month")
    # A monthly trend is a trend visual; requiring a period_change for it is wrong.
    assert first("Add a monthly sales trend") is None


# --- M2 / M3: the gate checks the time operation and its period --------------------------


def test_hyphenated_year_over_year_page_needs_a_yoy_measure(
    studio: _Studio, caplog: pytest.LogCaptureFixture
) -> None:
    answer = _answer(studio, _chart("Sales by Region", "Total Sales", "region"))

    accepted, _revised = _gate(
        studio, caplog, "Add a page showing year-over-year sales by region", answer
    )

    assert not accepted


def test_a_yoy_page_is_not_answered_by_the_drafts_mom_measure(
    studio: _Studio, caplog: pytest.LogCaptureFixture
) -> None:
    common = {
        "operation": "period_change",
        "table_id": studio.table("orders"),
        "base_measure_id": studio.measure("Total Sales"),
        "date_column_id": "order_date",
        "format_string": "0.0%",
    }
    mom = GovernedMeasureSpec(id="sales_mom_pct", name="Sales MoM %", period="month", **common)
    yoy = GovernedMeasureSpec(id="sales_yoy_pct", name="Sales YoY %", period="year", **common)
    draft = studio.with_measures(mom, yoy)
    request = "Add a page showing year over year sales growth by region"

    def answer(measure: str) -> dict[str, Any]:
        return _answer(studio, _chart("Sales Growth by Region", measure, "region"), draft=draft)

    assert not _gate(studio, caplog, request, answer(mom.name), draft)[0]
    assert _gate(studio, caplog, request, answer(yoy.name), draft)[0]


# --- H2: unsupported visuals are refused before the model runs ---------------------------


@pytest.mark.parametrize(
    "request_text",
    [
        "create a year over year matirx at the bottom",
        "Add a page with a matrix of sales by region",
        "Add a new page with a matrx of sales by region",
        "Add a martix of profit by segment",
        "Add a matix of profit by segment",
        "Add a page with a pivot table of sales",
        "Add a region slicer",
        "Add a gauge for total profit",
        "Add a page with a scatter chart of sales and profit",
        "Add a treemap of sales by category",
        "Add a combo chart of sales and profit",
        "Add a ribbon chart of sales by region",
        "Add a decomposition tree for profit",
        "Add key influencers for profit",
        "Add a multi-row card with sales and profit",
    ],
)
def test_unsupported_visuals_are_refused_before_any_model_call(
    studio: _Studio, request_text: str
) -> None:
    model = _Model()

    with pytest.raises(StudioPlanningError) as refused:
        studio.revise(model, request_text)

    assert str(refused.value) == UNSUPPORTED
    assert model.calls == []


@pytest.mark.parametrize(
    "request_text",
    [
        "Add a page with the key metrics for sales by region",
        "Add a metrix page for sales by region",
        "Add a page for matric results by region",
    ],
)
def test_metrics_and_matric_words_are_not_a_matrix(
    studio: _Studio, caplog: pytest.LogCaptureFixture, request_text: str
) -> None:
    answer = _answer(studio, _chart("Sales by Region", "Total Sales", "region"))

    accepted, revised = _gate(studio, caplog, request_text, answer)

    assert accepted and revised is not None


def test_names_the_data_already_uses_are_not_visual_requests(studio: _Studio) -> None:
    draft = studio.draft.model_copy(
        update={
            "pages": [
                *studio.draft.pages,
                studio.draft.pages[0].model_copy(
                    update={"id": "gauge_review", "title": "Gauge Review"}
                ),
            ]
        }
    )
    catalog = planner._catalog(studio.manifest)

    assert not planner._requests_unsupported_visual(
        "Add a page like Gauge Review for profit", draft, catalog
    )
    assert planner._requests_unsupported_visual("Add a gauge for profit", draft, catalog)


def test_an_existing_page_placement_is_not_answered_with_a_new_page(
    studio: _Studio, caplog: pytest.LogCaptureFixture
) -> None:
    yoy = _change(studio, "Sales YoY %", "year")
    request = "Put the YoY sales growth below the trend"
    page = _page(
        "Model Answer",
        _visual("kpi", "cardVisual", "Sales YoY %", measure_id="model_change"),
        measures=[yoy],
    )

    assert not _gate(studio, caplog, request, page)[0]
    # The measure alone places nothing, so it adds what was asked without a wrong page.
    assert _gate(studio, caplog, request, {"measures": [yoy]})[0]


# --- H3: edits are not answered with unrelated additions ---------------------------------


@pytest.mark.parametrize(
    ("request_text", "answer_kind"),
    [
        ("Make the trend monthly", "trend_page"),
        ("Update the scorecard to show average discount instead of total quantity", "maximum"),
        ("Change the trend to a bar chart", "trend_page"),
        ("Replace total quantity with average discount on the scorecard", "maximum"),
        ("Switch the sales chart to profit", "trend_page"),
        # No page, measure, grouping or value to check, and nothing to add.
        ("Change the colors", "trend_page"),
    ],
)
def test_edit_requests_are_not_answered_with_additions(
    studio: _Studio, caplog: pytest.LogCaptureFixture, request_text: str, answer_kind: str
) -> None:
    answers = {
        "trend_page": _page(
            "Model Answer",
            _visual(
                "trend",
                "lineChart",
                "Sales over time",
                measure_id=studio.measure("Total Sales"),
                category_column_id="order_date",
            ),
        ),
        "maximum": {
            "measures": [
                {
                    "id": "discount_max",
                    "name": "Maximum Discount",
                    "operation": "maximum",
                    "table_id": studio.table("orders"),
                    "column_id": "discount",
                }
            ]
        },
    }

    _accepted, revised = _gate(studio, caplog, request_text, answers[answer_kind])

    # Refused before the model runs, or by the gate and then the fallback; nothing is saved.
    assert revised is None


@pytest.mark.parametrize(
    "request_text",
    [
        "Make the trend monthly",
        "Remove the detail page",
        "Rename the sales page to Overview",
        "Change the trend to a bar chart",
        "Please move the scorecard to the second page",
    ],
)
def test_pure_edits_are_refused_before_any_model_call(studio: _Studio, request_text: str) -> None:
    model = _Model()

    with pytest.raises(StudioPlanningError) as refused:
        studio.revise(model, request_text)

    assert str(refused.value) == planner._EDIT_REQUEST_MESSAGE
    assert model.calls == []


def test_a_request_for_something_new_is_left_to_the_gate(
    studio: _Studio, caplog: pytest.LogCaptureFixture
) -> None:
    # "instead of" makes it an edit for the gate, but the user asked to add a page.
    request = "Add a page showing profit by region instead of sales"
    answer = _page(
        "Model Answer",
        _visual(
            "category",
            "barChart",
            "Sales by Region",
            measure_id=studio.measure("Total Sales"),
            category_column_id="region",
        ),
    )

    accepted, _revised = _gate(studio, caplog, request, answer)

    assert not accepted  # the model ran and the gate judged its answer


@pytest.mark.parametrize(
    ("text", "placed"),
    [
        ("put the yoy sales growth below the trend", True),
        ("create a year over year matrix at the bottom", True),
        ("add a card next to the sales chart", True),
        ("add a page showing orders with discount above 20%", False),
        ("show products with sales under $10", False),
        ("show the top 10 products by sales", False),
    ],
)
def test_placement_needs_an_object_in_the_report(text: str, placed: bool) -> None:
    assert bool(planner._EXISTING_PAGE_PLACEMENT.search(text)) is placed


@pytest.mark.parametrize(
    ("request_text", "specs"),
    [
        (
            "Show profit by region as a bar chart",
            [_chart("Profit by Region", "Total Profit", "region", "clusteredBarChart")],
        ),
        (
            "Make a page for profit by region",
            [_chart("Profit by Region", "Total Profit", "region", "clusteredBarChart")],
        ),
        ("Include a card for total sales", [_card("Total Sales")]),
        # Eval R1 and R3, answered correctly.
        (
            "Add a page showing profit by sub-category",
            [
                _card("Total Profit"),
                _chart("Profit by Sub-Category", "Total Profit", "sub_category"),
            ],
        ),
        (
            R3,
            [
                _chart("Average Discount by Ship Mode", "Average Discount", "ship_mode"),
                _chart("Total Quantity by Ship Mode", "Total Quantity", "ship_mode", "columnChart"),
            ],
        ),
        # The grouping is not the metric; a donut shows shares; "Total Orders" counts.
        (
            "Add a page showing sales per segment",
            [_chart("Sales per Segment", "Total Sales", "segment")],
        ),
        (
            "Add a page showing sales by region",
            [_chart("Sales Share by Region", "Total Sales", "region", "donutChart")],
        ),
        (
            "Add a page showing the number of orders by ship mode",
            [_chart("Total Orders by Ship Mode", "Distinct Order ID", "ship_mode")],
        ),
        # "Update the report" is not an edit of one element, nor is a purpose clause.
        (
            "Update the report with a page showing profit by region",
            [_chart("Profit by Region", "Total Profit", "region")],
        ),
        (
            "Add a page showing profit by region to make the comparison easier",
            [_chart("Profit by Region", "Total Profit", "region")],
        ),
    ],
)
def test_legitimate_additions_still_pass_the_gate(
    studio: _Studio, caplog: pytest.LogCaptureFixture, request_text: str, specs: list[Spec]
) -> None:
    accepted, revised = _gate(studio, caplog, request_text, _answer(studio, *specs))

    assert accepted and revised is not None
    assert revised.pages[-1].title == "Model Answer"


# --- M1: every value the request names is what the answer shows --------------------------


@pytest.mark.parametrize(
    ("request_text", "specs"),
    [
        (
            "Add a page showing profit by sub-category",
            [_chart("Sales by Sub-Category", "Total Sales", "sub_category")],
        ),
        # R3 without Total Quantity.
        (R3, [_chart("Average Discount by Ship Mode", "Average Discount", "ship_mode")]),
        # Profit only as an overall card; the grouped chart plots sales.
        (
            "Add a page showing total profit by segment",
            [_card("Total Profit"), _chart("Total Sales by Segment", "Total Sales", "segment")],
        ),
        # The model bound SUM(Profit) itself under an "Average" title.
        (
            "Add a page showing average profit by region",
            [_chart("Average Profit by Region", "Total Profit", "region")],
        ),
        # A plain value is not answered by a derivation of it.
        (
            "Add a page showing sales by region",
            [_chart("Sales YoY % by Region", "Sales YoY %", "region")],
        ),
    ],
    ids=["wrong-metric", "dropped-metric", "metric-only-on-a-card", "average-over-sum", "derived"],
)
def test_answers_that_show_other_values_are_rejected(
    studio: _Studio, caplog: pytest.LogCaptureFixture, request_text: str, specs: list[Spec]
) -> None:
    yoy = GovernedMeasureSpec(
        id="sales_yoy_pct",
        name="Sales YoY %",
        operation="period_change",
        table_id=studio.table("orders"),
        base_measure_id=studio.measure("Total Sales"),
        date_column_id="order_date",
        period="year",
        format_string="0.0%",
    )
    draft = studio.with_measures(yoy)
    answer = _answer(studio, *specs, draft=draft)

    accepted, revised = _gate(studio, caplog, request_text, answer, draft)

    assert not accepted
    assert revised is None or revised.pages[-1].title != "Model Answer"


def test_a_measure_only_answer_must_compute_the_named_value(
    studio: _Studio, caplog: pytest.LogCaptureFixture
) -> None:
    def average(column: str) -> dict[str, Any]:
        return {
            "measures": [
                {
                    "id": f"average_{column}",
                    "name": f"Average {column.title()}",
                    "operation": "average",
                    "table_id": studio.table("orders"),
                    "column_id": column,
                }
            ]
        }

    assert _gate(studio, caplog, "Add a measure for the average sales", average("sales"))[0]
    assert not _gate(studio, caplog, "Add a measure for the average sales", average("profit"))[0]


# --- L1: duplicates are merged, not saved twice ------------------------------------------


def test_identical_visuals_on_a_model_page_are_saved_once(
    studio: _Studio, caplog: pytest.LogCaptureFixture
) -> None:
    chart = _visual(
        "category",
        "barChart",
        "Sales by Region",
        measure_id=studio.measure("Total Sales"),
        category_column_id="region",
    )
    answer = _page("Model Answer", chart, {**chart, "title": "Regional Sales"})

    accepted, revised = _gate(studio, caplog, "Add a page showing sales by region", answer)

    assert accepted and revised is not None
    (visual,) = revised.pages[-1].visuals
    assert (visual.title, visual.width) == ("Sales by Region", 12)


def test_a_restated_measure_is_bound_to_the_drafts_own(
    studio: _Studio, caplog: pytest.LogCaptureFixture
) -> None:
    answer = _page(
        "Model Answer",
        _visual(
            "category",
            "barChart",
            "Profit Total by Sub-Category",
            measure_id="profit_total",
            category_column_id="sub_category",
        ),
        measures=[
            {
                "id": "profit_total",
                "name": "Profit Total",
                "operation": "sum",
                "table_id": studio.table("orders"),
                "column_id": "profit",
            }
        ],
    )

    accepted, revised = _gate(
        studio, caplog, "Add a page showing total profit by sub-category", answer
    )

    assert accepted and revised is not None
    assert _added_measures(studio, revised) == []
    assert revised.pages[-1].visuals[0].measure_id == studio.measure("Total Profit")


# --- L2 / L3: category eligibility and listed groupings ----------------------------------


def test_a_key_column_is_a_category_only_when_the_user_names_it(
    studio: _Studio, caplog: pytest.LogCaptureFixture
) -> None:
    answer = _page(
        "Model Answer",
        _visual(
            "category",
            "barChart",
            "Sales by Order ID",
            measure_id=studio.measure("Total Sales"),
            category_column_id="order_id",
        ),
    )

    assert not _gate(studio, caplog, "Add a page showing sales by order", answer)[0]
    assert _gate(studio, caplog, "Add a page showing sales by order id", answer)[0]


def test_a_listing_table_that_includes_the_grouping_answers_it(
    studio: _Studio, caplog: pytest.LogCaptureFixture
) -> None:
    answer = _page(
        "Model Answer",
        _visual(
            "detail",
            "tableEx",
            "Orders",
            table_id=studio.table("orders"),
            detail_column_ids=["order_id", "region", "sales"],
        ),
    )

    accepted, revised = _gate(studio, caplog, "Add a page listing orders by region", answer)

    assert accepted and revised is not None
    assert revised.pages[-1].title == "Model Answer"


def test_an_unrequested_relationship_id_is_dropped_not_fatal(
    studio: _Studio, caplog: pytest.LogCaptureFixture
) -> None:
    # Qwen 4B answered eval R1 with the right page but put the Orders table ID in the
    # relationship list, which no join was offered for; that rejected the whole page.
    answer = _answer(studio, _chart("Profit by Region", "Total Profit", "region"))
    answer["recommended_relationship_ids"] = [studio.table("orders")]

    accepted, revised = _gate(studio, caplog, "Add a page showing profit by region", answer)

    assert accepted and revised is not None
    assert revised.pages[-1].title == "Model Answer"
