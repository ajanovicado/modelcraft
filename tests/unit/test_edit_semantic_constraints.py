"""Request fidelity against wrong model slots, without a model or edit execution."""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from ai_powerbi_analyst.studio.edit_context import EditContext, PageEntry, VisualEntry
from ai_powerbi_analyst.studio.edit_semantic_constraints import constrain_edit_request
from ai_powerbi_analyst.studio.edit_tools import ToolCard
from ai_powerbi_analyst.studio.edit_types import ParseKind


def entry(
    identifier: str, caption: str, kind: str, fields: tuple[str, ...], x: float
) -> VisualEntry:
    return VisualEntry(identifier, "summary", "Summary", kind, caption, fields, x, 10, 200, 150)


@pytest.fixture
def context() -> EditContext:
    return EditContext(
        pages=[
            PageEntry("summary", "Summary", 0, 1280, 720),
            PageEntry("forecast", "Forecast", 1, 1280, 720),
        ],
        visuals=[
            entry("sales", "Sales chart", "clusteredColumnChart", ("MTD Sales",), 10),
            entry("discount", "Discount chart", "lineChart", ("Discount Amount",), 300),
            entry("cost", "Cost chart", "barChart", ("Total Cost",), 600),
            entry("table", "Agent table", "tableEx", ("MTD Sales", "vs PM %", "Rank"), 10),
            entry("card", "Profit card", "cardVisual", ("Profit",), 300),
            entry("slicer", "Region", "slicer", ("Region",), 600),
        ],
        measures=[],
        tables={"Orders": ["Sales", "Discount Amount", "Total Cost"]},
        active_page="summary",
    )


def ask(result: object) -> str:
    clause = result.clarification  # type: ignore[attr-defined]
    assert clause is not None and clause.kind is ParseKind.CLARIFY
    assert not clause.ops and not clause.choices
    return clause.question


@pytest.mark.parametrize(
    "message",
    [
        "add a running total of discounts to the Sales chart",
        "put a running sum of costs on the Sales chart",
        "add a cumulative sum of discounts in the Sales chart",
        "give the Sales chart a running total of costs",
        "add a moving average of discounts to the Sales chart",
        "add a rolling average of costs onto the Sales chart",
    ],
)
def test_unbound_requested_operand_never_becomes_sales(context: EditContext, message: str) -> None:
    values = {"visual": "sales", "template": "running_sum", "field": "MTD Sales"}
    result = constrain_edit_request(context, message, "set_visual_calculation", values)
    question = ask(result)
    assert "does not show" in question and "bound field" in question
    assert values == {"visual": "sales", "template": "running_sum", "field": "MTD Sales"}


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("add a running sum of Discount Amount to the Discount chart", "Discount Amount"),
        ("put a cumulative total of discounts on the Discount chart", "Discount Amount"),
        ("add a running total of costs to the Cost chart", "Total Cost"),
        ("add a running total of MTD Sales as a visual calculation to the table", "MTD Sales"),
        ("put a running sum of Total Cost on the chart on the right", "Total Cost"),
    ],
)
def test_bound_operand_repairs_wrong_model_field(
    context: EditContext,
    message: str,
    expected: str,
) -> None:
    result = constrain_edit_request(
        context,
        message,
        "set_visual_calculation",
        {"visual": "sales", "template": "running_sum", "field": "Sales"},
    )
    assert result.clarification is None and result.values["field"] == expected


def test_missing_visual_and_ambiguous_field_ask(context: EditContext) -> None:
    result = constrain_edit_request(
        context,
        "add a running total",
        "set_visual_calculation",
        {"visual": "sales", "template": "running_sum", "field": "MTD Sales"},
    )
    assert "Which visual" in ask(result)
    context.selection = "table"
    result = constrain_edit_request(
        context,
        "add a running total of the values",
        "set_visual_calculation",
        {"visual": "table", "template": "running_sum", "field": "Rank"},
    )
    assert "Which bound field" in ask(result)


def test_selected_single_bound_value_is_unambiguous(context: EditContext) -> None:
    context.selection = "sales"
    result = constrain_edit_request(
        context,
        "add a running sum of its values",
        "set_visual_calculation",
        {"visual": "sales", "template": "running_sum"},
    )
    assert result.clarification is None and result.values["field"] == "MTD Sales"


def test_single_measure_excludes_category_from_generic_operand_choice(context: EditContext) -> None:
    context.visuals[0] = replace(
        context.visuals[0],
        fields=("Employee Name", "MTD Sales"),
        bindings=(("column", "Employees", "Employee Name"), ("measure", "Measures", "MTD Sales")),
    )
    result = constrain_edit_request(
        context,
        "add a running total to the Sales chart",
        "set_visual_calculation",
        {"visual": "sales", "template": "running_sum"},
    )
    assert result.clarification is None and result.values["field"] == "MTD Sales"


@pytest.mark.parametrize("qualifier", ["Avg", "Average", "Mean", "Median", "Distinct Count"])
def test_metric_qualifier_is_not_added_to_requested_operand(
    context: EditContext,
    qualifier: str,
) -> None:
    context.visuals[0] = replace(context.visuals[0], fields=(f"{qualifier} MTD Sales",))
    result = constrain_edit_request(
        context,
        "add a running total of MTD Sales to the Sales chart",
        "set_visual_calculation",
        {"visual": "sales", "template": "running_sum", "field": f"{qualifier} MTD Sales"},
    )
    assert "does not show MTD Sales" in ask(result)


@pytest.mark.parametrize(
    "message",
    [
        "make Sales chart only highlight the other visuals instead of filtering them",
        "Sales chart should highlight every other visual",
        "only highlight all remaining visuals when I select Sales chart",
        "change the Sales chart interactions to highlight",
    ],
)
def test_all_peer_highlights_do_not_silently_skip_ineligible_visuals(
    context: EditContext,
    message: str,
) -> None:
    result = constrain_edit_request(
        context, message, "set_interaction", {"source": "sales", "type": "highlight"}
    )
    question = ask(result)
    assert "Agent table" in question and "Profit card" in question and "Region" in question
    assert "disabled" in question and "filtering" in question


@pytest.mark.parametrize("mode", ["filter", "none", "default"])
def test_retained_nonhighlight_interaction_is_unchanged(context: EditContext, mode: str) -> None:
    values = {"source": "sales", "type": mode}
    result = constrain_edit_request(
        context, f"set Sales chart interaction to {mode}", "set_interaction", values
    )
    assert result.clarification is None and result.values == values


def test_explicit_eligible_highlight_peers_are_preserved(context: EditContext) -> None:
    values = {"source": "sales", "targets": ["discount", "cost"], "type": "highlight"}
    result = constrain_edit_request(
        context,
        "Sales chart should highlight Discount chart and Cost chart",
        "set_interaction",
        values,
    )
    assert result.clarification is None and result.values == values


def test_wrong_filter_slot_cannot_override_only_highlight_request(context: EditContext) -> None:
    result = constrain_edit_request(
        context,
        "make Sales chart only highlight the other visuals instead of filtering them",
        "set_interaction",
        {"source": "sales", "type": "filter"},
    )
    assert result.values["type"] == "highlight" and "disabled" in ask(result)


def test_negated_only_highlight_preserves_requested_filter(context: EditContext) -> None:
    values = {"source": "sales", "type": "filter"}
    result = constrain_edit_request(
        context,
        "filter the other visuals; don't only highlight them",
        "set_interaction",
        values,
    )
    assert result.clarification is None and result.values == values


def test_decoration_is_not_a_filtered_peer(context: EditContext) -> None:
    context.visuals = context.visuals[:2] + [entry("shape", "Decoration", "basicShape", (), 900)]
    result = constrain_edit_request(
        context,
        "highlight all other visuals",
        "set_interaction",
        {"source": "sales", "type": "highlight"},
    )
    assert result.clarification is None


@pytest.mark.parametrize("targets", [["discount"], ["sales"], ["missing"], ["table"]])
def test_incomplete_invalid_or_ineligible_peer_list_asks(
    context: EditContext, targets: list[str]
) -> None:
    result = constrain_edit_request(
        context,
        "highlight all other visuals",
        "set_interaction",
        {"source": "sales", "targets": targets, "type": "highlight"},
    )
    ask(result)


@pytest.mark.parametrize(
    "message",
    [
        "add a back button to the Forecast page",
        "insert a back button on Forecast",
        "put a go back button in Forecast",
    ],
)
def test_back_action_is_not_a_page_destination(context: EditContext, message: str) -> None:
    values = {"page": "forecast", "kind": "back", "action": "page", "target_page": "forecast"}
    result = constrain_edit_request(context, message, "add_button", values)
    assert result.clarification is None
    assert result.values == {"page": "forecast", "kind": "back", "action": "back"}
    assert values["action"] == "page" and values["target_page"] == "forecast"


@pytest.mark.parametrize("operation", [False, True])
def test_explicit_button_navigation_separates_placement_and_destination(
    context: EditContext,
    operation: bool,
) -> None:
    values = {"page": "forecast", "kind": "back", "action": "back"}
    if operation:
        values.update({"op": "add_button", "action": {"type": "back"}})
    result = constrain_edit_request(
        context, "add a back button on Forecast that opens Summary", "add_button", values
    )
    assert result.clarification is None and result.values["page"] == "forecast"
    if operation:
        assert result.values["action"] == {"type": "page", "page": "summary"}
    else:
        assert result.values["action"] == "page" and result.values["target_page"] == "summary"


def test_unknown_button_destination_asks(context: EditContext) -> None:
    result = constrain_edit_request(
        context,
        "add a back button that opens Unknown",
        "add_button",
        {"page": "forecast", "kind": "back", "action": "page"},
    )
    assert "destination" in ask(result)


@pytest.mark.parametrize(
    "message",
    [
        "Show colored arrows next to vs PM % values",
        "add coloured arrows to the vs PM % column",
        "show arrow icons on vs PM %",
    ],
)
def test_model_chosen_arrow_count_requires_user_choice(context: EditContext, message: str) -> None:
    result = constrain_edit_request(
        context,
        message,
        "set_conditional_format",
        {"visual": "table", "field": "vs PM %", "style": "icons", "icon_set": "ThreeColoredArrows"},
    )
    assert "three, four, or five" in ask(result)


@pytest.mark.parametrize(
    ("count", "set_name"),
    [("three", "ThreeColoredArrows"), ("4", "FourColoredArrows"), ("five", "FiveColoredArrows")],
)
def test_explicit_count_repairs_model_default(
    context: EditContext, count: str, set_name: str
) -> None:
    result = constrain_edit_request(
        context,
        f"show {count} colored arrows on vs PM %",
        "set_conditional_format",
        {"visual": "table", "style": "icons", "icon_set": "ThreeColoredArrows"},
    )
    assert result.clarification is None and result.values["icon_set"] == set_name


@pytest.mark.parametrize(
    "message",
    [
        "show ThreeColoredArrows on vs PM %",
        "use ThreeTrafficLightsRimmed on vs PM %",
        "show three colored arrows to the right of vs PM % values",
    ],
)
def test_explicit_presets_and_placement_are_preserved(context: EditContext, message: str) -> None:
    preset = "ThreeTrafficLightsRimmed" if "Traffic" in message else "ThreeColoredArrows"
    values = {"visual": "table", "style": "icons", "icon_set": preset, "icon_position": "right"}
    result = constrain_edit_request(context, message, "set_conditional_format", values)
    assert result.clarification is None and result.values == values


@pytest.mark.parametrize(
    "condition",
    [
        "below 0",
        "below zero",
        "under 0",
        "above 5%",
        "less than -10",
        "greater than 20",
        "< 0",
        "at most 0",
        "under the target",
        "between -10 and 0",
    ],
)
def test_preset_never_drops_requested_condition(context: EditContext, condition: str) -> None:
    result = constrain_edit_request(
        context,
        f"show three colored arrows on vs PM % values {condition}",
        "set_conditional_format",
        {"visual": "table", "field": "vs PM %", "style": "icons", "icon_set": "ThreeColoredArrows"},
    )
    question = ask(result)
    assert "condition" in question or "bounds" in question


def negative_rules() -> list[dict[str, object]]:
    return [
        {"icon": "ColoredArrowDown", "max": -20},
        {"icon": "ColoredArrowRight", "min": -20, "max": -10},
        {"icon": "ColoredArrowUp", "min": -10, "max": 0},
    ]


def test_complete_explicit_numeric_icon_rules_are_retained(context: EditContext) -> None:
    rules = negative_rules()
    values = {"visual": "table", "style": "icons", "icon_set": "ThreeColoredArrows", "rules": rules}
    result = constrain_edit_request(
        context,
        "show three colored arrows only below 0, with cutoffs -20 and -10",
        "set_conditional_format",
        values,
    )
    assert result.clarification is None and result.values["rules"] == rules
    assert "icon_set" not in result.values and values["icon_set"] == "ThreeColoredArrows"


@pytest.mark.parametrize(
    "defect",
    [
        "includes_zero",
        "whole_range",
        "gap",
        "unasked_cutoff",
        "wrong_icons",
        "repeated_icon",
        "percent_of_range",
    ],
)
def test_numeric_range_and_icon_identity_must_both_match(context: EditContext, defect: str) -> None:
    rules = negative_rules()
    if defect == "includes_zero":
        rules[-1]["max_inclusive"] = True
    elif defect == "whole_range":
        rules[-1].pop("max")
    elif defect == "gap":
        rules[1]["min_inclusive"] = False
    elif defect == "unasked_cutoff":
        rules[0]["max"] = -30
        rules[1]["min"] = -30
    elif defect == "wrong_icons":
        rules[0]["icon"] = "GreyArrowDown"
    elif defect == "repeated_icon":
        rules[1]["icon"] = "ColoredArrowDown"
    else:
        rules[0]["unit"] = "percent"
    result = constrain_edit_request(
        context,
        "show three colored arrows only below 0 with cutoffs -20 and -10",
        "set_conditional_format",
        {"style": "icons", "rules": rules},
    )
    ask(result)


def test_existing_color_rules_and_unrelated_operations_are_unchanged(context: EditContext) -> None:
    values = {"visual": "table", "style": "rules", "threshold": 0, "below_color": "#FF0000"}
    result = constrain_edit_request(
        context, "make negative Profit values red", "set_conditional_format", values
    )
    assert result.clarification is None and result.values == values
    unrelated = {"targets": ["sales"], "nested": {"values": [1, 2]}}
    original = copy.deepcopy(unrelated)
    card = ToolCard("set_visual_hidden", "report", "Hide a visual", ())
    result = constrain_edit_request(context, "hide Sales chart", card, unrelated)
    result.values["nested"]["values"].append(3)
    assert unrelated == original and result.clarification is None


def test_ambiguous_type_target_does_not_accept_a_model_guess(context: EditContext) -> None:
    context.visuals.append(
        replace(context.visuals[1], id="discount2", caption="Other discount chart")
    )
    result = constrain_edit_request(
        context,
        "add a running total of Discount Amount to a chart",
        "set_visual_calculation",
        {"visual": "discount", "template": "running_sum", "field": "Discount Amount"},
    )
    assert "Which visual" in ask(result)


_TWO_SIDED_ICON_CONDITIONS = [
    ("at least -5% and at most 0%", True, True),
    ("at most 0% and at least -5%", True, True),
    (">= -5% AND <= 0%", True, True),
    ("<= 0% and >= -5%", True, True),
    ("over -5% and below 0%", False, False),
    ("below 0% and over -5%", False, False),
    ("greater than -5% but less than 0%", False, False),
    ("under 0% and also above -5%", False, False),
    ("at least -5% and below 0%", True, False),
    ("above -5% and at most 0%", False, True),
    ("no less than -5% and no more than 0%", True, True),
    ("are at most 0% and are at least -5%", True, True),
    (">= -5%, and <= 0%", True, True),
    ("below 0% while above -5%", False, False),
    ("at least -5% as well as at most 0%", True, True),
]


@pytest.mark.parametrize(
    ("condition", "min_inclusive", "max_inclusive"), _TWO_SIDED_ICON_CONDITIONS
)
@pytest.mark.parametrize("defect", ["missing_min", "missing_max", "wrong_min", "wrong_max"])
def test_two_sided_icon_condition_never_loses_a_bound(
    context: EditContext,
    condition: str,
    min_inclusive: bool,
    max_inclusive: bool,
    defect: str,
) -> None:
    rule = {
        "icon": "ColoredArrowDown",
        "min": -0.05,
        "max": 0,
        "min_inclusive": min_inclusive,
        "max_inclusive": max_inclusive,
    }
    if defect == "missing_min":
        rule.pop("min")
    elif defect == "missing_max":
        rule.pop("max")
    elif defect == "wrong_min":
        rule["min_inclusive"] = not min_inclusive
    else:
        rule["max_inclusive"] = not max_inclusive
    result = constrain_edit_request(
        context,
        f"show arrow icons only for values {condition}",
        "set_conditional_format",
        {"visual": "table", "style": "icons", "rules": [rule]},
    )
    assert "condition" in ask(result)


@pytest.mark.parametrize(
    ("condition", "min_inclusive", "max_inclusive"), _TWO_SIDED_ICON_CONDITIONS
)
def test_complete_two_sided_icon_condition_keeps_both_bounds(
    context: EditContext,
    condition: str,
    min_inclusive: bool,
    max_inclusive: bool,
) -> None:
    rules = [
        {
            "icon": "ColoredArrowDown",
            "min": -0.05,
            "max": 0,
            "min_inclusive": min_inclusive,
            "max_inclusive": max_inclusive,
        }
    ]
    values = {"visual": "table", "style": "icons", "rules": rules}
    result = constrain_edit_request(
        context,
        f"show arrow icons only for values {condition}",
        "set_conditional_format",
        values,
    )
    assert result.clarification is None and result.values == values


def test_outer_icon_range_keeps_separate_per_icon_conditions(context: EditContext) -> None:
    rules = negative_rules()
    result = constrain_edit_request(
        context,
        "show three colored arrows only below 0, with down below -20, "
        "right from -20 to -10, and up above -10",
        "set_conditional_format",
        {"style": "icons", "rules": rules},
    )
    assert result.clarification is None and result.values["rules"] == rules
