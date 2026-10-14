"""Round 3 families registered in the facade: every card, example and probe goes through
``report_edits`` the way chat and the HTTP API send it.

The families were built and tested through their own dispatchers (``apply_conditional``,
``apply_navigation``, ``apply_extras``, ``apply_model_family``). This file holds them to the
facade: each card example parses with ``parse_operations`` and applies to the structure twin with
``apply_operations`` (model examples: parsed, then checked by the family's own handler against the
model its examples are written for), each Desktop probe parses and applies, each registered
operation has exactly one card, and the capability matrix calls each card's entry supported.
"""

from __future__ import annotations

import json
import re
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from agents_twin_model import twin_model
from test_report_edit_extras import SETUP as EXTRAS_SETUP

from ai_powerbi_analyst.studio import (
    edit_capabilities,
    edit_ops,
    edit_tools,
    imported_chat_edits,
    report_edit_conditional,
    report_edit_extras,
    report_edit_model,
    report_edit_navigation,
    report_edits,
)
from ai_powerbi_analyst.studio.edit_tools import ProbeCase, ToolCard
from ai_powerbi_analyst.studio.pbir_document import PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage
from ai_powerbi_analyst.studio.report_edit_context import (
    EditOutcome,
    ModelInfo,
    NeedsConfirmation,
)
from ai_powerbi_analyst.studio.report_edit_geometry import resolve_visual
from ai_powerbi_analyst.studio.report_edit_validation import capture_baseline, validate_document

TWIN = Path(__file__).resolve().parents[2] / "fixtures" / "imported" / "structure-twin.pbix"
REPORT_FAMILIES = (report_edit_conditional, report_edit_navigation, report_edit_extras)


@pytest.fixture(autouse=True)
def offline_m_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Power Query parser is a node process; the model examples get a stand-in."""
    monkeypatch.setattr(report_edit_model, "M_CHECKER", lambda text: [])


# Fields of the real Agents model that the twin's report does not use (so ``twin_model`` has no
# row for them) and that the extras cards and probes name.
EXTRA_MEASURES = ("Total Sales", "Profit", "PY Total Sales", "TopN/BottomN/ PMTD Sales")
EXTRA_COLUMNS = ({"TableName": "Date", "ColumnName": "Date", "PandasDataType": "datetime64[ns]"},)
EXTRA_TYPES = ({"table": "Date", "name": "Date", "dataType": 9, "summarizeBy": "none"},)


def twin() -> PbirDocument:
    return PbirDocument(PbixPackage(TWIN))


def agents_model() -> ModelInfo:
    model = twin_model(EXTRA_COLUMNS, EXTRA_TYPES)
    for name in EXTRA_MEASURES:
        if model.measure_name(name) is None:
            model.measures[name] = "# Measures"
    return model


def saved_parts(document: PbirDocument, target: Path) -> dict[str, Any]:
    """Commit, write the PBIX and read every report JSON part back (what Desktop would open)."""
    document.commit()
    document.package.write(target)
    parts: dict[str, Any] = {}
    with zipfile.ZipFile(target) as archive:
        for name in archive.namelist():
            if name.startswith("Report/definition/") and name.endswith(".json"):
                parts[name] = json.loads(archive.read(name).decode("utf-8"))
    return parts


def op_name(cls: type) -> str:
    return str(cls.model_fields["op"].default)  # type: ignore[attr-defined]


def apply(document: PbirDocument, steps: Sequence[Mapping[str, Any]], model: ModelInfo) -> Any:
    """One batch through the facade, as the editor's dry run applies it."""
    parsed = report_edits.parse_operations([dict(step) for step in steps])
    return report_edits.apply_operations(document, list(parsed), model)


def setup_for(card: ToolCard) -> list[Mapping[str, Any]]:
    """What a card's examples assume has run on the twin: navigation's ``EXAMPLE_SETUP`` for
    every navigation card, the extras test file's per-operation setup for the extras cards."""
    if card.family == "navigation":
        return list(report_edit_navigation.EXAMPLE_SETUP)
    if card.family == "extras":
        return list(EXTRAS_SETUP.get(card.op, []))
    return []


def report_examples() -> list[Any]:
    return [
        pytest.param(card, request, example, id=f"{card.op}-{index}")
        for module in REPORT_FAMILIES
        for card in module.TOOL_CARDS
        for index, (request, example) in enumerate(card.examples)
    ]


def model_examples() -> list[Any]:
    return [
        pytest.param(card, request, example, id=f"{card.op}-{index}")
        for card in report_edit_model.TOOL_CARDS
        for index, (request, example) in enumerate(card.examples)
    ]


# --- card examples ------------------------------------------------------------------------------


@pytest.mark.parametrize(("card", "request_text", "example"), report_examples())
def test_every_report_card_example_applies_to_the_twin_through_the_facade(
    card: ToolCard, request_text: str, example: Mapping[str, Any]
) -> None:
    assert request_text and example["op"] == card.op
    model = agents_model()
    document = twin()
    baseline = capture_baseline(document, model)
    setup = setup_for(card)
    if setup:
        apply(document, setup, model)
    outcome: EditOutcome = apply(document, [example], model)
    assert outcome.report_changed, card.op
    assert outcome.model_operations == [], card.op
    assert len(outcome.summaries) == 1 and outcome.summaries[0].strip(), card.op
    assert validate_document(document, model, baseline=baseline) == []


@pytest.mark.parametrize(("card", "request_text", "example"), model_examples())
def test_every_model_card_example_parses_and_its_family_accepts_it(
    card: ToolCard, request_text: str, example: Mapping[str, Any]
) -> None:
    assert request_text and example["op"] == card.op
    (parsed,) = report_edits.parse_operations([dict(example)])
    assert isinstance(parsed, report_edit_model.MODEL_FAMILY_OPERATIONS)
    assert isinstance(parsed, report_edits.MODEL_OPERATIONS)
    result = report_edit_model.check_model_operation(
        twin(), parsed, report_edit_model.example_model()
    )
    assert result.summary.endswith("."), card.op


def test_every_builtin_card_example_parses() -> None:
    """The cards of the operations that existed before the planner are written for the planner's
    eval contexts (a Details page, a Date[Year] column), not for the twin: parsed here only."""
    from ai_powerbi_analyst.studio import edit_tool_cards

    examples = [ex for card in edit_tool_cards.TOOL_CARDS for _, ex in card.examples]
    assert len(examples) >= 90
    parsed = [
        operation
        for start in range(0, len(examples), 50)
        for operation in report_edits.parse_operations(
            [dict(ex) for ex in examples[start : start + 50]]
        )
    ]
    assert [op.op for op in parsed] == [ex["op"] for ex in examples]


def test_the_facade_hands_model_operations_to_desktop_and_keeps_them_off_the_report() -> None:
    """Stage one (the dry run) lists a model operation for Desktop and changes no report part;
    stage two (on the file Desktop saved) skips it, as it skips a measure's create."""
    step = {
        "op": "create_calculated_column",
        "table": "FactSales",
        "name": "Margin",
        "expression": "FactSales[SalesAmount] - FactSales[TotalCost]",
    }
    document = twin()
    outcome = apply(document, [step], report_edit_model.example_model())
    assert [op.op for op in outcome.model_operations] == ["create_calculated_column"]
    assert outcome.report_changed is False
    assert outcome.summaries == ["Created the calculated column FactSales[Margin]."]
    assert document.commit() == []  # no report part was written
    second = report_edits.apply_report_operations(
        twin(), report_edits.parse_operations([step]), report_edit_model.example_model()
    )
    assert (second.summaries, second.model_operations, second.report_changed) == ([], [], False)


def test_the_report_half_of_a_table_rename_rewrites_the_report() -> None:
    document = twin()
    before = {t for _, t, _ in report_edit_model.field_references(document)}
    assert "DimStore" in before
    step = {"op": "rename_table", "name": "DimStore", "new_name": "Stores"}
    outcome = report_edits.apply_report_operations(
        document, report_edits.parse_operations([step]), twin_model()
    )
    after = {t for _, t, _ in report_edit_model.field_references(document)}
    assert "DimStore" not in after and "Stores" in after
    assert outcome.report_changed is True and outcome.model_operations == []
    assert len(outcome.summaries) == 1


def test_a_column_header_is_renamed_one_way_only() -> None:
    """A field display name writes both names Desktop stores without changing its queryRef."""
    document = twin()
    step = {
        "op": "set_field_display_name",
        "visual": "e82e02f93055d6b0b544",
        "field": "MTD Sales",
        "name": "Sales this month",
    }

    def values() -> list[dict[str, Any]]:
        state = document.visual("e82e02f93055d6b0b544")[1]["visual"]["query"]["queryState"]
        return [dict(p) for p in state["Values"]["projections"]]

    before = [p for p in values() if "MTD Sales" in (p.get("displayName"), p.get("nativeQueryRef"))]
    apply(document, [step], agents_model())
    after = [p for p in values() if p.get("displayName") == "Sales this month"]
    assert len(before) == len(after) == 1
    assert after[0]["nativeQueryRef"] == "Sales this month"
    assert after[0]["queryRef"] == before[0]["queryRef"]


# --- confirmations ------------------------------------------------------------------------------

STUDIO = Path(report_edits.__file__).parent
_RAISED = re.compile(r"NeedsConfirmation\(\s*\"([a-z_]+)\"")


def raised_confirmations() -> dict[str, set[str]]:
    """Every ``NeedsConfirmation`` kind the operation families raise, by module."""
    found: dict[str, set[str]] = {}
    for path in sorted(STUDIO.glob("report_edit_*.py")):
        kinds = set(_RAISED.findall(path.read_text(encoding="utf-8")))
        if kinds:
            found[path.stem] = kinds
    return found


def test_every_confirmation_a_family_asks_for_has_an_answer() -> None:
    """A yes in chat or in the UI is the flag ``CONFIRMATION_FLAGS`` names (the service and
    ``ops_with_answer``) and ``_CONFIRM_FIELD`` names (the chat turn): a kind missing from
    either would ask the same question again after the yes."""
    raised = raised_confirmations()
    kinds = set().union(*raised.values())
    assert kinds == {
        "replace_conditional",
        "remove_page_references",
        "remove_bookmark_references",
        "clear_page_links",
        "cleanup_references",
    }, raised
    fields = {name for cls in report_edits.REPORT_OPERATIONS for name in cls.model_fields}
    for kind in sorted(kinds):
        assert kind in edit_ops.CONFIRMATION_FLAGS, kind
        flags = edit_ops.CONFIRMATION_FLAGS[kind]
        assert set(flags) <= fields and all(value is True for value in flags.values()), kind
        chat_flag = imported_chat_edits._CONFIRM_FIELD.get(kind, kind)
        assert {chat_flag} == set(flags), kind


@pytest.mark.parametrize(
    ("kind", "made"),
    [
        ("remove_bookmark_references", {"op": "delete_bookmark", "bookmark": "Overview"}),
        ("clear_page_links", {"op": "clear_page_type", "page": "Agent details"}),
        ("cleanup_references", {"op": "ungroup_visuals", "group": "KPI cards"}),
    ],
)
def test_a_yes_to_a_navigation_or_group_question_sets_cleanup_references(
    kind: str, made: dict[str, Any]
) -> None:
    (answered,) = edit_ops.ops_with_answer([made], kind, step=1)
    assert answered == {**made, "cleanup_references": True}
    (chat,) = imported_chat_edits._with_flags([made], [(kind, 1)])
    assert chat == answered
    report_edits.parse_operations([answered])


def test_the_delete_of_a_used_bookmark_asks_and_the_yes_applies() -> None:
    """End to end on the twin: the question, its kind, and the answer's batch."""
    model = agents_model()
    document = twin()
    apply(document, report_edit_navigation.EXAMPLE_SETUP, model)
    button = {
        "op": "add_button",
        "page": "1",
        "kind": "bookmark",
        "action": {"type": "bookmark", "bookmark": "Overview"},
    }
    apply(document, [button], model)
    step = {"op": "delete_bookmark", "bookmark": "Overview"}
    with pytest.raises(NeedsConfirmation) as asked:
        apply(document, [step], model)
    assert asked.value.kind == "remove_bookmark_references" and asked.value.step == 1
    answered = edit_ops.ops_with_answer([step], asked.value.kind, step=asked.value.step)
    outcome = apply(document, answered, model)
    assert outcome.summaries[0].startswith("Deleted the bookmark")


def test_an_empty_button_tooltip_clears_it_and_a_missing_one_keeps_it() -> None:
    """``set_button_action``: ``tooltip: ""`` removes the tooltip, ``tooltip`` left out (or
    null) leaves it as it is."""
    model = agents_model()
    document = twin()
    apply(document, report_edit_navigation.EXAMPLE_SETUP, model)

    def tooltip() -> Any:
        _, visual = resolve_visual(document, "Details")
        links = visual["visual"]["visualContainerObjects"]["visualLink"]
        return next((e["properties"].get("tooltip") for e in links), None)

    apply(document, [{"op": "set_button_action", "visual": "Details", "tooltip": "Stores"}], model)
    assert tooltip() == {"expr": {"Literal": {"Value": "'Stores'"}}}
    back = {"op": "set_button_action", "visual": "Details", "action": {"type": "back"}}
    apply(document, [back], model)
    assert tooltip() == {"expr": {"Literal": {"Value": "'Stores'"}}}
    outcome = apply(
        document, [{"op": "set_button_action", "visual": "Details", "tooltip": ""}], model
    )
    assert tooltip() is None
    assert outcome.summaries == ["Its tooltip was removed."]


# --- probes -------------------------------------------------------------------------------------


def probes() -> list[Any]:
    return [
        pytest.param(probe, id=f"{probe.family}-{probe.name}")
        for probe in edit_tools.desktop_probes()
    ]


@pytest.mark.parametrize("probe", probes())
def test_every_probe_parses_and_applies_to_the_twin_through_the_facade(
    probe: ProbeCase, tmp_path: Path
) -> None:
    """One batch, as the probe runner sends it; Studio's own output must pass the probe's check,
    so a failure in Desktop means Desktop changed or dropped what Studio wrote."""
    assert probe.operations and probe.expect and probe.page
    parsed = report_edits.parse_operations([dict(step) for step in probe.operations])
    assert [op.op for op in parsed] == [step["op"] for step in probe.operations]
    document = twin()
    report_edits.apply_operations(document, list(parsed), agents_model())
    if probe.check is not None:
        assert probe.check(saved_parts(document, tmp_path / "probe.pbix")) == []


def test_every_report_family_has_probes() -> None:
    families = {probe.family for probe in edit_tools.desktop_probes()}
    assert {"conditional", "navigation", "extras"} <= families


# --- cards and the capability matrix ------------------------------------------------------------


def test_every_registered_operation_has_exactly_one_card() -> None:
    cards = edit_tools.tool_cards()
    ops = [card.op for card in cards]
    assert len(ops) == len(set(ops))
    registered = sorted(op_name(cls) for cls in report_edits.REPORT_OPERATIONS)
    assert sorted(ops) == registered
    assert len(registered) == 103


def test_every_card_is_offered_to_the_planner_or_opts_out() -> None:
    offered = {card.op for card in edit_tools.planner_cards()}
    for card in edit_tools.tool_cards():
        assert (card.op in offered) == card.planner, card.op


def test_model_cards_need_desktop_and_a_yes_and_report_cards_do_not() -> None:
    model_ops = {op_name(cls) for cls in report_edits.MODEL_OPERATIONS}
    for card in edit_tools.tool_cards():
        if card.family == "model":
            assert card.op in model_ops and card.needs_desktop and card.confirm, card.op
        elif card.family in {"conditional", "navigation", "extras"}:
            assert card.op not in model_ops and not card.needs_desktop, card.op


def test_the_capability_matrix_marks_every_card_supported() -> None:
    edit_capabilities.CAPABILITY_MATRIX.refresh()
    for card in edit_tools.tool_cards():
        key = edit_capabilities.card_capability(card)
        entry = edit_capabilities.CAPABILITY_MATRIX[key]
        assert entry.status == "supported", (card.op, key)
        assert card.op in entry.family.split(", "), (card.op, key)
