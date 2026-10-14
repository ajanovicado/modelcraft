"""Tool cards (edit_tools, edit_tool_cards) and the capability matrix derived from them.

Every operation report_edits accepts has a card; every example of a built-in card is a real
operation on the Agents dashboard's structure twin: it validates with ``parse_operations`` and
applies to the twin. The slots of a card are the operation's fields (or its ``build`` maps them),
and the matrix calls supported exactly what a registered card provides.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, get_args

import pytest

from ai_powerbi_analyst.studio import edit_capabilities, edit_tool_cards, edit_tools
from ai_powerbi_analyst.studio.edit_capabilities import (
    ALWAYS_REFUSED,
    CAPABILITY_MATRIX,
    CapabilityMatrix,
    card_capability,
    derive_matrix,
    detect_capability,
    detect_unsupported,
)
from ai_powerbi_analyst.studio.edit_tool_cards import PAGE, TOOL_CARDS, filter_spec
from ai_powerbi_analyst.studio.edit_tools import Slot, ToolCard
from ai_powerbi_analyst.studio.pbir_document import PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage
from ai_powerbi_analyst.studio.report_edits import (
    ModelInfo,
    NeedsChoice,
    NeedsConfirmation,
    apply_operations,
    parse_operations,
)

EVALS = Path(__file__).resolve().parents[2] / "evals" / "imported_edit"


def _load(name: str, file: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, EVALS / file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


contexts = _load("tool_cards_contexts", "contexts.py")
if not contexts.TWIN.exists():  # pragma: no cover - the twin is committed
    pytest.skip("the structure twin fixture is missing", allow_module_level=True)

REPORT = contexts.agents_report()
KINDS = set(get_args(edit_tools.SlotKind))
BUILTIN_OPS = {card.op for card in TOOL_CARDS}
EXAMPLES = [(card.op, n) for card in TOOL_CARDS for n in range(len(card.examples))]


def _card(op: str) -> ToolCard:
    return next(card for card in TOOL_CARDS if card.op == op)


def _setup(op: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What the twin needs before an example can apply: a second page for the page examples
    that name 'Details', and a filter for a clear."""
    made: list[dict[str, Any]] = []
    if op.get("page") == "Details":
        made.append({"op": "add_page", "name": "Details"})
    if op["op"] == "clear_filter":
        spec = {
            "kind": "categorical",
            "field": op.get("field") or {"table": "Date", "name": "Year"},
            "values": [2019],
        }
        if op["scope"] == "report":
            made.append({"op": "set_report_filter", "filter": spec})
        elif op["scope"] == "page":
            made.append({"op": "set_page_filter", "page": op["target"], "filter": spec})
        else:
            made.append({"op": "set_visual_filter", "visual": op["target"], "filter": spec})
    return made


def _apply(batch: list[dict[str, Any]]) -> None:
    document = PbirDocument(PbixPackage(contexts.TWIN))
    model = ModelInfo.from_report(REPORT.tables, REPORT.measures)
    apply_operations(document, parse_operations(batch), model)


# --- coverage of the operations --------------------------------------------------------------


def test_every_registered_operation_has_exactly_one_card() -> None:
    registered = set(edit_tools.registered_operations())
    cards = edit_tools.tool_cards()
    assert registered <= {card.op for card in cards}, sorted(registered - {c.op for c in cards})
    assert len({card.op for card in cards}) == len(cards)
    # the built-in module covers the operations that existed before the planner
    assert {
        "align_visuals",
        "add_shape",
        "create_measure",
        "clear_filter",
    } <= BUILTIN_OPS
    assert len(BUILTIN_OPS) == 30 and registered >= BUILTIN_OPS


def test_a_family_module_is_read_when_it_exists_and_skipped_when_it_does_not() -> None:
    assert edit_tools.tool_cards(("no_such_family_module",)) == ()
    only_builtin = edit_tools.tool_cards((edit_tools.BUILTIN_MODULE,))
    assert only_builtin == TOOL_CARDS
    assert edit_tools.builtin_ops() == frozenset(BUILTIN_OPS)
    # cards the model may not choose are listed but not planned with
    planned = {card.op for card in edit_tools.planner_cards((edit_tools.BUILTIN_MODULE,))}
    assert planned == BUILTIN_OPS


# --- the examples are real ---------------------------------------------------------------------


@pytest.mark.parametrize(("op", "number"), EXAMPLES)
def test_every_example_validates_and_applies_to_the_structure_twin(op: str, number: int) -> None:
    card = _card(op)
    request, operation = card.examples[number]
    assert request.strip() and request == request.strip()
    assert operation["op"] == card.op
    json.dumps(operation)  # plain data, as chat sends it
    (parsed,) = parse_operations([dict(operation)])
    assert type(parsed).model_fields["op"].default == card.op
    try:
        _apply([*_setup(operation), dict(operation)])
    except (NeedsChoice, NeedsConfirmation) as asked:  # pragma: no cover - none ask today
        pytest.fail(f"{op} example {number} asks instead of applying: {asked}")


def test_each_card_has_three_examples_with_different_requests() -> None:
    for card in TOOL_CARDS:
        requests = [request for request, _ in card.examples]
        assert len(requests) >= 3, card.op
        assert len(set(requests)) == len(requests), card.op


# --- the slots are the operation's fields --------------------------------------------------------


def test_slots_are_well_formed() -> None:
    for card in edit_tools.tool_cards():
        names = [slot.name for slot in card.slots]
        assert len(names) == len(set(names)), card.op
        for slot in card.slots:
            assert slot.kind in KINDS, (card.op, slot.name)
            assert slot.description, (card.op, slot.name)
            assert (slot.kind == "enum") == bool(slot.choices), (card.op, slot.name)
            if slot.minimum is not None and slot.maximum is not None:
                assert slot.minimum <= slot.maximum


def test_a_card_without_a_builder_maps_each_slot_onto_a_field_of_its_operation() -> None:
    operations = edit_tools.registered_operations()
    for card in TOOL_CARDS:
        cls = operations[card.op]
        fields = set(cls.model_fields)
        required = {
            name for name, info in cls.model_fields.items() if info.is_required() and name != "op"
        }
        if card.build is not None:
            continue
        targets = {(slot.field or slot.name).split(".")[0] for slot in card.slots}
        assert targets <= fields, (card.op, sorted(targets - fields))
        covered = targets | set(card.fixed)
        assert required <= covered, (card.op, sorted(required - covered))


def test_slot_values_become_the_operation_through_the_card() -> None:
    swap = _card("swap_visuals").to_operation({"first": "a1", "second": "b2"})
    assert swap == {"op": "swap_visuals", "a": "a1", "b": "b2"}
    placed = _card("place_visual").to_operation({"visual": "a1", "next_to": "b2", "side": "below"})
    assert placed == {"op": "place_visual", "visual": "a1", "relative_to": "b2", "side": "below"}


def test_builders_turn_words_into_what_the_operation_takes() -> None:
    added = _card("add_visual").to_operation(
        {"page": "p", "visual_type": "pie", "fields": [{"table": "T", "name": "A"}]}
    )
    assert added == {
        "op": "add_visual",
        "page": "p",
        "visual_type": "pieChart",
        "fields": [{"table": "T", "name": "A"}],
    }
    changed = _card("change_visual_type").to_operation(
        {"visual": "v", "visual_type": "stacked bar"}
    )
    assert changed == {"op": "change_visual_type", "visual": "v", "visual_type": "barChart"}
    shape = _card("add_shape").to_operation({"page": "p", "shape": "triangle", "fill": "#FF0000"})
    assert shape == {"op": "add_shape", "page": "p", "shape": "triangleIsoc", "fill": "#FF0000"}
    moved = _card("move_page").to_operation({"page": "p", "position": 1})
    assert moved == {"op": "move_page", "page": "p", "index": 0}


def test_the_filter_slots_become_each_kind_of_filter() -> None:
    field = {"table": "Date", "name": "Year"}
    by = {"table": "# Measures", "name": "Total Sales"}
    assert filter_spec({"field": field, "values": "2019, 2020 and 2021"}) == {
        "kind": "categorical",
        "field": field,
        "values": [2019, 2020, 2021],
    }
    assert filter_spec({"field": field, "values": "Online", "exclude": True}) == {
        "kind": "categorical",
        "field": field,
        "values": ["Online"],
        "exclude": True,
    }
    assert filter_spec({"field": field, "condition": "greater_than", "value": "1.5"}) == {
        "kind": "advanced",
        "field": field,
        "conditions": [{"operator": "greater_than", "value": 1.5}],
    }
    assert filter_spec({"field": field, "condition": "is_blank", "value": "x"}) == {
        "kind": "advanced",
        "field": field,
        "conditions": [{"operator": "is_blank"}],
    }
    assert filter_spec({"field": field, "bottom": 3, "by": by}) == {
        "kind": "top_n",
        "field": field,
        "n": 3,
        "by": by,
        "direction": "bottom",
    }
    page = _card("set_page_filter").to_operation({"page": "p1", "field": field, "values": "2019"})
    assert page == {
        "op": "set_page_filter",
        "page": "p1",
        "filter": {"kind": "categorical", "field": field, "values": [2019]},
    }
    cleared = _card("clear_filter").to_operation({"visual": "v1"})
    assert cleared == {"op": "clear_filter", "scope": "visual", "target": "v1"}
    everything = _card("clear_filter").to_operation({"scope": "report", "page": "p1"})
    assert everything == {"op": "clear_filter", "scope": "report"}
    for made in (page, cleared, everything):
        parse_operations([made])


def test_the_fixture_ids_are_the_twins() -> None:
    ids = {v.id for p in REPORT.pages for v in p.visuals}
    names = {"TABLE", "CHART_TOTAL", "CHART_AVG", "SLICER_STORE", "CARD_TOPN", "CARD_100K"}
    assert {getattr(edit_tool_cards, n) for n in names} <= ids
    assert REPORT.pages[0].id == PAGE


# --- the capability matrix --------------------------------------------------------------------


def test_every_built_in_card_names_a_supported_entry_and_every_supported_entry_has_a_card() -> None:
    static = {entry.key: entry for entry in edit_capabilities._LIST}
    for card in TOOL_CARDS:
        assert card.capability in static, card.op
        assert static[card.capability].status == "supported", card.op
        assert CAPABILITY_MATRIX[card.capability].status == "supported"
    provided = {card_capability(c) for c in edit_tools.tool_cards()}
    for key, entry in CAPABILITY_MATRIX.items():
        if entry.status == "supported":
            assert key in provided and entry.family, key
        else:
            assert entry.reason and entry.desktop_steps, key
    assert CAPABILITY_MATRIX["publishing"].status == "refused"


def _family_card(
    op: str, examples: tuple[str, ...], capability: str = "", family: str = "demo"
) -> ToolCard:
    return ToolCard(
        op=op,
        family=family,
        summary="A demo card.",
        slots=(Slot("visual", "visual", "the visual"),),
        examples=tuple((text, {"op": op, "visual": "v"}) for text in examples),
        capability=capability,
    )


def test_a_family_card_makes_a_planned_entry_supported_and_keeps_its_steps() -> None:
    bars = _family_card(
        "set_data_bars",
        (
            "add data bars to the Rank column",
            "show data bars on Total Sales",
            "remove the data bars from Rank",
        ),
    )
    assert card_capability(bars) == "conditional_formatting"  # by its examples
    matrix = derive_matrix([*TOOL_CARDS, bars])
    entry = matrix["conditional_formatting"]
    assert entry.status == "supported" and entry.family == "set_data_bars" and entry.reason == ""
    assert entry.desktop_steps == CAPABILITY_MATRIX["conditional_formatting"].desktop_steps
    assert entry.pattern is not None  # a request for it is still recognized
    # a card that names its entry wins over its examples; publishing never becomes supported
    named = _family_card("add_bookmark_x", ("do something",), capability="bookmarks")
    sneaky = _family_card("publish_now", ("publish this report to the service",))
    matrix = derive_matrix([*TOOL_CARDS, named, sneaky])
    assert matrix["bookmarks"].status == "supported"
    assert matrix["publishing"].status == "refused"
    # a card that fits no entry adds one under its family
    odd = _family_card("set_odd_thing", ("frobnicate the widget",), family="extras")
    matrix = derive_matrix([*TOOL_CARDS, odd])
    assert matrix["extras"].status == "supported" and matrix["extras"].family == "set_odd_thing"
    assert "Analytics lines" in matrix["extras"].title
    assert {
        "publishing",
        "sign_in",
        "data_gateways",
        "custom_visuals",
        "styling",
        "column_formatting",
        "format_painter",
        "visual_header",
    } == ALWAYS_REFUSED


def test_the_matrix_is_worked_out_on_first_use_and_can_be_worked_out_again() -> None:
    matrix = CapabilityMatrix()
    assert matrix._items is None
    assert matrix["layout"].status == "supported" and "layout" in matrix and len(matrix) > 30
    assert matrix._items is not None
    matrix.refresh()
    assert matrix._items is None and dict(matrix) == dict(CAPABILITY_MATRIX)
    assert edit_capabilities.unsupported_keys() == edit_capabilities.UNSUPPORTED_KEYS
    assert "publishing" in edit_capabilities.UNSUPPORTED_KEYS
    assert "layout" not in edit_capabilities.UNSUPPORTED_KEYS


def test_a_broken_family_module_leaves_the_built_in_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(modules: Any = edit_tools.FAMILY_MODULES) -> tuple[ToolCard, ...]:
        if tuple(modules) == (edit_tools.BUILTIN_MODULE,):
            return TOOL_CARDS
        raise SyntaxError("a family module does not import")

    monkeypatch.setattr(edit_tools, "tool_cards", explode)
    matrix = CapabilityMatrix()
    assert matrix["layout"].status == "supported"
    assert matrix["conditional_formatting"].status == "planned"


def test_detection_tells_a_supported_feature_from_a_refused_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = _family_card("set_data_bars", ("add data bars to Rank",), "conditional_formatting")
    matrix = CapabilityMatrix()
    monkeypatch.setattr(matrix, "_load", lambda: derive_matrix([*TOOL_CARDS, bars]))
    monkeypatch.setattr(edit_capabilities, "CAPABILITY_MATRIX", matrix)
    wanted = detect_capability("add data bars to the Rank column")
    assert wanted is not None and wanted.key == "conditional_formatting"
    assert wanted.status == "supported"
    assert detect_unsupported("add data bars to the Rank column") is None
    # an entry Studio does not do comes first
    both = detect_capability("publish the report with data bars")
    assert both is not None and both.key == "publishing"
    refused = detect_unsupported("add a bookmark for this view")
    assert refused is not None and refused.key == "bookmarks" and refused.status == "planned"


def _matrix_with(monkeypatch: pytest.MonkeyPatch, *cards: ToolCard) -> None:
    matrix = CapabilityMatrix()
    monkeypatch.setattr(matrix, "_load", lambda: derive_matrix([*TOOL_CARDS, *cards]))
    monkeypatch.setattr(edit_capabilities, "CAPABILITY_MATRIX", matrix)


@pytest.mark.parametrize(
    ("message", "key"),
    [
        ("create a hierarchy called Calendar on Date", "model"),
        ("hide the EmployeeKey column in the model", "model"),
        ("add a running total visual calculation to the table", "visual_calculations"),
        ("split the chart by ChannelName into small multiples", "small_multiples"),
        ("hide the visual header of the table", "visual_header"),
        ("keep the layer order of the StoreName slicer", "layer_order"),
        ("set the page view to fit to width", "page_view"),
        ("lock the objects on this page", "lock_objects"),
        ("sign in to the data source with my password", "sign_in"),
        ("set up a gateway for the refresh", "data_gateways"),
        ("change the data type of StoreKey to text", "data_gateways"),
        ("edit the power query of FactSales", "power_query"),
        ("drill down the hierarchy of the chart", "drillthrough_tooltip_pages"),
    ],
)
def test_every_request_the_families_named_has_its_entry(message: str, key: str) -> None:
    found = detect_capability(message)
    assert found is not None and found.key == key, (message, found and found.key)
    entry = CAPABILITY_MATRIX[key]
    assert entry.desktop_steps  # help to do it in Desktop, supported or not


def test_query_edits_can_become_supported_but_gateways_and_sign_in_never(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = _family_card("set_table_query", ("change the power query of FactSales",), "power_query")
    gate = _family_card("set_gateway", ("set up a gateway",), "data_gateways")
    login = _family_card("sign_me_in", ("sign in to the server",), "sign_in")
    _matrix_with(monkeypatch, query, gate, login)
    assert edit_capabilities.CAPABILITY_MATRIX["power_query"].status == "supported"
    for key in ("data_gateways", "sign_in", "publishing", "custom_visuals"):
        assert edit_capabilities.CAPABILITY_MATRIX[key].status == "refused"
        assert key in ALWAYS_REFUSED
    # with query edits supported, a gateway or a data type change is still refused
    for message, key in (
        ("set up a gateway so it refreshes every night", "data_gateways"),
        ("change the data type of StoreKey to text", "data_gateways"),
        ("refresh the data with my SQL server credentials", "sign_in"),
    ):
        refused = detect_unsupported(message)
        assert refused is not None and refused.key == key and refused.status == "refused"
    # a hierarchy is the model family's, never a drillthrough page
    hierarchy = detect_capability("create a hierarchy called Calendar on Date")
    assert hierarchy is not None and hierarchy.key == "model"


def test_a_card_that_names_its_entry_is_authoritative() -> None:
    delete = _family_card("delete_column", ("remove the relationship column",), "model")
    role = _family_card("update_role", ("change the RLS filter of a role",), "rls")
    tabs = _family_card("set_tab_order", ("set the tab order",), "alt_text")
    assert [card_capability(c) for c in (delete, role, tabs)] == ["model", "rls", "alt_text"]
    matrix = derive_matrix([*TOOL_CARDS, delete, role, tabs])
    assert matrix["model"].status == "supported" and "delete_column" in matrix["model"].family
    assert matrix["relationships"].status == "planned"  # not made supported by a word
