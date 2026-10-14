"""Displayed field headers remain usable without replacing model bindings or leaking bodies."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ai_powerbi_analyst.studio.edit_context import EditContext
from ai_powerbi_analyst.studio.edit_planner import available_cards, build_prompt, fill_from_words
from ai_powerbi_analyst.studio.report_import import read_report


def _context(projections: list[dict[str, Any]]) -> EditContext:
    visual = SimpleNamespace(
        id="table-v",
        visual_type="tableEx",
        definition={
            "visual": {
                "visualType": "tableEx",
                "query": {"queryState": {"Values": {"projections": projections}}},
            }
        },
        bindings=[{"name": "Net Revenue"}],
        x=0,
        y=0,
        width=300,
        height=200,
    )
    report = SimpleNamespace(
        pages=[SimpleNamespace(id="p", title="Overview", width=1280, height=720, visuals=[visual])],
        measures=[{"Name": "Net Revenue", "TableName": "Metrics", "Expression": ""}],
        tables=[],
    )
    return EditContext.from_report(report, active_page="p")  # type: ignore[arg-type]


def _measure(**metadata: Any) -> dict[str, Any]:
    return {
        "field": {
            "Measure": {
                "Expression": {"SourceRef": {"Entity": "Metrics"}},
                "Property": "Net Revenue",
            }
        },
        "queryRef": "Metrics.Net Revenue",
        **metadata,
    }


def _calculation_card() -> Any:
    return next(card for card in available_cards() if card.op == "set_visual_calculation")


def test_display_and_native_aliases_keep_the_canonical_binding() -> None:
    context = _context([_measure(displayName="Sales today", nativeQueryRef="Daily revenue")])
    visual = context.visuals[0]
    assert visual.fields == ("Sales today", "Daily revenue", "Net Revenue")
    assert visual.bindings == (("measure", "Metrics", "Net Revenue"),)
    assert context.measures[0].name == "Net Revenue"


def test_missing_blank_and_repeated_aliases_do_not_replace_canonical_names() -> None:
    context = _context(
        [
            _measure(displayName=" ", nativeQueryRef=None),
            _measure(displayName="Sales today", nativeQueryRef="Sales today"),
        ]
    )
    assert context.visuals[0].fields == ("Sales today", "Net Revenue")
    assert context.visuals[0].bindings == (
        ("measure", "Metrics", "Net Revenue"),
        ("measure", "Metrics", "Net Revenue"),
    )


def test_alias_names_ground_a_template_without_copying_calculation_bodies() -> None:
    marker = "PRIVATE-CALCULATION-BODY-SENTINEL"
    context = _context(
        [
            _measure(displayName="Sales today", nativeQueryRef="Daily revenue"),
            {
                "field": {
                    "NativeVisualCalculation": {"Name": "Running revenue", "Expression": marker}
                },
                "queryRef": "Running revenue",
                "nativeQueryRef": "Running revenue",
            },
        ]
    )
    message = "add a running total of Sales today as a visual calculation to the table"
    card = _calculation_card()
    values = fill_from_words(card, message, context)
    assert values is not None
    assert values["field"] == "Sales today"
    assert values["visual"] == "table-v"
    assert values["template"] == "running_sum"
    assert "Running revenue" in context.visuals[0].fields
    prompt = build_prompt(message, context, [card])
    assert "Sales today" in prompt.user
    slots = prompt.schema["properties"]["steps"]["items"]["anyOf"][0]["properties"]["slots"]
    assert slots["properties"]["field"]["enum"] == ["Sales today"]
    assert marker not in prompt.user
    assert marker not in str(prompt.schema)
    assert marker not in str(context.visuals[0].fields)


def test_structure_twin_displayed_header_is_grounded_for_running_total() -> None:
    twin = Path(__file__).resolve().parents[2] / "fixtures" / "imported" / "structure-twin.pbix"
    pages, _, _ = read_report(twin)
    table = next(v for page in pages for v in page.visuals if v.id.startswith("e82e02f9"))
    page = next(page for page in pages if table in page.visuals)
    report = SimpleNamespace(pages=pages, measures=[], tables=[])
    context = EditContext.from_report(report, active_page=page.id)  # type: ignore[arg-type]
    entry = context.visual(table.id)
    assert entry is not None
    assert "MTD Sales" in entry.fields
    projection = next(
        projection
        for bucket in table.definition["visual"]["query"]["queryState"].values()
        for projection in bucket["projections"]
        if projection.get("nativeQueryRef") == "MTD Sales"
        or projection.get("displayName") == "MTD Sales"
    )
    canonical = projection["field"]["Measure"]["Property"]
    assert canonical != "MTD Sales"
    assert canonical in entry.fields
    assert any(binding[2] == canonical for binding in entry.bindings)
    message = "add a running total of MTD Sales as a visual calculation to the table"
    values = fill_from_words(_calculation_card(), message, context)
    assert values is not None and values["field"] == "MTD Sales"
    assert values["visual"] == table.id
