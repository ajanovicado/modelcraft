"""Value-arrow requests reach semantic icon guards before numeric/categorical filters."""

from __future__ import annotations

import pytest
from test_edit_intents import PAGE, TABLE, ParseKind, agents, only_clause, parse
from test_imported_chat_edits import PlannerStub, Shown, find, make_rig, names, text_of

from ai_powerbi_analyst.studio.edit_capabilities import STYLING_REASON
from ai_powerbi_analyst.studio.edit_planner import _icon_kinds, available_cards
from ai_powerbi_analyst.studio.edit_semantic_constraints import constrain_edit_request
from ai_powerbi_analyst.studio.imported_chat_edits import PENDING_TTL_SECONDS
from ai_powerbi_analyst.studio.report_edit_conditional import ICON_SETS

COUNT_REQUEST = "Show colored arrows next to vs PM % values"
COUNT_PRESETS = ["ThreeColoredArrows", "FourColoredArrows", "FiveColoredArrows"]


@pytest.mark.parametrize(
    "message", [COUNT_REQUEST, "Show coloured arrows to the right of vs PM % values"]
)
def test_default_colored_arrow_count_menu_preserves_all_original_slots(message: str) -> None:
    clause = only_clause(parse(message))
    assert clause.kind is ParseKind.CLARIFY and not clause.ops
    assert [choice.label for choice in clause.choices] == COUNT_PRESETS
    for choice in clause.choices:
        assert len(choice.ops) == 1
        operation = choice.ops[0]
        assert operation["op"] == "set_conditional_format" and operation["visual"] == TABLE
        assert operation["field"] == "vs PM %" and operation["style"] == "icons"
        assert operation["icon_set"] == choice.label
        if "right" in message:
            assert operation["icon_position"] == "right"


def test_invalid_arrow_count_candidates_keep_card_clause_return_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ai_powerbi_analyst.studio.edit_planner.build_step", lambda *args, **kwargs: "invalid"
    )
    clause = only_clause(parse(COUNT_REQUEST))
    assert clause.kind is ParseKind.CLARIFY
    assert "three, four, or five" in clause.question
    assert not clause.ops and not clause.choices


@pytest.mark.parametrize("reply", ["three", "four", "five", "3", "4", "5"])
def test_bare_count_without_a_menu_is_not_a_fresh_edit(reply: str) -> None:
    rig = make_rig()
    assert rig.ask(reply, key="arrow-count-fresh-0001") is None
    assert rig.port.calls == [] and rig.port.head_id == "r0003"


@pytest.mark.parametrize("index", [0, 1, 2])
def test_arrow_count_buttons_apply_only_the_original_grounded_request(index: int) -> None:
    rig = make_rig()
    events = rig.ask("Show coloured arrows to the right of vs PM % values")
    question = find(events, "edit_choices")
    assert [choice["label"] for choice in question["choices"]] == COUNT_PRESETS
    assert rig.port.calls == [] and rig.port.head_id == "r0003"
    answered = rig.answer(question["pending_id"], index=index)
    assert "edit_applied" in names(answered)
    ((_, call),) = rig.port.calls
    (operation,) = call["ops"]
    assert operation["visual"] == TABLE and operation["field"] == "vs PM %"
    assert operation["icon_position"] == "right" and operation["icon_set"] == COUNT_PRESETS[index]


@pytest.mark.parametrize(
    ("reply", "preset"),
    [
        ("three", "ThreeColoredArrows"),
        ("four", "FourColoredArrows"),
        ("five", "FiveColoredArrows"),
        ("3", "ThreeColoredArrows"),
        ("4", "FourColoredArrows"),
        ("5", "FiveColoredArrows"),
    ],
)
def test_bare_count_reply_uses_only_the_live_grounded_arrow_menu(reply: str, preset: str) -> None:
    rig = make_rig()
    first = rig.ask("Show coloured arrows to the right of vs PM % values")
    assert len(find(first, "edit_choices")["choices"]) == 3
    assert rig.port.calls == [] and rig.port.head_id == "r0003"
    answered = rig.ask(reply, key="arrow-count-reply-0001")
    assert "edit_applied" in names(answered)
    ((_, call),) = rig.port.calls
    (operation,) = call["ops"]
    assert operation["op"] == "set_conditional_format" and operation["visual"] == TABLE
    assert operation["field"] == "vs PM %" and operation["icon_position"] == "right"
    assert operation["icon_set"] == preset and call["head"] == "r0003"


@pytest.mark.parametrize("stale", ["expired", "new_head"])
def test_bare_count_does_not_apply_a_stale_arrow_menu(stale: str) -> None:
    rig = make_rig()
    assert find(rig.ask(COUNT_REQUEST), "edit_choices")
    if stale == "expired":
        rig.clock[0] = PENDING_TTL_SECONDS + 1
    else:
        rig.port.head_id = "r0004"
    assert rig.ask("three", key="arrow-count-stale-0001") is None
    assert rig.port.calls == []


@pytest.mark.parametrize(
    "followup", ["Thanks", "What are the totals?", "yes", "three more columns"]
)
def test_unrelated_followup_does_not_answer_or_reuse_an_arrow_count_menu(followup: str) -> None:
    rig = make_rig(_icon_model())
    assert find(rig.ask(COUNT_REQUEST), "edit_choices")
    rig.ask(followup, key="arrow-count-new-topic-0001")
    assert rig.ask("three", key="arrow-count-after-topic-0001") is None
    assert not any(
        operation["op"] == "set_conditional_format"
        for _, call in rig.port.calls
        for operation in call.get("ops", [])
    )


def test_bare_count_does_not_answer_an_arbitrary_numbered_choice() -> None:
    rig = make_rig()
    assert find(rig.ask("move the visual 10 pixels right"), "edit_choices")
    assert rig.ask("three", key="arrow-count-other-choice-0001") is None
    assert rig.port.calls == []


@pytest.mark.parametrize("condition", ["only above 0", "only for Online", "only under the target"])
def test_restricted_unknown_arrow_count_keeps_a_no_choice_condition_question(
    condition: str,
) -> None:
    rig = make_rig(_icon_model())
    events = rig.ask(f"Show colored arrows next to vs PM % values {condition}")
    assert "edit_choices" not in names(events) and "condition" in text_of(events)
    assert rig.ask("three", key="arrow-count-condition-0001") is None
    assert rig.port.calls == [] and rig.port.head_id == "r0003"


def test_gray_arrow_count_question_does_not_offer_colored_sets_or_a_five_gray_default() -> None:
    values = {"visual": TABLE, "field": "vs PM %", "style": "icons"}
    result = constrain_edit_request(
        agents(), "Show gray arrows next to vs PM % values", "set_conditional_format", values
    )
    assert result.clarification is not None and result.clarification.kind is ParseKind.CLARIFY
    assert "gray arrow set" in result.clarification.question
    assert "five" not in result.clarification.question
    assert not result.choices and not result.clarification.choices and not result.clarification.ops


def test_numbered_gray_arrows_never_become_an_unknown_colored_count_menu() -> None:
    values = {"visual": TABLE, "field": "vs PM %", "style": "icons", "icon_set": "FourGrayArrows"}
    result = constrain_edit_request(
        agents(), "Show four gray arrows next to vs PM % values", "set_conditional_format", values
    )
    assert result.clarification is None and not result.choices
    assert result.values == values


BOUNDED_ARROWS = [
    "Show three colored arrows next to vs PM % values only above -5% and below zero",
    "Display four grey arrows on vs PM % values only between -5% and zero",
    "Use five colored arrows alongside the vs PM % values only above -5% and below 0",
    "Show 3 gray arrows next to vs PM % values only between -5% and 0%",
    "Show 3 arrows in vs PM % cells only above -5% and below zero",
    "Show ThreeColoredArrows next to vs PM % values only above -5% and below zero",
    "Use FourGrayArrows on vs PM % values only below zero",
    "Show colored arrows next to vs PM % values only above -5% and below zero",
]


@pytest.mark.parametrize("message", BOUNDED_ARROWS)
def test_value_arrows_defer_to_the_conditional_planner(message: str) -> None:
    result = parse(message)
    assert not result.ops
    if result.unparsed:
        assert result.unparsed == (message,)
        assert not result.clauses
    else:
        clause = only_clause(result)
        assert clause.kind is ParseKind.CLARIFY
        assert "condition" in clause.question and "cutoffs" in clause.question


def _icon_model() -> PlannerStub:
    stub = PlannerStub()

    def preset(shown: Shown) -> dict[str, object]:
        tool = shown.tool("set_conditional_format")
        schema = stub.calls[-1]["response"].model_json_schema()
        step = next(
            item
            for item in schema["properties"]["steps"]["items"]["anyOf"]
            if item["properties"]["tool"]["const"] == tool
        )
        slots = step["properties"]["slots"]["properties"]
        return {
            "kind": "plan",
            "steps": [
                {
                    "tool": tool,
                    "slots": {
                        "visual": slots["visual"]["enum"][0],
                        "field": "vs PM %",
                        "style": "icons",
                        "icon_set": slots["icon_set"]["enum"][0],
                    },
                }
            ],
        }

    stub.answer = preset
    return stub


@pytest.mark.parametrize("message", BOUNDED_ARROWS)
def test_incomplete_arrow_bounds_clarify_without_any_edit(message: str) -> None:
    stub = _icon_model()
    rig = make_rig(stub)
    events = rig.ask(message)
    assert len(stub.calls) == (0 if message.startswith("Use FourGrayArrows") else 1)
    if "between" in message:
        assert "numeric bounds should the icon condition" in text_of(events)
        assert "each range" in text_of(events)
    else:
        assert "condition" in text_of(events)
        assert "cutoffs" in text_of(events)
    assert "Which measure or number field" not in text_of(events)
    assert rig.port.calls == [] and rig.port.head_id == "r0003"
    assert rig.questions == []
    assert not {"edit_applied", "edit_plan", "edit_confirm"}.intersection(names(events))
    assert "edit_choices" not in names(events)


def test_registered_column_arrow_reader_also_clarifies_bounds_without_a_filter() -> None:
    message = "Add three coloured arrows to the vs PM % column only below zero"
    clause = only_clause(parse(message))
    assert clause.kind is ParseKind.CLARIFY
    assert "condition (below 0)" in clause.question and "cutoffs" in clause.question
    assert not clause.ops
    rig = make_rig()
    events = rig.ask(message)
    assert text_of(events) == clause.question
    assert rig.port.calls == [] and rig.port.head_id == "r0003" and rig.questions == []


@pytest.mark.parametrize(
    "preset",
    [
        "ThreeColoredArrows",
        "ThreeGrayedArrows",
        "FourGrayArrows",
        "FiveArrows",
    ],
)
def test_exact_canonical_icon_names_are_grounded_choices(preset: str) -> None:
    card = next(card for card in available_cards() if card.op == "set_conditional_format")
    assert _icon_kinds(card, f"use {preset} on the values") == [preset]
    assert _icon_kinds(card, f"use {preset}Archive on the values") == []


@pytest.mark.parametrize("preset", ["ThreeGrayedArrows", "FourGrayArrows", "FiveArrows"])
@pytest.mark.parametrize("defect", ["none", "wrong_count", "wrong_icons"])
def test_named_gray_arrow_presets_require_their_exact_count_and_icons(
    preset: str, defect: str
) -> None:
    icons = list(ICON_SETS[preset])
    if defect == "wrong_count":
        icons.pop(-2)
    elif defect == "wrong_icons":
        icons = ["ColoredArrowDown"] * len(icons)
    rules = [
        {
            "icon": icon,
            "min": index * 10,
            "min_inclusive": index != 0,
            **({"max": (index + 1) * 10} if index != len(icons) - 1 else {}),
        }
        for index, icon in enumerate(icons)
    ]
    values = {
        "visual": TABLE,
        "field": "vs PM %",
        "style": "icons",
        "icon_set": preset,
        "rules": rules,
    }
    result = constrain_edit_request(
        agents(),
        f"show {preset} on vs PM % values only above 0 with cutoffs 10, 20, 30 and 40",
        "set_conditional_format",
        values,
    )
    if defect == "none":
        assert result.clarification is None and result.values["rules"] == rules
        assert "icon_set" not in result.values
    else:
        assert result.clarification is not None and not result.clarification.ops


@pytest.mark.parametrize("target", ["column", "table", "page"])
@pytest.mark.parametrize("quote", ['"', "'"])
def test_quoted_icon_named_targets_do_not_override_the_requested_preset(
    target: str, quote: str
) -> None:
    card = next(card for card in available_cards() if card.op == "set_conditional_format")
    assert _icon_kinds(
        card, f"Use FourGrayArrows on {quote}ThreeColoredArrows{quote} {target}"
    ) == ["FourGrayArrows"]
    assert _icon_kinds(card, f"Use {quote}FourGrayArrows{quote} on values") == ["FourGrayArrows"]
    assert _icon_kinds(
        card, f"Use FourGrayArrows on {target} {quote}ThreeColoredArrows{quote}"
    ) == ["FourGrayArrows"]


@pytest.mark.parametrize("quote", ['"', "'"])
def test_quoted_field_words_do_not_create_an_icon_condition(quote: str) -> None:
    values = {
        "visual": TABLE,
        "field": "Only for Online",
        "style": "icons",
        "icon_set": "ThreeColoredArrows",
    }
    constrained = constrain_edit_request(
        agents(),
        f"show three colored arrows on {quote}Only for Online{quote} values",
        "set_conditional_format",
        values,
    )
    assert constrained.clarification is None and constrained.values == values


@pytest.mark.parametrize(
    "message",
    [
        "Show three colored arrows next to vs PM % values only for Online",
        'Show three colored arrows next to vs PM % values only for "Online"',
        "Add three coloured arrows to the vs PM % column only when ChannelName is Online",
        "Show three colored arrows next to vs PM % values only for Online and above 0",
    ],
)
def test_categorical_icon_scope_clarifies_before_deterministic_or_model_edits(message: str) -> None:
    proposed = {
        "visual": TABLE,
        "field": "vs PM %",
        "style": "icons",
        "icon_set": "ThreeColoredArrows",
    }
    constrained = constrain_edit_request(agents(), message, "set_conditional_format", proposed)
    assert constrained.clarification is not None
    assert not constrained.clarification.ops
    assert "field and condition" in constrained.clarification.question
    result = parse(message)
    assert not result.ops
    if result.unparsed:
        assert result.unparsed == (message,) and not result.clauses
    else:
        clause = only_clause(result)
        assert clause.kind is ParseKind.CLARIFY
    rig = make_rig(_icon_model())
    events = rig.ask(message)
    assert text_of(events) == constrained.clarification.question
    assert rig.port.calls == [] and rig.port.head_id == "r0003" and rig.questions == []


@pytest.mark.parametrize(
    "condition",
    [
        "above 0 and ChannelName is Online",
        "above 0 and below 5 and ChannelName is Online",
        'above 0 and ChannelName is "Online"',
        "between 0 and 5 and ChannelName is Online",
    ],
)
def test_numeric_rules_never_drop_a_trailing_categorical_condition(condition: str) -> None:
    values = {
        "visual": TABLE,
        "field": "vs PM %",
        "style": "icons",
        "rules": [{"icon": "ColoredArrowDown", "min": 0, "min_inclusive": False}],
    }
    constrained = constrain_edit_request(
        agents(),
        f"show arrow icons only for values {condition}",
        "set_conditional_format",
        values,
    )
    assert constrained.clarification is not None and not constrained.clarification.ops
    assert "field and condition" in constrained.clarification.question


@pytest.mark.parametrize(
    "condition",
    [
        "only above 0 and ChannelName is Online",
        "above 0 and ChannelName is Online",
        "only for values above 0, only for Online",
        "when values above 0 and ChannelName is Online",
    ],
)
def test_bare_or_repeated_numeric_conditions_keep_their_categorical_restrictions(
    condition: str,
) -> None:
    values = {
        "visual": TABLE,
        "field": "vs PM %",
        "style": "icons",
        "rules": [{"icon": "ColoredArrowUp", "min": 0, "min_inclusive": False}],
    }
    constrained = constrain_edit_request(
        agents(),
        f"show arrow icons {condition}",
        "set_conditional_format",
        values,
    )
    assert constrained.clarification is not None and not constrained.clarification.ops


@pytest.mark.parametrize("subject", ["Total Sales", '"Total Sales"'])
@pytest.mark.parametrize("field", ["Total Sales", "vs PM %"])
def test_numeric_condition_operand_must_match_the_proposed_rule_field(
    subject: str, field: str
) -> None:
    values = {
        "visual": TABLE,
        "field": field,
        "style": "icons",
        "rules": [{"icon": "ColoredArrowUp", "min": 0, "min_inclusive": False}],
    }
    constrained = constrain_edit_request(
        agents(),
        f"show arrow icons only when {subject} is above 0",
        "set_conditional_format",
        values,
    )
    if field == "Total Sales":
        assert constrained.clarification is None and constrained.values == values
    else:
        assert constrained.clarification is not None and not constrained.clarification.ops


@pytest.mark.parametrize("basis", ["Total Sales", "Profit"])
@pytest.mark.parametrize("field", ["Total Sales", "vs PM %"])
def test_icon_condition_matches_the_effective_basis_even_when_target_matches(
    field: str, basis: str
) -> None:
    values = {
        "visual": TABLE,
        "field": field,
        "style": "icons",
        "based_on": {"table": "# Measures", "name": basis},
        "rules": [{"icon": "ColoredArrowUp", "min": 0, "min_inclusive": False}],
    }
    constrained = constrain_edit_request(
        agents(),
        "show arrow icons only when Total Sales above 0",
        "set_conditional_format",
        values,
    )
    if basis == "Total Sales":
        assert constrained.clarification is None and constrained.values == values
    else:
        assert constrained.clarification is not None and not constrained.clarification.ops


@pytest.mark.parametrize("intro", ["with", "only"])
@pytest.mark.parametrize("operand", ["Total Sales", "SalesAmount"])
def test_bare_numeric_conditions_ground_full_column_and_measure_operands(
    intro: str, operand: str
) -> None:
    values = {
        "visual": TABLE,
        "field": "Total Sales",
        "style": "icons",
        "rules": [{"icon": "ColoredArrowUp", "min": 0, "min_inclusive": False}],
    }
    constrained = constrain_edit_request(
        agents(),
        f"show arrow icons {intro} {operand} above 0",
        "set_conditional_format",
        values,
    )
    if operand == "Total Sales":
        assert constrained.clarification is None and constrained.values == values
    else:
        assert constrained.clarification is not None and not constrained.clarification.ops


@pytest.mark.parametrize(
    "scope",
    [
        'on the "Only for Online" table',
        'on page "Only for Online"',
        'on the "Only for Online" page',
    ],
)
def test_numeric_icon_scope_keeps_a_quoted_target_with_condition_words(scope: str) -> None:
    values = {
        "visual": TABLE,
        "field": "vs PM %",
        "style": "icons",
        "rules": [{"icon": "ColoredArrowUp", "min": 0, "min_inclusive": False}],
    }
    constrained = constrain_edit_request(
        agents(),
        f"show arrow icons only for values above 0 {scope}",
        "set_conditional_format",
        values,
    )
    assert constrained.clarification is None and constrained.values == values


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize(
    "literal",
    [
        "three colored arrows",
        "Show three colored arrows next to values only above 5",
        "ThreeColoredArrows",
        "4 gray arrows in cells only below zero",
    ],
)
def test_arrow_words_inside_filter_values_remain_exact_data(quote: str, literal: str) -> None:
    clause = only_clause(parse(f"filter ChannelName to {quote}{literal}{quote} on this page"))
    assert clause.kind is ParseKind.APPLY
    assert clause.ops == (
        {
            "op": "set_page_filter",
            "page": PAGE,
            "filter": {
                "kind": "categorical",
                "field": {"table": "DimChannel", "name": "ChannelName"},
                "values": [literal],
                "exclude": False,
            },
        },
    )


@pytest.mark.parametrize(
    "message",
    [
        "only show total sales above 0",
        "filter total sales to above 0",
        "only keep total sales above 0",
        "show only total sales above 0",
    ],
)
def test_explicit_numeric_filter_commands_keep_their_filter_route(message: str) -> None:
    clause = only_clause(parse(message))
    assert clause.kind is ParseKind.APPLY
    assert clause.ops == (
        {
            "op": "set_page_filter",
            "page": PAGE,
            "filter": {
                "kind": "advanced",
                "field": {"table": "# Measures", "name": "Total Sales"},
                "conditions": [{"operator": "greater_than", "value": 0}],
                "logic": "and",
            },
        },
    )


@pytest.mark.parametrize("field", ["Arrows", "Icons"])
def test_arrow_or_icon_named_numeric_fields_are_not_format_requests(field: str) -> None:
    context = agents()
    context.tables["Metrics"] = [field]
    context.column_types[f"Metrics.{field}"] = "double"
    clause = only_clause(parse(f"only show {field} above 0", context))
    assert clause.kind is ParseKind.APPLY
    assert clause.ops[0]["op"] == "set_page_filter"
    assert clause.ops[0]["filter"]["field"] == {"table": "Metrics", "name": field}
    assert clause.ops[0]["filter"]["conditions"] == [{"operator": "greater_than", "value": 0}]


@pytest.mark.parametrize(
    "message",
    [
        "draw a red arrow",
        "insert a red arrow at 100, 50",
        "add an arrow to this page",
        "draw an arrow beside the vs PM % values",
    ],
)
def test_arrow_shapes_remain_object_edits(message: str) -> None:
    result = parse(message)
    clause = only_clause(result)
    assert clause.kind is ParseKind.APPLY and not result.unparsed
    assert len(clause.ops) == 1
    assert clause.ops[0]["op"] == "add_shape"
    assert clause.ops[0]["shape"] == "arrow" and clause.ops[0]["page"] == PAGE


@pytest.mark.parametrize(
    "message",
    [
        "make the table background green",
        "change the vs PM % column font to Arial",
        "give the table rounded corners",
    ],
)
def test_unconditional_styling_keeps_the_exact_refusal(message: str) -> None:
    result = parse(message, agents(selection=TABLE))
    clause = only_clause(result)
    assert clause.kind is ParseKind.UNSUPPORTED
    assert clause.reason == STYLING_REASON
    assert not result.ops and not result.unparsed
