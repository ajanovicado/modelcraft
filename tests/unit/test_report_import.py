from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pandas as pd
import pytest
from editing_support import FakeDesktop, patch_diff
from fastapi.testclient import TestClient

from ai_powerbi_analyst.integrity import sha256_file
from ai_powerbi_analyst.studio.api import create_app
from ai_powerbi_analyst.studio.contracts import DaxEvaluateRequest
from ai_powerbi_analyst.studio.desktop_session import DesktopReadiness
from ai_powerbi_analyst.studio.imported_editing import ImportedEditError, ImportedEditingService
from ai_powerbi_analyst.studio.report_import import ImportReportRequest, read_report
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import StateConflictError


def report_file(tmp_path: Path, *, embedded: bool = True) -> Path:
    file = tmp_path / "Original dashboard.pbix"
    with zipfile.ZipFile(file, "w") as z:
        z.writestr(
            "Report/definition/pages/pages.json", json.dumps({"pageOrder": ["second", "first"]})
        )
        for name in ("first", "second"):
            z.writestr(
                f"Report/definition/pages/{name}/page.json",
                json.dumps(
                    {"name": name, "displayName": name.title(), "width": 1920, "height": 1200}
                ),
            )
            z.writestr(
                f"Report/definition/pages/{name}/visuals/v1/visual.json",
                json.dumps(
                    {
                        "name": "v1",
                        "position": {"x": 35, "y": 70, "width": 900, "height": 400},
                        "visual": {
                            "visualType": "slicer",
                            "query": {
                                "queryState": {
                                    "Values": {
                                        "projections": [
                                            {
                                                "field": {
                                                    "Column": {
                                                        "Expression": {
                                                            "SourceRef": {"Entity": "Orders"}
                                                        },
                                                        "Property": "Region",
                                                    }
                                                }
                                            }
                                        ]
                                    }
                                }
                            },
                        },
                    }
                ),
            )
        if embedded:
            z.writestr("DataModel", b"fixture-decoder-substituted")
    return file


@pytest.fixture
def service(tmp_path: Path, monkeypatch) -> StudioService:
    def model(_source, destination):
        pd.DataFrame({"Region": ["East", "West"], "Sales": [100.0, 250.0]}).to_parquet(
            destination / "table-0.parquet", index=False
        )
        return {
            "tables": [{"name": "Orders", "file": "table-0.parquet", "rows": 2}],
            "facts": {
                "version": 1,
                "tables": [
                    {"name": name, "source": "m", "calculated": False}
                    for name in ("Orders", "Measures")
                ],
                "columns": [
                    {"table": "Orders", "name": "Region", "kind": "data", "dataType": 2},
                    {"table": "Orders", "name": "Sales", "kind": "data", "dataType": 8},
                ],
                "measures": [
                    {
                        "table": "Measures",
                        "name": "Original Sales",
                        "expression": "SUM('Orders'[Sales])",
                    }
                ],
                "relationships": [],
                "partitions": [
                    {"table": name, "source": "m", "mode": "import"}
                    for name in ("Orders", "Measures")
                ],
            },
            "schema": [{"TableName": "Orders", "ColumnName": "Sales", "PandasDataType": "float64"}],
            "measures": [
                {
                    "TableName": "Measures",
                    "Name": "Original Sales",
                    "Expression": "SUM('Orders'[Sales])",
                }
            ],
            "relationships": [],
        }

    monkeypatch.setattr("ai_powerbi_analyst.studio.report_import.extract_model", model)
    return StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")


def test_import_keeps_native_pages_slicers_data_and_original_dax(service, tmp_path):
    source = report_file(tmp_path)
    original_hash = sha256_file(source)
    workspace = service.import_existing_report(
        ImportReportRequest(path=str(source), idempotency_key="import-fixture-01")
    )
    report = service.imported_report(workspace.id)
    assert [p.title for p in report.pages] == ["Second", "First"]
    assert report.pages[0].visuals[0].visual_type == "slicer"
    assert report.pages[0].visuals[0].x == 35
    assert report.pages[0].visuals[0].bindings[0]["name"] == "Region"
    assert report.measures[0]["Expression"] == "SUM('Orders'[Sales])"
    assert sha256_file(source) == sha256_file(Path(report.archived_path)) == original_hash
    snapshot = service.snapshot(workspace.active_snapshot_id)
    assert snapshot.tables[0].table.name == "Orders"
    assert snapshot.tables[0].table.row_count == 2
    result = service.evaluate_dax(
        DaxEvaluateRequest(workspace_id=workspace.id, expression="[Original Sales]")
    )
    assert result.rows[0][result.columns[0]] == 350
    assert "Original Sales" in service._chat_context(workspace, "Explain Original Sales")
    # Import survives a process restart without reparsing or changing the user's file.
    reloaded = StudioService(Path(__file__).resolve().parents[2], service.state_root)
    assert reloaded.imported_report(workspace.id) == report


def test_import_is_idempotent_and_does_not_duplicate_workspaces(service, tmp_path):
    request = ImportReportRequest(
        path=str(report_file(tmp_path)), idempotency_key="import-fixture-retry"
    )
    first = service.import_existing_report(request)
    assert service.import_existing_report(request) == first
    assert len(service.list_workspaces()) == 1
    with pytest.raises(StateConflictError):
        service.import_existing_report(request.model_copy(update={"path": "different.pbix"}))


def test_thin_report_imports_pages_without_fabricating_data(service, tmp_path):
    workspace = service.import_existing_report(
        ImportReportRequest(
            path=str(report_file(tmp_path, embedded=False)), idempotency_key="thin-report-fixture"
        )
    )
    assert workspace.active_snapshot_id is None
    report = service.imported_report(workspace.id)
    assert report.pages and report.warnings and not report.tables


def test_desktop_opens_working_copy_and_preserves_later_edits(service, tmp_path, monkeypatch):
    """ "Open in Power BI" opens Studio's working copy (never the user's file or the preserved
    original), and a save the user made there is recorded as a revision before the next open
    replaces the working copy. Power BI Desktop is the in-memory fake of the edit tests."""
    patch_diff(monkeypatch)
    source = report_file(tmp_path)
    original_hash = sha256_file(source)
    workspace = service.import_existing_report(
        ImportReportRequest(path=str(source), idempotency_key="desktop-import-fixture")
    )
    report = service.imported_report(workspace.id)
    desktop = FakeDesktop()
    unloads: list[str] = []
    service.editing = ImportedEditingService(
        service.state_root,
        service.store,
        load_report=service.imported_report,
        port_factory=lambda: desktop,
        readiness_factory=lambda: DesktopReadiness(True, True, True, True, "2.157", []),
        read_measures=lambda path: list(report.measures),
        read_types=lambda path: {"columns": [], "measures": []},
        before_launch=lambda: unloads.append("model unloaded"),
        disk_free=lambda path: 10**12,
        profile_dir=tmp_path,
    )
    opened = service.open_imported_report(workspace.id)
    assert opened == {"launch_requested": True, "already_open": False}
    working = Path(report.archived_path).parent / "desktop" / "Report.pbix"
    assert working != source and working != Path(report.archived_path)
    assert desktop.find(working) is not None and unloads == ["model unloaded"]
    assert desktop.events == ["open normalize=True"]
    part = "Report/definition/pages/first/visuals/v1/visual.json"
    desktop.user_edit(working, part, lambda visual: visual["position"].__setitem__("x", 99.0))
    desktop.user_saves(working)
    again = service.open_imported_report(workspace.id)  # Desktop still has it: nothing changes
    assert again == {"launch_requested": True, "already_open": True}
    assert unloads == ["model unloaded"]
    desktop.user_closes(working)
    service.open_imported_report(workspace.id)
    history = service.editing.history(workspace.id)
    assert [(row["id"], row["author"]) for row in history] == [
        ("r0001", "desktop"),
        ("r0000", "import"),
    ]
    with zipfile.ZipFile(working) as archive:
        assert json.loads(archive.read(part))["position"]["x"] == 99.0  # the user's save kept
    assert sha256_file(source) == sha256_file(Path(report.archived_path)) == original_hash
    Path(report.archived_path).write_bytes(b"tampered archive")
    desktop.user_closes(working)
    with pytest.raises(ImportedEditError, match="integrity check"):
        service.open_imported_report(workspace.id)
    assert desktop.find(working) is None


def test_legacy_layout_resolves_query_aliases(tmp_path):
    path = tmp_path / "legacy.pbix"
    layout = {
        "sections": [
            {
                "name": "p1",
                "visualContainers": [
                    {
                        "x": 10,
                        "y": 20,
                        "width": 500,
                        "height": 300,
                        "config": json.dumps(
                            {
                                "name": "v1",
                                "singleVisual": {
                                    "visualType": "tableEx",
                                    "prototypeQuery": {
                                        "From": [{"Name": "o", "Entity": "Orders"}],
                                        "Select": [
                                            {
                                                "Column": {
                                                    "Expression": {"SourceRef": {"Source": "o"}},
                                                    "Property": "Region",
                                                }
                                            }
                                        ],
                                    },
                                },
                            }
                        ),
                    }
                ],
            }
        ]
    }
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Report/Layout", json.dumps(layout).encode("utf-16-le"))
    pages, kind, _ = read_report(path)
    assert kind == "Legacy PBIX"
    assert pages[0].visuals[0].bindings[0]["table"] == "Orders"


@pytest.mark.parametrize("member", ["../escape.json", "/absolute.json", "C:/escape.json"])
def test_rejects_unsafe_archive_members(tmp_path, member):
    path = report_file(tmp_path)
    with zipfile.ZipFile(path, "a") as z:
        z.writestr(member, "{}")
    with pytest.raises(ValueError, match="invalid component"):
        read_report(path)


def test_import_api_requires_auth_and_returns_real_import(service, tmp_path, monkeypatch):
    token = "report-import-test-token"
    payload = {"path": str(report_file(tmp_path)), "idempotency_key": "api-import-fixture"}
    with TestClient(create_app(service, token)) as client:
        assert client.post("/v1/reports/import", json=payload).status_code == 401
        response = client.post(
            "/v1/reports/import", headers={"Authorization": f"Bearer {token}"}, json=payload
        )
        assert response.status_code == 200, response.text
        workspace = response.json()
        report = client.get(
            f"/v1/workspaces/{workspace['id']}/imported-report",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert report.status_code == 200 and len(report.json()["pages"]) == 2
        page_id = report.json()["pages"][0]["id"]
        preview_path = f"/v1/workspaces/{workspace['id']}/imported-report/pages/{page_id}/preview"
        monkeypatch.setattr(
            service,
            "preview_imported_page",
            lambda wid, pid, refresh, revision=None: {
                "workspace_id": wid,
                "page_id": pid,
                "cached": not refresh,
            },
        )
        assert client.post(preview_path).status_code == 401
        preview = client.post(
            preview_path + "?refresh=true", headers={"Authorization": f"Bearer {token}"}
        )
        assert preview.status_code == 200 and preview.json()["page_id"] == page_id
        assert preview.json()["cached"] is False
