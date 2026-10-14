"""Round 3 of the canvas payload: what Studio draws of the developer features.

Conditional formatting evaluated per row, visual-level number formats, table totals and
layout, analytics lines, buttons and navigators, bookmarks, drillthrough and tooltip pages,
interactions, synced slicers, the phone layout, visual calculations and small multiples.
Every test builds the PBIR shapes (Desktop-authored where one exists: the Agents table's
field-value font colors and data bars) and asserts the exact payload
(docs/imported-report-render-contract.md, sections 12 to 22).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.integrity import sha256_file
from ai_powerbi_analyst.studio import live_report as lr
from ai_powerbi_analyst.studio.live_report import (
    LiveReportRequest,
    VisualSelection,
    compile_page,
    data_key,
    render_formatting,
    render_formatting_payload,
    render_page,
)
from ai_powerbi_analyst.studio.render_analytics import (
    evaluate_calculation,
    parse_calculation,
    percentile_exclusive,
    percentile_inclusive,
)
from ai_powerbi_analyst.studio.render_rules import blend, display_format
from ai_powerbi_analyst.studio.report_import import ImportedReport, read_report
from ai_powerbi_analyst.studio.report_tokens import property_is_rendered, unrendered_properties

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "imported"
WILDCARD = {"dataViewWildcard": {"matchingOption": 1}}
PAGE_1 = "ReportSection8454c812b60b8bb83b99"
DETAILS = "ReportSection3f1c0d5e7a9b2c4d6e8f"
DRILL = "ReportSectiond2a4c6e8f0b1a3c5e7d9"
TOOLTIP = "ReportSectionb7e9f1a3c5d7e9f1a3b5"


# --- builders -------------------------------------------------------------------------------


def lit(value: str) -> dict[str, Any]:
    return {"expr": {"Literal": {"Value": value}}}


def raw(value: str) -> dict[str, Any]:
    """An unwrapped literal, as a gradient stop or a rule value holds it."""
    return {"Literal": {"Value": value}}


def solid(value: str) -> dict[str, Any]:
    return {"solid": {"color": lit(f"'{value}'")}}


def rule(expression: dict[str, Any]) -> dict[str, Any]:
    return {"solid": {"color": {"expr": expression}}}


def col(name: str, entity: str = "Orders") -> dict[str, Any]:
    return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": name}}


def mea(name: str, entity: str = "Orders") -> dict[str, Any]:
    return {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": name}}


def cmp(kind: int, left: dict[str, Any], value: str) -> dict[str, Any]:
    return {"Comparison": {"ComparisonKind": kind, "Left": left, "Right": raw(value)}}


def entry(props: dict[str, Any], **selector: Any) -> dict[str, Any]:
    return {"properties": props, **({"selector": selector} if selector else {})}


def visual(
    name: str,
    visual_type: str,
    roles: dict[str, list[dict[str, Any]]] | None = None,
    *,
    objects: dict[str, Any] | None = None,
    container: dict[str, Any] | None = None,
    filters: dict[str, Any] | None = None,
    x: float = 0,
    y: float = 0,
    z: int = 0,
    hidden: bool = False,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    visual_body: dict[str, Any] = {"visualType": visual_type, **(body or {})}
    if roles:
        visual_body["query"] = {
            "queryState": {
                role: {
                    "projections": [
                        f
                        if "field" in f
                        else {
                            "field": f,
                            "queryRef": (
                                f"{next(iter(f.values()))['Expression']['SourceRef']['Entity']}."
                                f"{next(iter(f.values()))['Property']}"
                            ),
                        }
                        for f in fields
                    ]
                }
                for role, fields in roles.items()
            }
        }
    if objects:
        visual_body["objects"] = objects
    if container:
        visual_body["visualContainerObjects"] = container
    found: dict[str, Any] = {
        "name": name,
        "position": {"x": x, "y": y, "width": 300, "height": 200, "z": z},
        "visual": visual_body,
    }
    if filters:
        found["filterConfig"] = filters
    if hidden:
        found["isHidden"] = True
    return found


MEASURES = ["Sales", "Profit", "Color", "Title", "Icon", *(f"M{i}" for i in range(1, 11))]


def make_report(
    tmp_path: Path,
    pages: dict[str, dict[str, Any]],
    *,
    report: dict[str, Any] | None = None,
    parts: dict[str, Any] | None = None,
    name: str = "r.pbix",
) -> ImportedReport:
    """A PBIX of ``pages`` ({id: {"visuals": [...], "page": {...}, "mobile": {id: pos}}})."""
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Report/definition/pages/pages.json", json.dumps({"pageOrder": list(pages)}))
        for page_id, spec in pages.items():
            page = {"name": page_id, "displayName": page_id.title(), "width": 1280, "height": 720}
            z.writestr(
                f"Report/definition/pages/{page_id}/page.json",
                json.dumps({**page, **spec.get("page", {})}),
            )
            for v in spec.get("visuals", []):
                folder = f"Report/definition/pages/{page_id}/visuals/{v['name']}/"
                z.writestr(folder + "visual.json", json.dumps(v))
                if v["name"] in spec.get("mobile", {}):
                    z.writestr(
                        folder + "mobile.json",
                        json.dumps({"position": spec["mobile"][v["name"]]}),
                    )
        if report is not None:
            z.writestr("Report/definition/report.json", json.dumps(report))
        for part, content in (parts or {}).items():
            z.writestr(part, json.dumps(content))
        z.writestr("DataModel", b"model-bytes" * 20)
    found, _, _ = read_report(path)
    return ImportedReport(
        id="report-r3",
        workspace_id="w",
        name="R3",
        source_path=str(path),
        archived_path=str(path),
        source_hash=sha256_file(path),
        base_hash="a" * 64,
        format="PBIR",
        pages=found,
        tables=[{"TableName": "Orders", "ColumnName": c} for c in ("Region", "Segment", "Amount")],
        measures=[{"TableName": "Orders", "Name": m, "Expression": "1"} for m in MEASURES],
    )


def one_page(tmp_path: Path, *visuals: dict[str, Any], **kwargs: Any) -> ImportedReport:
    page = kwargs.pop("page", {})
    return make_report(tmp_path, {"one": {"visuals": list(visuals), "page": page}}, **kwargs)


class Engine:
    """Answers each query with the ``rows`` of the pieces it asks for (a row's visual tag
    is in the query) and remembers what it was asked. ``fail`` makes a query that asks for
    one of those tags fail, as an engine that cannot compute it."""

    label = "fake engine"

    def __init__(self, rows: list[dict[str, Any]], fail: tuple[str, ...] = ()) -> None:
        self.rows = rows
        self.fail = fail
        self.queries: list[tuple[str, int]] = []

    def query(self, report: Any, query: str, max_rows: int) -> dict[str, Any]:
        self.queries.append((query, max_rows))
        if any(f'"Visual", "{tag}"' in query for tag in self.fail):
            raise ValueError("The engine cannot compute this")
        return {"rows": [r for r in self.rows if f'"Visual", "{r["[Visual]"]}"' in query]}


def row(visual_id: str, *cells: Any, formatted: dict[int, str] | None = None) -> dict[str, Any]:
    """A result row: cells are texts (text) or numbers (number) or None (blank)."""
    found: dict[str, Any] = {"[Visual]": visual_id}
    for i, cell in enumerate(cells):
        if cell is None:
            found[f"[C{i}]"], found[f"[K{i}]"] = None, "blank"
        elif isinstance(cell, bool):
            found[f"[C{i}]"], found[f"[K{i}]"] = str(cell).lower(), "boolean"
        elif isinstance(cell, int | float):
            found[f"[C{i}]"], found[f"[K{i}]"] = str(cell), "number"
        else:
            found[f"[C{i}]"], found[f"[K{i}]"] = cell, "text"
    for i, text in (formatted or {}).items():
        found[f"[F{i}]"] = text
    return found


def by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {v["id"]: v for v in payload["visuals"]}


@pytest.fixture(autouse=True)
def no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # The synthetic archives have no decodable model: number formats stay empty.
    monkeypatch.setattr(lr, "model_formats", lambda *_: {})


NO_TOTALS = {"total": [entry({"totals": lit("false")})]}


def gradient(low: str, high: str, *, input_node: dict[str, Any] | None = None) -> dict[str, Any]:
    return rule(
        {
            "FillRule": {
                "Input": input_node or mea("Sales"),
                "FillRule": {
                    "linearGradient2": {
                        "min": {"color": raw(f"'{low}'")},
                        "max": {"color": raw(f"'{high}'")},
                    }
                },
            }
        }
    )


# --- conditional formatting -----------------------------------------------------------------


def test_a_table_gradient_background_is_a_computed_column_colored_per_row(tmp_path):
    table = visual(
        "t",
        "tableEx",
        {"Values": [col("Region"), mea("Sales")]},
        objects={
            **NO_TOTALS,
            "values": [
                entry(
                    {"backColor": gradient("#000000", "#FFFFFF")},
                    data=[WILDCARD],
                    metadata="Orders.Sales",
                )
            ],
        },
    )
    report = one_page(tmp_path, table)
    engine = Engine([row("t", "C", 10), row("t", "A", 0), row("t", "B", 5)])
    found = by_id(render_page(engine, report, "one", LiveReportRequest()))["t"]
    columns = found["columns"]
    assert [c["name"] for c in columns] == ["Region", "Sales", "values.backColor"]
    assert columns[1]["backgroundIndex"] == 2 and columns[0]["backgroundIndex"] is None
    assert columns[2]["kind"] == "computed" and columns[2]["computed"] == "gradient"
    assert columns[2]["hidden"] is True and columns[2]["role"] == "Formatting"
    # Lowest value takes the min stop, highest the max, 5 sits halfway (127.5 rounds up).
    assert found["rows"] == [["A", 0.0, "#000000"], ["B", 5.0, "#808080"], ["C", 10.0, "#ffffff"]]
    assert found["formatted_rows"] == [[None, None, None]] * 3
    assert found["fidelity"] == "exact" and found["unrendered"] == []
    # The computed column is not part of the query.
    query = engine.queries[0][0]
    assert "values.backColor" not in query and '"C2"' in query and '"C2", BLANK()' in query


def test_rules_color_the_font_by_the_first_case_that_holds(tmp_path):
    rules = rule(
        {
            "Conditional": {
                "Cases": [
                    {
                        "Condition": {
                            "And": {
                                "Left": cmp(2, mea("Sales"), "0D"),
                                "Right": cmp(3, mea("Sales"), "5D"),
                            }
                        },
                        "Value": raw("'#FF0000'"),
                    },
                    {"Condition": cmp(2, mea("Sales"), "5L"), "Value": raw("'#00FF00'")},
                ]
            }
        }
    )
    table = visual(
        "t",
        "tableEx",
        {"Values": [col("Region"), mea("Sales")]},
        objects={
            **NO_TOTALS,
            "values": [entry({"fontColor": rules}, data=[WILDCARD], metadata="Orders.Sales")],
        },
    )
    engine = Engine([row("t", "A", 0), row("t", "B", 4.5), row("t", "C", 5), row("t", "D", None)])
    found = by_id(render_page(engine, one_page(tmp_path, table), "one", LiveReportRequest()))["t"]
    assert found["columns"][1]["colorIndex"] == 2
    assert found["columns"][2]["computed"] == "rules"
    assert [r[2] for r in found["rows"]] == ["#ff0000", "#ff0000", "#00ff00", None]


def test_a_rule_on_a_summarized_column_adds_the_aggregate_to_the_query(tmp_path):
    amount = {"Aggregation": {"Expression": col("Amount"), "Function": 0}}
    table = visual(
        "t",
        "tableEx",
        {"Values": [col("Region"), mea("Sales")]},
        objects={
            **NO_TOTALS,
            "values": [
                entry(
                    {"backColor": gradient("#000000", "#FFFFFF", input_node=amount)},
                    data=[WILDCARD],
                    metadata="Orders.Sales",
                )
            ],
        },
    )
    visuals, query = compile_page(one_page(tmp_path, table), "one", LiveReportRequest())
    columns = visuals[0]["columns"]
    assert [c["name"] for c in columns] == ["Region", "Sales", "Sum of Amount", "values.backColor"]
    assert columns[2]["hidden"] is True and columns[2]["kind"] == "measure"
    assert "\"M2\", SUM('Orders'[Amount])" in query
    assert columns[1]["backgroundIndex"] == 3


def test_a_three_color_scale_with_numbers_and_a_blank_color():
    from ai_powerbi_analyst.studio.render_rules import color_rule, evaluate

    node = rule(
        {
            "FillRule": {
                "Input": mea("Sales"),
                "FillRule": {
                    "linearGradient3": {
                        "min": {"color": raw("'#000000'"), "value": raw("0D")},
                        "mid": {"color": raw("'#FF0000'"), "value": raw("50D")},
                        "max": {"color": raw("'#FFFFFF'"), "value": raw("100D")},
                        "nullColoringStrategy": {
                            "strategy": raw("'specificColor'"),
                            "color": raw("'#00FF00'"),
                        },
                    }
                },
            }
        }
    )
    found = color_rule(node, lambda _: 0, [])
    assert found is not None and found.kind == "gradient"
    rows = [[-5.0], [25.0], [50.0], [75.0], [150.0], [None]]
    assert evaluate(found, rows) == [
        "#000000",
        "#800000",
        "#ff0000",
        "#ff8080",
        "#ffffff",
        "#00ff00",
    ]


def test_blanks_count_as_zero_in_a_scale_unless_told_otherwise():
    from ai_powerbi_analyst.studio.render_rules import Rule, Stop, evaluate

    # Power BI's lowest and highest values ignore blanks; a blank colored "as zero" takes
    # the color of 0 within that range: below the lowest (10), so the first stop's.
    scale = Rule("gradient", 0, stops=(Stop("#000000", None), Stop("#ffffff", None)))
    assert evaluate(scale, [[10.0], [None], [20.0]]) == ["#000000", "#000000", "#ffffff"]
    assert evaluate(scale, [[-10.0], [None], [10.0]]) == ["#000000", "#808080", "#ffffff"]
    skip = Rule(
        "gradient", 0, stops=(Stop("#000000", None), Stop("#ffffff", None)), null_strategy="noColor"
    )
    assert evaluate(skip, [[10.0], [None], [20.0]]) == ["#000000", None, "#ffffff"]
    assert blend("#000000", "#ffffff", 0.5) == "#808080"


def test_a_field_value_chart_color_points_at_its_helper_and_is_sanitized(tmp_path):
    chart = visual(
        "c",
        "clusteredColumnChart",
        {"Category": [col("Region")], "Y": [mea("Sales")]},
        objects={"dataPoint": [entry({"fill": rule(mea("Color"))})]},
    )
    engine = Engine(
        [
            row("c", "A", 1, "#123456"),
            row("c", "B", 2, "red"),
            row("c", "C", 3, "url(javascript:x)"),
        ]
    )
    found = by_id(render_page(engine, one_page(tmp_path, chart), "one", LiveReportRequest()))["c"]
    assert found["formatting"]["series"]["ruleColors"] == [{"metadata": None, "index": 2}]
    assert found["formatting"]["series"]["defaultColor"] is None
    assert [r[2] for r in found["rows"]] == ["#123456", "red", None]
    assert found["columns"][2]["name"] == "Color" and found["columns"][2]["hidden"] is True
    assert found["fidelity"] == "exact"
    assert "\"M2\", 'Orders'[Color]" in engine.queries[0][0]


def test_a_chart_color_scale_of_one_series_is_computed_per_point(tmp_path):
    chart = visual(
        "c",
        "clusteredColumnChart",
        {"Category": [col("Region")], "Y": [mea("Sales")]},
        objects={
            "dataPoint": [
                entry(
                    {"fill": gradient("#000000", "#FFFFFF")},
                    data=[WILDCARD],
                    metadata="Orders.Sales",
                )
            ]
        },
    )
    engine = Engine([row("c", "A", 0), row("c", "B", 10)])
    found = by_id(render_page(engine, one_page(tmp_path, chart), "one", LiveReportRequest()))["c"]
    assert found["formatting"]["series"]["ruleColors"] == [{"metadata": "Orders.Sales", "index": 2}]
    assert [r[2] for r in found["rows"]] == ["#000000", "#ffffff"]
    assert found["fidelity"] == "exact"


def test_a_card_value_color_rule_uses_the_single_row(tmp_path):
    rules = rule(
        {
            "Conditional": {
                "Cases": [{"Condition": cmp(1, mea("Sales"), "100D"), "Value": raw("'#00AA00'")}],
                "DefaultValue": raw("'#AA0000'"),
            }
        }
    )
    card = visual(
        "k",
        "cardVisual",
        {"Data": [mea("Sales")]},
        objects={"value": [entry({"fontColor": rules}, metadata="Orders.Sales")]},
    )
    report = one_page(tmp_path, card)
    high = by_id(render_page(Engine([row("k", 150)]), report, "one", LiveReportRequest()))["k"]
    assert high["formatting"]["cardValue"]["colorIndex"] == 1
    assert high["rows"] == [[150.0, "#00aa00"]] and high["fidelity"] == "exact"
    low = by_id(render_page(Engine([row("k", 50)]), report, "one", LiveReportRequest()))["k"]
    assert low["rows"] == [[50.0, "#aa0000"]]
    assert low["formatting"]["cardLabel"]["colorIndex"] is None


def test_icons_are_named_per_cell_with_their_layout(tmp_path):
    icons = {
        "kind": "Icon",
        "layout": lit("'IconOnly'"),
        "verticalAlignment": lit("'Middle'"),
        "value": {
            "expr": {
                "Conditional": {
                    "Cases": [{"Condition": cmp(2, mea("Sales"), "5D"), "Value": raw("'ArrowUp'")}],
                    "DefaultValue": raw("'ArrowDown'"),
                }
            }
        },
    }
    table = visual(
        "t",
        "tableEx",
        {"Values": [col("Region"), mea("Sales")]},
        objects={
            **NO_TOTALS,
            "values": [entry({"icon": icons}, data=[WILDCARD], metadata="Orders.Sales")],
        },
    )
    engine = Engine([row("t", "A", 1), row("t", "B", 9)])
    found = by_id(render_page(engine, one_page(tmp_path, table), "one", LiveReportRequest()))["t"]
    assert found["columns"][1]["icon"] == {"index": 2, "layout": "only", "alignment": "middle"}
    assert [r[2] for r in found["rows"]] == ["ArrowDown", "ArrowUp"]
    assert found["unrendered"] == [] and found["fidelity"] == "exact"


def test_a_rule_studio_cannot_evaluate_leaves_the_visual_approximate(tmp_path):
    percent = rule(
        {
            "Conditional": {
                "Cases": [
                    {
                        "Condition": {
                            "Comparison": {
                                "ComparisonKind": 2,
                                "Left": mea("Sales"),
                                "Right": {"Arithmetic": {"Left": raw("1D"), "Right": raw("2D")}},
                            }
                        },
                        "Value": raw("'#FF0000'"),
                    }
                ]
            }
        }
    )
    table = visual(
        "t",
        "tableEx",
        {"Values": [col("Region"), mea("Sales")]},
        objects={
            **NO_TOTALS,
            "values": [entry({"backColor": percent}, data=[WILDCARD], metadata="Orders.Sales")],
        },
    )
    found = render_formatting(one_page(tmp_path, table), "one", LiveReportRequest())["t"]
    assert found["columns"][1]["backgroundIndex"] is None
    assert [c["name"] for c in found["columns"]] == ["Region", "Sales"]
    assert found["fidelity"] == "approximate" and "error" not in found


def test_a_rule_past_the_field_limit_is_approximate_not_a_failure(tmp_path):
    table = visual(
        "t",
        "tableEx",
        {"Values": [col("Region"), col("Segment"), *(mea(f"M{i}") for i in range(1, 11))]},
        objects={
            **NO_TOTALS,
            "values": [
                entry(
                    {"fontColor": rule(mea("Profit"))},
                    data=[WILDCARD],
                    metadata="Orders.M1",
                )
            ],
        },
    )
    found = render_formatting(one_page(tmp_path, table), "one", LiveReportRequest())["t"]
    assert "error" not in found and found["fidelity"] == "approximate"
    assert len(found["columns"]) == 12 and found["columns"][2]["colorIndex"] is None


def test_a_plain_color_for_every_cell_of_a_column_is_a_column_token(tmp_path):
    table = visual(
        "t",
        "tableEx",
        {"Values": [col("Region"), mea("Sales")]},
        objects={
            **NO_TOTALS,
            "values": [
                entry({"fontColor": solid("#123456")}, data=[WILDCARD], metadata="Orders.Sales")
            ],
        },
    )
    found = render_formatting(one_page(tmp_path, table), "one", LiveReportRequest())["t"]
    column = found["formatting"]["table"]["columns"]["Orders.Sales"]
    assert column["fontColor"] == "#123456" and column["background"] is None
    assert found["columns"][1]["colorIndex"] is None
    assert found["unrendered"] == [] and found["fidelity"] == "exact"


def test_the_data_key_follows_a_rule_but_not_a_plain_color(tmp_path):
    def chart(fill: dict[str, Any], title: str) -> dict[str, Any]:
        return visual(
            "c",
            "clusteredColumnChart",
            {"Category": [col("Region")], "Y": [mea("Sales")]},
            objects={"dataPoint": [entry({"fill": fill})]},
            container={"title": [entry({"text": lit(f"'{title}'")})]},
        )

    request = LiveReportRequest()

    def key(fill: dict[str, Any], title: str, name: str) -> str:
        report = one_page(tmp_path, chart(fill, title), name=name)
        return data_key(report, report.pages[0].visuals[0], request)

    red_blue = key(gradient("#FF0000", "#0000FF"), "A", "a.pbix")
    assert key(gradient("#FF0000", "#0000FF"), "B", "b.pbix") == red_blue  # a title edit
    assert key(gradient("#FF0000", "#00FF00"), "A", "c.pbix") != red_blue  # the colors change
    assert key(solid("#111111"), "A", "d.pbix") == key(solid("#222222"), "A", "e.pbix")


def test_the_agents_tables_data_bars_carry_axis_direction_and_bar_only():
    report = fixture_report("structure-twin.pbix")
    rows = render_formatting(report, report.pages[0].id, LiveReportRequest())
    columns = {c.get("query_ref"): c for c in rows["e82e02f93055d6b0b544"]["columns"]}
    assert columns["# Measures.vs PM"]["dataBars"] == {
        "positive": "#1E90FF",
        "negative": "#DC143C",
        "hideText": True,
        "axis": "#b3b3b3",
        "reverse": False,
        "min": None,
        "max": None,
    }
    # Desktop's field-value font colors keep pointing at their hidden helper measures.
    rank = columns["# Measures.Rank"]
    helper = rows["e82e02f93055d6b0b544"]["columns"][rank["colorIndex"]]
    assert helper["name"] == "CF Table" and helper["hidden"] is True


# --- the dashboard fixtures -----------------------------------------------------------------


def fixture_report(name: str) -> ImportedReport:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"{name} is not built in this checkout")
    pages, _, _ = read_report(path)
    columns: set[tuple[str, str]] = set()
    measures: set[tuple[str, str]] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for kind, target in (("Column", columns), ("Measure", measures)):
                found = node.get(kind)
                if isinstance(found, dict) and "Property" in found:
                    entity = found.get("Expression", {}).get("SourceRef", {}).get("Entity")
                    if entity:
                        target.add((entity, found["Property"]))
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    for page in pages:
        for v in page.visuals:
            walk(v.definition)
    return ImportedReport(
        id="fixture",
        workspace_id="w",
        name="Fixture",
        source_path=str(path),
        archived_path=str(path),
        source_hash=sha256_file(path),
        base_hash="a" * 64,
        format="PBIR",
        pages=pages,
        tables=[{"TableName": t, "ColumnName": c} for t, c in sorted(columns)],
        measures=[{"TableName": t, "Name": m, "Expression": "1"} for t, m in sorted(measures)],
    )


# --- number formats -------------------------------------------------------------------------


def test_display_units_and_decimals_become_the_format_the_query_uses():
    assert display_format("$#,0.00", 1e6, None) == (1e6, '$#,0.00"M"')
    # Every section keeps its own text; the suffix goes after each section's digits.
    assert display_format("\\$#,0.00;(\\$#,0.00)", 1000, 0) == (
        1000.0,
        '\\$#,0"K";(\\$#,0"K")',
    )
    assert display_format("0.00%", 1000, 1) == (1.0, "0.0%")  # a percentage is not scaled
    assert display_format("", None, 2) == (1.0, "#,0.00")
    assert display_format("#,0", 1e9, None) == (1e9, '#,0"bn"')
    assert display_format("#,0", 1, None) == (1.0, "#,0")  # "None": no scaling
    assert display_format("#,0", 0, None) is None  # Auto
    assert display_format("#,0", 7, None) is None  # not a unit Power BI has
    assert display_format("Currency", 1000, None) == (1000.0, '$#,##0.00"K";($#,##0.00"K")')
    # Literal text is left alone: only the digits outside quotes change.
    assert display_format('"Rank #"0', None, 1) == (1.0, '"Rank #"0.0')


def test_negative_and_zero_sections_keep_their_text():
    """Review bug 4: '▲ 0.0%; ▼ -0.0%' with 2 decimals showed a negative value as
    '-▲ 87.42%', and a bracketed currency lost its brackets."""
    from ai_powerbi_analyst.studio.dax_format import format_number

    arrows = display_format("▲ 0.0%; ▼ -0.0%", None, 2)
    assert arrows == (1.0, "▲ 0.00%; ▼ -0.00%")
    assert format_number(-0.8742, arrows[1]) == " ▼ -87.42%"
    assert format_number(0.8742, arrows[1]) == "▲ 87.42%"
    money = display_format("\\$#,0.00;(\\$#,0.00)", 1000, 1)
    assert money is not None and format_number(-15400 / money[0], money[1]) == "($15.4K)"
    zero = display_format('#,0;-#,0;"zero"', 1000, None)
    assert zero is not None and format_number(0, zero[1]) == "zero"


def test_a_table_columns_units_and_decimals_format_its_cells(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, "model_formats", lambda *_: {("Orders", "Sales"): "#,0"})
    table = visual(
        "t",
        "tableEx",
        {"Values": [col("Region"), mea("Sales"), mea("Profit")]},
        objects={
            **NO_TOTALS,
            "columnFormatting": [
                entry(
                    {"labelDisplayUnits": lit("1000D"), "labelPrecision": lit("1L")},
                    metadata="Orders.Sales",
                )
            ],
            "general": [entry({"formatString": lit("'0.0%'")}, metadata="Orders.Profit")],
        },
    )
    visuals, query = compile_page(one_page(tmp_path, table), "one", LiveReportRequest())
    sales, profit = visuals[0]["columns"][1], visuals[0]["columns"][2]
    assert (sales["format"], sales["displayUnits"], sales["decimals"]) == ("#,0", 1000.0, 1)
    assert profit["format"] == "0.0%" and "displayUnits" not in profit
    assert 'FORMAT([M1]/1000, "#,0.0""K""", "en-US")' in query
    assert 'FORMAT([M2], "0.0%", "en-US")' in query
    columns = visuals[0]["formatting"]["table"]["columns"]
    assert columns["Orders.Sales"]["displayUnits"] == 1000.0
    assert columns["Orders.Sales"]["decimals"] == 1


def test_a_card_with_its_own_units_does_not_pick_them_automatically(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, "model_formats", lambda *_: {("Orders", "Sales"): "#,0"})
    fixed = visual(
        "k",
        "cardVisual",
        {"Data": [mea("Sales")]},
        objects={"value": [entry({"labelDisplayUnits": lit("1000000D")}, metadata="Orders.Sales")]},
    )
    _, query = compile_page(one_page(tmp_path, fixed), "one", LiveReportRequest())
    assert 'FORMAT([M0]/1000000, "#,0""M""", "en-US")' in query and "SWITCH" not in query
    automatic = visual("k", "cardVisual", {"Data": [mea("Sales")]})
    _, query = compile_page(
        one_page(tmp_path, automatic, name="b.pbix"), "one", LiveReportRequest()
    )
    assert 'ABS([M0]) >= 1000000000, FORMAT([M0]/1000000000,"#,0""bn""","en-US")' in query


def test_axis_display_units_and_decimals_are_tokens(tmp_path):
    chart = visual(
        "c",
        "clusteredColumnChart",
        {"Category": [col("Region")], "Y": [mea("Sales")]},
        objects={
            "valueAxis": [entry({"labelDisplayUnits": lit("1000D"), "labelPrecision": lit("2L")})]
        },
    )
    found = render_formatting(one_page(tmp_path, chart), "one", LiveReportRequest())["c"]
    axis = found["formatting"]["axes"]["value"]
    assert (axis["displayUnits"], axis["decimals"]) == (1000.0, 2)
    assert found["formatting"]["axes"]["category"]["displayUnits"] is None
    assert found["fidelity"] == "exact"


# --- table layout and totals ----------------------------------------------------------------


def test_a_table_shows_totals_computed_by_a_second_piece_of_the_query(tmp_path):
    table = visual("t", "tableEx", {"Values": [col("Region"), mea("Sales"), mea("Profit")]})
    report = one_page(tmp_path, table)
    total = row("t~total", None, 15, 3, formatted={1: "15"})
    engine = Engine([row("t", "A", 5, 1), row("t", "B", 10, 2), total])
    found = by_id(render_page(engine, report, "one", LiveReportRequest()))["t"]
    assert found["totals"] == {"values": [None, 15.0, 3.0], "formatted": [None, "15", None]}
    assert found["rows"] == [["A", 5.0, 1.0], ["B", 10.0, 2.0]]
    # The page query is what it was before totals existed; the totals are a second query.
    (page, page_rows), (extras, extra_rows) = engine.queries
    assert "~total" not in page and page_rows == 102
    assert "ROW(\"M1\", 'Orders'[Sales],\"M2\", 'Orders'[Profit])" in extras
    assert '"Visual", "t~total"' in extras and extra_rows == 2
    assert found["formatting"]["table"]["total"]["show"] is True
    assert found["fidelity"] == "exact" and "dataNotes" not in found
    # A formatting-only render has no totals: they are data.
    light = render_formatting(report, "one", LiveReportRequest())["t"]
    assert "totals" not in light and light["data_key"] == found["data_key"]


def test_totals_the_engine_cannot_compute_leave_the_page_local(tmp_path):
    """Review bug 1: a measure that fails at the grand total sent the whole page to
    Desktop. Now only that table loses its totals row, marked approximate."""
    table = visual("t", "tableEx", {"Values": [col("Region"), mea("Sales")]})
    chart = visual("c", "clusteredColumnChart", {"Category": [col("Region")], "Y": [mea("Sales")]})
    report = one_page(tmp_path, table, chart)
    engine = Engine(
        [row("t", "A", 5), row("c", "A", 5), row("t~total", None, 5)], fail=("t~total",)
    )
    rows = by_id(render_page(engine, report, "one", LiveReportRequest()))
    table_row = rows["t"]
    assert table_row["rows"] == [["A", 5.0]] and "totals" not in table_row
    assert table_row["fidelity"] == "approximate"
    assert "total.totals" in table_row["unrendered"]
    assert table_row["dataNotes"] == [
        "The totals, highlights or whole-visual values of this visual could not be computed here"
    ]
    assert rows["c"]["rows"] == [["A", 5.0]] and rows["c"]["fidelity"] == "exact"
    # The shared extras query failed, then each visual's own was tried: 1 + 1 + 1 queries.
    assert len(engine.queries) == 3


def test_totals_under_a_measure_filter_are_not_invented(tmp_path):
    measure_filter = {
        "filters": [
            {
                "filter": {
                    "Version": 2,
                    "From": [{"Name": "o", "Entity": "Orders", "Type": 0}],
                    "Where": [
                        {
                            "Condition": {
                                "Comparison": {
                                    "ComparisonKind": 1,
                                    "Left": {
                                        "Measure": {
                                            "Expression": {"SourceRef": {"Source": "o"}},
                                            "Property": "Sales",
                                        }
                                    },
                                    "Right": raw("1D"),
                                }
                            }
                        }
                    ],
                }
            }
        ]
    }
    table = visual(
        "t", "tableEx", {"Values": [col("Region"), mea("Sales")]}, filters=measure_filter
    )
    engine = Engine([row("t", "A", 5)])
    found = by_id(render_page(engine, one_page(tmp_path, table), "one", LiveReportRequest()))["t"]
    assert "~total" not in engine.queries[0][0] and "totals" not in found
    assert found["fidelity"] == "approximate" and "error" not in found


def test_total_row_wrap_and_grid_weight_tokens(tmp_path):
    table = visual(
        "t",
        "tableEx",
        {"Values": [col("Region"), mea("Sales")]},
        objects={
            "total": [
                entry(
                    {
                        "label": lit("'Grand total'"),
                        "fontColor": solid("#FF0000"),
                        "bold": lit("false"),
                    }
                )
            ],
            "values": [entry({"wordWrap": lit("false")})],
            "grid": [entry({"gridHorizontalWeight": lit("2D")})],
        },
    )
    found = render_formatting(one_page(tmp_path, table), "one", LiveReportRequest())["t"]
    tokens = found["formatting"]["table"]
    assert tokens["total"] == {
        "show": True,
        "label": "Grand total",
        "fontSize": 13.33,
        "color": "#FF0000",
        "family": '"Segoe UI"',
        "background": None,
        "bold": False,
    }
    assert tokens["values"]["wrap"] is False and tokens["header"]["wrap"] is True
    assert tokens["grid"]["horizontalWeight"] == 2.0 and tokens["grid"]["verticalWeight"] is None
    assert found["unrendered"] == [] and found["fidelity"] == "exact"


# --- analytics lines ------------------------------------------------------------------------


def select_ref(query_ref: str) -> dict[str, Any]:
    return {"SelectRef": {"ExpressionName": query_ref}}


def test_constant_average_percentile_and_trend_lines_are_computed_from_the_rows(tmp_path):
    chart = visual(
        "c",
        "clusteredColumnChart",
        {"Category": [col("Region")], "Y": [mea("Sales")]},
        objects={
            "y1AxisReferenceLine": [
                entry(
                    {
                        "show": lit("true"),
                        "displayName": lit("'Target'"),
                        "value": lit("7D"),
                        "lineColor": solid("#FF0000"),
                        "style": lit("'dotted'"),
                        "transparency": lit("50D"),
                        "dataLabelShow": lit("true"),
                    },
                    id="0",
                )
            ],
            "referenceLine": [
                entry(
                    {
                        "show": lit("true"),
                        "value": {
                            "expr": {
                                "Aggregation": {
                                    "Expression": select_ref("Orders.Sales"),
                                    "Function": 1,
                                }
                            }
                        },
                    },
                    id="1",
                ),
                entry(
                    {
                        "show": lit("true"),
                        "value": {
                            "expr": {
                                "Percentile": {"Expression": select_ref("Orders.Sales"), "K": 0.5}
                            }
                        },
                    },
                    id="2",
                ),
            ],
            "trend": [entry({"show": lit("true")})],
        },
    )
    engine = Engine([row("c", "B", 4), row("c", "A", 2), row("c", "C", 9)])
    found = by_id(render_page(engine, one_page(tmp_path, chart), "one", LiveReportRequest()))["c"]
    lines = found["referenceLines"]
    assert [(line["kind"], line["value"]) for line in lines] == [
        ("constant", 7.0),
        ("average", 5.0),
        ("percentile", 4.0),
        ("trend", None),
    ]
    assert lines[0] == {
        "id": "0",
        "object": "y1AxisReferenceLine",
        "kind": "constant",
        "axis": "value",
        "series": "Orders.Sales",
        "value": 7.0,
        "name": "Target",
        "color": "#FF0000",
        "alpha": 0.5,
        "style": "dotted",
        "width": None,
        "position": "front",
        "label": {
            "show": True,
            "text": "Value",
            "color": None,
            "decimals": None,
            "displayUnits": None,
            "horizontal": "left",
            "vertical": "above",
        },
    }
    # Least squares over A=2, B=4, C=9 in display order: 1.5 + 3.5x.
    assert (lines[3]["start"], lines[3]["end"]) == (1.5, 8.5)
    assert found["fidelity"] == "exact" and found["unrendered"] == []
    assert (
        "referenceLines"
        not in render_formatting(
            one_page(tmp_path, chart, name="b.pbix"), "one", LiveReportRequest()
        )["c"]
    )


def test_a_constant_line_a_measure_gives_is_read_from_the_totals_piece(tmp_path):
    chart = visual(
        "c",
        "clusteredColumnChart",
        {"Category": [col("Region")], "Y": [mea("Sales")]},
        objects={"referenceLine": [entry({"value": {"expr": mea("Profit")}}, id="0")]},
    )
    engine = Engine([row("c", "A", 2), row("c~total", None, 6, 3)])
    found = by_id(render_page(engine, one_page(tmp_path, chart), "one", LiveReportRequest()))["c"]
    assert [(line["kind"], line["value"]) for line in found["referenceLines"]] == [("measure", 3.0)]
    assert found["columns"][2]["name"] == "Profit" and found["columns"][2]["hidden"] is True


def test_analytics_studio_does_not_draw_are_listed():
    body = {
        "objects": {
            "error": [entry({"enabled": lit("true")})],
            "forecast": [entry({"show": lit("true")})],
            "y1AxisReferenceLine": [entry({"value": lit("1D"), "shadeShow": lit("true")}, id="0")],
        }
    }
    assert unrendered_properties("lineChart", body) == [
        "error.enabled",
        "forecast.show",
        "y1AxisReferenceLine.shadeShow",
    ]


def test_percentiles_interpolate_like_percentile_inc():
    assert percentile_inclusive([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert percentile_inclusive([10.0], 90) == 10.0
    assert percentile_inclusive([1.0, 2.0, 3.0, 4.0, 5.0], 90) == 4.6


# --- buttons and navigators -----------------------------------------------------------------


def test_a_button_has_a_look_per_state_and_an_action(tmp_path):
    button = visual(
        "b",
        "actionButton",
        objects={
            "fill": [
                entry({"fillColor": solid("#999999")}),  # no state selector: Desktop ignores it
                entry({"show": lit("true"), "fillColor": solid("#112233")}, id="default"),
                entry({"fillColor": solid("#445566")}, id="hover"),
            ],
            "text": [entry({"show": lit("true"), "text": lit("'Go'")}, id="default")],
            "icon": [entry({"shapeType": lit("'rightArrow'")}, id="default")],
        },
        container={
            "visualLink": [
                entry(
                    {
                        "show": lit("true"),
                        "type": lit("'PageNavigation'"),
                        "navigationSection": lit("'two'"),
                        "tooltip": lit("'Next page'"),
                    }
                )
            ]
        },
    )
    report = make_report(tmp_path, {"one": {"visuals": [button]}, "two": {}})
    found = render_formatting(report, "one", LiveReportRequest())["b"]
    assert found["kind"] == "button" and found["fidelity"] == "exact"
    token = found["formatting"]["button"]
    assert (token["shape"], token["tileShape"], token["radius"]) == ("rectangle", "rectangle", None)
    default, hover = token["states"]["default"], token["states"]["hover"]
    assert default["fill"] == "#112233ff" and hover["fill"] == "#445566ff"
    assert token["states"]["selected"] is None and token["states"]["disabled"] is None
    assert default["text"]["show"] is True and default["text"]["text"] == "Go"
    assert hover["text"]["text"] == "Go"  # a state takes what it does not set from default
    assert default["icon"]["name"] == "rightArrow" and default["icon"]["show"] is True
    assert default["outline"] == {"show": True, "color": "#605E5C", "width": 1.0}
    assert found["action"] == {
        "type": "page",
        "target": "two",
        "valid": True,
        "tooltip": "Next page",
    }


@pytest.mark.parametrize(
    ("link", "action"),
    [
        (
            {"type": lit("'WebUrl'"), "webUrl": lit("'https://example.com/a'")},
            {"type": "url", "target": None, "valid": True, "url": "https://example.com/a"},
        ),
        (
            {"type": lit("'WebUrl'"), "webUrl": lit("'javascript:alert(1)'")},
            {"type": "url", "target": None, "valid": False, "url": None},
        ),
        ({"type": lit("'Back'")}, {"type": "back", "target": None, "valid": True}),
        (
            {"type": lit("'Bookmark'"), "bookmark": lit("'Gone'")},
            {"type": "bookmark", "target": "Gone", "valid": False},
        ),
        (
            {"type": lit("'Drillthrough'"), "drillthroughSection": lit("'one'")},
            {"type": "drillthrough", "target": "one", "valid": True},
        ),
    ],
)
def test_actions_say_where_they_go_and_whether_it_exists(tmp_path, link, action):
    shape = visual("s", "shape", container={"visualLink": [entry({"show": lit("true"), **link})]})
    found = render_formatting(one_page(tmp_path, shape), "one", LiveReportRequest())["s"]
    assert found["action"] == {**action, "tooltip": None}


def test_a_page_navigator_lists_the_pages_desktop_shows(tmp_path):
    navigator = visual(
        "n",
        "pageNavigator",
        objects={"layout": [entry({"orientation": lit("1D"), "rowCount": lit("2L")})]},
    )
    report = make_report(
        tmp_path,
        {
            "one": {"visuals": [navigator]},
            "two": {"page": {"visibility": "HiddenInViewMode"}},
            "three": {"page": {"type": "Tooltip"}},
            "four": {},
        },
    )
    found = render_formatting(report, "one", LiveReportRequest())["n"]
    assert found["kind"] == "navigator" and found["fidelity"] == "exact"
    assert found["formatting"]["navigator"] == {
        "kind": "pages",
        "items": [
            {
                "label": "One",
                "action": {"type": "page", "target": "one", "valid": True},
                "selected": True,
            },
            {
                "label": "Four",
                "action": {"type": "page", "target": "four", "valid": True},
                "selected": False,
            },
        ],
    }
    assert found["formatting"]["button"]["layout"] == {
        "orientation": "vertical",
        "rows": 2,
        "columns": None,
        "cellPadding": None,
    }
    assert "icon" not in found["formatting"]["button"]["states"]["default"]


def test_a_bookmark_navigator_lists_the_reports_bookmarks():
    report = fixture_report("multipage.pbix")
    body = {"visualType": "bookmarkNavigator"}
    navigator = (
        report.pages[1]
        .visuals[0]
        .model_copy(
            update={"id": "nav", "visual_type": "bookmarkNavigator", "definition": {"visual": body}}
        )
    )
    page = report.pages[1].model_copy(update={"visuals": [*report.pages[1].visuals, navigator]})
    from ai_powerbi_analyst.studio import render_navigation as nav

    with zipfile.ZipFile(report.archived_path) as z:
        bookmarks = nav.read_bookmarks(z)
    items = nav.navigator_items(navigator, [page], bookmarks, page.id, "Details view")
    assert [(i["label"], i["action"]["target"], i["selected"]) for i in items] == [
        ("Channel focus", "Bookmarkc7a5beae9c2a6a44bb3c", False),
        ("Details view", "Bookmark213a31c763c98e893e32", True),
    ]


# --- bookmarks ------------------------------------------------------------------------------


def test_the_page_payload_lists_the_reports_bookmarks():
    report = fixture_report("multipage.pbix")
    payload = render_formatting_payload(report, PAGE_1, LiveReportRequest())
    assert payload["bookmark"] is None
    assert payload["bookmarks"] == [
        {
            "name": "Bookmarkc7a5beae9c2a6a44bb3c",
            "displayName": "Channel focus",
            "group": None,
            "page": PAGE_1,
            "targets": ["54be5cf0783e66471540", "b404a461ede7604a2774"],
            "captures": {"data": True, "display": True, "page": True},
            "hidden": [],
            "shown": ["54be5cf0783e66471540", "b404a461ede7604a2774"],
        },
        {
            "name": "Bookmark213a31c763c98e893e32",
            "displayName": "Details view",
            "group": None,
            "page": DETAILS,
            "targets": None,
            "captures": {"data": True, "display": True, "page": True},
            "hidden": [],
            # A whole-page bookmark also shows the visuals it does not list.
            "shown": [
                "04ba32659627f6e8c7d7",
                "2b5b8e3a7ed15138a983",
                "81ab76274a5b4e9e62da",
                "d6c05c8319906941fe02",
                "dfb4645ec076d1209ebe",
            ],
        },
    ]


def test_a_bookmark_applies_its_slicer_selection_to_the_page():
    report = fixture_report("multipage.pbix")
    plain = LiveReportRequest()
    marked = LiveReportRequest(bookmark="Channel focus")
    visuals, query = compile_page(report, PAGE_1, marked)
    slicer = next(v for v in visuals if v["id"] == "54be5cf0783e66471540")
    assert slicer["selected"] == ["Online"]
    assert "'DimChannel'[ChannelName] IN {\"Online\"}" in query
    payload = render_formatting_payload(report, PAGE_1, marked)
    assert payload["bookmark"] == {
        "name": "Bookmarkc7a5beae9c2a6a44bb3c",
        "displayName": "Channel focus",
        "page": PAGE_1,
        "captures": {"data": True, "display": True, "page": True},
        "notes": [],
    }
    # The slicer's saved state is data: a table the slicer filters gets another key.
    table = next(v for v in report.pages[0].visuals if v.id == "e82e02f93055d6b0b544")
    assert data_key(report, table, marked) != data_key(report, table, plain)
    with pytest.raises(ValueError, match="no bookmark named 'Nope'"):
        render_formatting(report, PAGE_1, LiveReportRequest(bookmark="Nope"))


def bookmark_part(name: str, page: str, containers: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "displayName": name.title(),
        "name": name,
        "explorationState": {
            "version": "1.3",
            "activeSection": page,
            "sections": {page: {"visualContainers": containers, **extra.pop("section", {})}},
            **extra.pop("exploration", {}),
        },
        **extra,
    }


def test_a_bookmark_hides_and_shows_visuals_and_says_what_it_cannot_apply(tmp_path):
    keep = visual("keep", "textbox", hidden=True)
    drop = visual("drop", "textbox")
    containers = {
        "keep": {"singleVisual": {"visualType": "textbox"}},
        "drop": {
            "singleVisual": {
                "visualType": "textbox",
                "display": {"mode": "hidden"},
                "orderBy": [{"Direction": 2, "Expression": mea("Sales")}],
            }
        },
    }
    parts = {
        "Report/definition/bookmarks/bookmarks.json": {
            "items": [{"name": "group", "displayName": "Views", "children": ["focus", "away"]}]
        },
        "Report/definition/bookmarks/focus.bookmark.json": bookmark_part(
            "focus", "one", containers
        ),
        "Report/definition/bookmarks/away.bookmark.json": bookmark_part("away", "two", containers),
    }
    report = make_report(tmp_path, {"one": {"visuals": [keep, drop]}, "two": {}}, parts=parts)
    payload = render_formatting_payload(report, "one", LiveReportRequest(bookmark="Focus"))
    rows = payload["visuals"]
    assert rows["keep"]["hidden"] is False and rows["drop"]["hidden"] is True
    assert rows["drop"]["hidden_own"] is True
    assert payload["bookmark"]["notes"] == [
        "The sort, drill level or field choices this bookmark saved are shown in Power BI only"
    ]
    assert [(b["name"], b["group"]) for b in payload["bookmarks"]] == [
        ("focus", {"name": "group", "displayName": "Views"}),
        ("away", {"name": "group", "displayName": "Views"}),
    ]
    # A bookmark of another page leaves this one as the file has it, and says where it goes.
    away = render_formatting_payload(report, "one", LiveReportRequest(bookmark="away"))
    assert away["visuals"]["keep"]["hidden"] is True and away["visuals"]["drop"]["hidden"] is False
    assert away["bookmark"]["notes"] == ["This bookmark opens another page"]
    plain = render_formatting(report, "one", LiveReportRequest())
    assert plain["keep"]["hidden"] is True and plain["drop"]["hidden"] is False


def test_a_bookmarks_page_filter_state_replaces_the_pages_by_name(tmp_path):
    def region_filter(value: str) -> dict[str, Any]:
        return {
            "Version": 2,
            "From": [{"Name": "o", "Entity": "Orders", "Type": 0}],
            "Where": [
                {
                    "Condition": {
                        "In": {
                            "Expressions": [
                                {
                                    "Column": {
                                        "Expression": {"SourceRef": {"Source": "o"}},
                                        "Property": "Region",
                                    }
                                }
                            ],
                            "Values": [[raw(f"'{value}'")]],
                        }
                    }
                }
            ],
        }

    chart = visual("c", "clusteredColumnChart", {"Category": [col("Region")], "Y": [mea("Sales")]})
    page = {"filterConfig": {"filters": [{"name": "F1", "filter": region_filter("West")}]}}
    parts = {
        "Report/definition/bookmarks/east.bookmark.json": bookmark_part(
            "east",
            "one",
            {},
            section={"filters": {"byExpr": [{"name": "F1", "filter": region_filter("East")}]}},
        )
    }
    report = make_report(tmp_path, {"one": {"visuals": [chart], "page": page}}, parts=parts)
    _, west = compile_page(report, "one", LiveReportRequest())
    _, east = compile_page(report, "one", LiveReportRequest(bookmark="east"))
    assert 'IN {"West"}' in west and 'IN {"East"}' in east and "West" not in east


# --- drillthrough and tooltip pages ---------------------------------------------------------


def test_a_drillthrough_page_takes_values_for_its_bound_field():
    report = fixture_report("multipage.pbix")
    payload = render_formatting_payload(report, DRILL, LiveReportRequest())
    assert payload["page"]["page_type"] == "drillthrough"
    assert payload["page"]["binding"] == {
        "type": "drillthrough",
        "fields": [{"field": "DimStore.StoreName", "table": "DimStore", "column": "StoreName"}],
        "keepAllFilters": True,
        "crossReport": False,
    }
    request = LiveReportRequest.model_validate(
        {"drill": {"field": "DimStore.StoreName", "values": ["Store 1"]}}
    )
    visuals, query = compile_page(report, DRILL, request)
    assert "'DimStore'[StoreName] IN {\"Store 1\"}" in query
    card = visuals[0]
    plain = next(v for v in report.pages[2].visuals if v.id == card["id"])
    assert card["data_key"] != data_key(report, plain, LiveReportRequest())
    assert card["data_key"] == data_key(report, plain, request)
    # "Keep all filters" is on: the page also takes the source page's other columns, and
    # without the source page's filters (drill_source) its values are approximate.
    other = LiveReportRequest.model_validate(
        {"drill": [{"field": "DimChannel.ChannelName", "values": ["Online"]}]}
    )
    visuals, query = compile_page(report, DRILL, other)
    assert "'DimChannel'[ChannelName] IN {\"Online\"}" in query
    assert render_formatting_payload(report, DRILL, other)["notes"] == [lr.KEEP_ALL_FILTERS_NOTE]
    drawn = [v for v in visuals if v["kind"] in lr.DATA_KINDS and "error" not in v]
    assert drawn and all(v["fidelity"] == "approximate" for v in drawn)
    with pytest.raises(ValueError, match="Only drillthrough and tooltip pages"):
        compile_page(report, PAGE_1, request)


REGION = col("Region")
WEST = {
    "Version": 2,
    "From": [{"Name": "o", "Entity": "Orders", "Type": 0}],
    "Where": [
        {
            "Condition": {
                "In": {
                    "Expressions": [
                        {
                            "Column": {
                                "Expression": {"SourceRef": {"Source": "o"}},
                                "Property": "Region",
                            }
                        }
                    ],
                    "Values": [[raw("'West'")]],
                }
            }
        }
    ],
}


def drill_page(**binding: Any) -> dict[str, Any]:
    """A drillthrough page bound to Orders.Region, whose saved filter F1 holds 'West'."""
    return {
        "type": "Drillthrough",
        "pageBinding": {
            "name": "b",
            "type": "Drillthrough",
            "parameters": [{"name": "p", "boundFilter": "F1", "fieldExpr": REGION}],
            **binding,
        },
        "filterConfig": {
            "filters": [
                {
                    "name": "F1",
                    "field": REGION,
                    "type": "Categorical",
                    "filter": WEST,
                    "howCreated": "Drillthrough",
                }
            ]
        },
    }


def test_a_drill_value_replaces_the_filter_the_page_saved_for_its_field(tmp_path):
    card = visual("k", "card", {"Values": [mea("Sales")]})
    report = one_page(tmp_path, card, page=drill_page(acceptsFilterContext="None"))
    east = LiveReportRequest.model_validate(
        {"drill": {"field": "Orders.Region", "values": ["East"]}}
    )
    _, query = compile_page(report, "one", east)
    assert "'Orders'[Region] IN {\"East\"}" in query
    assert "West" not in query  # the saved value would blank the card
    payload = render_formatting_payload(report, "one", east)
    assert payload["page"]["binding"]["keepAllFilters"] is False
    assert payload["notes"] == []
    # Without a drill value the page shows what it saved.
    assert '"West"' in compile_page(report, "one", LiveReportRequest())[1]
    # With "Keep all filters" off only the bound field can be drilled.
    wrong = LiveReportRequest.model_validate(
        {"drill": [{"field": "Orders.Segment", "values": ["Consumer"]}]}
    )
    with pytest.raises(ValueError, match="not a drillthrough field of this page"):
        compile_page(report, "one", wrong)


def test_keep_all_filters_takes_the_source_pages_filters_and_selections(tmp_path):
    card = visual("k", "card", {"Values": [mea("Sales")]})
    chart = visual("c", "clusteredColumnChart", {"Category": [col("Segment")], "Y": [mea("Sales")]})
    other = visual("o", "clusteredColumnChart", {"Category": [REGION], "Y": [mea("Sales")]})
    source_filter = {
        "filters": [{"name": "S1", "field": REGION, "type": "Categorical", "filter": WEST}]
    }
    report = make_report(
        tmp_path,
        {
            "source": {"visuals": [chart, other], "page": {"filterConfig": source_filter}},
            "drill": {"visuals": [card], "page": drill_page()},
        },
    )
    request = LiveReportRequest.model_validate(
        {
            "drill": {"field": "Orders.Region", "values": ["West"]},
            "drill_source": {
                "page": "source",
                "visual": "o",
                "selections": [{"visual_id": "c", "values": ["Consumer"]}],
            },
        }
    )
    visuals, query = compile_page(report, "drill", request)
    # The source page's filter and the selection in its other chart come along.
    assert "'Orders'[Segment] IN {\"Consumer\"}" in query
    assert query.count('"West"') == 2  # the drill value and the source page's filter
    assert visuals[0]["fidelity"] == "exact"
    assert render_formatting_payload(report, "drill", request)["notes"] == []
    # A source visual that is not on the source page is refused.
    request = LiveReportRequest.model_validate(
        {
            "drill": {"field": "Orders.Region", "values": ["West"]},
            "drill_source": {"page": "source", "visual": "missing"},
        }
    )
    with pytest.raises(ValueError, match="not on its page"):
        compile_page(report, "drill", request)
    # Without drill_source the values are approximate and the envelope says why.
    request = LiveReportRequest.model_validate(
        {"drill": {"field": "Orders.Region", "values": ["W"]}}
    )
    visuals, _ = compile_page(report, "drill", request)
    assert visuals[0]["fidelity"] == "approximate"
    assert render_formatting_payload(report, "drill", request)["notes"] == [
        lr.KEEP_ALL_FILTERS_NOTE
    ]


def test_a_visual_names_its_tooltip_page_and_that_page_takes_the_hovered_point():
    report = fixture_report("multipage.pbix")
    rows = render_formatting(report, PAGE_1, LiveReportRequest())
    assert rows["b404a461ede7604a2774"]["tooltipPage"] == {
        "page": TOOLTIP,
        "title": "Sales tooltip",
        "width": 320,
        "height": 240,
    }
    assert "tooltipPage" not in rows["e82e02f93055d6b0b544"]
    hovered = LiveReportRequest.model_validate(
        {"drill": [{"field": "Clustered Employees.Clustered Employees", "values": ["TopN"]}]}
    )
    _, query = compile_page(report, TOOLTIP, hovered)
    assert "'Clustered Employees'[Clustered Employees] IN {\"TopN\"}" in query


# --- interactions and sync slicers ----------------------------------------------------------


def test_the_page_says_how_each_visual_reacts_to_a_selection_in_another():
    report = fixture_report("multipage.pbix")
    interactions = render_formatting_payload(report, PAGE_1, LiveReportRequest())["interactions"]
    # Explicit: the Channel slicer does not filter the chart; the Store slicer filters the table.
    assert interactions["54be5cf0783e66471540"]["b404a461ede7604a2774"] == "none"
    assert interactions["5b0dbccd61c6c4adb415"]["e82e02f93055d6b0b544"] == "filter"
    # Defaults: a chart highlights another column chart and filters a table or a card.
    assert interactions["b404a461ede7604a2774"]["da32cdbb0090e885649b"] == "highlight"
    assert interactions["b404a461ede7604a2774"]["e82e02f93055d6b0b544"] == "filter"
    assert interactions["b404a461ede7604a2774"]["fb4c5aa34e0b084b62b9"] == "filter"
    assert "b404a461ede7604a2774" not in interactions["b404a461ede7604a2774"]


def test_a_chart_selection_highlights_charts_and_filters_tables(tmp_path):
    source = visual("s", "clusteredColumnChart", {"Category": [col("Region")], "Y": [mea("Sales")]})
    target = visual(
        "c", "clusteredColumnChart", {"Category": [col("Segment")], "Y": [mea("Sales")]}
    )
    table = visual("t", "tableEx", {"Values": [col("Segment"), mea("Sales")]}, objects=NO_TOTALS)
    report = one_page(tmp_path, source, target, table)
    selection = [VisualSelection(visual_id="s", values=["West"])]
    request = LiveReportRequest(selections=selection, highlight=True)
    engine = Engine(
        [
            row("c", "Consumer", 10),
            row("c", "Corporate", 6),
            row("c~highlight", "Consumer", 4),
            row("t", "Consumer", 4),
        ]
    )
    rows = by_id(render_page(engine, report, "one", request))
    assert rows["c"]["rows"] == [["Consumer", 10.0], ["Corporate", 6.0]]
    assert rows["c"]["highlights"] == [["Consumer", 4.0], ["Corporate", None]]
    (page, _), (extras, _) = engine.queries

    def pieces_of(query: str) -> dict[str, str]:
        return {
            piece.split('"Visual", "', 1)[1].split('"', 1)[0]: piece
            for piece in query.split("SELECTCOLUMNS(")[1:]
        }

    main, lit_up = pieces_of(page), pieces_of(extras)
    assert set(main) == {"s", "c", "t"} and set(lit_up) == {"s~highlight", "c~highlight"}
    # The table is filtered by the selection, the other chart is not (it highlights).
    assert 'IN {"West"}' in main["t"] and 'IN {"West"}' not in main["c"]
    assert 'IN {"West"}' in lit_up["c~highlight"]
    # The source chart keeps all its bars and highlights the one selected (this fake engine
    # answers no row for it, so no highlights come back).
    assert 'IN {"West"}' not in main["s"] and "highlights" not in rows["s"]
    assert rows["s"]["rows"] == []
    report_setting = one_page(
        tmp_path,
        source,
        target,
        report={"settings": {"defaultFilterActionIsDataFilter": True}},
        name="b.pbix",
    )
    interactions = render_formatting_payload(report_setting, "one", LiveReportRequest())
    assert interactions["interactions"]["s"]["c"] == "filter"


def test_without_the_highlight_flag_a_selection_filters_as_before(tmp_path):
    """Review bug 2: a client that draws no highlights saw nothing change on a click. Without
    ``highlight: true`` every target (and the chart itself) is filtered, as before round 3."""
    source = visual("s", "clusteredColumnChart", {"Category": [col("Region")], "Y": [mea("Sales")]})
    target = visual(
        "c", "clusteredColumnChart", {"Category": [col("Segment")], "Y": [mea("Sales")]}
    )
    report = one_page(tmp_path, source, target)
    selection = [VisualSelection(visual_id="s", values=["West"])]
    engine = Engine([row("c", "Consumer", 4), row("s", "West", 4)])
    rows = by_id(render_page(engine, report, "one", LiveReportRequest(selections=selection)))
    assert len(engine.queries) == 1  # no highlight pieces
    assert engine.queries[0][0].count('IN {"West"}') == 2
    assert rows["c"]["rows"] == [["Consumer", 4.0]] and "highlights" not in rows["c"]
    # The highlight flag is part of the data key: the rows differ.
    lit = LiveReportRequest(selections=selection, highlight=True)
    chart = next(v for v in report.pages[0].visuals if v.id == "c")
    assert data_key(report, chart, lit) != rows["c"]["data_key"]


def test_synced_slicers_share_a_group_across_pages(tmp_path):
    sync = {"syncGroup": {"groupName": "Region", "fieldChanges": True, "filterChanges": True}}
    first = visual("a", "slicer", {"Values": [col("Region")]}, body=sync)
    second = visual("b", "slicer", {"Values": [col("Region")]}, body=sync, hidden=True)
    report = make_report(tmp_path, {"one": {"visuals": [first]}, "two": {"visuals": [second]}})
    payload = render_formatting_payload(report, "one", LiveReportRequest())
    assert payload["visuals"]["a"]["syncGroup"] == {
        "groupName": "Region",
        "fieldChanges": True,
        "filterChanges": True,
    }
    assert payload["sync_groups"] == {
        "Region": [
            {"page": "one", "visual": "a", "hidden": False},
            {"page": "two", "visual": "b", "hidden": True},
        ]
    }


# --- phone layout ---------------------------------------------------------------------------


def test_the_phone_layout_lists_the_visuals_placed_on_it():
    report = fixture_report("multipage.pbix")
    payload = render_formatting_payload(report, PAGE_1, LiveReportRequest())
    assert payload["mobile"] == {
        "visuals": {
            "eff2d7b04706cc92f1db": {"x": 0.0, "y": 0.0, "width": 320.0, "height": 60.0, "z": 0.0}
        }
    }
    assert render_formatting_payload(report, DETAILS, LiveReportRequest())["mobile"] is None


# --- visual calculations, small multiples, tooltips, dynamic titles ------------------------


def calc(name: str, expression: str) -> dict[str, Any]:
    return {
        "field": {
            "NativeVisualCalculation": {"Language": "dax", "Expression": expression, "Name": name}
        },
        "queryRef": name,
        "nativeQueryRef": name,
    }


def test_common_visual_calculations_are_computed_over_the_axis(tmp_path):
    table = visual(
        "t",
        "tableEx",
        {
            "Values": [
                col("Region"),
                mea("Sales"),
                calc("Running sum", "RUNNINGSUM([Sales])"),
                calc("Share", "DIVIDE([Sales], COLLAPSEALL([Sales], ROWS))"),
                calc("Odd", "RANK()"),
            ]
        },
        objects=NO_TOTALS,
    )
    table["visual"]["query"]["sortDefinition"] = {
        "sort": [{"field": mea("Sales"), "direction": "Descending"}]
    }
    report = one_page(tmp_path, table)
    # The share's denominator is the visual's grand total from the engine (10), not the sum
    # of the rows drawn.
    rows = [row("t", "B", 2), row("t", "A", 6), row("t", "C", 2), row("t~total", None, 10)]
    engine = Engine(rows)
    found = by_id(render_page(engine, report, "one", LiveReportRequest()))["t"]
    assert [c["name"] for c in found["columns"]] == [
        "Region",
        "Sales",
        "Running sum",
        "Share",
        "Odd",
    ]
    assert [c["kind"] for c in found["columns"][2:]] == ["computed"] * 3
    # Rows in the visual's sort (Sales descending), the running sum along the axis (A, B, C).
    assert found["rows"] == [
        ["A", 6.0, 6.0, 0.6, None],
        ["B", 2.0, 8.0, 0.2, None],
        ["C", 2.0, 10.0, 0.2, None],
    ]
    assert found["columns"][3]["format"] == "0.00%"
    # RANK() is not one Studio computes: drawn blank and the visual is approximate.
    assert found["fidelity"] == "approximate"
    assert "RUNNINGSUM" not in engine.queries[0][0]


def test_calculation_patterns_name_their_source_field():
    fields = [
        {"kind": "column", "label": "Month", "name": "Month"},
        {"kind": "measure", "label": "Sales", "name": "Sales"},
        {"kind": "computed", "label": "Calc", "name": "Calc"},
    ]
    moving = parse_calculation("MOVINGAVERAGE([Sales], 2)", fields, 2)
    assert moving is not None and (moving.kind, moving.source, moving.window) == (
        "movingAverage",
        1,
        2,
    )
    # Rows in another order than the axis (which is ascending by month: 01, 02, 03, 04).
    rows = [["03", None, None], ["01", 1.0, None], ["04", 5.0, None], ["02", 3.0, None]]
    # Window of two: 01 -> 1, 02 -> (1+3)/2, 03 -> 3 (a blank is skipped), 04 -> 5.
    assert evaluate_calculation(moving, fields, rows) == [3.0, 1.0, 5.0, 2.0]
    versus = parse_calculation("[Sales] - PREVIOUS([Sales])", fields, 2)
    assert versus is not None and versus.kind == "difference"
    # A blank is 0 in the subtraction: 01 -> 1, 02 -> 2, 03 -> -3, 04 -> 5.
    assert evaluate_calculation(versus, fields, rows) == [-3.0, 1.0, 5.0, 2.0]
    assert parse_calculation("[Missing] - PREVIOUS([Missing])", fields, 2) is None


def test_small_multiples_are_split_into_panels(tmp_path):
    chart = visual(
        "c",
        "clusteredColumnChart",
        {"Category": [col("Region")], "Y": [mea("Sales")], "Rows": [col("Segment")]},
        objects={
            "smallMultiplesLayout": [entry({"rowCount": lit("2L"), "columnCount": lit("3L")})]
        },
    )
    engine = Engine(
        [
            row("c", "East", 1, "Corporate"),
            row("c", "West", 2, "Consumer"),
            row("c", "East", 3, "Consumer"),
        ]
    )
    found = by_id(render_page(engine, one_page(tmp_path, chart), "one", LiveReportRequest()))["c"]
    assert found["formatting"]["smallMultiples"] == {
        "index": 2,
        "field": "Segment",
        "query_ref": "Orders.Segment",
        "layout": {"type": "auto", "rows": 2, "columns": 3},
    }
    # Rows sorted by Region: East/Corporate, East/Consumer, West/Consumer.
    assert found["smallMultiples"] == {
        "index": 2,
        "field": "Segment",
        "values": ["Consumer", "Corporate"],
        "panels": [
            {"value": "Consumer", "rows": [1, 2]},
            {"value": "Corporate", "rows": [0]},
        ],
    }
    assert found["fidelity"] == "exact"


def test_a_tooltip_field_is_queried_but_not_drawn_or_titled(tmp_path):
    chart = visual(
        "c",
        "clusteredColumnChart",
        {"Category": [col("Region")], "Y": [mea("Sales")], "Tooltips": [mea("Profit")]},
    )
    found = render_formatting(one_page(tmp_path, chart), "one", LiveReportRequest())["c"]
    assert found["columns"][2]["hidden"] is True and found["columns"][2]["role"] == "Tooltips"
    assert found["title"] == "Sales by Region"


def test_a_measure_driven_title_is_evaluated_for_the_whole_visual(tmp_path):
    chart = visual(
        "c",
        "clusteredColumnChart",
        {"Category": [col("Region")], "Y": [mea("Sales")]},
        container={
            "title": [entry({"text": {"expr": mea("Title")}, "fontColor": rule(mea("Color"))})]
        },
    )
    engine = Engine(
        [row("c", "A", 1, None, None), row("c~total", None, 1, "Sales <b>2026</b>", "#00FF00")]
    )
    found = by_id(render_page(engine, one_page(tmp_path, chart), "one", LiveReportRequest()))["c"]
    assert found["formatting"]["dynamicProperties"] == ["title.fontColor", "title.text"]
    assert found["formatting"]["title"]["textSource"] == "dynamic"
    assert found["dynamic"] == {"title.text": "Sales 2026", "title.fontColor": "#00FF00"}
    assert found["fidelity"] == "exact"


def test_a_cards_dynamic_title_is_read_from_the_whole_visual_piece(tmp_path):
    card = visual(
        "k",
        "cardVisual",
        {"Data": [mea("Sales")]},
        container={"title": [entry({"show": lit("true"), "text": {"expr": mea("Title")}})]},
    )
    engine = Engine([row("k", 5), row("k~total", 5, "Five")])
    found = by_id(render_page(engine, one_page(tmp_path, card), "one", LiveReportRequest()))["k"]
    assert found["dynamic"] == {"title.text": "Five"}
    assert found["fidelity"] == "exact"
    # The title's measure is not in the card's own piece: one it fails costs only the title.
    assert "[Title]" not in engine.queries[0][0] and "~total" not in engine.queries[0][0]
    assert "[Title]" in engine.queries[1][0]
    failing = Engine([row("k", 5)], fail=("k~total",))
    found = by_id(render_page(failing, one_page(tmp_path, card), "one", LiveReportRequest()))["k"]
    assert found["rows"] == [[5.0, None]] and "dynamic" not in found
    assert found["unrendered"] == ["title.text"]
    assert found["fidelity"] == "approximate" and found["dataNotes"]


# --- fidelity and the Format pane -----------------------------------------------------------


def test_the_rendered_table_answers_for_the_new_features():
    assert property_is_rendered("actionButton", "fill", "fillColor")
    assert property_is_rendered("actionButton", "icon", "shapeType")
    assert not property_is_rendered("actionButton", "glow", "show")
    assert property_is_rendered("pageNavigator", "layout", "orientation")
    assert property_is_rendered("tableEx", "total", "totals")
    assert property_is_rendered("tableEx", "values", "icon")
    assert property_is_rendered("tableEx", "columnFormatting", "labelDisplayUnits")
    assert property_is_rendered("clusteredColumnChart", "y1AxisReferenceLine", "value")
    assert property_is_rendered("clusteredColumnChart", "trend", "lineColor")
    assert property_is_rendered("cardVisual", "value", "labelDisplayUnits")
    assert not property_is_rendered("clusteredColumnChart", "error", "enabled")
    assert not property_is_rendered("tableEx", "values", "bold")  # still Power BI only
    # A button fill written without a state selector is ignored by Desktop and by Studio,
    # so it is not listed as something Studio fails to draw.
    body = {"objects": {"fill": [entry({"fillColor": solid("#111111")})]}}
    assert unrendered_properties("actionButton", body) == []


def test_formatting_rows_never_carry_data_keys(tmp_path):
    table = visual("t", "tableEx", {"Values": [col("Region"), mea("Sales")]})
    found = render_formatting(one_page(tmp_path, table), "one", LiveReportRequest())["t"]
    assert not set(found) & lr.DATA_KEYS
    assert {"totals", "highlights", "referenceLines", "smallMultiples", "dynamic"} <= lr.DATA_KEYS


# --- the shapes the conditional-formatting and navigation families write --------------------


def range_percent(fraction: float) -> dict[str, Any]:
    """Desktop's percent bound (lowest + fraction x (highest - lowest) of the visual)."""
    return {
        "RangePercent": {"Min": {"ScopedEval": {}}, "Max": {"ScopedEval": {}}, "Percent": fraction}
    }


def test_an_icon_set_splits_the_visuals_range_by_percent(tmp_path):
    # Desktop's 3 arrows: under 33% down, 33% to 67% right, 67% and up (highest first).
    cases = [
        {
            "Condition": {
                "Comparison": {
                    "ComparisonKind": 2,
                    "Left": mea("Sales"),
                    "Right": range_percent(0.67),
                }
            },
            "Value": raw("'ColoredArrowUp'"),
        },
        {
            "Condition": {
                "And": {
                    "Left": {
                        "Comparison": {
                            "ComparisonKind": 2,
                            "Left": mea("Sales"),
                            "Right": range_percent(0.33),
                        }
                    },
                    "Right": {
                        "Comparison": {
                            "ComparisonKind": 3,
                            "Left": mea("Sales"),
                            "Right": range_percent(0.67),
                        }
                    },
                }
            },
            "Value": raw("'ColoredArrowRight'"),
        },
        {
            "Condition": {
                "Comparison": {
                    "ComparisonKind": 3,
                    "Left": mea("Sales"),
                    "Right": range_percent(0.33),
                }
            },
            "Value": raw("'ColoredArrowDown'"),
        },
    ]
    icons = {
        "kind": "Icon",
        "layout": {"expr": raw("'Before'")},
        "verticalAlignment": {"expr": raw("'Top'")},
        "value": {"expr": {"Conditional": {"Cases": cases}}},
    }
    table = visual(
        "t",
        "tableEx",
        {"Values": [col("Region"), mea("Sales")]},
        objects={
            **NO_TOTALS,
            "values": [entry({"icon": icons}, data=[WILDCARD], metadata="Orders.Sales")],
        },
    )
    # Range 0..100: 33 and 67 are the bounds.
    engine = Engine([row("t", "A", 0), row("t", "B", 40), row("t", "C", 67), row("t", "D", 100)])
    found = by_id(render_page(engine, one_page(tmp_path, table), "one", LiveReportRequest()))["t"]
    assert found["columns"][1]["icon"] == {"index": 2, "layout": "before", "alignment": "top"}
    assert [r[2] for r in found["rows"]] == [
        "ColoredArrowDown",
        "ColoredArrowRight",
        "ColoredArrowUp",
        "ColoredArrowUp",
    ]


def test_rules_for_blank_values_and_for_any_value():
    from ai_powerbi_analyst.studio.render_rules import color_rule, evaluate

    node = rule(
        {
            "Conditional": {
                "Cases": [
                    {
                        "Condition": {
                            "Comparison": {
                                "ComparisonKind": 0,
                                "Left": mea("Sales"),
                                "Right": raw("null"),
                            }
                        },
                        "Value": raw("'#999999'"),
                    },
                    {"Condition": raw("true"), "Value": raw("'#000000'")},
                ]
            }
        }
    )
    found = color_rule(node, lambda _: 0, [])
    assert found is not None
    assert evaluate(found, [[None], [5.0]]) == ["#999999", "#000000"]


def test_a_card_rule_under_the_every_instance_selector_is_drawn(tmp_path):
    card = visual(
        "k",
        "cardVisual",
        {"Data": [mea("Sales")]},
        objects={
            "value": [
                entry({"fontColor": rule(mea("Color"))}, data=[WILDCARD], metadata="Orders.Sales")
            ]
        },
    )
    found = by_id(
        render_page(
            Engine([row("k", 5, "#00FF00")]), one_page(tmp_path, card), "one", LiveReportRequest()
        )
    )["k"]
    assert found["formatting"]["cardValue"]["colorIndex"] == 1
    assert found["rows"] == [[5.0, "#00FF00"]]
    assert found["unrendered"] == [] and found["fidelity"] == "exact"


def test_a_projections_own_format_and_a_custom_label_format(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, "model_formats", lambda *_: {("Orders", "Sales"): "#,0"})
    sales = {"field": mea("Sales"), "queryRef": "Orders.Sales", "format": "#,0.0"}
    chart = visual(
        "c",
        "clusteredColumnChart",
        {"Category": [col("Region")], "Y": [sales]},
        objects={
            "labels": [
                entry(
                    {
                        "show": lit("true"),
                        "labelDisplayUnits": lit("-1D"),
                        "valueCustomFormatString": lit("'0.00'"),
                    }
                )
            ]
        },
    )
    visuals, query = compile_page(one_page(tmp_path, chart), "one", LiveReportRequest())
    assert visuals[0]["columns"][1]["format"] == "#,0.0"
    assert 'FORMAT([M1], "#,0.0", "en-US")' in query
    labels = visuals[0]["formatting"]["labels"]
    assert labels["format"] == "0.00" and labels["displayUnits"] is None
    assert visuals[0]["fidelity"] == "exact"


def test_a_canvas_tooltip_names_its_page(tmp_path):
    chart = visual(
        "c",
        "clusteredColumnChart",
        {"Category": [col("Region")], "Y": [mea("Sales")]},
        container={
            "visualTooltip": [
                entry({"show": lit("true"), "type": lit("'Canvas'"), "section": lit("'tip'")})
            ]
        },
    )
    report = make_report(
        tmp_path,
        {
            "one": {"visuals": [chart]},
            "tip": {"page": {"type": "Tooltip", "width": 320, "height": 240}},
        },
    )
    found = render_formatting(report, "one", LiveReportRequest())["c"]
    assert found["tooltipPage"] == {"page": "tip", "title": "Tip", "width": 320, "height": 240}


def test_desktops_bookmark_serializer_shapes_are_applied(tmp_path):
    selection = {
        "filter": {
            "Version": 2,
            "From": [{"Name": "o", "Entity": "Orders", "Type": 0}],
            "Where": [
                {
                    "Condition": {
                        "In": {
                            "Expressions": [
                                {
                                    "Column": {
                                        "Expression": {"SourceRef": {"Source": "o"}},
                                        "Property": "Region",
                                    }
                                }
                            ],
                            "Values": [[raw("'East'")]],
                        }
                    }
                }
            ],
        }
    }
    region = visual(
        "region",
        "slicer",
        {"Values": [col("Region")]},
        objects={"general": [entry({"filter": {"filter": selection["filter"]}})]},
    )
    segment = visual("segment", "slicer", {"Values": [col("Segment")]})
    segment["visual"]["objects"] = {
        "general": [
            entry(
                {
                    "filter": {
                        "filter": {
                            **selection["filter"],
                            "Where": [
                                {
                                    "Condition": {
                                        "In": {
                                            "Expressions": [
                                                {
                                                    "Column": {
                                                        "Expression": {
                                                            "SourceRef": {"Source": "o"}
                                                        },
                                                        "Property": "Segment",
                                                    }
                                                }
                                            ],
                                            "Values": [[raw("'Consumer'")]],
                                        }
                                    }
                                }
                            ],
                        }
                    }
                }
            )
        ]
    }
    group = {
        "name": "g",
        "position": {"x": 0, "y": 0, "width": 100, "height": 100, "z": 0},
        "visualGroup": {"displayName": "g", "groupMode": "ScaleMode"},
    }
    member = visual("inside", "textbox")
    member["parentGroupName"] = "g"
    west = {**selection["filter"]}
    west["Where"] = json.loads(json.dumps(selection["filter"]["Where"]).replace("East", "West"))
    containers = {
        "region": {
            "singleVisual": {
                "visualType": "slicer",
                "objects": {"merge": {"general": [{"properties": {"filter": {"filter": west}}}]}},
            }
        }
    }
    parts = {
        "Report/definition/bookmarks/b.bookmark.json": bookmark_part(
            "b",
            "one",
            containers,
            section={"visualContainerGroups": {"g": {"isHidden": True}}},
        )
    }
    report = make_report(
        tmp_path, {"one": {"visuals": [region, segment, group, member]}}, parts=parts
    )
    visuals, query = compile_page(report, "one", LiveReportRequest(bookmark="b"))
    rows = {v["id"]: v for v in visuals}
    assert rows["region"]["selected"] == ["West"]
    # A whole-page bookmark clears the slicers it did not capture.
    assert rows["segment"]["selected"] == [] and "Consumer" not in query
    assert rows["g"]["hidden"] is True and rows["inside"]["hidden"] is True
    plain = {v["id"]: v for v in compile_page(report, "one", LiveReportRequest())[0]}
    assert plain["region"]["selected"] == ["East"] and plain["segment"]["selected"] == ["Consumer"]
    assert plain["g"]["hidden"] is False


def test_a_navigator_has_a_press_state_and_reads_a_selector_less_show(tmp_path):
    navigator = visual(
        "n",
        "bookmarkNavigator",
        objects={
            "fill": [
                entry({"show": lit("true")}),
                entry({"fillColor": solid("#101010")}, id="default"),
                entry({"fillColor": solid("#202020")}, id="press"),
            ]
        },
    )
    found = render_formatting(one_page(tmp_path, navigator), "one", LiveReportRequest())["n"]
    states = found["formatting"]["button"]["states"]
    assert list(states) == ["default", "hover", "press", "selected"]
    assert states["default"]["fill"] == "#101010ff" and states["press"]["fill"] == "#202020ff"
    assert states["hover"] is None
    assert found["formatting"]["button"]["layout"]["orientation"] == "horizontal"
    assert found["formatting"]["navigator"] == {"kind": "bookmarks", "items": []}


# --- what only Power BI Desktop can compute --------------------------------------------------


def test_calculation_groups_and_field_parameters_are_drawn_by_power_bi(tmp_path):
    chart = visual(
        "c", "clusteredColumnChart", {"Category": [col("Item", "Time Calc")], "Y": [mea("Sales")]}
    )
    plain = visual("p", "clusteredColumnChart", {"Category": [col("Region")], "Y": [mea("Sales")]})
    choice = visual(
        "s",
        "slicer",
        {"Values": [col("Metric", "Metric")]},
        objects={
            "general": [
                entry(
                    {
                        "filter": {
                            "filter": {
                                "Where": [
                                    {
                                        "Condition": {
                                            "In": {
                                                "Expressions": [col("Metric", "Metric")],
                                                "Values": [[raw("'Sales'")]],
                                            }
                                        }
                                    }
                                ]
                            }
                        }
                    }
                )
            ]
        },
    )
    report = one_page(tmp_path, chart, plain, choice)
    report = report.model_copy(
        update={
            "tables": [
                *report.tables,
                {"TableName": "Time Calc", "ColumnName": "Item"},
                {"TableName": "Metric", "ColumnName": "Metric"},
            ],
            "model": {
                "calculationGroups": [{"table": "Time Calc"}],
                "fieldParameters": [{"table": "Metric"}],
            },
        }
    )
    visuals, query = compile_page(report, "one", LiveReportRequest())
    rows = {v["id"]: v for v in visuals}
    assert rows["c"]["error"] == (
        "This visual uses the calculation group 'Time Calc', which Power BI applies"
    )
    assert rows["c"]["needs_desktop"] is True and rows["c"]["fidelity"] == "none"
    assert rows["s"]["error"] == (
        "This visual uses the field parameter 'Metric', which Power BI switches"
    )
    # The parameter's saved choice does not filter the rest of the page.
    assert "error" not in rows["p"] and "Metric" not in query


def test_a_hidden_projection_is_queried_but_not_drawn_and_field_parameters_are_approximate(
    tmp_path,
):
    hidden = {"field": mea("Profit"), "queryRef": "Orders.Profit", "hidden": True}
    table = visual(
        "t",
        "tableEx",
        {"Values": [col("Region"), mea("Sales"), hidden]},
        objects=NO_TOTALS,
    )
    table["visual"]["query"]["queryState"]["Values"]["fieldParameters"] = [
        {"parameterExpr": col("Metric", "Metric"), "index": 1, "length": 1}
    ]
    found = render_formatting(one_page(tmp_path, table), "one", LiveReportRequest())["t"]
    assert found["columns"][2]["hidden"] is True
    assert found["fidelity"] == "approximate" and "error" not in found


# --- the whole query on Studio's own DAX engine ---------------------------------------------


def test_the_new_query_pieces_run_on_studios_dax_engine(tmp_path, monkeypatch):
    """Totals, highlights, an aggregated rule input, display units and a dynamic title,
    computed by the local engine rather than a fake one."""
    import pandas as pd

    from ai_powerbi_analyst.studio.dax_engine import Model
    from ai_powerbi_analyst.studio.report_engine import LocalReportEngine

    monkeypatch.setattr(lr, "model_formats", lambda *_: {("Orders", "Sales"): "#,0"})
    frame = pd.DataFrame(
        {
            "Region": ["East", "East", "West", "West", "North"],
            "Segment": ["Consumer", "Corporate", "Consumer", "Consumer", "Corporate"],
            "Amount": [1000.0, 3000.0, 2500.0, 500.0, 6000.0],
        }
    )
    model = Model.from_frames(
        {"Orders": frame},
        measures={
            "Sales": "SUM(Orders[Amount])",
            "Profit": "SUM(Orders[Amount]) / 10",
            "Title": '"Sales by region"',
        },
        measure_table="Orders",
    )
    table = visual(
        "t",
        "tableEx",
        {"Values": [col("Region"), mea("Sales")]},
        objects={
            "columnFormatting": [
                entry(
                    {"labelDisplayUnits": lit("1000D"), "labelPrecision": lit("1L")},
                    metadata="Orders.Sales",
                )
            ],
            "values": [
                entry(
                    {
                        "backColor": gradient(
                            "#000000",
                            "#FFFFFF",
                            input_node={
                                "Aggregation": {"Expression": col("Amount"), "Function": 0}
                            },
                        )
                    },
                    data=[WILDCARD],
                    metadata="Orders.Sales",
                )
            ],
        },
    )
    source = visual("s", "clusteredColumnChart", {"Category": [col("Region")], "Y": [mea("Sales")]})
    target = visual(
        "c",
        "clusteredColumnChart",
        {"Category": [col("Segment")], "Y": [mea("Sales")]},
        container={"title": [entry({"text": {"expr": mea("Title")}})]},
    )
    report = one_page(tmp_path, table, source, target)
    request = LiveReportRequest(
        selections=[VisualSelection(visual_id="s", values=["East"])], highlight=True
    )
    payload = render_page(LocalReportEngine(model), report, "one", request)
    rows = by_id(payload)
    table_row = rows["t"]
    assert "error" not in table_row
    # A table is filtered by a chart selection. With one row the lowest and highest value
    # are the same, which takes the scale's first color.
    assert table_row["rows"] == [["East", 4000.0, 4000.0, "#000000"]]
    assert table_row["formatted_rows"][0][1] == "4.0K"
    assert table_row["totals"] == {
        "values": [None, 4000.0, 4000.0, None],
        "formatted": [None, "4.0K", None, None],
    }
    # The other chart keeps all its bars and highlights East's part of each. The title's
    # measure is read for the whole visual (its cell in the grouped rows stays blank).
    chart = rows["c"]
    assert chart["rows"] == [["Consumer", 4000.0, None], ["Corporate", 9000.0, None]]
    assert chart["highlights"] == [["Consumer", 1000.0, None], ["Corporate", 3000.0, None]]
    assert chart["dynamic"] == {"title.text": "Sales by region"}
    assert chart["fidelity"] == "exact"
    # The chart the selection was made in highlights its own bar.
    assert rows["s"]["highlights"] == [
        ["East", 4000.0],
        ["North", None],
        ["West", None],
    ]


def test_a_tooltip_page_drilled_on_a_date_runs_on_studios_dax_engine(tmp_path):
    import pandas as pd

    from ai_powerbi_analyst.studio.dax_engine import Model
    from ai_powerbi_analyst.studio.report_engine import LocalReportEngine

    frame = pd.DataFrame(
        {
            "Day": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-02"]),
            "Amount": [1.0, 2.0, 5.0],
        }
    )
    model = Model.from_frames(
        {"Orders": frame}, measures={"Sales": "SUM(Orders[Amount])"}, measure_table="Orders"
    )
    card = visual("k", "cardVisual", {"Data": [mea("Sales")]})
    report = make_report(tmp_path, {"tip": {"visuals": [card], "page": {"type": "Tooltip"}}})
    report = report.model_copy(
        update={
            "tables": [
                {"TableName": "Orders", "ColumnName": "Day", "PandasDataType": "datetime64[ns]"},
                {"TableName": "Orders", "ColumnName": "Amount", "PandasDataType": "float64"},
            ]
        }
    )
    hovered = LiveReportRequest.model_validate(
        {"drill": {"field": "Orders.Day", "values": ["2026-01-02T00:00:00"]}}
    )
    rows = by_id(render_page(LocalReportEngine(model), report, "tip", hovered))
    assert rows["k"]["rows"] == [[7.0]]
    with pytest.raises(lr.Unsupported, match="time of day"):
        compile_page(
            report,
            "tip",
            LiveReportRequest.model_validate(
                {"drill": {"field": "Orders.Day", "values": ["2026-01-02T10:30:00"]}}
            ),
        )


def test_a_visual_reading_what_the_local_model_lacks_is_left_to_power_bi(tmp_path):
    import pandas as pd

    from ai_powerbi_analyst.studio.dax_engine import Model
    from ai_powerbi_analyst.studio.report_engine import LocalReportEngine

    frame = pd.DataFrame({"Region": ["East", "West"], "Amount": [1.0, 2.0]})
    model = Model.from_frames(
        {"Orders": frame},
        measures={"Sales": "SUM(Orders[Amount])", "Profit": "SUM(Orders[Amount]) / 10"},
        measure_table="Orders",
    )
    why = "Power BI reports a problem in the measure 'Profit'; Power BI shows it."
    model.unavailable_measures = {"profit": why}  # type: ignore[attr-defined]
    good = visual(
        "good", "clusteredColumnChart", {"Category": [col("Region")], "Y": [mea("Sales")]}
    )
    bad = visual("bad", "clusteredColumnChart", {"Category": [col("Region")], "Y": [mea("Profit")]})
    titled = visual(
        "titled",
        "clusteredColumnChart",
        {"Category": [col("Region")], "Y": [mea("Sales")]},
        container={"title": [entry({"text": {"expr": mea("Profit")}})]},
    )
    report = one_page(tmp_path, good, bad, titled)
    rows = by_id(render_page(LocalReportEngine(model), report, "one", LiveReportRequest()))
    # The rest of the page still renders locally.
    assert rows["good"]["rows"] == [["East", 1.0], ["West", 2.0]]
    assert rows["good"]["fidelity"] == "exact"
    assert rows["bad"]["error"] == why and rows["bad"]["needs_desktop"] is True
    assert rows["bad"]["declined"] is True and rows["bad"]["fidelity"] == "none"
    # A title whose measure the model lacks costs only the title.
    assert rows["titled"]["rows"] == [["East", 1.0, None], ["West", 2.0, None]]
    assert "dynamic" not in rows["titled"] and rows["titled"]["unrendered"] == ["title.text"]
    assert rows["titled"]["fidelity"] == "approximate"
    # "declined" is data: a formatting-only row does not carry it.
    assert "declined" not in render_formatting(report, "one", LiveReportRequest())["bad"]


# --- the reviewers' repros -------------------------------------------------------------------


def time_calc_slicer(values: list[str] | None) -> dict[str, Any]:
    objects = None
    if values:
        condition = {
            "In": {
                "Expressions": [col("Item", "Time Calc")],
                "Values": [[raw(f"'{v}'")] for v in values],
            }
        }
        where = {"Where": [{"Condition": condition}]}
        objects = {"general": [entry({"filter": {"filter": where}})]}
    return visual("s", "slicer", {"Values": [col("Item", "Time Calc")]}, objects=objects)


def with_time_calc(report: ImportedReport) -> ImportedReport:
    return report.model_copy(
        update={
            "tables": [*report.tables, {"TableName": "Time Calc", "ColumnName": "Item"}],
            "model": {"tables": [], "columns": [], "calculationGroups": [{"table": "Time Calc"}]},
        }
    )


TIME_CALC = (
    "The slicer on the calculation group 'Time Calc' changes the measures of this page, "
    "which Power BI applies"
)


def test_a_calculation_group_slicers_choice_sends_the_measures_it_reaches_to_power_bi(tmp_path):
    chart = visual("p", "clusteredColumnChart", {"Category": [col("Region")], "Y": [mea("Sales")]})
    regions = visual("r", "tableEx", {"Values": [col("Region")]}, objects=NO_TOTALS)
    apart = visual("a", "card", {"Values": [mea("Sales")]})
    page = {"visualInteractions": [{"source": "s", "target": "a", "type": "NoFilter"}]}
    slicer = time_calc_slicer(["YTD"])
    report = with_time_calc(one_page(tmp_path, chart, regions, apart, slicer, page=page))
    visuals, query = compile_page(report, "one", LiveReportRequest())
    rows = {v["id"]: v for v in visuals}
    assert rows["p"]["error"] == TIME_CALC and rows["p"]["needs_desktop"] is True
    # A visual with no measure, and one the slicer does not reach, are drawn by Studio.
    assert "error" not in rows["r"] and "error" not in rows["a"]
    assert "Time Calc" not in query
    # With nothing selected the measures are the plain ones...
    plain = with_time_calc(one_page(tmp_path, chart, time_calc_slicer(None), name="b.pbix"))
    assert (
        "error" not in {v["id"]: v for v in compile_page(plain, "one", LiveReportRequest())[0]}["p"]
    )
    # ...until the user picks an item.
    chosen = LiveReportRequest(selections=[VisualSelection(visual_id="s", values=["YTD"])])
    assert {v["id"]: v for v in compile_page(plain, "one", chosen)[0]}["p"]["error"] == TIME_CALC


def test_while_the_model_is_unread_tables_that_may_be_power_bi_only_are_left_to_it(tmp_path):
    calc_chart = visual(
        "c", "clusteredColumnChart", {"Category": [col("Item", "Time Calc")], "Y": [mea("Sales")]}
    )
    param_chart = visual(
        "f", "clusteredColumnChart", {"Category": [col("Metric", "Metric")], "Y": [mea("Sales")]}
    )
    plain = visual("p", "clusteredColumnChart", {"Category": [col("Region")], "Y": [mea("Sales")]})
    report = one_page(tmp_path, calc_chart, param_chart, plain)
    tables = [
        *report.tables,
        {"TableName": "Time Calc", "ColumnName": "Item"},
        {"TableName": "Time Calc", "ColumnName": "Ordinal"},
        {"TableName": "Metric", "ColumnName": "Metric"},
        {"TableName": "Metric", "ColumnName": "Metric Fields"},
        {"TableName": "Metric", "ColumnName": "Metric Order"},
    ]

    def errors(**update: Any) -> dict[str, Any]:
        found = report.model_copy(update={"tables": tables, **update})
        return {v["id"]: v.get("error") for v in compile_page(found, "one", LiveReportRequest())[0]}

    unread = {
        "c": "Studio has not read this report's model, and 'Time Calc' may be a calculation "
        "group, which Power BI applies",
        "f": "Studio has not read this report's model, and 'Metric' may be a field parameter, "
        "which Power BI switches",
        "p": None,
    }
    # Nothing known: tables shaped like Desktop's calculation groups and field parameters.
    assert errors(model={"version": 1}) == unread
    # The model of the imported rows says which tables are.
    parameter = "{(\"Sales\", NAMEOF('Orders'[Sales]), 0)}"
    digest = {
        "tables": [
            {"name": "Orders", "source": "entity"},
            {"name": "Time Calc", "source": "calculationGroup"},
            {"name": "Metric", "source": "calculated", "expression": parameter},
        ]
    }
    assert errors(model=None, snapshot_model=digest) == unread
    ordinary = {"tables": [{"name": n} for n in ("Orders", "Time Calc", "Metric")]}
    assert errors(model=None, snapshot_model=ordinary) == {"c": None, "f": None, "p": None}
    # Once read, the facts decide, and the visuals' data key moves with them.
    assert errors(model={"tables": [], "columns": []}) == {"c": None, "f": None, "p": None}
    read = report.model_copy(update={"tables": tables, "model": {"tables": [], "columns": []}})
    unread_report = report.model_copy(update={"tables": tables, "model": {"version": 1}})
    target = report.pages[0].visuals[0]
    assert data_key(read, target, LiveReportRequest()) != data_key(
        unread_report, target, LiveReportRequest()
    )


def drill_chart(name: str, active: dict[int, bool], visual_type: str = "clusteredColumnChart"):
    projections: list[dict[str, Any]] = [
        {"field": col("Region"), "queryRef": "Orders.Region"},
        {"field": col("Segment"), "queryRef": "Orders.Segment"},
    ]
    for index, flag in active.items():
        projections[index]["active"] = flag
    role = "Values" if visual_type == "tableEx" else "Category"
    roles = {role: projections} if role == "Values" else {role: projections, "Y": [mea("Sales")]}
    return visual(name, visual_type, roles, objects=NO_TOTALS if role == "Values" else None)


@pytest.mark.parametrize(
    ("active", "shown"),
    [
        ({}, ["Region"]),  # no flag: the top level
        ({0: False, 1: True}, ["Segment"]),  # drilled down one level
        ({0: True, 1: True}, ["Region", "Segment"]),  # expanded to the next level
        ({0: False, 1: False}, ["Region"]),
    ],
)
def test_a_chart_queries_the_drill_level_its_axis_shows(tmp_path, active, shown):
    report = one_page(tmp_path, drill_chart("c", active))
    visuals, query = compile_page(report, "one", LiveReportRequest())
    assert [c["name"] for c in visuals[0]["columns"] if c["kind"] == "column"] == shown
    assert ("'Orders'[Segment]" in query) is ("Segment" in shown)
    assert ("'Orders'[Region]" in query) is ("Region" in shown)


def test_a_table_is_no_drill_hierarchy(tmp_path):
    report = one_page(tmp_path, drill_chart("t", {0: False, 1: True}, "tableEx"))
    columns = render_formatting(report, "one", LiveReportRequest())["t"]["columns"]
    assert [c["name"] for c in columns] == ["Region", "Segment"]


def test_percent_of_total_divides_by_the_measure_at_the_grand_total(tmp_path):
    table = visual(
        "t",
        "tableEx",
        {
            "Values": [
                col("Region"),
                mea("Sales"),
                calc("Share", "DIVIDE([Sales], COLLAPSEALL([Sales], ROWS))"),
                calc("Gap", "[Sales] - COLLAPSEALL([Sales], ROWS)"),
            ]
        },
        objects=NO_TOTALS,
    )
    report = one_page(tmp_path, table)
    # Sales is an average: at the grand total it is 4, not the rows' sum (8).
    engine = Engine([row("t", "A", 6), row("t", "B", 2), row("t~total", None, 4)])
    found = by_id(render_page(engine, report, "one", LiveReportRequest()))["t"]
    assert found["rows"] == [["A", 6.0, 1.5, 2.0], ["B", 2.0, 0.5, -2.0]]
    assert found["fidelity"] == "exact"
    # Without the grand total nothing is invented.
    failing = Engine([row("t", "A", 6), row("t", "B", 2)], fail=("t~total",))
    found = by_id(render_page(failing, report, "one", LiveReportRequest()))["t"]
    assert found["rows"] == [["A", 6.0, None, None], ["B", 2.0, None, None]]
    assert found["fidelity"] == "approximate"
    assert found["dataNotes"] == [
        "The totals, highlights or whole-visual values of this visual could not be computed here"
    ]


def test_percent_of_parent_is_the_grand_total_with_one_level_and_approximate_with_two(tmp_path):
    share = calc("Share", "DIVIDE([Sales], COLLAPSE([Sales], ROWS))")
    one = visual(
        "t", "tableEx", {"Values": [col("Region"), mea("Sales"), share]}, objects=NO_TOTALS
    )
    two = visual(
        "t",
        "tableEx",
        {"Values": [col("Region"), col("Segment"), mea("Sales"), share]},
        objects=NO_TOTALS,
    )
    single = one_page(tmp_path, one)
    engine = Engine([row("t", "A", 6), row("t", "B", 2), row("t~total", None, 4)])
    found = by_id(render_page(engine, single, "one", LiveReportRequest()))["t"]
    assert [r[2] for r in found["rows"]] == [1.5, 0.5] and found["fidelity"] == "exact"
    # Two levels: the parent's value is summed from the rows, right only for a measure that
    # adds up.
    double = one_page(tmp_path, two, name="b.pbix")
    assert render_formatting(double, "one", LiveReportRequest())["t"]["fidelity"] == "approximate"


def test_values_computed_over_more_rows_than_studio_reads_are_approximate(tmp_path):
    scaled = visual(
        "t",
        "tableEx",
        {"Values": [col("Region"), mea("Sales")]},
        objects={
            **NO_TOTALS,
            "values": [
                entry(
                    {"backColor": gradient("#000000", "#FFFFFF")},
                    data=[WILDCARD],
                    metadata="Orders.Sales",
                )
            ],
        },
    )
    plain = visual("p", "tableEx", {"Values": [col("Region"), mea("Sales")]}, objects=NO_TOTALS)
    rows = [row(v, f"R{i:03}", i) for v in ("t", "p") for i in range(101)]
    report = one_page(tmp_path, scaled, plain)
    formatting = render_formatting(report, "one", LiveReportRequest())
    assert formatting["t"]["formatting"]["rangeDependent"] is True
    assert formatting["p"]["formatting"]["rangeDependent"] is False
    found = by_id(render_page(Engine(rows), report, "one", LiveReportRequest()))
    assert found["t"]["truncated"] is True and len(found["t"]["rows"]) == 100
    assert found["t"]["dataNotes"] == [
        "Only the first 100 rows were used for the values computed here"
    ]
    assert found["t"]["fidelity"] == "approximate"
    # A plain table shows its first 100 rows exactly.
    assert found["p"]["truncated"] is True and found["p"]["fidelity"] == "exact"
    assert "dataNotes" not in found["p"]


def test_a_column_the_model_sorts_by_another_is_ordered_by_it(tmp_path):
    chart = visual("c", "clusteredColumnChart", {"Category": [col("Month")], "Y": [mea("Sales")]})
    table = visual(
        "t",
        "tableEx",
        {"Values": [col("Month"), mea("Sales"), calc("Running", "RUNNINGSUM([Sales])")]},
        objects=NO_TOTALS,
    )
    report = one_page(tmp_path, chart, table)
    report = report.model_copy(
        update={
            "tables": [
                *report.tables,
                {"TableName": "Orders", "ColumnName": "Month"},
                {"TableName": "Orders", "ColumnName": "MonthNumber"},
            ],
            "model": {
                "tables": [],
                "columns": [{"table": "Orders", "name": "Month", "sortByColumn": "MonthNumber"}],
            },
        }
    )
    engine = Engine(
        [
            *(row("c", m, s, n) for m, s, n in (("Feb", 5, 2), ("Jan", 3, 1), ("Mar", 1, 3))),
            *(row("t", m, s, n) for m, s, n in (("Mar", 1, 3), ("Jan", 3, 1), ("Feb", 5, 2))),
        ]
    )
    found = by_id(render_page(engine, report, "one", LiveReportRequest()))
    helper = found["c"]["columns"][2]
    assert (helper["name"], helper["role"], helper["hidden"]) == ("MonthNumber", "SortBy", True)
    assert found["c"]["sort"] == [{"index": 2, "descending": False}]
    assert [r[0] for r in found["c"]["rows"]] == ["Jan", "Feb", "Mar"]
    assert "'Orders'[MonthNumber], ASC" in engine.queries[0][0]
    # The running sum follows the months' order, not the names'.
    assert [(r[0], r[2]) for r in found["t"]["rows"]] == [
        ("Jan", 3.0),
        ("Feb", 8.0),
        ("Mar", 9.0),
    ]


@pytest.mark.parametrize(("option", "on_rows"), [(1, True), (0, True), (2, False)])
def test_formatting_that_reaches_the_totals_row_is_listed_as_not_drawn(tmp_path, option, on_rows):
    selector = [{"dataViewWildcard": {"matchingOption": option}}]
    table = visual(
        "t",
        "tableEx",
        {"Values": [col("Region"), mea("Sales")]},
        objects={
            "values": [
                entry(
                    {"backColor": gradient("#000000", "#FFFFFF")},
                    data=selector,
                    metadata="Orders.Sales",
                )
            ]
        },
    )
    found = render_formatting(one_page(tmp_path, table), "one", LiveReportRequest())["t"]
    assert (found["columns"][1].get("backgroundIndex") is not None) is on_rows
    assert ("values.backColor" in found["unrendered"]) is (option != 1)
    assert found["fidelity"] == ("exact" if option == 1 else "approximate")


@pytest.mark.parametrize(("option", "on_rows"), [(0, True), (2, False)])
def test_data_bars_on_the_totals_row_are_listed_as_not_drawn(tmp_path, option, on_rows):
    bars = {"positiveColor": solid("#00FF00"), "totalMatchingOption": lit(f"{option}L")}
    table = visual(
        "t",
        "tableEx",
        {"Values": [col("Region"), mea("Sales")]},
        objects={"columnFormatting": [entry({"dataBars": bars}, metadata="Orders.Sales")]},
    )
    found = render_formatting(one_page(tmp_path, table), "one", LiveReportRequest())["t"]
    assert ("dataBars" in found["columns"][1]) is on_rows
    assert found["unrendered"] == ["columnFormatting.dataBars"]


def test_a_trend_line_per_measure_when_series_are_not_combined(tmp_path):
    def chart(combine: bool, legend: bool = False) -> dict[str, Any]:
        roles = {"Category": [col("Region")], "Y": [mea("Sales"), mea("Profit")]}
        if legend:
            roles = {"Category": [col("Region")], "Series": [col("Segment")], "Y": [mea("Sales")]}
        trend = {"show": lit("true"), "combineSeries": lit(str(combine).lower())}
        return visual("c", "clusteredColumnChart", roles, objects={"trend": [entry(trend)]})

    rows = [row("c", "A", 1, 10), row("c", "B", 2, 20), row("c", "C", 3, 30)]
    report = one_page(tmp_path, chart(False))
    found = by_id(render_page(Engine(rows), report, "one", LiveReportRequest()))["c"]
    assert [(line["series"], line["start"], line["end"]) for line in found["referenceLines"]] == [
        ("Orders.Sales", 1.0, 3.0),
        ("Orders.Profit", 10.0, 30.0),
    ]
    assert found["fidelity"] == "exact"
    combined = one_page(tmp_path, chart(True), name="b.pbix")
    lines = by_id(render_page(Engine(rows), combined, "one", LiveReportRequest()))["c"][
        "referenceLines"
    ]
    assert len(lines) == 1 and lines[0]["series"] is None
    # One line per legend series is Power BI's to draw.
    split = render_formatting(
        one_page(tmp_path, chart(False, legend=True), name="c.pbix"), "one", LiveReportRequest()
    )["c"]
    assert split["unrendered"] == ["trend.combineSeries"] and split["fidelity"] == "approximate"


def test_an_exclusive_percentile_line_uses_percentile_exc(tmp_path):
    def chart(exclusive: bool) -> dict[str, Any]:
        percentile: dict[str, Any] = {"Expression": select_ref("Orders.Sales"), "K": 0.25}
        if exclusive:
            percentile["Exclusive"] = True
        line = {"show": lit("true"), "value": {"expr": {"Percentile": percentile}}}
        return visual(
            "c",
            "clusteredColumnChart",
            {"Category": [col("Region")], "Y": [mea("Sales")]},
            objects={"referenceLine": [entry(line, id="0")]},
        )

    rows = [row("c", k, v) for k, v in (("A", 1), ("B", 2), ("C", 3), ("D", 4))]
    values = []
    for exclusive, name in ((False, "a.pbix"), (True, "b.pbix")):
        report = one_page(tmp_path, chart(exclusive), name=name)
        found = by_id(render_page(Engine(rows), report, "one", LiveReportRequest()))["c"]
        values.append(found["referenceLines"][0]["value"])
    assert values == [1.75, 1.25]  # PERCENTILE.INC and PERCENTILE.EXC of 1..4 at 25%
    # Below the first rank PERCENTILE.EXC has no value.
    assert percentile_exclusive([1.0, 2.0], 10) is None


def themed(tmp_path: Path, colors: list[str], fill: dict[str, Any], name: str) -> ImportedReport:
    report_json = {
        "themeCollection": {"customTheme": {"name": "Mine.json", "type": "RegisteredResources"}},
        "resourcePackages": [
            {
                "name": "RegisteredResources",
                "type": "RegisteredResources",
                "items": [{"name": "Mine.json", "path": "Mine.json", "type": "CustomTheme"}],
            }
        ],
    }
    table = visual(
        "t",
        "tableEx",
        {"Values": [col("Region"), mea("Sales")]},
        objects={
            **NO_TOTALS,
            "values": [entry({"fontColor": fill}, data=[WILDCARD], metadata="Orders.Sales")],
        },
    )
    parts = {"Report/StaticResources/RegisteredResources/Mine.json": {"dataColors": colors}}
    return one_page(tmp_path, table, report=report_json, parts=parts, name=name)


def test_a_rule_with_a_theme_color_moves_the_data_key_with_the_theme(tmp_path):
    def rules(value: dict[str, Any]) -> dict[str, Any]:
        return rule(
            {"Conditional": {"Cases": [{"Condition": cmp(2, mea("Sales"), "0D"), "Value": value}]}}
        )

    theme_color = rules({"ThemeDataColor": {"ColorId": 2, "Percent": 0}})
    literal = rules(raw("'#FF0000'"))

    def key(fill: dict[str, Any], color: str, name: str) -> str:
        report = themed(tmp_path, [color], fill, name)
        return data_key(report, report.pages[0].visuals[0], LiveReportRequest())

    assert key(theme_color, "#111111", "a.pbix") != key(theme_color, "#222222", "b.pbix")
    assert key(literal, "#111111", "c.pbix") == key(literal, "#222222", "d.pbix")
    report = themed(tmp_path, ["#111111"], theme_color, "e.pbix")
    found = by_id(render_page(Engine([row("t", "A", 1)]), report, "one", LiveReportRequest()))
    assert found["t"]["rows"] == [["A", 1.0, "#111111"]]


def test_a_bookmark_notes_display_modes_and_only_the_states_it_changes(tmp_path):
    sales = mea("Sales")
    chart = visual("c", "clusteredColumnChart", {"Category": [col("Region")], "Y": [sales]})
    chart["visual"]["query"]["sortDefinition"] = {
        "sort": [{"field": sales, "direction": "Descending"}]
    }
    other = visual("d", "clusteredColumnChart", {"Category": [col("Region")], "Y": [sales]})
    third = visual("e", "textbox")

    def single(mode: str | None = None, **state: Any) -> dict[str, Any]:
        found: dict[str, Any] = {"visualType": "clusteredColumnChart", **state}
        if mode:
            found["display"] = {"mode": mode}
        return {"singleVisual": found}

    same_sort = [{"Direction": 2, "Expression": sales}]
    parts = {
        "Report/definition/bookmarks/same.bookmark.json": bookmark_part(
            "same", "one", {"c": single(orderBy=same_sort), "d": single(orderBy=[])}
        ),
        "Report/definition/bookmarks/modes.bookmark.json": bookmark_part(
            "modes",
            "one",
            {"c": single("spotlight"), "d": single("maximize"), "e": single("elevation")},
        ),
        "Report/definition/bookmarks/resorted.bookmark.json": bookmark_part(
            "resorted", "one", {"c": single(orderBy=[{"Direction": 1, "Expression": sales}])}
        ),
    }
    report = make_report(tmp_path, {"one": {"visuals": [chart, other, third]}}, parts=parts)

    def notes(name: str) -> list[str]:
        payload = render_formatting_payload(report, "one", LiveReportRequest(bookmark=name))
        return list(payload["bookmark"]["notes"])

    # A bookmark that saved the sort the visuals have now needs no note.
    assert notes("same") == []
    assert notes("modes") == [
        "The spotlight this bookmark saved is shown in Power BI only",
        "The focus mode this bookmark saved is shown in Power BI only",
        "The layer order this bookmark saved is shown in Power BI only",
    ]
    assert notes("resorted") == [
        "The sort, drill level or field choices this bookmark saved are shown in Power BI only"
    ]


def test_the_envelope_lists_the_reports_drillthrough_pages(tmp_path):
    report = make_report(
        tmp_path,
        {
            "one": {},
            "drill": {"page": drill_page(acceptsFilterContext="None")},
            "shared": {"page": drill_page(referenceScope="CrossReport")},
            "tip": {"page": {"type": "Tooltip"}},
        },
    )
    payload = render_formatting_payload(report, "one", LiveReportRequest())
    field = [{"field": "Orders.Region", "table": "Orders", "column": "Region"}]
    assert payload["drillthroughs"] == [
        {
            "page": "drill",
            "title": "Drill",
            "fields": field,
            "keepAllFilters": False,
            "crossReport": False,
        },
        {
            "page": "shared",
            "title": "Shared",
            "fields": field,
            "keepAllFilters": True,
            "crossReport": True,
        },
    ]
    assert payload["notes"] == []


def test_every_column_carries_the_same_keys(tmp_path):
    table = visual(
        "t",
        "tableEx",
        {
            "Values": [
                col("Region"),
                mea("Sales"),
                calc("Running", "RUNNINGSUM([Sales])"),
            ]
        },
        objects={
            **NO_TOTALS,
            "values": [
                entry(
                    {
                        "fontColor": rule(mea("Color")),
                        "backColor": gradient("#000000", "#FFFFFF"),
                    },
                    data=[WILDCARD],
                    metadata="Orders.Sales",
                )
            ],
        },
    )
    columns = render_formatting(one_page(tmp_path, table), "one", LiveReportRequest())["t"][
        "columns"
    ]
    kinds = {c["name"]: c["kind"] for c in columns}
    assert kinds["Color"] == "measure" and "computed" in kinds.values()
    expected = set(lr._COLUMN_DEFAULTS)
    assert all(expected <= set(c) for c in columns)
    helper = next(c for c in columns if c["name"] == "Color")
    assert helper["hidden"] is True and helper["query_ref"] is None


def test_the_shapes_no_desktop_file_shows_are_listed():
    from ai_powerbi_analyst.studio import (
        render_analytics,
        render_navigation,
        render_rules,
        report_tokens,
    )

    expected = {
        render_rules: {
            "dataPoint.fill rule selector",
            "cardVisual value/label/fillCustom rule selector",
            "card labels.color rule",
            "columnFormatting.dataBars.totalMatchingOption",
        },
        report_tokens: {
            "projection.format",
            "labels.valueCustomFormatString",
            "columnFormatting",
            "navigator states",
        },
        render_navigation: {
            "syncGroup",
            "visualInteractions",
            "settings.defaultFilterActionIsDataFilter",
            "bookmark display modes",
            "bookmark objects.merge",
        },
        render_analytics: {"trend", "Percentile.Exclusive"},
        lr: {"projection.active", "sortByColumn"},
    }
    for module, names in expected.items():
        shapes = module.UNVERIFIED_SHAPES
        assert names <= set(shapes), module.__name__
        assert all(isinstance(text, str) and text.strip() for text in shapes.values())


def test_a_bookmark_applies_a_locked_filter_it_saved_by_name(tmp_path):
    """The navigation family's add_bookmark saves a filter locked in view mode under byName,
    as Desktop's capture does: applying the bookmark brings its saved value back."""
    from ai_powerbi_analyst.studio import report_edit_navigation as navigation
    from ai_powerbi_analyst.studio.pbir_document import PbirDocument
    from ai_powerbi_analyst.studio.pbix_package import PbixPackage
    from ai_powerbi_analyst.studio.report_edit_context import ModelInfo

    chart = visual("c", "clusteredColumnChart", {"Category": [REGION], "Y": [mea("Sales")]})
    locked = {
        "name": "F1",
        "field": REGION,
        "type": "Categorical",
        "filter": WEST,
        "isLockedInViewMode": True,
    }
    page = {"filterConfig": {"filters": [locked]}}
    report = make_report(tmp_path, {"one": {"visuals": [chart], "page": page}}, report={})
    document = PbirDocument(PbixPackage(report.archived_path))
    model = ModelInfo(columns={"Orders": {"Region": "object"}}, measures={"Sales": "Orders"})
    operation = {"op": "add_bookmark", "name": "West", "page": "one", "data": True}
    navigation.apply_navigation(document, navigation.parse_navigation([operation]), model)
    # The page's filter moves on after the bookmark was taken.
    edited = document.page("one")
    east = json.loads(json.dumps(WEST).replace("'West'", "'East'"))
    edited.data["filterConfig"]["filters"][0]["filter"] = east
    document.touch_page(edited)
    document.commit()
    path = tmp_path / "bookmarked.pbix"
    document.package.write(path)
    pages, _, _ = read_report(path)
    report = report.model_copy(
        update={
            "source_path": str(path),
            "archived_path": str(path),
            "source_hash": sha256_file(path),
            "pages": pages,
        }
    )
    with zipfile.ZipFile(path) as archive:
        saved = lr.nav.read_bookmarks(archive)[0]
    assert "byName" in saved.section("one")["filters"]
    _, now = compile_page(report, "one", LiveReportRequest())
    assert '"East"' in now and '"West"' not in now
    request = LiveReportRequest(bookmark="West")
    visuals, then = compile_page(report, "one", request)
    assert '"West"' in then and '"East"' not in then
    assert visuals[0]["fidelity"] == "exact"
    payload = render_formatting_payload(report, "one", request)
    assert payload["bookmark"]["notes"] == []
    # The bookmark's filter state decides the rows: it is in the data key.
    chart_now = report.pages[0].visuals[0]
    assert data_key(report, chart_now, request) != data_key(report, chart_now, LiveReportRequest())


def test_a_bookmark_filter_state_studio_cannot_apply_is_noted(tmp_path):
    chart = visual("c", "clusteredColumnChart", {"Category": [REGION], "Y": [mea("Sales")]})
    text = visual("x", "textbox")
    page = {"filterConfig": {"filters": [{"name": "F1", "field": REGION, "filter": WEST}]}}
    # An include filter's state with no name and no field: nothing says which filter it is.
    section = {"filters": {"byType": [{"type": "Categorical", "howCreated": 3}]}}
    parts = {
        "Report/definition/bookmarks/b.bookmark.json": bookmark_part(
            "b", "one", {}, section=section
        )
    }
    report = make_report(tmp_path, {"one": {"visuals": [chart, text], "page": page}}, parts=parts)
    payload = render_formatting_payload(report, "one", LiveReportRequest(bookmark="b"))
    assert payload["bookmark"]["notes"] == [lr.BOOKMARK_FILTERS_NOTE]
    assert payload["visuals"]["c"]["fidelity"] == "approximate"
    assert payload["visuals"]["x"]["fidelity"] == "exact"  # no data: nothing to filter
    # A state with no name is matched by its field when exactly one filter is on it.
    by_field = {"filters": {"byExpr": [{"expression": REGION, "filter": None}]}}
    parts = {
        "Report/definition/bookmarks/b.bookmark.json": bookmark_part(
            "b", "one", {}, section=by_field
        )
    }
    report = make_report(
        tmp_path, {"one": {"visuals": [chart], "page": page}}, parts=parts, name="b.pbix"
    )
    visuals, query = compile_page(report, "one", LiveReportRequest(bookmark="b"))
    assert "West" not in query and visuals[0]["fidelity"] == "exact"
