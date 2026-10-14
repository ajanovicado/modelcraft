"""The extras family: analytics lines, the phone layout, groups, visual calculations, small
multiples and accessibility settings.

Everything runs on fixtures/imported/structure-twin.pbix (the Agents page, Desktop-authored) and
multipage.pbix (groups, bookmarks, a phone layout part), with a hand-written model of the Agents
fields the operations use. Every edit is checked against the report's structural validation with
the file's own findings as the baseline, so an operation may not introduce a dangling reference.
"""

from __future__ import annotations

import copy
import json
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.studio import edit_tools, report_edits
from ai_powerbi_analyst.studio import report_edit_extras as extras
from ai_powerbi_analyst.studio.pbir_document import DocumentError, PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage
from ai_powerbi_analyst.studio.report_edit_context import (
    ModelInfo,
    NeedsChoice,
    NeedsConfirmation,
)
from ai_powerbi_analyst.studio.report_edit_geometry import page_box, resolve_page, resolve_visual
from ai_powerbi_analyst.studio.report_edit_validation import capture_baseline, validate_document
from ai_powerbi_analyst.studio.report_refs import ReferenceIndex

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "imported"
PAGE1 = "ReportSection8454c812b60b8bb83b99"
DETAILS = "ReportSection3f1c0d5e7a9b2c4d6e8f"
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"

TABLE = "e82e02f93055d6b0b544"
CHART = "b404a461ede7604a2774"  # 'Total MTD Sales', stacked column chart
CHART_AVG = "da32cdbb0090e885649b"
CHART_CHANGE = "ab13db00c71dcec86e99"
CHART_DISCOUNT = "97d1504080b699eae7d7"
CARDS = (
    "fb4c5aa34e0b084b62b9",
    "2202e311b1493069e4eb",
    "9479353528b0eb22b958",
    "71a164765d9c63200302",
)
CARD_TOPN = "8a8ba163374a72762030"
SLICERS = ("b85aa81a8b5c60030d13", "54be5cf0783e66471540", "5b0dbccd61c6c4adb415")
BUTTON = "eff2d7b04706cc92f1db"  # multipage: page 1 action button with a mobile.json
HEADER_GROUP = "40c896eda14bfa8f6a97"  # multipage Details: 'Header block' holds 'Filters'
FILTERS_GROUP = "7799291338ca3548d7d5"
SLICER_A = "04ba32659627f6e8c7d7"  # in 'Filters'
GROUP_CARD = "dfb4645ec076d1209ebe"  # in 'Header block'
MEASURE = "# Measures.TopN/BottomN/ MTD Sales"

_COLUMNS = {
    "DimChannel": ["ChannelName", "ChannelKey"],
    "DimStore": ["StoreName", "StoreKey"],
    "DimEmployee": ["EmployeeKey"],
    "Date": ["Date"],
    "Clustered Employees": ["Clustered Employees", "Order"],
    "Top-Bottom-N": ["Top-Bottom-N"],
    "Actuals-vs-PctChange": ["Top-Bottom-Field", "Order"],
}
_MEASURES = (
    "Total Sales",
    "Profit",
    "PY Total Sales",
    "Employee Name",
    "Rank",
    "Table MTD Sales",
    "vs PM MOM%",
    "vs Previous Month",
    "Employees Avg MTD Sales",
    "Employees Avg MTD % Change",
    "Employees with Positive Change %",
    "Number of Employees selling more than 100K MTD",
    "TopN/BottomN/ MTD Sales",
    "TopN/BottomN/ Avg MTD Sales",
    "TopN/BottomN/ MTD % Change",
    "TopN/BottomN/ Avg Total Discount",
    "TopN/BottomN/ PMTD Sales",
    "Rank Filtering Dynamics",
    "CF Table",
    "dummy",
)
_TYPES = {"ChannelName": 2, "StoreName": 2, "Date": 9, "Clustered Employees": 2}


def model() -> ModelInfo:
    tables = [
        {"TableName": table, "ColumnName": column, "PandasDataType": "object"}
        for table, columns in _COLUMNS.items()
        for column in columns
    ]
    measures = [{"Name": name, "TableName": "# Measures"} for name in _MEASURES]
    types = {
        "columns": [
            {"table": t, "name": c, "dataType": _TYPES.get(c, 6), "summarizeBy": "none"}
            for t, columns in _COLUMNS.items()
            for c in columns
        ]
    }
    return ModelInfo.from_report(tables, measures, types)


def load(name: str = "structure-twin") -> PbirDocument:
    return PbirDocument(PbixPackage(FIXTURES / f"{name}.pbix"))


def run(document: PbirDocument, *operations: Mapping[str, Any]) -> report_edits.EditOutcome:
    """Apply operations of any family in order (existing ones through report_edits) and hold the
    result to the report's validation, with the file's own findings as the baseline."""
    baseline = capture_baseline(document, model())
    outcome = report_edits.EditOutcome()
    for operation in operations:
        if operation["op"] in extras.EXTRAS_HANDLERS:
            step = extras.apply_extras(document, extras.parse_extras([operation]), model())
        else:
            parsed = report_edits.parse_operations([operation])
            step = report_edits.apply_operations(document, list(parsed), model())
        outcome.summaries.extend(step.summaries)
        outcome.warnings.extend(step.warnings)
        outcome.visible_in.extend(step.visible_in)
    assert validate_document(document, model(), baseline=baseline) == []
    return outcome


def visual(document: PbirDocument, name: str) -> dict[str, Any]:
    found: dict[str, Any] = document.visual(name)[1]
    return found


def objects(document: PbirDocument, name: str) -> dict[str, Any]:
    found: dict[str, Any] = visual(document, name)["visual"].get("objects", {})
    return found


def box(document: PbirDocument, name: str) -> tuple[float, float, float, float]:
    page, _ = document.visual(name)
    found = page_box(page, name)
    return found.x, found.y, found.width, found.height


def lit(value: str) -> dict[str, Any]:
    return {"expr": {"Literal": {"Value": value}}}


def hex_fill(value: str) -> dict[str, Any]:
    return {"solid": {"color": lit(f"'{value}'")}}


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


def as_clustered(document: PbirDocument, name: str = CHART) -> None:
    visual(document, name)["visual"]["visualType"] = "clusteredColumnChart"


# --- the family's exports ---------------------------------------------------------------------


def test_operations_handlers_and_cards_agree() -> None:
    ops = {cls.model_fields["op"].default for cls in extras.EXTRAS_OPERATIONS}
    assert ops == set(extras.EXTRAS_HANDLERS)
    assert len(ops) == 16
    assert [card.op for card in extras.TOOL_CARDS] == [
        cls.model_fields["op"].default for cls in extras.EXTRAS_OPERATIONS
    ]
    assert {card.family for card in extras.TOOL_CARDS} == {"extras"}
    assert all(len(card.examples) >= 3 for card in extras.TOOL_CARDS)
    assert all(not card.needs_desktop for card in extras.TOOL_CARDS)
    capabilities = {card.op: card.capability for card in extras.TOOL_CARDS}
    assert capabilities["set_analytics_line"] == "analytics_lines"
    assert capabilities["group_visuals"] == "group_align_distribute"
    assert capabilities["arrange_mobile_layout"] == "mobile_layout"
    analytics = next(c for c in extras.TOOL_CARDS if c.op == "set_analytics_line")
    assert {"columnChart", "lineChart", "scatterChart"} <= set(analytics.visual_types)
    assert "tableEx" not in analytics.visual_types
    split = next(c for c in extras.TOOL_CARDS if c.op == "set_small_multiples")
    assert {"columnChart", "cardVisual"} <= set(split.visual_types)
    existing = {cls.model_fields["op"].default for cls in report_edits.REPORT_OPERATIONS}
    assert ops <= existing  # registered in the facade (round 3 integration)
    assert [c.op for c in edit_tools.tool_cards(("report_edit_extras",))] == sorted(
        ops, key=[c.op for c in extras.TOOL_CARDS].index
    )


def test_shape_registries_name_every_unconfirmed_construct() -> None:
    assert set(extras.VERIFIED_SHAPES) == {
        "group.container",
        "field.display_name",
        "position.tabOrder",
        "page.displayOption.FitToPage",
        "container.general.keepLayerOrder",
    }
    for key in (
        "analytics.dynamic_lines",
        "analytics.error_bars",
        "analytics.forecast",
        "mobile.json",
        "visual_calculation",
        "small_multiples",
        "group.scroll_mode",
        "group.nested",
        "group.z_and_tab_order",
        "field.blank_header_again",
    ):
        assert key in extras.UNVERIFIED_SHAPES
    assert not set(extras.VERIFIED_SHAPES) & set(extras.UNVERIFIED_SHAPES)
    assert "visualContainerMobileState/2.7.0/" in extras.MOBILE_SCHEMA


# What an example needs on the twin before it can apply (a group to ungroup, a line to remove).
SETUP: dict[str, list[dict[str, Any]]] = {
    "remove_analytics_line": [
        {
            "op": "set_analytics_line",
            "visual": CHART,
            "kind": "constant",
            "value": 1,
            "name": "Target",
        },
        {"op": "set_analytics_line", "visual": CHART_AVG, "kind": "trend"},
    ],
    "set_analytics_line": [],
    "remove_from_mobile": [{"op": "arrange_mobile_layout", "page": "1"}],
    "ungroup_visuals": [
        {"op": "group_visuals", "visuals": list(CARDS), "name": "KPI cards"},
        {"op": "group_visuals", "visuals": [CHART, CHART_AVG], "name": "Sales charts"},
        {"op": "group_visuals", "visuals": list(SLICERS), "name": "Filters"},
    ],
    "remove_visual_calculation": [
        {
            "op": "set_visual_calculation",
            "visual": TABLE,
            "name": "Running MTD Sales",
            "template": "running_sum",
            "field": "MTD Sales",
        },
        {
            "op": "set_visual_calculation",
            "visual": TABLE,
            "name": "Share of MTD Sales",
            "template": "percent_of_grand_total",
            "field": "MTD Sales",
        },
        {
            "op": "set_visual_calculation",
            "visual": CHART,
            "name": "Moving average",
            "template": "moving_average",
            "field": "TopN/BottomN/ MTD Sales",
        },
    ],
}
SETUP["set_group"] = SETUP["ungroup_visuals"]
SETUP["set_small_multiples"] = [
    {
        "op": "set_small_multiples",
        "visual": CHART,
        "field": {"table": "DimStore", "name": "StoreName"},
    }
]


@pytest.mark.parametrize(
    ("op", "index"),
    [(card.op, i) for card in extras.TOOL_CARDS for i in range(len(card.examples))],
)
def test_every_card_example_parses_and_applies_to_the_twin(op: str, index: int) -> None:
    card = next(c for c in extras.TOOL_CARDS if c.op == op)
    request, operation = card.examples[index]
    assert request and operation["op"] == op
    parsed = extras.EXTRAS_ADAPTER.validate_python(dict(operation))
    assert parsed.op == op
    document = load()
    outcome = run(document, *SETUP.get(op, []), operation)
    assert outcome.summaries[-1]


@pytest.mark.parametrize("card", extras.TOOL_CARDS, ids=lambda card: card.op)
def test_card_slots_rebuild_the_examples(card: edit_tools.ToolCard) -> None:
    """Every field an example uses is a slot, so the planner can fill any example's shape."""
    for _, operation in card.examples:
        if card.build is not None:
            continue
        filled = {
            slot.name: operation[slot.field or slot.name]
            for slot in card.slots
            if (slot.field or slot.name) in operation
        }
        assert card.to_operation(filled) == dict(operation)
    for slot in card.slots:
        if slot.kind == "enum":
            assert slot.choices


def card_for(op: str) -> edit_tools.ToolCard:
    return next(c for c in extras.TOOL_CARDS if c.op == op)


def test_remove_cards_read_all_as_a_choice_not_an_off() -> None:
    lines = card_for("remove_analytics_line")
    assert lines.to_operation({"visual": CHART, "scope": "all"}) == {
        "op": "remove_analytics_line",
        "visual": CHART,
        "all": True,
    }
    assert lines.to_operation({"visual": CHART, "kind": "trend"}) == {
        "op": "remove_analytics_line",
        "visual": CHART,
        "kind": "trend",
    }
    mobile = card_for("remove_from_mobile")
    assert mobile.to_operation({"page": "1", "scope": "all"}) == {
        "op": "remove_from_mobile",
        "page": "1",
        "all": True,
    }
    # "take all the slicers off the phone layout": the slicers, not the page
    assert mobile.to_operation({"visuals": list(SLICERS), "page": "1", "scope": "all"}) == {
        "op": "remove_from_mobile",
        "visuals": list(SLICERS),
        "page": "1",
    }
    split = card_for("set_small_multiples")
    assert split.to_operation({"visual": CHART, "small_multiples": False, "rows": 2}) == {
        "op": "set_small_multiples",
        "visual": CHART,
        "remove": True,
    }
    assert split.to_operation({"visual": CHART, "small_multiples": True, "rows": 2}) == {
        "op": "set_small_multiples",
        "visual": CHART,
        "rows": 2,
    }


def _planner_context() -> Any:
    import importlib.util
    import sys

    path = Path(__file__).resolve().parents[2] / "evals" / "imported_edit" / "contexts.py"
    name = "extras_planner_contexts"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name].context_for("agents", {})


def test_the_planners_own_fillers_make_operations_the_family_accepts() -> None:
    """The card tier with no model: every slot the planner's helpers fill from an example's
    words lands in an operation the family parses and applies, and agrees with the example."""
    from ai_powerbi_analyst.studio import edit_planner as planner

    context = _planner_context()
    applied = 0
    for card in extras.TOOL_CARDS:
        for request, expected in card.examples:
            filled = planner.fill_from_words(card, request, context)
            if filled is None:
                continue  # a slot the words do not give: the model fills it
            operation = card.to_operation(filled)
            try:
                extras.parse_extras([operation])
            except DocumentError:
                continue  # says too little to be an edit: the model fills it
            colors = {s.field or s.name for s in card.slots if s.kind == "color"}
            pages = {s.field or s.name for s in card.slots if s.kind == "page"}
            for key, value in operation.items():
                # a color word is the planner's palette's ("grey" is the theme's grey)
                if key not in expected or key == "visuals" or key in colors:
                    continue
                if key in pages:  # the planner names a page by its id, an example by its name
                    twin = load()
                    value = resolve_page(twin, value).name
                    assert value == resolve_page(twin, expected[key]).name, (card.op, request)
                    continue
                assert value == expected[key], (card.op, request, key)
            try:
                run(load(), *SETUP.get(card.op, []), operation)
            except (NeedsChoice, DocumentError):
                continue  # the edit asks which, or refuses (a source painted onto itself)
            applied += 1
    assert applied >= 10
    # words the planner cannot read never become a removal or a reset
    for op, words in (
        ("set_alt_text", "set the alt text of the agents table to Agents ranked by MTD sales"),
        ("set_field_display_name", "call the Clustered Employees axis Agent group on the chart"),
        ("set_small_multiples", "split the Total MTD Sales chart by channel"),
    ):
        filled = planner.fill_from_words(card_for(op), words, context)
        if filled is not None:
            with pytest.raises(DocumentError):
                extras.parse_extras([card_for(op).to_operation(filled)])
    # a template takes the field the words name, as the visual shows it; without its field
    # a template is a guess and the words fill nothing (the model's to read, or to ask)
    running = card_for("set_visual_calculation")
    filled = planner.fill_from_words(
        running, "add a running total of MTD Sales to the agents table", context
    )
    assert filled is None or (filled.get("template") and filled.get("field") == "MTD Sales")
    no_field = "add a running total to the agents table"
    assert planner.fill_from_words(running, no_field, context) is None
    # field slots land as the operation field takes them: a name where it takes text
    analytics = card_for("set_analytics_line")
    measure = next(s for s in analytics.slots if s.name == "measure")
    shape = planner._target_shape(analytics, measure)
    assert planner._field_value("measure", "# Measures", "Profit", shape) == "Profit"
    split = card_for("set_small_multiples")
    column = next(s for s in split.slots if s.name == "field")
    assert planner._field_value(
        "column", "DimChannel", "ChannelName", planner._target_shape(split, column)
    ) == {"table": "DimChannel", "name": "ChannelName"}
    # text slots that name a field of the visual take the name as the visual shows it
    for op in ("set_visual_calculation", "set_field_display_name"):
        assert next(s for s in card_for(op).slots if s.name == "field").kind == "text"
    # bools and choices from the words
    keep = next(s for s in card_for("set_keep_layer_order").slots if s.name == "layer_order")
    assert planner._bool_value(keep, "stop maintaining layer order on the card") is False
    assert planner._bool_value(keep, "keep the card's layer order") is True
    toggle = next(s for s in split.slots if s.name == "small_multiples")
    assert planner._bool_value(toggle, "turn off the small multiples") is False
    assert planner._bool_value(toggle, "remove the small multiples of the chart") is False
    scope = next(s for s in card_for("remove_analytics_line").slots if s.name == "scope")
    assert planner._choice_in(scope, "clear all analytics lines from the chart") == "all"
    kind = next(s for s in analytics.slots if s.name == "kind")
    assert planner._choice_in(kind, "add a trend line") == "trend"


def _probe_run(document: PbirDocument, operations: Sequence[Mapping[str, Any]]) -> None:
    run(document, *operations)


@pytest.mark.parametrize("probe", extras.DESKTOP_PROBES, ids=lambda probe: probe.name)
def test_desktop_probes_apply_to_the_twin_and_their_checks_pass_on_studio_output(
    probe: edit_tools.ProbeCase, tmp_path: Path
) -> None:
    """A probe is a batch the lead opens in Desktop; Studio's own output must already pass its
    check, so a failure in Desktop means Desktop changed or dropped what Studio wrote."""
    assert probe.family == "extras"
    assert probe.expect
    document = load()
    _probe_run(document, probe.operations)
    if probe.check is not None:
        assert probe.check(saved_parts(document, tmp_path / "probe.pbix")) == []


def test_probes_cover_every_operation() -> None:
    used = {op["op"] for probe in extras.DESKTOP_PROBES for op in probe.operations}
    assert set(extras.EXTRAS_HANDLERS) - used == {
        "remove_analytics_line",
        "remove_visual_calculation",
    }
    names = {probe.name for probe in extras.DESKTOP_PROBES}
    assert "extras_page_view_actual_size" in names
    assert all(probe.check is not None for probe in extras.DESKTOP_PROBES)
    views = {
        op["view"]
        for probe in extras.DESKTOP_PROBES
        for op in probe.operations
        if op["op"] == "set_page_view"
    }
    assert views == {"fit_to_width", "actual_size"}
    kinds = {
        op["kind"]
        for probe in extras.DESKTOP_PROBES
        for op in probe.operations
        if op["op"] == "set_analytics_line"
    }
    assert kinds == {
        "constant",
        "x_constant",
        "min",
        "max",
        "average",
        "median",
        "percentile",
        "trend",
        "forecast",
        "error_bars",
        "ratio",
        "symmetry_shading",
    }


def test_a_probe_check_reports_what_desktop_dropped(tmp_path: Path) -> None:
    probe = next(p for p in extras.DESKTOP_PROBES if p.name == "extras_group_cards")
    document = load()
    _probe_run(document, probe.operations)
    parts = saved_parts(document, tmp_path / "probe.pbix")
    card = next(k for k in parts if f"/visuals/{CARDS[1]}/visual.json" in k)
    parts[card]["position"]["x"] = 0
    assert probe.check is not None
    assert probe.check(parts) == [f"{CARDS[1]} moved from (612.0, 154.0) to (173.0, 154.0)"]


def _saved_visual(parts: dict[str, Any], title: str) -> dict[str, Any]:
    for name, part in parts.items():
        if name.endswith("/visual.json") and title in json.dumps(part):
            found: dict[str, Any] = part
            return found
    raise AssertionError(title)


def test_probe_checks_compare_what_desktop_saved_not_how_much(tmp_path: Path) -> None:
    probe = next(p for p in extras.DESKTOP_PROBES if p.name == "extras_forecast_and_x_line")
    assert probe.check is not None
    document = load()
    _probe_run(document, probe.operations)
    parts = saved_parts(document, tmp_path / "probe.pbix")
    assert probe.check(parts) == []
    forecast = _saved_visual(parts, "Probe sales trend")["visual"]["objects"]["forecast"][0]
    forecast["properties"]["transform"]["parameters"][1]["Literal"]["Value"] = "10L"
    assert len(probe.check(parts)) == 1
    forecast["properties"]["transform"]["parameters"][1]["Literal"]["Value"] = "6L"
    kept = forecast["selector"]
    forecast["selector"] = {"metadata": "# Measures.Total Sales"}
    assert len(probe.check(parts)) == 1
    forecast["selector"] = kept
    forecast["properties"].pop("transform")
    assert len(probe.check(parts)) == 1
    mobile = next(p for p in extras.DESKTOP_PROBES if p.name == "extras_mobile_layout")
    assert mobile.check is not None
    document = load()
    _probe_run(document, mobile.operations)
    parts = saved_parts(document, tmp_path / "mobile.pbix")
    assert mobile.check(parts) == []
    first = next(k for k in parts if k.endswith("/mobile.json"))
    parts[first]["$schema"] = SCHEMA + "visualContainerMobileState/2.12.0/schema.json"
    assert mobile.check(parts)


# --- analytics lines -------------------------------------------------------------------------


def test_constant_line_is_written_with_its_label_and_style() -> None:
    document = load()
    outcome = run(
        document,
        {
            "op": "set_analytics_line",
            "visual": CHART,
            "kind": "constant",
            "value": 50000,
            "name": "Target",
            "color": "#d64550",
            "style": "dashed",
            "width": 2,
            "transparency": 20,
            "position": "front",
            "data_label": True,
            "label_text": "value_and_name",
            "label_horizontal": "right",
            "label_vertical": "above",
            "label_decimals": 0,
            "label_units": "thousands",
            "shade": "before",
            "shade_color": {"theme": 2, "percent": 0.4},
            "shade_transparency": 80,
        },
    )
    assert outcome.summaries == [
        "Added a constant line 'Target' at 50,000 to the 'Total MTD Sales' stacked column chart."
    ]
    assert objects(document, CHART)["y1AxisReferenceLine"] == [
        {
            "properties": {
                "value": lit("50000D"),
                "show": lit("true"),
                "displayName": lit("'Target'"),
                "lineColor": hex_fill("#D64550"),
                "style": lit("'dashed'"),
                "width": lit("2D"),
                "transparency": lit("20D"),
                "position": lit("'front'"),
                "dataLabelShow": lit("true"),
                "dataLabelText": lit("'ValueAndName'"),
                "dataLabelHorizontalPosition": lit("'right'"),
                "dataLabelVerticalPosition": lit("'above'"),
                "dataLabelDecimalPoints": lit("0D"),
                "dataLabelDisplayUnits": lit("1000D"),
                "shadeColor": {
                    "solid": {"color": {"expr": {"ThemeDataColor": {"ColorId": 2, "Percent": 0.4}}}}
                },
                "shadeTransparency": lit("80D"),
                "shadeShow": lit("true"),
                "shadeRegion": lit("'before'"),
            },
            "selector": {"id": "1"},
        }
    ]


def test_where_a_line_shows_follows_what_the_canvas_draws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drawn = {"show", "value", "lineColor", "displayName"}
    monkeypatch.setattr(
        extras, "property_is_rendered", lambda _t, _o, prop, container=None: prop in drawn
    )
    operation = {
        "op": "set_analytics_line",
        "visual": CHART,
        "kind": "constant",
        "value": 1,
        "shade": "after",
        "style": "dashed",
    }
    outcome = run(load(), operation)
    assert [(v.where, v.detail) for v in outcome.visible_in] == [
        (
            "powerbi_only",
            "Studio draws the constant line; its line style and shading show only in Power BI.",
        )
    ]
    drawn.clear()
    outcome = run(load(), operation)
    assert [(v.where, v.detail) for v in outcome.visible_in] == [
        ("powerbi_only", "Studio's canvas does not draw constant line yet; Power BI shows it.")
    ]
    drawn.update({"show", "value", "displayName", "shadeShow", "shadeRegion", "style"})
    assert run(load(), operation).visible_in == []


def test_a_second_constant_line_gets_the_next_id_and_default_name() -> None:
    document = load()
    run(
        document,
        {"op": "set_analytics_line", "visual": CHART, "kind": "constant", "value": 1},
        {
            "op": "set_analytics_line",
            "visual": CHART,
            "kind": "constant",
            "value": "2,500",
            "new": True,
        },
    )
    entries = objects(document, CHART)["y1AxisReferenceLine"]
    assert [e["selector"] for e in entries] == [{"id": "1"}, {"id": "2"}]
    assert [e["properties"]["displayName"] for e in entries] == [
        lit("'Y-Axis Constant Line 1'"),
        lit("'Y-Axis Constant Line 2'"),
    ]
    assert entries[1]["properties"]["value"] == lit("2500D")


def test_default_names_take_the_first_free_number_so_they_never_repeat() -> None:
    document = load()
    add = {"op": "set_analytics_line", "visual": CHART, "kind": "constant", "new": True}
    run(document, {**add, "value": 1}, {**add, "value": 2}, {**add, "value": 3})
    run(document, {"op": "remove_analytics_line", "visual": CHART, "line": 1})
    run(document, {**add, "value": 4}, {**add, "value": 5})
    entries = objects(document, CHART)["y1AxisReferenceLine"]
    assert [e["properties"]["displayName"] for e in entries] == [
        lit("'Y-Axis Constant Line 2'"),
        lit("'Y-Axis Constant Line 3'"),
        lit("'Y-Axis Constant Line 1'"),
        lit("'Y-Axis Constant Line 4'"),
    ]
    # ids only grow: a removed line's id is not given again
    assert [e["selector"]["id"] for e in entries] == ["2", "3", "4", "5"]


def test_lines_with_the_same_name_are_chosen_by_number() -> None:
    document = load()
    add = {"op": "set_analytics_line", "visual": CHART, "kind": "constant", "new": True}
    run(document, {**add, "value": 1, "name": "Goal"}, {**add, "value": 2, "name": "Goal"})
    with pytest.raises(NeedsChoice) as raised:
        run(document, {"op": "set_analytics_line", "visual": CHART, "kind": "constant", "width": 3})
    fills = [c.fill for c in raised.value.options]
    assert fills[:2] == [{"line": 1}, {"line": 2}]
    with pytest.raises(NeedsChoice) as named:
        run(
            document,
            {
                "op": "set_analytics_line",
                "visual": CHART,
                "kind": "constant",
                "line": "goal",
                "width": 3,
            },
        )
    assert [c.fill for c in named.value.options] == [{"line": 1}, {"line": 2}]
    run(document, {**add, "new": False, "line": 2, "width": 3})
    entries = objects(document, CHART)["y1AxisReferenceLine"]
    assert "width" not in entries[0]["properties"]
    assert entries[1]["properties"]["width"] == lit("3D")


def test_changing_a_line_edits_its_entry_in_place() -> None:
    document = load()
    run(
        document,
        {
            "op": "set_analytics_line",
            "visual": CHART,
            "kind": "constant",
            "value": 5,
            "name": "Goal",
        },
    )
    outcome = run(
        document,
        {
            "op": "set_analytics_line",
            "visual": CHART,
            "kind": "constant",
            "color": "#00FF00",
            "value": 7,
        },
    )
    assert outcome.summaries == [
        "Changed the constant line 'Goal' on the 'Total MTD Sales' stacked column chart: moved "
        "it to 7 and changed its color."
    ]
    (entry,) = objects(document, CHART)["y1AxisReferenceLine"]
    assert entry["properties"]["value"] == lit("7D")
    assert entry["properties"]["lineColor"] == hex_fill("#00FF00")
    styled = run(
        document,
        {"op": "set_analytics_line", "visual": CHART, "kind": "constant", "style": "dotted"},
    )
    assert styled.summaries == [
        "Changed the constant line 'Goal' on the 'Total MTD Sales' stacked column chart: line "
        "style."
    ]
    from_measure = run(
        document,
        {
            "op": "set_analytics_line",
            "visual": CHART,
            "kind": "constant",
            "measure": "Employees Avg MTD Sales",
        },
    )
    assert from_measure.summaries == [
        "Changed the constant line 'Goal' on the 'Total MTD Sales' stacked column chart: it now "
        "takes its value from the measure 'Employees Avg MTD Sales'."
    ]
    same = run(
        document, {"op": "set_analytics_line", "visual": CHART, "kind": "constant", "line": "goal"}
    )
    assert same.summaries == [
        "The constant line 'Goal' on the 'Total MTD Sales' stacked column chart was already set "
        "that way."
    ]


def test_constant_line_from_a_measure_uses_the_measure_as_its_value() -> None:
    document = load()
    run(
        document,
        {
            "op": "set_analytics_line",
            "visual": CHART,
            "kind": "constant",
            "measure": "Employees Avg MTD Sales",
        },
    )
    (entry,) = objects(document, CHART)["y1AxisReferenceLine"]
    assert entry["properties"]["value"] == {
        "expr": {
            "Measure": {
                "Expression": {"SourceRef": {"Entity": "# Measures"}},
                "Property": "Employees Avg MTD Sales",
            }
        }
    }
    with pytest.raises(DocumentError, match="no column or measure named 'Nope'"):
        run(
            load(),
            {"op": "set_analytics_line", "visual": CHART, "kind": "constant", "measure": "Nope"},
        )


def test_dynamic_lines_aggregate_the_series_they_follow() -> None:
    document = load()
    as_clustered(document)
    outcome = run(
        document,
        {"op": "set_analytics_line", "visual": CHART, "kind": "average", "color": "#118DFF"},
        {"op": "set_analytics_line", "visual": CHART, "kind": "min"},
        {"op": "set_analytics_line", "visual": CHART, "kind": "max"},
        {"op": "set_analytics_line", "visual": CHART, "kind": "median"},
        {"op": "set_analytics_line", "visual": CHART, "kind": "percentile", "percentile": 90},
    )
    assert outcome.summaries[0] == (
        "Added an average line 'Average line 1' to the 'Total MTD Sales' clustered column chart."
    )
    assert outcome.summaries[4] == (
        "Added a percentile line 'Percentile line 1' at the 90th percentile to the 'Total MTD "
        "Sales' clustered column chart."
    )
    entries = objects(document, CHART)["referenceLine"]
    select = {"SelectRef": {"ExpressionName": MEASURE}}
    assert entries[0] == {
        "properties": {
            "value": {"expr": {"Aggregation": {"Expression": select, "Function": 1}}},
            "show": lit("true"),
            "displayName": lit("'Average line 1'"),
            "lineColor": hex_fill("#118DFF"),
        },
        "selector": {"metadata": MEASURE, "id": "1"},
    }
    assert [
        e["properties"]["value"]["expr"].get("Aggregation", {}).get("Function") for e in entries
    ] == [
        1,
        3,
        4,
        6,
        None,
    ]
    assert entries[4]["properties"]["value"] == {
        "expr": {"Percentile": {"Expression": select, "K": 0.9}}
    }
    assert [e["selector"]["id"] for e in entries] == ["1", "2", "3", "4", "5"]
    lines = extras.analytics_lines(visual(document, CHART))
    assert [(line.kind, line.measure, line.number) for line in lines] == [
        ("average", MEASURE, 1),
        ("min", MEASURE, 1),
        ("max", MEASURE, 1),
        ("median", MEASURE, 1),
        ("percentile", MEASURE, 1),
    ]


def test_the_lines_each_visual_type_offers_come_from_the_catalog() -> None:
    assert extras.analytics_kinds("columnChart") == [
        "constant",
        "x_constant",
        "trend",
        "error_bars",
    ]
    assert extras.analytics_kinds("lineChart") == [
        "constant",
        "x_constant",
        "min",
        "max",
        "average",
        "median",
        "percentile",
        "trend",
        "forecast",
        "error_bars",
    ]
    assert extras.analytics_kinds("scatterChart") == [
        "constant",
        "x_constant",
        "min",
        "max",
        "average",
        "median",
        "percentile",
        "trend",
        "ratio",
        "symmetry_shading",
    ]
    assert extras.analytics_kinds("tableEx") == []


def test_changing_a_dynamic_line_keeps_what_was_not_said() -> None:
    document = load()
    as_clustered(document)
    second = copy.deepcopy(visual(document, CHART_AVG)["visual"]["query"]["queryState"]["Y"])
    state = visual(document, CHART)["visual"]["query"]["queryState"]
    state["Y"]["projections"] += second["projections"]
    run(
        document,
        {
            "op": "set_analytics_line",
            "visual": CHART,
            "kind": "percentile",
            "percentile": 33.3,
            "measure": "TopN/BottomN/ MTD Sales",
        },
    )
    changed = run(
        document,
        {
            "op": "set_analytics_line",
            "visual": CHART,
            "kind": "percentile",
            "line": 1,
            "measure": "TopN/BottomN/ Avg MTD Sales",
        },
    )
    assert changed.summaries == [
        "Changed the percentile line 'Percentile line 1' on the 'Total MTD Sales' clustered column "
        "chart: it now follows 'TopN/BottomN/ Avg MTD Sales'."
    ]
    (entry,) = objects(document, CHART)["referenceLine"]
    other = "# Measures.TopN/BottomN/ Avg MTD Sales"
    assert entry["properties"]["value"] == {
        "expr": {"Percentile": {"Expression": {"SelectRef": {"ExpressionName": other}}, "K": 0.333}}
    }
    assert entry["selector"] == {"metadata": other, "id": "1"}
    moved = run(
        document,
        {"op": "set_analytics_line", "visual": CHART, "kind": "percentile", "percentile": 75},
    )
    assert moved.summaries == [
        "Changed the percentile line 'Percentile line 1' on the 'Total MTD Sales' clustered column "
        "chart: it now marks the 75th percentile."
    ]


def test_changing_error_bars_keeps_the_bound_that_was_not_said() -> None:
    document = load()
    run(
        document,
        {
            "op": "set_analytics_line",
            "visual": CHART,
            "kind": "error_bars",
            "upper": "PY Total Sales",
            "lower": "Profit",
        },
        {"op": "set_analytics_line", "visual": CHART, "kind": "error_bars", "upper": "Total Sales"},
    )
    (entry,) = objects(document, CHART)["error"]
    bounds = entry["properties"]["errorRange"]["explicit"]
    assert bounds["upperBound"]["expr"]["Measure"]["Property"] == "Total Sales"
    assert bounds["lowerBound"]["expr"]["Measure"]["Property"] == "Profit"


def test_a_chart_without_the_line_says_which_visuals_have_it() -> None:
    with pytest.raises(DocumentError) as raised:
        run(load(), {"op": "set_analytics_line", "visual": CHART, "kind": "average"})
    assert str(raised.value) == (
        "A stacked column chart has no average line in Power BI's Analytics pane. Visuals that "
        "have one: clustered column chart, clustered bar chart, line chart, area chart and "
        "scatter chart."
    )


def test_a_setting_the_line_does_not_have_is_refused_by_name() -> None:
    with pytest.raises(
        DocumentError, match="^The trend line has no data label setting in Power BI$"
    ):
        run(
            load(),
            {"op": "set_analytics_line", "visual": CHART, "kind": "trend", "data_label": True},
        )
    with pytest.raises(DocumentError, match="^The trend line has no shading setting in Power BI$"):
        run(
            load(), {"op": "set_analytics_line", "visual": CHART, "kind": "trend", "shade": "after"}
        )


def test_two_lines_of_a_kind_ask_which_one() -> None:
    document = load()
    as_clustered(document)
    run(
        document,
        {"op": "set_analytics_line", "visual": CHART, "kind": "average"},
        {
            "op": "set_analytics_line",
            "visual": CHART,
            "kind": "average",
            "new": True,
            "name": "Two",
        },
    )
    with pytest.raises(NeedsChoice) as raised:
        run(document, {"op": "set_analytics_line", "visual": CHART, "kind": "average", "width": 3})
    assert [(c.label, c.fill) for c in raised.value.options] == [
        ("Average line 1", {"line": "Average line 1"}),
        ("Two", {"line": "Two"}),
        ("Add another average line", {"new": True}),
    ]
    run(
        document,
        {"op": "set_analytics_line", "visual": CHART, "kind": "average", "line": 2, "width": 3},
    )
    assert objects(document, CHART)["referenceLine"][1]["properties"]["width"] == lit("3D")


def test_a_dynamic_line_on_a_chart_with_two_values_asks_which_value() -> None:
    document = load()
    as_clustered(document)
    second = copy.deepcopy(visual(document, CHART_AVG)["visual"]["query"]["queryState"]["Y"])
    visual(document, CHART)["visual"]["query"]["queryState"]["Y"]["projections"] += second[
        "projections"
    ]
    with pytest.raises(NeedsChoice) as raised:
        run(document, {"op": "set_analytics_line", "visual": CHART, "kind": "max"})
    assert [c.fill for c in raised.value.options] == [
        {"measure": "TopN/BottomN/ MTD Sales"},
        {"measure": "TopN/BottomN/ Avg MTD Sales"},
    ]
    run(
        document,
        {
            "op": "set_analytics_line",
            "visual": CHART,
            "kind": "max",
            "measure": "TopN/BottomN/ Avg MTD Sales",
        },
    )
    (entry,) = objects(document, CHART)["referenceLine"]
    assert entry["selector"] == {"metadata": "# Measures.TopN/BottomN/ Avg MTD Sales", "id": "1"}
    with pytest.raises(DocumentError, match="'Clustered Employees' is not one"):
        run(
            document,
            {
                "op": "set_analytics_line",
                "visual": CHART,
                "kind": "min",
                "measure": "Clustered Employees",
            },
        )


def test_trend_error_bars_forecast_and_date_line_on_a_line_chart() -> None:
    document = load()
    run(
        document,
        {
            "op": "add_visual",
            "page": "1",
            "visual_type": "lineChart",
            "fields": [
                {"table": "Date", "name": "Date"},
                {"table": "# Measures", "name": "Total Sales"},
            ],
            "title": "Sales trend",
            "x": 20,
            "y": 1000,
            "width": 400,
            "height": 150,
        },
    )
    outcome = run(
        document,
        {
            "op": "set_analytics_line",
            "visual": "Sales trend",
            "kind": "forecast",
            "transparency": 30,
        },
        {
            "op": "set_analytics_line",
            "visual": "Sales trend",
            "kind": "x_constant",
            "value": "2019-06-30",
        },
        {
            "op": "set_analytics_line",
            "visual": "Sales trend",
            "kind": "error_bars",
            "upper": "PY Total Sales",
            "lower": "# Measures[Profit]",
            "band": True,
        },
        {
            "op": "set_analytics_line",
            "visual": "Sales trend",
            "kind": "trend",
            "combine_series": False,
        },
    )
    assert outcome.summaries == [
        "Added a forecast 'Forecast 1' to the 'Sales trend' line chart.",
        "Added an X-axis constant line 'X-Axis Constant Line 1' at 2019-06-30 to the 'Sales "
        "trend' line chart.",
        "Added error bars to the 'Sales trend' line chart.",
        "Added a trend line 'Trend line 1' to the 'Sales trend' line chart.",
    ]
    assert outcome.warnings == [
        "Error bars are not confirmed yet: Studio writes them the way Power BI Desktop's own code "
        "saves them, but has not seen Desktop keep them in a file. Check them in Power BI Desktop."
    ]
    _, line = resolve_visual(document, "Sales trend")
    found = line["visual"]["objects"]

    def parameters(*values: tuple[str, str]) -> list[dict[str, Any]]:
        return [{"Literal": {"Value": value}, "Name": name} for name, value in values]

    assert found["forecast"] == [
        {
            "properties": {
                "transform": {
                    "algorithm": "Forecast",
                    "parameters": parameters(
                        ("Unit", "7L"),
                        ("ForecastLength", "10L"),
                        ("IgnoreLast", "0L"),
                        ("ConfidenceLevel", "0.95D"),
                    ),
                },
                "show": lit("true"),
                "displayName": lit("'Forecast 1'"),
                "strokeTransparency": lit("30D"),
            },
            "selector": {"metadata": "# Measures.Total Sales", "id": "1"},
        }
    ]
    settings = run(
        document,
        {
            "op": "set_analytics_line",
            "visual": "Sales trend",
            "kind": "forecast",
            "forecast_length": 6,
            "seasonality": 12,
            "confidence": 90,
        },
    )
    assert settings.summaries == [
        "Changed the forecast 'Forecast 1' on the 'Sales trend' line chart: changed its forecast "
        "settings."
    ]
    assert found["forecast"][0]["properties"]["transform"]["parameters"] == parameters(
        ("Unit", "7L"),
        ("ForecastLength", "6L"),
        ("IgnoreLast", "0L"),
        ("ConfidenceLevel", "0.9D"),
        ("MaxSeasonality", "12L"),
    )
    assert found["xAxisReferenceLine"][0]["properties"]["value"] == lit(
        "datetime'2019-06-30T00:00:00'"
    )

    def measure(name: str) -> dict[str, Any]:
        return {
            "expr": {
                "Measure": {"Expression": {"SourceRef": {"Entity": "# Measures"}}, "Property": name}
            }
        }

    assert found["error"] == [
        {
            "properties": {
                "errorRange": {
                    "kind": "ErrorRange",
                    "explicit": {
                        "isRelative": lit("false"),
                        "upperBound": measure("PY Total Sales"),
                        "lowerBound": measure("Profit"),
                    },
                },
                "enabled": lit("true"),
                "shadeShow": lit("true"),
            },
            "selector": {"metadata": "# Measures.Total Sales"},
        }
    ]
    assert found["trend"][0]["properties"]["combineSeries"] == lit("false")
    with pytest.raises(DocumentError, match="need the two measures"):
        run(
            load(),
            {"op": "set_analytics_line", "visual": CHART, "kind": "error_bars", "upper": "Profit"},
        )
    with pytest.raises(DocumentError, match="^The axis 'Date' holds dates: give the line a date"):
        run(
            document,
            {
                "op": "set_analytics_line",
                "visual": "Sales trend",
                "kind": "x_constant",
                "value": 5,
                "new": True,
            },
        )
    with pytest.raises(DocumentError, match="axis 'Clustered Employees' is text$"):
        run(load(), {"op": "set_analytics_line", "visual": CHART, "kind": "x_constant", "value": 1})
    with pytest.raises(DocumentError, match="'2019-13' is not a number or a date"):
        run(
            document,
            {
                "op": "set_analytics_line",
                "visual": "Sales trend",
                "kind": "x_constant",
                "value": "2019-13",
                "new": True,
            },
        )


def _line_chart(document: PbirDocument, *fields: Mapping[str, str], title: str = "Trend") -> None:
    run(
        document,
        {
            "op": "add_visual",
            "page": "1",
            "visual_type": "lineChart",
            "fields": list(fields),
            "title": title,
            "x": 20,
            "y": 1000,
            "width": 400,
            "height": 150,
        },
    )


DATE = {"table": "Date", "name": "Date"}
TOTAL = {"table": "# Measures", "name": "Total Sales"}


def test_a_forecast_needs_one_value_no_legend_and_a_date_or_number_axis() -> None:
    forecast = {"op": "set_analytics_line", "visual": "Trend", "kind": "forecast"}
    two = load()
    _line_chart(two, DATE, TOTAL, {"table": "# Measures", "name": "Profit"})
    with pytest.raises(DocumentError) as raised:
        run(two, forecast)
    assert str(raised.value) == (
        "Power BI forecasts a line chart with exactly one value; this one has 2 ('Total Sales' "
        "and 'Profit'). Keep one value in the chart to add a forecast"
    )
    legend = load()
    _line_chart(legend, DATE, TOTAL)
    _, line = resolve_visual(legend, "Trend")
    state = line["visual"]["query"]["queryState"]
    state["Series"] = copy.deepcopy(
        visual(legend, CHART)["visual"]["query"]["queryState"]["Category"]
    )
    with pytest.raises(DocumentError, match="^Power BI cannot forecast a line chart that has a"):
        extras.apply_extras(legend, extras.parse_extras([forecast]), model())
    text = load()
    _line_chart(text, {"table": "DimStore", "name": "StoreName"}, TOTAL)
    with pytest.raises(DocumentError, match="axis 'StoreName' is text$"):
        run(text, forecast)


def test_error_bars_switch_off_the_series_color_when_given_their_own() -> None:
    document = load()
    _line_chart(document, DATE, TOTAL)
    run(
        document,
        {
            "op": "set_analytics_line",
            "visual": "Trend",
            "kind": "error_bars",
            "upper": "PY Total Sales",
            "lower": "Profit",
            "color": "#333333",
            "shade_color": "#CCCCCC",
        },
    )
    _, line = resolve_visual(document, "Trend")
    (entry,) = line["visual"]["objects"]["error"]
    props = entry["properties"]
    assert props["barColor"] == hex_fill("#333333")
    assert props["barMatchSeriesColor"] == lit("false")
    assert props["shadeColor"] == hex_fill("#CCCCCC")
    assert props["shadeMatchSeriesColor"] == lit("false")
    with pytest.raises(DocumentError, match="^The error bars of this visual have no shading color"):
        run(
            load(),
            {
                "op": "set_analytics_line",
                "visual": CHART,
                "kind": "error_bars",
                "upper": "PY Total Sales",
                "lower": "Profit",
                "shade_color": "#CCCCCC",
            },
        )
    plain = load()
    run(
        plain,
        {
            "op": "set_analytics_line",
            "visual": CHART,
            "kind": "error_bars",
            "upper": "PY Total Sales",
            "lower": "Profit",
        },
    )
    (bare,) = objects(plain, CHART)["error"]
    assert "barMatchSeriesColor" not in bare["properties"]


def test_changing_an_x_axis_line_checks_the_axis_again() -> None:
    document = load()
    _line_chart(document, DATE, TOTAL)
    run(
        document,
        {
            "op": "set_analytics_line",
            "visual": "Trend",
            "kind": "x_constant",
            "value": "2019-06-30",
        },
    )
    with pytest.raises(DocumentError, match="^The axis 'Date' holds dates: give the line a date"):
        run(
            document,
            {"op": "set_analytics_line", "visual": "Trend", "kind": "x_constant", "value": 5},
        )
    moved = run(
        document,
        {
            "op": "set_analytics_line",
            "visual": "Trend",
            "kind": "x_constant",
            "value": "2019-07-31",
        },
    )
    assert moved.summaries == [
        "Changed the X-axis constant line 'X-Axis Constant Line 1' on the 'Trend' line chart: "
        "moved it to 2019-07-31."
    ]


def test_ratio_line_and_symmetry_shading_on_a_scatter_chart() -> None:
    document = load()
    run(
        document,
        {
            "op": "add_visual",
            "page": "1",
            "visual_type": "scatterChart",
            "fields": [
                {"table": "DimStore", "name": "StoreName"},
                {"table": "# Measures", "name": "Total Sales"},
                {"table": "# Measures", "name": "Profit"},
            ],
            "title": "Stores",
            "x": 20,
            "y": 1000,
            "width": 300,
            "height": 150,
        },
        {"op": "set_analytics_line", "visual": "Stores", "kind": "ratio", "style": "dashed"},
        {
            "op": "set_analytics_line",
            "visual": "Stores",
            "kind": "symmetry_shading",
            "upper_color": "#C6E0B4",
            "lower_color": "#F8CBAD",
            "transparency": 60,
        },
    )
    _, scatter = resolve_visual(document, "Stores")
    assert scatter["visual"]["objects"]["ratioLine"] == [
        {
            "properties": {
                "show": lit("true"),
                "displayName": lit("'Ratio line 1'"),
                "style": lit("'dashed'"),
            }
        }
    ]
    assert scatter["visual"]["objects"]["plotAreaShading"] == [
        {
            "properties": {
                "show": lit("true"),
                "displayName": lit("'Symmetry shading 1'"),
                "transparency": lit("60D"),
                "upperShadingColor": hex_fill("#C6E0B4"),
                "lowerShadingColor": hex_fill("#F8CBAD"),
            }
        }
    ]
    again = run(
        document, {"op": "set_analytics_line", "visual": "Stores", "kind": "ratio", "width": 2}
    )
    assert again.summaries == [
        "Changed the ratio line 'Ratio line 1' on the 'Stores' scatter chart: width."
    ]
    assert len(scatter["visual"]["objects"]["ratioLine"]) == 1


def test_removing_lines_by_name_kind_or_all() -> None:
    document = load()
    run(
        document,
        {"op": "set_analytics_line", "visual": CHART, "kind": "constant", "value": 1, "name": "A"},
        {
            "op": "set_analytics_line",
            "visual": CHART,
            "kind": "constant",
            "value": 2,
            "name": "B",
            "new": True,
        },
        {"op": "set_analytics_line", "visual": CHART, "kind": "trend"},
    )
    with pytest.raises(NeedsChoice) as raised:
        run(document, {"op": "remove_analytics_line", "visual": CHART, "kind": "constant"})
    assert [c.label for c in raised.value.options] == ["A", "B", "All of them"]
    removed = run(document, {"op": "remove_analytics_line", "visual": CHART, "line": "b"})
    assert removed.summaries == [
        "Removed the constant line 'B' from the 'Total MTD Sales' stacked column chart."
    ]
    assert [e["selector"] for e in objects(document, CHART)["y1AxisReferenceLine"]] == [{"id": "1"}]
    run(document, {"op": "remove_analytics_line", "visual": CHART, "all": True})
    assert "y1AxisReferenceLine" not in objects(document, CHART)
    assert "trend" not in objects(document, CHART)
    with pytest.raises(DocumentError, match="has no trend lines$"):
        run(document, {"op": "remove_analytics_line", "visual": CHART, "kind": "trend"})
    with pytest.raises(DocumentError, match="has no error bars$"):
        run(document, {"op": "remove_analytics_line", "visual": CHART, "kind": "error_bars"})


def test_removing_by_number_without_a_kind_asks_when_kinds_share_it() -> None:
    document = load()
    run(
        document,
        {"op": "set_analytics_line", "visual": CHART, "kind": "constant", "value": 1},
        {"op": "set_analytics_line", "visual": CHART, "kind": "trend"},
    )
    with pytest.raises(NeedsChoice) as raised:
        run(document, {"op": "remove_analytics_line", "visual": CHART, "line": 1})
    assert [c.fill for c in raised.value.options] == [
        {"line": "Y-Axis Constant Line 1", "kind": "constant"},
        {"line": "Trend line 1", "kind": "trend"},
    ]
    run(document, {"op": "remove_analytics_line", "visual": CHART, "line": 1, "kind": "trend"})
    assert "trend" not in objects(document, CHART)
    alone = run(document, {"op": "remove_analytics_line", "visual": CHART, "line": 1})
    assert alone.summaries == [
        "Removed the constant line 'Y-Axis Constant Line 1' from the 'Total MTD Sales' stacked "
        "column chart."
    ]


def test_a_line_that_is_not_there_lists_the_ones_that_are() -> None:
    document = load()
    run(
        document,
        {
            "op": "set_analytics_line",
            "visual": CHART,
            "kind": "constant",
            "value": 1,
            "name": "Goal",
        },
    )
    with pytest.raises(DocumentError) as raised:
        run(document, {"op": "remove_analytics_line", "visual": CHART, "line": "Target"})
    assert str(raised.value) == (
        "The 'Total MTD Sales' stacked column chart has no analytics line 'Target'. It has 'Goal'."
    )


# --- the phone layout --------------------------------------------------------------------------


def mobile_json(document: PbirDocument, name: str, page: str = PAGE1) -> Any:
    part = f"Report/definition/pages/{page}/visuals/{name}/mobile.json"
    return document.package.read_json(part) if document.package.has(part) else None


def test_placing_a_visual_writes_its_mobile_part(tmp_path: Path) -> None:
    document = load()
    outcome = run(
        document,
        {"op": "place_on_mobile", "visual": SLICERS[2], "x": 0, "y": 0, "width": 320, "height": 60},
        {"op": "place_on_mobile", "visual": CHART},
    )
    assert outcome.summaries == [
        "Added the slicer of Store to the phone layout of page '1', 320 x 60 at (0, 0).",
        "Added the 'Total MTD Sales' stacked column chart to the phone layout of page '1', "
        "320 x 199 at (0, 60).",
    ]
    assert mobile_json(document, SLICERS[2]) == {
        "$schema": SCHEMA + "visualContainerMobileState/2.7.0/schema.json",
        "position": {"x": 0, "y": 0, "z": 0, "height": 60, "width": 320, "tabOrder": 0},
    }
    assert mobile_json(document, CHART)["position"] == {
        "x": 0,
        "y": 60,
        "z": 1000,
        "height": 199,
        "width": 320,
        "tabOrder": 1000,
    }
    moved = run(document, {"op": "place_on_mobile", "visual": CHART, "y": 100, "height": 150})
    assert moved.summaries == [
        "Moved and resized the 'Total MTD Sales' stacked column chart on the phone layout to "
        "320 x 150 at (0, 100)."
    ]
    assert mobile_json(document, CHART)["position"]["z"] == 1000
    parts = saved_parts(document, tmp_path / "mobile.pbix")
    key = f"Report/definition/pages/{PAGE1}/visuals/{CHART}/mobile.json"
    assert parts[key]["position"] == {
        "x": 0,
        "y": 100,
        "z": 1000,
        "height": 150,
        "width": 320,
        "tabOrder": 1000,
    }
    assert extras.describe_mobile_layout(load(), "1") == "Page '1' has no phone layout yet."


def test_removing_from_the_phone_layout_deletes_the_part(tmp_path: Path) -> None:
    document = load("multipage")
    assert extras.mobile_layout(document, PAGE1)[0].visual == BUTTON
    outcome = run(document, {"op": "remove_from_mobile", "visuals": [BUTTON, TABLE]})
    assert outcome.summaries == ["Took the action button at 1668, 110 off the phone layout."]
    assert outcome.warnings == ["The table of Agent ID was not on the phone layout."]
    parts = saved_parts(document, tmp_path / "gone.pbix")
    assert not any(part.endswith("/mobile.json") for part in parts)
    with pytest.raises(DocumentError, match="is not on the phone layout$"):
        run(load(), {"op": "remove_from_mobile", "visuals": [TABLE]})


def test_a_new_part_takes_the_mobile_schema_desktop_ships_and_an_old_part_keeps_its_own() -> None:
    document = load("multipage")
    assert mobile_json(document, BUTTON)["$schema"] == (
        SCHEMA + "visualContainerMobileState/2.6.0/schema.json"
    )
    run(
        document,
        {"op": "place_on_mobile", "visual": TABLE},
        {"op": "place_on_mobile", "visual": BUTTON, "y": 400},
    )
    assert mobile_json(document, TABLE)["$schema"] == extras.MOBILE_SCHEMA
    assert extras.MOBILE_SCHEMA == SCHEMA + "visualContainerMobileState/2.7.0/schema.json"
    assert mobile_json(document, BUTTON)["$schema"] == (
        SCHEMA + "visualContainerMobileState/2.6.0/schema.json"
    )


def test_arrange_lays_the_page_out_in_two_columns() -> None:
    document = load()
    outcome = run(
        document,
        {"op": "place_on_mobile", "visual": CHART},
        {"op": "arrange_mobile_layout", "page": "1", "visuals": [*CARDS, TABLE]},
    )
    assert outcome.summaries[-1] == (
        "Laid out the phone layout of page '1': 5 visuals in 2 columns, in the order given."
    )
    positions = {
        p.visual: (p.x, p.y, p.width, p.height) for p in extras.mobile_layout(document, "1")
    }
    assert positions == {
        CARDS[0]: (0.0, 0.0, 155.0, 137.0),
        CARDS[1]: (165.0, 0.0, 155.0, 137.0),
        CARDS[2]: (0.0, 147.0, 155.0, 137.0),
        CARDS[3]: (165.0, 147.0, 155.0, 137.0),
        TABLE: (0.0, 294.0, 320.0, 223.0),
    }
    assert mobile_json(document, CHART) is None  # not in the list: taken off
    assert [mobile_json(document, n)["position"]["tabOrder"] for n in (*CARDS, TABLE)] == [
        0,
        1000,
        2000,
        3000,
        4000,
    ]
    assert extras.describe_mobile_layout(document, "1").splitlines()[:2] == [
        "The phone layout of page '1' has 5 visuals:",
        "1. The 'Agents with Positive Change %' card: 155 x 137 at (0, 0)",
    ]


def test_arrange_by_reading_order_in_one_column_skips_group_members() -> None:
    document = load("multipage")
    outcome = run(document, {"op": "arrange_mobile_layout", "page": "Details", "columns": 1})
    assert outcome.summaries == [
        "Laid out the phone layout of page 'Details': 2 visuals in one column, in the page's "
        "reading order."
    ]
    assert outcome.warnings == [
        "Left out the group 'Header block': groups, hidden visuals and background panels are "
        "placed in Power BI Desktop's Mobile layout view."
    ]
    placed = [(p.visual, p.y, p.width) for p in extras.mobile_layout(document, DETAILS)]
    assert placed == [
        ("81ab76274a5b4e9e62da", 0.0, 320.0),
        ("d6c05c8319906941fe02", 330.0, 320.0),
    ]
    order = extras._reading_order(document.pages[PAGE1], list(SLICERS) + [CARDS[1], CARDS[0]])
    assert order == [SLICERS[0], SLICERS[1], SLICERS[2], CARDS[0], CARDS[1]]


def test_arrange_keeps_the_phone_placements_it_does_not_make() -> None:
    document = load("multipage")
    part = f"Report/definition/pages/{DETAILS}/visuals/{SLICER_A}/mobile.json"
    placement = {
        "$schema": SCHEMA + "visualContainerMobileState/2.6.0/schema.json",
        "position": {"x": 0, "y": 900, "z": 0, "height": 60, "width": 320, "tabOrder": 0},
    }
    document.package.write_json(part, placement)
    outcome = run(document, {"op": "arrange_mobile_layout", "page": "Details"})
    assert document.package.read_json(part) == placement
    assert len(outcome.warnings) == 2
    assert outcome.warnings[1].startswith("Kept the phone placement of ")
    assert outcome.warnings[1].endswith(
        ": Studio does not arrange groups, grouped, hidden or background visuals."
    )
    # a visual the layout could place is taken off when it is not in the new layout
    document = load()
    run(document, {"op": "place_on_mobile", "visual": CHART})
    run(document, {"op": "arrange_mobile_layout", "page": "1", "visuals": [TABLE]})
    assert mobile_json(document, CHART) is None


def test_groups_and_their_members_are_placed_in_desktop() -> None:
    document = load("multipage")
    with pytest.raises(DocumentError, match="is inside a group. Studio places single visuals"):
        run(document, {"op": "place_on_mobile", "visual": SLICER_A})
    with pytest.raises(DocumentError, match="is a group. Studio places single visuals"):
        run(document, {"op": "arrange_mobile_layout", "page": "Details", "visuals": [HEADER_GROUP]})


def test_clearing_a_pages_phone_layout() -> None:
    document = load()
    run(document, {"op": "arrange_mobile_layout", "page": "1"})
    assert len(extras.mobile_layout(document, "1")) == 15
    with pytest.raises(NeedsChoice) as raised:
        run(document, {"op": "remove_from_mobile", "page": "1"})
    assert [(c.label, c.fill) for c in raised.value.options] == [
        ("Clear the phone layout of page '1'", {"all": True})
    ]
    outcome = run(document, {"op": "remove_from_mobile", "page": "1", "all": True})
    assert outcome.summaries == ["Cleared the phone layout of page '1' (15 visuals)."]
    assert extras.mobile_layout(document, "1") == []


# --- groups ---------------------------------------------------------------------------------------


def test_grouping_keeps_every_visual_where_it_was() -> None:
    document = load()
    before = {name: box(document, name) for name in CARDS}
    outcome = run(document, {"op": "group_visuals", "visuals": list(CARDS), "name": "KPI cards"})
    assert outcome.summaries == [
        "Grouped the 'Agents with Positive Change %' card, the 'Agents Avg MTD Sales' card, the "
        "'Agents Avg MTD % Change' card and the 'Number of Agents Sold above 100K' card into the "
        "group 'KPI cards'; they keep their places on the page. The group resizes its visuals "
        "with it."
    ]
    # the four charts' layers (6000-9000) sit between the cards' (3000-5000 and 10000)
    assert outcome.warnings == [
        "Their layers were interleaved with the 'Total MTD Sales' stacked column chart, the 'Avg "
        "MTD Sales by Agents' stacked column chart, the 'Avg MTD % change by Agents' stacked "
        "column chart and 1 more: in Power BI a group stacks as one block, so those visuals may "
        "now draw above or below all of them. Bring them forward or back if they do."
    ]
    page = document.pages[PAGE1]
    (group,) = [v for v in page.visuals.values() if "visualGroup" in v]
    # its own tab stop, just before its first member's (3000), which no visual has
    assert group == {
        "$schema": SCHEMA + "visualContainer/2.12.0/schema.json",
        "name": group["name"],
        "position": {"x": 173, "y": 154, "z": 3000, "height": 313, "width": 1672, "tabOrder": 2999},
        "visualGroup": {"displayName": "KPI cards", "groupMode": "ScaleMode"},
    }
    orders = [v["position"]["tabOrder"] for v in page.visuals.values()]
    assert len(orders) == len(set(orders))
    slicers = run(document, {"op": "group_visuals", "visuals": list(SLICERS[:2])})
    assert slicers.warnings == []
    assert [visual(document, n)["parentGroupName"] for n in CARDS] == [group["name"]] * 4
    assert [
        (visual(document, n)["position"]["x"], visual(document, n)["position"]["y"]) for n in CARDS
    ] == [
        (0, 0),
        (439, 0),
        (878, 0),
        (1317, 0),
    ]
    assert {name: box(document, name) for name in CARDS} == before


def test_group_then_ungroup_restores_the_stored_positions_exactly() -> None:
    document = load()
    names = [*SLICERS, CHART, TABLE]
    before = {n: copy.deepcopy(visual(document, n)["position"]) for n in names}
    run(document, {"op": "group_visuals", "visuals": names})
    (group,) = [n for n, v in document.pages[PAGE1].visuals.items() if "visualGroup" in v]
    assert document.pages[PAGE1].visuals[group]["visualGroup"]["displayName"] == "Group 1"
    outcome = run(document, {"op": "ungroup_visuals", "group": "Group 1"})
    assert outcome.summaries == [
        "Ungrouped the group 'Group 1': its 5 visuals stay where they are."
    ]
    assert {n: visual(document, n)["position"] for n in names} == before
    assert all("parentGroupName" not in visual(document, n) for n in names)
    assert group not in document.pages[PAGE1].visuals


def test_nested_groups_add_up_and_ungrouping_the_outer_one_keeps_the_inner_ones() -> None:
    document = load()
    charts = (CHART, CHART_AVG, CHART_CHANGE, CHART_DISCOUNT)
    before = {n: box(document, n) for n in charts}
    run(
        document,
        {"op": "group_visuals", "visuals": [CHART, CHART_AVG], "name": "Top"},
        {
            "op": "group_visuals",
            "visuals": [CHART_CHANGE, CHART_DISCOUNT],
            "name": "Bottom",
            "mode": "scroll",
        },
        {"op": "group_visuals", "visuals": ["Top", "Bottom"], "name": "Charts"},
    )
    page = document.pages[PAGE1]
    groups = {
        v["visualGroup"]["displayName"]: n for n, v in page.visuals.items() if "visualGroup" in v
    }
    assert page.visuals[groups["Top"]]["parentGroupName"] == groups["Charts"]
    assert page.visuals[groups["Bottom"]]["visualGroup"]["groupMode"] == "ScrollMode"
    assert page.visuals[groups["Charts"]]["position"]["x"] == 172  # floor of 172.97
    assert page.visuals[groups["Top"]]["position"]["x"] == 2  # 174 on the page
    assert {n: box(document, n) for n in charts} == pytest.approx(before)
    run(document, {"op": "ungroup_visuals", "group": "Charts"})
    assert "parentGroupName" not in page.visuals[groups["Top"]]
    assert page.visuals[CHART]["parentGroupName"] == groups["Top"]
    assert {n: box(document, n) for n in charts} == pytest.approx(before)


def test_grouping_refuses_what_desktop_cannot_group() -> None:
    document = load("multipage")
    with pytest.raises(DocumentError, match="A group holds visuals of one page"):
        run(document, {"op": "group_visuals", "visuals": [CHART, SLICER_A]})
    with pytest.raises(DocumentError, match="sit in different groups"):
        run(document, {"op": "group_visuals", "visuals": [SLICER_A, GROUP_CARD]})
    with pytest.raises(DocumentError, match="is already inside the group 'Filters'"):
        run(document, {"op": "group_visuals", "visuals": [SLICER_A, FILTERS_GROUP]})
    with pytest.raises(DocumentError, match="at least two different visuals"):
        run(document, {"op": "group_visuals", "visuals": [CHART, CHART]})
    with pytest.raises(DocumentError, match="is not a group$"):
        run(document, {"op": "ungroup_visuals", "group": CHART})


def test_a_group_inside_a_group_is_made_under_the_shared_parent() -> None:
    document = load("multipage")
    before = {n: box(document, n) for n in (FILTERS_GROUP, GROUP_CARD)}
    run(document, {"op": "group_visuals", "visuals": [FILTERS_GROUP, GROUP_CARD], "name": "Inner"})
    page = document.pages[DETAILS]
    inner = next(
        n for n, v in page.visuals.items() if v.get("visualGroup", {}).get("displayName") == "Inner"
    )
    assert page.visuals[inner]["parentGroupName"] == HEADER_GROUP
    assert {n: box(document, n) for n in (FILTERS_GROUP, GROUP_CARD)} == before
    assert box(document, SLICER_A) == (120.0, 180.0, 200.0, 60.0)


def test_ungrouping_clears_every_reference_to_the_group() -> None:
    document = load("multipage")
    page = document.pages[DETAILS]
    before = {n: box(document, n) for n in (FILTERS_GROUP, GROUP_CARD, SLICER_A)}
    bookmark = next(iter(document.bookmarks().values()))
    section = (
        bookmark.setdefault("explorationState", {})
        .setdefault("sections", {})
        .setdefault(DETAILS, {})
    )
    section.setdefault("visualContainerGroups", {})[HEADER_GROUP] = {"isHidden": False}
    with pytest.raises(NeedsConfirmation) as raised:
        run(document, {"op": "ungroup_visuals", "group": "Header block"})
    assert raised.value.kind == "cleanup_references"
    assert str(raised.value) == (
        f"Ungrouping the group 'Header block': '{bookmark.get('displayName')}' remembers the "
        "group's state, which would be dropped."
    )
    assert HEADER_GROUP in page.visuals
    outcome = run(
        document, {"op": "ungroup_visuals", "group": "Header block", "cleanup_references": True}
    )
    assert outcome.summaries[0].endswith("The group was also removed from 1 bookmark.")
    assert HEADER_GROUP not in page.visuals
    assert "parentGroupName" not in page.visuals[FILTERS_GROUP]
    assert {n: box(document, n) for n in before} == before
    assert HEADER_GROUP not in json.dumps(document.bookmarks())
    index = ReferenceIndex.build(document)
    assert index.visual_uses(HEADER_GROUP) == []


def test_ungrouping_a_hidden_group_asks_first() -> None:
    document = load("multipage")
    page = document.pages[DETAILS]
    page.visuals[FILTERS_GROUP]["isHidden"] = True
    with pytest.raises(NeedsConfirmation) as raised:
        run(document, {"op": "ungroup_visuals", "group": "Filters"})
    assert raised.value.kind == "cleanup_references"
    assert "the group is hidden, so its" in str(raised.value)
    assert str(raised.value).endswith("would show on the page.")
    run(document, {"op": "ungroup_visuals", "group": "Filters", "cleanup_references": True})
    assert FILTERS_GROUP not in page.visuals


def test_renaming_a_group_and_changing_its_mode() -> None:
    document = load("multipage")
    outcome = run(
        document, {"op": "set_group", "group": "Filters", "name": "  Slicers ", "mode": "scroll"}
    )
    assert outcome.summaries == [
        "Changed the group 'Filters': renamed it 'Slicers' and it now keeps its visuals' size and "
        "scrolls."
    ]
    assert document.pages[DETAILS].visuals[FILTERS_GROUP]["visualGroup"] == {
        "displayName": "Slicers",
        "groupMode": "ScrollMode",
    }
    again = run(document, {"op": "set_group", "group": "Slicers", "mode": "scroll"})
    assert again.summaries == ["The group 'Slicers' was already set that way."]
    with pytest.raises(DocumentError, match="Say the group's new name"):
        run(document, {"op": "set_group", "group": "Slicers"})


def test_grouping_visuals_with_interactions_keeps_them_valid() -> None:
    document = load("multipage")
    interactions = copy.deepcopy(document.pages[PAGE1].data.get("visualInteractions"))
    assert interactions
    sources = {i["source"] for i in interactions} | {i["target"] for i in interactions}
    run(document, {"op": "group_visuals", "visuals": sorted(sources), "name": "Linked"})
    assert document.pages[PAGE1].data["visualInteractions"] == interactions


# --- visual calculations -------------------------------------------------------------------------


def calcs(document: PbirDocument, name: str = TABLE) -> list[tuple[str, str]]:
    return [(c.name, c.expression) for c in extras.visual_calculations(visual(document, name))]


def test_a_template_calculation_is_a_native_visual_calculation_projection() -> None:
    document = load()
    outcome = run(
        document,
        {
            "op": "set_visual_calculation",
            "visual": TABLE,
            "name": "Running MTD Sales",
            "template": "running_sum",
            "field": "MTD Sales",
        },
    )
    assert outcome.summaries == [
        "Added the visual calculation 'Running MTD Sales' = RUNNINGSUM([MTD Sales]) to the table "
        "of Agent ID."
    ]
    projection = visual(document, TABLE)["visual"]["query"]["queryState"]["Values"]["projections"][
        -1
    ]
    assert projection == {
        "field": {
            "NativeVisualCalculation": {
                "Language": "dax",
                "Expression": "RUNNINGSUM([MTD Sales])",
                "Name": "Running MTD Sales",
            }
        },
        "queryRef": "Running MTD Sales",
        "nativeQueryRef": "Running MTD Sales",
    }


def test_where_a_calculation_shows_follows_what_the_canvas_computes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_powerbi_analyst.studio import render_analytics

    seen: list[tuple[str, list[dict[str, Any]], int]] = []
    computed = {"yes": True}

    def parse(expression: str, fields: list[dict[str, Any]], own: int) -> object | None:
        seen.append((expression, fields, own))
        return object() if computed["yes"] else None

    monkeypatch.setattr(render_analytics, "parse_calculation", parse, raising=False)
    operation = {
        "op": "set_visual_calculation",
        "visual": CHART,
        "name": "Running",
        "template": "running_sum",
        "field": "TopN/BottomN/ MTD Sales",
    }
    assert run(load(), operation).visible_in == []
    expression, fields, own = seen[-1]
    assert expression == "RUNNINGSUM([TopN/BottomN/ MTD Sales])"
    assert fields == [
        {
            "label": "Clustered Employees",
            "name": "Clustered Employees",
            "query_ref": "Clustered Employees.Clustered Employees",
        },
        {
            "label": "TopN/BottomN/ MTD Sales",
            "name": "TopN/BottomN/ MTD Sales",
            "query_ref": MEASURE,
        },
        {"label": "Running", "name": "Running", "query_ref": "Running"},
    ]
    assert own == 2
    computed["yes"] = False
    outcome = run(load(), operation)
    assert [(v.op, v.where) for v in outcome.visible_in] == [
        ("set_visual_calculation", "powerbi_only")
    ]


def test_where_small_multiples_show_follows_what_the_canvas_draws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drawn = {"rowCount", "columnCount"}
    monkeypatch.setattr(
        extras, "property_is_rendered", lambda _t, _o, prop, container=None: prop in drawn
    )
    operation = {
        "op": "set_small_multiples",
        "visual": CHART,
        "field": {"table": "DimChannel", "name": "ChannelName"},
        "rows": 2,
        "shared_y_axis": True,
        "titles": True,
    }
    assert [v.detail for v in run(load(), operation).visible_in] == [
        "Power BI shows the shared y-axis and the titles of the charts; Studio does not yet."
    ]
    drawn.clear()
    assert [v.detail for v in run(load(), operation).visible_in] == [
        "Studio draws the visual as one chart; Power BI shows the grid of small multiples."
    ]


@pytest.mark.parametrize(
    ("template", "extra", "expression"),
    [
        ("moving_average", {"window": 5}, "MOVINGAVERAGE([Rank], 5)"),
        ("percent_of_parent", {}, "DIVIDE([Rank], COLLAPSE([Rank], ROWS))"),
        ("percent_of_grand_total", {}, "DIVIDE([Rank], COLLAPSEALL([Rank], ROWS))"),
        ("average_of_children", {}, "EXPAND(AVERAGE([Rank]), ROWS)"),
        ("versus_previous", {}, "[Rank] - PREVIOUS([Rank])"),
        ("versus_next", {}, "[Rank] - NEXT([Rank])"),
        ("versus_parent", {}, "[Rank] - COLLAPSE([Rank], ROWS)"),
        ("versus_grand_total", {}, "[Rank] - COLLAPSEALL([Rank], ROWS)"),
    ],
)
def test_every_template(template: str, extra: dict[str, Any], expression: str) -> None:
    document = load()
    run(
        document,
        {
            "op": "set_visual_calculation",
            "visual": TABLE,
            "name": "Calc",
            "template": template,
            "field": "rank",
            **extra,
        },
    )
    assert calcs(document) == [("Calc", expression)]


def test_a_template_without_a_name_is_named_as_power_bi_names_it() -> None:
    document = load()
    operation = {
        "op": "set_visual_calculation",
        "visual": TABLE,
        "template": "running_sum",
        "field": "Rank",
    }
    outcome = run(document, operation, operation)
    assert outcome.summaries == [
        "Added the visual calculation 'Running sum' = RUNNINGSUM([Rank]) to the table of Agent ID.",
        "Added the visual calculation 'Running sum 2' = RUNNINGSUM([Rank]) to the table of Agent "
        "ID.",
    ]
    with pytest.raises(DocumentError, match="^Say the calculation's name$"):
        run(load(), {"op": "set_visual_calculation", "visual": TABLE, "expression": "[Rank]"})


def test_a_calculation_may_only_use_the_visuals_fields() -> None:
    def add(expression: str, name: str = "Calc") -> None:
        run(
            load(),
            {
                "op": "set_visual_calculation",
                "visual": TABLE,
                "name": name,
                "expression": expression,
            },
        )

    with pytest.raises(DocumentError) as raised:
        add("RUNNINGSUM([Sales])")
    assert str(raised.value) == (
        "[Sales] is not a field of this visual. Its fields: 'Agent ID', 'Agent Name', 'Rank', "
        "'MTD Sales', 'vs PM %' and 'vs Previous Month'."
    )
    with pytest.raises(DocumentError, match=r"write \[Rank\], not '# Measures'\[Rank\]"):
        add("'# Measures'[Rank] * 2")
    with pytest.raises(DocumentError, match="^The calculation is not valid DAX: "):
        add("RUNNINGSUM([Rank]")
    with pytest.raises(DocumentError, match="cannot use itself"):
        add("[Calc] + 1")
    with pytest.raises(DocumentError, match="already has a field called 'Rank'"):
        add("[Agent ID]", name="rank")
    with pytest.raises(DocumentError, match="either an expression or a template"):
        run(
            load(),
            {
                "op": "set_visual_calculation",
                "visual": TABLE,
                "name": "X",
                "expression": "[Rank]",
                "template": "running_sum",
                "field": "Rank",
            },
        )


def test_renaming_a_calculation_updates_the_ones_that_use_it() -> None:
    document = load()
    run(
        document,
        {
            "op": "set_visual_calculation",
            "visual": TABLE,
            "name": "Base",
            "expression": "[Rank] * 2",
        },
        {
            "op": "set_visual_calculation",
            "visual": TABLE,
            "name": "Twice",
            "expression": '[base] + "[Base]"',
        },
    )
    outcome = run(
        document,
        {"op": "set_visual_calculation", "visual": TABLE, "name": "Base", "new_name": "Doubled"},
    )
    assert outcome.summaries == [
        "Changed the visual calculation 'Base' on the table of Agent ID: renamed it 'Doubled' and "
        "updated the calculation that uses it."
    ]
    assert calcs(document) == [("Doubled", "[Rank] * 2"), ("Twice", '[Doubled] + "[Base]"')]
    projection = visual(document, TABLE)["visual"]["query"]["queryState"]["Values"]["projections"][
        -2
    ]
    assert (projection["queryRef"], projection["nativeQueryRef"]) == ("Base", "Doubled")
    with pytest.raises(DocumentError) as raised:
        run(document, {"op": "remove_visual_calculation", "visual": TABLE, "name": "doubled"})
    assert str(raised.value) == (
        "The calculation 'Doubled' is used by 'Twice'; change or remove those first"
    )
    run(
        document,
        {"op": "remove_visual_calculation", "visual": TABLE, "name": "Twice"},
        {"op": "remove_visual_calculation", "visual": TABLE, "name": "Doubled"},
    )
    assert calcs(document) == []
    assert (
        len(visual(document, TABLE)["visual"]["query"]["queryState"]["Values"]["projections"]) == 6
    )


def test_removing_a_calculation_drops_its_formatting_and_sort() -> None:
    document = load()
    run(
        document,
        {
            "op": "set_visual_calculation",
            "visual": CHART,
            "name": "Moving average",
            "template": "moving_average",
            "field": "TopN/BottomN/ MTD Sales",
            "role": "Tooltips",
        },
    )
    state = visual(document, CHART)["visual"]["query"]["queryState"]
    assert [p["queryRef"] for p in state["Tooltips"]["projections"]] == ["Moving average"]
    objects(document, CHART)["labels"].append(
        {"properties": {"show": lit("true")}, "selector": {"metadata": "Moving average"}}
    )
    outcome = run(
        document, {"op": "remove_visual_calculation", "visual": CHART, "name": "Moving average"}
    )
    assert "Tooltips" not in state
    assert outcome.warnings == ["Removed 1 formatting setting for Moving average."]
    assert objects(document, CHART)["labels"] == [{"properties": {"fontSize": lit("16D")}}]
    with pytest.raises(
        DocumentError, match="has no visual calculation 'Moving average'. It has none."
    ):
        run(
            document, {"op": "remove_visual_calculation", "visual": CHART, "name": "Moving average"}
        )


def test_visual_calculations_and_small_multiples_exclude_each_other() -> None:
    document = load()
    run(
        document,
        {
            "op": "set_visual_calculation",
            "visual": CHART,
            "name": "C",
            "expression": "[TopN/BottomN/ MTD Sales]",
        },
    )
    with pytest.raises(DocumentError, match="with visual calculations; remove the calculations"):
        run(
            document,
            {
                "op": "set_small_multiples",
                "visual": CHART,
                "field": {"table": "DimChannel", "name": "ChannelName"},
            },
        )
    other = load()
    run(
        other,
        {
            "op": "set_small_multiples",
            "visual": CHART,
            "field": {"table": "DimChannel", "name": "ChannelName"},
        },
    )
    with pytest.raises(DocumentError, match="split into small multiples; remove the small"):
        run(
            other, {"op": "set_visual_calculation", "visual": CHART, "name": "C", "expression": "1"}
        )


# --- small multiples -------------------------------------------------------------------------


def test_small_multiples_put_the_column_in_its_well_and_set_the_grid() -> None:
    document = load()
    outcome = run(
        document,
        {
            "op": "set_small_multiples",
            "visual": CHART,
            "field": {"table": "DimChannel", "name": "ChannelName"},
            "rows": 1,
            "columns": 2,
            "shared_y_axis": True,
            "titles": False,
        },
    )
    assert outcome.summaries == [
        "Set the small multiples of the 'Total MTD Sales' stacked column chart: split by "
        "ChannelName, a grid of 1 x 2, a shared y-axis and no titles on the charts."
    ]
    assert visual(document, CHART)["visual"]["query"]["queryState"]["Rows"] == {
        "projections": [
            {
                "field": {
                    "Column": {
                        "Expression": {"SourceRef": {"Entity": "DimChannel"}},
                        "Property": "ChannelName",
                    }
                },
                "queryRef": "DimChannel.ChannelName",
                "nativeQueryRef": "ChannelName",
            }
        ]
    }
    found = objects(document, CHART)
    assert found["smallMultiplesLayout"] == [
        {
            "properties": {
                "layoutType": lit("'custom'"),
                "rowCount": lit("1L"),
                "columnCount": lit("2L"),
            }
        }
    ]
    assert found["valueAxis"][0]["properties"]["sharedAxis"] == lit("true")
    assert found["subheader"] == [{"properties": {"show": lit("false")}}]
    off = run(document, {"op": "set_small_multiples", "visual": CHART, "remove": True})
    assert off.summaries == [
        "Turned off the small multiples of the 'Total MTD Sales' stacked column chart."
    ]
    assert "Rows" not in visual(document, CHART)["visual"]["query"]["queryState"]
    assert "smallMultiplesLayout" not in objects(document, CHART)
    assert "subheader" not in objects(document, CHART)


def test_small_multiples_need_a_column_on_a_visual_that_has_them() -> None:
    with pytest.raises(DocumentError, match="and \\[Profit\\] is a measure"):
        run(
            load(),
            {
                "op": "set_small_multiples",
                "visual": CHART,
                "field": {"table": "# Measures", "name": "Profit"},
            },
        )
    with pytest.raises(DocumentError, match="cannot be split into small multiples in Power BI"):
        run(load(), {"op": "set_small_multiples", "visual": TABLE, "rows": 2})
    with pytest.raises(DocumentError, match="Say which field to split"):
        run(load(), {"op": "set_small_multiples", "visual": CHART, "rows": 2})


# --- accessibility and other settings --------------------------------------------------------


def test_alt_text_is_written_and_removed() -> None:
    document = load()
    outcome = run(document, {"op": "set_alt_text", "visual": TABLE, "text": " Agents  by sales "})
    assert outcome.summaries == ["Set the alt text of the table of Agent ID to 'Agents by sales'."]
    container = visual(document, TABLE)["visual"]["visualContainerObjects"]
    assert container["general"] == [{"properties": {"altText": lit("'Agents by sales'")}}]
    same = run(document, {"op": "set_alt_text", "visual": TABLE, "text": "Agents by sales"})
    assert same.summaries == ["The table of Agent ID already has that alt text."]
    removed = run(document, {"op": "set_alt_text", "visual": TABLE, "text": ""})
    assert removed.summaries == ["Removed the alt text of the table of Agent ID."]
    assert "general" not in container


def test_tab_order_puts_the_named_visuals_first_and_stays_unique() -> None:
    document = load()
    outcome = run(document, {"op": "set_tab_order", "visuals": [TABLE, SLICERS[1]]})
    assert outcome.summaries == [
        "Set the keyboard tab order of page '1': 1. the table of Agent ID, 2. the 'Country' "
        "slicer, 3. the slicer of Top-Bottom-N, 4. the 'Top/Bottom Field' button slicer and 11 "
        "more."
    ]
    orders = {n: v["position"]["tabOrder"] for n, v in document.pages[PAGE1].visuals.items()}
    assert orders[TABLE] == 0 and orders[SLICERS[1]] == 1000
    assert sorted(orders.values()) == list(range(0, 15000, 1000))


def test_tab_order_by_position_and_groups_follow_their_first_member() -> None:
    document = load()
    run(
        document,
        {"op": "group_visuals", "visuals": list(CARDS), "name": "KPI cards"},
        {"op": "set_tab_order", "page": "1", "by_position": True},
    )
    page = document.pages[PAGE1]
    orders = {n: v["position"]["tabOrder"] for n, v in page.visuals.items()}
    assert [orders[n] for n in (*SLICERS, *CARDS)] == [0, 1000, 2000, 3000, 4000, 5000, 6000]
    group = next(n for n, v in page.visuals.items() if "visualGroup" in v)
    assert orders[group] == 2999  # just before its first member, and unique
    assert len(set(orders.values())) == len(orders)
    again = run(document, {"op": "set_tab_order", "page": "1", "by_position": True})
    assert again.summaries[0].startswith("The tab order of page '1' was already 1. the slicer")


def test_keep_layer_order() -> None:
    document = load()
    outcome = run(
        document,
        {"op": "set_keep_layer_order", "visuals": [CARD_TOPN, *SLICERS[:1]]},
    )
    assert outcome.summaries == [
        "The 'TopN/BottomN' card and the slicer of Channel now keep their place in the stack when "
        "clicked.",
    ]
    general = visual(document, CARD_TOPN)["visual"]["visualContainerObjects"]["general"]
    assert general == [{"properties": {"keepLayerOrder": lit("true")}}]
    assert {v.where for v in outcome.visible_in} == {"powerbi_only"}


def test_page_view() -> None:
    document = load()
    outcome = run(document, {"op": "set_page_view", "page": "1", "view": "actual_size"})
    assert outcome.summaries == ["Set the page view of page '1' to actual size."]
    assert document.pages[PAGE1].data["displayOption"] == "ActualSize"
    same = run(document, {"op": "set_page_view", "page": "1", "view": "actual_size"})
    assert same.summaries == ["Page '1' was already set to actual size."]


def test_field_display_name_is_written_as_desktop_writes_a_rename() -> None:
    document = load()
    run(
        document,
        {
            "op": "set_visual_calculation",
            "visual": TABLE,
            "name": "Running",
            "expression": "RUNNINGSUM([MTD Sales])",
        },
    )
    outcome = run(
        document,
        {
            "op": "set_field_display_name",
            "visual": TABLE,
            "field": "mtd sales",
            "name": "Sales this month",
        },
    )
    assert outcome.summaries == [
        "The field 'MTD Sales' in the table of Agent ID is now called 'Sales this month'. The "
        "visual calculation that uses it was updated."
    ]
    projections = visual(document, TABLE)["visual"]["query"]["queryState"]["Values"]["projections"]
    renamed = next(p for p in projections if p["queryRef"] == "# Measures.Table MTD Sales")
    assert {k: v for k, v in renamed.items() if k != "field"} == {
        "queryRef": "# Measures.Table MTD Sales",
        "nativeQueryRef": "Sales this month",
        "displayName": "Sales this month",
    }
    assert calcs(document) == [("Running", "RUNNINGSUM([Sales this month])")]
    reset = run(
        document,
        {"op": "set_field_display_name", "visual": TABLE, "field": "Sales this month", "name": ""},
    )
    assert reset.summaries[0].startswith(
        "The field 'Sales this month' in the table of Agent ID goes by its own name, 'Table MTD "
        "Sales', again."
    )
    assert "displayName" not in renamed and renamed["nativeQueryRef"] == "Table MTD Sales"
    with pytest.raises(DocumentError, match="already has a field called 'Rank'"):
        run(
            document,
            {"op": "set_field_display_name", "visual": TABLE, "field": "Agent ID", "name": "rank"},
        )
    with pytest.raises(DocumentError, match="'Nope' is not a field of the table of Agent ID"):
        run(
            document,
            {"op": "set_field_display_name", "visual": TABLE, "field": "Nope", "name": "X"},
        )


def test_a_blank_header_is_one_space_and_a_second_one_stays_unique() -> None:
    document = load()
    projections = visual(document, TABLE)["visual"]["query"]["queryState"]["Values"]["projections"]
    rank = next(p for p in projections if p["queryRef"] == "# Measures.Rank")
    outcome = run(
        document, {"op": "set_field_display_name", "visual": TABLE, "field": "Rank", "name": " "}
    )
    assert outcome.summaries == [
        "The column 'Rank' in the table of Agent ID now has a blank header."
    ]
    # the Agents table's 'vs PM' already has the one-space header Desktop wrote
    assert (rank["displayName"], rank["nativeQueryRef"]) == (" ", "  ")
    run(
        document,
        {"op": "set_field_display_name", "visual": TABLE, "field": "Agent Name", "name": "   "},
    )
    name = next(p for p in projections if p["queryRef"] == "# Measures.Employee Name")
    assert (name["displayName"], name["nativeQueryRef"]) == (" ", "   ")
    reset = run(
        document,
        {"op": "set_field_display_name", "visual": TABLE, "field": "# Measures.Rank", "name": None},
    )
    assert reset.summaries == [
        "The field 'Rank' in the table of Agent ID goes by its own name, 'Rank', again."
    ]
    assert "displayName" not in rank and rank["nativeQueryRef"] == "Rank"


def test_a_blank_header_is_refused_for_a_field_a_calculation_names() -> None:
    document = load()
    run(
        document,
        {
            "op": "set_visual_calculation",
            "visual": TABLE,
            "name": "Running",
            "expression": "RUNNINGSUM([Rank])",
        },
    )
    with pytest.raises(DocumentError, match="a blank header cannot be named in DAX"):
        run(
            document,
            {"op": "set_field_display_name", "visual": TABLE, "field": "Rank", "name": " "},
        )


def test_resetting_an_aggregated_column_restores_its_own_native_name() -> None:
    document = load()
    projections = visual(document, TABLE)["visual"]["query"]["queryState"]["Values"]["projections"]
    projections.append(
        {
            "field": {
                "Aggregation": {
                    "Expression": {
                        "Column": {
                            "Expression": {"SourceRef": {"Entity": "DimStore"}},
                            "Property": "StoreKey",
                        }
                    },
                    "Function": 0,
                }
            },
            "queryRef": "Sum(DimStore.StoreKey)",
            "nativeQueryRef": "Stores",
            "displayName": "Stores",
        }
    )
    outcome = run(
        document, {"op": "set_field_display_name", "visual": TABLE, "field": "Stores", "name": ""}
    )
    assert outcome.summaries == [
        "The field 'Stores' in the table of Agent ID goes by its own name, 'StoreKey', again."
    ]
    assert projections[-1]["nativeQueryRef"] == "StoreKey"
    assert "displayName" not in projections[-1]


def test_a_sort_by_a_calculation_follows_it_and_goes_with_it() -> None:
    document = load()
    run(
        document,
        {
            "op": "set_visual_calculation",
            "visual": TABLE,
            "name": "Running",
            "expression": "RUNNINGSUM([Rank])",
        },
    )
    query = visual(document, TABLE)["visual"]["query"]
    field = copy.deepcopy(query["queryState"]["Values"]["projections"][-1]["field"])
    sort = query.setdefault("sortDefinition", {}).setdefault("sort", [])
    sort.insert(0, {"field": field, "direction": "Descending"})
    run(
        document,
        {
            "op": "set_visual_calculation",
            "visual": TABLE,
            "name": "Running",
            "expression": "RUNNINGSUM([MTD Sales])",
        },
    )
    assert sort[0]["field"]["NativeVisualCalculation"]["Expression"] == "RUNNINGSUM([MTD Sales])"
    run(
        document,
        {"op": "set_visual_calculation", "visual": TABLE, "name": "Running", "new_name": "Total"},
    )
    assert sort[0]["field"]["NativeVisualCalculation"]["Name"] == "Total"
    run(
        document,
        {"op": "set_field_display_name", "visual": TABLE, "field": "MTD Sales", "name": "Sales"},
    )
    assert sort[0]["field"]["NativeVisualCalculation"]["Expression"] == "RUNNINGSUM([Sales])"
    sort[0]["field"]["NativeVisualCalculation"]["Expression"] = "RUNNINGSUM([Old])"  # stale copy
    outcome = run(document, {"op": "remove_visual_calculation", "visual": TABLE, "name": "total"})
    assert "Removed the sort by Total." in outcome.warnings
    assert "NativeVisualCalculation" not in json.dumps(query.get("sortDefinition", {}))


def test_the_family_dispatcher_numbers_failing_steps() -> None:
    document = load()
    operations = extras.parse_extras(
        [
            {"op": "set_page_view", "page": "1", "view": "fit_to_width"},
            {"op": "set_alt_text", "visual": "No such visual", "text": "x"},
        ]
    )
    with pytest.raises(DocumentError, match="^Step 2: There is no visual 'No such visual'"):
        extras.apply_extras(document, operations, model())
    with pytest.raises(DocumentError, match="^Step 1 \\(set_page_view\\): view: "):
        extras.parse_extras([{"op": "set_page_view", "page": "1", "view": "zoom"}])
