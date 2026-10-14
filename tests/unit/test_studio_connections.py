"""Connector behavior is tested with fake SDKs and isolated extracts, never credentials."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from ai_powerbi_analyst.contracts import ColumnDataType
from ai_powerbi_analyst.studio import connections
from ai_powerbi_analyst.studio.connections import (
    DatabaseImportRequest,
    DatabaseSource,
    DatabaseSourceError,
    DatabaseTableRequest,
)
from ai_powerbi_analyst.studio.ingestion import StudioIngestionService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


def _driver(monkeypatch, *, native=("ODBC Driver 18 for SQL Server",), load_error=None):
    attempts = []
    closes = []
    connection = SimpleNamespace(close=lambda: closes.append(True))

    def connect(*args, **kwargs):
        attempts.append((args, kwargs))
        return connection

    def import_module(name):
        if load_error:
            raise load_error
        assert name in {"snowflake.connector", "pyodbc"}
        return SimpleNamespace(connect=connect, drivers=lambda: list(native))

    monkeypatch.setattr(connections.importlib, "import_module", import_module)
    monkeypatch.setattr(connections.importlib.util, "find_spec", lambda name: object())
    return attempts, closes, connection


def test_readiness_checks_sdk_and_native_prerequisites_without_connecting(monkeypatch):
    attempts, _, _ = _driver(monkeypatch, native=("SQL Server",))
    snowflake, sqlserver = connections.drivers()
    assert snowflake["installed"] and snowflake["ready"]
    assert "sign-in" in snowflake["detail"]
    assert sqlserver["installed"] and not sqlserver["ready"]
    assert "ODBC Driver 17 or 18" in sqlserver["detail"]
    assert attempts == []


def test_readiness_rejects_namespace_without_connector_and_hides_load_error(monkeypatch):
    secret = "account=private;password=secret;C:/private/profile.toml"
    _driver(monkeypatch, load_error=OSError(secret))
    rows = connections.drivers()
    assert all(row["installed"] and not row["ready"] for row in rows)
    assert secret not in json.dumps(rows)

    def missing(name):
        if name == "snowflake.connector":
            raise ModuleNotFoundError("snowflake.connector")
        return None

    monkeypatch.setattr(connections.importlib.util, "find_spec", missing)
    assert not any(row["installed"] for row in connections.drivers())


@pytest.mark.parametrize("provider", ["sqlserver", "snowflake"])
def test_connect_closes_and_releases_slot_after_body_failure(monkeypatch, provider):
    attempts, closes, connection = _driver(monkeypatch)
    source = DatabaseSource(provider=provider, profile="isolated test")
    with (
        pytest.raises(DatabaseSourceError, match="Database request failed") as failure,
        connections.connect(source),
    ):
        raise RuntimeError("password=do-not-expose")
    assert "do-not-expose" not in str(failure.value)
    assert closes == [True]
    with connections.connect(source) as reopened:
        assert reopened is connection
    assert len(attempts) == 2 and len(closes) == 2
    if provider == "sqlserver":
        assert "Encrypt=yes;TrustServerCertificate=no;" in attempts[0][0][0]
        assert connection.timeout == 60
    else:
        assert attempts[0][1]["connection_name"] == "isolated test"
        assert attempts[0][1]["client_session_keep_alive"] is False


def test_missing_supported_native_driver_does_not_attempt_connection(monkeypatch):
    attempts, closes, _ = _driver(monkeypatch, native=("SQL Server",))
    with (
        pytest.raises(DatabaseSourceError, match="ODBC Driver 17 or 18"),
        connections.connect(DatabaseSource(provider="sqlserver", profile="isolated")),
    ):
        pytest.fail("Missing driver must not reach the connection body")
    assert not attempts and not closes


def test_provider_schema_defaults_and_invalid_names():
    assert DatabaseSource(provider="sqlserver", profile="test").schema_name == "dbo"
    assert DatabaseSource(provider="snowflake", profile="test").schema_name == "PUBLIC"
    for kwargs in [{"profile": "   "}, {"schema_name": "a\nb"}]:
        with pytest.raises(ValidationError):
            DatabaseSource(provider="snowflake", **{"profile": "test", **kwargs})
    for name in ["", "x" * 129, "a\x00b"]:
        with pytest.raises(ValidationError):
            DatabaseTableRequest(
                source=DatabaseSource(provider="snowflake", profile="test"), table=name
            )


def test_sql_identifiers_do_not_depend_on_quoted_identifier_setting():
    source = DatabaseSource(provider="sqlserver", profile="test", schema_name="a]b")
    assert connections._select(source, 'O"Brien] ?;DROP X', 51) == (
        'SELECT TOP (51) * FROM [a]]b].[O"Brien]] ?;DROP X]'
    )
    assert connections.quote('O"Brien?') == '"O""Brien?"'


def test_declared_decimal_integer_precision_is_exact_or_text():
    source = DatabaseSource(provider="sqlserver", profile="test")
    types = connections.declared_types(
        source,
        [
            ("Safe ID", Decimal, None, None, 18, 0, True),
            ("Wide ID", Decimal, None, None, 38, 0, True),
            ("Amount", Decimal, None, None, 38, 4, True),
        ],
    )
    assert types == {
        "Safe ID": ColumnDataType.INTEGER,
        "Wide ID": ColumnDataType.STRING,
        "Amount": ColumnDataType.NUMBER,
    }


@pytest.mark.parametrize("failure", ["later_table", "receipt"])
def test_failed_extract_removes_partial_tables_and_releases_cursor(monkeypatch, tmp_path, failure):
    class Cursor:
        description = [("Value", int)]

        def __init__(self):
            self.selected = 0
            self.fetched = False
            self.closed = False

        def execute(self, query, params=()):
            if "TOP" in query:
                self.selected += 1
                self.fetched = False

        def fetchone(self):
            return ("Sales",)

        def fetchmany(self, count):
            if self.fetched:
                return []
            self.fetched = True
            return [(1,)] * (2 if self.selected == 2 else 1)

        def close(self):
            self.closed = True

    cursor = Cursor()

    @contextmanager
    def fake_connect(source):
        yield SimpleNamespace(cursor=lambda: cursor)

    monkeypatch.setattr(connections, "connect", fake_connect)
    monkeypatch.setattr(
        connections.psutil, "virtual_memory", lambda: SimpleNamespace(available=2**32)
    )
    if failure == "receipt":
        original = connections.Path.write_text

        def fail_receipt(path, *args, **kwargs):
            if path.name == "source-receipt.json":
                raise OSError("isolated receipt disk failure")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(connections.Path, "write_text", fail_receipt)
    request = DatabaseImportRequest(
        workspace_id="workspace-qa",
        source=DatabaseSource(provider="sqlserver", profile="isolated"),
        tables=["First", "Second"] if failure == "later_table" else ["First"],
        max_rows=1,
        expected_base_hash=EMPTY_HASH,
        idempotency_key="failed-extract",
    )
    with pytest.raises((DatabaseSourceError, OSError)):
        connections.extract(request, tmp_path)
    assert cursor.closed
    root = next((tmp_path / "database-extracts").iterdir())
    assert list(root.glob("*.parquet")) == []
    if failure == "later_table":
        assert json.loads((root / "source-receipt.json").read_text())["status"] == "failed"


def test_duplicate_selection_fails_before_connecting_or_creating_extract(monkeypatch, tmp_path):
    def unexpected(*args, **kwargs):
        pytest.fail("Duplicate table selection must fail before connecting")

    monkeypatch.setattr(connections, "connect", unexpected)
    request = DatabaseImportRequest(
        workspace_id="workspace-qa",
        source=DatabaseSource(provider="sqlserver", profile="isolated"),
        tables=["Sales", "Sales"],
        expected_base_hash=EMPTY_HASH,
        idempotency_key="duplicate-selection",
    )
    with pytest.raises(DatabaseSourceError, match="each source table once"):
        connections.extract(request, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_fractional_decimal_extract_preserves_values_across_batches_and_explains_text(
    monkeypatch, tmp_path
):
    class Cursor:
        description = [
            ("ID", Decimal, None, None, 18, 0, True),
            ("Exact amount", Decimal, None, None, 38, 4, True),
            ("Ordinary amount", Decimal, None, None, 12, 2, True),
        ]

        def __init__(self):
            self.batches = iter(
                [
                    [(Decimal("123456789012345678"), Decimal("1.25"), Decimal("1.25"))],
                    [(None, Decimal("12345678901234.5678"), Decimal("2.50"))],
                    [],
                ]
            )
            self.closed = False

        def execute(self, query, params=()):
            assert query.startswith("SELECT")

        def fetchone(self):
            return ("Sales",)

        def fetchmany(self, count):
            return next(self.batches)

        def close(self):
            self.closed = True

    cursor = Cursor()

    @contextmanager
    def fake_connect(source):
        yield SimpleNamespace(cursor=lambda: cursor)

    monkeypatch.setattr(connections, "connect", fake_connect)
    monkeypatch.setattr(
        connections.psutil, "virtual_memory", lambda: SimpleNamespace(available=2**32)
    )
    request = DatabaseImportRequest(
        workspace_id="workspace-qa",
        source=DatabaseSource(provider="sqlserver", profile="isolated"),
        tables=["Sales"],
        max_rows=10,
        expected_base_hash=EMPTY_HASH,
        idempotency_key="precise-extract",
    )
    items, root = connections.extract(request, tmp_path)
    receipt = json.loads((root / "source-receipt.json").read_text())
    assert cursor.closed
    assert receipt["column_types"]["Sales"] == {
        "ID": "integer",
        "Exact amount": "string",
        "Ordinary amount": "number",
    }
    warnings = receipt["type_warnings"]["Sales"]
    assert "preserve decimal precision" in warnings["Exact amount"]
    path = items[0].absolute_path
    snapshot, _ = StudioIngestionService(tmp_path / "state").ingest(
        request.workspace_id,
        items,
        column_types={
            path: connections.declared_types(request.source, cursor.description)
            | {"Exact amount": ColumnDataType.STRING}
        },
        column_type_warnings={path: warnings},
    )
    values = pq.read_table(snapshot.tables[0].normalized_parquet_path).to_pydict()
    assert values == {
        "ID": [123456789012345678, None],
        "Exact amount": ["1.25", "12345678901234.5678"],
        "Ordinary amount": [1.25, 2.5],
    }
    assert any("preserve decimal precision" in finding.message for finding in snapshot.findings)


@pytest.mark.parametrize("mixed_naive", [False, True])
def test_timestamp_zone_handling_preserves_dst_instants_and_rejects_missing_zone(
    monkeypatch, tmp_path, mixed_naive
):
    first = datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=-5)))
    second = datetime(2026, 7, 1, 12, tzinfo=timezone(timedelta(hours=-4)))
    if mixed_naive:
        second = second.replace(tzinfo=None)

    class Cursor:
        description = [
            ("Observed at ", datetime, None, None, None, None, True),
            ("Naive", datetime, None, None, None, None, True),
        ]

        def __init__(self):
            self.batches = iter(
                [
                    [(first, datetime(2026, 1, 1, 12))],
                    [(second, datetime(2026, 7, 1, 12, 0, 0, 123456))],
                    [],
                ]
            )

        def execute(self, query, params=()):
            pass

        def fetchone(self):
            return ("Sales",)

        def fetchmany(self, count):
            return next(self.batches)

        def close(self):
            pass

    @contextmanager
    def fake_connect(source):
        yield SimpleNamespace(cursor=lambda: Cursor())

    monkeypatch.setattr(connections, "connect", fake_connect)
    monkeypatch.setattr(
        connections.psutil, "virtual_memory", lambda: SimpleNamespace(available=2**32)
    )
    request = DatabaseImportRequest(
        workspace_id="workspace-qa",
        source=DatabaseSource(provider="sqlserver", profile="isolated"),
        tables=["Sales"],
        max_rows=10,
        expected_base_hash=EMPTY_HASH,
        idempotency_key="zoned-extract",
    )
    if mixed_naive:
        with pytest.raises(DatabaseSourceError, match="one explicit time zone"):
            connections.extract(request, tmp_path)
        assert list((tmp_path / "database-extracts").glob("*/*.parquet")) == []
        return
    items, root = connections.extract(request, tmp_path)
    receipt = json.loads((root / "source-receipt.json").read_text())
    assert receipt["utc_columns"]["Sales"] == ["Observed at "]
    path = items[0].absolute_path
    service = StudioIngestionService(tmp_path / "state")
    original_frames = service._frames

    def separate_batches(*args, **kwargs):
        frames = original_frames(*args, **kwargs)
        name, sheet, factory = frames[0]

        def split():
            for frame in factory():
                for index in range(len(frame)):
                    yield frame.iloc[[index]]

        return [(name, sheet, split)]

    monkeypatch.setattr(service, "_frames", separate_batches)
    snapshot, _ = service.ingest(
        request.workspace_id,
        items,
        column_types={
            path: {
                name: ColumnDataType(kind)
                for name, kind in receipt["column_types"]["Sales"].items()
            }
        },
        column_type_warnings={path: receipt["type_warnings"]["Sales"]},
        utc_columns={path: set(receipt["utc_columns"]["Sales"])},
    )
    values = pq.read_table(snapshot.tables[0].normalized_parquet_path).to_pydict()
    assert values["Observed at"] == [
        first.astimezone(UTC),
        second.astimezone(UTC),
    ]
    assert values["Naive"] == [datetime(2026, 1, 1, 12), datetime(2026, 7, 1, 12, 0, 0, 123456)]
    assert all(value.tzinfo is None for value in values["Naive"])
    assert any("normalized to UTC" in finding.message for finding in snapshot.findings)
