"""Eval accuracy must describe the operation applied, with conservative equivalences."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.studio.edit_context import EditContext, MeasureEntry, PageEntry
from ai_powerbi_analyst.studio.edit_planner import PlanOutcome, PlanStep, available_cards
from ai_powerbi_analyst.studio.edit_tools import Slot, ToolCard
from ai_powerbi_analyst.studio.edit_types import Clause, ParseKind


@pytest.fixture
def scorer(monkeypatch: pytest.MonkeyPatch) -> Any:
    path = Path(__file__).resolve().parents[2] / "evals" / "imported_edit"
    monkeypatch.syspath_prepend(str(path))
    return importlib.import_module("planner_eval")


@pytest.fixture
def context() -> EditContext:
    return EditContext(
        pages=[
            PageEntry("active", "Agents", 0, 1920, 1200),
            PageEntry("destination", "Forecast", 1, 1920, 1200),
        ],
        visuals=[],
        measures=[],
        active_page="active",
        tables={"Date": ["Date", "Year"], "FactSales": ["StoreKey"], "DimStore": ["StoreKey"]},
        column_types={"Date.Date": "datetime64[ns]", "Date.Year": "int64"},
    )


def score(
    scorer: Any,
    context: EditContext,
    op: str,
    wanted: dict[str, Any],
    operation: dict[str, Any] | None,
    slots: dict[str, Any] | None = None,
    cards: tuple[ToolCard, ...] | None = None,
    message: str = "",
) -> dict[str, Any]:
    case = {
        "context": "synthetic",
        "no_apply": True,
        "message": message,
        "expect": {"kind": "plan", "steps": [{"op": op, "slots": wanted}]},
    }
    steps = (
        ()
        if operation is None
        else (PlanStep(operation["op"], operation, "synthetic", slots=slots or {}),)
    )
    return scorer.score_outcome(
        case,
        PlanOutcome("plan", steps),
        cards or available_cards(),
        context,
        None,
    )


@pytest.mark.parametrize("operation", [None, {"op": "wrong_tool"}])
def test_missing_or_wrong_tool_still_counts_all_gold_slots(
    scorer: Any,
    context: EditContext,
    operation: dict[str, Any] | None,
) -> None:
    card = ToolCard(
        "correct_tool",
        "test",
        "test",
        (
            Slot("visual", "visual", ""),
            Slot("show", "bool", ""),
        ),
    )
    result = score(
        scorer, context, card.op, {"visual": "v", "show": True}, operation, cards=(card,)
    )
    assert (result["tools_ok"], result["slots_ok"], result["slots"]) == (0, 0, 2)


def test_relationship_tables_are_scored_from_effective_operation(
    scorer: Any,
    context: EditContext,
) -> None:
    wanted = {
        "from_table": "FactSales",
        "from": "FactSales[StoreKey]",
        "to_table": "DimStore",
        "to": "DimStore[StoreKey]",
    }
    operation = {
        "op": "create_relationship",
        "from_table": "FactSales",
        "from_column": "StoreKey",
        "to_table": "DimStore",
        "to_column": "StoreKey",
    }
    raw = {
        "from": {"table": "FactSales", "name": "StoreKey"},
        "to": {"table": "DimStore", "name": "StoreKey"},
    }
    result = score(scorer, context, operation["op"], wanted, operation, raw)
    assert (result["slots_ok"], result["slots"], result["unasked"]) == (4, 4, 0)
    operation["to_table"] = "FactSales"
    wrong = score(scorer, context, operation["op"], wanted, operation, raw)
    assert wrong["slots_ok"] == 2  # accurate raw slots cannot hide the wrong applied end


def test_unregistered_gold_card_still_counts_declared_slots(
    scorer: Any,
    context: EditContext,
) -> None:
    case = {
        "context": "synthetic",
        "no_apply": True,
        "expect": {
            "kind": "plan",
            "steps": [
                {"op": "unregistered", "slots_by_kind": {"visual": "v", "bool": True}},
            ],
        },
    }
    result = scorer.score_outcome(case, PlanOutcome("failed"), (), context, None)
    assert (result["tools_ok"], result["slots_ok"], result["slots"]) == (0, 0, 2)


def test_hierarchy_levels_use_operation_column_names(
    scorer: Any,
    context: EditContext,
) -> None:
    result = score(
        scorer,
        context,
        "create_hierarchy",
        {"levels": ["Year", "MonthName"]},
        {
            "op": "create_hierarchy",
            "table": "Date",
            "name": "Calendar",
            "levels": [{"column": "Year"}, {"column": "MonthName"}],
        },
    )
    assert (result["slots_ok"], result["slots"]) == (1, 1)


@pytest.mark.parametrize(
    "enabled,field,remove,unasked",
    [
        (True, "ChannelName", False, 0),
        (False, "ChannelName", True, 1),
        (True, "Other", False, 1),
    ],
)
def test_small_multiples_enable_is_implied_only_by_the_requested_field(
    scorer: Any,
    context: EditContext,
    enabled: bool,
    field: str,
    remove: bool,
    unasked: int,
) -> None:
    operation: dict[str, Any] = {
        "op": "set_small_multiples",
        "visual": "v",
        "field": {"table": "DimChannel", "name": field},
    }
    if remove:
        operation["remove"] = True
    result = score(
        scorer,
        context,
        operation["op"],
        {"visual": "v", "field": "ChannelName"},
        operation,
        {"small_multiples": enabled},
    )
    assert result["unasked"] == unasked
    assert result["slots_ok"] == (2 if field == "ChannelName" else 1)


def test_small_multiples_unasked_layout_remains_an_error(
    scorer: Any,
    context: EditContext,
) -> None:
    result = score(
        scorer,
        context,
        "set_small_multiples",
        {"visual": "v", "field": "ChannelName"},
        {"op": "set_small_multiples", "visual": "v", "field": {"name": "ChannelName"}, "rows": 4},
    )
    assert result["unasked"] == 1


@pytest.mark.parametrize(
    "column,second_date,unasked",
    [
        ("Date", False, 0),
        ("Year", False, 1),
        ("Date", True, 1),
    ],
)
def test_date_column_default_requires_the_unique_date_column(
    scorer: Any,
    context: EditContext,
    column: str,
    second_date: bool,
    unasked: int,
) -> None:
    if second_date:
        context.tables["Date"].append("OtherDate")
        context.column_types["Date.OtherDate"] = "datetime64[ns]"
    result = score(
        scorer,
        context,
        "mark_date_table",
        {"table": "Date"},
        {"op": "mark_date_table", "table": "Date", "column": column},
    )
    assert (result["slots_ok"], result["slots"], result["unasked"]) == (1, 1, unasked)


@pytest.mark.parametrize("placement,unasked", [("active", 0), ("destination", 1)])
def test_navigation_button_destination_does_not_replace_implicit_placement(
    scorer: Any,
    context: EditContext,
    placement: str,
    unasked: int,
) -> None:
    operation = {
        "op": "add_button",
        "page": placement,
        "action": {"type": "page", "page": "destination"},
    }
    result = score(
        scorer,
        context,
        "add_button",
        {"action": "page", "target_page": "Forecast"},
        operation,
        {"page": "active", "target_page": "destination", "action": "page"},
    )
    assert (result["slots_ok"], result["slots"], result["unasked"]) == (2, 2, unasked)


def test_explicit_placement_and_unasked_button_text_are_independent(
    scorer: Any,
    context: EditContext,
) -> None:
    result = score(
        scorer,
        context,
        "add_button",
        {"page": "Forecast", "action": "page", "target_page": "Forecast"},
        {
            "op": "add_button",
            "page": "destination",
            "text": "Invented label",
            "action": {"type": "page", "page": "destination"},
        },
    )
    assert (result["slots_ok"], result["slots"], result["unasked"]) == (3, 3, 1)


def test_nested_operation_destination_overrules_accurate_raw_slots(
    scorer: Any,
    context: EditContext,
) -> None:
    result = score(
        scorer,
        context,
        "add_button",
        {"action": "page", "target_page": "Forecast"},
        {"op": "add_button", "page": "active", "action": {"type": "page", "page": "active"}},
        {"action": "page", "target_page": "destination"},
    )
    assert (result["slots_ok"], result["slots"]) == (1, 2)


@pytest.mark.parametrize(
    "action,target,unasked",
    [
        ("back", None, 0),
        ("back", "active", 1),
        ("page", "active", 2),
    ],
)
def test_back_kind_implies_back_action_but_no_unrelated_destination(
    scorer: Any,
    context: EditContext,
    action: str,
    target: str | None,
    unasked: int,
) -> None:
    nested = {"type": action}
    raw: dict[str, Any] = {"kind": "back", "action": action}
    if target is not None:
        nested["page"] = target
        raw["target_page"] = target
    result = score(
        scorer,
        context,
        "add_button",
        {"kind": "back"},
        {"op": "add_button", "page": "active", "kind": "back", "action": nested},
        raw,
    )
    assert (result["slots_ok"], result["slots"], result["unasked"]) == (1, 1, unasked)


@pytest.mark.parametrize("actual,correct", [("clusteredBarChart", 1), ("barChart", 0)])
def test_visual_type_alias_requires_the_same_stored_type(
    scorer: Any,
    context: EditContext,
    actual: str,
    correct: int,
) -> None:
    result = score(
        scorer,
        context,
        "change_visual_type",
        {"visual_type": "bar"},
        {"op": "change_visual_type", "visual_type": actual, "visual": "v"},
    )
    assert result["slots_ok"] == correct


@pytest.mark.parametrize("target,unasked", [("canvas", 0), ("wallpaper", 1)])
def test_builder_default_is_allowed_but_a_changed_default_is_unasked(
    scorer: Any,
    context: EditContext,
    target: str,
    unasked: int,
) -> None:
    card = ToolCard(
        "example",
        "test",
        "test",
        (Slot("target", "enum", ""),),
        build=lambda _slots: {"op": "example", "target": "canvas"},
    )
    result = score(
        scorer, context, "example", {}, {"op": "example", "target": target}, cards=(card,)
    )
    assert result["unasked"] == unasked


def test_an_explicit_unasked_slot_is_not_excused_as_a_builder_default(
    scorer: Any,
    context: EditContext,
) -> None:
    card = ToolCard(
        "example",
        "test",
        "test",
        (Slot("target", "enum", ""),),
        build=lambda _slots: {"op": "example", "target": "canvas"},
    )
    result = score(
        scorer,
        context,
        "example",
        {},
        {"op": "example", "target": "canvas"},
        {"target": "canvas"},
        cards=(card,),
    )
    assert result["unasked"] == 1


@pytest.mark.parametrize("page,kept", [("active", False), ("destination", True)])
def test_noisy_navigation_page_accepts_only_the_required_active_placement(
    scorer: Any,
    context: EditContext,
    monkeypatch: pytest.MonkeyPatch,
    page: str,
    kept: bool,
) -> None:
    case = {
        "context": "synthetic",
        "message": "add a button that goes to Forecast",
        "expect": {
            "kind": "plan",
            "steps": [
                {"op": "add_button", "slots": {"target_page": "Forecast", "action": "page"}},
            ],
        },
    }
    model = scorer.NoisyModel(case, available_cards(), 3)
    model.invented = [(0, "page", page)]
    monkeypatch.setattr(scorer, "context_of", lambda _case: context)
    step = PlanStep("add_button", {"op": "add_button", "page": page}, "", slots={"page": page})
    assert bool(scorer.kept_noise(model, PlanOutcome("plan", (step,)), 1)) is kept


# --- drafted formulas, role filters, built values, asking, places, unverified -----------------

DISCOUNT_SHARE = {
    "formula": {
        "uses": ["FactSales[DiscountAmount]", "[Total Sales]"],
        "op": "divide",
        "numerator": ["FactSales[DiscountAmount]"],
        "denominator": ["[Total Sales]"],
    },
    "example": "DIVIDE(SUM(FactSales[DiscountAmount]), [Total Sales])",
}


@pytest.fixture
def model_context(context: EditContext) -> EditContext:
    context.tables.update(
        {
            "FactSales": ["SalesAmount", "TotalCost", "DiscountAmount", "StoreKey"],
            "DimChannel": ["ChannelKey", "ChannelName"],
        }
    )
    context.measures = [
        MeasureEntry("Total Sales", "# Measures", "SUM ( FactSales[SalesAmount] )"),
        MeasureEntry(
            "Profit",
            "# Measures",
            "SUMX ( FactSales, FactSales[SalesAmount] - FactSales[TotalCost] )",
        ),
        MeasureEntry("Helper", "# Measures", "0"),
    ]
    return context


@pytest.mark.parametrize(
    "formula,right",
    [
        ("DIVIDE(SUM(FactSales[DiscountAmount]), [Total Sales])", True),
        ("SUM(FactSales[DiscountAmount]) / SUM(FactSales[SalesAmount])", True),  # what it computes
        ("DIVIDE([Total Sales],[Total Sales])", False),  # lv13 as Qwen drafted it: always 1
        ("DIVIDE([Total Sales], SUM(FactSales[DiscountAmount]))", False),  # upside down
        ("SUM(FactSales[DiscountAmount]) - [Total Sales]", False),  # not a division
        ("DIVIDE(SUM(FactSales[DiscountAmount]), [Total Sales]", False),  # does not parse
    ],
)
def test_a_drafted_formula_is_scored_by_what_it_computes(
    scorer: Any,
    model_context: EditContext,
    formula: str,
    right: bool,
) -> None:
    result = score(
        scorer,
        model_context,
        "create_measure",
        {"name": "QA Discount Share", "expression": DISCOUNT_SHARE},
        {
            "op": "create_measure",
            "name": "QA Discount Share",
            "expression": formula,
            "table": "# Measures",
        },
        {"name": "QA Discount Share"},
    )
    assert (result["slots_ok"], result["slots"]) == ((2 if right else 1), 2)
    assert scorer.plan_is_right(result) is right
    assert all(formula not in problem for problem in result["problems"])  # names only


PROFIT_GOLD = (
    "SUMX(FactSales, FactSales[SalesAmount] - FactSales[TotalCost] - FactSales[DiscountAmount])"
)


@pytest.mark.parametrize(
    "formula,right",
    [
        (PROFIT_GOLD, True),
        ("[Total Sales] - SUM(FactSales[TotalCost]) - SUM(FactSales[DiscountAmount])", True),
        ("SUMX(FactSales, [SalesAmount] - [TotalCost]) - SUM(FactSales[DiscountAmount])", True),
        ("[Total Sales] - [Helper]", False),  # ms07 as Qwen drafted it: the cost is gone
        ("[Profit] - SUM(FactSales[DiscountAmount])", False),  # circular: Profit names itself
    ],
)
def test_a_reference_formula_gold_gives_its_references_and_operation(
    scorer: Any,
    model_context: EditContext,
    formula: str,
    right: bool,
) -> None:
    result = score(
        scorer,
        model_context,
        "update_measure",
        {"name": "Profit", "expression": PROFIT_GOLD},
        {"op": "update_measure", "name": "Profit", "expression": formula},
    )
    assert (result["slots_ok"] == result["slots"]) is right


@pytest.mark.parametrize(
    "formula,wrong",
    [
        ("DIVIDE([Total Sales],[Total Sales])", 1),
        ("SUM(FactSales[DiscountAmount])", 0),
    ],
)
def test_a_formula_the_gold_does_not_specify_is_unverified_and_still_checked_for_defects(
    scorer: Any,
    model_context: EditContext,
    formula: str,
    wrong: int,
) -> None:
    result = score(
        scorer,
        model_context,
        "create_measure",
        {"name": "QA Discount Share"},
        {
            "op": "create_measure",
            "name": "QA Discount Share",
            "expression": formula,
            "table": "# Measures",
        },
        {"name": "QA Discount Share"},
    )
    assert result["wrong_formulas"] == wrong
    assert any("no spec" in reason for reason in result["unverified"])
    assert scorer.plan_is_right(result) is (wrong == 0)


ROLE = "create a security role Online only that only sees the Online channel"


@pytest.mark.parametrize(
    "filters,right",
    [
        (None, False),  # md06 as planned: a role with no filter sees every row
        ([{"table": "DimChannel", "expression": '[ChannelName] = "Online"'}], True),
    ],
)
def test_a_role_the_request_limits_needs_a_filter(
    scorer: Any,
    model_context: EditContext,
    filters: Any,
    right: bool,
) -> None:
    operation: dict[str, Any] = {"op": "create_role", "name": "Online only"}
    if filters is not None:
        operation["filters"] = filters
    result = score(
        scorer,
        model_context,
        "create_role",
        {"name": "Online only"},
        operation,
        {"name": "Online only"},
        message=ROLE,
    )
    assert (result["slots_ok"], result["slots"]) == ((2 if right else 1), 2)
    assert result["unasked"] == 0  # DimChannel is the table of the filter's column
    if right:  # present, but what it keeps is not in this gold
        assert any("filter" in reason for reason in result["unverified"])


@pytest.mark.parametrize(
    "expression,right",
    [
        ('[ChannelName] = "Online"', True),
        ('DimChannel[ChannelName] IN {"Online"}', True),
        ('[ChannelName] = "Store"', False),
        ('[StoreKey] = "Online"', False),
    ],
)
def test_a_role_filter_spec_requires_its_field_and_value(
    scorer: Any,
    model_context: EditContext,
    expression: str,
    right: bool,
) -> None:
    spec = {"formula": {"uses": ["DimChannel[ChannelName]"], "values": ["Online"], "op": "equals"}}
    result = score(
        scorer,
        model_context,
        "create_role",
        {"name": "Online only", "table": "DimChannel", "filter": spec},
        {
            "op": "create_role",
            "name": "Online only",
            "filters": [{"table": "DimChannel", "expression": expression}],
        },
        message=ROLE,
    )
    assert (result["slots_ok"] == result["slots"] == 3) is right


CF02 = "color the vs Previous Month values red when they are below 0"
RED_BELOW = {"max": 0, "color": "#D13438"}
GREEN_ABOVE = {"min": 0, "color": "#1AAB40"}


@pytest.mark.parametrize(
    "message,rules,unasked",
    [
        (CF02, [RED_BELOW, GREEN_ABOVE], 1),  # cf02 as built: a green rule nobody asked for
        (CF02, [RED_BELOW], 0),
        (CF02 + " and green from 0 up", [RED_BELOW, GREEN_ABOVE], 0),
    ],
)
def test_a_color_the_build_adds_is_unasked_unless_the_request_names_it(
    scorer: Any,
    context: EditContext,
    message: str,
    rules: list[dict[str, Any]],
    unasked: int,
) -> None:
    wanted = {
        "visual": "e82e02f9",
        "field": "vs Previous Month",
        "style": "rules",
        "threshold": 0,
        "below_color": "red",
    }
    raw = {
        "visual": "e82e02f9",
        "field": "vs Previous Month",
        "style": "rules",
        "threshold": 0,
        "below_color": "#D13438",
    }
    result = score(
        scorer,
        context,
        "set_conditional_format",
        wanted,
        {
            "op": "set_conditional_format",
            "visual": "e82e02f9",
            "style": "rules",
            "field": "vs Previous Month",
            "rules": rules,
        },
        raw,
        message=message,
    )
    assert result["unasked"] == unasked
    if unasked:
        assert any("#1AAB40" in problem for problem in result["problems"])


PLACED = ToolCard(
    "placed",
    "test",
    "test",
    (
        Slot("page", "page", ""),
        Slot("table", "table", ""),
        Slot("field", "field", ""),
    ),
)


@pytest.mark.parametrize(
    "operation,unasked",
    [
        ({"op": "placed", "page": "active"}, 0),  # the page on screen
        ({"op": "placed", "page": "destination"}, 1),  # a page nobody named
        ({"op": "placed", "table": "DimStore"}, 1),  # a table nobody named
        (
            {
                "op": "placed",
                "table": "DimStore",
                "field": {"table": "DimStore", "name": "StoreKey"},
            },
            0,
        ),  # the table of the field the step names
    ],
)
def test_an_unrequested_page_or_table_is_unasked_unless_implied(
    scorer: Any,
    context: EditContext,
    operation: dict[str, Any],
    unasked: int,
) -> None:
    wanted = {"field": "StoreKey"} if "field" in operation else {}
    result = score(scorer, context, "placed", wanted, operation, cards=(PLACED,))
    assert result["unasked"] == unasked


@pytest.mark.parametrize("table,unasked", [("# Measures", 0), ("FactSales", 1)])
def test_a_new_measure_may_go_only_to_the_measures_home_table(
    scorer: Any,
    model_context: EditContext,
    table: str,
    unasked: int,
) -> None:
    result = score(
        scorer,
        model_context,
        "create_measure",
        {"name": "Total Cost", "expression": "SUM(FactSales[TotalCost])"},
        {
            "op": "create_measure",
            "name": "Total Cost",
            "expression": "SUM(FactSales[TotalCost])",
            "table": table,
        },
        {"name": "Total Cost"},
    )
    assert result["unasked"] == unasked


@pytest.mark.parametrize(
    "outcome,asks",
    [
        (PlanOutcome("failed", reason="gave up"), False),
        (PlanOutcome("plan"), False),
        (PlanOutcome("unsupported"), False),
        (
            PlanOutcome("clarify", clause=Clause("x", ParseKind.CLARIFY, question="Which one?")),
            True,
        ),
    ],
)
def test_model_asks_accepts_only_a_question_or_a_choice(
    scorer: Any,
    outcome: PlanOutcome,
    asks: bool,
) -> None:
    assert scorer.asks_back(outcome) is asks


class _Checker:
    def __init__(self) -> None:
        self.calls = 0

    def check_ops(self, ops: tuple[dict[str, Any], ...], context: str) -> list[str]:
        self.calls += 1
        return []


def test_no_apply_plans_are_unverified_and_checked_ones_are_not(
    scorer: Any,
    context: EditContext,
) -> None:
    step = PlanStep("delete_bookmark", {"op": "delete_bookmark", "bookmark": "Q3 view"}, "")
    gold = {"kind": "plan", "steps": [{"op": "delete_bookmark", "slots": {"bookmark": "Q3 view"}}]}
    checker = _Checker()
    skipped = scorer.score_outcome(
        {"context": "agents", "no_apply": "the twin has no bookmarks", "expect": gold},
        PlanOutcome("plan", (step,)),
        available_cards(),
        context,
        checker,
    )
    assert checker.calls == 0
    assert skipped["unverified"] == ["not applied to the twin: the twin has no bookmarks"]
    checked = scorer.score_outcome(
        {"context": "agents", "expect": gold},
        PlanOutcome("plan", (step,)),
        available_cards(),
        context,
        checker,
    )
    assert checker.calls == 1 and checked["unverified"] == []


def test_all_live_takes_every_left_plan_case_and_every_negative(
    scorer: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def case(ident: str, kind: str = "plan") -> dict[str, Any]:
        steps = [{"op": "set_position", "slots": {"visual": "v"}}] if kind == "plan" else []
        return {
            "id": ident,
            "context": "agents",
            "message": ident,
            "expect": {"kind": kind, "steps": steps},
        }

    cases = [
        case("left1"),
        case("read1"),
        case("neg1", "not_edit"),
        case("left2"),
        case("neg2", "no_plan"),
        {**case("blocked"), "blocked_by": "x"},
    ]
    monkeypatch.setattr(
        scorer,
        "deterministic_outcome",
        lambda c: {"planner": c["id"].startswith("left") or c["id"] == "blocked"},
    )
    chosen = scorer.all_live(cases, available_cards())
    assert [c["id"] for c in chosen] == ["left1", "neg1", "left2", "neg2"]


def test_coverage_refusal_with_a_substitute_edit_fails(
    scorer: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = {
        "id": "style",
        "context": "agents",
        "message": "make the chart green",
        "expect": {"kind": "unsupported", "capability": "styling"},
    }
    monkeypatch.setattr(
        scorer,
        "deterministic_outcome",
        lambda _case: {
            "planner": False,
            "edits": ["set_position"],
            "ops": [{"op": "set_position", "visual": "v", "x": 200}],
            "kinds": ["unsupported", "apply"],
            "capabilities": ["styling"],
        },
    )
    report = scorer.evaluate_coverage([case])
    assert report["false_edits"] == 1 and not report["passed"]
    assert report["by_kind"]["unsupported"]["correct"] == 0


def test_live_aggregate_counts_unverified_plans_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    path = Path(__file__).resolve().parents[2] / "evals" / "imported_edit"
    monkeypatch.syspath_prepend(str(path))
    live = importlib.import_module("planner_live")
    scores = {
        "tools": 1,
        "tools_ok": 1,
        "slots": 1,
        "slots_ok": 1,
        "extra_steps": 0,
        "unasked": 0,
        "invalid": 0,
        "wrong_formulas": 0,
    }

    def row(ident: str, ok: bool, unverified: list[str], expected: str = "plan") -> dict[str, Any]:
        return {
            "id": ident,
            "expected": expected,
            "outcome": "plan",
            "ok": ok,
            "calls": [],
            "seconds": 1.0,
            "unverified": unverified,
            "scores": scores if expected == "plan" else {"planned_steps": 0},
        }

    summary = live._aggregate(
        [
            row("checked", True, []),
            row("bookmark", True, ["not applied to the twin: x"]),
            row("wrong", False, ["not applied to the twin: x"]),
            row("neg", True, [], "not_edit"),
        ]
    )
    assert summary["right_plans"] == 1
    assert summary["unverified"] == {
        "plan_cases": 2,
        "right_as_far_as_checked": 1,
        "ids": ["bookmark", "wrong"],
    }
