"""Chat edit intents: slots, targets, undo, vague requests, measures, refusals, the 4B tier.

The contexts come from evals/imported_edit/contexts.py: the committed structure twin of the
Agents Performance dashboard (one 1920 x 1200 page, 15 visuals, its real captions) and a
5-page synthetic report. ``run.py`` next to the cases holds the ship gates; the last tests
here run it on the whole case file.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from ai_powerbi_analyst.studio.edit_capabilities import CAPABILITY_MATRIX
from ai_powerbi_analyst.studio.edit_context import EditContext, LastEdit, hue_family
from ai_powerbi_analyst.studio.edit_intents import (
    MODEL_INTENTS,
    MODEL_SYSTEM,
    IndexTooLarge,
    ModelIntent,
    ParseKind,
    apply_choice,
    model_index,
    page_visuals,
    parse_edit_message,
    resolve_model_intent,
    stale_literal_mentions,
)
from ai_powerbi_analyst.studio.edit_targets import resolve_visuals
from ai_powerbi_analyst.studio.edit_text import classify, split_clauses, split_sentences
from ai_powerbi_analyst.studio.report_edits import ReportOperation

EVALS = Path(__file__).resolve().parents[2] / "evals" / "imported_edit"


def _load(name: str, file: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, EVALS / file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


contexts = _load("imported_edit_contexts", "contexts.py")
if not contexts.TWIN.exists():  # pragma: no cover - the twin is committed
    pytest.skip("the structure twin fixture is missing", allow_module_level=True)

TABLE = "e82e02f93055d6b0b544"
CHART = "b404a461ede7604a2774"  # 'Total MTD Sales', top left
CHART_TR = "da32cdbb0090e885649b"  # 'Avg MTD Sales by Agents'
CARD = "2202e311b1493069e4eb"  # caption 'Agents Avg MTD Sales' (field 'Employees Avg MTD Sales')
CARD_100K = "71a164765d9c63200302"
SLICER_STORE = "5b0dbccd61c6c4adb415"
OVERLAY = "8a8ba163374a72762030"  # a card's text lying over the TopN/BottomN slicer
SLICER_TOPN = "953a59a3cad98b0b5a39"
PAGE = "ReportSection8454c812b60b8bb83b99"


def agents(**options: Any) -> EditContext:
    context: EditContext = contexts.context_for("agents", dict(options))
    return context


def parse(message: str, context: EditContext | None = None) -> Any:
    return parse_edit_message(message, context or agents())


def kinds(result: Any) -> list[str]:
    return [c.kind.value for c in result.clauses]


def only_clause(result: Any) -> Any:
    assert len(result.clauses) == 1, [c.summary or c.question for c in result.clauses]
    return result.clauses[0]


# --- slots: imperative, declarative and question forms -------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "make the chart wider",
        "the chart should be wider",
        "could the chart be wider?",
        "I'd like the chart wider",
        "Please could you make the chart wider",
        "can the chart be wider",
    ],
)
def test_one_request_in_many_forms_reads_as_one_operation(message: str) -> None:
    clause = only_clause(parse(message, agents(selection="b404a461")))
    assert clause.kind is ParseKind.APPLY
    assert clause.ops[0]["op"] == "set_position" and clause.ops[0]["visual"] == CHART
    assert clause.ops[0]["width"] > agents().visual(CHART).width
    assert "'Total MTD Sales'" in clause.summary


def test_a_visual_title_is_the_target_even_when_it_is_the_only_object_named() -> None:
    clause = only_clause(parse("Make Total MTD Sales wider"))
    assert clause.ops[0]["op"] == "set_position" and clause.ops[0]["visual"] == CHART
    assert clause.targets[0].visual_ids == (CHART,) and clause.targets[0].how == "caption"


def test_clauses_split_only_where_each_side_has_its_own_action() -> None:
    result = parse(
        "make the bars green, add data labels and move the table left",
        agents(selection="b404a461"),
    )
    assert kinds(result) == ["unsupported", "apply"]
    assert result.clauses[1].ops[0]["visual"] == TABLE  # its own target, not the chart
    assert len(result.ops) == 1  # only the supported layout change
    assert kinds(parse("rename Sales and Profit")) == ["clarify"]
    assert split_clauses("rename Sales and Profit") == ["rename Sales and Profit"]
    assert split_clauses("add a table with Employee Name, Rank and Sales", ["Rank"]) == [
        "add a table with Employee Name, Rank and Sales"
    ]
    assert split_clauses("make the value bigger and bold") == ["make the value bigger and bold"]


def test_a_shared_ambiguity_is_asked_once_for_the_whole_message() -> None:
    result = parse("make the charts wider and taller")
    assert kinds(result) == ["apply"]
    assert len(result.ops) == 4
    assert all(op["op"] == "set_position" for op in result.ops)
    assert all("width" in op and "height" in op for op in result.ops)


# --- relative words: a change where the operation takes one, absolute numbers where not ----------


def test_relative_visual_size_is_deterministic_and_font_changes_are_refused() -> None:
    context = agents(selection="71a16476")
    first = parse("make it bigger", context)
    assert first.ops == parse("make it bigger", context).ops  # the same numbers each time
    op = first.ops[0]
    assert op["op"] == "set_position" and op["visual"] == CARD_100K
    assert (op["width"], op["height"]) == (425.2, 375.6)  # 354.33 x 313 grown by 20%
    assert kinds(parse("I'd like the font larger", context)) == ["unsupported"]
    assert kinds(parse("make the table font 2 pt smaller", context)) == ["unsupported"]
    assert kinds(parse("add 2 pt to the title font", context)) == ["unsupported"]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("move it 50 px to the right", {"x": 662.0}),
        ("move it a bit to the left", {"x": 583.2}),
        ("make it 300 x 200", {"width": 300.0, "height": 200.0}),
        ("make it a bit wider", {"width": 389.76}),
        ("make it 10 px wider", {"width": 364.33}),
    ],
)
def test_moves_and_sizes_become_absolute_positions(
    message: str, expected: dict[str, float]
) -> None:
    op = parse(message, agents(selection="2202e311")).ops[0]
    assert op["op"] == "set_position" and op["visual"] == CARD
    for key, value in expected.items():
        assert op[key] == pytest.approx(value, abs=0.011)


# --- targets -------------------------------------------------------------------------------------


def test_the_canvas_selection_wins_for_it_and_this_and_without_one_the_parser_asks() -> None:
    assert (
        only_clause(parse("make it bigger", agents(selection="71a16476"))).targets[0].how
        == "selection"
    )
    asked = only_clause(parse("make it bigger"))
    assert asked.kind is ParseKind.CLARIFY and "Select it on the canvas" in asked.question
    assert only_clause(parse("make this a line chart")).kind is ParseKind.CLARIFY


@pytest.mark.parametrize(
    ("message", "target"),
    [
        ("make the top left chart wider", CHART),
        ("make the bottom right chart taller", "ab13db00c71dcec86e99"),
        ("make the last chart narrower", "ab13db00c71dcec86e99"),
        ("make the right slicer wider", SLICER_STORE),
        ("make the left slicer smaller", "29ba010b20b2162d1536"),
        ("make the Agents Avg MTD Sales card bigger", CARD),  # the caption a person sees
        ("make the Employees Avg MTD Sales card bigger", CARD),  # its field
        ("make the TopN/BottomN card bigger", OVERLAY),  # a card lying inside a slicer
        ("make the TopN/BottomN slicer wider", SLICER_TOPN),
    ],
)
def test_position_caption_and_field_words_find_one_visual(message: str, target: str) -> None:
    clause = only_clause(parse(message))
    assert clause.kind is ParseKind.APPLY
    assert {op["visual"] for op in clause.ops} == {target}


def test_the_second_card_is_second_in_reading_order() -> None:
    clause = only_clause(parse("make the second card taller"))
    assert clause.targets[0].visual_ids == (CARD,)
    assert clause.ops[0]["visual"] == CARD and "height" in clause.ops[0]


def test_plurals_and_quantifiers_make_multi_target_operations() -> None:
    cards = only_clause(parse("make all the cards bigger"))
    assert len(cards.ops) == 5 and {op["visual"] for op in cards.ops} >= {CARD, CARD_100K}
    assert kinds(parse("every chart needs data labels")) == ["unsupported"]
    # "both" names two; five slicers is a question, and so is a bare "make them all blue"
    assert only_clause(parse("both slicers should be smaller")).kind is ParseKind.CLARIFY
    assert only_clause(parse("make them all blue")).kind is ParseKind.UNSUPPORTED
    top_right = only_clause(parse("the slicers on the top right are too small"))
    assert len(top_right.ops) == 3  # a region of the page, not one extreme
    two_top = only_clause(parse("make the two top charts the same size"))
    assert {op["visual"] for op in two_top.ops} == {CHART_TR}  # the first one is the reference


def test_a_target_on_another_page_and_page_names_resolve_with_the_page_named() -> None:
    context = contexts.context_for("multipage", {})
    slicers = only_clause(parse("make the slicers on Forecast smaller", context))
    assert {op["visual"] for op in slicers.ops} == {"page4-v06", "page4-v07"}
    rename = only_clause(parse("rename page 3 to Mix", context))
    assert rename.ops == ({"op": "rename_page", "page": "page2", "name": "Mix"},)
    front = only_clause(parse("move Forecast to the front", context))
    assert front.ops == ({"op": "move_page", "page": "page4", "index": 0},)


def test_hidden_visuals_and_groups_are_not_swept_up_and_show_finds_the_hidden_one() -> None:
    from dataclasses import replace

    context = agents()
    context.visuals = [replace(v, hidden=True) if v.id == OVERLAY else v for v in context.visuals]
    cards = only_clause(parse("make all the cards bigger", context))
    assert OVERLAY not in {op["visual"] for op in cards.ops} and len(cards.ops) == 4
    shown = only_clause(parse("show the hidden card", context))
    assert shown.ops == ({"op": "set_visual_hidden", "visual": OVERLAY, "hidden": False},)
    assert "No visual in this report is hidden" in only_clause(parse("show the hidden card")).reason
    grouped = agents()
    grouped.visuals = [
        replace(v, visual_type="visualGroup") if v.id == CARD else v for v in grouped.visuals
    ]
    assert CARD not in {op["visual"] for op in parse("make all the cards bigger", grouped).ops}


def test_moving_or_hiding_an_existing_picture_or_button_is_an_ordinary_edit() -> None:
    assert only_clause(parse("add our logo to the top left")).kind is ParseKind.UNSUPPORTED
    button = parse("add a button that goes to page 2")
    if "buttons_actions" in _provided_by_cards():
        # a family provides buttons: the request is the tool cards' and the planner's
        assert button.unparsed == ("add a button that goes to page 2",) and not button.ops
    else:
        assert only_clause(button).kind is ParseKind.UNSUPPORTED
    context = agents()
    button = next(v for v in context.visuals if v.id == CARD)
    context.visuals = [
        type(button)(**{**button.__dict__, "visual_type": "actionButton", "definition": None})
        if v.id == CARD
        else v
        for v in context.visuals
    ]
    moved = only_clause(parse("move the button left", context))
    assert moved.kind is ParseKind.APPLY and moved.ops[0]["visual"] == CARD
    # a new name or text is a value, not a request: "Filters" is not a filter
    multi = contexts.context_for("multipage", {})
    renamed = only_clause(parse("rename the Regional Performance page to Filters", multi))
    assert renamed.ops == ({"op": "rename_page", "page": "page1", "name": "Filters"},)


def test_questions_phrased_politely_do_not_reach_the_model_as_edits() -> None:
    for message in (
        "Could you tell me what the title color is?",
        "Can you check whether the title is bold?",
        "I want to know how many agents hit the target",
        "Can you explain the legend?",
    ):
        result = parse(message, agents(selection="b404a461"))
        assert set(kinds(result)) == {"not_an_edit"} and not result.unparsed and result.ops == []
    for message in (
        "Is it possible to make the bars green?",
        "Any chance you could make the title bigger?",
    ):
        clause = only_clause(parse(message, agents(selection="b404a461")))
        assert clause.kind is ParseKind.UNSUPPORTED


# --- formatting operations -----------------------------------------------------------------------


# --- pages, new visuals, fields, sort ------------------------------------------------------------


def test_the_matrix_request_from_the_chat_tests_adds_a_visual_and_renames_the_page() -> None:
    result = parse(
        "Add a matrix to this report with StoreName on rows, ChannelName on columns and "
        "Total Sales as the values, and rename the page to Agent Scorecard."
    )
    add, rename = result.ops
    assert add["op"] == "add_visual" and add["visual_type"] == "pivotTable"
    assert [f["name"] for f in add["fields"]] == ["StoreName", "ChannelName", "Total Sales"]
    assert add["page"] == PAGE
    assert rename == {"op": "rename_page", "page": PAGE, "name": "Agent Scorecard"}


def test_a_new_chart_is_titled_and_left_to_the_operation_to_place() -> None:
    clause = only_clause(parse("add a bar chart of Total Sales by channel"))
    op = clause.ops[0]
    assert op["visual_type"] == "clusteredBarChart" and op["title"] == "Total Sales by ChannelName"
    assert [f["name"] for f in op["fields"]] == ["Total Sales", "ChannelName"]
    # add_visual finds free space itself (it sees what the batch added before it)
    assert not {"x", "y", "width", "height"} & set(op)
    assert only_clause(parse("Make a table")).kind is ParseKind.CLARIFY
    placed = only_clause(parse("add a card for Profit at 100, 50"))
    assert (placed.ops[0]["x"], placed.ops[0]["y"]) == (100.0, 50.0)  # unless the person says
    sized = only_clause(parse("add a card for Profit 300 x 200"))
    assert (sized.ops[0]["width"], sized.ops[0]["height"]) == (300.0, 200.0)
    assert "x" not in sized.ops[0]


def test_page_size_is_sent_alone_because_the_operation_scales_the_visuals() -> None:
    clause = only_clause(parse("make the page 1280x720"))
    assert clause.kind is ParseKind.CLARIFY and "10 of the 15 visuals" in clause.question
    chosen = apply_choice(clause, 0, agents())
    # set_page_size scales every box (groups included) itself: no set_position per visual, and no
    # scale flag, so the operation's default scaling applies once
    assert chosen.ops == ({"op": "set_page_size", "page": PAGE, "width": 1280.0, "height": 720.0},)
    assert "scaled its 15 visuals" in chosen.summary
    inside = only_clause(parse("set the page size to 2400x1500"))
    assert inside.kind is ParseKind.APPLY  # nothing falls outside a bigger page
    assert inside.ops == ({"op": "set_page_size", "page": PAGE, "width": 2400.0, "height": 1500.0},)
    assert "scaled its 15 visuals with it" in inside.summary


def test_a_person_can_keep_the_visuals_where_they_are_when_the_page_changes() -> None:
    kept = only_clause(parse("make the page 2400 x 1500 but keep the visuals where they are"))
    assert kept.ops == (
        {"op": "set_page_size", "page": PAGE, "width": 2400.0, "height": 1500.0, "scale": False},
    )
    stuck = only_clause(parse("make the page 1280x720 but keep the visuals where they are"))
    assert stuck.kind is ParseKind.UNSUPPORTED and "cannot stay where they are" in stuck.reason
    assert not stuck.ops


def test_fields_and_sorting_use_the_visuals_own_bindings() -> None:
    added = only_clause(parse("add Profit to the table"))
    assert added.ops == (
        {
            "op": "set_fields",
            "visual": TABLE,
            "role": "Values",
            "mode": "add",
            "fields": [{"table": "# Measures", "name": "Profit"}],
        },
    )
    swap = parse("replace Rank with Profit in the table").ops
    assert [op["mode"] for op in swap] == ["remove", "add"]
    sort = parse("sort by TopN/BottomN/ MTD Sales ascending", agents(selection="b404a461")).ops
    assert sort[0]["direction"] == "ascending"
    assert sort[0]["field"] == {"table": "# Measures", "name": "TopN/BottomN/ MTD Sales"}


def test_layout_words_relate_visuals_to_each_other() -> None:
    swapped = parse("swap the top left chart and the top right chart").ops
    assert swapped == [{"op": "swap_visuals", "a": CHART, "b": CHART_TR}]
    beside = parse("put the ChannelName slicer next to the StoreName slicer").ops
    assert beside == [
        {
            "op": "place_visual",
            "visual": "b85aa81a8b5c60030d13",
            "relative_to": SLICER_STORE,
            "side": "right_of",
        }
    ]
    edge = parse("move it to the left edge", agents(selection="b404a461")).ops
    assert edge == [{"op": "set_position", "visual": CHART, "x": 0.0}]


# --- questions, what-ifs, models: nothing edits without a clear request --------------------------


@pytest.mark.parametrize(
    "message",
    [
        "what color is the title?",
        "is the legend on?",
        "does the chart have a legend?",
        "why are the bars blue?",
        "what would it look like green?",
        "what if the bars were green?",
        "I wonder if green bars would look better",
        "the bars are blue",
        "show me sales by channel",
        "thanks!",
        "how many agents hit the target?",
    ],
)
def test_questions_and_remarks_never_edit(message: str) -> None:
    result = parse(message, agents(selection="b404a461"))
    assert set(kinds(result)) == {"not_an_edit"} and result.ops == []
    assert result.remainder == message


def test_a_what_if_previews_and_a_change_of_a_measure_needs_confirmation() -> None:
    what_if = only_clause(parse("what if the target were 120K?"))
    assert what_if.kind is ParseKind.MODEL_EDIT and what_if.ops == ()
    # the dashboard's only fixed number is the 100K threshold, so "the target" finds that measure
    assert what_if.what_if is not None
    assert what_if.what_if.measure == "Number of Employees selling more than 100K MTD"
    assert ">= 120000" in what_if.what_if.new_expression and not what_if.needs_confirmation
    change = parse("Change the 100K target on that card to 120K. How many agents would make it?")
    edit, question = change.clauses
    assert edit.kind is ParseKind.MODEL_EDIT and edit.needs_confirmation
    assert edit.ops[0]["op"] == "update_measure"
    assert edit.ops[0]["name"] == "Number of Employees selling more than 100K MTD"
    assert ">= 120000" in edit.ops[0]["expression"] and "100000" not in edit.ops[0]["expression"]
    assert edit.what_if is not None and edit.what_if.measure == edit.ops[0]["name"]
    assert (
        question.kind is ParseKind.NOT_AN_EDIT
        and change.remainder == "How many agents would make it?"
    )
    assert change.ops == []  # a model edit is never part of the report-only revision
    assert ">= 120000" in only_clause(parse("Change the target to 120K")).ops[0]["expression"]


def test_creating_renaming_deleting_and_formatting_measures() -> None:
    create = only_clause(
        parse("create a measure called Profit Margin as DIVIDE([Profit],[Total Sales])")
    )
    assert create.ops == (
        {
            "op": "create_measure",
            "table": "# Measures",
            "name": "Profit Margin",
            "expression": "DIVIDE([Profit],[Total Sales])",
        },
    )
    assert (
        only_clause(parse("rename the measure Rank to Agent Rank")).ops[0]["new_name"]
        == "Agent Rank"
    )
    assert only_clause(parse("delete the measure Profit")).ops[0]["op"] == "delete_measure"
    fmt = only_clause(parse("format Total Sales as percent"))
    assert fmt.ops == ({"op": "update_measure", "name": "Total Sales", "format_string": "0.0%"},)
    assert all(c.needs_confirmation for c in (create, fmt))


def test_after_a_measure_change_captions_and_names_that_still_say_the_old_number_are_found() -> (
    None
):
    mentions = stale_literal_mentions("100K", agents(), "120K")
    caption = next(m for m in mentions if m.kind == "caption")
    assert caption.text == "Number of Agents Sold above 100K"
    assert caption.suggestion == "Number of Agents Sold above 120K"
    assert caption.ops == ()  # stale captions are advisory; edit them in Desktop
    measure = next(m for m in mentions if m.kind == "measure")
    assert measure.ops[0]["op"] == "rename_measure"
    assert measure.suggestion == "Number of Employees selling more than 120K MTD"
    assert {m.suggestion for m in stale_literal_mentions(100000, agents())} == {""}
    assert stale_literal_mentions("250K", agents()) == []


# --- undo family ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("undo", "undo"),
        ("undo that", "undo"),
        ("put it back", "undo"),
        ("go back to how it was", "undo"),
        ("redo", "redo"),
        ("restore the original", "restore"),
        ("Reset the report to the original please", "restore"),
        ("start over", "restore"),
        ("what did you change", "what_changed"),
        ("Just give me a summary of what you changed", "what_changed"),
    ],
)
def test_the_undo_family_is_deterministic(message: str, kind: str) -> None:
    result = parse(message)
    assert kinds(result) == [kind] and result.ops == [] and not result.unparsed


def test_undo_the_size_change_checks_the_last_edit() -> None:
    changed_size = LastEdit(
        "r4", ({"op": "set_position", "visual": CHART, "width": 500},), "Changed the chart size."
    )
    other = LastEdit(
        "r5", ({"op": "set_position", "visual": TABLE, "x": 900},), "Moved the table left."
    )
    context = agents()
    context.last_edit = changed_size
    assert kinds(parse("undo the size change", context)) == ["undo"]
    context.last_edit = other
    asked = only_clause(parse("undo the size change", context))
    assert asked.kind is ParseKind.CLARIFY and len(asked.choices) == 2
    assert apply_choice(asked, 0, context).kind is ParseKind.UNDO
    assert apply_choice(asked, 1, context).kind is ParseKind.WHAT_CHANGED
    context.last_edit = None
    assert only_clause(parse("undo the size change", context)).kind is ParseKind.CLARIFY


# --- vague requests ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "make it look nicer",
        "use a brand palette",
        "make the chart look more professional",
        "give the card rounded corners",
        "make the title blue",
        "the title should be blue",
        "could the title be blue?",
        "use Arial everywhere",
        "show data labels",
        "hide the legend",
        "add a shadow to the slicer",
        "copy the chart formatting to the table",
        "change the page background to white",
        "wrap the column headers",
        "band the rows",
        "remove the axis gridlines",
        "rename the chart to Revenue",
        "set the font to 14 pt",
        "make the Rank column wider",
        "turn off visual header icons",
        "translate every chart title to Spanish",
        "please convert the visual titles into German",
        "rewrite the card titles in Italian",
        "nudge the bars closer together",
        "give the bars a bit more breathing room",
        "increase the gap between the bars",
        "space the bars farther apart",
    ],
)
def test_styling_requests_are_short_refusals_with_no_substitute_edit(message: str) -> None:
    from ai_powerbi_analyst.studio.edit_capabilities import STYLING_REASON

    result = parse(message, agents(selection=CHART))
    clause = only_clause(result)
    assert clause.kind is ParseKind.UNSUPPORTED
    assert clause.reason == STYLING_REASON
    assert not result.ops and not result.unparsed and not clause.choices


# --- what Studio does not do ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "capability"),
    [
        ("add conditional formatting to the table", "conditional_formatting"),
        ("color the cards green if the value is above 100K", "conditional_formatting"),
        ("add a bookmark for this view", "bookmarks"),
        ("create a relationship between DimChannel and FactSales", "relationships"),
        ("set up row level security for each store", "rls"),
        ("refresh the data from the SQL server", "power_query"),
        ("publish this report to the Power BI service", "publishing"),
        ("add a trend line to the chart", "analytics_lines"),
        ("add a bulleted list to the text box", "textbox_rich_text"),
        ("add our logo to the top left", "images_shapes"),
        ("add a button that goes to page 2", "buttons_actions"),
        ("make the Rank column wider", "column_formatting"),
        ("group the cards together", "group_align_distribute"),
        ("ungroup the header block", "group_align_distribute"),
    ],
)
def test_out_of_scope_requests_are_refused_with_the_matrix_entry_and_desktop_steps(
    message: str, capability: str
) -> None:
    entry = CAPABILITY_MATRIX[capability]
    result = parse(message)
    if entry.status == "supported":
        # a tool family provides it now: the parser leaves it to the cards and the planner,
        # and never reads it as an operation that only looks similar
        assert not any(c.kind is ParseKind.UNSUPPORTED for c in result.clauses)
        assert result.unparsed or all(c.ops for c in result.clauses if c.kind is ParseKind.APPLY)
        return
    clause = only_clause(result)
    assert clause.kind is ParseKind.UNSUPPORTED and clause.capability == capability
    assert clause.reason and clause.desktop_steps == entry.desktop_steps and clause.desktop_steps
    assert entry.status in {"planned", "refused"}


def test_the_capability_matrix_lists_what_a_developer_asks_for() -> None:
    wanted = {
        "conditional_formatting",
        "textbox_rich_text",
        "buttons_actions",
        "bookmarks",
        "edit_interactions",
        "drillthrough_tooltip_pages",
        "sync_slicers",
        "analytics_lines",
        "visual_number_formats",
        "group_align_distribute",
        "format_painter",
        "column_formatting",
        "field_display_names",
        "alt_text",
        "mobile_layout",
        "calculated_columns",
        "calculated_tables",
        "relationships",
        "field_parameters",
        "calculation_groups",
        "rls",
        "power_query",
        "custom_visuals",
        "publishing",
        "sign_in",
        "data_gateways",
        "lock_objects",
        "visual_calculations",
        "small_multiples",
        "visual_header",
        "layer_order",
        "page_view",
        "model",
    }
    assert wanted <= set(CAPABILITY_MATRIX)
    for key in ("publishing", "sign_in", "data_gateways", "custom_visuals"):
        assert CAPABILITY_MATRIX[key].status == "refused"  # whatever a tool card says
    provided = _provided_by_cards()
    # query edits through Desktop are a slice a family provides; gateways and sign-in never
    assert CAPABILITY_MATRIX["power_query"].status == (
        "supported" if "power_query" in provided else "planned"
    )
    for entry in CAPABILITY_MATRIX.values():
        if entry.status != "supported":
            assert entry.reason and entry.desktop_steps, entry.key
        else:
            assert entry.family


# --- the chat's own wordings ---------------------------------------------------------------------


def test_data_requests_phrased_as_a_table_stay_questions_and_a_colon_ends_a_sentence() -> None:
    made = parse("Could you make a table of the top 5 agents by total sales?")
    assert kinds(made) == ["not_an_edit"] and made.remainder == "Show top 5 agents by total sales?"
    assert parse("Put the top 5 agents by total sales in a table").remainder == made.remainder
    scene = parse("Set the scene: which agents hit the 100K target this month?")
    assert set(kinds(scene)) == {"not_an_edit"} and not scene.unparsed
    assert [c.kind.value for c in parse("Add a table of sales by channel").clauses] == ["apply"]
    assert only_clause(parse("Please delete the slicer")).kind is ParseKind.CLARIFY


def test_sentences_and_forms() -> None:
    assert split_sentences("Set the scene: which agents win? Then rename it.") == [
        "Set the scene:",
        "which agents win?",
        "Then rename it.",
    ]
    assert classify("could the bars be green?").kind.value == "edit"
    assert classify("would the bars look better green?").kind.value == "question"
    assert classify("what if the target were 120K?").kind.value == "what_if"
    assert classify("the title is too small").body == "make the title bigger"
    assert classify("no legend please").body.startswith("remove legend")


# --- every operation is one report_edits can validate --------------------------------------------


def test_every_operation_and_choice_the_cases_produce_validates_and_applies_to_the_twin() -> None:
    run = _load("imported_edit_run", "run.py")
    checker = run.OperationChecker()
    seen = 0
    for case in run.load_cases():
        result = parse_edit_message(
            case["message"], contexts.context_for(case["context"], dict(case.get("options", {})))
        )
        for clause in result.clauses:
            assert checker.check(clause, case["context"]) == [], case["id"]
            seen += len(clause.ops)
    assert seen > 200


def test_ops_use_only_names_report_edits_knows() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(ReportOperation)
    for message in [
        "make the bars green",
        "hide it",
        "add a card for Profit",
        "rename page 1 to X",
    ]:
        for op in parse(message, agents(selection="b404a461")).ops:
            adapter.validate_python(op)


# --- context from an ImportedReport --------------------------------------------------------------


def test_the_context_reads_what_a_person_sees_and_tolerates_the_new_fields() -> None:
    context = agents()
    by_id = {v.id: v for v in context.visuals}
    assert by_id[CARD].caption == "Agents Avg MTD Sales" and by_id[CARD].fields == (
        "Employees Avg MTD Sales",
    )
    assert by_id["54be5cf0783e66471540"].caption == "Country"  # the header, not the field
    assert by_id["b85aa81a8b5c60030d13"].caption == "ChannelName"  # a slicer shows its field
    assert {"blue", "red"} <= set(by_id[CHART].hues)  # its TopN and BottomN fills
    assert by_id[CHART].categories == ("Others", "TopN", "BottomN")
    assert by_id[OVERLAY].z == 14000 and by_id[SLICER_TOPN].z == 1000
    visual = SimpleNamespace(
        id="v1",
        title="Sales",
        visual_type="cardVisual",
        x=1.0,
        y=2.0,
        width=30.0,
        height=40.0,
        bindings=[{"name": "Total"}],
        definition={},
        z=7,
        hidden=True,
        parent_group="g1",
        colors=["#FF0000", "#00FF00"],
    )
    report = SimpleNamespace(
        pages=[SimpleNamespace(id="p1", title="P", width=100.0, height=100.0, visuals=[visual])],
        measures=[{"Name": "M", "TableName": "T", "Expression": "1", "FormatString": "0"}],
        tables=[{"TableName": "T", "ColumnName": "C", "PandasDataType": "int64"}],
    )
    built = EditContext.from_report(report, selection="v1", palette={"brand": "#123456"})  # type: ignore[arg-type]
    row = built.visuals[0]
    assert (row.z, row.hidden, row.group, row.hues) == (7, True, "g1", ("red", "green"))
    assert built.measures[0].format_string == "0" and built.tables == {"T": ["C"]}


def test_hue_families() -> None:
    assert [hue_family(c) for c in ("#1E90FF", "#DC143C", "#A30024", "#b3b3b3", "#107C10")] == [
        "blue",
        "red",
        "red",
        "gray",
        "green",
    ]


# --- the 4B fallback tier ------------------------------------------------------------------------


def test_the_model_may_only_pick_a_closed_intent_indices_and_two_slots() -> None:
    assert "set_visual_property" not in MODEL_INTENTS and "set_theme" not in MODEL_INTENTS
    assert not any("measure" in name or "add_visual" in name for name in MODEL_INTENTS)
    for bad in (
        '{"intent":"set_visual_property"}',
        '{"intent":"set_theme","slots":["blue"]}',
        '{"intent":"add_visual"}',
        '{"intent":"create_measure"}',
        '{"intent":"set_color","slots":["a","b","c"]}',
        '{"intent":"set_color","targets":[1,2,3,4,5,6,7]}',
        '{"intent":"set_color","extra":"x"}',
        '{"intent":"unsupported"}',
    ):
        with pytest.raises(ValidationError):
            ModelIntent.model_validate_json(bad)
    forged = ModelIntent.model_construct(intent="set_theme", targets=[0], slots=["blue"])
    with pytest.raises(ValueError, match="may not choose"):
        resolve_model_intent(forged, agents())
    assert "untrusted" in MODEL_SYSTEM


def test_a_closed_intent_becomes_the_same_operations_the_parser_builds() -> None:
    context = agents()
    index = {row[2]: row[0] for row in model_index(context, "the top left chart")["v"]}
    number = index["Total MTD Sales"]
    hidden = resolve_model_intent(ModelIntent(intent="hide", targets=[number]), context)
    assert list(hidden.ops) == parse("hide Total MTD Sales").ops
    page = resolve_model_intent(
        ModelIntent(intent="rename_page", page=0, slots=["Overview"]), context
    )
    assert page.ops == ({"op": "rename_page", "page": PAGE, "name": "Overview"},)
    refused = resolve_model_intent(
        ModelIntent(intent="unsupported", capability="custom visuals from AppSource"), context
    )
    assert refused.kind is ParseKind.UNSUPPORTED and refused.capability == "custom_visuals"
    assert refused.desktop_steps
    asked = resolve_model_intent(
        ModelIntent(intent="clarify", options=[number, index["Country"]]), context
    )
    assert asked.kind is ParseKind.CLARIFY and len(asked.choices) == 2
    with pytest.raises(ValueError, match="not in the index"):
        resolve_model_intent(ModelIntent(intent="hide", targets=[99]), context)
    empty = resolve_model_intent(ModelIntent(intent="hide", targets=[]), context)
    assert empty.kind is ParseKind.CLARIFY


def test_the_index_stays_within_the_prompt_budget_and_never_truncates_silently() -> None:
    context = contexts.context_for("multipage", {})
    assert len(context.pages) == 5 and len(context.visuals) == 40
    for message in (
        "make the revenue by region chart on the forecast page nicer",
        "tidy up",
        "x" * 900,
    ):
        index = model_index(context, message)
        text = json.dumps(index, separators=(",", ":"), ensure_ascii=False)
        assert len(text.encode()) <= 3000 - len(MODEL_SYSTEM.encode())
        assert len(MODEL_SYSTEM.encode()) + len(text.encode()) <= 3000
        assert index["pages"] and 0 <= index["page"] < 5 and index["v"]
    scoped = model_index(context, "make the pie chart on the Forecast page bigger")
    assert scoped["page"] == 4  # page-scoped: only that page's visuals are listed
    listed = {row[0] for row in scoped["v"]}
    assert listed <= set(range(8))
    # a 60-visual page cannot be listed: with nothing named the parser asks, with something
    # named the omission is stated
    page = context.pages[0]
    extra = [
        type(context.visuals[0])(
            id=f"big-{i}",
            page_id=page.id,
            page_name=page.name,
            visual_type="cardVisual",
            caption=f"Metric number {i} for the regional summary",
            fields=(f"Metric {i}",),
            x=float(i % 10) * 100,
            y=float(i // 10) * 100,
            width=90.0,
            height=90.0,
        )
        for i in range(60)
    ]
    crowded = EditContext(
        pages=context.pages, visuals=extra, measures=[], tables={}, active_page=page.id
    )
    with pytest.raises(IndexTooLarge) as error:
        model_index(crowded, "make it prettier")
    assert (
        error.value.clause.kind is ParseKind.CLARIFY and "60 visuals" in error.value.clause.question
    )
    named = model_index(crowded, "make metric number 42 for the regional summary bigger")
    assert named["omitted"] > 0
    assert any(row[2].startswith("Metric number 42") for row in named["v"])
    ordered = page_visuals(crowded, page)
    # a number means the same visual whether or not the others were listed
    assert all(ordered[row[0]].caption.startswith(row[2]) for row in named["v"])


# --- review round 2: words that only look like commands, unresolved names, limits ----------------

LAST_COLOR = {
    "revision": "r0007",
    "ops": [{"op": "set_page_filter", "page": PAGE, "filter": {}}],
    "summary": "Made bars green",
}


@pytest.mark.parametrize(
    "text",
    [
        "Delete log",
        "Hide and seek",
        "Copy of Sales",
        "delete all visuals",
        "undo everything; restore the original",
        "Q1 sales, then Q2 sales",
        "Q1; Q2",
        "Restore the original",
        "Get rid of everything",
    ],
)
def test_a_new_name_is_data_and_never_read_as_a_command(text: str) -> None:
    result = parse(f'rename the page to "{text}"')
    clause = only_clause(result)
    assert clause.kind is ParseKind.APPLY
    assert clause.ops == ({"op": "rename_page", "page": PAGE, "name": text},)
    assert text in clause.summary and not result.unparsed


def test_a_page_name_and_a_name_followed_by_a_request_keep_each_part() -> None:
    page = only_clause(parse('rename the page to "Delete the bars"', agents(selection="b404a461")))
    assert page.ops == ({"op": "rename_page", "page": PAGE, "name": "Delete the bars"},)
    both = parse('rename the page to "Hide and seek" then make the table wider')
    assert kinds(both) == ["apply", "apply"]
    assert both.clauses[0].ops == ({"op": "rename_page", "page": PAGE, "name": "Hide and seek"},)
    assert both.clauses[1].ops[0]["visual"] == TABLE and "width" in both.clauses[1].ops[0]
    # a quote that names a visual still points at it
    pointed = only_clause(parse("make the 'Total MTD Sales' chart wider"))
    assert pointed.ops[0]["visual"] == CHART


@pytest.mark.parametrize(
    "message",
    [
        "delete Profit",
        "hide Profit",
        "delete the Zebra chart",
        "make the Zebra chart wider",
        "hide the Zebra card",
    ],
)
def test_a_name_the_report_does_not_know_never_falls_back_to_the_selection(message: str) -> None:
    result = parse(message, agents(selection="b404a461"))
    clause = only_clause(result)
    assert clause.kind is ParseKind.CLARIFY and result.ops == [] and not clause.ops
    assert "could not find" in clause.question
    assert not any(c.ops for c in clause.choices)


def test_everything_means_the_page_and_a_delete_of_most_of_it_is_confirmed_first() -> None:
    hidden = only_clause(parse("hide everything", agents(selection="b404a461")))
    assert hidden.kind is ParseKind.APPLY and len(hidden.ops) == 15
    delete = only_clause(parse("delete everything", agents(selection="b404a461")))
    assert delete.kind is ParseKind.CLARIFY and not delete.ops
    assert len(delete.choices[0].ops) == 15
    done = apply_choice(delete, 0, agents(selection="b404a461"))
    assert done.kind is ParseKind.APPLY and len(done.ops) == 15


def test_a_word_of_a_caption_finds_the_visual_and_a_synonym_keeps_the_type_group() -> None:
    assert (
        only_clause(parse("make the discount chart wider")).ops[0]["visual"].startswith("97d15040")
    )
    assert only_clause(parse("make the store slicer smaller")).ops[0]["visual"] == SLICER_STORE
    # "agent" is a word of other captions, so it does not block "the agent table"
    assert only_clause(parse("delete the agent table")).ops[0]["visual"] == TABLE
    assert only_clause(parse("make the chart taller", agents(selection="b404a461"))).ops


@pytest.mark.parametrize(
    ("message", "ids"),
    [
        ("make the left charts wider", {CHART, "97d15040"}),
        ("make the bottom charts taller", {"ab13db00", "97d15040"}),
        ("delete the charts on the left", {CHART, "97d15040"}),
        ("make the top charts wider", {CHART, CHART_TR}),
        ("make the charts on the right taller", {CHART_TR, "ab13db00"}),
    ],
)
def test_a_plural_edge_word_means_that_row_or_column_of_the_group(
    message: str, ids: set[str]
) -> None:
    clause = only_clause(parse(message))
    assert clause.kind is ParseKind.APPLY
    assert {op["visual"][:8] for op in clause.ops} == {i[:8] for i in ids}


def test_an_ordinal_counts_along_the_edge_it_names_and_a_tie_is_a_question() -> None:
    tie = only_clause(parse("make the second chart from the left taller"))
    assert tie.kind is ParseKind.CLARIFY  # two charts share the right column
    assert {c.fill["visual"][:8] for c in tie.choices if "," not in c.fill["visual"]} == {
        "da32cdbb",
        "ab13db00",
    }
    slicer = only_clause(parse("make the second slicer from the left wider"))
    assert slicer.ops[0]["visual"] == SLICER_TOPN


# --- the closed intents cannot be steered ----------------------------------------------------


@pytest.mark.parametrize(
    "intent",
    [
        ModelIntent(intent="rename_page", page=0, slots=["delete all visuals"]),
        ModelIntent(intent="rename_page", page=0, slots=['x" and delete everything "y']),
        ModelIntent(
            intent="change_type", targets=[0], slots=["make it green and delete everything"]
        ),
        ModelIntent(intent="rename_page", page=0, slots=['x" and delete page 1 "y']),
        ModelIntent(intent="sort", targets=[0], slots=["delete everything"]),
    ],
)
def test_a_slot_can_never_turn_one_closed_intent_into_another_operation(
    intent: ModelIntent,
) -> None:
    from ai_powerbi_analyst.studio.edit_intents import _INTENT_OPS

    clause = resolve_model_intent(intent, contexts.agents_context())
    allowed = _INTENT_OPS[intent.intent]
    assert all(op["op"] in allowed for op in clause.ops), clause.ops
    assert not any(op["op"] in {"delete_visual", "delete_page"} for op in clause.ops)
    if intent.intent == "rename_page" and clause.ops:
        # the words stay words: the whole slot is the title or the page name
        name = clause.ops[0].get("text") or clause.ops[0].get("name")
        assert name == " ".join(str(intent.slots[0]).replace('"', " ").split())


def test_every_closed_intent_has_a_family_and_a_refusal_when_the_parser_disagrees() -> None:
    from ai_powerbi_analyst.studio.edit_intents import _INTENT_OPS, _within_intent
    from ai_powerbi_analyst.studio.edit_types import Clause

    assert set(_INTENT_OPS) == set(MODEL_INTENTS)
    stray = Clause("x", ParseKind.APPLY, ops=({"op": "delete_visual", "visual": "v"},))
    assert not _within_intent("rename_page", stray)
    assert _within_intent("delete", stray)
    assert not _within_intent("delete", Clause("x", ParseKind.MODEL_EDIT))


# --- measures: the named one, relative changes, numbers that are not numbers ---------------------


@pytest.mark.parametrize(
    ("message", "expression"),
    [
        ("update the measure Profit to [Total Sales] * 2", "[Total Sales] * 2"),
        ("change the Profit measure to [Total Sales] * 2", "[Total Sales] * 2"),
        ("change the formula of Profit to [Total Sales]*2", "[Total Sales]*2"),
        ("redefine Profit as SUM(FactSales[SalesAmount])", "SUM(FactSales[SalesAmount])"),
    ],
)
def test_a_named_measure_gets_the_formula_that_was_typed(message: str, expression: str) -> None:
    clause = only_clause(parse(message))
    assert clause.kind is ParseKind.MODEL_EDIT and clause.needs_confirmation
    assert clause.ops == ({"op": "update_measure", "name": "Profit", "expression": expression},)


@pytest.mark.parametrize(
    ("message", "number"),
    [
        ("lower the target by 10K", "90000"),
        ("increase the target by 20%", "120000"),
        ("raise the target by 10%", "110000"),
        ("change the target from 100K to 120K", "120000"),
        ("change the 100K to 120K", "120000"),
        ("Change the target to 120K", "120000"),
    ],
)
def test_a_relative_change_is_computed_from_the_current_number(message: str, number: str) -> None:
    clause = only_clause(parse(message))
    assert clause.kind is ParseKind.MODEL_EDIT
    assert clause.ops[0]["name"] == "Number of Employees selling more than 100K MTD"
    assert (
        f">= {number}" in clause.ops[0]["expression"]
        and "100000" not in clause.ops[0]["expression"]
    )


@pytest.mark.parametrize(
    "message",
    [
        "change the target to 1e400",
        "change the target to 120000000000000000000000000",
        "change the Profit measure to 5",
        "change it to 120K",
    ],
)
def test_a_number_that_is_not_usable_or_a_measure_with_no_number_is_a_question(
    message: str,
) -> None:
    clause = only_clause(parse(message))
    assert clause.kind is ParseKind.CLARIFY and not clause.ops


def test_the_target_is_the_measure_that_says_so_when_the_model_has_one() -> None:
    from ai_powerbi_analyst.studio.edit_context import MeasureEntry

    context = agents()
    context.measures.append(MeasureEntry("Sales Target", "# Measures", "100000"))
    for message, number in (
        ("change the target from 100K to 120K", "120000"),
        ("lower the target by 10K", "90000"),
        ("Change the target to 120K", "120000"),
    ):
        clause = only_clause(parse(message, context))
        assert clause.ops == (
            {"op": "update_measure", "name": "Sales Target", "expression": number},
        ), message
    # the number alone finds every holder; "on that card" prefers the one a card shows
    both = only_clause(parse("change the 100K to 120K", context))
    assert both.kind is ParseKind.CLARIFY and not both.ops
    assert {c.fill["measure"] for c in both.choices} == {
        "Sales Target",
        "Number of Employees selling more than 100K MTD",
    }
    on_card = only_clause(parse("Change the 100K target on that card to 120K", context))
    assert on_card.ops[0]["name"] == "Number of Employees selling more than 100K MTD"
    none = only_clause(parse("change the target from 250K to 300K", agents()))
    assert none.kind is ParseKind.CLARIFY and "No measure has 250K" in none.question


def test_change_it_to_a_number_on_a_selected_card_finds_the_measure_it_shows() -> None:
    clause = only_clause(parse("change it to 120K", agents(selection="71a16476")))
    assert clause.kind is ParseKind.MODEL_EDIT and ">= 120000" in clause.ops[0]["expression"]


@pytest.mark.parametrize(
    ("message", "name", "expression"),
    [
        (
            "create a measure called Foo = SUM(FactSales[SalesAmount])",
            "Foo",
            "SUM(FactSales[SalesAmount])",
        ),
        (
            "create a measure named Foo as SUM(FactSales[SalesAmount])",
            "Foo",
            "SUM(FactSales[SalesAmount])",
        ),
        ("new measure Foo = 1", "Foo", "1"),
    ],
)
def test_a_new_measure_name_ends_where_its_definition_begins(
    message: str, name: str, expression: str
) -> None:
    clause = only_clause(parse(message))
    assert clause.kind is ParseKind.MODEL_EDIT
    assert clause.ops[0]["name"] == name and clause.ops[0]["expression"] == expression


@pytest.mark.parametrize(
    "message",
    [
        "Create a measure called QA Discount Share that is total DiscountAmount "
        "divided by Total Sales",
        "Create a measure called 'QA Discount Share' which divides the discount amount by sales",
        "Add a measure named Delete Log that counts the discount rows",
        "Make a new measure called Avg Discount which averages DiscountAmount",
        "Create a measure called Men's Sales that sums SalesAmount",
        "Build a measure named Cost Share that is TotalCost as a percentage of SalesAmount",
    ],
)
def test_a_new_measure_calculation_description_is_left_to_local_drafting(message: str) -> None:
    result = parse(message)
    assert result.unparsed
    assert not result.ops
    assert not any(clause.kind is ParseKind.CLARIFY for clause in result.clauses)
    assert "that" in result.unparsed[0] or "which" in result.unparsed[0]


@pytest.mark.parametrize(
    ("message", "name"),
    [
        ("Create a measure called Sales YTD that I can use later", "Sales YTD"),
        ("Make a measure called Sales YTD that we need for the dashboard", "Sales YTD"),
        ("Create a measure called Sales YTD which is important", "Sales YTD"),
        ("Add a measure named Board Number that the CFO asked for", "Board Number"),
        (
            "Create a measure called 'QA Discount Share' which calculates a custom adjustment",
            "QA Discount Share",
        ),
    ],
)
def test_words_after_that_which_describe_no_calculation_ask_instead_of_drafting(
    message: str, name: str
) -> None:
    # "that I can use later" is no calculation: nothing is drafted from it, the user is asked
    result = parse(message)
    assert not result.unparsed
    clause = only_clause(result)
    assert clause.kind is ParseKind.CLARIFY and f"[{name}]" in clause.question


@pytest.mark.parametrize(
    "message",
    [
        "add a role Online Team that can only see the Online channel",
        "create a security role Online only that only sees the Online channel",
        "create a role Web Staff restricted to the Online channel",
        "add an RLS role Store Team that sees only the Store channel",
        "set up a row-level security role for the Online channel",
    ],
)
def test_a_role_request_is_never_read_as_a_filter_on_the_page(message: str) -> None:
    # what a role's members see is the role's row filter, left to the planner and the role card
    result = parse(message)
    assert not result.ops
    assert result.unparsed


@pytest.mark.parametrize(
    "message",
    [
        "only show the Online channel on this page",
        "filter this page to the Online channel",
    ],
)
def test_a_page_filter_without_a_role_is_still_a_page_filter(message: str) -> None:
    result = parse(message)
    assert [op["op"] for op in result.ops] == ["set_page_filter"]


def test_a_capitalized_word_after_that_stays_part_of_the_name() -> None:
    # "Sales that Matter" is a name, not "Sales" with the calculation "Matter"
    from ai_powerbi_analyst.studio.edit_text import new_measure_calculation

    assert new_measure_calculation("Create a measure called Sales that Matter") is None
    assert new_measure_calculation(
        "Create a measure called Sales that Matter that sums SalesAmount"
    ) == ("Sales that Matter", "sums SalesAmount")


def test_a_quoted_measure_name_that_contains_definition_words_remains_a_name() -> None:
    clause = only_clause(parse('Create a measure called "Share that is total"'))
    assert clause.kind is ParseKind.CLARIFY
    assert "[Share that is total]" in clause.question


# --- undo ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "undo the size change",
        "undo the title change",
        "undo the deletion",
        "undo the legend change",
    ],
)
def test_undoing_a_kind_of_change_the_last_edit_was_not_asks_first(message: str) -> None:
    context = agents(last_edit=LAST_COLOR)
    clause = only_clause(parse(message, context))
    assert clause.kind is ParseKind.CLARIFY and "not" in clause.question
    assert [c.fill["kind"] for c in clause.choices] == ["undo", "what_changed"]
    assert only_clause(parse("undo the filter change", context)).kind is ParseKind.UNDO


def test_undo_many_and_revert_everything() -> None:
    context = agents(last_edit=LAST_COLOR)
    many = only_clause(parse("undo the last two changes", context))
    assert many.kind is ParseKind.CLARIFY and "one change at a time" in many.question
    assert apply_choice(many, 2, context).kind is ParseKind.RESTORE
    assert kinds(parse("revert everything", context)) == ["restore"]
    assert kinds(parse("undo everything", context)) == ["restore"]


def test_an_undo_next_to_a_change_is_its_own_clause_and_never_dropped() -> None:
    context = agents(last_edit=LAST_COLOR, selection="b404a461")
    first = parse("undo, then make the chart wider", context)
    assert kinds(first) == ["undo", "apply"]
    assert first.clauses[1].ops[0]["op"] == "set_position"
    assert first.clauses[1].ops[0]["visual"] == CHART
    for message in (
        "Make the chart wider and undo that",
        "Make the chart wider then undo",
        "Make the chart wider. Then undo that.",
    ):
        clause = only_clause(parse(message, context))
        assert clause.kind is ParseKind.CLARIFY and "undo" in clause.question
        assert [c.label for c in clause.choices][-1] == "Only undo the latest change"
    only = apply_choice(
        only_clause(parse("Make the chart wider and undo that", context)), 0, context
    )
    assert only.kind is ParseKind.APPLY and only.ops[0]["op"] == "set_position"


# --- pages ---------------------------------------------------------------------------------------


def test_a_page_or_visual_named_like_a_feature_is_a_name_not_a_request_for_it() -> None:
    context = contexts.multipage_context()
    hide = only_clause(parse("hide the Forecast page", context))
    assert hide.ops == ({"op": "set_page_hidden", "page": "page4", "hidden": True},)
    rename = only_clause(parse("rename Forecast to Outlook", context))
    assert rename.ops[0]["name"] == "Outlook"
    move = only_clause(parse("move Forecast before Product Mix", context))
    assert move.ops == ({"op": "move_page", "page": "page4", "index": 2},)
    taken = only_clause(parse("rename the Forecast page to Product Mix", context))
    assert taken.kind is ParseKind.UNSUPPORTED and "already a page" in taken.reason
    # a real request for one is still one: refused, or the tool cards' once a family has it
    line = parse("add a forecast line to the chart", context)
    if "analytics_lines" in _provided_by_cards():
        assert line.unparsed == ("add a forecast line to the chart",) and not line.ops
    else:
        clause = only_clause(line)
        assert clause.kind is ParseKind.UNSUPPORTED and clause.capability == "analytics_lines"


def test_cards_added_to_a_named_page_all_go_there_and_do_not_stack() -> None:
    context = contexts.multipage_context()
    result = parse("add a card for Measure 1 and a card for Measure 2 to page Forecast", context)
    assert kinds(result) == ["apply", "apply"]
    first, second = (c.ops[0] for c in result.clauses)
    assert first["page"] == second["page"] == "page4"
    cards = parse("add a card for Profit and a card for Total Sales and a card for Rank")
    # one add_visual each, in order: each is placed in free space after the one before it
    assert [c.ops[0]["fields"][0]["name"] for c in cards.clauses] == [
        "Profit",
        "Total Sales",
        "Rank",
    ]
    assert not any({"x", "y"} & set(c.ops[0]) for c in cards.clauses)


def test_adding_to_a_new_page_adds_the_page_first_in_the_same_revision() -> None:
    clause = only_clause(parse("add a bar chart of Profit by ChannelName to a new page"))
    assert [op["op"] for op in clause.ops] == ["add_page", "add_visual"]
    assert clause.ops[1]["page"] == clause.ops[0]["name"] == "Page 2"
    assert "new page" in clause.summary


def test_deleting_many_pages_names_the_ones_kept_and_asks_before_deleting() -> None:
    context = contexts.multipage_context()
    keep_first = only_clause(parse("delete every page but the first", context))
    assert keep_first.kind is ParseKind.CLARIFY and not keep_first.ops
    assert "keeps 'Executive Summary'" in keep_first.question
    done = apply_choice(keep_first, 0, context)
    assert [op["page"] for op in done.ops] == ["page1", "page2", "page3", "page4"]
    both = only_clause(parse("delete every page except Forecast and the second", context))
    assert [op["page"] for op in both.choices[0].ops] == ["page0", "page2", "page3"]
    every = only_clause(parse("delete all the pages", context))
    assert every.kind is ParseKind.CLARIFY and len(every.choices) == 5
    again = apply_choice(every, 1, context)  # "keep the second page" asks for the final yes
    assert again.kind is ParseKind.CLARIFY and len(again.choices[0].ops) == 4
    for message in ("delete page -1", "delete page 0"):
        assert only_clause(parse(message, context)).kind is ParseKind.UNSUPPORTED
    assert "no second page" in only_clause(parse("delete the second page")).reason


@pytest.mark.parametrize(
    "message",
    [
        "add a title to the page",
        "add a page title",
        "give the page a title",
        "add a heading to this page",
    ],
)
def test_a_title_for_the_page_is_never_a_rename_or_a_new_page(message: str) -> None:
    """A title that shows on the page is a text box: asked for its words, never applied blind."""
    clause = only_clause(parse(message))
    assert clause.kind is ParseKind.CLARIFY and not clause.ops
    assert "What should the title say" in clause.question
    named = only_clause(parse("title the page Sales Overview"))
    assert named.ops == ({"op": "rename_page", "page": PAGE, "name": "Sales Overview"},)
    said = only_clause(parse("add a title to the page saying Agents Dashboard"))
    assert said.ops == (
        {
            "op": "add_textbox",
            "page": PAGE,
            "text": "Agents Dashboard",
            "x": 11.52,
            "y": 11.52,
            "width": 1896.96,
            "height": 50.0,
        },
    )


def test_page_sizes_are_read_whole_and_checked_against_what_power_bi_allows() -> None:
    big = only_clause(parse("make the page 16000 x 9000"))
    assert big.ops[0]["width"] == 16000.0 and big.ops[0]["height"] == 9000.0
    huge = only_clause(parse("set the page size to 100000 x 100000"))
    assert huge.kind is ParseKind.CLARIFY and not huge.ops
    assert all(not c.ops for c in huge.choices)


# --- wordings that only sound different -----------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "get rid of the table",
        "kill the table",
        "lose the table",
        "scrap the table",
        "the table should be deleted",
        "the table needs to go",
    ],
)
def test_other_ways_to_say_delete_are_deletes(message: str) -> None:
    clause = only_clause(parse(message))
    assert clause.ops == ({"op": "delete_visual", "visual": TABLE},)


def test_removal_words_work_on_parts_and_go_away_hides() -> None:
    context = agents(selection="b404a461")
    assert only_clause(parse("ditch the legend", context)).kind is ParseKind.UNSUPPORTED
    both = only_clause(parse("get rid of the legend and the gridlines", context))
    assert both.kind is ParseKind.UNSUPPORTED and not both.ops
    assert only_clause(parse("zap the title", context)).kind is ParseKind.UNSUPPORTED
    away = only_clause(parse("make the chart go away", context))
    assert away.ops == ({"op": "set_visual_hidden", "visual": CHART, "hidden": True},)
    grumble = only_clause(parse("I don't like the bars, make them blue", context))
    assert grumble.kind is ParseKind.UNSUPPORTED and not grumble.ops


def test_a_negative_order_turns_the_thing_off_and_a_negative_question_is_not_an_edit() -> None:
    context = agents(selection="b404a461")
    legend = only_clause(parse("don't show the legend", context))
    assert legend.kind is ParseKind.UNSUPPORTED and not legend.ops
    labels = only_clause(parse("do not show data labels", context))
    assert labels.kind is ParseKind.UNSUPPORTED and not labels.ops
    data = parse("don't show me the data", context)
    assert kinds(data) == ["not_an_edit"] and data.ops == []


def test_a_refused_sentence_with_and_leaves_no_fragment_for_the_model() -> None:
    for message, key in (
        ("add a relationship between the store table and the sales table", "relationships"),
        ("enable cross-filtering between the cards and the chart", "edit_interactions"),
        ("add a relationship between DimStore and FactSales", "relationships"),
    ):
        result = parse(message)
        if key in _provided_by_cards():
            # supported now: the whole sentence goes on as one piece, not its fragments
            assert result.unparsed == (message,) and not result.ops
            continue
        assert kinds(result) == ["unsupported"] and result.clauses[0].capability == key
        assert not result.unparsed


def test_each_side_with_its_own_value_is_a_clause_and_a_target_named_last_is_carried_back() -> None:
    two = parse("make Total MTD Sales wider and make Avg MTD Sales by Agents taller")
    assert [c.ops[0]["visual"] for c in two.clauses] == [CHART, CHART_TR]
    assert "width" in two.clauses[0].ops[0] and "height" in two.clauses[1].ops[0]
    carried = parse("make it wider and taller", agents(selection=CHART))
    assert all(op["visual"] == CHART for op in carried.ops)
    assert carried.ops[0]["width"] > agents().visual(CHART).width
    assert carried.ops[0]["height"] > agents().visual(CHART).height


def test_copies_percentages_and_limits() -> None:
    three = only_clause(parse("duplicate the table 3 times"))
    assert [op["op"] for op in three.ops] == ["duplicate_visual"] * 3 and "3 times" in three.summary
    assert len(only_clause(parse("make 3 copies of the table")).ops) == 3
    assert len(only_clause(parse("duplicate the table twice")).ops) == 2
    assert only_clause(parse("duplicate the table 50 times")).kind is ParseKind.CLARIFY
    assert only_clause(parse("make the table -50% wider")).kind is ParseKind.CLARIFY
    assert only_clause(parse("make the table 50% narrower")).ops[0]["width"] == 447.0
    assert only_clause(parse("make the table 20% wider")).kind is ParseKind.APPLY


def test_unusable_messages_are_answered_fast_and_without_an_error() -> None:
    import time

    started = time.perf_counter()
    for message in (
        "make the font size " + "1 " * 5000 + "pt bigger",
        "make the bars green" * 2000,
        "add a card for Profit " * 90,
        "make the bars green, " * 60,
    ):
        result = parse(message)
        assert not result.ops and result.clauses
        assert result.clauses[0].kind in {ParseKind.CLARIFY, ParseKind.UNSUPPORTED}
    assert time.perf_counter() - started < 3.0
    assert kinds(parse_edit_message(None, agents())) == ["not_an_edit"]  # type: ignore[arg-type]
    assert kinds(parse("   ")) == ["not_an_edit"]
    name = only_clause(parse("rename page to " + "x" * 150))
    assert name.kind is ParseKind.CLARIFY and "at most 100" in name.question and not name.ops
    title = only_clause(parse("rename the chart to " + "y" * 400, agents(selection="b404a461")))
    assert title.kind is ParseKind.UNSUPPORTED and not title.ops


def test_no_operation_leaves_the_parser_that_report_edits_would_refuse() -> None:
    adapter: Any = TypeAdapter(ReportOperation)
    for message in (
        "make the page 16000 x 9000",
        "set the page size to 100000 x 100000",
        "duplicate the table 3 times",
        "add a bar chart of Profit by ChannelName to a new page",
        "delete every page but the first",
        "rename page to " + "x" * 150,
    ):
        for clause in parse(message).clauses:
            for made in (*clause.ops, *(op for c in clause.choices for op in c.ops)):
                adapter.validate_python(made)


# --- the eval gates ------------------------------------------------------------------------------


def test_the_case_file_meets_the_ship_gates() -> None:
    run = _load("imported_edit_run", "run.py")
    cases = run.load_cases()
    assert len(cases) >= 60
    kinds_seen = {c["kind"] for case in cases for c in case["clauses"]}
    assert kinds_seen >= {
        "apply",
        "clarify",
        "unsupported",
        "undo",
        "redo",
        "restore",
        "what_changed",
        "model_edit",
        "not_an_edit",
    }
    report = run.evaluate(cases)
    assert report["gates"] == {
        "coverage>=90%": True,
        "wrong_targets==0": True,
        "unsupported==100%": True,
        "operations_valid": True,
        "cases_match": True,
    }
    assert report["wrong_target_cases"] == 0 and report["false_edits"] == 0
    assert report["unsupported_accuracy"] == 1.0 and report["mismatches"] == 0, report["findings"]
    assert report["coverage"] >= 0.9
    assert report["latency_ms"]["p95"] < 500  # the deterministic tier is instant next to the model


def test_the_runner_counts_a_case_that_needs_the_model() -> None:
    run = _load("imported_edit_run", "run.py")
    case = {
        "id": "n",
        "context": "agents",
        # Styling is now deterministic. This mixed request still leaves its units clause
        # to the model, so a partial styling refusal must not count as full coverage.
        "message": "add data labels to the Total MTD Sales chart and show them in thousands",
        "clauses": [{"kind": "unsupported", "capability": "styling"}],
        "needs_model": True,
    }
    report = run.evaluate([case])
    assert report["coverage"] == 0.0 and report["mismatches"] == 0 and not report["passed"]


# --- integration: the layout, object, color and filter operations --------------------------------

NEW_OPERATIONS = {
    "align_visuals",
    "distribute_visuals",
    "swap_visuals",
    "place_visual",
    "add_textbox",
    "add_shape",
    "set_visual_filter",
    "set_page_filter",
    "set_report_filter",
    "clear_filter",
}


def test_the_case_files_hold_the_new_operations_and_no_id_twice() -> None:
    run = _load("imported_edit_run", "run.py")
    cases = run.load_cases()
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    added = run.load_cases(EVALS / "cases_ops.jsonl")
    assert len(added) >= 60
    used = {
        op["op"] for case in added for clause in case["clauses"] for op in clause.get("ops", [])
    }
    assert used >= NEW_OPERATIONS, sorted(NEW_OPERATIONS - used)
    kinds_added = {clause["kind"] for case in added for clause in case["clauses"]}
    assert {"apply", "clarify", "unsupported", "not_an_edit"} <= kinds_added


def _provided_by_cards() -> set[str]:
    """The matrix entries the registered tool cards provide (a family may be registered after
    these tests were written: what it provides is supported, the rest stays planned)."""
    from ai_powerbi_analyst.studio import edit_tools
    from ai_powerbi_analyst.studio.edit_capabilities import card_capability

    known = edit_tools.registered_operations()
    return {card_capability(c) for c in edit_tools.tool_cards() if c.op in known}


def test_the_matrix_says_what_is_supported_now_and_what_is_still_planned() -> None:
    supported = {"filters", "text_boxes", "shapes", "align_distribute"}
    assert all(CAPABILITY_MATRIX[key].status == "supported" for key in supported)
    assert all(CAPABILITY_MATRIX[key].family for key in supported)
    planned = {
        "conditional_formatting",
        "textbox_rich_text",
        "images_shapes",
        "group_align_distribute",
    }
    provided = _provided_by_cards()
    for key in planned:
        assert CAPABILITY_MATRIX[key].status == ("supported" if key in provided else "planned")
    # a supported request is no longer refused: nothing in it names a planned feature
    for message in (
        "only show 2019",
        "add a red circle",
        "duplicate the chart",
        "align the cards",
    ):
        assert only_clause(parse(message)).kind is not ParseKind.UNSUPPORTED


def test_several_visuals_placed_by_one_go_in_a_row_or_a_column_and_the_edge_one_is_the_anchor() -> (
    None
):
    below = only_clause(parse("put the two top charts below the ChannelName slicer"))
    assert [(op["visual"], op["relative_to"], op["side"]) for op in below.ops] == [
        (CHART, "b85aa81a8b5c60030d13", "below"),
        (CHART_TR, CHART, "right_of"),  # the second one goes beside the first, not on it
    ]
    beside = only_clause(parse("put the two top charts to the left of the table"))
    assert [op["side"] for op in beside.ops] == ["left_of", "below"]
    # "below the cards" is below the lowest card: the one that sets the edge is the anchor
    table = only_clause(parse("move the table below the cards"))
    lowest = max(
        (v for v in agents().visuals if v.caption and v.visual_type == "cardVisual"),
        key=lambda v: v.y + v.height,
    )
    assert table.ops[0]["relative_to"] == lowest.id and table.ops[0]["side"] == "below"
    spaced = only_clause(parse("put the table 30 px below the cards"))
    assert spaced.ops[0]["gap"] == 30.0


def test_aligning_with_one_of_several_asks_and_every_answer_is_an_operation() -> None:
    clause = only_clause(parse("align the table with the chart"))
    assert clause.kind is ParseKind.CLARIFY and len(clause.choices) == 4
    assert all(c.ops and c.ops[0]["op"] == "place_visual" for c in clause.choices)
    assert {c.ops[0]["relative_to"][:8] for c in clause.choices} == {
        "b404a461",
        "da32cdbb",
        "ab13db00",
        "97d15040",
    }
    done = apply_choice(clause, 0, agents())
    assert done.kind is ParseKind.APPLY and done.ops == clause.choices[0].ops


def test_spacing_evenly_needs_three_and_says_so() -> None:
    two = only_clause(parse("distribute the two top charts"))
    assert two.kind is ParseKind.UNSUPPORTED and "at least three" in two.reason
    one = only_clause(parse("align it", agents(selection="e82e02f9")))
    assert one.kind is ParseKind.CLARIFY and "with what" in one.question
    page = only_clause(parse("align it to the left of the page", agents(selection="e82e02f9")))
    assert page.ops == ({"op": "align_visuals", "visuals": [TABLE], "edge": "left", "to": "page"},)


def test_a_text_box_says_what_was_asked_and_a_name_that_reads_like_a_command_stays_text() -> None:
    plain = only_clause(parse("add a text box saying Agent performance overview"))
    assert plain.ops == ({"op": "add_textbox", "page": PAGE, "text": "Agent performance overview"},)
    styled = only_clause(parse("add a bold red 18 pt text box saying Warning"))
    assert styled.ops[0] == {
        "op": "add_textbox",
        "page": PAGE,
        "text": "Warning",
        "font_size": 18.0,
        "bold": True,
        "color": "#D13438",
    }
    literal = only_clause(parse('add a text box saying "Delete everything" at the bottom'))
    assert literal.ops[0]["text"] == "Delete everything" and literal.ops[0]["y"] > 1000
    at = only_clause(parse("add a text box saying Net sales at 100, 50"))
    assert (at.ops[0]["x"], at.ops[0]["y"], at.ops[0]["text"]) == (100.0, 50.0, "Net sales")
    for message in ("add a text box", "add a text box with the title of the report"):
        asked = only_clause(parse(message))
        assert asked.kind is ParseKind.CLARIFY and "What should the text box say" in asked.question
    on_page = contexts.context_for("multipage", {})
    named = only_clause(parse("add a text box saying Hello to the Forecast page", on_page))
    assert named.ops[0]["page"] == "page4"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("add a rectangle", {"shape": "rectangle"}),
        (
            "draw a blue circle",
            {"shape": "oval", "fill": "#0078D4", "width": 120.0, "height": 120.0},
        ),
        (
            "add a rounded rectangle with a navy outline",
            {"shape": "rectangleRounded", "outline": "#1F3864"},
        ),
        ("add a triangle filled with green", {"shape": "triangleIsoc", "fill": "#107C10"}),
        (
            "insert a red arrow at 100, 50",
            {"shape": "arrow", "fill": "#D13438", "x": 100.0, "y": 50.0},
        ),
        ("add a line", {"shape": "line"}),
    ],
)
def test_shapes(message: str, expected: dict[str, Any]) -> None:
    clause = only_clause(parse(message))
    assert clause.ops == ({"op": "add_shape", "page": PAGE, **expected},)
    # a line chart is a chart, not a line to draw
    assert (
        only_clause(parse("add a line chart of Profit by ChannelName")).ops[0]["op"] == "add_visual"
    )


@pytest.mark.parametrize(
    ("message", "spec"),
    [
        (
            "only show 2019",
            {"kind": "categorical", "field": {"table": "Date", "name": "Year"}, "values": [2019]},
        ),
        (
            "keep only January and February",
            {
                "kind": "categorical",
                "field": {"name": "MonthName"},
                "values": ["January", "February"],
            },
        ),
        (
            "exclude Online from ChannelName",
            {"kind": "categorical", "field": {"name": "ChannelName"}, "exclude": True},
        ),
        (
            "limit this page to the last 6 months",
            {"kind": "relative_date", "unit": "months", "direction": "last", "count": 6},
        ),
        (
            "only show last month",
            {"kind": "relative_date", "unit": "calendar_months", "direction": "last"},
        ),
        (
            "only show next 2 weeks",
            {"kind": "relative_date", "unit": "weeks", "direction": "next", "count": 2},
        ),
        (
            "only show total sales over 100K",
            {"kind": "advanced", "conditions": [{"operator": "greater_than", "value": 100000}]},
        ),
        (
            "only show total sales under 5000",
            {"kind": "advanced", "conditions": [{"operator": "less_than", "value": 5000}]},
        ),
        (
            "filter Year to between 2018 and 2020",
            {
                "kind": "advanced",
                "conditions": [
                    {"operator": "greater_or_equal", "value": 2018},
                    {"operator": "less_or_equal", "value": 2020},
                ],
            },
        ),
    ],
)
def test_filters_read_the_field_the_values_and_the_period(
    message: str, spec: dict[str, Any]
) -> None:
    clause = only_clause(parse(message))
    assert clause.kind is ParseKind.APPLY and len(clause.ops) == 1
    made = clause.ops[0]
    assert made["op"] == "set_page_filter" and made["page"] == PAGE
    for key, value in spec.items():
        if isinstance(value, dict):
            assert value.items() <= made["filter"][key].items()
        else:
            assert made["filter"][key] == value


def test_a_filter_goes_where_the_message_says_and_a_top_n_belongs_to_a_visual() -> None:
    report = only_clause(parse("filter the whole report to 2019"))
    assert report.ops[0]["op"] == "set_report_filter" and "page" not in report.ops[0]
    visual = only_clause(parse("only show Online in the ChannelName slicer"))
    assert visual.ops[0]["op"] == "set_visual_filter"
    assert visual.ops[0]["visual"] == "b85aa81a8b5c60030d13"
    assert visual.ops[0]["filter"]["field"] == {"table": "DimChannel", "name": "ChannelName"}
    top = only_clause(parse("filter the Total MTD Sales chart to the top 3 months by total sales"))
    assert top.ops == (
        {
            "op": "set_visual_filter",
            "visual": CHART,
            "filter": {
                "kind": "top_n",
                "field": {"table": "Date", "name": "MonthName"},
                "n": 3,
                "by": {"table": "# Measures", "name": "Total Sales"},
                "direction": "top",
            },
        },
    )
    # with no visual named a top N asks which one: it ranks what one visual shows
    asked = only_clause(parse("filter the chart to the top 5 stores by profit"))
    assert asked.kind is ParseKind.CLARIFY and len(asked.choices) == 5


def test_a_filter_is_never_guessed() -> None:
    """Values alone do not say which field they belong to: the answer lists the candidates, and
    each answer is the finished operation."""
    clause = only_clause(parse("filter this page to Store and Online"))
    assert clause.kind is ParseKind.CLARIFY and not clause.ops
    names = [c.label for c in clause.choices]
    assert "ChannelName" in names and "StoreName" in names
    chosen = apply_choice(clause, names.index("ChannelName"), agents())
    assert chosen.kind is ParseKind.APPLY
    assert chosen.ops[0]["filter"]["values"] == ["Store", "Online"]
    assert chosen.ops[0]["filter"]["field"]["name"] == "ChannelName"
    ranked = only_clause(parse("only show the top 10 agents"))
    assert ranked.kind is ParseKind.CLARIFY and not ranked.ops
    empty = only_clause(parse("filter this page"))
    assert empty.kind is ParseKind.CLARIFY
    # a number in the wrong kind of field is a question too
    text = only_clause(parse("only show 2019 for ChannelName"))
    assert text.kind is ParseKind.APPLY and text.ops[0]["filter"]["values"] == ["2019"]
    number = only_clause(parse("only show Online for Year"))
    assert number.kind is ParseKind.CLARIFY and not number.ops


def test_removing_a_filter_names_what_it_removes_and_where() -> None:
    page = only_clause(parse("remove the filter on Year from this page"))
    assert page.ops == (
        {
            "op": "clear_filter",
            "scope": "page",
            "field": {"table": "Date", "name": "Year"},
            "target": PAGE,
        },
    )
    assert only_clause(parse("clear all filters on the whole report")).ops == (
        {"op": "clear_filter", "scope": "report"},
    )
    chart = only_clause(parse("remove the filter from the Total MTD Sales chart"))
    assert chart.ops == ({"op": "clear_filter", "scope": "visual", "target": CHART},)
    selected = only_clause(parse("remove the filter", agents(selection="b404a461")))
    assert selected.ops[0]["scope"] == "visual" and selected.ops[0]["target"] == CHART
    assert only_clause(parse("remove the filter")).ops[0]["scope"] == "page"


def test_words_about_pages_and_visuals_with_a_filter_word_in_them_are_not_filters() -> None:
    context = contexts.multipage_context()
    for message, op in (
        ("delete every page except Forecast", "delete_page"),
        ("hide the Product Mix page", "set_page_hidden"),
        ("rename the Regional Performance page to Filters", "rename_page"),
    ):
        clause = only_clause(parse(message, context))
        ops_seen = {o["op"] for o in (*clause.ops, *(o for c in clause.choices for o in c.ops))}
        assert ops_seen == {op}, message
    assert only_clause(parse("show only the legend")).kind is ParseKind.UNSUPPORTED


def test_undo_the_filter_checks_the_last_edit_and_the_new_operations_are_named_kinds() -> None:
    from ai_powerbi_analyst.studio.edit_context import change_kinds, op_changes_color

    assert change_kinds([{"op": "set_page_filter"}]) == {"filter"}
    assert change_kinds([{"op": "align_visuals"}, {"op": "swap_visuals"}]) == {"layout"}
    assert change_kinds([{"op": "add_textbox"}]) == {"add"}
    assert change_kinds([{"op": "rename_page", "name": "x"}]) == {"rename", "page"}
    assert op_changes_color({"op": "set_conditional_format", "min_color": "#FFFFFF"})
    context = agents()
    context.last_edit = LastEdit(
        "r6", ({"op": "set_page_filter", "page": PAGE, "filter": {}},), "Filtered page '1' to 2019."
    )
    assert kinds(parse("undo the filter", context)) == ["undo"]
    context.last_edit = LastEdit("r7", ({"op": "align_visuals"},), "Aligned them.")
    asked = only_clause(parse("undo the filter change", context))
    assert asked.kind is ParseKind.CLARIFY


def test_a_closed_intent_to_align_reaches_the_align_operations() -> None:
    context = agents()
    index = {row[2]: row[0] for row in model_index(context, "align the top charts")["v"]}
    numbers = [index["Total MTD Sales"], index["Avg MTD Sales by Agents"]]
    clause = resolve_model_intent(
        ModelIntent(intent="align", targets=numbers, slots=["top"]), context
    )
    assert clause.kind is ParseKind.APPLY
    assert clause.ops == (
        {"op": "align_visuals", "visuals": [CHART, CHART_TR], "edge": "top", "to": "selection"},
    )


# --- answering the questions the edit layer asks -------------------------------------------------


def test_ops_with_answer_fills_a_flag_or_a_choice_into_the_step_that_asked() -> None:
    from ai_powerbi_analyst.studio.edit_ops import ops_with_answer

    batch = [
        {"op": "set_conditional_format", "visual": CHART, "style": "gradient"},
        {"op": "set_visual_hidden", "visual": CHART, "hidden": True},
        {"op": "delete_page", "page": "Details"},
        {"op": "set_number_format", "visual": CHART, "decimals": 2},
    ]
    replaced = ops_with_answer(batch, "replace_conditional")
    assert [op.get("replace_conditional") for op in replaced] == [True, None, None, None]
    three = [dict(batch[0], visual=v) for v in (CHART, TABLE, CHART)]
    first = ops_with_answer(three, "replace_conditional")
    assert [op.get("replace_conditional") for op in first] == [True, None, None]
    assert ops_with_answer(batch, "remove_page_references")[2]["cleanup_references"] is True
    selected = ops_with_answer(batch, "replace_conditional", 4)
    assert selected[3]["replace_conditional"] is True and selected[:3] == batch[:3]
    assert "replace_conditional" not in batch[0]
    for answer, step in [("replace_conditional", 2), ("replace_conditional", 9), ("unknown", None)]:
        with pytest.raises(ValueError):
            ops_with_answer(batch, answer, step)
    with pytest.raises(ValueError):
        ops_with_answer([{"op": "delete_visual", "visual": CHART}], "replace_conditional")
    with pytest.raises(ValueError):
        ops_with_answer(batch, {})


def test_every_question_the_edit_layer_asks_is_answered_and_the_answer_applies() -> None:
    from ai_powerbi_analyst.studio.edit_ops import ops_with_answer
    from ai_powerbi_analyst.studio.pbir_document import PbirDocument
    from ai_powerbi_analyst.studio.pbix_package import PbixPackage
    from ai_powerbi_analyst.studio.report_edits import (
        ModelInfo,
        NeedsConfirmation,
        apply_operations,
        parse_operations,
    )

    report = contexts.agents_report()
    model = ModelInfo.from_report(report.tables, report.measures)
    conditional = [
        {
            "op": "set_conditional_format",
            "visual": TABLE,
            "field": "MTD Sales",
            "style": "gradient",
            "target": "font_color",
            "min_color": "#FF0000",
            "max_color": "#00FF00",
        }
    ]

    def fresh() -> PbirDocument:
        return PbirDocument(PbixPackage(contexts.TWIN))

    with pytest.raises(NeedsConfirmation) as confirm:
        apply_operations(fresh(), parse_operations(conditional), model)
    assert confirm.value.kind == "replace_conditional"
    answered = ops_with_answer(conditional, confirm.value.kind, confirm.value.step)
    result = apply_operations(fresh(), parse_operations(answered), model)
    assert result.report_changed and result.replaced


def test_the_checker_treats_a_question_as_an_outcome_and_counts_it() -> None:
    run = _load("imported_edit_run", "run.py")
    checker = run.OperationChecker()
    conditional = (
        {
            "op": "set_conditional_format",
            "visual": TABLE,
            "field": "MTD Sales",
            "style": "gradient",
            "target": "font_color",
            "min_color": "#FF0000",
            "max_color": "#00FF00",
        },
    )
    assert checker.check_ops(conditional, "agents") == []
    assert checker.asked["replace_conditional"] == 1
    broken = checker.check_ops(({"op": "set_position", "visual": "nope", "x": 100},), "agents")
    assert broken and "do not apply" in broken[0]
    assert checker.check_ops(({"op": "clear_filter", "scope": "report"},), "agents") == []
    invalid = checker.check_ops(({"op": "explode"},), "agents")
    assert invalid and invalid[0].startswith("invalid operation")


# --- second review: wrong readings that the first round left ------------------------------------


def test_a_filter_on_a_type_word_that_names_several_visuals_asks_which_one() -> None:
    """ "the chart" with four charts on the page must not widen to the whole page."""
    for message in (
        "filter the chart to 2019",
        "only show 2019 on the chart",
        "only show 2019 on that chart",
        "only show 2019 on the slicer",
        "only show 2019 on the visual",
        "clear the filter on the chart",
        "remove the filter from the chart",
    ):
        clause = only_clause(parse(message))
        assert clause.kind is ParseKind.CLARIFY, message
        assert clause.question.startswith("Which "), message
        assert not clause.ops
    selected = only_clause(parse("filter the chart to 2019", agents(selection="b404a461")))
    assert selected.ops[0]["op"] == "set_visual_filter" and selected.ops[0]["visual"] == CHART
    assert only_clause(parse("only show 2019")).ops[0]["op"] == "set_page_filter"
    named = only_clause(parse("filter the Total MTD Sales chart to 2019"))
    assert named.ops[0]["op"] == "set_visual_filter" and named.ops[0]["visual"] == CHART


def test_remove_all_filters_asks_where_and_never_clears_the_visuals_one_by_one() -> None:
    from ai_powerbi_analyst.studio.pbir_document import PbirDocument
    from ai_powerbi_analyst.studio.pbix_package import PbixPackage
    from ai_powerbi_analyst.studio.report_edits import (
        ModelInfo,
        apply_operations,
        parse_operations,
    )

    report = contexts.agents_report()
    model = ModelInfo.from_report(report.tables, report.measures)
    for message in ("remove all filters", "clear all the filters", "remove every filter"):
        clause = only_clause(parse(message))
        assert clause.kind is ParseKind.CLARIFY and not clause.ops, message
        assert [c.label for c in clause.choices] == [
            "This page ('1')",
            "The whole report",
            "The 1 visual with a filter",
        ]
        for choice in clause.choices:
            # each answer applies to the twin on its own: only scopes that hold a filter, or
            # (page, report) a clear the operation accepts
            document = PbirDocument(PbixPackage(contexts.TWIN))
            operations = parse_operations(list(choice.ops))
            if choice.label == "The 1 visual with a filter":
                apply_operations(document, operations, model)
                assert [op["target"] for op in choice.ops] == [TABLE]
    assert only_clause(parse("clear all the filters on this page")).ops == (
        {"op": "clear_filter", "scope": "page", "target": PAGE},
    )
    assert only_clause(parse("remove all the filters from the report")).ops == (
        {"op": "clear_filter", "scope": "report"},
    )
    selected = only_clause(parse("remove all filters", agents(selection="e82e02f9")))
    assert selected.ops == ({"op": "clear_filter", "scope": "visual", "target": TABLE},)
    # several visuals of which none holds a filter: an answer, not a batch that fails on step 1
    cards = only_clause(parse("remove all filters from the cards"))
    assert cards.kind is ParseKind.UNSUPPORTED and "no filters" in cards.reason


@pytest.mark.parametrize(
    "tail",
    [
        "and don't move the visuals",
        "without moving anything",
        "and don't resize the visuals",
        "and do not scale",
        "and keep everything in place",
        "and keep the visuals where they are",
        "and leave the visuals alone",
        "but do not scale it",
        ", keep the visuals where they are",
    ],
)
def test_every_way_of_saying_do_not_scale_the_visuals_sends_scale_false(tail: str) -> None:
    context = contexts.multipage_context()
    clause = only_clause(parse(f"make the page 1920 by 1080 {tail}", context))
    assert clause.kind is ParseKind.APPLY, tail
    assert clause.ops == (
        {"op": "set_page_size", "page": "page0", "width": 1920.0, "height": 1080.0, "scale": False},
    )
    plain = only_clause(parse("make the page 1920 by 1080", context))
    assert "scale" not in plain.ops[0]  # SetPageSize scales by default


def test_keeping_visuals_in_place_says_so_when_they_would_fall_off_the_page() -> None:
    fits = only_clause(parse("make the page 2400 x 1500 and keep the visuals where they are"))
    assert fits.ops[0]["scale"] is False
    outside = only_clause(parse("make the page 1920 by 1080, keep the visuals where they are"))
    assert outside.kind is ParseKind.UNSUPPORTED and "cannot stay where they are" in outside.reason
    assert not parse("make the page 2400 x 1500 and keep the visuals where they are").unparsed


def test_an_aspect_ratio_is_a_page_shape_and_not_a_page_number() -> None:
    context = contexts.multipage_context()
    for message in ("make the page 16:9", "resize the page to 16:9"):
        clause = only_clause(parse(message, context))
        assert clause.ops == (
            {"op": "set_page_size", "page": "page0", "width": 1280.0, "height": 720.0},
        )
    ask = only_clause(parse("make the page 4:3", context))
    assert ask.kind is ParseKind.CLARIFY  # 2 visuals would fall off a 960 x 720 page


def test_deleting_a_page_can_take_the_buttons_that_point_to_it() -> None:
    context = contexts.multipage_context()
    for message in (
        "delete page 2 and clean up the buttons",
        "delete page 2 and remove the references",
        "delete page 2, remove the buttons that point to it",
        "delete the Regional Performance page and fix the links",
    ):
        result = parse(message, context)
        clause = only_clause(result)
        assert clause.ops == (
            {"op": "delete_page", "page": "page1", "cleanup_references": True},
        ), message
        assert not result.unparsed
    plain = only_clause(parse("delete page 2", context))
    assert plain.ops == ({"op": "delete_page", "page": "page1"},)
    # words about buttons on a page are not a page delete
    other = only_clause(parse("remove the buttons on page 2", context))
    assert other.kind is not ParseKind.APPLY or other.ops[0]["op"] != "delete_page"


def test_a_page_that_does_not_exist_is_never_replaced_by_the_active_one() -> None:
    context = contexts.multipage_context()
    for message in ("delete the Zebra page", "delete the Details page", "hide the Zebra page"):
        clause = only_clause(parse(message, context))
        assert clause.kind is ParseKind.CLARIFY, message
        assert "could not find a page called" in clause.question
        labels = [c.label for c in clause.choices]
        assert labels[:2] == ["Executive Summary", "Regional Performance"]
    for message in ("delete the first page", "delete the last page", "delete this page"):
        assert only_clause(parse(message, context)).kind is ParseKind.APPLY, message


def test_deleting_two_named_pages_asks_and_deletes_both_or_neither() -> None:
    context = contexts.multipage_context()
    clause = only_clause(parse("delete the Forecast page and the Product Mix page", context))
    assert clause.kind is ParseKind.CLARIFY and clause.question.startswith("That deletes 2 pages")
    assert [op["page"] for op in clause.choices[0].ops] == ["page2", "page4"]
    renamed = only_clause(parse("rename the Forecast page to Product Mix", context))
    assert renamed.kind is ParseKind.UNSUPPORTED and "already a page named" in renamed.reason
    fine = only_clause(parse("rename the Forecast page to Outlook", context))
    assert fine.ops == ({"op": "rename_page", "page": "page4", "name": "Outlook"},)


def test_a_text_box_keeps_what_was_typed_and_says_when_it_did_not_place_it() -> None:
    def text_of(message: str) -> str:
        return str(only_clause(parse(message)).ops[0]["text"])

    assert text_of('add a text box saying Q3 "final" review') == 'Q3 "final" review'
    assert text_of('add a text box saying Sales for "Store" and "Online"') == (
        'Sales for "Store" and "Online"'
    )
    assert text_of('add a text box that says "hello" and "world"') == '"hello" and "world"'
    assert text_of('add a text box saying "Q3 review"') == "Q3 review"
    assert text_of('add a text box saying "Q3 review" at the top') == "Q3 review"
    assert text_of("add a text box saying hi below the table") == "hi"
    assert text_of("add a text box saying hi on the left") == "hi"
    assert text_of("add a text box saying <b>hi</b> & \"quotes\" 'single'") == (
        "<b>hi</b> & \"quotes\" 'single'"
    )
    unplaced = only_clause(parse("add a text box saying hi in the top right"))
    assert "in free space" in unplaced.summary and "x" not in unplaced.ops[0]
    placed = only_clause(parse('add a text box saying "hi" at 100, 50'))
    assert placed.ops[0]["x"] == 100.0 and "in free space" not in placed.summary
    negative = only_clause(parse("add a text box saying hello at -10, -10"))
    assert negative.kind is ParseKind.CLARIFY and "starts at 0, 0" in negative.question
    shape = only_clause(parse("add a rectangle with the text hello"))
    assert shape.kind is ParseKind.CLARIFY and "cannot hold text" in shape.question
    assert shape.choices[0].ops[0]["op"] == "add_textbox"
    assert shape.choices[0].ops[0]["text"] == "hello"


def test_a_new_visual_with_a_size_or_a_place_in_words_is_read_and_told() -> None:
    sized = only_clause(parse("add a 300 x 200 card for Total Sales"))
    assert sized.ops[0]["op"] == "add_visual" and sized.ops[0]["visual_type"] == "card"
    assert (sized.ops[0]["width"], sized.ops[0]["height"]) == (300.0, 200.0)
    for message in (
        "add a card for Total Sales in the top right",
        "add a card for Total Sales below the table",
        "add a card for Total Sales next to the StoreName slicer",
        "add a bar chart of Total Sales by StoreName on the left",
    ):
        clause = only_clause(parse(message))
        assert "x" not in clause.ops[0] and "in free space" in clause.summary, message
    numbers = only_clause(parse("add a card for Total Sales at 100, 50"))
    assert numbers.ops[0]["x"] == 100.0 and "in free space" not in numbers.summary


def test_lining_up_visuals_that_share_no_row_or_column_asks_for_the_edge() -> None:
    for message in ("line up the cards", "align all the visuals", "line up everything"):
        clause = only_clause(parse(message))
        assert clause.kind is ParseKind.CLARIFY, message
        assert [c.label for c in clause.choices] == ["Left edges", "Tops", "Centers"]
        assert {op["edge"] for c in clause.choices for op in c.ops} == {"left", "top", "center_h"}
    row = only_clause(parse("align the cards to the top"))
    assert row.kind is ParseKind.APPLY and row.ops[0]["edge"] == "top"
    many = only_clause(parse("align all the charts to the left"))
    assert many.kind is ParseKind.CLARIFY and many.choices[0].ops[0]["edge"] == "left"
    assert many.choices[0].label == "Yes, align 4 visuals"


def test_bare_phrases_about_top_n_or_recent_months_stay_chat_questions() -> None:
    """ "top 3 months by total sales" alone is a question about data, not an order to filter:
    the parser hands it to the chat (with an order verb it is a filter)."""
    for message in (
        "top 3 months by total sales",
        "show the top 3 months by total sales",
        "top 5 stores by profit",
        "last 6 months",
    ):
        assert only_clause(parse(message)).kind is ParseKind.NOT_AN_EDIT, message
    assert only_clause(parse("show only the last 6 months")).ops[0]["op"] == "set_page_filter"


# --- the planner fix pass: readings the no-model tier got wrong (planner_bugs.md) --------------


@pytest.mark.parametrize(
    "message",
    [
        "remove the frame around the StoreName slicer",
        "hide the totals in the table",
        "remove the icons from the table",
        "remove the totals from the table",
        "remove the banded rows from the table",
        "remove the small multiples from the Total MTD Sales chart",
        "remove the running total from the table",
        "remove the data bars from the table",
        "hide the EmployeeKey column in the model",
        "hide the Rank column",
    ],
)
def test_removing_or_hiding_a_part_never_deletes_or_hides_the_whole_visual(message: str) -> None:
    result = parse(message)
    made = [op["op"] for op in result.ops]
    assert "delete_visual" not in made and "set_visual_hidden" not in made, (message, made)


def test_readings_the_coverage_gate_caught_are_right_now() -> None:
    def ops(message: str) -> list[dict[str, Any]]:
        return list(parse(message).ops)

    assert kinds(parse("hide the x axis on Avg MTD % change by Agents")) == ["unsupported"]
    assert ops("widen the Total MTD Sales chart to 500 pixels")[0]["width"] == 500.0
    assert ops("drop shadow on the table please") == [] or all(
        op.get("shadow") is not False for op in ops("drop shadow on the table please")
    )
    corners = parse("give the Number of Agents Sold above 100K card rounded corners of 12 px")
    assert kinds(corners) == ["unsupported"]
    assert kinds(parse("put a navy border around the ChannelName slicer")) == ["unsupported"]
    assert ops("put the StoreName slicer on top of everything") == [
        {"op": "arrange", "visual": "5b0dbccd61c6c4adb415", "to": "front"}
    ]
    labels = ops("the category labels under Total MTD Sales should be 11 pt")
    assert labels == []
    title = ops("turn on the title of the x axis in Total MTD Sales")
    assert title == []
    size = ops("the title of the Avg Discount per Transaction chart should be 14 pt")
    assert all(op.get("text") != "14 pt" for op in size)
    placed = ops("place the Country slicer at x 1300, y 40")
    assert placed and placed[0].get("x") == 1300.0 and placed[0].get("y", 40.0) == 40.0
    renamed = parse("rename the column StoreName to Store")
    assert not any(op["op"] == "delete_visual" for op in renamed.ops)


@pytest.mark.parametrize(
    "message",
    [
        "include last year in the comparison?",
        "we want to filter by store later, what stores are there",
        "bring up the sales by store",
    ],
)
def test_a_question_or_a_plan_for_later_is_no_filter_and_no_move(message: str) -> None:
    assert parse(message).ops == []


def test_the_live_test_messages_of_2026_09_29_read_right() -> None:
    """Messages from the live app test: data bars reach the conditional card (never the
    data colors, which Power BI refuses for data bars); "that chart" is the chart named
    earlier in the message; "at 10M" is ten million; a table column "in thousands" is a
    number format, never the field added to the table."""
    bars = parse("add data bars to the Rank column in the agents table")
    if "conditional_formatting" in _provided_by_cards():
        (op,) = bars.ops
        assert op["op"] == "set_conditional_format" and op["style"] == "data_bars"
        assert op.get("target") in (None, "data_bars") and op["field"] == "Rank"
    line = parse("make the Total MTD Sales title bold and add a constant line at 10M to that chart")
    assert line.clauses[0].kind is ParseKind.UNSUPPORTED
    if "analytics_lines" in _provided_by_cards():
        assert line.ops[0]["visual"] == CHART and line.ops[0]["value"] == 10_000_000
    else:  # the planner gets the chart by its name
        assert "Total MTD Sales" in " ".join(line.unparsed)
    thousands = parse("show MTD Sales in the table in thousands")
    assert all(op["op"] != "set_fields" for op in thousands.ops)


def test_the_live_test_messages_of_2026_09_29_b_read_right() -> None:
    """Live test 2: a column shaded by value is conditional formatting, never the whole
    table's background; "the four KPI cards at the top" is the top row of four cards."""
    for message in (
        "shade the Rank column background from white to green",
        "make the Rank column background light green in the table",
        "make the Rank column font from red to green",
        "color the Rank column by value",
    ):
        made = [op["op"] for op in parse(message).ops]
        assert all(op == "set_conditional_format" for op in made), (message, made)
    shaded = parse("shade the Rank column background from white to green")
    if "conditional_formatting" in _provided_by_cards():
        (op,) = shaded.ops
        assert op["op"] == "set_conditional_format" and op["style"] == "gradient"
        assert op["target"] == "background" and op["field"] == "Rank"
        assert (op["min_color"], op["max_color"]) == ("#FFFFFF", "#107C10")
    assert kinds(parse("make the table background light grey")) == ["unsupported"]
    top_row = ["fb4c5aa3", "2202e311", "94793535", "71a16476"]
    for message in ("the four KPI cards at the top", "the four cards", "the KPI cards at the top"):
        found = resolve_visuals(message, agents())
        assert found.ok and [e.id[:8] for e in found.entries] == top_row, message
    assert not resolve_visuals("the two cards", agents()).ok  # no row of two: asked
    labels = parse("show the Total MTD Sales chart data labels in millions with 1 decimal")
    assert labels.ops[0]["display_units"] == "millions" and labels.ops[0]["decimals"] == 1
    assert "decimals 1" in labels.clauses[0].summary


@pytest.mark.parametrize(
    "message",
    [
        "show the values in thousands",
        "display values in millions",
        "show values with 2 decimals",
    ],
)
def test_displaying_number_units_never_changes_visual_visibility(message: str) -> None:
    result = parse(message, agents(selection=CHART))
    assert result.ops and all(op["op"] == "set_number_format" for op in result.ops)


def test_a_pair_joined_by_and_after_between_stays_one_request() -> None:
    parts = split_clauses("add a relationship between the store table and the sales table")
    assert parts == ["add a relationship between the store table and the sales table"]
    assert split_clauses("make the bars green and the title blue") == [
        "make the bars green",
        "make the title blue",
    ]
