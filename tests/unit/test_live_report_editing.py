"""The page payload the canvas draws while a report is being edited.

Covers what the render contract (docs/imported-report-render-contract.md) promises: stacking
rank, hidden and grouped visuals, framed Power BI only visuals, per-visual filter tolerance,
the fidelity signal, the revision echo, the formatting-only render and the data key.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.integrity import sha256_file
from ai_powerbi_analyst.studio import live_report as lr
from ai_powerbi_analyst.studio.live_report import (
    LiveReportRequest,
    Unsupported,
    VisualSelection,
    compile_page,
    data_key,
    render_formatting,
    render_formatting_payload,
    render_page,
)
from ai_powerbi_analyst.studio.report_import import ImportedReport, read_report

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "imported"


def lit(value: str) -> dict[str, Any]:
    return {"expr": {"Literal": {"Value": value}}}


def solid(value: str) -> dict[str, Any]:
    return {"solid": {"color": lit(f"'{value}'")}}


def col(name: str, entity: str = "Orders") -> dict[str, Any]:
    return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": name}}


def mea(name: str, entity: str = "Orders") -> dict[str, Any]:
    return {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": name}}


def visual(
    name: str,
    visual_type: str,
    roles: dict[str, list[dict[str, Any]]] | None = None,
    *,
    x: float = 0,
    y: float = 0,
    width: float = 300,
    height: float = 200,
    z: int = 0,
    tab: int | None = None,
    objects: dict[str, Any] | None = None,
    container: dict[str, Any] | None = None,
    hidden: bool = False,
    parent: str | None = None,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    position: dict[str, Any] = {"x": x, "y": y, "width": width, "height": height, "z": z}
    if tab is not None:
        position["tabOrder"] = tab
    body: dict[str, Any] = {"visualType": visual_type}
    if roles:
        body["query"] = {
            "queryState": {
                role: {
                    "projections": [
                        {"field": f, "queryRef": f"Orders.{next(iter(f.values()))['Property']}"}
                        for f in fields
                    ]
                }
                for role, fields in roles.items()
            }
        }
    if objects:
        body["objects"] = objects
    if container:
        body["visualContainerObjects"] = container
    raw: dict[str, Any] = {"name": name, "position": position, "visual": body}
    if hidden:
        raw["isHidden"] = True
    if parent:
        raw["parentGroupName"] = parent
    if filters:
        raw["filterConfig"] = filters
    return raw


def group(name: str, **kwargs: Any) -> dict[str, Any]:
    raw = visual(name, "x", **kwargs)
    raw["visualGroup"] = {"displayName": name, "groupMode": "ScaleMode"}
    del raw["visual"]
    return raw


def entry(props: dict[str, Any], **selector: Any) -> dict[str, Any]:
    return {"properties": props, **({"selector": selector} if selector else {})}


def make_report(
    tmp_path: Path,
    visuals: list[dict[str, Any]],
    *,
    page: dict[str, Any] | None = None,
    report: dict[str, Any] | None = None,
    parts: dict[str, Any] | None = None,
    name: str = "original.pbix",
) -> ImportedReport:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Report/definition/pages/pages.json", json.dumps({"pageOrder": ["one"]}))
        z.writestr(
            "Report/definition/pages/one/page.json",
            json.dumps(
                {"name": "one", "displayName": "One", "width": 1280, "height": 720, **(page or {})}
            ),
        )
        if report is not None:
            z.writestr("Report/definition/report.json", json.dumps(report))
        for part, body in (parts or {}).items():
            z.writestr(part, json.dumps(body))
        for v in visuals:
            z.writestr(
                f"Report/definition/pages/one/visuals/{v['name']}/visual.json", json.dumps(v)
            )
        z.writestr("DataModel", b"model-bytes" * 20)
    pages, _, _ = read_report(path)
    return ImportedReport(
        id="report-test",
        workspace_id="workspace-test",
        name="Test",
        source_path=str(path),
        archived_path=str(path),
        source_hash=sha256_file(path),
        base_hash="a" * 64,
        format="PBIR",
        pages=pages,
        tables=[
            {"TableName": "Orders", "ColumnName": "Region"},
            {"TableName": "Orders", "ColumnName": "Segment"},
        ],
        measures=[
            {"TableName": "Orders", "Name": "Sales", "Expression": "1"},
            {"TableName": "Orders", "Name": "Profit", "Expression": "1"},
        ],
    )


@pytest.fixture(autouse=True)
def no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # The synthetic archives have no decodable model: number formats stay empty.
    monkeypatch.setattr(lr, "model_formats", lambda *_: {})


class ForbiddenEngine:
    """An engine that fails the test when asked a question."""

    label = "forbidden"

    def query(self, report: Any, query: str, max_rows: int) -> dict[str, Any]:
        raise AssertionError(f"the engine must not run: {query[:60]}")


class Engine:
    label = "fake engine"

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls = 0

    def query(self, report: Any, query: str, max_rows: int) -> dict[str, Any]:
        self.calls += 1
        return {"rows": self.rows}


CHART = visual(
    "chart",
    "clusteredColumnChart",
    {"Category": [col("Region")], "Y": [mea("Sales")]},
)


def by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {v["id"]: v for v in payload["visuals"]}


# --- filter tolerance (P26) -----------------------------------------------------------------


def topn_filter() -> dict[str, Any]:
    """A Top N page filter: its In expression has a Table subquery instead of Values."""
    return {
        "filters": [
            {
                "name": "Filter0123456789abcdef0123",
                "type": "TopN",
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
                                    "Table": {"SourceRef": {"Source": "subquery"}},
                                }
                            }
                        }
                    ],
                },
            }
        ]
    }


def test_unsupported_page_filter_marks_visuals_instead_of_failing_the_page(tmp_path):
    text = visual(
        "note",
        "textbox",
        objects={"general": [entry({"paragraphs": [{"textRuns": [{"value": "Hello"}]}]})]},
    )
    gauge = visual(
        "gauge", "gauge", {"Y": [mea("Sales")]}, container={"title": [entry({"text": lit("'G'")})]}
    )
    report = make_report(tmp_path, [CHART, text, gauge], page={"filterConfig": topn_filter()})
    payload = render_page(ForbiddenEngine(), report, "one", LiveReportRequest())
    rows = by_id(payload)
    assert rows["chart"]["error"] == "This page filter needs Power BI Desktop"
    assert rows["chart"]["needs_desktop"] is True and rows["chart"]["fidelity"] == "none"
    # The visual keeps its box, caption and formatting even though it cannot be drawn.
    assert rows["chart"]["position"] == {"x": 0, "y": 0, "width": 300, "height": 200}
    assert rows["chart"]["formatting"]["container"]["background"] == "#ffffffff"
    assert rows["chart"]["formatting"]["title"]["text"] == "Sales by Region"
    # Content that needs no query is unaffected.
    assert "error" not in rows["note"]
    assert rows["note"]["formatting"]["content"][0]["runs"][0]["text"] == "Hello"
    assert rows["gauge"]["unsupported_reason"] == "Gauge visuals are previewed in Power BI only"
    assert payload["engine"] == "forbidden"


def test_report_level_filter_failure_is_also_per_visual(tmp_path):
    report_json = {
        "filterConfig": {"filters": [{"filter": {"Where": [{"Condition": {"Unknown": {}}}]}}]}
    }
    report = make_report(tmp_path, [CHART], report=report_json)
    visuals, query = compile_page(report, "one", LiveReportRequest())
    assert visuals[0]["error"] == "This page filter needs Power BI Desktop" and query == ""


def test_unsupported_carries_the_needs_desktop_flag_and_stays_a_value_error():
    error = Unsupported("x")
    assert isinstance(error, ValueError) and error.needs_desktop is True
    assert Unsupported.needs_desktop is True


def test_a_supported_page_filter_still_filters_every_visual(tmp_path):
    saved = {
        "filters": [
            {
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
                                    "Values": [[{"Literal": {"Value": "'West'"}}]],
                                }
                            }
                        }
                    ],
                }
            }
        ]
    }
    report = make_report(tmp_path, [CHART], page={"filterConfig": saved})
    _, query = compile_page(report, "one", LiveReportRequest())
    assert "'Orders'[Region] IN {\"West\"}" in query


# --- stacking, hidden and groups (P17) -------------------------------------------------------


def test_visuals_are_ranked_by_z_then_tab_order_and_groups_cascade_hidden(tmp_path):
    visuals = [
        visual("a", "textbox", z=5000, tab=2),
        visual("b", "textbox", z=1000),
        visual("k", "textbox", z=2000, parent="g", x=10, y=20),
        visual("c", "textbox", z=5000, tab=1),
        group("g", z=0, hidden=True, x=0, y=0, width=200, height=100),
        visual("solo", "textbox", z=9000, hidden=True),
    ]
    report = make_report(tmp_path, visuals)
    payload = render_page(ForbiddenEngine(), report, "one", LiveReportRequest())
    order = [v["id"] for v in payload["visuals"]]
    assert order == ["g", "b", "k", "c", "a", "solo"]
    assert [v["rank"] for v in payload["visuals"]] == [1, 2, 3, 4, 5, 6]
    rows = by_id(payload)
    assert rows["k"]["z"] == 2000 and rows["c"]["z"] == 5000
    assert rows["g"]["kind"] == "group" and rows["g"]["container_kind"] == "group"
    assert "error" not in rows["g"] and rows["g"]["group_children"] == ["k"]
    assert rows["g"]["hidden"] is True and rows["g"]["hidden_own"] is True
    # A member of a hidden group is hidden even though its own flag is not set.
    assert rows["k"]["hidden"] is True and rows["k"]["hidden_own"] is False
    assert rows["k"]["parent_group"] == "g"
    assert rows["solo"]["hidden"] is True and rows["b"]["hidden"] is False
    assert rows["g"]["formatting"]["container"]["background"] is None  # no box of its own


def test_ranks_cover_the_whole_page_even_for_a_subset(tmp_path):
    report = make_report(
        tmp_path,
        [visual("a", "textbox", z=2), visual("b", "textbox", z=1), visual("c", "textbox", z=3)],
    )
    only_c = render_formatting(report, "one", LiveReportRequest(), {"c"})
    assert list(only_c) == ["c"] and only_c["c"]["rank"] == 3


def test_power_bi_only_visuals_keep_their_box_and_get_a_stable_reason(tmp_path):
    visuals = [
        visual(
            "kpi",
            "kpi",
            {"Y": [mea("Sales")]},
            x=5,
            y=6,
            width=120,
            height=80,
            container={
                "background": [entry({"color": solid("#EEEEEE")})],
                "title": [entry({"text": lit("'KPI'")})],
            },
            objects={"indicator": [entry({"fontSize": lit("30D")})]},
        ),
        visual("matrix", "pivotTable", {"Values": [mea("Sales")]}),
        visual("thing", "someCustomVisual123", {}),
    ]
    payload = render_page(
        ForbiddenEngine(), make_report(tmp_path, visuals), "one", LiveReportRequest()
    )
    rows = by_id(payload)
    kpi = rows["kpi"]
    assert kpi["kind"] == "unsupported" and kpi["fidelity"] == "none"
    assert kpi["unsupported_reason"] == "KPI visuals are previewed in Power BI only"
    assert kpi["error"] == kpi["unsupported_reason"]  # older clients only know "error"
    assert kpi["rows"] == [] and kpi["columns"] == []
    assert kpi["position"] == {"x": 5, "y": 6, "width": 120, "height": 80}
    assert kpi["formatting"]["title"]["text"] == "KPI"
    assert kpi["formatting"]["container"]["background"] == "#eeeeeeff"
    assert kpi["unrendered"] == ["indicator.fontSize"]
    assert rows["matrix"]["unsupported_reason"] == (
        "Matrix subtotals and hierarchy expansion are previewed in Power BI only"
    )
    assert rows["matrix"]["kind"] == "matrix"
    assert (
        rows["thing"]["unsupported_reason"]
        == "Custom visual visuals are previewed in Power BI only"
    )


# --- fidelity and automatic titles ------------------------------------------------------------


def test_fidelity_signal_and_automatic_title_reach_the_visual_row(tmp_path):
    chart = visual(
        "chart",
        "clusteredColumnChart",
        {"Category": [col("Region")], "Y": [mea("Sales")], "Series": [col("Segment")]},
        objects={"categoryAxis": [entry({"innerPadding": lit("45L")})]},
    )
    payload = render_page(Engine([]), make_report(tmp_path, [chart]), "one", LiveReportRequest())
    row = payload["visuals"][0]
    # Desktop shows the automatic title of a chart with no title text.
    assert row["title"] == "Sales by Region and Segment"
    assert row["formatting"]["title"]["textSource"] == "automatic"
    assert row["fidelity"] == "approximate" and row["unrendered"] == ["categoryAxis.innerPadding"]
    assert "fidelity" not in row["formatting"] and "unrendered" not in row["formatting"]


def test_content_visuals_render_without_a_query(tmp_path):
    shape = visual(
        "shape",
        "shape",
        objects={"fill": [entry({"fillColor": solid("#FF0000")}, id="default")]},
    )
    box = visual("card", "cardVisual", {"Data": [mea("Sales")]})
    report = make_report(tmp_path, [shape, box])
    visuals, query = compile_page(report, "one", LiveReportRequest())
    rows = {v["id"]: v for v in visuals}
    assert (
        rows["shape"]["formatting"]["shape"]["fill"] == "#ff0000ff"
        and rows["shape"]["kind"] == "shape"
    )
    assert rows["shape"]["fidelity"] == "exact"
    assert "'Orders'[Sales]" in query and '"shape"' not in query


# --- formatting-only render (P6, P19) ----------------------------------------------------------


def test_render_formatting_runs_no_query(tmp_path, monkeypatch):
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no DAX for a formatting-only render")

    monkeypatch.setattr(lr, "compile_page", boom)
    monkeypatch.setattr(lr.LocalReportEngine, "query", boom)
    monkeypatch.setattr(lr.NativeReportPreview, "query", boom)
    chart = visual(
        "chart",
        "clusteredColumnChart",
        {"Category": [col("Region")], "Y": [mea("Sales")]},
        objects={"dataPoint": [entry({"fill": solid("#AA0000")})]},
    )
    report = make_report(tmp_path, [chart, visual("note", "textbox")])
    found = render_formatting(report, "one", LiveReportRequest())
    assert set(found) == {"chart", "note"}
    row = found["chart"]
    assert row["formatting"]["series"]["defaultColor"] == "#AA0000"
    assert row["fidelity"] == "exact" and row["unrendered"] == []
    assert [c["name"] for c in row["columns"]] == ["Region", "Sales"]
    assert row["title"] == "Sales by Region" and row["rank"] == 1 and row["hidden"] is False
    # The data belongs to the data query, not to this response.
    assert not set(row) & lr.DATA_KEYS
    assert "id" not in row
    with pytest.raises(ValueError, match="distinct"):
        render_formatting(
            report, "one", LiveReportRequest(selections=[VisualSelection(visual_id="nope")])
        )


def test_render_formatting_payload_has_the_envelope(tmp_path):
    report = make_report(tmp_path, [CHART])
    payload = render_formatting_payload(report, "one", LiveReportRequest(), {"chart"}, "r0004")
    assert payload["revision"] == "r0004" and payload["head_hash"] == report.source_hash
    assert set(payload["visuals"]) == {"chart"}
    assert payload["page"] == {
        "id": "one",
        "title": "One",
        "width": 1280,
        "height": 720,
        "hidden": False,
        "page_type": "default",
        "binding": None,
    }
    assert payload["theme"]["dataColors"][0] == "#118DFF"
    assert payload["page_style"]["color"] == "#ffffff"


# --- revision echo and the head file (P6) ----------------------------------------------------


def revise(
    report: ImportedReport, tmp_path: Path, edit: Callable[[dict[str, Any]], None]
) -> ImportedReport:
    """A head revision: the original file with the chart's visual.json changed."""
    head = tmp_path / "r0002.pbix"
    part = "Report/definition/pages/one/visuals/chart/visual.json"
    with zipfile.ZipFile(report.archived_path) as source, zipfile.ZipFile(head, "w") as z:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == part:
                body = json.loads(data)
                edit(body)
                data = json.dumps(body).encode()
            z.writestr(info.filename, data)
    pages, _, _ = read_report(head)
    return report.model_copy(
        update={"revision_path": str(head), "revision_hash": sha256_file(head), "pages": pages}
    )


def test_render_page_echoes_the_revision_and_reads_the_head_file(tmp_path):
    report = make_report(tmp_path, [CHART])
    rows = [{"[Visual]": "chart", "[C0]": "West", "[K0]": "text", "[C1]": "5", "[K1]": "number"}]
    first = render_page(Engine(rows), report, "one", LiveReportRequest(), revision="r0001")
    assert first["revision"] == "r0001" and first["head_hash"] == report.source_hash
    assert first["visuals"][0]["formatting"]["series"]["defaultColor"] is None

    def recolor(body: dict[str, Any]) -> None:
        body["visual"]["objects"] = {"dataPoint": [entry({"fill": solid("#00A651")})]}

    edited = revise(report, tmp_path, recolor)
    second = render_page(Engine(rows), edited, "one", LiveReportRequest(), revision="r0002")
    assert second["revision"] == "r0002" and second["head_hash"] == edited.revision_hash
    assert second["head_hash"] != first["head_hash"]
    assert second["visuals"][0]["formatting"]["series"]["defaultColor"] == "#00A651"
    assert second["visuals"][0]["rows"] == [["West", 5.0]]
    assert render_page(Engine(rows), edited, "one", LiveReportRequest())["revision"] is None


def test_head_file_is_integrity_checked(tmp_path):
    report = make_report(tmp_path, [CHART])
    edited = revise(report, tmp_path, lambda body: None)
    broken = edited.model_copy(update={"revision_hash": "0" * 64})
    with pytest.raises(ValueError, match="integrity"):
        render_formatting(broken, "one", LiveReportRequest())


def test_theme_of_the_head_file_reaches_the_payload(tmp_path):
    theme_part = "Report/StaticResources/RegisteredResources/Mine.json"
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
    report = make_report(
        tmp_path,
        [CHART],
        report=report_json,
        parts={
            theme_part: {
                "dataColors": ["#0B6E4F", "#F26419"],
                "textClasses": {"title": {"fontSize": 20}},
            }
        },
    )
    payload = render_page(Engine([]), report, "one", LiveReportRequest())
    assert payload["theme"]["dataColors"] == ["#0B6E4F", "#F26419"]
    assert payload["theme"]["palette"][2] == "#0b6e4f"
    assert payload["theme"]["textClasses"]["title"]["fontSizePx"] == 26.67
    assert payload["visuals"][0]["formatting"]["title"]["fontSize"] == 26.67


# --- data key (P19) ---------------------------------------------------------------------------


def test_data_key_ignores_formatting_and_follows_everything_that_changes_rows(tmp_path):
    report = make_report(
        tmp_path, [CHART, visual("slicer", "slicer", {"Values": [col("Segment")]})]
    )
    request = LiveReportRequest()
    chart = next(v for v in report.pages[0].visuals if v.id == "chart")
    base = data_key(report, chart, request)
    assert len(base) == 64 and data_key(report, chart, request) == base

    def restyle(body: dict[str, Any]) -> None:
        body["visual"]["objects"] = {"dataPoint": [entry({"fill": solid("#AA0000")})]}
        body["visual"]["visualContainerObjects"] = {"title": [entry({"text": lit("'Renamed'")})]}
        body["position"]["x"] = 40

    restyled = revise(report, tmp_path, restyle)
    same = next(v for v in restyled.pages[0].visuals if v.id == "chart")
    assert data_key(restyled, same, request) == base  # a formatting edit keeps the cached data

    def rebind(body: dict[str, Any]) -> None:
        body["visual"]["query"]["queryState"]["Y"]["projections"][0]["field"] = mea("Profit")

    rebound = revise(report, tmp_path, rebind)
    moved = next(v for v in rebound.pages[0].visuals if v.id == "chart")
    assert data_key(rebound, moved, request) != base
    picked = LiveReportRequest(selections=[VisualSelection(visual_id="slicer", values=["West"])])
    assert data_key(report, chart, picked) != base
    slicer = next(v for v in report.pages[0].visuals if v.id == "slicer")
    # A slicer's own selection does not change its own option list.
    assert data_key(report, slicer, picked) == data_key(report, slicer, request)
    searched = LiveReportRequest(searches={"slicer": "we"})
    assert data_key(report, slicer, searched) != data_key(report, slicer, request)
    assert data_key(report, chart, searched) == base


def test_data_key_follows_page_filters_and_formatting_measures(tmp_path):
    report = make_report(tmp_path, [CHART])
    filtered = make_report(tmp_path, [CHART], page={"filterConfig": topn_filter()}, name="f.pbix")
    request = LiveReportRequest()
    chart = report.pages[0].visuals[0]
    assert data_key(filtered, filtered.pages[0].visuals[0], request) != data_key(
        report, chart, request
    )

    def add_font_rule(body: dict[str, Any]) -> None:
        body["visual"]["objects"] = {
            "values": [entry({"fontColor": {"solid": {"color": {"expr": mea("Sales")}}}})]
        }

    rule = revise(report, tmp_path, add_font_rule)
    assert data_key(rule, rule.pages[0].visuals[0], request) != data_key(report, chart, request)
    with pytest.raises(ValueError, match="does not belong"):
        data_key(report, chart.model_copy(update={"id": "ghost"}), request)


# --- the dashboard fixtures -------------------------------------------------------------------


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


def test_structure_twin_page_payload_is_typed_and_ranked():
    report = fixture_report("structure-twin.pbix")
    page = report.pages[0].id
    rows = render_formatting(report, page, LiveReportRequest())
    assert len(rows) == 15
    assert sorted(r["rank"] for r in rows.values()) == list(range(1, 16))
    assert all(r["fidelity"] in {"exact", "approximate", "none"} for r in rows.values())
    chart = rows["b404a461ede7604a2774"]
    assert chart["kind"] == "column" and chart["formatting"]["chart"]["stacking"] == "stacked"
    colors = {c["value"]: c["color"] for c in chart["formatting"]["series"]["categoryColors"]}
    # ThemeDataColor 0 at -20% is the grey "Others" bar; the others are literals.
    assert colors == {"Others": "#cccccc", "TopN": "#1E90FF", "BottomN": "#A30024"}
    assert chart["formatting"]["axes"]["category"]["color"] == "#000000"  # ColorId 1 is black
    # Under the base theme a chart shows its title and a card does not.
    assert chart["formatting"]["title"]["show"] is True
    card = rows["fb4c5aa34e0b084b62b9"]
    assert card["formatting"]["title"]["show"] is False
    assert card["formatting"]["cardValue"]["fontSize"] == 45.33  # 34 pt
    table = rows["e82e02f93055d6b0b544"]
    assert table["formatting"]["table"]["grid"]["horizontal"] is False
    assert table["formatting"]["table"]["values"]["fontSize"] == 16.0  # 12 pt, whatever the grid
    tile = rows["29ba010b20b2162d1536"]["formatting"]["tile"]
    assert tile["selected"]["outline"]["color"] == "#b3b3b3"  # ColorId 0 at -30%
    assert tile["default"]["outline"]["color"] == "#e6e6e6"  # ColorId 0 at -10%


def test_multipage_fixture_has_groups_hidden_content_and_a_custom_theme():
    report = fixture_report("multipage.pbix")
    assert len(report.pages) >= 2
    page = report.pages[0]
    payload = render_formatting_payload(report, page.id, LiveReportRequest(), None, "r0001")
    assert payload["theme"]["dataColors"][0] == "#0B6E4F"  # FixtureTheme is the custom theme
    assert payload["revision"] == "r0001"
    assert payload["page_style"]["alpha"] == 1.0 and "image" in payload["page_style"]
    button = payload["visuals"]["eff2d7b04706cc92f1db"]
    # Buttons are drawn and followed in view mode (they used to be framed only).
    assert button["kind"] == "button" and button["fidelity"] == "exact"
    assert "unsupported_reason" not in button
    assert button["action"] == {
        "type": "page",
        "target": "ReportSection3f1c0d5e7a9b2c4d6e8f",
        "valid": True,
        "tooltip": None,
    }
    detail = next(p for p in report.pages if p.title == "Details")
    kinds = {v["kind"] for v in render_formatting(report, detail.id, LiveReportRequest()).values()}
    assert {"group", "image"} <= kinds


def test_rule_driven_table_font_colors_survive_specificity_resolution(tmp_path):
    # Desktop scopes a conditional font color to "every value cell of this column"; the
    # entry only covers a data point, so the lookup targets one.
    wildcard = {"dataViewWildcard": {"matchingOption": 1}}
    rule = {"solid": {"color": {"expr": mea("Profit")}}}
    table = visual(
        "table",
        "tableEx",
        {"Values": [col("Region"), mea("Sales")]},
        objects={
            "values": [
                entry({"fontSize": lit("12D")}),
                entry({"fontColor": rule}, data=[wildcard], metadata="Orders.Sales"),
            ]
        },
    )
    visuals, query = compile_page(make_report(tmp_path, [table]), "one", LiveReportRequest())
    columns = visuals[0]["columns"]
    assert [c["name"] for c in columns] == ["Region", "Sales", "Profit"]
    assert columns[2]["hidden"] is True and columns[2]["role"] == "Formatting"
    assert columns[1]["colorIndex"] == 2 and columns[0]["colorIndex"] is None
    assert "'Orders'[Profit]" in query
    assert visuals[0]["formatting"]["table"]["values"]["fontSize"] == 16.0


def test_every_row_carries_its_data_key_so_a_formatting_edit_can_skip_the_query(tmp_path):
    report = make_report(tmp_path, [CHART, visual("note", "textbox")])
    request = LiveReportRequest()
    before = render_formatting(report, "one", request)
    assert before["chart"]["data_key"] == data_key(report, report.pages[0].visuals[0], request)

    def restyle(body: dict[str, Any]) -> None:
        body["visual"]["objects"] = {"dataPoint": [entry({"fill": solid("#AA0000")})]}

    edited = revise(report, tmp_path, restyle)
    after = render_formatting(edited, "one", request)
    assert after["chart"]["data_key"] == before["chart"]["data_key"]  # keep the cached rows
    assert after["chart"]["formatting"]["series"]["defaultColor"] == "#AA0000"
    rows = [{"[Visual]": "chart", "[C0]": "West", "[K0]": "text", "[C1]": "5", "[K1]": "number"}]
    page = render_page(Engine(rows), edited, "one", request)
    assert by_id(page)["chart"]["data_key"] == after["chart"]["data_key"]


def test_textbox_shape_and_image_backgrounds_default_to_off(tmp_path):
    kinds = ["textbox", "shape", "image"]
    report = make_report(tmp_path, [visual(k, k) for k in kinds] + [visual("gauge", "gauge")])
    found = render_formatting(report, "one", LiveReportRequest())
    assert [found[k]["formatting"]["container"]["background"] for k in kinds] == [None] * 3
    assert found["gauge"]["formatting"]["container"]["background"] == "#ffffffff"


# --- the formatting-only render and the data render describe a visual the same way -----------


def hidden_sort_chart() -> dict[str, Any]:
    raw = visual(
        "chart", "clusteredColumnChart", {"Category": [col("Region")], "Y": [mea("Sales")]}
    )
    raw["visual"]["query"]["sortDefinition"] = {
        "sort": [{"field": mea("Profit"), "direction": "Descending"}]
    }
    return raw


def test_both_renders_give_a_visual_the_same_error_needs_desktop_and_fidelity(tmp_path):
    cases: list[tuple[ImportedReport, LiveReportRequest, str]] = [
        (
            make_report(tmp_path, [CHART], page={"filterConfig": topn_filter()}, name="a.pbix"),
            LiveReportRequest(),
            "This page filter needs Power BI Desktop",
        ),
        (
            make_report(tmp_path, [hidden_sort_chart()], name="b.pbix"),
            LiveReportRequest(),
            "The visual sorts by a hidden field not yet supported",
        ),
        (
            make_report(
                tmp_path,
                [CHART, visual("slicer", "slicer", {"Values": [col("Segment")]})],
                page={
                    "visualInteractions": [
                        {"source": "slicer", "target": "chart", "type": "Highlight"}
                    ]
                },
                name="c.pbix",
            ),
            LiveReportRequest(selections=[VisualSelection(visual_id="slicer", values=["A"])]),
            "This page uses a custom visual interaction not yet supported",
        ),
    ]
    for report, request, message in cases:
        full = by_id(render_page(Engine([]), report, "one", request))["chart"]
        light = render_formatting(report, "one", request)["chart"]
        assert full["error"] == light["error"] == message
        assert full["needs_desktop"] is light["needs_desktop"] is True
        assert full["fidelity"] == light["fidelity"] == "none"
        # The client merges the light row over its cached one: nothing may flip back.
        assert {**full, **light}["fidelity"] == "none"


def test_a_clean_visual_is_the_same_in_both_renders(tmp_path):
    report = make_report(tmp_path, [CHART])
    rows = [{"[Visual]": "chart", "[C0]": "West", "[K0]": "text", "[C1]": "5", "[K1]": "number"}]
    full = by_id(render_page(Engine(rows), report, "one", LiveReportRequest()))["chart"]
    light = render_formatting(report, "one", LiveReportRequest())["chart"]
    assert "error" not in full and "error" not in light
    for key in ("fidelity", "unrendered", "formatting", "columns", "data_key", "rank", "title"):
        assert full[key] == light[key], key


def test_a_query_asked_of_a_visual_that_needs_desktop_gets_no_answer(tmp_path):
    # Extra figures are read as "the value under this card's filters": with a filter Studio
    # cannot apply there is no such value, and an unfiltered one would be wrong.
    card = visual("card", "card", {"Values": [mea("Sales")]})
    report = make_report(tmp_path, [card], page={"filterConfig": topn_filter()})
    with pytest.raises(Unsupported, match="page filter needs Power BI Desktop"):
        compile_page(report, "one", LiveReportRequest(), only={"card"}, extra={"card": ["[Sales]"]})
    # Without a request for extras the visual is only marked.
    metadata, query = compile_page(report, "one", LiveReportRequest(), only={"card"})
    assert metadata[0]["needs_desktop"] is True and query == ""


# --- the data key changes exactly when a visual's columns do ----------------------------------


def image_card(show: bool) -> dict[str, Any]:
    return visual(
        "card",
        "cardVisual",
        {"Data": [mea("Sales")]},
        objects={
            "image": [
                entry(
                    {"show": lit("true" if show else "false"), "imageUrl": {"expr": mea("Profit")}},
                    metadata="Orders.Sales",
                )
            ]
        },
    )


def test_data_key_follows_a_switch_that_adds_a_formatting_measure(tmp_path):
    request = LiveReportRequest()
    off = make_report(tmp_path, [image_card(False)], name="off.pbix")
    on = make_report(tmp_path, [image_card(True)], name="on.pbix")
    off_row = by_id(render_page(Engine([]), off, "one", request))["card"]
    on_row = by_id(render_page(Engine([]), on, "one", request))["card"]
    assert [c["name"] for c in off_row["columns"]] == ["Sales"]
    assert [c["name"] for c in on_row["columns"]] == ["Sales", "Profit"]
    # The columns and the query differ, so the key must, or a client would keep rows
    # that have no picture column.
    assert off_row["data_key"] != on_row["data_key"]
    assert data_key(on, on.pages[0].visuals[0], request) == on_row["data_key"]

    # A change that leaves the columns alone leaves the key alone.
    def recolor(body: dict[str, Any]) -> None:
        body["visual"]["objects"]["image"][0]["properties"]["scaling"] = lit("'Fit'")

    same = revise_card(on, tmp_path, recolor)
    assert data_key(same, same.pages[0].visuals[0], request) == on_row["data_key"]


def revise_card(
    report: ImportedReport, tmp_path: Path, edit: Callable[[dict[str, Any]], None]
) -> ImportedReport:
    head = tmp_path / "card-head.pbix"
    part = "Report/definition/pages/one/visuals/card/visual.json"
    with zipfile.ZipFile(report.archived_path) as source, zipfile.ZipFile(head, "w") as z:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == part:
                body = json.loads(data)
                edit(body)
                data = json.dumps(body).encode()
            z.writestr(info.filename, data)
    pages, _, _ = read_report(head)
    return report.model_copy(
        update={"revision_path": str(head), "revision_hash": sha256_file(head), "pages": pages}
    )


# --- one malformed visual never takes the page down -------------------------------------------


def test_a_malformed_visual_is_marked_and_the_rest_of_the_page_renders(tmp_path):
    broken_projection = visual("bad", "clusteredColumnChart", {"Category": [col("Region")]})
    broken_projection["visual"]["query"]["queryState"]["Category"]["projections"] = ["oops"]
    broken_scope = visual(
        "worse",
        "clusteredColumnChart",
        {"Category": [col("Region")], "Y": [mea("Sales")]},
        objects={
            "dataPoint": [
                {"properties": {"fill": solid("#FF0000")}, "selector": "oops"},
                {
                    "properties": {"fill": solid("#00FF00")},
                    "selector": {"data": [{"scopeId": {"Comparison": {"Left": 1, "Right": 2}}}]},
                },
            ]
        },
    )
    report = make_report(tmp_path, [CHART, broken_projection, broken_scope])
    payload = render_page(Engine([]), report, "one", LiveReportRequest())
    rows = by_id(payload)
    assert "error" not in rows["chart"] and rows["chart"]["fidelity"] == "exact"
    assert rows["bad"]["error"] == "This visual definition needs additional local support"
    assert rows["bad"]["needs_desktop"] is True and rows["bad"]["fidelity"] == "none"
    assert rows["bad"]["position"] == {"x": 0, "y": 0, "width": 300, "height": 200}
    # Bad entries are dropped and reported as not drawn; the visual itself still renders.
    assert "error" not in rows["worse"] and rows["worse"]["unrendered"] == ["dataPoint.fill"]
    assert rows["worse"]["fidelity"] == "approximate"
