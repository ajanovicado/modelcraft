from contextlib import contextmanager
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_powerbi_analyst.studio import connections
from ai_powerbi_analyst.studio.connections import (
    DatabaseBrowseRequest,
    DatabaseImportRequest,
    DatabaseSource,
    DatabaseTableRequest,
)
from ai_powerbi_analyst.studio.contracts import CreateWorkspaceRequest, IngestRequest
from ai_powerbi_analyst.studio.data_access import TablePreviewRequest, preview_table
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH, StateConflictError


@pytest.fixture
def data(tmp_path):
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    source = tmp_path / "data.csv"
    source.write_text("Name,Value\n" + "".join(f"Customer {i},{i}\n" for i in range(123)))
    w = service.create_workspace(
        CreateWorkspaceRequest(
            name="Data QA",
            selected_folder=str(tmp_path),
            idempotency_key="create-data-qa",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=w.id,
            item_paths=[str(source)],
            idempotency_key="import-data-qa",
            expected_base_hash=w.base_hash,
        )
    )
    return service, snapshot


def test_preview_pages_and_projection(data):
    service, snapshot = data
    table = snapshot.tables[0].table
    original = service.workspace(snapshot.workspace_id).base_hash
    pages = [
        preview_table(
            snapshot,
            TablePreviewRequest(table_id=table.id, column_ids=[table.columns[1].id], offset=n),
            service.state_root / "spill",
        )
        for n in (0, 50, 100)
    ]
    assert [len(p["rows"]) for p in pages] == [50, 50, 23]
    assert [p["has_more"] for p in pages] == [True, True, False]
    assert [row[0] for p in pages for row in p["rows"]] == list(range(123))
    assert service.workspace(snapshot.workspace_id).base_hash == original


def test_preview_rejects_identifiers_and_unbounded_requests(data):
    service, snapshot = data
    with pytest.raises(ValueError):
        preview_table(
            snapshot,
            TablePreviewRequest(
                table_id=snapshot.tables[0].id, column_ids=["name\" FROM read_csv('secret') --"]
            ),
            service.state_root / "spill",
        )
    for kwargs in [{"page_size": 1000}, {"offset": -1}, {"column_ids": ["x"] * 41}]:
        with pytest.raises(ValidationError):
            TablePreviewRequest(table_id="orders", **kwargs)


class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.position = 0
        self.description = [("Name", str), ("Value", int)]
        self.calls = []
        self.closed = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def close(self):
        self.closed += 1

    def execute(self, query, params=()):
        self.calls.append((query, params))
        self.position = 0
        return self

    def fetchone(self):
        return ("Sales",)

    def fetchmany(self, count):
        result = self.rows[self.position : self.position + count]
        self.position += len(result)
        return result


@pytest.fixture
def remote(monkeypatch):
    cursor = Cursor([("A", 12), ("B", 34)])

    class Connection:
        def cursor(self):
            return cursor

    @contextmanager
    def connect(_):
        yield Connection()

    monkeypatch.setattr(connections, "connect", connect)
    monkeypatch.setattr(
        connections.psutil,
        "virtual_memory",
        lambda: type("Memory", (), {"available": 4 * 1024**3})(),
    )
    return cursor


def test_database_import_keeps_provenance_and_replays(data, remote):
    service, before = data
    w = service.workspace(before.workspace_id)
    request = DatabaseImportRequest(
        workspace_id=w.id,
        source=DatabaseSource(provider="snowflake", profile="qa", schema_name="PUBLIC"),
        tables=["Sales"],
        max_rows=10,
        expected_base_hash=w.base_hash,
        idempotency_key="remote-data-qa",
    )
    value = service.import_database(request)
    assert value.tables[0].table.name == "Sales"
    assert value.tables[0].table.row_count == 2
    assert service.snapshot(before.id).id == before.id
    assert service.import_database(request).id == value.id
    assert len(list(service.state_root.glob("database-extracts/*/source-receipt.json"))) == 1
    assert all(q.startswith("SELECT") for q, _ in remote.calls)
    assert any("LIMIT 11" in q for q, _ in remote.calls)


def test_over_limit_never_activates_partial_snapshot(data, remote):
    service, before = data
    w = service.workspace(before.workspace_id)
    with pytest.raises(ValueError, match="exceeds"):
        service.import_database(
            DatabaseImportRequest(
                workspace_id=w.id,
                source=DatabaseSource(provider="snowflake", profile="qa"),
                tables=["Sales"],
                max_rows=1,
                expected_base_hash=w.base_hash,
                idempotency_key="remote-limit-qa",
            )
        )
    assert service.workspace(w.id).active_snapshot_id == before.id
    receipt = next(service.state_root.glob("database-extracts/*/source-receipt.json"))
    assert '"status": "failed"' in receipt.read_text()


def test_stale_import_does_not_query(data, remote):
    service, snapshot = data
    with pytest.raises(StateConflictError):
        service.import_database(
            DatabaseImportRequest(
                workspace_id=snapshot.workspace_id,
                source=DatabaseSource(provider="sqlserver", profile="qa"),
                tables=["Sales"],
                expected_base_hash=EMPTY_HASH,
                idempotency_key="remote-stale-qa",
            )
        )
    assert remote.calls == []


def test_quoted_remote_names_and_bounded_preview(remote):
    request = DatabaseTableRequest(
        source=DatabaseSource(provider="sqlserver", profile="qa"), table='O"Brien;DROP TABLE X'
    )
    result = connections.preview(request)
    assert len(result["rows"]) == 2
    assert '[O"Brien;DROP TABLE X]' in remote.calls[-1][0]
    assert "TOP (50)" in remote.calls[-1][0]
    assert remote.closed == 1


def test_connection_strings_cannot_be_injected():
    with pytest.raises(ValidationError):
        DatabaseSource(provider="sqlserver", profile="qa;UID=admin;PWD=oops")


def test_new_endpoints_require_authentication(data):
    from fastapi.testclient import TestClient

    from ai_powerbi_analyst.studio.api import create_app

    service, snapshot = data
    client = TestClient(create_app(service, "data-workbench-test-token"))
    path = f"/v1/snapshots/{snapshot.id}/preview"
    payload = {"table_id": snapshot.tables[0].id}
    assert client.post(path, json=payload).status_code == 401
    headers = {"Authorization": "Bearer data-workbench-test-token"}
    assert len(client.post(path, json=payload, headers=headers).json()["rows"]) == 50
    assert (
        client.post(path, json={**payload, "page_size": 5000}, headers=headers).status_code == 422
    )
    assert client.get("/v1/connections/drivers").status_code == 401


def test_profile_column_identifiers_do_not_collide():
    import pandas as pd

    from ai_powerbi_analyst.data.onboarding import _profile_frames

    profile = _profile_frames(
        [pd.DataFrame({"Customer ID": [1], "Customer-ID": [2], "Customer_ID_2": [3]})],
        "customers",
        "Customers",
        sheet=None,
    )
    ids = [c.id for c in profile.columns]
    assert len(set(ids)) == 3


def test_schema_search_uses_parameters(remote):
    connections.browse(
        DatabaseBrowseRequest(
            source=DatabaseSource(provider="snowflake", profile="qa"), search="x' OR 1=1 --"
        )
    )
    query, params = remote.calls[0]
    assert "x' OR" not in query and "LIMIT 201" in query
    assert "x' or" in params[1]


def test_database_text_ids_and_empty_tables(data, remote):
    service, before = data
    remote.rows = [("00123", 5), ("00001", 8)]
    w = service.workspace(before.workspace_id)
    request = DatabaseImportRequest(
        workspace_id=w.id,
        source=DatabaseSource(provider="sqlserver", profile="qa"),
        tables=["Sales"],
        expected_base_hash=w.base_hash,
        idempotency_key="typed-text-id-qa",
    )
    value = service.import_database(request)
    result = preview_table(
        value, TablePreviewRequest(table_id=value.tables[0].id), service.state_root / "spill"
    )
    assert result["rows"] == [["00123", 5], ["00001", 8]]
    assert value.tables[0].table.columns[0].data_type.value == "string"
    remote.rows = []
    w = service.workspace(w.id)
    empty = service.import_database(
        request.model_copy(
            update={"expected_base_hash": w.base_hash, "idempotency_key": "empty-remote-table-qa"}
        )
    )
    assert empty.tables[0].table.row_count == 0
    assert empty.tables[0].table.columns[1].data_type.value == "integer"
