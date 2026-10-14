"""DAX lab formulas that filter or group through approved relationships.

Superstore-shaped: Returns lists some orders, People maps each Region to a manager.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_powerbi_analyst.studio.contracts import (
    AddDaxMeasureRequest,
    ApproveAdvancedDaxRequest,
    ApproveDraftRequest,
    CreateWorkspaceRequest,
    DaxEvaluateRequest,
    DraftRequest,
    IngestRequest,
)
from ai_powerbi_analyst.studio.planner import StudioPlanningError, StudioPlanningService
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


@pytest.fixture
def superstore(tmp_path: Path) -> tuple[StudioService, str]:
    root = tmp_path / "superstore"
    root.mkdir()
    (root / "Orders.csv").write_text(
        "Order ID,Region,Sales\n"
        "A-1,East,100\nA-1,East,20\nA-2,West,250\nA-3,West,150\nA-4,Central,80\n",
        encoding="utf-8",
    )
    (root / "Returns.csv").write_text("Returned,Order ID\nYes,A-2\nYes,A-4\n", encoding="utf-8")
    (root / "People.csv").write_text(
        "Person,Region\nAnn,East\nBob,West\nCy,Central\n", encoding="utf-8"
    )
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Superstore",
            selected_folder=str(root),
            idempotency_key="related-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(root / name) for name in ("Orders.csv", "Returns.csv", "People.csv")],
            idempotency_key="related-ingest",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Build a sales dashboard",
            idempotency_key="related-draft",
            expected_base_hash=workspace.base_hash,
        )
    )
    return service, workspace.id


def _approve(service: StudioService, workspace_id: str, *, include: set[str], key: str) -> None:
    draft = service.draft(service.workspace(workspace_id).active_draft_id)
    service.approve_draft(
        draft.id,
        ApproveDraftRequest(
            workspace_id=workspace_id,
            approved_relationship_ids=[
                item.id
                for item in draft.semantic_model.relationships
                if item.to_table_id.startswith(tuple(include))
            ],
            idempotency_key=key,
            expected_base_hash=draft.base_hash,
        ),
    )


def _value(service: StudioService, workspace_id: str, expression: str) -> object:
    result = service.evaluate_dax(
        DaxEvaluateRequest(workspace_id=workspace_id, expression=expression)
    )
    return result.rows if len(result.rows) != 1 else next(iter(result.rows[0].values()))


def _ask(
    service: StudioService,
    workspace_id: str,
    goal: str,
    answers: list[str],
    seen: list[str],
    *,
    expression: str = "",
    discussion: str = "",
    all_calls: list[str] | None = None,
) -> str:
    """Generate with a guarded-model stand-in: one correction after a validation error.

    Answers starting with "{" are grouped plans, the others formula expressions.
    """
    from threading import Event
    from types import SimpleNamespace

    from ai_powerbi_analyst.llm.runtime import LocalModelError
    from ai_powerbi_analyst.studio.contracts import DaxAssistRequest
    from ai_powerbi_analyst.studio.dax_assistant import dax_assistant_events

    def generate(system, message, response_model, **kwargs):
        if all_calls is not None:
            all_calls.append(response_model.__name__)
        # Scripted expressions test legacy repair/filter handling after the
        # closed selector explicitly declines; real pattern flows have their own suite.
        if "pattern" in response_model.model_fields:
            return response_model(pattern=None)

        def parse(answer: str):
            if answer.startswith("{"):
                return response_model.model_validate_json(answer)
            return response_model(expression=answer)

        seen.append(system)
        try:
            return parse(answers[0])
        except ValueError as error:
            seen.append(str(error))
        try:
            return parse(answers[1])
        except ValueError as error:
            raise LocalModelError(
                "Local model failed strict schema/artifact validation: " + str(error)
            ) from error

    service.model = SimpleNamespace(generate=generate, unload=lambda: None)
    events = dax_assistant_events(
        service,
        DaxAssistRequest(
            workspace_id=workspace_id,
            snapshot_id=service.workspace(workspace_id).active_snapshot_id,
            request_id="flag-" + str(abs(hash((goal, *answers)))),
            goal=goal,
            expression=expression,
            discussion=discussion,
        ),
        Event(),
    )
    return "".join(e["data"].get("text", "") for e in events)


def test_related_filters_need_an_approved_relationship(superstore) -> None:
    service, workspace_id = superstore
    with pytest.raises(ValueError, match="Include the Orders → Returns relationship"):
        _value(service, workspace_id, "CALCULATE(SUM(Orders[Sales]), Returns[Returned] = TRUE())")


def test_filters_and_groups_follow_power_bi_blank_semantics(superstore) -> None:
    service, workspace_id = superstore
    _approve(service, workspace_id, include={"returns", "people"}, key="related-approve")

    def value(expression: str) -> object:
        return _value(service, workspace_id, expression)

    assert value("CALCULATE(SUM(Orders[Sales]), Returns[Returned] = TRUE())") == 330
    # Orders without a Returns row sit on the (Blank) member: BLANK <> TRUE and BLANK = FALSE.
    assert value("CALCULATE(SUM('Orders'[Sales]), 'Returns'[Returned] <> TRUE())") == 270
    assert value("CALCULATE(SUM('Orders'[Sales]), 'Returns'[Returned] = FALSE())") == 270
    assert value("CALCULATE(DISTINCTCOUNT(Orders[Order ID]), Returns[Returned] = TRUE)") == 2
    assert value('CALCULATE(SUM(Orders[Sales]), People[Person] = "Bob")') == 400
    assert (
        value('CALCULATE(SUM(Orders[Sales]), People[Person] = "Bob", Returns[Returned] = TRUE())')
        == 250
    )
    by_person = value('EVALUATE SUMMARIZECOLUMNS(People[Person], "Sales", SUM(Orders[Sales]))')
    assert by_person == [
        {"Person": "Bob", "Sales": 400},
        {"Person": "Ann", "Sales": 120},
        {"Person": "Cy", "Sales": 80},
    ]
    by_returned = value(
        'EVALUATE SUMMARIZECOLUMNS(Returns[Returned], "Sales", SUM(Orders[Sales])) '
        "ORDER BY Returns[Returned] ASC"
    )
    assert by_returned == [{"Returned": None, "Sales": 270}, {"Returned": True, "Sales": 330}]
    with pytest.raises(ValueError, match="support = and <>"):
        value('CALCULATE(SUM(Orders[Sales]), People[Person] > "A")')
    # Shares: the numerator narrows the denominator, on the same or a related table.
    assert (
        value(
            "DIVIDE(CALCULATE(DISTINCTCOUNT(Orders[Order ID]), Returns[Returned] = TRUE()), "
            "DISTINCTCOUNT(Orders[Order ID]))"
        )
        == 0.5
    )
    assert value(
        'DIVIDE(CALCULATE(SUM(Orders[Sales]), Orders[Region] = "West"), SUM(Orders[Sales]))'
    ) == pytest.approx(400 / 600)
    assert value(
        "CALCULATE(DIVIDE(CALCULATE(SUM(Orders[Sales]), Returns[Returned] = TRUE()), "
        'SUM(Orders[Sales])), People[Person] = "Bob")'
    ) == pytest.approx(250 / 400)
    return_rate_by_person = value(
        'EVALUATE SUMMARIZECOLUMNS(People[Person], "Return rate", '
        "DIVIDE(CALCULATE(DISTINCTCOUNT(Orders[Order ID]), Returns[Returned] = TRUE()), "
        "DISTINCTCOUNT(Orders[Order ID]))) ORDER BY People[Person] ASC"
    )
    # Ann has no returns: her rate is BLANK and SUMMARIZECOLUMNS omits the row.
    assert return_rate_by_person == [
        {"Person": "Bob", "Return rate": 0.5},
        {"Person": "Cy", "Return rate": 1.0},
    ]
    # FILTER over a table with simple predicates is the same set of rows.
    assert value("COUNTROWS(FILTER('Returns', 'Returns'[Returned] = TRUE))") == 2
    assert (
        value(
            "DIVIDE(CALCULATE(DISTINCTCOUNT('Orders'[Order ID]), "
            "FILTER('Returns', 'Returns'[Returned] = TRUE())), DISTINCTCOUNT('Orders'[Order ID]))"
        )
        == 0.5
    )
    assert value('COUNTROWS(FILTER(Orders, Orders[Region] = "West" && Orders[Sales] > 200))') == 1
    with pytest.raises(ValueError, match="filtered table's own columns"):
        value("COUNTROWS(FILTER('Orders', 'Returns'[Returned] = TRUE()))")
    with pytest.raises(ValueError, match="Aggregate Orders on both sides"):
        value("DIVIDE(COUNTROWS('Returns'), COUNTROWS('Orders'))")
    # DAX would show West's sales on every Region row; the lab refuses rather than differ.
    with pytest.raises(ValueError, match="replaces the grouping"):
        value(
            'EVALUATE SUMMARIZECOLUMNS(Orders[Region], "Share", DIVIDE(CALCULATE('
            'SUM(Orders[Sales]), Orders[Region] = "West"), SUM(Orders[Sales])))'
        )
    with pytest.raises(ValueError, match="replaces the grouping"):
        value(
            'EVALUATE SUMMARIZECOLUMNS(People[Person], "S", '
            'CALCULATE(SUM(Orders[Sales]), People[Person] = "Bob"))'
        )
    with pytest.raises(ValueError, match="may add CALCULATE filters"):
        value(
            'DIVIDE(CALCULATE(SUM(Orders[Sales]), Orders[Region] = "West"), '
            'CALCULATE(SUM(Orders[Sales]), Orders[Region] = "East"))'
        )
    # Breakdowns read Orders alone and would drop the Returns filter.
    result = service.evaluate_dax(
        DaxEvaluateRequest(
            workspace_id=workspace_id,
            expression="CALCULATE(SUM(Orders[Sales]), Returns[Returned] = TRUE())",
        )
    )
    assert result.preview_fields == []


def test_counting_orders_uses_distinct_order_ids_not_line_rows(superstore) -> None:
    from ai_powerbi_analyst.studio.dax_assistant import _entity_count_formula, _request_gap

    service, workspace_id = superstore
    snapshot = service.snapshot(service.workspace(workspace_id).active_snapshot_id)
    goal = "Calculate the return rate as returned orders divided by all orders."
    model_wrote = (
        "DIVIDE(CALCULATE(COUNTROWS('Orders'), 'Returns'[Returned] = TRUE), COUNTROWS('Orders'))"
    )
    assert "DISTINCTCOUNT('Orders'[Order ID])" in _request_gap(
        goal, model_wrote, snapshot, "", refinement=False
    )
    assert _entity_count_formula(goal, model_wrote, snapshot) == (
        "DIVIDE(CALCULATE(DISTINCTCOUNT('Orders'[Order ID]), 'Returns'[Returned] = TRUE), "
        "DISTINCTCOUNT('Orders'[Order ID]))"
    )
    # Asking for rows keeps the row count.
    assert _entity_count_formula("Count order rows", "COUNTROWS(Orders)", snapshot) is None


def test_a_share_divided_by_itself_gets_the_named_flag(superstore) -> None:
    from ai_powerbi_analyst.studio.dax_assistant import (
        _entity_count_formula,
        _flag_share_formula,
        _request_gap,
    )

    service, workspace_id = superstore
    snapshot = service.snapshot(service.workspace(workspace_id).active_snapshot_id)
    goal = "Calculate the return rate: returned orders divided by all orders"
    repaired = "DIVIDE(COUNTROWS('Orders'), COUNTROWS('Orders'))"
    assert "always 1" in _request_gap(goal, repaired, snapshot, "", refinement=False)
    counted = _entity_count_formula(goal, repaired, snapshot)
    assert _flag_share_formula(goal, counted, snapshot) == (
        "DIVIDE(CALCULATE(DISTINCTCOUNT('Orders'[Order ID]), 'Returns'[Returned] = TRUE()), "
        "DISTINCTCOUNT('Orders'[Order ID]))"
    )
    assert _flag_share_formula("Share of orders not returned", counted, snapshot) is None
    from ai_powerbi_analyst.studio.dax_assistant import _flag_rate_formula

    assert _flag_rate_formula(goal, snapshot) == _flag_share_formula(goal, counted, snapshot)
    assert _flag_rate_formula("Calculate returned orders", snapshot) is None  # not a rate
    assert _flag_rate_formula("Return rate of order rows", snapshot) is None  # rows asked
    _approve(service, workspace_id, include={"returns", "people"}, key="related-approve")
    assert _value(service, workspace_id, _flag_share_formula(goal, counted, snapshot)) == 0.5


def test_a_named_flag_the_formula_ignores_is_a_gap_and_is_completed(superstore) -> None:
    from ai_powerbi_analyst.studio.dax_assistant import _request_gap, _with_named_flag

    service, workspace_id = superstore
    snapshot = service.snapshot(service.workspace(workspace_id).active_snapshot_id)
    # Measured on Superstore: the 4B model's repair SUM('Orders'[Profit]) ran and
    # was accepted, the profit of all orders instead of the returned ones.
    goal = "Total sales on orders that were returned"
    total = "SUM('Orders'[Sales])"
    assert "'Returns'[Returned] = TRUE()" in _request_gap(goal, total, snapshot, "", False)
    completed = _with_named_flag(goal, total, snapshot)
    assert completed == "CALCULATE(SUM('Orders'[Sales]), 'Returns'[Returned] = TRUE())"
    # "No other filters" keeps this one.
    only = "Total sales on returned orders, no other filters"
    assert _with_named_flag(only, total, snapshot) == completed
    # The flag joins the filters already there, in one CALCULATE.
    bob = 'CALCULATE(SUM(Orders[Sales]), People[Person] = "Bob")'
    assert _with_named_flag("Sales on returned orders managed by Bob", bob, snapshot) == (
        bob[:-1] + ", 'Returns'[Returned] = TRUE())"
    )
    # A leading CALCULATE that is only the first term: the flag filters the whole formula.
    difference = 'CALCULATE(SUM(Orders[Sales]), Orders[Region] = "West") - SUM(Orders[Sales])'
    whole = _with_named_flag("West sales minus all sales of returned orders", difference, snapshot)
    assert whole == f"CALCULATE({difference}, 'Returns'[Returned] = TRUE())"
    # A quote inside a column name is part of the name, not a string.
    quoted = 'CALCULATE(SUM(Orders[Sales]), Orders[Buyer\'s Region] = "West")'
    assert _with_named_flag(goal, quoted, snapshot) == (
        quoted[:-1] + ", 'Returns'[Returned] = TRUE())"
    )
    for applied in (
        completed,
        "CALCULATE(SUM(Orders[Sales]), Returns[Returned] = TRUE)",
        "CALCULATE(SUM(Orders[Sales]), FILTER('Returns', 'Returns'[Returned] = TRUE()))",
        "[Returned Sales]",
        "VAR base = 0 RETURN [Returned Sales] + base",
        'EVALUATE SUMMARIZECOLUMNS(Returns[Returned], "Sales", SUM(Orders[Sales]))',
    ):
        assert _request_gap(goal, applied, snapshot, "", False) is None, applied
        assert _with_named_flag(goal, applied, snapshot) is None, applied
    # The flag on the wrong table, or a query column merely named after it, is not applied.
    for ignored in (
        "CALCULATE(SUM('Orders'[Sales]), 'Orders'[Returned] = TRUE())",
        "CALCULATE(SUM('Orders'[Sales]), 'Orders' [Returned] = TRUE())",
        "CALCULATE(SUM(Orders[Sales]), Orders [Returned] = TRUE())",
        'EVALUATE SUMMARIZECOLUMNS(People[Person], "Returned Sales", SUM(Orders[Sales])) '
        "ORDER BY [Returned Sales] DESC",
    ):
        assert "ignores it" in _request_gap(goal, ignored, snapshot, "", False), ignored
    # Another query form is left to the model: CALCULATE cannot wrap it.
    defined = 'DEFINE MEASURE Orders[Total] = SUM(Orders[Sales]) EVALUATE ROW("Total", [Total])'
    assert _with_named_flag(goal, defined, snapshot) is None
    # A grouped query gets the condition on each value and keeps its sort.
    by_person = _with_named_flag(
        "Sales of returned orders by person",
        'EVALUATE SUMMARIZECOLUMNS(People[Person], "Sales", SUM(Orders[Sales])) '
        "ORDER BY People[Person] ASC",
        snapshot,
    )
    assert by_person == (
        'EVALUATE SUMMARIZECOLUMNS(People[Person], "Sales", '
        "CALCULATE(SUM(Orders[Sales]), 'Returns'[Returned] = TRUE())) ORDER BY People[Person] ASC"
    )
    # The evaluator drops the grand-total row under a related filter: left to the model.
    rollup = (
        "EVALUATE SUMMARIZECOLUMNS(ROLLUPADDISSUBTOTAL(ROLLUPGROUP(People[Person]), "
        '"IsGrandTotal"), "Sales", SUM(Orders[Sales]))'
    )
    assert _with_named_flag("Sales of returned orders by person", rollup, snapshot) is None
    _approve(service, workspace_id, include={"returns", "people"}, key="flag-approve")
    assert _value(service, workspace_id, completed) == 330
    assert _value(service, workspace_id, whole) == 250 - 330
    assert _value(service, workspace_id, by_person) == [
        {"Person": "Bob", "Sales": 250},
        {"Person": "Cy", "Sales": 80},
    ]


def test_a_flag_the_request_negates_broadens_or_drops_is_not_added(superstore) -> None:
    from ai_powerbi_analyst.studio.dax_assistant import _request_gap, _with_named_flag

    service, workspace_id = superstore
    snapshot = service.snapshot(service.workspace(workspace_id).active_snapshot_id)
    total = "SUM('Orders'[Sales])"
    flagged = "CALCULATE(SUM('Orders'[Sales]), 'Returns'[Returned] = TRUE())"
    for goal in (
        "Sales on orders not returned",
        "Sales of non-returned orders",
        "Sales on orders with no returned items",
        "Returned vs unreturned sales",
        "Total sales on all orders, including returned ones",
        "Sales for every order, returned or kept",
        "Sales split between returned and kept orders",
        "Use all orders instead of only returned ones",
        "Change it to use all orders rather than just returned ones",
        "Remove the returned condition",
        "Undo the returned filter and show total sales",
        "Clear the returned filter",
        "Forget the returned condition and show all sales",
        "Show total sales with no returned filter",
        "Show sales with no filter on returned orders",
        "Take the returned filter off",
        "Stop filtering on returned orders",
        # Only the full flag name filters: the stem also names other things.
        "Calculate the return on sales",
    ):
        assert _request_gap(goal, total, snapshot, "", False) is None, goal
        assert _with_named_flag(goal, total, snapshot) is None, goal
    # Dropping the flag from the current formula is not undone.
    dropped = "Use all orders instead of only returned ones"
    assert _request_gap(dropped, total, snapshot, "", True) is None
    assert _with_named_flag("Show sales for returned orders", flagged, snapshot) is None


def test_rate_requests_keep_the_flag_on_the_numerator(superstore) -> None:
    from ai_powerbi_analyst.studio.dax_assistant import (
        _flag_rate_formula,
        _request_gap,
        _with_named_flag,
    )

    service, workspace_id = superstore
    snapshot = service.snapshot(service.workspace(workspace_id).active_snapshot_id)
    total = "SUM('Orders'[Sales])"
    share = "Share of sales from returned orders"
    assert "DIVIDE(CALCULATE(x, 'Returns'[Returned] = TRUE()), x)" in _request_gap(
        share, total, snapshot, "", False
    )
    # Filtering the whole formula would make the share 1: the rate paths write it.
    assert _with_named_flag(share, total, snapshot) is None
    assert _flag_rate_formula(share, snapshot) == (
        "DIVIDE(CALCULATE(SUM('Orders'[Sales]), 'Returns'[Returned] = TRUE()), "
        "SUM('Orders'[Sales]))"
    )
    # A named value or another ratio is not the order-count return rate, even when
    # orders are named.
    for goal in (
        "Returned sales rate",
        "Sales margin percentage on returned orders",
        "Margin percentage on returned orders",
    ):
        assert _flag_rate_formula(goal, snapshot) is None, goal
    # A ratio of two different values is filtered as a whole, however it divides.
    rate = "Sales per order rate for returned orders"
    divided = "SUM('Orders'[Sales]) / DISTINCTCOUNT('Orders'[Order ID])"
    for per_order in (divided, "DIVIDE(SUM('Orders'[Sales]), DISTINCTCOUNT('Orders'[Order ID]))"):
        assert _with_named_flag(rate, per_order, snapshot) == (
            f"CALCULATE({per_order}, 'Returns'[Returned] = TRUE())"
        )
    west = (
        "CALCULATE(DIVIDE(SUM('Orders'[Sales]), DISTINCTCOUNT('Orders'[Order ID])), "
        "'Orders'[Region] = \"West\")"
    )
    returned_west = _with_named_flag("Sales per order rate of returned West orders", west, snapshot)
    assert returned_west == west[:-1] + ", 'Returns'[Returned] = TRUE())"
    # So is each value of a grouped query; a share-shaped value leaves the query alone.
    by_region = _with_named_flag(
        "Sales per order rate of returned orders by region",
        "EVALUATE SUMMARIZECOLUMNS('Orders'[Region], \"Per order\", "
        "DIVIDE(SUM('Orders'[Sales]), DISTINCTCOUNT('Orders'[Order ID])))",
        snapshot,
    )
    assert by_region == (
        "EVALUATE SUMMARIZECOLUMNS('Orders'[Region], \"Per order\", CALCULATE(DIVIDE("
        "SUM('Orders'[Sales]), DISTINCTCOUNT('Orders'[Order ID])), 'Returns'[Returned] = TRUE()))"
    )
    grouped = "EVALUATE SUMMARIZECOLUMNS('Orders'[Region], \"Sales\", SUM('Orders'[Sales]))"
    assert _with_named_flag(share + " by region", grouped, snapshot) is None
    _approve(service, workspace_id, include={"returns", "people"}, key="flag-rate-approve")
    assert _value(service, workspace_id, _with_named_flag(rate, divided, snapshot)) == 330 / 2
    assert _value(service, workspace_id, returned_west) == 250
    assert _value(service, workspace_id, by_region) == [
        {"Region": "West", "Per order": 250},
        {"Region": "Central", "Per order": 80},
    ]


def test_flag_filter_rescue_answers_only_what_it_accounts_for(superstore) -> None:
    from ai_powerbi_analyst.studio.dax_assistant import _flag_filter_formula

    service, workspace_id = superstore
    snapshot = service.snapshot(service.workspace(workspace_id).active_snapshot_id)
    assert _flag_filter_formula("Total sales on orders that were returned", snapshot) == (
        "CALCULATE(SUM('Orders'[Sales]), 'Returns'[Returned] = TRUE())"
    )
    assert _flag_filter_formula("Number of returned orders", snapshot) == (
        "CALCULATE(DISTINCTCOUNT('Orders'[Order ID]), 'Returns'[Returned] = TRUE())"
    )
    west = _flag_filter_formula("Total sales on returned orders in the West region", snapshot)
    assert west.count("CALCULATE") == 1 and "'Returns'[Returned] = TRUE()" in west
    assert _flag_filter_formula("Average sales of returned orders", snapshot) == (
        "CALCULATE(AVERAGE('Orders'[Sales]), 'Returns'[Returned] = TRUE())"
    )
    assert _flag_filter_formula("Highest sales on returned orders", snapshot) == (
        "CALCULATE(MAX('Orders'[Sales]), 'Returns'[Returned] = TRUE())"
    )
    for goal in (
        "Average order value of returned orders",
        "Sales per returned order",
        "Sales by region for returned orders",
        # Constraints it cannot express would be silently dropped.
        "Total sales on returned orders in 2016",
        "Total sales on returned orders for Phones",
        "Total sales on returned orders for East, West",
        "Calculate returned orders rate",
        # A ranking is no MAX or MIN of the value.
        "Total sales of returned orders in the highest region",
        "Sales of returned orders from the lowest region",
        "Highest number of returned orders",
    ):
        assert _flag_filter_formula(goal, snapshot) is None, goal
    _approve(service, workspace_id, include={"returns", "people"}, key="flag-rescue-approve")
    assert _value(service, workspace_id, west) == 250


def test_flag_filter_rescue_keeps_ids_and_per_order_values_out(superstore, tmp_path) -> None:
    from ai_powerbi_analyst.studio.dax_assistant import _flag_filter_formula, _is_value_column

    service, _ = superstore
    root = tmp_path / "order-values"
    root.mkdir()
    (root / "Orders.csv").write_text(
        "Row ID,storeId,Order ID,Order Value\n1,7,A-1,100\n2,8,A-1,20\n3,9,A-2,250\n",
        encoding="utf-8",
    )
    (root / "Returns.csv").write_text("Returned,Order ID\nYes,A-2\n", encoding="utf-8")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Order values",
            selected_folder=str(root),
            idempotency_key="values-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(root / "Orders.csv"), str(root / "Returns.csv")],
            idempotency_key="values-ingest",
            expected_base_hash=workspace.base_hash,
        )
    )
    orders = next(item.table for item in snapshot.tables if item.table.name == "Orders")
    # Every number here is unique; the ID-like names are keys, not values.
    assert [column.name for column in orders.columns if _is_value_column(column)] == ["Order Value"]
    assert _flag_filter_formula("Total order value of returned orders", snapshot) == (
        "CALCULATE(SUM('Orders'[Order Value]), 'Returns'[Returned] = TRUE())"
    )
    # The row average of Order Value is not the average order value.
    assert _flag_filter_formula("Average order value of returned orders", snapshot) is None


def test_generation_completes_a_named_flag_without_a_repair(superstore) -> None:
    service, workspace_id = superstore
    _approve(service, workspace_id, include={"returns", "people"}, key="flag-generate-approve")
    goal = "Total sales on orders that were returned"
    flagged = "CALCULATE(SUM('Orders'[Sales]), 'Returns'[Returned] = TRUE())"
    seen: list[str] = []
    assert flagged in _ask(service, workspace_id, goal, ["SUM('Orders'[Sales])", "unused"], seen)
    assert len(seen) == 1
    seen = []
    text = _ask(
        service,
        workspace_id,
        "Sales on returned orders managed by Bob",
        ['CALCULATE(SUM(Orders[Sales]), People[Person] = "Bob")', "unused"],
        seen,
    )
    assert "People[Person] = \"Bob\", 'Returns'[Returned] = TRUE())" in text
    assert len(seen) == 1
    assert _value(service, workspace_id, text.split("```dax\n")[1].split("\n```")[0]) == 250
    # No candidate evaluates (the bounded grammar has no SUMX; a small model put the
    # flag on Orders): the rescue answers.
    for failing in (
        "SUMX(Orders, Orders[Sales])",
        "CALCULATE(SUM('Orders'[Sales]), 'Orders'[Returned] = TRUE())",
    ):
        seen = []
        assert flagged in _ask(service, workspace_id, goal, [failing, "x"], seen), failing
        assert len(seen) == 1
    seen = []
    share = (
        "DIVIDE(CALCULATE(SUM('Orders'[Sales]), 'Returns'[Returned] = TRUE()), "
        "SUM('Orders'[Sales]))"
    )
    text = _ask(
        service,
        workspace_id,
        "Share of sales from returned orders",
        ["SUM('Orders'[Sales])", "unused"],
        seen,
    )
    assert share in text
    assert _value(service, workspace_id, share) == pytest.approx(330 / 600)


def test_a_scalar_rescue_never_replaces_a_breakdown_or_another_ratio(superstore) -> None:
    service, workspace_id = superstore
    _approve(service, workspace_id, include={"returns", "people"}, key="flag-breakdown-approve")
    # Percentages skip plan mode, so the model's grouped query is validated as written.
    counts = (
        "EVALUATE SUMMARIZECOLUMNS('Orders'[Region], \"Orders\", DISTINCTCOUNT('Orders'[Order ID]))"
    )
    rates = (
        "EVALUATE SUMMARIZECOLUMNS('Orders'[Region], \"Return rate\", DIVIDE(CALCULATE("
        "DISTINCTCOUNT('Orders'[Order ID]), 'Returns'[Returned] = TRUE()), "
        "DISTINCTCOUNT('Orders'[Order ID])))"
    )
    seen: list[str] = []
    text = _ask(
        service, workspace_id, "Percentage of returned orders by region", [counts, rates], seen
    )
    # Before, the order return rate answered at once, one number for all regions.
    assert rates in text
    assert "DIVIDE(CALCULATE(x, 'Returns'[Returned] = TRUE()), x)" in seen[1]
    assert _value(service, workspace_id, rates) == [
        {"Region": "Central", "Return rate": 1.0},
        {"Region": "West", "Return rate": 0.5},
    ]
    sales = "EVALUATE SUMMARIZECOLUMNS('Orders'[Region], \"Sales\", SUM('Orders'[Sales]))"
    for goal, answer in (
        ("Share of sales from returned orders by region", sales),
        ("What percentage of orders were returned in each region", counts),
        # A query answer keeps its grouping even when the request names none.
        ("Percentage of orders that were returned", counts),
        # A requested grouping stays when no answer evaluates.
        ("Percentage of returned orders by region", "SUMX(Orders, Orders[Sales])"),
        # A margin is no order return rate.
        ("Margin percentage on returned orders", "SUM('Orders'[Sales])"),
    ):
        seen = []
        with pytest.raises(ValueError, match="did not pass local validation"):
            _ask(service, workspace_id, goal, [answer, answer], seen)
        assert len(seen) == 2, goal


def test_plan_mode_emits_the_completed_query_and_never_a_scalar(superstore) -> None:
    service, workspace_id = superstore
    _approve(service, workspace_id, include={"returns", "people"}, key="flag-plan-approve")
    plan = json.dumps(
        {
            "table": "Orders",
            "group_by": ["Region"],
            "operation": "sum",
            "value_column": "Sales",
            "sort_by": [{"column": "value", "direction": "desc"}],
        }
    )
    # The plan cannot name a related table's column; before, the completed query
    # was validated and then discarded for the unfiltered plan.
    text = _ask(service, workspace_id, "Total sales of returned orders by region", [plan, "x"], [])
    formula = text.split("```dax\n")[1].split("\n```")[0]
    assert formula == (
        "EVALUATE SUMMARIZECOLUMNS('Orders'[Region], \"Total Sales\", "
        "CALCULATE(SUM('Orders'[Sales]), 'Returns'[Returned] = TRUE())) "
        "ORDER BY [Total Sales] DESC"
    )
    assert _value(service, workspace_id, formula) == [
        {"Region": "West", "Total Sales": 250},
        {"Region": "Central", "Total Sales": 80},
    ]
    # A scalar rescue would drop the grouping; before, the failing plan was emitted.
    person = json.dumps(
        {
            "table": "Orders",
            "group_by": ["Person"],
            "operation": "distinctcount",
            "value_column": "Order ID",
        }
    )
    with pytest.raises(ValueError, match="Unknown column 'Person'"):
        _ask(service, workspace_id, "Rate of returned orders for each person", [person, person], [])


def test_plan_mode_ships_the_completed_query_in_the_requested_order(superstore) -> None:
    service, workspace_id = superstore
    _approve(service, workspace_id, include={"returns", "people"}, key="flag-order-approve")
    # Sorted by name, as the model planned, Central would come before West.
    plan = json.dumps(
        {
            "table": "Orders",
            "group_by": ["Region"],
            "operation": "sum",
            "value_column": "Sales",
            "sort_by": [{"column": "Region", "direction": "asc"}],
        }
    )
    seen: list[str] = []
    goal = "Total sales of returned orders by region, highest first"
    text = _ask(service, workspace_id, goal, [plan, "x"], seen)
    formula = text.split("```dax\n")[1].split("\n```")[0]
    assert formula == (
        "EVALUATE SUMMARIZECOLUMNS('Orders'[Region], \"Total Sales\", "
        "CALCULATE(SUM('Orders'[Sales]), 'Returns'[Returned] = TRUE())) "
        "ORDER BY [Total Sales] DESC"
    )
    assert len(seen) == 1
    assert _value(service, workspace_id, formula) == [
        {"Region": "West", "Total Sales": 250},
        {"Region": "Central", "Total Sales": 80},
    ]


def test_a_flag_without_its_relationship_fails_with_the_reason(superstore) -> None:
    service, workspace_id = superstore
    seen: list[str] = []
    with pytest.raises(ValueError, match="Orders → Returns relationship") as failure:
        _ask(
            service,
            workspace_id,
            "Total sales on orders that were returned",
            ["SUM('Orders'[Sales])", "SUM('Orders'[Sales])"],
            seen,
        )
    # The repair turn names both the missing condition and why it could not be added.
    assert "ignores it" in seen[1]
    assert "Orders → Returns relationship" in seen[1]
    # The user's message is clipped, but keeps the whole reason.
    assert "only through an approved relationship." in str(failure.value)


def test_constant_ratios_are_found_in_any_spelling_and_filter_context(superstore) -> None:
    from ai_powerbi_analyst.studio.dax import degenerate_ratio
    from ai_powerbi_analyst.studio.dax_assistant import _request_gap

    service, workspace_id = superstore
    snapshot = service.snapshot(service.workspace(workspace_id).active_snapshot_id)
    goal = "Share of total sales that comes from the West region"
    # Measured on Superstore: the named filter wrapped around DIVIDE(x, x) reached
    # both sides, and this "share of total sales" was accepted at 1.
    wrapped = (
        "CALCULATE(DIVIDE(SUM(Orders[Sales]), SUM('Orders'[Sales])), Orders[Region] = \"West\")"
    )
    both = (
        'CALCULATE(DIVIDE(CALCULATE(SUM(Orders[Sales]), Orders[Region] = "West"), '
        'SUM(Orders[Sales])), Orders[Region] = "West")'
    )
    for constant in (
        wrapped,
        "SUM(Orders[Sales]) / SUM('Orders'[Sales])",
        # The outer filter reaches the total too: both sides see West alone.
        both,
        # One Region under a Region filter: the "share" is West's sales.
        "CALCULATE(DIVIDE(SUM(Orders[Sales]), DISTINCTCOUNT(Orders[Region])), "
        'Orders[Region] = "West")',
        'EVALUATE SUMMARIZECOLUMNS(People[Person], "S", '
        "DIVIDE(SUM(Orders[Sales]), SUM(Orders[Sales])))",
        # A filter written twice is still the one filter.
        'DIVIDE(CALCULATE(SUM(Orders[Sales]), Orders[Region] = "West", Orders[Region] = "West"), '
        'CALCULATE(SUM(Orders[Sales]), Orders[Region] = "West"))',
    ):
        assert "always 1" in degenerate_ratio(constant), constant
        gap = _request_gap(goal, constant, snapshot, "", False)
        assert "always 1" in gap and "DIVIDE(CALCULATE(x, 'Orders'[Region] = \"West\"), x)" in gap
    for ratio in (
        'DIVIDE(CALCULATE(SUM(Orders[Sales]), Orders[Region] = "West"), SUM(Orders[Sales]))',
        "CALCULATE(DIVIDE(CALCULATE(SUM(Orders[Sales]), Returns[Returned] = TRUE()), "
        'SUM(Orders[Sales])), People[Person] = "Bob")',
        "DIVIDE(SUM(Orders[Sales]), DISTINCTCOUNT(Orders[Order ID]))",
        # Outside the lab subset: the evaluator reports it instead.
        "DIVIDE(SUM(Orders[Sales]), CALCULATE(SUM(Orders[Sales]), REMOVEFILTERS(Orders)))",
    ):
        assert degenerate_ratio(ratio) is None, ratio
    # The lab computes DAX's constant as well; only the check tells it from a share.
    assert _value(service, workspace_id, wrapped) == 1
    assert _value(service, workspace_id, both) == 1


def test_an_explicit_share_gets_its_part_on_the_numerator_only(superstore) -> None:
    from ai_powerbi_analyst.studio.dax_assistant import (
        _named_share_formula,
        _request_gap,
        _share_part,
        _with_named_filters,
    )

    service, workspace_id = superstore
    snapshot = service.snapshot(service.workspace(workspace_id).active_snapshot_id)
    goal = "Share of total sales that comes from the West region"
    share = (
        "DIVIDE(CALCULATE(SUM('Orders'[Sales]), 'Orders'[Region] = \"West\"), SUM('Orders'[Sales]))"
    )
    self_ratio = "DIVIDE(SUM(Orders[Sales]), SUM(Orders[Sales]))"
    # Wrapped in the named filter, both sides would see West alone.
    assert _with_named_filters(goal, self_ratio, snapshot) is None
    for written in (
        self_ratio,
        "SUM(Orders[Sales]) / SUM('Orders'[Sales])",
        f'CALCULATE({self_ratio}, Orders[Region] = "West")',
        'DIVIDE(CALCULATE(SUM(Orders[Sales]), Orders[Region] = "West"), '
        'CALCULATE(SUM(Orders[Sales]), Orders[Region] = "West"))',
    ):
        assert _named_share_formula(goal, written, snapshot) == share, written
    # People[Region] lists West too: one part, on the aggregate's own table.
    assert _share_part(goal, snapshot, "Orders") == ("Orders", "Region", "West")
    assert _share_part(goal, snapshot, "People") == ("People", "Region", "West")
    # One aggregate is no share yet, but it is never rewritten: a request can read like a
    # share and not be one. It gets the share's shape as its repair hint, an average too.
    hint = (
        "The request asks for a share: divide the part by the total, e.g. "
        "DIVIDE(CALCULATE(SUM('Orders'[Sales]), 'Orders'[Region] = \"West\"), "
        "SUM('Orders'[Sales]))."
    )
    for plain in (
        "SUM(Orders[Sales])",
        'CALCULATE(SUM(Orders[Sales]), Orders[Region] = "West")',
        'CALCULATE(SUM(Orders[Sales]), People[Region] = "West")',
    ):
        assert _named_share_formula(goal, plain, snapshot) is None, plain
        assert _request_gap(goal, plain, snapshot, "", False) == hint, plain
    average = 'CALCULATE(AVERAGE(Orders[Sales]), Orders[Region] = "West")'
    assert _named_share_formula(goal, average, snapshot) is None
    assert _request_gap(goal, average, snapshot, "", False) == (
        "The request asks for a share: divide the part by the total, e.g. "
        "DIVIDE(CALCULATE(x, 'Orders'[Region] = \"West\"), x)."
    )
    assert _request_gap(goal, share, snapshot, "", False) is None
    # The model's measures are kept; a measure alone may already be the share.
    assert _named_share_formula(goal, "DIVIDE([Total Sales], [Total Sales])", snapshot) == (
        "DIVIDE(CALCULATE([Total Sales], 'Orders'[Region] = \"West\"), [Total Sales])"
    )
    assert _named_share_formula(goal, "[West Share]", snapshot) is None
    assert "A share" not in _request_gap(goal, "[West Share]", snapshot, "", False)
    # A ratio of two different values stands.
    per_order = "DIVIDE(SUM(Orders[Sales]), DISTINCTCOUNT(Orders[Order ID]))"
    assert _named_share_formula(goal, per_order, snapshot) is None
    assert _value(service, workspace_id, share) == pytest.approx(400 / 600)


def test_a_share_rewrite_needs_share_wording_and_an_explicit_part(superstore) -> None:
    from ai_powerbi_analyst.studio.dax_assistant import _named_share_formula, _share_part

    service, workspace_id = superstore
    snapshot = service.snapshot(service.workspace(workspace_id).active_snapshot_id)
    total = 'CALCULATE(SUM(Orders[Sales]), Orders[Region] = "West")'
    self_ratio = (
        'CALCULATE(DIVIDE(SUM(Orders[Sales]), SUM(Orders[Sales])), Orders[Region] = "West")'
    )
    for goal in (
        # No share wording: West is an ordinary filter.
        "Calculate total sales from the West region",
        # West only scopes the share; its part is not a listed value.
        "Calculate the percentage of West region sales above 100",
        "Percentage of total sales in the West region that is above 100",
        "Share of sales not from the West region",
        "Calculate the contribution margin from the West region",
        "Average sales per order from the West region",
        # Two values of one column are no single equality filter.
        "Share of sales from the West and East regions",
    ):
        assert _share_part(goal, snapshot) is None, goal
        for written in (total, self_ratio):
            assert _named_share_formula(goal, written, snapshot) is None, goal
    # A query keeps its grouping: it is never rewritten to a scalar share.
    grouped = (
        'EVALUATE SUMMARIZECOLUMNS(People[Person], "S", '
        "DIVIDE(SUM(Orders[Sales]), SUM(Orders[Sales])))"
    )
    by_person = "Share of total sales from the West region by person"
    assert _named_share_formula(by_person, grouped, snapshot) is None


def test_generation_answers_an_explicit_share_without_a_repair(superstore) -> None:
    service, workspace_id = superstore
    _approve(service, workspace_id, include={"returns", "people"}, key="share-generate-approve")
    goal = "Share of total sales that comes from the West region"
    share = (
        "DIVIDE(CALCULATE(SUM('Orders'[Sales]), 'Orders'[Region] = \"West\"), SUM('Orders'[Sales]))"
    )
    # A constant ratio, or a share whose total the lab cannot parse (REMOVEFILTERS), gets
    # the share on the first model call.
    for answer in (
        "DIVIDE(SUM(Orders[Sales]), SUM(Orders[Sales]))",
        'DIVIDE(CALCULATE(SUM(Orders[Sales]), Orders[Region] = "West"), '
        "CALCULATE(SUM(Orders[Sales]), REMOVEFILTERS(Orders[Region])))",
    ):
        seen: list[str] = []
        assert f"```dax\n{share}\n```" in _ask(service, workspace_id, goal, [answer, answer], seen)
        assert len(seen) == 1, answer
    # A single aggregate, an average too, gets the repair turn; the model's share ships.
    for answer in ("AVERAGE('Orders'[Sales])", "SUM('Orders'[Sales])"):
        seen = []
        assert f"```dax\n{share}\n```" in _ask(service, workspace_id, goal, [answer, share], seen)
        assert len(seen) == 2 and "asks for a share" in seen[1], answer
    assert _value(service, workspace_id, share) == pytest.approx(400 / 600)
    # A refined share scoped to West keeps its own part: nothing is moved or rewritten.
    returned = (
        "DIVIDE(CALCULATE(SUM(Orders[Sales]), Returns[Returned] = TRUE()), SUM(Orders[Sales]))"
    )
    scoped = f'CALCULATE({returned}, Orders[Region] = "West")'
    seen = []
    text = _ask(
        service,
        workspace_id,
        "Now show the same share for the West region only",
        [scoped, "unused"],
        seen,
        expression=returned,
        discussion=f"User: Share of sales from returned orders\nAssistant: ```dax\n{returned}\n```",
    )
    assert f"```dax\n{scoped}\n```" in text
    assert len(seen) == 1
    assert _value(service, workspace_id, scoped) == pytest.approx(250 / 400)
    # A grouped answer is repaired by the model, never replaced by a scalar share.
    constant = (
        'EVALUATE SUMMARIZECOLUMNS(People[Person], "Share", '
        "DIVIDE(SUM(Orders[Sales]), SUM(Orders[Sales])))"
    )
    by_person = (
        'EVALUATE SUMMARIZECOLUMNS(People[Person], "Share", '
        'DIVIDE(CALCULATE(SUM(Orders[Sales]), Orders[Region] = "West"), SUM(Orders[Sales])))'
    )
    seen = []
    text = _ask(
        service,
        workspace_id,
        "Show the share of total sales from the West region by person",
        [constant, by_person],
        seen,
    )
    assert f"```dax\n{by_person}\n```" in text
    assert "always 1" in seen[1]


def test_share_rescue_answers_only_what_it_accounts_for(superstore) -> None:
    from ai_powerbi_analyst.studio.dax_assistant import _share_rescue

    service, workspace_id = superstore
    snapshot = service.snapshot(service.workspace(workspace_id).active_snapshot_id)
    share = (
        "DIVIDE(CALCULATE(SUM('Orders'[Sales]), 'Orders'[Region] = \"West\"), SUM('Orders'[Sales]))"
    )
    for goal in (
        "Share of total sales that comes from the West region",
        "What percentage of total sales comes from West",
        "Calculate the West share of sales",
    ):
        assert _share_rescue(goal, snapshot) == share, goal
    for goal in (
        # It answers from the request alone, before any repair turn: a constraint the
        # lab cannot express, or one it does not list, would be dropped.
        "Share of total sales that comes from the West region in 2016",
        "Share of total sales that comes from the West region for large orders",
        "Share of total sales that comes from the West region by person",
    ):
        assert _share_rescue(goal, snapshot) is None, goal
    # Before, the first failed attempt was answered with West's share of all years.
    unparsable = (
        'DIVIDE(CALCULATE(SUM(Orders[Sales]), Orders[Region] = "West"), '
        "CALCULATE(SUM(Orders[Sales]), REMOVEFILTERS(Orders[Region])))"
    )
    # The closed pattern gate now refuses the unsupported year before a model
    # repair. Preserve the original protection against silently dropping 2016.
    for goal in (
        "Share of total sales that comes from the West region in 2016",
        "What percentage of total sales comes from West during 2016?",
        "Calculate the West share of sales for 2016",
    ):
        before = service.workspace(workspace_id)
        draft_before = service.draft(before.active_draft_id)
        seen: list[str] = []
        all_calls: list[str] = []
        text = _ask(
            service,
            workspace_id,
            goal,
            [unparsable, unparsable],
            seen,
            all_calls=all_calls,
        )
        assert (
            "requested filter or fiscal calendar resolved" in text
            or "requested date or current period filter first" in text
        ), goal
        assert "No formula or report change was applied" in text
        assert "```" not in text and "Local preview" not in text
        assert seen == []
        assert all_calls == []
        assert service.workspace(workspace_id) == before
        assert service.draft(before.active_draft_id) == draft_before


def test_saved_related_measure_requires_its_relationship_at_approval(superstore) -> None:
    service, workspace_id = superstore
    _approve(service, workspace_id, include={"returns", "people"}, key="related-approve")
    draft = service.draft(service.workspace(workspace_id).active_draft_id)
    saved = service.add_dax_measure(
        workspace_id,
        AddDaxMeasureRequest(
            workspace_id=workspace_id,
            name="Returned Sales",
            expression="CALCULATE(SUM('Orders'[Sales]), 'Returns'[Returned] = TRUE())",
            idempotency_key="related-save",
            expected_base_hash=draft.base_hash,
        ),
    )
    measure = next(m for m in saved.measure_proposal.measures if m.name == "Returned Sales")
    assert measure.table_id.startswith("orders")
    # A saved share measure still groups by a related table: nesting counts measure hops.
    assert _value(
        service,
        workspace_id,
        'EVALUATE SUMMARIZECOLUMNS(People[Person], "Returned", [Returned Sales]) '
        "ORDER BY People[Person] ASC",
    ) == [
        {"Person": "Bob", "Returned": 250},
        {"Person": "Cy", "Returned": 80},
    ]
    service.approve_advanced_dax(
        saved.id,
        measure.id,
        ApproveAdvancedDaxRequest(
            workspace_id=workspace_id,
            draft_id=saved.id,
            measure_id=measure.id,
            approved=True,
            approval_note="Returned sales reviewed",
            idempotency_key="related-approve-measure",
            expected_base_hash=saved.base_hash,
        ),
    )
    with pytest.raises(StudioPlanningError, match="Returned Sales"):
        _approve(service, workspace_id, include={"people"}, key="related-approve-without-returns")
    _approve(service, workspace_id, include={"returns", "people"}, key="related-approve-returns")
