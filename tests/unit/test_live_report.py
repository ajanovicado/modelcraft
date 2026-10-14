import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_powerbi_analyst.integrity import sha256_file
from ai_powerbi_analyst.studio.api import create_app
from ai_powerbi_analyst.studio.live_report import (
    Compiler,
    LiveReportRequest,
    Unsupported,
    VisualSelection,
    compile_page,
    literal,
    render_page,
)
from ai_powerbi_analyst.studio.report_import import ImportedReport, read_report
from ai_powerbi_analyst.studio.service import StudioService


def field(kind, name):
    return {kind: {"Expression": {"SourceRef": {"Entity": "Orders"}}, "Property": name}}


def visual(name, kind, fields):
    return {
        "name": name,
        "position": {"width": 300, "height": 200},
        "visual": {
            "visualType": kind,
            "query": {"queryState": {"Values": {"projections": [{"field": f} for f in fields]}}},
        },
    }


@pytest.fixture
def imported(tmp_path):
    def make(*, unsupported=False, no_filter=False, missing_slicer=False):
        slicer = visual(
            "slicer", "slicer", [field("Column", "Missing" if missing_slicer else "Region")]
        )
        saved = {
            "Where": [
                {
                    "Condition": {
                        "In": {
                            "Expressions": [field("Column", "Region")],
                            "Values": [[{"Literal": {"Value": "'West'"}}]],
                        }
                    }
                }
            ]
        }
        slicer["visual"]["objects"] = {"general": [{"properties": {"filter": {"filter": saved}}}]}
        chart = visual(
            "chart", "columnChart", [field("Column", "Region"), field("Measure", "Sales")]
        )
        if unsupported:
            chart["filterConfig"] = {
                "filters": [{"filter": {"Where": [{"Condition": {"Unknown": {}}}]}}]
            }
        path = tmp_path / "original.pbix"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "Report/definition/pages/pages.json", json.dumps({"pageOrder": ["one"]})
            )
            archive.writestr(
                "Report/definition/pages/one/page.json",
                json.dumps(
                    {
                        "name": "one",
                        "displayName": "One",
                        "visualInteractions": [
                            {"source": "slicer", "target": "chart", "type": "NoFilter"}
                        ]
                        if no_filter
                        else [],
                    }
                ),
            )
            for v in (slicer, chart):
                archive.writestr(
                    f"Report/definition/pages/one/visuals/{v['name']}/visual.json", json.dumps(v)
                )
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
            tables=[{"TableName": "Orders", "ColumnName": "Region"}],
            measures=[{"TableName": "Orders", "Name": "Sales", "Expression": "1"}],
        )

    return make


def test_saved_slicer_context_is_applied_to_chart_but_not_its_own_options(imported):
    visuals, query = compile_page(imported(), "one", LiveReportRequest())
    assert visuals[0]["selected"] == ["West"]
    assert query.count("'Orders'[Region] IN {\"West\"}") == 1
    assert "KEEPFILTERS" in query and "TOPN(101" in query


def test_explicit_clear_overrides_saved_selection_and_no_filter_is_preserved(imported):
    _, cleared = compile_page(
        imported(), "one", LiveReportRequest(selections=[VisualSelection(visual_id="slicer")])
    )
    assert '"West"' not in cleared
    _, ignored = compile_page(imported(no_filter=True), "one", LiveReportRequest())
    assert '"West"' not in ignored


def test_unsupported_filter_does_not_silently_render_unfiltered_values(imported):
    visuals, query = compile_page(imported(unsupported=True), "one", LiveReportRequest())
    assert "filter type" in visuals[1]["error"]
    assert '"chart"' not in query


def test_field_and_selection_validation_and_literal_escaping(imported):
    report = imported()
    with pytest.raises(Unsupported, match="missing"):
        Compiler(report).field(field("Measure", 'Injected] EVALUATE ROW("x",1)'))
    with pytest.raises(ValueError, match="distinct"):
        compile_page(
            report,
            "one",
            LiveReportRequest(selections=[VisualSelection(visual_id="elsewhere", values=["West"])]),
        )
    with pytest.raises(ValueError, match="finite"):
        literal(float("inf"))
    assert literal('a"b') == '"a""b"'
    _, query = compile_page(
        report,
        "one",
        LiveReportRequest(
            selections=[VisualSelection(visual_id="slicer", values=['x" EVALUATE ROW("a",1)'])]
        ),
    )
    assert '"x"" EVALUATE ROW(""a"",1)"' in query


def test_integrity_check_precedes_query_compilation(imported):
    report = imported().model_copy(update={"source_hash": "0" * 64})
    with pytest.raises(ValueError, match="integrity"):
        compile_page(report, "one", LiveReportRequest())


def test_batch_decodes_types_and_marks_bounded_results(imported):
    class Engine:
        def query(self, report, query, max_rows):
            assert max_rows == 203
            return {
                "rows": [
                    {
                        "[Visual]": "chart",
                        "[C0]": f"Region {i:03}",
                        "[K0]": "text",
                        "[C1]": "42.25",
                        "[K1]": "number",
                    }
                    for i in range(101)
                ]
            }

    result = render_page(Engine(), imported(), "one", LiveReportRequest())
    chart = result["visuals"][1]
    assert chart["truncated"] and len(chart["rows"]) == 100
    assert chart["rows"][0] == ["Region 000", 42.25]


def test_render_endpoint_requires_auth_and_closed_body(tmp_path, monkeypatch):
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    monkeypatch.setattr(
        service,
        "render_imported_page",
        lambda wid, pid, req, revision=None: {"visuals": [], "page": pid},
    )
    with TestClient(create_app(service, "live-report-test-token")) as client:
        path = "/v1/workspaces/workspace-test/imported-report/pages/one/render"
        assert client.post(path, json={}).status_code == 401
        headers = {"Authorization": "Bearer live-report-test-token"}
        assert (
            client.post(path, headers=headers, json={"query": "EVALUATE anything"}).status_code
            == 422
        )
        assert client.post(path, headers=headers, json={}).json()["page"] == "one"


def test_unsupported_active_slicer_blocks_unfiltered_page(imported):
    with pytest.raises(Unsupported, match="unsupported field"):
        compile_page(imported(missing_slicer=True), "one", LiveReportRequest())


def test_formatted_cells_follow_raw_rows_when_native_results_are_reordered(imported):
    class Engine:
        def query(self, report, query, max_rows):
            return {
                "rows": [
                    {
                        "[Visual]": "chart",
                        "[C0]": region,
                        "[K0]": "text",
                        "[C1]": raw,
                        "[K1]": "number",
                        "[F1]": shown,
                    }
                    for region, raw, shown in (
                        ("West", "0.75", "75.00%"),
                        ("East", "0.25", "25.00%"),
                    )
                ]
            }

    result = render_page(Engine(), imported(), "one", LiveReportRequest())
    chart = next(v for v in result["visuals"] if v["id"] == "chart")
    assert chart["rows"] == [["East", 0.25], ["West", 0.75]]
    assert chart["formatted_rows"] == [[None, "25.00%"], [None, "75.00%"]]


def test_measure_format_is_literal_escaped_in_native_query(imported):
    report = imported()
    from unittest.mock import patch

    with patch(
        "ai_powerbi_analyst.studio.live_report.model_formats",
        return_value={("Orders", "Sales"): '0.00% "literal"'},
    ):
        visuals, query = compile_page(report, "one", LiveReportRequest())
    assert '"0.00% ""literal"""' in query
    assert visuals[1]["columns"][1]["format"] == '0.00% "literal"'
