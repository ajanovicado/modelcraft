from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ai_powerbi_analyst.studio import pbix_extract
from ai_powerbi_analyst.studio.pbix_extract import metadata_types

# (ID, Name, SystemFlags). Date is a CALENDAR() table (flag 2, a real model table);
# H$/R$/U$ names and flag-1 tables are engine internals.
TABLES = [
    (1, "FactSales", 0),
    (2, "Date", 2),
    (3, "# Measures", 0),
    (4, "H$FactSales (1)$StoreKey (11)", 1),
    (5, "Engine Owned", 3),
]
# (ID, TableID, ExplicitName, InferredName, ExplicitDataType, InferredDataType, SummarizeBy,
#  IsHidden, IsKey, Type). Codes are the values read from the Agents report: data types
# 1 automatic, 2 text, 6 whole, 8 float, 9 date/time, 11 true/false, 19 unknown;
# SummarizeBy 1 default, 2 none, 3 sum, 7 average; Type 1 data, 3 row number,
# 4 calculated-table column.
COLUMNS = [
    (10, 1, "RowNumber-2662979B", None, 6, 19, 1, 1, 1, 3),
    (11, 1, "SalesKey", None, 6, 19, 2, 0, 0, 1),
    (12, 1, "DateKey", None, 9, 19, 2, 0, 0, 1),
    (13, 1, "SalesAmount", None, 8, 19, 2, 0, 0, 1),
    (14, 1, "Region", None, 2, 19, 2, 0, 0, 1),
    (15, 1, "Margin", None, 10, 19, 3, 0, 0, 1),
    (16, 1, "Score", None, 8, 19, 7, 1, 0, 1),
    (20, 2, None, "Date", 1, 9, 2, 0, 1, 4),
    (21, 2, None, "Year", 1, 6, 2, 0, 0, 4),
    (22, 2, None, "DateWithTransactions", 1, 11, 2, 1, 0, 4),
    (23, 2, None, "Unknown Type", 19, 19, 2, 0, 0, 4),
    (40, 4, "StoreKey", None, 6, 19, 2, 0, 0, 1),
    (50, 5, "Key", None, 6, 19, 2, 0, 0, 1),
    (60, 3, None, None, 6, 19, 2, 0, 0, 1),
]
# (ID, TableID, Name, DataType, IsHidden); 20 is a variant (no fixed type).
MEASURES = [
    (1, 3, "Total Sales", 8, 0),
    (2, 3, "Order Count", 6, 0),
    (3, 3, "Label", 2, 0),
    (4, 2, "_ShowValueForDates", 11, 1),
    (5, 3, "Untyped", 20, 0),
    (6, 4, "Engine Measure", 8, 0),
]

EXPECTED_COLUMNS = [
    ("FactSales", "SalesKey", 6, "none", 2, False, False),
    ("FactSales", "DateKey", 9, "none", 2, False, False),
    ("FactSales", "SalesAmount", 8, "none", 2, False, False),
    ("FactSales", "Region", 2, "none", 2, False, False),
    ("FactSales", "Margin", 10, "sum", 3, False, False),
    ("FactSales", "Score", 8, "average", 7, True, False),
    ("Date", "Date", 9, "none", 2, False, True),
    ("Date", "Year", 6, "none", 2, False, False),
    ("Date", "DateWithTransactions", 11, "none", 2, True, False),
    ("Date", "Unknown Type", None, "none", 2, False, False),
]
COLUMN_KEYS = ("table", "name", "dataType", "summarizeBy", "summarizeByCode", "hidden", "key")


def metadata_db(*, drop_columns: tuple[str, ...] = ()) -> sqlite3.Connection:
    """A metadata database; ``drop_columns`` removes columns to mimic other schemas."""
    column_defs = {
        "ExplicitName": "TEXT",
        "InferredName": "TEXT",
        "ExplicitDataType": "INTEGER",
        "InferredDataType": "INTEGER",
        "SummarizeBy": "INTEGER",
        "IsHidden": "INTEGER",
        "IsKey": "INTEGER",
        "Type": "INTEGER",
    }
    kept = [name for name in column_defs if name not in drop_columns]
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE [Table] (ID INTEGER, Name TEXT, SystemFlags INTEGER)")
    conn.execute(
        "CREATE TABLE [Column] (ID INTEGER, TableID INTEGER, "
        + ", ".join(f"{name} {column_defs[name]}" for name in kept)
        + ")"
    )
    conn.execute(
        "CREATE TABLE Measure (ID INTEGER, TableID INTEGER, Name TEXT, DataType INTEGER, "
        "IsHidden INTEGER)"
    )
    conn.executemany("INSERT INTO [Table] VALUES (?, ?, ?)", TABLES)
    order = [
        "ExplicitName",
        "InferredName",
        "ExplicitDataType",
        "InferredDataType",
        "SummarizeBy",
        "IsHidden",
        "IsKey",
        "Type",
    ]
    for row in COLUMNS:
        values = dict(zip(order, row[2:], strict=True))
        conn.execute(
            f"INSERT INTO [Column] (ID, TableID, {', '.join(kept)}) "  # noqa: S608
            f"VALUES (?, ?, {', '.join('?' for _ in kept)})",
            (row[0], row[1], *(values[name] for name in kept)),
        )
    conn.executemany("INSERT INTO Measure VALUES (?, ?, ?, ?, ?)", MEASURES)
    return conn


class Reader:
    """Raises on SQL errors, unlike PBIXRay's reader; both paths must give None."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def query(self, sql: str) -> pd.DataFrame:
        return pd.read_sql_query(sql, self.conn)


def test_types_of_columns_and_measures():
    found = metadata_types(Reader(metadata_db()))
    assert found is not None
    assert [tuple(c[key] for key in COLUMN_KEYS) for c in found["columns"]] == EXPECTED_COLUMNS
    assert found["measures"] == [
        {"table": "# Measures", "name": "Total Sales", "dataType": 8, "hidden": False},
        {"table": "# Measures", "name": "Order Count", "dataType": 6, "hidden": False},
        {"table": "# Measures", "name": "Label", "dataType": 2, "hidden": False},
        {"table": "Date", "name": "_ShowValueForDates", "dataType": 11, "hidden": True},
        {"table": "# Measures", "name": "Untyped", "dataType": 20, "hidden": False},
    ]


def test_an_automatic_explicit_type_falls_back_to_the_inferred_type():
    found = metadata_types(Reader(metadata_db()))
    assert found is not None
    date_columns = {c["name"]: c["dataType"] for c in found["columns"] if c["table"] == "Date"}
    # ExplicitDataType 1 (automatic) on a calculated-table column: the inferred type counts
    assert date_columns == {"Date": 9, "Year": 6, "DateWithTransactions": 11, "Unknown Type": None}


def test_row_number_engine_and_unnamed_columns_are_left_out():
    found = metadata_types(Reader(metadata_db()))
    assert found is not None
    names = {(c["table"], c["name"]) for c in found["columns"]}
    assert not any(name.startswith("RowNumber") for _, name in names)
    assert not {t for t, _ in names} & {"H$FactSales (1)$StoreKey (11)", "Engine Owned"}
    assert ("# Measures", None) not in names
    assert all(m["table"] != "H$FactSales (1)$StoreKey (11)" for m in found["measures"])


@pytest.mark.parametrize(
    "missing",
    [
        ("SummarizeBy",),
        ("ExplicitDataType", "InferredDataType"),
        ("IsHidden", "IsKey", "Type"),
        ("InferredName",),
    ],
)
def test_a_schema_without_some_columns_still_reads(missing):
    found = metadata_types(Reader(metadata_db(drop_columns=missing)))
    assert found is not None
    by_name = {(c["table"], c["name"]): c for c in found["columns"]}
    if "InferredName" in missing:
        # calculated-table columns have no ExplicitName either, so they drop out
        assert ("Date", "Year") not in by_name
    else:
        assert by_name[("Date", "Year")]["dataType"] == (
            6 if "InferredDataType" not in missing else None
        )
    sales = by_name[("FactSales", "SalesKey")]
    if "SummarizeBy" in missing:
        assert (sales["summarizeBy"], sales["summarizeByCode"]) == (None, None)
    else:
        assert sales["summarizeBy"] == "none"
    if "ExplicitDataType" in missing:
        assert sales["dataType"] is None
    if "Type" in missing:
        # without Type the row-number column cannot be told apart
        assert ("FactSales", "RowNumber-2662979B") in by_name


def test_metadata_without_column_names_is_unknown():
    conn = metadata_db(drop_columns=("ExplicitName", "InferredName"))
    assert metadata_types(Reader(conn)) is None


def test_a_failed_metadata_query_is_none():
    assert metadata_types(Reader(sqlite3.connect(":memory:"))) is None
    # PBIXRay's reader returns an empty, column-less frame instead of raising
    assert metadata_types(SimpleNamespace(query=lambda _sql: pd.DataFrame())) is None


def test_queries_are_fixed_sql_without_user_text():
    seen: list[str] = []

    class Recording(Reader):
        def query(self, sql: str) -> pd.DataFrame:
            seen.append(sql)
            return super().query(sql)

    assert metadata_types(Recording(metadata_db())) is not None
    select = [sql for sql in seen if sql.startswith("SELECT")]
    assert len(select) == 2
    assert "COALESCE(c.ExplicitName, c.InferredName) AS ColumnName" in select[0]
    assert "FROM [Column] c JOIN [Table] t ON c.TableID = t.ID" in select[0]
    assert "c.SummarizeBy AS SummarizeBy" in select[0]
    assert "FROM Measure m JOIN [Table] t ON m.TableID = t.ID" in select[1]
    assert all("?" not in sql and "%" not in sql for sql in select)


def test_the_production_reader_runs_the_same_queries():
    # The production reader is APSW over the serialized metadata database.
    reader_type = pytest.importorskip("pbixray.meta.sqlite_source")._SqliteReader
    reader = reader_type(metadata_db().serialize())
    try:
        found = metadata_types(reader)
    finally:
        reader.close()
    assert found is not None
    assert [tuple(c[key] for key in COLUMN_KEYS) for c in found["columns"]] == EXPECTED_COLUMNS


def test_types_reads_a_pbix_through_pbixray(monkeypatch, tmp_path):
    conn = metadata_db()
    model = SimpleNamespace(_metadata=SimpleNamespace(source=SimpleNamespace(_db=Reader(conn))))

    class FakePBIXRay:
        def __init__(self, path, **_kwargs):
            assert path == str(tmp_path / "report.pbix")

        def __enter__(self):
            return model

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(pbix_extract, "PBIXRay", FakePBIXRay)
    found = pbix_extract.types(tmp_path / "report.pbix")
    assert [c["name"] for c in found["columns"]][:2] == ["SalesKey", "DateKey"]
    assert len(found["measures"]) == 5
    # a model without a reachable metadata database is empty, not an error
    model._metadata.source._db = None
    assert pbix_extract.types(tmp_path / "report.pbix") == {"columns": [], "measures": []}
    model._metadata.source._db = Reader(sqlite3.connect(":memory:"))
    assert pbix_extract.types(tmp_path / "report.pbix") == {"columns": [], "measures": []}


# --- the DAX of calculated columns and tables ---------------------------------------------------

# PBIXRay's frames: dax_tables (TableName, Expression = the partition's QueryDefinition) and
# dax_columns (TableName, ColumnName, Expression of the type-2 columns).
CALCULATED_TABLES = pd.DataFrame(
    [
        {"TableName": "Metric Picker", "Expression": '{ ("Sales", NAMEOF([Total Sales]), 0) }'},
        {"TableName": "Date", "Expression": "CALENDAR(DATE(2019, 1, 1), DATE(2024, 12, 31))"},
        {"TableName": "H$Engine (1)", "Expression": "ROW()"},  # engine storage, never authored
        {"TableName": "No Definition", "Expression": None},
    ]
)
CALCULATED_COLUMNS = pd.DataFrame(
    [
        {"TableName": "FactSales", "ColumnName": "Margin %", "Expression": "[Total Sales] / 2"},
        {"TableName": "FactSales", "ColumnName": "Blank", "Expression": None},
        {"TableName": "R$Engine", "ColumnName": "Key", "Expression": "1"},
    ]
)


def test_calculated_tables_and_columns_map_to_their_dax():
    model = SimpleNamespace(dax_tables=CALCULATED_TABLES, dax_columns=CALCULATED_COLUMNS)
    assert pbix_extract.metadata_calculated(model) == {
        "Metric Picker": '{ ("Sales", NAMEOF([Total Sales]), 0) }',
        "Date": "CALENDAR(DATE(2019, 1, 1), DATE(2024, 12, 31))",
        "FactSales[Margin %]": "[Total Sales] / 2",
    }


def test_no_calculated_objects_is_an_empty_map_not_unknown():
    # PBIXRay's reader gives a column-less frame for "no rows" (and for a SQL error)
    model = SimpleNamespace(dax_tables=pd.DataFrame(), dax_columns=pd.DataFrame())
    assert pbix_extract.metadata_calculated(model) == {}


def test_calculated_objects_are_unknown_when_pbixray_cannot_give_them():
    assert pbix_extract.metadata_calculated(SimpleNamespace()) is None

    class Broken:
        @property
        def dax_tables(self):
            raise RuntimeError("metadata is unreadable")

        dax_columns = CALCULATED_COLUMNS

    assert pbix_extract.metadata_calculated(Broken()) is None


def _fake_pbixray(monkeypatch, model):
    class FakePBIXRay:
        def __init__(self, path, **_kwargs):
            pass

        def __enter__(self):
            return model

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(pbix_extract, "PBIXRay", FakePBIXRay)


def test_types_carries_the_calculated_map_and_leaves_it_out_when_unknown(monkeypatch, tmp_path):
    source = SimpleNamespace(_db=Reader(metadata_db()))
    model = SimpleNamespace(
        _metadata=SimpleNamespace(source=source),
        dax_tables=CALCULATED_TABLES,
        dax_columns=CALCULATED_COLUMNS,
    )
    _fake_pbixray(monkeypatch, model)
    found = pbix_extract.types(tmp_path / "report.pbix")
    assert set(found) == {"columns", "measures", "calculated"}
    assert found["calculated"]["FactSales[Margin %]"] == "[Total Sales] / 2"
    assert len(found["measures"]) == 5
    # without the frames the types stay as they were and "calculated" is absent (unknown)
    del model.dax_tables
    assert set(pbix_extract.types(tmp_path / "report.pbix")) == {"columns", "measures"}
    # a model whose type metadata is unreadable still reports its calculated objects
    model = SimpleNamespace(
        _metadata=SimpleNamespace(source=SimpleNamespace(_db=None)),
        dax_tables=CALCULATED_TABLES,
        dax_columns=pd.DataFrame(),
    )
    _fake_pbixray(monkeypatch, model)
    assert pbix_extract.types(tmp_path / "report.pbix") == {
        "columns": [],
        "measures": [],
        "calculated": {
            "Metric Picker": '{ ("Sales", NAMEOF([Total Sales]), 0) }',
            "Date": "CALENDAR(DATE(2019, 1, 1), DATE(2024, 12, 31))",
        },
    }


AGENTS = Path.home() / "Downloads" / "Agents Performance - Dashboard.pbix"


@pytest.mark.skipif(not AGENTS.exists(), reason="the real Agents Performance report is absent")
def test_the_real_agents_report_types():
    result = subprocess.run(  # noqa: S603 - the module's own CLI on a local file
        [sys.executable, str(Path(pbix_extract.__file__)), str(AGENTS), "--types"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=True,
    )
    found = json.loads(result.stdout)
    sales = {c["name"]: c for c in found["columns"] if c["table"] == "FactSales"}
    # every FactSales column is SummarizeBy None, keys and amounts alike
    assert {c["summarizeBy"] for c in sales.values()} == {"none"}
    assert sales["SalesAmount"]["dataType"] == 8
    assert sales["SalesQuantity"]["dataType"] == 6
    assert sales["DateKey"]["dataType"] == 9
    total = next(m for m in found["measures"] if m["name"] == "Total Sales")
    assert (total["table"], total["dataType"]) == ("# Measures", 8)
    # the DAX of calculated tables and columns comes with the types, keyed "Table" or "Table[Col]"
    calculated = found["calculated"]
    assert calculated and all(
        isinstance(key, str) and isinstance(dax, str) and dax for key, dax in calculated.items()
    )
