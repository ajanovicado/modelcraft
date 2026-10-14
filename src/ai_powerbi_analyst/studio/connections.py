"""Explicit read-only database sources. Credentials stay in vendor-managed profiles.

No user/model SQL is accepted. Every result is bounded at the server and fetched
in batches. Imports produce immutable local snapshots, never DirectQuery models.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import shutil
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from threading import BoundedSemaphore
from typing import Annotated, Any, Literal

import psutil
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import Field, field_validator

from ai_powerbi_analyst.contracts import ClosedModel, ColumnDataType

from .contracts import IdempotentMutation, SourceInventoryItem, StudioId
from .inventory import inventory_folder

_CONNECTION_SLOTS = BoundedSemaphore(1)
DatabaseIdentifier = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$")
]


class DatabaseSourceError(ValueError):
    """Sanitized, actionable data-source error."""


def _default_schema(data: dict[str, Any]) -> str:
    return "dbo" if data.get("provider") == "sqlserver" else "PUBLIC"


class DatabaseSource(ClosedModel):
    provider: Literal["snowflake", "sqlserver"]
    profile: str = Field(min_length=1, max_length=120, pattern=r"^[\w .-]+$")
    schema_name: str = Field(default_factory=_default_schema, min_length=1, max_length=128)

    @field_validator("profile", "schema_name")
    @classmethod
    def usable_name(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Use a nonempty name without control characters")
        return value


class DatabaseBrowseRequest(ClosedModel):
    source: DatabaseSource
    search: str = Field(default="", max_length=100)


class DatabaseTableRequest(ClosedModel):
    source: DatabaseSource
    table: DatabaseIdentifier


class DatabaseImportRequest(IdempotentMutation):
    workspace_id: StudioId
    source: DatabaseSource
    tables: list[DatabaseIdentifier] = Field(min_length=1, max_length=10)
    max_rows: int = Field(default=100_000, ge=1, le=1_000_000)


def drivers() -> list[dict[str, Any]]:
    """Report local prerequisites, never test credentials or enumerate profiles/DSNs."""
    result = []
    for provider, name, module_name in [
        ("snowflake", "Snowflake", "snowflake.connector"),
        ("sqlserver", "SQL Server / Azure SQL", "pyodbc"),
    ]:
        try:
            installed = importlib.util.find_spec(module_name) is not None
        except (ImportError, ValueError, OSError):
            installed = False
        ready = False
        detail = "Install this app's database extras and restart to load the Python connector."
        if installed:
            try:
                _load_driver(provider)
                ready = True
                detail = (
                    "Python connector available. A configured Snowflake profile and sign-in "
                    "are still required."
                    if provider == "snowflake"
                    else "Microsoft ODBC Driver 17 or 18 for SQL Server available. A configured "
                    "DSN, certificate validation and sign-in are still required."
                )
            except DatabaseSourceError as error:
                detail = str(error)
        result.append(
            {"id": provider, "name": name, "installed": installed, "ready": ready, "detail": detail}
        )
    return result


def _load_driver(provider: str) -> Any:
    try:
        module = importlib.import_module(
            "snowflake.connector" if provider == "snowflake" else "pyodbc"
        )
    except Exception as error:
        # Native load failures can include local paths or sensitive environment values.
        raise DatabaseSourceError(
            "The Python database connector could not load. Install this app's database extras "
            "and restart."
        ) from error
    if provider == "sqlserver":
        try:
            supported = any(
                re.fullmatch(r"ODBC Driver (?:17|18) for SQL Server", name, re.I)
                for name in module.drivers()
            )
        except Exception as error:
            raise DatabaseSourceError(
                "SQL Server ODBC drivers could not be checked. Install Microsoft ODBC Driver "
                "17 or 18 for SQL Server for this app's architecture."
            ) from error
        if not supported:
            raise DatabaseSourceError(
                "Install Microsoft ODBC Driver 17 or 18 for SQL Server for this app's "
                "architecture, then configure a DSN using that driver."
            )
    return module


@contextmanager
def connect(source: DatabaseSource) -> Iterator[Any]:
    connection = None
    if not _CONNECTION_SLOTS.acquire(blocking=False):
        raise DatabaseSourceError("Another database request is running. Wait for it to finish.")
    try:
        if source.provider == "snowflake":
            module = _load_driver(source.provider)
            connection = module.connect(
                connection_name=source.profile,
                login_timeout=20,
                network_timeout=30,
                socket_timeout=30,
                client_session_keep_alive=False,
                client_prefetch_threads=1,
                session_parameters={
                    "STATEMENT_TIMEOUT_IN_SECONDS": 60,
                    "STATEMENT_QUEUED_TIMEOUT_IN_SECONDS": 20,
                    "QUERY_TAG": "AI_Analyst_Studio_read_only_import",
                },
            )
        else:
            module = _load_driver(source.provider)
            # DSN name cannot contain connection-string separators (validated above).
            connection = module.connect(
                f"DSN={source.profile};ApplicationIntent=ReadOnly;"
                "Encrypt=yes;TrustServerCertificate=no;",
                timeout=20,
                autocommit=True,
            )
            connection.timeout = 60
        yield connection
    except ImportError as error:
        raise DatabaseSourceError(
            "Database driver is not installed. Install the database extras and restart the app."
        ) from error
    except DatabaseSourceError:
        raise
    except Exception as error:
        # Vendor errors may contain account/user names or connection strings.
        raise DatabaseSourceError(
            "Database request failed. Check the configured profile, sign-in, schema "
            "and read permissions. Queries have a 60-second limit."
        ) from error
    finally:
        if connection is not None:
            # Vendor cleanup errors can contain credentials; release our slot regardless.
            with suppress(Exception):
                connection.close()
        _CONNECTION_SLOTS.release()


@contextmanager
def _cursor(connection: Any) -> Iterator[Any]:
    cursor = connection.cursor()
    try:
        yield cursor
    finally:
        # DB-API context managers do not uniformly close cursors (notably pyodbc).
        with suppress(Exception):
            cursor.close()


def quote(name: str, *, sqlserver: bool = False) -> str:
    if not name or len(name) > 128 or any(ord(char) < 32 for char in name):
        raise DatabaseSourceError("Invalid database identifier")
    if sqlserver:
        return "[" + name.replace("]", "]]") + "]"
    return '"' + name.replace('"', '""') + '"'


def _execute(cursor: Any, source: DatabaseSource, query: str, params: tuple[Any, ...] = ()) -> Any:
    if source.provider == "snowflake":
        query = query.replace("?", "%s")
    return cursor.execute(query, params)


def browse(request: DatabaseBrowseRequest) -> dict[str, Any]:
    source = request.source
    with connect(source) as connection, _cursor(connection) as cursor:
        prefix = "SELECT TOP (201)" if source.provider == "sqlserver" else "SELECT"
        suffix = " LIMIT 201" if source.provider == "snowflake" else ""
        search_clause = (
            "CHARINDEX(LOWER(?), LOWER(TABLE_NAME)) > 0"
            if source.provider == "sqlserver"
            else "POSITION(LOWER(?) IN LOWER(TABLE_NAME)) > 0"
        )
        _execute(
            cursor,
            source,
            f"{prefix} TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
            f"WHERE TABLE_SCHEMA = ? AND {search_clause} "
            f"ORDER BY TABLE_NAME{suffix}",
            (
                source.schema_name,
                request.search.lower(),
            ),
        )
        rows = cursor.fetchmany(201)
    return {
        "tables": [{"name": str(row[0]), "kind": str(row[1])} for row in rows[:200]],
        "has_more": len(rows) > 200,
    }


def _check_table(cursor: Any, source: DatabaseSource, name: str) -> None:
    _execute(
        cursor,
        source,
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
        (source.schema_name, name),
    )
    if cursor.fetchone() is None:
        raise DatabaseSourceError("This table is no longer available in the selected schema")


def _select(source: DatabaseSource, table: str, limit: int) -> str:
    sqlserver = source.provider == "sqlserver"
    target = f"{quote(source.schema_name, sqlserver=sqlserver)}.{quote(table, sqlserver=sqlserver)}"
    return (
        f"SELECT TOP ({int(limit)}) * FROM {target}"  # noqa: S608
        if source.provider == "sqlserver"
        else f"SELECT * FROM {target} LIMIT {int(limit)}"  # noqa: S608
    )  # noqa: S608


def declared_types(source: DatabaseSource, description: Any) -> dict[str, ColumnDataType]:
    """Honor database text types, including numeric-looking customer IDs."""
    result = {}
    for item in description:
        code = item[1]
        if source.provider == "snowflake" and isinstance(code, int):
            constants = importlib.import_module("snowflake.connector.constants")
            name = (
                constants.FIELD_TYPES[code].name
                if 0 <= code < len(constants.FIELD_TYPES)
                else "UNKNOWN"
            )
            kind = {
                "TEXT": "string",
                "REAL": "number",
                "BOOLEAN": "boolean",
                "DATE": "date",
                "TIMESTAMP": "datetime",
                "TIMESTAMP_LTZ": "datetime",
                "TIMESTAMP_TZ": "datetime",
                "TIMESTAMP_NTZ": "datetime",
            }.get(name, "string")
            if name == "FIXED":
                kind = "number" if item[5] else "integer" if (item[4] or 38) <= 18 else "string"
        else:
            kind = {
                str: "string",
                int: "integer",
                float: "number",
                Decimal: "number",
                bool: "boolean",
                date: "date",
                datetime: "datetime",
            }.get(code, "string")
            if code is Decimal and len(item) > 5 and item[5] == 0:
                # ODBC represents DECIMAL(p,0) as Decimal. Integer identifiers wider
                # than Int64 must stay text instead of silently rounding through Float64.
                kind = "integer" if isinstance(item[4], int) and item[4] <= 18 else "string"
        result[str(item[0])] = ColumnDataType(kind)
    return result


def _preserve_decimal_precision(
    names: list[str], batch: Any, types: dict[str, ColumnDataType], warnings: dict[str, str]
) -> None:
    for index, name in enumerate(names):
        if types[name] is not ColumnDataType.NUMBER:
            continue
        if any(
            isinstance(row[index], Decimal)
            and (not row[index].is_finite() or Decimal(str(float(row[index]))) != row[index])
            for row in batch
            if row[index] is not None
        ):
            types[name] = ColumnDataType.STRING
            warnings[name] = (
                f"{name} was imported as text to preserve decimal precision; "
                "numeric normalization would change at least one source value."
            )


def _record_timestamp_zones(
    names: list[str],
    batch: Any,
    types: dict[str, ColumnDataType],
    modes: dict[str, bool],
    utc_columns: list[str],
    warnings: dict[str, str],
) -> None:
    for index, name in enumerate(names):
        if types[name] is not ColumnDataType.DATETIME:
            continue
        for row in batch:
            value = row[index]
            if not isinstance(value, datetime):
                continue
            aware = value.tzinfo is not None and value.utcoffset() is not None
            if name in modes and modes[name] != aware:
                raise DatabaseSourceError(
                    f"{name} mixes timestamps with and without time zones. Use a source view "
                    "with one explicit time zone before importing."
                )
            modes[name] = aware
            if aware and name not in utc_columns:
                utc_columns.append(name)
                warnings[name] = (
                    f"{name}: timezone-aware timestamps were normalized to UTC, "
                    "preserving their instants."
                )


def preview(request: DatabaseTableRequest) -> dict[str, Any]:
    with connect(request.source) as connection, _cursor(connection) as cursor:
        _check_table(cursor, request.source, request.table)
        cursor.execute(_select(request.source, request.table, 50))
        names = [str(item[0]) for item in cursor.description]
        if len(names) > 200:
            raise DatabaseSourceError(
                "This table is too wide. Create a database view with at most 200 columns."
            )
        rows = [
            [None if value is None else str(value)[:500] for value in row[:40]]
            for row in cursor.fetchmany(50)
        ]
    return {"columns": names[:40], "rows": rows, "limited": True}


def extract(
    request: DatabaseImportRequest, state_root: Path
) -> tuple[list[SourceInventoryItem], Path]:
    """Fail rather than silently import a truncated subset; never publish partial tables."""
    if len(set(request.tables)) != len(request.tables):
        raise DatabaseSourceError("Select each source table once")
    root = state_root / "database-extracts" / f"extract-{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    receipt: dict[str, Any] = {
        "source": request.source.model_dump(),
        "tables": request.tables,
        "max_rows_per_table": request.max_rows,
        "started_at": datetime.now(UTC).isoformat(),
        "mode": "immutable_import",
        "status": "running",
        "row_counts": {},
        "column_types": {},
        "type_warnings": {},
        "utc_columns": {},
    }
    try:
        with connect(request.source) as connection, _cursor(connection) as cursor:
            for index, name in enumerate(request.tables):
                _check_table(cursor, request.source, name)
                cursor.execute(_select(request.source, name, request.max_rows + 1))
                names = [str(item[0]) for item in cursor.description]
                receipt["column_types"][name] = declared_types(request.source, cursor.description)
                receipt["type_warnings"][name] = {}
                receipt["utc_columns"][name] = []
                timestamp_modes: dict[str, bool] = {}
                if not names or len(names) > 200 or len(set(names)) != len(names):
                    raise DatabaseSourceError("Use a source view with 1–200 uniquely named columns")
                path = root / f"{index + 1:02d}.parquet"
                writer = None
                count = 0
                byte_count = 0
                try:
                    while True:
                        if time.monotonic() - started > 180:
                            raise DatabaseSourceError(
                                "Import reached its three-minute limit. "
                                "Select fewer tables or a smaller source view."
                            )
                        if psutil.virtual_memory().available < 1024**3:
                            raise DatabaseSourceError(
                                "Import stopped safely: at least 1 GiB free RAM "
                                "is required for data extraction."
                            )
                        if shutil.disk_usage(root).free < 1024**3:
                            raise DatabaseSourceError(
                                "Import stopped safely: at least 1 GiB free disk is required."
                            )
                        batch = cursor.fetchmany(2_000)
                        if not batch:
                            break
                        count += len(batch)
                        if count > request.max_rows:
                            raise DatabaseSourceError(
                                f"{name} exceeds the selected {request.max_rows:,}-row limit. "
                                "Use a filtered source view or raise the import limit. "
                                "No partial snapshot was activated."
                            )
                        _preserve_decimal_precision(
                            names,
                            batch,
                            receipt["column_types"][name],
                            receipt["type_warnings"][name],
                        )
                        _record_timestamp_zones(
                            names,
                            batch,
                            receipt["column_types"][name],
                            timestamp_modes,
                            receipt["utc_columns"][name],
                            receipt["type_warnings"][name],
                        )
                        # String staging preserves vendor decimal/date text across batches;
                        # existing normalization then profiles every value deterministically.
                        if any(
                            value is not None and len(str(value)) > 100_000
                            for row in batch
                            for value in row
                        ):
                            raise DatabaseSourceError(
                                "A value exceeds the 100 KB cell limit. Use a narrower source view."
                            )
                        arrays = [
                            pa.array(
                                [None if row[j] is None else str(row[j]) for row in batch],
                                type=pa.string(),
                            )
                            for j in range(len(names))
                        ]
                        table = pa.Table.from_arrays(arrays, names=names)
                        byte_count += table.nbytes
                        if byte_count > 256 * 1024**2:
                            raise DatabaseSourceError(
                                "Table exceeds the 256 MiB extraction limit. "
                                "Select a smaller source view."
                            )
                        if writer is None:
                            writer = pq.ParquetWriter(path, table.schema, compression="zstd")
                        writer.write_table(table)
                    if writer is None:
                        pq.write_table(
                            pa.table({key: pa.array([], type=pa.string()) for key in names}), path
                        )
                finally:
                    if writer is not None:
                        writer.close()
                receipt["row_counts"][name] = count
        receipt["status"] = "extracted"
        items = inventory_folder(root)
        receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (root / "source-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        return [item for item in items if item.selectable], root
    except Exception:
        receipt["status"] = "failed"
        # Keep diagnostic metadata, but do not leave completed earlier tables
        # behind when a later table or the receipt fails to finish the import.
        for path in root.glob("*.parquet"):
            with suppress(OSError):
                path.unlink()
        receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
        with suppress(OSError):
            (root / "source-receipt.json").write_text(
                json.dumps(receipt, indent=2), encoding="utf-8"
            )
        raise
