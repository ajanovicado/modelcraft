"""Grounding regressions captured in the local Qwen round-three chat evals."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.studio import edit_planner as planner
from ai_powerbi_analyst.studio.edit_context import EditContext
from ai_powerbi_analyst.studio.edit_text import find_colors

EVALS = Path(__file__).resolve().parents[2] / "evals" / "imported_edit"
spec = importlib.util.spec_from_file_location("live_regression_contexts", EVALS / "contexts.py")
assert spec is not None and spec.loader is not None
contexts = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = contexts
spec.loader.exec_module(contexts)

TABLE = "e82e02f93055d6b0b544"
SALES = "b404a461ede7604a2774"
AVERAGE = "da32cdbb0090e885649b"
CHANGE = "ab13db00c71dcec86e99"
DISCOUNT = "97d1504080b699eae7d7"


class Stub:
    def __init__(
        self,
        answer: Callable[[planner.Prompt], dict[str, Any]],
        draft: str = "1",
    ) -> None:
        self.answer = answer
        self.draft = draft
        self.planner: planner.EditPlanner | None = None
        self.draft_calls = 0

    def generate(
        self,
        _system: str,
        _user: str,
        response_model: type[Any],
        **_options: Any,
    ) -> Any:
        if issubclass(response_model, planner.DraftAnswer):
            self.draft_calls += 1
            return response_model(expression=self.draft)
        assert self.planner is not None and self.planner.prompt is not None
        return response_model.model_validate(self.answer(self.planner.prompt))


def visual(prompt: planner.Prompt, identity: str) -> int:
    return next(n for n, entry in enumerate(prompt.lists.visuals, 1) if entry.id == identity)


def field(prompt: planner.Prompt, name: str) -> int:
    return next(n for n, entry in enumerate(prompt.lists.fields, 1) if entry[2] == name)


def response(op: str, slots: Callable[[planner.Prompt], dict[str, Any]]) -> Stub:
    def answer(prompt: planner.Prompt) -> dict[str, Any]:
        number = next(n for n, card in enumerate(prompt.cards, 1) if card.op == op)
        return {"kind": "plan", "steps": [{"tool": number, "slots": slots(prompt)}]}

    return Stub(answer)


def run(
    message: str,
    op: str,
    stub: Stub,
    *,
    context: str = "agents",
    shortlist: bool = False,
) -> tuple[planner.PlanOutcome, planner.Prompt]:
    ctx: EditContext = contexts.context_for(context, {})
    cards = planner.available_cards()
    made = planner.EditPlanner(ctx, stub, cards=cards, check_dax_expression=lambda _s, _c: [])
    if not shortlist:
        chosen = next(c for c in cards if c.op == op)
        made.shortlist = lambda _pieces: [chosen]  # type: ignore[method-assign]
    stub.planner = made
    result = made.plan([message])
    assert made.prompt is not None
    return result, made.prompt


def slot_properties(prompt: planner.Prompt, op: str) -> dict[str, Any]:
    number = next(n for n, card in enumerate(prompt.cards) if card.op == op)
    return dict(
        prompt.schema["properties"]["steps"]["items"]["anyOf"][number]["properties"]["slots"][
            "properties"
        ]
    )


def test_sf02_field_removal_does_not_offer_unrelated_bookmark_movement() -> None:
    message = "take Employee Name out of the table"
    result, prompt = run(
        message,
        "set_fields",
        response(
            "set_fields",
            lambda p: {
                "visual": visual(p, TABLE),
                "fields": [field(p, "Employee Name")],
                "role": "Values",
            },
        ),
        shortlist=True,
    )
    assert [c.op for c in prompt.cards] == ["set_fields"]
    assert result.kind == "plan", result.reason
    assert result.ops[0]["mode"] == "remove"


@pytest.mark.parametrize("reverse_model_order", [False, True])
def test_a_size_step_is_grounded_in_its_own_clause_before_table_sorting(
    reverse_model_order: bool,
) -> None:
    def answer(prompt: planner.Prompt) -> dict[str, Any]:
        tools = {card.op: number for number, card in enumerate(prompt.cards, 1)}
        steps = [
            {
                "tool": tools["set_position"],
                "slots": {
                    "visual": visual(prompt, "5b0dbccd61c6c4adb415"),
                    "width": 500,
                },
            },
            {
                "tool": tools["set_sort"],
                "slots": {
                    "visual": visual(prompt, TABLE),
                    "field": field(prompt, "Rank"),
                },
            },
        ]
        return {"kind": "plan", "steps": list(reversed(steps)) if reverse_model_order else steps}

    stub = Stub(answer)
    made = planner.EditPlanner(contexts.context_for("agents"), stub)
    stub.planner = made
    outcome = made.plan(
        [
            "make the StoreName slicer 500 pixels wide.",
            "Order the table by Rank.",
        ]
    )
    assert outcome.kind == "plan", outcome.reason
    assert [op["op"] for op in outcome.ops] == ["set_position", "set_sort"]
    assert outcome.ops[0] == {"op": "set_position", "visual": "5b0dbccd61c6c4adb415", "width": 500}
    assert outcome.ops[1]["visual"] == TABLE
    assert outcome.ops[1]["field"]["name"] == "Rank"
    assert not outcome.notes


@pytest.mark.parametrize("reverse_model_order", [False, True])
def test_a_conditional_default_does_not_attach_the_step_to_a_totals_clause(
    reverse_model_order: bool,
) -> None:
    def answer(prompt: planner.Prompt) -> dict[str, Any]:
        tools = {card.op: number for number, card in enumerate(prompt.cards, 1)}
        steps = [
            {
                "tool": tools["set_conditional_format"],
                "slots": {
                    "visual": visual(prompt, TABLE),
                    "style": "data_bars",
                    "apply_to": "values",
                },
            },
            {
                "tool": tools["set_table_layout"],
                "slots": {
                    "visual": visual(prompt, TABLE),
                    "totals": True,
                },
            },
        ]
        return {"kind": "plan", "steps": list(reversed(steps)) if reverse_model_order else steps}

    stub = Stub(answer)
    cards = planner.available_cards()
    made = planner.EditPlanner(contexts.context_for("agents"), stub, cards=cards)
    made.shortlist = lambda _pieces: [  # type: ignore[method-assign]
        next(card for card in cards if card.op == operation)
        for operation in ("set_conditional_format", "set_table_layout")
    ]
    stub.planner = made
    outcome = made.plan(
        [
            "add data bars to Rank in the table",
            "turn on the totals of the table",
        ]
    )
    assert outcome.kind == "plan", outcome.reason
    assert [op["op"] for op in outcome.ops] == ["set_conditional_format", "set_table_layout"]
    assert outcome.ops[0]["field"] == "Rank"
    assert outcome.ops[0].get("apply_to", "values") == "values"
    assert outcome.ops[1]["totals"] is True


@pytest.mark.parametrize("reverse_model_order", [False, True])
@pytest.mark.parametrize(
    ("show_words", "hide_words"),
    [
        ("unhide", "hide"),
        ("show again", "hide"),
        ("make visible", "make invisible"),
    ],
)
def test_opposite_hidden_settings_keep_their_requested_targets(
    reverse_model_order: bool,
    show_words: str,
    hide_words: str,
) -> None:
    def answer(prompt: planner.Prompt) -> dict[str, Any]:
        number = next(n for n, card in enumerate(prompt.cards, 1) if card.op == "set_visual_hidden")
        steps = [
            {"tool": number, "slots": {"visual": visual(prompt, AVERAGE), "hidden": False}},
            {"tool": number, "slots": {"visual": visual(prompt, SALES), "hidden": True}},
        ]
        return {"kind": "plan", "steps": list(reversed(steps)) if reverse_model_order else steps}

    stub = Stub(answer)
    made = planner.EditPlanner(contexts.context_for("agents"), stub)
    visibility = next(card for card in made.cards if card.op == "set_visual_hidden")
    made.shortlist = lambda _pieces: [visibility]  # type: ignore[method-assign]
    stub.planner = made
    outcome = made.plan(
        [
            f"{show_words} Avg MTD Sales by Agents",
            f"{hide_words} Total MTD Sales",
        ]
    )
    assert outcome.kind == "plan", outcome.reason
    assert outcome.ops == (
        {"op": "set_visual_hidden", "visual": AVERAGE, "hidden": False},
        {"op": "set_visual_hidden", "visual": SALES, "hidden": True},
    )


@pytest.mark.parametrize("reverse_model_order", [False, True])
def test_different_heights_keep_their_requested_targets(reverse_model_order: bool) -> None:
    def answer(prompt: planner.Prompt) -> dict[str, Any]:
        number = next(n for n, card in enumerate(prompt.cards, 1) if card.op == "set_position")
        steps = [
            {"tool": number, "slots": {"visual": visual(prompt, AVERAGE), "height": 300}},
            {"tool": number, "slots": {"visual": visual(prompt, SALES), "height": 400}},
        ]
        return {"kind": "plan", "steps": list(reversed(steps)) if reverse_model_order else steps}

    stub = Stub(answer)
    made = planner.EditPlanner(contexts.context_for("agents"), stub)
    position = next(card for card in made.cards if card.op == "set_position")
    made.shortlist = lambda _pieces: [position]  # type: ignore[method-assign]
    stub.planner = made
    outcome = made.plan(
        [
            "make Avg MTD Sales by Agents 300 pixels high",
            "make Total MTD Sales 400 pixels high",
        ]
    )
    assert outcome.kind == "plan", outcome.reason
    assert outcome.ops == (
        {"op": "set_position", "visual": AVERAGE, "height": 300},
        {"op": "set_position", "visual": SALES, "height": 400},
    )


@pytest.mark.parametrize("reverse_model_order", [False, True])
def test_two_target_only_deletions_keep_the_requested_targets(reverse_model_order: bool) -> None:
    def answer(prompt: planner.Prompt) -> dict[str, Any]:
        number = next(n for n, card in enumerate(prompt.cards, 1) if card.op == "delete_visual")
        steps = [
            {"tool": number, "slots": {"visual": visual(prompt, AVERAGE)}},
            {"tool": number, "slots": {"visual": visual(prompt, SALES)}},
        ]
        return {"kind": "plan", "steps": list(reversed(steps)) if reverse_model_order else steps}

    stub = Stub(answer)
    made = planner.EditPlanner(contexts.context_for("agents"), stub)
    delete = next(card for card in made.cards if card.op == "delete_visual")
    made.shortlist = lambda _pieces: [delete]  # type: ignore[method-assign]
    stub.planner = made
    outcome = made.plan(
        [
            "delete the Avg MTD Sales by Agents chart",
            "delete the Total MTD Sales chart",
        ]
    )
    assert outcome.kind == "plan", outcome.reason
    assert outcome.ops == (
        {"op": "delete_visual", "visual": AVERAGE},
        {"op": "delete_visual", "visual": SALES},
    )


@pytest.mark.parametrize("model_gives_icon_settings", [False, True])
def test_lv14_colored_arrows_recover_omitted_style_and_explicit_optional_settings(
    model_gives_icon_settings: bool,
) -> None:
    message = "Show three colored arrows next to vs PM % values, on the right."

    def slots(prompt: planner.Prompt) -> dict[str, Any]:
        values: dict[str, Any] = {"visual": visual(prompt, TABLE), "field": "vs PM %"}
        if model_gives_icon_settings:
            values.update(icon_set="ThreeColoredArrows", icon_position="right")
        return values

    result, prompt = run(
        message,
        "set_conditional_format",
        response("set_conditional_format", slots),
    )
    assert result.kind == "plan", result.reason
    assert result.ops == (
        {
            "op": "set_conditional_format",
            "visual": TABLE,
            "field": "vs PM %",
            "style": "icons",
            "icon_set": "ThreeColoredArrows",
            "icon_position": "right",
        },
    )
    offered = slot_properties(prompt, "set_conditional_format")
    assert not {"target", "min_color", "max_color", "mid_color", "bar_only"} & offered.keys()


def test_a_chart_calculation_cannot_take_a_field_alias_from_an_unrelated_table() -> None:
    from ai_powerbi_analyst.studio import report_edit_extras as extras
    from ai_powerbi_analyst.studio.pbir_document import PbirDocument
    from ai_powerbi_analyst.studio.pbix_package import PbixPackage
    from ai_powerbi_analyst.studio.report_edit_context import ModelInfo
    from ai_powerbi_analyst.studio.report_edit_geometry import resolve_leaf

    message = "add a 3-point moving average to the Total MTD Sales chart"
    card = next(c for c in extras.TOOL_CARDS if c.op == "set_visual_calculation")
    expected = next(op for request, op in card.examples if request == message)
    context = contexts.context_for("agents")
    filled = planner.fill_from_words(card, message, context)
    assert filled is not None
    operation = card.to_operation(filled)
    assert operation.get("field") != "MTD Sales"  # this alias belongs to the table
    model = ModelInfo.from_report(
        [
            {"TableName": table, "ColumnName": name}
            for table, names in context.tables.items()
            for name in names
        ],
        [{"TableName": measure.table, "Name": measure.name} for measure in context.measures],
    )

    def native_hash(op: dict[str, Any]) -> str:
        fixture = EVALS.parents[1] / "fixtures/imported/structure-twin.pbix"
        document = PbirDocument(PbixPackage(fixture))
        extras.apply_extras(document, extras.parse_extras([op]), model)
        _page, visual = resolve_leaf(document, SALES)
        calculations = [
            projection
            for _role, projection in extras._role_projections(visual)
            if extras._calc_of(projection) is not None
        ]
        assert len(calculations) == 1
        return hashlib.sha256(json.dumps(calculations, sort_keys=True).encode()).hexdigest()

    assert native_hash(operation) == native_hash(expected)


def test_icon_position_recovery_does_not_choose_an_unasked_icon_set() -> None:
    result, _prompt = run(
        "show icons next to vs PM % values on the right",
        "set_conditional_format",
        response(
            "set_conditional_format",
            lambda p: {
                "visual": visual(p, TABLE),
                "field": "vs PM %",
                "style": "icons",
            },
        ),
    )
    assert result.kind == "plan", result.reason
    assert result.ops == (
        {
            "op": "set_conditional_format",
            "visual": TABLE,
            "field": "vs PM %",
            "style": "icons",
            "icon_position": "right",
        },
    )


def test_fl10_missing_online_exclusion_value_is_recovered() -> None:
    result, _prompt = run(
        "leave the Online channel out of every page",
        "set_report_filter",
        response(
            "set_report_filter", lambda p: {"field": field(p, "ChannelName"), "exclude": True}
        ),
    )
    assert result.kind == "plan", result.reason
    assert result.ops[0]["filter"]["values"] == ["Online"]
    assert result.ops[0]["filter"]["exclude"] is True


def test_nv02_back_button_cannot_keep_a_destination_page() -> None:
    result, prompt = run(
        "add a back button to the page",
        "add_button",
        response(
            "add_button",
            lambda _p: {
                "kind": "back",
                "action": "back",
                "target_page": 1,
                "target_bookmark": "1",
                "url": "V",
            },
        ),
    )
    assert "target_page" not in slot_properties(prompt, "add_button")
    assert "target_bookmark" not in slot_properties(prompt, "add_button")
    assert "url" not in slot_properties(prompt, "add_button")
    assert result.kind == "plan", result.reason
    assert result.ops[0]["action"] == {"type": "back"}


def test_dl02_chart_bar_label_units_use_visual_number_formatting() -> None:
    message = "label the bars of Total MTD Sales in millions"
    stub = response(
        "set_number_format", lambda p: {"visual": visual(p, SALES), "display_units": "millions"}
    )
    outcome, prompt = run(message, "set_number_format", stub, shortlist=True)
    assert outcome.kind == "plan" and len(outcome.steps) == 1
    operation = outcome.steps[0].operation
    assert operation["op"] == "set_number_format" and operation["visual"] == SALES
    assert operation["display_units"] == "millions" and "show" not in operation
    assert "show" not in slot_properties(prompt, "set_number_format")


def test_ax01_chart_grid_lines_are_refused_without_a_substitute_edit() -> None:
    from ai_powerbi_analyst.studio.edit_intents import parse_edit_message
    from ai_powerbi_analyst.studio.edit_types import ParseKind

    message = "I want horizontal grid lines on the Avg MTD Sales by Agents chart"
    outcome = parse_edit_message(message, contexts.context_for("agents"))
    assert not outcome.unparsed and len(outcome.clauses) == 1
    clause = outcome.clauses[0]
    assert clause.kind is ParseKind.UNSUPPORTED and clause.capability == "styling"
    assert not clause.ops


@pytest.mark.parametrize(
    "message",
    [
        "rotate the axis labels",
        "tilt the axis labels by 45 degrees",
        "angle the x axis labels to 30 degrees",
        "rotate the category labels by 90 degrees",
    ],
)
def test_axis_label_rotation_uses_the_short_styling_refusal(message: str) -> None:
    from ai_powerbi_analyst.studio.edit_intents import parse_edit_message
    from ai_powerbi_analyst.studio.edit_types import ParseKind

    outcome = parse_edit_message(message, contexts.context_for("agents", {"selection": SALES[:8]}))
    assert not outcome.unparsed and len(outcome.clauses) == 1
    clause = outcome.clauses[0]
    assert clause.kind is ParseKind.UNSUPPORTED and clause.capability == "styling"
    assert not clause.ops
    assert clause.reason == (
        "Studio doesn't change styling such as colours, fonts or titles; do it in Power BI "
        "Desktop's Format pane."
    )


@pytest.mark.parametrize(
    ("message", "units"),
    [
        ("show values in millions", "millions"),
        ("display the values in thousands", "thousands"),
        ("put the values in billions", "billions"),
    ],
)
def test_value_unit_requests_format_only_the_selected_visual(message: str, units: str) -> None:
    from ai_powerbi_analyst.studio.edit_intents import parse_edit_message
    from ai_powerbi_analyst.studio.edit_types import ParseKind

    outcome = parse_edit_message(message, contexts.context_for("agents", {"selection": SALES[:8]}))
    assert not outcome.unparsed and len(outcome.clauses) == 1
    assert outcome.clauses[0].kind is ParseKind.APPLY
    assert outcome.ops == [
        {
            "op": "set_number_format",
            "visual": SALES,
            "preset": units,
            "display_units": units,
        }
    ]


def test_sw03_wrong_first_model_choice_is_replaced_by_the_other_named_chart() -> None:
    message = "trade places between Avg MTD % change by Agents and Avg Discount per Transaction"
    stub = response(
        "swap_visuals", lambda p: {"first": visual(p, SALES), "second": visual(p, DISCOUNT)}
    )
    outcome, _prompt = run(message, "swap_visuals", stub)
    assert outcome.kind == "plan" and len(outcome.steps) == 1
    operation = outcome.steps[0].operation
    assert {operation["a"], operation["b"]} == {CHANGE, DISCOUNT}


def test_fl01_omitted_categorical_value_is_taken_from_store_channel() -> None:
    message = "limit the Avg MTD Sales by Agents chart to the Store channel"
    stub = response(
        "set_visual_filter",
        lambda p: {"visual": visual(p, AVERAGE), "field": field(p, "ChannelName")},
    )
    outcome, _prompt = run(message, "set_visual_filter", stub)
    assert outcome.kind == "plan" and len(outcome.steps) == 1
    operation = outcome.steps[0].operation
    assert operation["visual"] == AVERAGE
    assert operation["filter"]["kind"] == "categorical"
    assert operation["filter"]["field"]["name"] == "ChannelName"
    assert operation["filter"]["values"] == ["Store"]


def test_cf02_omitted_zero_threshold_is_recovered_and_invented_black_is_discarded() -> None:
    message = "color the vs Previous Month values red when they are below 0"
    stub = response(
        "set_conditional_format",
        lambda p: {
            "visual": visual(p, TABLE),
            "field": "vs Previous Month",
            "style": "rules",
            "below_color": "red",
            "above_color": "black",
        },
    )
    outcome, _prompt = run(message, "set_conditional_format", stub)
    assert outcome.kind == "plan" and len(outcome.steps) == 1
    operation = outcome.steps[0].operation
    assert operation["field"] == "vs Previous Month" and operation["style"] == "rules"
    assert operation["rules"][0]["max"] == 0
    assert operation["rules"][0]["color"] == find_colors("red")[0].hex
    assert all(rule["color"] != find_colors("black")[0].hex for rule in operation["rules"])


def test_nv12_omitted_navigator_kind_is_bookmark_instead_of_page_default() -> None:
    message = "add a bookmark navigator on the Forecast page"
    stub = response(
        "add_navigator",
        lambda p: {
            "page": next(n for n, page in enumerate(p.lists.pages, 1) if page.name == "Forecast"),
        },
    )
    outcome, _prompt = run(message, "add_navigator", stub, context="multipage")
    assert outcome.kind == "plan" and len(outcome.steps) == 1
    assert outcome.steps[0].operation["page"] == "page4"
    assert outcome.steps[0].operation["kind"] == "bookmark"


def test_ex09_calculation_schema_uses_visible_field_without_invented_names() -> None:
    message = "add a running total of MTD Sales as a visual calculation to the table"
    stub = response(
        "set_visual_calculation",
        lambda p: {
            "visual": visual(p, TABLE),
            "template": "running_sum",
            "field": "MTD Sales",
            "name": "MTD Sales Running Total",
            "new_name": "MTD Sales Running Total",
        },
    )
    outcome, prompt = run(message, "set_visual_calculation", stub)
    properties = slot_properties(prompt, "set_visual_calculation")
    assert not {"name", "new_name"} & properties.keys()
    assert properties["field"]["enum"] == ["MTD Sales"]
    assert outcome.kind == "plan" and len(outcome.steps) == 1
    operation = outcome.steps[0].operation
    assert operation["template"] == "running_sum" and operation["field"] == "MTD Sales"
    assert "name" not in operation and "new_name" not in operation


def test_ex09_an_unrequested_model_column_cannot_be_applied() -> None:
    message = "add a running total of MTD Sales as a visual calculation to the table"
    stub = response(
        "set_visual_calculation",
        lambda p: {
            "visual": visual(p, TABLE),
            "template": "running_sum",
            "field": "FactSales[SalesAmount]",
            "name": "MTD Sales Running Total",
            "new_name": "MTD Sales Running Total",
        },
    )
    outcome, _prompt = run(message, "set_visual_calculation", stub)
    assert all(step.operation.get("field") == "MTD Sales" for step in outcome.steps)
    assert all(
        "name" not in step.operation and "new_name" not in step.operation for step in outcome.steps
    )


@pytest.mark.parametrize("op", ["create_calculated_column", "update_measure"])
def test_md01_ms07_formula_requests_do_not_offer_or_keep_unrelated_format_string(op: str) -> None:
    # Fixed synthetic fixture formulas. Body comparisons use hashes so failures show no code.
    expression = 'DimEmployee[FirstName] & " " & DimEmployee[LastName]'
    if op == "create_calculated_column":
        message = "create a calculated column Full Name = " + expression
        stub = response(
            op,
            lambda _p: {
                "name": "Full Name",
                "format_string": "[formula kept locally]",
            },
        )
    else:
        message = "change the formula of Profit so it also subtracts the discount"
        expression = "SUM(FactSales[SalesAmount]) - SUM(FactSales[DiscountAmount])"
        stub = response(op, lambda p: {"name": field(p, "Profit"), "format_string": "Unexpected"})
        stub.draft = expression
    outcome, prompt = run(message, op, stub)
    assert "format_string" not in slot_properties(prompt, op)
    assert outcome.kind == "plan" and len(outcome.steps) == 1
    operation = outcome.steps[0].operation
    assert "format_string" not in operation
    assert (
        hashlib.sha256(operation["expression"].encode()).hexdigest()
        == hashlib.sha256(expression.encode()).hexdigest()
    )
    if op == "create_calculated_column":
        assert operation["table"] == "DimEmployee" and operation["name"] == "Full Name"
        assert stub.draft_calls == 0
    else:
        assert stub.draft_calls == 1


# --- review round: every value backed by the words or the selection, else asked ------------

MULTI_FORECAST = "page4"
MULTI_SUMMARY = "page0"
MULTI_MIX = "page2"
CHANNEL = "b85aa81a8b5c60030d13"


def plan_with(
    message: str | list[str],
    op: str,
    slots: Callable[[planner.Prompt], dict[str, Any]],
    *,
    context: str = "agents",
    options: dict[str, Any] | None = None,
    draft: str = "1",
    shortlist: bool = False,
) -> tuple[planner.PlanOutcome, planner.Prompt, Stub]:
    """``run`` with context options (a selection) and the stub kept for its draft calls."""
    ctx: EditContext = contexts.context_for(context, dict(options or {}))
    cards = planner.available_cards()
    stub = response(op, slots)
    stub.draft = draft
    made = planner.EditPlanner(ctx, stub, cards=cards, check_dax_expression=_blank_is_wrong)
    if not shortlist:
        chosen = next(c for c in cards if c.op == op)
        made.shortlist = lambda _pieces: [chosen]  # type: ignore[method-assign]
    stub.planner = made
    outcome = made.plan(message if isinstance(message, list) else [message])
    assert made.prompt is not None
    return outcome, made.prompt, stub


def _blank_is_wrong(expression: str, _context: EditContext) -> list[str]:
    return [] if expression.strip() else ["The formula is empty."]


def page_number(prompt: planner.Prompt, page_id: str) -> int:
    return next(n for n, page in enumerate(prompt.lists.pages, 1) if page.id == page_id)


# 1. a visual calculation is never swapped onto the chart's own value -------------------------

VISUAL_CALCULATION = next(c for c in planner.available_cards() if c.op == "set_visual_calculation")


@pytest.mark.parametrize(
    "message",
    [
        "add a running total of discounts to the Total MTD Sales chart",
        "add a moving average of the discount to the Total MTD Sales chart",
        "put a running sum of costs on the Total MTD Sales chart",
        "add a running total of MTD Sales to the Avg MTD Sales by Agents chart",
        "give the Total MTD Sales chart a running total of the orders",
    ],
)
def test_a_calculation_of_a_field_the_chart_lacks_is_asked_never_applied(message: str) -> None:
    context = contexts.context_for("agents")
    assert planner.fill_from_words(VISUAL_CALCULATION, message, context) is None
    assert planner.card_clause(message, context) is None  # nothing applies without a model
    target = AVERAGE if "Avg MTD Sales by Agents" in message else SALES
    template = "moving_average" if "moving" in message else "running_sum"
    outcome, _prompt, _stub = plan_with(
        message,
        "set_visual_calculation",
        lambda p: {
            "visual": visual(p, target),
            "template": template,
        },
    )
    assert outcome.kind == "clarify" and not outcome.ops
    assert outcome.clause is not None
    # the question offers only the fields the chart shows, each for the user to pick
    for choice in outcome.clause.choices:
        assert choice.ops[0]["visual"] == target


@pytest.mark.parametrize(
    "message",
    [
        "add a running total to the Total MTD Sales chart",
        "add a 3-point moving average to the Total MTD Sales chart",
        "give the Total MTD Sales chart a running total of its values",
    ],
)
def test_a_calculation_on_a_named_chart_alone_takes_the_charts_one_value(message: str) -> None:
    filled = planner.fill_from_words(VISUAL_CALCULATION, message, contexts.context_for("agents"))
    assert filled is not None and filled["visual"] == SALES
    assert filled["field"] == "TopN/BottomN/ MTD Sales"


# 2. a filter's values are the categories said, never other words -----------------------------


@pytest.mark.parametrize(
    ("message", "values", "exclude"),
    [
        ("restrict the Avg MTD Sales by Agents chart to just the Online channel", ["Online"], None),
        ("set the Avg MTD Sales by Agents chart to exclude the Online channel", ["Online"], True),
        (
            "limit the Avg MTD Sales by Agents chart to the Store and Online channels",
            ["Store", "Online"],
            None,
        ),
        ("Filter Online channel only on the Avg MTD Sales by Agents chart", ["Online"], None),
        ("limit the Avg MTD Sales by Agents chart to the Store channel", ["Store"], None),
    ],
)
def test_omitted_filter_values_are_the_categories_said(
    message: str, values: list[str], exclude: bool | None
) -> None:
    outcome, _prompt, _stub = plan_with(
        message,
        "set_visual_filter",
        lambda p: {
            "visual": visual(p, AVERAGE),
            "field": field(p, "ChannelName"),
            **({"exclude": True} if exclude else {}),
        },
    )
    assert outcome.kind == "plan", outcome.reason
    assert outcome.ops[0]["filter"]["values"] == values
    assert outcome.ops[0]["filter"].get("exclude") is exclude


def test_two_named_categories_left_out_of_every_page_are_both_kept() -> None:
    outcome, _prompt, _stub = plan_with(
        "leave the Store channel and the Online channel out of every page",
        "set_report_filter",
        lambda p: {"field": field(p, "ChannelName"), "exclude": True},
    )
    assert outcome.kind == "plan", outcome.reason
    assert outcome.ops[0]["filter"]["values"] == ["Store", "Online"]


@pytest.mark.parametrize(
    ("message", "op"),
    [
        ("limit the Avg MTD Sales by Agents chart to our biggest channel", "set_visual_filter"),
        (
            "limit the Avg MTD Sales by Agents chart to the best-selling channel",
            "set_visual_filter",
        ),
        (
            "limit the Avg MTD Sales by Agents chart to the channel with the most sales",
            "set_visual_filter",
        ),
        ("filter the Avg MTD Sales by Agents chart by channel", "set_visual_filter"),
        (
            "limit the Avg MTD Sales by Agents chart to Rank and the Online channel",
            "set_visual_filter",
        ),
        ("keep the channel filter on every page", "set_report_filter"),
        ("filter every page to the channel", "set_report_filter"),
    ],
)
def test_a_filter_with_no_value_said_asks_for_the_values(message: str, op: str) -> None:
    def slots(p: planner.Prompt) -> dict[str, Any]:
        given: dict[str, Any] = {"field": field(p, "ChannelName")}
        if op == "set_visual_filter":
            given["visual"] = visual(p, AVERAGE)
        return given

    outcome, _prompt, _stub = plan_with(message, op, slots)
    assert outcome.kind == "clarify" and not outcome.ops
    assert outcome.clause is not None and "values should the filter" in outcome.clause.question


# 3. an optional list of visuals never takes the selection on its own -------------------------


@pytest.mark.parametrize(
    "message",
    [
        "add a bookmark called Q1 view",
        "save a bookmark named Q1 view of the page",
        "bookmark the page as Q1 view",
    ],
)
@pytest.mark.parametrize("model_hides_it", [False, True])
def test_a_bookmark_does_not_hide_or_narrow_to_the_selected_visual(
    message: str, model_hides_it: bool
) -> None:
    def slots(p: planner.Prompt) -> dict[str, Any]:
        given: dict[str, Any] = {"name": "Q1 view"}
        if model_hides_it:
            given.update(visuals=[visual(p, SALES)], hide=[visual(p, SALES)])
        return given

    outcome, _prompt, _stub = plan_with(
        message, "add_bookmark", slots, options={"selection": SALES[:8]}
    )
    assert outcome.kind == "plan", outcome.reason
    assert "hide" not in outcome.ops[0] and "visuals" not in outcome.ops[0]


def test_a_bookmark_hides_only_the_visual_the_words_hide() -> None:
    outcome, _prompt, _stub = plan_with(
        "add a bookmark called Q1 view that hides the Total MTD Sales chart",
        "add_bookmark",
        lambda _p: {"name": "Q1 view"},
        options={"selection": TABLE[:8]},
    )
    assert outcome.kind == "plan", outcome.reason
    assert outcome.ops[0]["hide"] == [SALES]
    assert "visuals" not in outcome.ops[0]


@pytest.mark.parametrize(
    ("message", "op", "slots"),
    [
        ("set the tab order of this page by position", "set_tab_order", {"by_position": True}),
        ("clear the mobile layout of this page", "remove_from_mobile", {}),
        ("order the tabs on this page top to bottom", "set_tab_order", {"by_position": True}),
    ],
)
def test_page_wide_lists_are_not_narrowed_to_the_selection(
    message: str, op: str, slots: dict[str, Any]
) -> None:
    outcome, _prompt, _stub = plan_with(
        message, op, lambda _p: dict(slots), options={"selection": SALES[:8]}
    )
    assert all("visuals" not in o for o in outcome.ops)


def test_interaction_targets_are_recovered_from_the_named_visuals_only() -> None:
    outcome, _prompt, _stub = plan_with(
        "stop the Country slicer from filtering Total MTD Sales",
        "set_interaction",
        lambda p: {"source": visual(p, "54be5cf0783e66471540"), "type": "none"},
        options={"selection": TABLE[:8]},
    )
    assert outcome.kind == "plan", outcome.reason
    assert outcome.ops[0]["targets"] == [SALES]


# 4. a button goes on the page its words put it on ------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "add a back button to the Forecast page",
        "put a back button onto Forecast",
        "add a back button on the Forecast page",
        "place a back button in Forecast",
    ],
)
def test_a_back_button_goes_on_the_page_named_for_it(message: str) -> None:
    outcome, prompt, _stub = plan_with(
        message,
        "add_button",
        lambda p: {
            "page": page_number(p, MULTI_FORECAST),
            "kind": "back",
        },
        context="multipage",
    )
    assert outcome.kind == "plan", outcome.reason
    assert outcome.ops[0]["page"] == MULTI_FORECAST
    assert "target_page" not in slot_properties(prompt, "add_button")


@pytest.mark.parametrize(
    "message",
    [
        "add a button to the Forecast page that goes to Product Mix",
        "add a button on Forecast that takes you to Product Mix",
        "put a button onto the Forecast page that links to the Product Mix page",
    ],
)
def test_a_button_is_placed_and_pointed_by_their_own_words(message: str) -> None:
    outcome, _prompt, _stub = plan_with(
        message,
        "add_button",
        lambda p: {
            "page": page_number(p, MULTI_FORECAST),
            "target_page": page_number(p, MULTI_MIX),
        },
        context="multipage",
    )
    assert outcome.kind == "plan", outcome.reason
    assert outcome.ops[0]["page"] == MULTI_FORECAST
    assert outcome.ops[0]["action"] == {"type": "page", "page": MULTI_MIX}


def test_a_button_that_goes_to_a_page_is_placed_on_the_page_on_screen() -> None:
    outcome, _prompt, _stub = plan_with(
        "add a button that goes to the Forecast page",
        "add_button",
        lambda p: {
            "page": page_number(p, MULTI_FORECAST),
            "action": "page",
            "target_page": page_number(p, MULTI_FORECAST),
        },
        context="multipage",
    )
    assert outcome.kind == "plan", outcome.reason
    assert outcome.ops[0]["page"] == MULTI_SUMMARY
    assert outcome.ops[0]["action"] == {"type": "page", "page": MULTI_FORECAST}


def test_a_button_to_a_page_that_could_be_either_is_not_both() -> None:
    # "add a button to Forecast": on Forecast, or going to it? Never both on a guess.
    outcome, _prompt, _stub = plan_with(
        "add a button to Forecast",
        "add_button",
        lambda p: {
            "page": page_number(p, MULTI_FORECAST),
            "action": "page",
            "target_page": page_number(p, MULTI_FORECAST),
        },
        context="multipage",
    )
    assert not any(
        o.get("page") == MULTI_FORECAST and o.get("action", {}).get("page") == MULTI_FORECAST
        for o in outcome.ops
    )


# 5. icons: the style, set and place the words give -----------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Show 3 colored arrows next to vs PM % values, on the right.",
        "show three colored arrows on vs PM %, on the right",
        "put 3 coloured arrows on the right of the vs PM % values",
    ],
)
def test_a_counted_icon_set_is_the_icons_style_in_any_spelling(message: str) -> None:
    outcome, _prompt, _stub = plan_with(
        message,
        "set_conditional_format",
        lambda p: {
            "visual": visual(p, TABLE),
            "field": "vs PM %",
        },
    )
    assert outcome.kind == "plan", outcome.reason
    operation = outcome.ops[0]
    assert operation["style"] == "icons" and operation["icon_set"] == "ThreeColoredArrows"
    assert operation["icon_position"] == "right"


@pytest.mark.parametrize(
    ("message", "choices"),
    [
        ("Show colored arrows next to vs PM % values", 3),
        ("add coloured arrows to the vs PM % column of the table", 3),
        ("show arrow icons next to vs PM % values", 6),
    ],
)
def test_an_icon_kind_without_its_count_asks_which_set(message: str, choices: int) -> None:
    outcome, _prompt, _stub = plan_with(
        message,
        "set_conditional_format",
        lambda p: {
            "visual": visual(p, TABLE),
            "field": "vs PM %",
        },
    )
    assert outcome.kind == "clarify" and outcome.clause is not None
    assert len(outcome.clause.choices) == choices
    assert all("Arrows" in choice.label for choice in outcome.clause.choices)


@pytest.mark.parametrize(
    "message",
    [
        "Show three colored arrows in the vs PM % column of the table on the left side of the page",
        "Put three colored arrows on vs PM %, with the right icons",
    ],
)
@pytest.mark.parametrize("model_gives_style", [False, True])
def test_conditions_and_other_places_do_not_style_or_place_the_icons(
    message: str, model_gives_style: bool
) -> None:
    outcome, _prompt, _stub = plan_with(
        message,
        "set_conditional_format",
        lambda p: {
            "visual": visual(p, TABLE),
            "field": "vs PM %",
            **({"style": "icons"} if model_gives_style else {}),
        },
    )
    assert outcome.kind == "plan", outcome.reason
    operation = outcome.ops[0]
    assert operation["style"] == "icons" and operation["icon_set"] == "ThreeColoredArrows"
    assert "icon_alignment" not in operation and "icon_position" not in operation
    assert "rules" not in operation


@pytest.mark.parametrize(
    "message",
    [
        "Show three colored arrows on vs PM % values below 0",
        "Show three colored arrows on vs PM % when it is above 5%",
        "show three colored arrows on vs PM % values under the target",
    ],
)
@pytest.mark.parametrize("model_gives_style", [False, True])
def test_icon_conditions_require_ranges_instead_of_a_whole_range_preset(
    message: str,
    model_gives_style: bool,
) -> None:
    outcome, _prompt, _stub = plan_with(
        message,
        "set_conditional_format",
        lambda p: {
            "visual": visual(p, TABLE),
            "field": "vs PM %",
            **({"style": "icons"} if model_gives_style else {}),
        },
    )
    assert outcome.kind == "clarify" and outcome.clause is not None
    assert not outcome.ops
    assert "condition" in outcome.clause.question or "bounds" in outcome.clause.question


@pytest.mark.parametrize(
    "message",
    [
        "remove the three colored arrows from vs PM % values",
        "take the three colored arrows off vs PM % in the table",
        "don't show three colored arrows on vs PM %",
        "no more colored arrows on vs PM % please",
    ],
)
def test_removing_or_refusing_arrows_never_adds_them(message: str) -> None:
    outcome, _prompt, _stub = plan_with(
        message,
        "set_conditional_format",
        lambda p: {
            "visual": visual(p, TABLE),
            "field": "vs PM %",
            "style": "icons",
            "icon_set": "ThreeColoredArrows",
        },
    )
    assert outcome.kind != "plan" and not outcome.ops


# 5b. a rule writes only the colors said -----------------------------------------------------


@pytest.mark.parametrize(
    ("message", "rules"),
    [
        ("color the vs Previous Month values red when they are below 0", [("max", "red")]),
        ("make the vs PM % values green when they are above 0", [("min", "green")]),
        (
            "color vs Previous Month red below 0 and green from 0 up",
            [("max", "red"), ("min", "green")],
        ),
    ],
)
def test_a_rule_writes_only_the_colors_the_request_names(
    message: str, rules: list[tuple[str, str]]
) -> None:
    name = "vs PM %" if "vs PM %" in message else "vs Previous Month"
    given: dict[str, Any] = {"field": name, "style": "rules", "threshold": 0}
    for side, color in rules:
        given["below_color" if side == "max" else "above_color"] = color
    outcome, _prompt, _stub = plan_with(
        message,
        "set_conditional_format",
        lambda p: {
            **given,
            "visual": visual(p, TABLE),
        },
    )
    assert outcome.kind == "plan", outcome.reason
    written = outcome.ops[0]["rules"]
    assert [(next(k for k in ("max", "min") if k in r), r["color"]) for r in written] == [
        (side, find_colors(color)[0].hex) for side, color in rules
    ]


def test_a_rule_with_no_color_said_asks_for_it() -> None:
    outcome, _prompt, _stub = plan_with(
        "use a rule on vs PM % at 0",
        "set_conditional_format",
        lambda p: {
            "visual": visual(p, TABLE),
            "field": "vs PM %",
            "style": "rules",
            "threshold": 0,
        },
    )
    assert outcome.kind == "clarify" and not outcome.ops


# 6. a new measure is drafted only from a stated calculation ---------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Create a measure called Sales YTD that I can use later",
        "add a measure named Board Number",
        "make a measure called Sales YTD which is important",
    ],
)
def test_the_planner_asks_what_a_named_only_measure_calculates(message: str) -> None:
    name = "Board Number" if "Board" in message else "Sales YTD"
    outcome, _prompt, stub = plan_with(message, "create_measure", lambda _p: {"name": name})
    assert outcome.kind == "clarify" and not outcome.ops
    assert stub.draft_calls == 0


# 7. a field a visual shows does not name it against the words ------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "add a running total of MTD Sales to the chart",
        "put a running total of MTD Sales on the chart",
        "the chart should get a running total of MTD Sales",
    ],
)
def test_the_chart_is_not_the_table_that_shows_the_field(message: str) -> None:
    context = contexts.context_for("agents")
    assert TABLE not in {e.id for e in planner.named_visuals(message, context)}
    outcome, _prompt, _stub = plan_with(
        message,
        "set_visual_calculation",
        lambda p: {
            "visual": visual(p, SALES),
            "template": "running_sum",
        },
    )
    assert all(o.get("visual") != TABLE for o in outcome.ops)
    assert outcome.kind == "clarify"


@pytest.mark.parametrize(
    "message",
    [
        "add a running total of MTD Sales to the table",
        "add data bars to MTD Sales",
        "add a running total of MTD Sales as a visual calculation to the table",
    ],
)
def test_a_field_still_names_the_visual_that_shows_it_when_the_words_agree(message: str) -> None:
    context = contexts.context_for("agents")
    assert TABLE in {e.id for e in planner.named_visuals(message, context)}


# 8. sync: every page, hidden pages, and a question when no page can be named -----------------


@pytest.mark.parametrize(
    "message",
    [
        "remove every filter from the table",
        "clear all filters from the table",
        "remove each filter from the table",
    ],
)
@pytest.mark.parametrize("selection", [None, CHANNEL])
def test_a_filter_quantifier_cannot_broaden_the_named_table_target(
    message: str,
    selection: str | None,
) -> None:
    context = contexts.context_for("agents", {"selection": selection})
    assert planner.gather_evidence([message], context).visuals == frozenset({TABLE})
    outcome, prompt, _stub = plan_with(
        message,
        "clear_filter",
        lambda p: {
            "scope": "visual",
            "visual": visual(p, CHANNEL),
        },
        options={"selection": selection},
    )
    assert outcome.kind == "plan", outcome.reason
    assert outcome.ops == ({"op": "clear_filter", "scope": "visual", "target": TABLE},)
    assert slot_properties(prompt, "clear_filter")["visual"]["enum"] == [visual(prompt, TABLE)]


@pytest.mark.parametrize("noun", ["slicer", "visual", "visualization", "item"])
def test_an_explicit_visual_quantifier_still_names_every_requested_visual(noun: str) -> None:
    context = contexts.context_for("agents")
    message = f"stop every {noun} from filtering the table"
    assert len(context.pages) == 1
    targets = {
        entry.id
        for entry in context.visuals
        if entry.page_id == context.pages[0].id
        and (noun != "slicer" or "slicer" in entry.visual_type.casefold())
    }
    assert planner.gather_evidence([message], context).visuals == frozenset(targets | {TABLE})


@pytest.mark.parametrize(
    "message",
    [
        "Sync the Channel slicer to every page",
        "Sync the Channel slicer across all the pages",
        "sync the Channel slicer everywhere",
        "sync the Channel slicer to each of the other pages",
    ],
)
def test_every_page_is_every_other_page(message: str) -> None:
    outcome, _prompt, _stub = plan_with(
        message,
        "sync_slicer",
        lambda p: {"visual": visual(p, CHANNEL)},
        context="agents-sync-qa",
    )
    assert outcome.kind == "plan", outcome.reason
    assert outcome.ops == ({"op": "sync_slicer", "visual": CHANNEL, "pages": ["Sync QA"]},)


@pytest.mark.parametrize(
    "message",
    [
        "Sync the Channel slicer to Sync QA but hide it there",
        "sync the Channel slicer to Sync QA but keep it hidden there",
        "sync the Channel slicer to Sync QA, hidden on Sync QA",
    ],
)
def test_a_synced_slicer_hidden_there_is_hidden_on_those_pages(message: str) -> None:
    outcome, _prompt, _stub = plan_with(
        message,
        "sync_slicer",
        lambda p: {"visual": visual(p, CHANNEL)},
        context="agents-sync-qa",
    )
    assert outcome.kind == "plan", outcome.reason
    assert outcome.ops == ({"op": "sync_slicer", "visual": CHANNEL, "hidden_on": ["Sync QA"]},)


def test_a_synced_slicer_the_words_do_not_hide_stays_visible() -> None:
    outcome, _prompt, _stub = plan_with(
        "sync the Channel slicer to Sync QA but don't hide it",
        "sync_slicer",
        lambda p: {"visual": visual(p, CHANNEL)},
        context="agents-sync-qa",
    )
    assert outcome.kind == "plan", outcome.reason
    assert outcome.ops == ({"op": "sync_slicer", "visual": CHANNEL, "pages": ["Sync QA"]},)


@pytest.mark.parametrize(
    "message",
    [
        "sync the ChannelName slicer across all pages",
        "sync the Channel slicer",
        "keep the Channel slicer in sync",
    ],
)
def test_a_sync_with_no_other_page_asks(message: str) -> None:
    outcome, _prompt, _stub = plan_with(
        message,
        "sync_slicer",
        lambda p: {"visual": visual(p, CHANNEL)},
    )
    assert outcome.kind == "clarify" and not outcome.ops


@pytest.mark.parametrize(
    "message",
    [
        "sync the ChannelName slicer across all pages",
        "sync the Channel slicer",
        "keep the Channel slicer in sync",
    ],
)
def test_an_invented_sync_destination_cannot_prevent_the_single_page_question(
    message: str,
) -> None:
    outcome, _prompt, _stub = plan_with(
        message,
        "sync_slicer",
        lambda p: {
            "visual": visual(p, CHANNEL),
            "pages": "Invented by the model",
        },
    )
    assert outcome.kind == "clarify" and not outcome.ops, outcome.reason
    assert outcome.clause is not None
    assert "no other page" in outcome.clause.question
    assert not outcome.clause.choices


@pytest.mark.parametrize(
    "message",
    [
        "sync the ChannelName slicer",
        "sync the Channel slicer",
        "keep the Channel slicer in sync",
    ],
)
def test_an_invented_sync_destination_asks_for_an_existing_page(message: str) -> None:
    outcome, _prompt, _stub = plan_with(
        message,
        "sync_slicer",
        lambda p: {
            "visual": visual(p, CHANNEL),
            "pages": "Invented by the model",
        },
        context="agents-sync-qa",
    )
    assert outcome.kind == "clarify" and not outcome.ops, outcome.reason
    assert outcome.clause is not None
    assert "Which pages" in outcome.clause.question
    assert [choice.ops for choice in outcome.clause.choices] == [
        (
            {
                "op": "sync_slicer",
                "visual": CHANNEL,
                "pages": ["Sync QA"],
            },
        )
    ]


@pytest.mark.parametrize(
    "message",
    [
        "sync the ChannelName slicer to Sync QA",
        "sync the Channel slicer across all pages",
        "sync the Channel slicer to every other page",
    ],
)
def test_a_sync_destination_in_the_request_replaces_an_invented_model_page(message: str) -> None:
    outcome, _prompt, _stub = plan_with(
        message,
        "sync_slicer",
        lambda p: {
            "visual": visual(p, CHANNEL),
            "pages": "Invented by the model",
        },
        context="agents-sync-qa",
    )
    assert outcome.kind == "plan", outcome.reason
    assert outcome.ops == ({"op": "sync_slicer", "visual": CHANNEL, "pages": ["Sync QA"]},)


def test_a_hidden_sync_with_no_page_asks_which() -> None:
    outcome, _prompt, _stub = plan_with(
        "sync the Channel slicer, hidden",
        "sync_slicer",
        lambda p: {"visual": visual(p, CHANNEL)},
        context="agents-sync-qa",
    )
    assert outcome.kind == "clarify" and not outcome.ops


# 9. general rules in place of phrases -------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "take Employee Name out of the table",
        "remove Employee Name from the table",
        "get rid of Rank in the table",
        "drop the Rank column from the table",
    ],
)
def test_removing_a_named_field_from_a_visual_offers_the_field_wells(message: str) -> None:
    assert planner._removes_a_field_from_a_visual(message, contexts.context_for("agents"))


@pytest.mark.parametrize(
    "message",
    [
        "take the title off the table",
        "remove the Total MTD Sales chart from the page",
        "remove Rank from the bookmark",
        "take the border off the StoreName slicer",
    ],
)
def test_removing_something_else_does_not_narrow_to_the_field_wells(message: str) -> None:
    assert not planner._removes_a_field_from_a_visual(message, contexts.context_for("agents"))


@pytest.mark.parametrize(
    "message",
    [
        "label the bars of Total MTD Sales in millions",
        "show the bar labels of Total MTD Sales in millions",
        "put labels on the columns of Total MTD Sales in millions",
    ],
)
def test_labelled_marks_with_units_offer_only_a_number_format(message: str) -> None:
    stub = response(
        "set_number_format",
        lambda p: {
            "visual": visual(p, SALES),
            "display_units": "millions",
        },
    )
    outcome, prompt = run(message, "set_number_format", stub, shortlist=True)
    assert "show" not in slot_properties(prompt, "set_number_format")
    assert outcome.kind == "plan" and outcome.ops[0]["display_units"] == "millions"


@pytest.mark.parametrize(
    "message",
    [
        "add a button that takes people back",
        "add a back arrow button",
        "add a button that goes back",
    ],
)
def test_a_back_button_in_other_words_offers_no_destination(message: str) -> None:
    stub = response("add_button", lambda _p: {"kind": "back"})
    _outcome, prompt = run(message, "add_button", stub)
    assert (
        not {"target_page", "target_bookmark", "url"} & slot_properties(prompt, "add_button").keys()
    )


@pytest.mark.parametrize(
    ("name", "forms"),
    [
        ("ChannelName", {"ChannelName", "Channel Name", "Channel"}),
        ("StoreKey", {"StoreKey", "Store Key", "Store"}),
        ("Product_Code", {"Product_Code", "Product Code", "Product"}),
        ("Country", {"Country"}),
    ],
)
def test_label_forms_drop_the_storage_suffix(name: str, forms: set[str]) -> None:
    assert set(planner._label_forms(name)) == forms


# the role a request limits to what it sees gets its row filter ------------------------------


@pytest.mark.parametrize(
    ("message", "name"),
    [
        ("create a security role Online only that only sees the Online channel", "Online only"),
        ("add a role Online Team that can only see the Online channel", "Online Team"),
        ("create a role Web Staff restricted to the Online channel", "Web Staff"),
    ],
)
def test_a_simple_role_gets_its_grounded_filter_without_drafting(message: str, name: str) -> None:
    outcome, _prompt, stub = plan_with(
        message,
        "create_role",
        lambda _p: {"name": name},
        draft='DimChannel[ChannelName] = "Online"',
    )
    assert outcome.kind == "plan", outcome.reason
    assert stub.draft_calls == 0
    assert outcome.ops[0]["filters"] == [
        {"table": "DimChannel", "expression": "'DimChannel'[ChannelName] = \"Online\""}
    ]


def test_a_simple_role_filter_does_not_depend_on_an_empty_model_draft() -> None:
    outcome, _prompt, stub = plan_with(
        "create a security role Online only that only sees the Online channel",
        "create_role",
        lambda _p: {"name": "Online only"},
        draft="",
    )
    assert stub.draft_calls == 0
    assert outcome.kind == "plan", outcome.reason
    assert outcome.ops[0]["filters"] == [
        {"table": "DimChannel", "expression": "'DimChannel'[ChannelName] = \"Online\""}
    ]


def test_a_role_the_words_do_not_limit_drafts_nothing() -> None:
    outcome, _prompt, stub = plan_with(
        "create a security role called Auditors",
        "create_role",
        lambda _p: {"name": "Auditors"},
    )
    assert stub.draft_calls == 0
    assert all("filters" not in o for o in outcome.ops)


# a question, not a give-up, when the words leave a required value open ---------------------


def test_a_missing_required_value_is_asked_not_given_up() -> None:
    outcome, _prompt, _stub = plan_with(
        "make the relationship between DimChannel and FactSales filter in both directions",
        "update_relationship",
        lambda _p: {"cross_filter": "both"},
    )
    assert outcome.kind == "clarify" and not outcome.ops
