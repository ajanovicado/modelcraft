"""Source provenance, exact browser values, and native-only model routing."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ai_powerbi_analyst.studio import connections
from ai_powerbi_analyst.studio.api import create_app
from ai_powerbi_analyst.studio.connections import DatabaseImportRequest, DatabaseSource
from ai_powerbi_analyst.studio.contracts import (
    CreateWorkspaceRequest,
    DatabaseSnapshotSource,
    DaxResult,
    IngestRequest,
    WorkspaceKind,
)
from ai_powerbi_analyst.studio.report_import import ImportedReport
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH

ROOT = Path(__file__).resolve().parents[2]
HEADERS = {"Authorization": "Bearer source-route-test-token"}


@pytest.fixture
def service(tmp_path):
    return StudioService(ROOT, tmp_path / "state")


def workspace(service):
    return service.create_workspace(
        CreateWorkspaceRequest(
            name="Database workspace",
            idempotency_key="create-database-workspace",
            expected_base_hash=EMPTY_HASH,
        )
    )


def test_folder_free_database_import_retains_limit_precision_and_exact_replay(service, monkeypatch):
    w = workspace(service)
    assert w.selected_folder is None and w.active_snapshot_id is None
    calls = []

    class Cursor:
        description = [
            ("Identifier", int),
            ("Amount", Decimal, None, None, 30, 20),
            ("RecordedAt", datetime),
        ]
        exhausted = False

        def execute(self, query, params=()):
            calls.append(query)
            self.exhausted = False

        def fetchone(self):
            return ("Sales",)

        def fetchmany(self, count):
            if self.exhausted:
                return []
            self.exhausted = True
            return [
                (
                    123456789012345678,
                    Decimal("0.12345678901234567890"),
                    datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=-5))),
                ),
                (None, None, datetime(2026, 7, 1, 12, tzinfo=timezone(timedelta(hours=-4)))),
            ]

        def close(self):
            pass

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def connect(_):
        yield Connection()

    monkeypatch.setattr(connections, "connect", connect)
    monkeypatch.setattr(
        connections.psutil,
        "virtual_memory",
        lambda: type("Memory", (), {"available": 4 * 1024**3})(),
    )
    request = DatabaseImportRequest(
        workspace_id=w.id,
        source=DatabaseSource(provider="sqlserver", profile="fixture"),
        tables=["Sales"],
        max_rows=500_000,
        expected_base_hash=w.base_hash,
        idempotency_key="database-import-exact",
    )
    imported = service.import_database(request)
    assert imported.database_source.max_rows == 500_000
    assert imported.database_source.schema_name == "dbo"
    assert any("TOP (500001)" in query for query in calls)
    assert imported.tables[0].table.columns[0].data_type == "integer"
    assert imported.tables[0].table.columns[1].data_type == "string"
    assert any(
        finding.column_id == imported.tables[0].table.columns[1].id
        and "preserve decimal precision" in finding.message
        for finding in imported.findings
    )
    frame = pd.read_parquet(imported.tables[0].normalized_parquet_path)
    assert frame.iloc[0, 0] == 123456789012345678
    assert frame.iloc[0, 1] == "0.12345678901234567890"
    assert list(frame["RecordedAt"]) == [
        pd.Timestamp("2026-01-01T17:00:00Z"),
        pd.Timestamp("2026-07-01T16:00:00Z"),
    ]
    assert any(
        finding.column_id == imported.tables[0].table.columns[2].id
        and "normalized to UTC" in finding.message
        for finding in imported.findings
    )
    head = service.workspace(w.id).base_hash
    monkeypatch.setattr(connections, "connect", lambda _: pytest.fail("Replay queried the source"))
    assert service.import_database(request).id == imported.id
    assert service.workspace(w.id).base_hash == head
    assert service.snapshot(imported.id).database_source.max_rows == 500_000


def test_browser_preview_preserves_large_nullable_integer_and_exact_filter(service, tmp_path):
    w = workspace(service)
    path = tmp_path / "identifiers.parquet"
    pd.DataFrame({"ID": pd.Series([123456789012345678, 42, None], dtype="Int64")}).to_parquet(path)
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=w.id,
            item_paths=[str(path)],
            idempotency_key="ingest-identifiers",
            expected_base_hash=w.base_hash,
        )
    )
    table = snapshot.tables[0]
    client = TestClient(create_app(service, "source-route-test-token"))
    route = f"/v1/snapshots/{snapshot.id}/preview"
    reply = client.post(route, headers=HEADERS, json={"table_id": table.id})
    assert reply.status_code == 200
    assert reply.json()["rows"] == [["123456789012345678"], [42], [None]]
    assert reply.json()["columns"][0]["data_type"] == "integer"
    filtered = client.post(
        route,
        headers=HEADERS,
        json={
            "table_id": table.id,
            "filters": [
                {
                    "column_id": table.table.columns[0].id,
                    "operator": "equals",
                    "value": "123456789012345678",
                }
            ],
        },
    )
    assert filtered.status_code == 200 and filtered.json()["rows"] == [["123456789012345678"]]


def attach_report(service, w, tmp_path, *, owner=None):
    original = tmp_path / "original.pbix"
    original.write_bytes(b"unchanged saved source report")
    report = ImportedReport(
        id="report-native-no-rows",
        workspace_id=owner or w.id,
        name="Native model",
        source_path=str(original),
        archived_path=str(original),
        source_hash="a" * 64,
        base_hash="a" * 64,
        format="PBIR",
        pages=[],
    )
    service.store.put(
        "imported_report",
        report,
        workspace_id=w.id,
        expected_base_hash=EMPTY_HASH,
        idempotency_key="store-imported-report",
        mutation_payload=report,
    )
    service.store.put(
        "workspace",
        w.model_copy(
            update={
                "imported_report_id": report.id,
                "kind": WorkspaceKind.IMPORTED_REPORT,
                "base_hash": "b" * 64,
            }
        ),
        workspace_id=w.id,
        expected_base_hash=w.base_hash,
        idempotency_key="attach-report",
        mutation_payload=report,
    )
    return report, original


def test_native_query_uses_owned_report_without_local_rows_and_preserves_source(
    service, tmp_path, monkeypatch
):
    w = workspace(service)
    report, original = attach_report(service, w, tmp_path)
    calls = []

    class NativeEngine:
        def evaluate(self, selected, snapshot, request):
            calls.append((selected.id, selected.workspace_id, snapshot, request.expression))
            return DaxResult(
                engine="powerbi",
                columns=["Value"],
                rows=[{"Value": 7}],
                row_count=1,
                elapsed_ms=1,
                plan="Exact Desktop model fixture",
            )

    monkeypatch.setattr(service, "native_report_engine", NativeEngine)
    client = TestClient(create_app(service, "source-route-test-token"))
    payload = {"workspace_id": w.id, "expression": "[Sales]", "engine": "powerbi"}
    route = f"/v1/workspaces/{w.id}/dax/evaluate"
    reply = client.post(route, json=payload, headers=HEADERS)
    assert reply.status_code == 200 and reply.json()["engine"] == "powerbi"
    assert calls == [(report.id, w.id, None, "[Sales]")]
    assert original.read_bytes() == b"unchanged saved source report"
    assert service.workspace(w.id).active_snapshot_id is None
    assert (
        client.post(route, json={**payload, "engine": "snapshot"}, headers=HEADERS).status_code
        == 422
    )
    assert (
        client.post(
            route, json={**payload, "workspace_id": "workspace-other"}, headers=HEADERS
        ).status_code
        == 422
    )
    assert len(calls) == 1


@pytest.mark.parametrize("foreign_report", [False, True])
def test_native_query_refuses_absent_or_foreign_report(
    service, tmp_path, monkeypatch, foreign_report
):
    w = workspace(service)
    if foreign_report:
        attach_report(service, w, tmp_path, owner="workspace-other")
    monkeypatch.setattr(
        service, "native_report_engine", lambda: pytest.fail("Foreign model queried")
    )
    client = TestClient(create_app(service, "source-route-test-token"))
    reply = client.post(
        f"/v1/workspaces/{w.id}/dax/evaluate",
        headers=HEADERS,
        json={"workspace_id": w.id, "expression": "[Sales]", "engine": "powerbi"},
    )
    assert reply.status_code == 422 and "no imported report" in reply.json()["detail"]


def test_legacy_source_limit_default_and_bounded_persisted_limit():
    source = {
        "provider": "sqlserver",
        "profile": "fixture",
        "schema_name": "dbo",
        "tables": ["Sales"],
    }
    assert DatabaseSnapshotSource(**source).max_rows == 100_000
    for value in (0, 1_000_001):
        with pytest.raises(ValidationError):
            DatabaseSnapshotSource(**source, max_rows=value)


def test_sidecar_installs_and_collects_optional_database_sdks():
    script = (ROOT / "scripts/build-desktop-sidecar.ps1").read_text(encoding="utf-8")
    assert "--frozen --extra desktop --extra databases pyinstaller" in script
    assert "--collect-all snowflake.connector" in script
    assert "--hidden-import pyodbc" in script
