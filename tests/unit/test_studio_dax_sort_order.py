"""Grouped DAX Lab answers follow the order the request states.

Measured (D13, 2026-09-24): for "Total sales by region, highest first" Qwen 4B and 2B
planned sort_by Region DESC and Granite Region ASC then value, so the rows came back by
name. The app now sets the plan's sort from the request before the evaluator checks it.
"""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from ai_powerbi_analyst.studio.contracts import (
    CreateWorkspaceRequest,
    DaxAssistRequest,
    DaxEvaluateRequest,
    IngestRequest,
)
from ai_powerbi_analyst.studio.dax_assistant import _in_requested_order, dax_assistant_events
from ai_powerbi_analyst.studio.dax_plan import GroupedDaxPlan
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH

# Region totals West 400, East 300, Central 200, South 100. Sorted by name they come
# back West, South, East, Central (DESC) or Central, East, South, West (ASC).
ORDERS = (
    "Order ID,Order Date,Postal Code,Segment,Category,Sub-Category,Region,Sales\n"
    "A-1,2024-01-05,10001,Consumer,Technology,Phones,West,300\n"
    "A-1,2024-01-05,10001,Consumer,Furniture,Chairs,West,100\n"
    "A-2,2024-02-11,20002,Corporate,Furniture,Tables,East,300\n"
    "A-3,2024-03-17,30003,Consumer,Technology,Phones,Central,200\n"
    "A-4,2024-04-23,40004,Home Office,Furniture,Chairs,South,100\n"
)
GOAL = "Total sales by region, highest first"
BASE = {"table": "Orders", "group_by": ["Region"], "operation": "sum", "value_column": "Sales"}


def _workspace(root: Path) -> tuple[StudioService, str]:
    root.mkdir(parents=True, exist_ok=True)
    (root / "Orders.csv").write_text(ORDERS, encoding="utf-8")
    service = StudioService(Path(__file__).resolve().parents[2], root / "state")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Sort order",
            selected_folder=str(root),
            idempotency_key="sort-order-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(root / "Orders.csv")],
            idempotency_key="sort-order-ingest",
            expected_base_hash=workspace.base_hash,
        )
    )
    return service, workspace.id


@pytest.fixture
def orders(tmp_path: Path) -> tuple[StudioService, str]:
    return _workspace(tmp_path)


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory: pytest.TempPathFactory):
    service, workspace_id = _workspace(tmp_path_factory.mktemp("sort-order"))
    return service.snapshot(service.workspace(workspace_id).active_snapshot_id)


def _sorts(*keys: str) -> list[dict[str, str]]:
    """sort_by entries from "Region desc", "value asc", ..."""
    return [dict(zip(("column", "direction"), key.rsplit(" ", 1), strict=True)) for key in keys]


def _plan(**changes: object) -> GroupedDaxPlan:
    return GroupedDaxPlan(**{**BASE, **changes})


def _keys(plan: GroupedDaxPlan) -> list[str]:
    return [f"{sort.column} {sort.direction}" for sort in plan.sort_by]


REGION_DESC = _sorts("Region desc")  # Qwen 4B and 2B
REGION_THEN_VALUE = _sorts("Region asc", "value desc")  # Granite
ORDER_IDS = {"operation": "distinctcount", "value_column": "Order ID"}
MULTI = {"group_by": ["Category", "Region"]}


@pytest.mark.parametrize(
    ("goal", "changes", "expected"),
    [
        (GOAL, {"sort_by": REGION_DESC, "grand_total": "last"}, ["value desc"]),
        (GOAL, {"sort_by": REGION_THEN_VALUE, "grand_total": "last"}, ["value desc"]),
        (GOAL, {}, ["value desc"]),
        # "Sort ... by region" names the grouping, and "Order" is a noun: no Region key.
        ("Sort total sales by region, highest first", {"sort_by": REGION_DESC}, ["value desc"]),
        (
            "Order count by region, highest first",
            {**ORDER_IDS, "sort_by": REGION_DESC},
            ["value desc"],
        ),
        (
            "Distinct count of Order ID by Region, highest first",
            {**ORDER_IDS, "sort_by": REGION_DESC},
            ["value desc"],
        ),
        ("Total sales by region, highest at the top", {"sort_by": REGION_DESC}, ["value desc"]),
        ("Total sales by region with most sales first", {"sort_by": REGION_DESC}, ["value desc"]),
        ("Rank total sales by region", {}, ["value desc"]),
        (
            "Total sales by region, sorted by sales descending",
            {"sort_by": REGION_DESC},
            ["value desc"],
        ),
        ("Total sales by region, top 3", {"sort_by": _sorts("Region asc")}, ["value desc"]),
        ("Total sales by region, lowest first", {"sort_by": _sorts("Region asc")}, ["value asc"]),
        ("Total sales by region, low to high", {"sort_by": REGION_DESC}, ["value asc"]),
        ("Total sales by region ranked ascending", {"sort_by": REGION_DESC}, ["value asc"]),
        # Several groups: those the request names as sort keys keep their place and the
        # model's direction; unrequested ones ahead of the value are dropped.
        (
            "Total sales by Category and Region, sorted by Category, highest first",
            {**MULTI, "sort_by": REGION_DESC},
            ["Category asc", "value desc"],
        ),
        (
            "Total sales by Category and Region, sorted by Category, highest first",
            {**MULTI, "sort_by": _sorts("Category desc", "Region asc")},
            ["Category desc", "value desc"],
        ),
        (
            "Total sales by Category and Region, highest first within each Category",
            {**MULTI, "sort_by": REGION_DESC},
            ["Category asc", "value desc"],
        ),
        (
            "Total sales by Category and Region, highest first",
            {**MULTI, "sort_by": _sorts("Category asc", "value desc")},
            ["value desc"],
        ),
    ],
)
def test_ranking_words_make_the_value_the_sort_key(snapshot, goal, changes, expected) -> None:
    plan = _plan(**changes)
    ordered = _in_requested_order(goal, plan, snapshot)
    assert _keys(ordered) == expected
    assert ordered.grand_total == plan.grand_total


TOP_TIER = {"column": "Segment", "operator": "=", "value": "Top Tier"}
TOP_FIVE = {"column": "Segment", "operator": "=", "value": "Top 5"}


@pytest.mark.parametrize(
    ("goal", "changes"),
    [
        # Already in the requested order: nothing is inserted and tie-breakers stay.
        ("Sort total sales by region, highest first", {"sort_by": _sorts("value desc")}),
        (GOAL, {"sort_by": _sorts("value desc", "Region asc")}),
        # The grouping field itself is the sort key.
        ("Total sales by region sorted by region descending", {"sort_by": REGION_DESC}),
        ("Total sales by region descending", {"sort_by": REGION_DESC}),
        ("Total sales by region, alphabetical", {"sort_by": _sorts("Region asc")}),
        # Grand-total placement orders no rows.
        (
            "Total sales by region with the grand total at the top",
            {"sort_by": _sorts("Region asc"), "grand_total": "first"},
        ),
        (
            "Total sales by region, grand total at the very bottom",
            {"sort_by": _sorts("Region asc"), "grand_total": "last"},
        ),
        (
            "Total sales by region, grand total up top",
            {"sort_by": _sorts("Region asc"), "grand_total": "first"},
        ),
        (
            "Total sales by region, grand total bottom",
            {"sort_by": _sorts("Region asc"), "grand_total": "last"},
        ),
        (
            "Total sales by Region sorted by sales, grand total last",
            {"sort_by": _sorts("value desc"), "grand_total": "last"},
        ),
        ("Total sales by region for top-level categories", {"sort_by": _sorts("Region asc")}),
        # Negated or contradictory wording stays with the model.
        (
            "Instead of highest first, sort alphabetically by region",
            {"sort_by": _sorts("Region asc")},
        ),
        ("Don't sort highest first, sort by region", {"sort_by": _sorts("Region asc")}),
        (
            "Don\N{RIGHT SINGLE QUOTATION MARK}t put the highest first, sort by region",
            {"sort_by": _sorts("Region asc")},
        ),
        (
            "Total sales by region, highest first then lowest first",
            {"sort_by": _sorts("Region asc")},
        ),
        # A bare direction on dates or numbers orders the keys: newest or largest first.
        (
            "Total sales by Order Date sorted descending",
            {"group_by": ["Order Date"], "sort_by": _sorts("Order Date desc")},
        ),
        (
            "Total sales by Postal Code sorted descending",
            {"group_by": ["Postal Code"], "sort_by": _sorts("Postal Code desc")},
        ),
        (
            "Total sales by Order Date, most recent first",
            {"group_by": ["Order Date"], "sort_by": _sorts("Order Date desc")},
        ),
        # The plan's own names and values are not wording, and neither is pasted DAX.
        ("Total sales by region for the Top Tier segment", {"filters": [TOP_TIER]}),
        ("Total sales by region for the Top 5 segment", {"filters": [TOP_FIVE]}),
        ("Average Rank by region", {"operation": "average", "value_column": "Rank"}),
        ("Fix ORDER BY 'Orders'[Region] DESC", {"sort_by": REGION_DESC}),
    ],
)
def test_other_wording_keeps_the_models_order(snapshot, goal, changes) -> None:
    plan = _plan(**changes)
    assert _in_requested_order(goal, plan, snapshot) is plan


def test_a_bare_direction_needs_a_text_group(snapshot) -> None:
    plan = _plan(sort_by=REGION_DESC)
    goal = "Total sales by region sorted descending"
    assert _keys(_in_requested_order(goal, plan, snapshot)) == ["value desc"]
    # Without the snapshot the group's type is unknown: the model's order stays.
    assert _in_requested_order(goal, plan, None) is plan


def _ask(service: StudioService, workspace_id: str, goal: str, plan: dict) -> tuple[str, list]:
    """The formula shipped for ``goal`` when the model returns ``plan``, and the calls."""
    calls: list[str] = []

    def generate(system, message, response_model, **kwargs):
        # Recorded grouped plans cover their own fallback contract, not pattern slots.
        if "pattern" in response_model.model_fields:
            return response_model(pattern=None)
        calls.append(response_model.__name__)
        return response_model.model_validate_json(json.dumps(plan))

    service.model = SimpleNamespace(generate=generate, unload=lambda: None)
    events = dax_assistant_events(
        service,
        DaxAssistRequest(
            workspace_id=workspace_id,
            snapshot_id=service.workspace(workspace_id).active_snapshot_id,
            request_id="sort-" + str(abs(hash((goal, json.dumps(plan))))),
            goal=goal,
        ),
        Event(),
    )
    text = "".join(e["data"].get("text", "") for e in events if e["event"] == "token")
    return text.split("```dax\n")[1].split("\n```")[0], calls


def _rows(service: StudioService, workspace_id: str, formula: str) -> list[tuple]:
    result = service.evaluate_dax(DaxEvaluateRequest(workspace_id=workspace_id, expression=formula))
    return [tuple(row.values())[:2] for row in result.rows]


@pytest.mark.parametrize(
    "recorded",
    [
        {"sort_by": REGION_DESC, "grand_total": "last", "filters": []},
        {"sort_by": REGION_THEN_VALUE, "grand_total": "last"},
        {"sort_by": _sorts("value desc")},
        {"sort_by": _sorts("value desc"), "grand_total": "last", "filters": []},
    ],
    ids=["qwen", "granite", "gemma", "coder"],
)
def test_recorded_d13_plans_list_the_highest_region_first(orders, recorded) -> None:
    service, workspace_id = orders
    formula, calls = _ask(service, workspace_id, GOAL, {**BASE, **recorded})
    assert calls == ["ValidatedGroupedDaxPlan"]  # no repair turn
    total = recorded.get("grand_total") == "last"
    assert formula.endswith(
        " ORDER BY [IsGrandTotal] ASC, [Total Sales] DESC"
        if total
        else " ORDER BY [Total Sales] DESC"
    )
    by_value = [("West", 400), ("East", 300), ("Central", 200), ("South", 100)]
    assert _rows(service, workspace_id, formula) == by_value + ([(None, 1000)] if total else [])


def test_the_grand_total_keeps_its_place_ahead_of_the_value_order(orders) -> None:
    service, workspace_id = orders
    goal = "Total sales by region, highest first, grand total at the top"
    plan = {**BASE, "sort_by": REGION_DESC, "grand_total": "first"}
    formula, calls = _ask(service, workspace_id, goal, plan)
    assert len(calls) == 1
    assert formula.endswith(" ORDER BY [IsGrandTotal] DESC, [Total Sales] DESC")
    assert [region for region, _ in _rows(service, workspace_id, formula)] == [
        None,
        "West",
        "East",
        "Central",
        "South",
    ]


def test_the_shipped_formula_is_the_text_that_passed_validation(orders, monkeypatch) -> None:
    service, workspace_id = orders
    evaluated: list[str] = []
    evaluate = service.evaluate_dax

    def spy(request: DaxEvaluateRequest):
        evaluated.append(request.expression)
        return evaluate(request)

    monkeypatch.setattr(service, "evaluate_dax", spy)
    # The plan drifts from 'Sub-Category': only the resolved name evaluates. Before, the
    # resolved text was validated but the plan's own rendering was shipped.
    plan = {**BASE, "group_by": ["Sub Category"], "sort_by": _sorts("Sub Category desc")}
    formula, calls = _ask(service, workspace_id, "Total sales by Sub-Category, highest first", plan)
    assert len(calls) == 1
    assert "'Orders'[Sub Category]" in evaluated[0]
    assert formula == evaluated[-1]
    assert formula == (
        "EVALUATE SUMMARIZECOLUMNS('Orders'[Sub-Category], \"Total Sales\", "
        "SUM('Orders'[Sales])) ORDER BY [Total Sales] DESC"
    )
    assert _rows(service, workspace_id, formula) == [
        ("Phones", 500),
        ("Tables", 300),
        ("Chairs", 200),
    ]
