from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ai_powerbi_analyst.contracts import ColumnDataType, ColumnProfile, TableProfile
from ai_powerbi_analyst.studio.contracts import DatasetSnapshot, NormalizedTableSnapshot
from ai_powerbi_analyst.studio.ingestion import StudioIngestionService
from ai_powerbi_analyst.studio.pbix_extract import metadata_relationships, relationships
from ai_powerbi_analyst.studio.report_import import (
    ImportedPage,
    ImportedReport,
    import_report,
    imported_relationships,
)

# (ID, Name, SystemFlags). Date is a CALENDAR() table (flag 2); H$/R$/U$ and flag-1
# tables are engine internals.
TABLES = [
    (1, "FactSales", 0),
    (2, "DimStore", 0),
    (3, "DimEmployee", 0),
    (4, "Date", 2),
    (5, "DimChannel", 0),
    (6, "H$FactSales (1)$StoreKey (11)", 1),
    (7, "R$FactSales (1)$guid (99)", 1),
    (8, "U$Date (4)$Calendar (40)", 0),
    (9, "Engine Owned", 3),
]
# (ID, TableID, ExplicitName, InferredName). Calculated-table columns only have InferredName.
COLUMNS = [
    (11, 1, "StoreKey", None),
    (12, 1, "DateKey", None),
    (13, 1, "channelKey", None),
    (14, 1, "ShipDateKey", None),
    (21, 2, "StoreKey", None),
    (22, 2, "EmployeeKey", None),
    (31, 3, "EmployeeKey", None),
    (41, 4, None, "Date"),
    (51, 5, "ChannelKey", None),
    (61, 6, "StoreKey", None),
    (71, 7, "Key", None),
    (81, 8, "Key", None),
    (91, 9, "Key", None),
]
# (ID, IsActive, CrossFilteringBehavior, RelyOnRI, FromTable, FromColumn, FromCard,
#  ToTable, ToColumn, ToCard). Cardinality 1 = one, 2 = many.
RELATIONSHIPS = [
    (100, 1, 1, 0, 1, 11, 2, 2, 21, 1),  # FactSales -> DimStore
    (101, 1, 1, 0, 2, 22, 2, 3, 31, 1),  # DimStore -> DimEmployee
    (102, 1, 1, 1, 1, 12, 2, 4, 41, 1),  # FactSales -> calculated Date
    (103, 0, 2, 0, 1, 14, 2, 4, 41, 1),  # inactive, both directions
    (104, 1, 3, 0, 5, 51, 1, 1, 13, 2),  # authored one-to-many, automatic filter
    (105, 1, 1, 0, 1, 11, 2, 6, 61, 1),  # to an H$ table
    (106, 1, 1, 0, 1, 11, 2, 7, 71, 1),  # to an R$ table
    (107, 1, 1, 0, 1, 11, 2, 8, 81, 1),  # to a U$ name without the system flag
    (108, 1, 1, 0, 9, 91, 2, 2, 21, 1),  # from a flag-1 table
]


def metadata_db(*, legacy: bool = False) -> sqlite3.Connection:
    end = "End" if legacy else ""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE [Table] (ID INTEGER, Name TEXT, SystemFlags INTEGER)")
    conn.execute(
        "CREATE TABLE [Column] (ID INTEGER, TableID INTEGER, ExplicitName TEXT, InferredName TEXT)"
    )
    conn.execute(
        "CREATE TABLE Relationship (ID INTEGER, IsActive INTEGER, CrossFilteringBehavior INTEGER, "
        f"RelyOnReferentialIntegrity INTEGER, From{end}TableID INTEGER, "
        f"From{end}ColumnID INTEGER, From{end}Cardinality INTEGER, To{end}TableID INTEGER, "
        f"To{end}ColumnID INTEGER, To{end}Cardinality INTEGER)"
    )
    conn.executemany("INSERT INTO [Table] VALUES (?, ?, ?)", TABLES)
    conn.executemany("INSERT INTO [Column] VALUES (?, ?, ?, ?)", COLUMNS)
    conn.executemany(
        "INSERT INTO Relationship VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", RELATIONSHIPS
    )
    return conn


class Reader:
    """Raises on SQL errors, unlike PBIXRay's reader; both paths must fall back."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def query(self, sql: str) -> pd.DataFrame:
        return pd.read_sql_query(sql, self.conn)


EXPECTED = [
    ("FactSales", "StoreKey", "DimStore", "StoreKey", 1, "M:1", "Single", 0),
    ("DimStore", "EmployeeKey", "DimEmployee", "EmployeeKey", 1, "M:1", "Single", 0),
    ("FactSales", "DateKey", "Date", "Date", 1, "M:1", "Single", 1),
    ("FactSales", "ShipDateKey", "Date", "Date", 0, "M:1", "Both", 0),
    ("DimChannel", "ChannelKey", "FactSales", "channelKey", 1, "1:M", "Single", 0),
]
KEYS = (
    "FromTableName",
    "FromColumnName",
    "ToTableName",
    "ToColumnName",
    "IsActive",
    "Cardinality",
    "CrossFilteringBehavior",
    "RelyOnReferentialIntegrity",
)


@pytest.mark.parametrize("legacy", [False, True])
def test_metadata_query_keeps_calculated_and_inactive_joins_but_not_internals(legacy):
    found = metadata_relationships(Reader(metadata_db(legacy=legacy)))
    assert found == [dict(zip(KEYS, row, strict=True)) for row in EXPECTED]


def test_pbixray_reader_runs_the_same_query():
    # The production reader is APSW over the serialized metadata database.
    reader_type = pytest.importorskip("pbixray.meta.sqlite_source")._SqliteReader
    reader = reader_type(metadata_db().serialize())
    try:
        found = metadata_relationships(reader)
    finally:
        reader.close()
    assert [(r["FromTableName"], r["ToTableName"]) for r in found] == [
        (row[0], row[2]) for row in EXPECTED
    ]


def test_failed_metadata_query_falls_back_to_pbixray_view():
    fallback = pd.DataFrame(
        [
            {
                "FromTableName": "FactSales",
                "FromColumnName": "StoreKey",
                "ToTableName": "DimStore",
                "ToColumnName": "StoreKey",
                "IsActive": 1,
                "Cardinality": "M:1",
                "CrossFilteringBehavior": "Single",
            }
        ]
    )
    broken = sqlite3.connect(":memory:")  # no Relationship table: the query raises
    assert metadata_relationships(Reader(broken)) is None
    for db in (Reader(broken), SimpleNamespace(query=lambda _sql: pd.DataFrame()), None):
        model = SimpleNamespace(
            _metadata=SimpleNamespace(source=SimpleNamespace(_db=db)), relationships=fallback
        )
        assert relationships(model) == json.loads(fallback.to_json(orient="records"))
    assert relationships(SimpleNamespace(relationships=fallback))[0]["ToTableName"] == "DimStore"


def test_direct_query_is_used_when_it_succeeds():
    model = SimpleNamespace(
        _metadata=SimpleNamespace(source=SimpleNamespace(_db=Reader(metadata_db()))),
        relationships=pd.DataFrame(),
    )
    assert len(relationships(model)) == len(EXPECTED)


def column(name: str, index: int) -> ColumnProfile:
    return ColumnProfile(
        id=f"c{index}_{name.casefold()}",
        name=name,
        data_type=ColumnDataType.INTEGER,
        row_count=1,
        null_count=0,
        distinct_count=1,
    )


def snapshot(tables: dict[str, list[str]]) -> DatasetSnapshot:
    digest = "0" * 64
    return DatasetSnapshot(
        id="snapshot-fixture",
        workspace_id="workspace-fixture",
        created_at=datetime.now(UTC),
        original_bytes=0,
        normalized_bytes=0,
        manifest_path="manifest.json",
        base_hash=digest,
        tables=[
            NormalizedTableSnapshot(
                id=f"table_{i}",
                source_item_id=f"pbix-table-{i}",
                original_sha256=digest,
                original_snapshot_path=f"table-{i}.parquet",
                normalized_sha256=digest,
                normalized_parquet_path=f"table-{i}.parquet",
                table=TableProfile(
                    id=f"table_{i}",
                    name=name,
                    source_id=f"source_{i}",
                    row_count=1,
                    columns=[column(c, j) for j, c in enumerate(columns)],
                ),
            )
            for i, (name, columns) in enumerate(tables.items())
        ],
    )


SNAPSHOT = snapshot(
    {
        "FactSales": ["StoreKey", "DateKey", "channelKey"],
        "DimStore": ["StoreKey", "EmployeeKey"],
        "DimEmployee": ["EmployeeKey", "FirstName"],
        "Date": ["Date"],
        "DimChannel": ["ChannelKey"],
    }
)


def report(rels: list[dict]) -> ImportedReport:
    return ImportedReport(
        id="report-fixture",
        workspace_id="workspace-fixture",
        name="Fixture",
        source_path="fixture.pbix",
        archived_path="original.pbix",
        source_hash="0" * 64,
        base_hash="0" * 64,
        format="PBIR",
        pages=[ImportedPage(id="p1", title="Page 1")],
        relationships=rels,
    )


def rel(from_t, from_c, to_t, to_c, cardinality="M:1", **extra) -> dict:
    return {
        "FromTableName": from_t,
        "FromColumnName": from_c,
        "ToTableName": to_t,
        "ToColumnName": to_c,
        "IsActive": 1,
        "Cardinality": cardinality,
        "CrossFilteringBehavior": "Single",
        **extra,
    }


def test_imported_relationships_map_the_authors_joins_to_snapshot_ids():
    found = imported_relationships(
        report(
            [
                rel("FactSales", "StoreKey", "DimStore", "StoreKey"),
                rel("DimStore", "EmployeeKey", "DimEmployee", "EmployeeKey"),
                rel("factsales", "DATEKEY", "date", "date", RelyOnReferentialIntegrity=1),
            ]
        ),
        SNAPSHOT,
    )
    assert [
        (r.id, r.from_table_id, r.from_column_id, r.to_table_id, r.to_column_id) for r in found
    ] == [
        ("imported_relationship_0", "table_0", "c0_storekey", "table_1", "c0_storekey"),
        ("imported_relationship_1", "table_1", "c1_employeekey", "table_2", "c0_employeekey"),
        ("imported_relationship_2", "table_0", "c1_datekey", "table_3", "c0_date"),
    ]
    for item in found:
        assert item.cardinality == "many_to_one"
        assert item.review_status == "approved" and item.confidence == 1.0
        assert item.evidence == ["Imported from the original Power BI model"]
        assert item.is_active and item.cross_filter_direction == "single"
        assert not item.blockers
    assert found[2].rely_on_referential_integrity and not found[0].rely_on_referential_integrity


def test_one_to_many_is_stored_with_the_many_side_first_and_flags_are_kept():
    found = imported_relationships(
        report(
            [
                rel("DimChannel", "ChannelKey", "FactSales", "channelKey", "1:M"),
                rel("DimStore", "StoreKey", "FactSales", "StoreKey", "1:1", IsActive=0),
                rel("FactSales", "DateKey", "Date", "Date", "M:M", CrossFilteringBehavior="Both"),
            ]
        ),
        SNAPSHOT,
    )
    swapped, one, many = found
    assert (swapped.from_table_id, swapped.from_column_id) == ("table_0", "c2_channelkey")
    assert (swapped.to_table_id, swapped.to_column_id) == ("table_4", "c0_channelkey")
    assert swapped.cardinality == "many_to_one"
    assert one.cardinality == "one_to_one" and not one.is_active
    assert one.from_table_id == "table_1"
    assert many.cardinality == "many_to_many" and many.cross_filter_direction == "both"


def test_unresolvable_relationships_are_skipped_not_raised():
    found = imported_relationships(
        report(
            [
                rel("# Measures", "Key", "DimStore", "StoreKey"),
                rel("FactSales", "Missing", "DimStore", "StoreKey"),
                rel("FactSales", "StoreKey", "DimStore", "StoreKey", "?"),
                rel(None, None, "DimStore", "StoreKey"),
                rel("FactSales", "StoreKey", "DimStore", "StoreKey"),
            ]
        ),
        SNAPSHOT,
    )
    assert [r.id for r in found] == ["imported_relationship_4"]


def test_exact_spelling_wins_and_ambiguous_case_matches_are_skipped():
    ambiguous = snapshot({"Sales": ["Key"], "sales": ["Key"], "Dim": ["Key"]})
    found = imported_relationships(
        report([rel("Sales", "Key", "Dim", "Key"), rel("SALES", "Key", "Dim", "Key")]), ambiguous
    )
    assert [(r.id, r.from_table_id) for r in found] == [("imported_relationship_0", "table_0")]


def pbix(tmp_path: Path) -> Path:
    file = tmp_path / "Agents.pbix"
    with zipfile.ZipFile(file, "w") as z:
        z.writestr("Report/definition/pages/pages.json", json.dumps({"pageOrder": ["p1"]}))
        z.writestr("Report/definition/pages/p1/page.json", json.dumps({"name": "p1"}))
        z.writestr("DataModel", b"fixture-decoder-substituted")
    return file


def import_with(tmp_path: Path, monkeypatch, rels: list[dict]):
    frames = {
        "FactSales": pd.DataFrame({"StoreKey": [1, 2], "DateKey": [1, 1], "Sales": [5.0, 7.0]}),
        "DimStore": pd.DataFrame({"StoreKey": [1, 2], "EmployeeKey": [10, 20]}),
        "DimEmployee": pd.DataFrame({"EmployeeKey": [10, 20], "FirstName": ["Ana", "Bo"]}),
    }

    def model(_source, destination):
        tables = []
        for index, (name, frame) in enumerate(frames.items()):
            frame.to_parquet(destination / f"table-{index}.parquet", index=False)
            tables.append({"name": name, "file": f"table-{index}.parquet", "rows": len(frame)})
        return {"tables": tables, "schema": [], "measures": [], "relationships": rels}

    monkeypatch.setattr("ai_powerbi_analyst.studio.report_import.extract_model", model)
    state = tmp_path / "state"
    state.mkdir()
    return import_report(
        str(pbix(tmp_path)), state, "workspace-fixture", StudioIngestionService(state)
    )


def test_import_warns_when_relationships_do_not_match_imported_tables(tmp_path, monkeypatch):
    rels = [
        rel("FactSales", "StoreKey", "DimStore", "StoreKey"),
        rel("DimStore", "EmployeeKey", "DimEmployee", "EmployeeKey"),
        rel("FactSales", "DateKey", "Date", "Date"),  # Date has no imported rows here
    ]
    imported, snap = import_with(tmp_path, monkeypatch, rels)
    assert imported.relationships == rels
    assert any(w.startswith("1 of 3 relationships") for w in imported.warnings)
    chain = imported_relationships(imported, snap)
    names = {t.id: t.table.name for t in snap.tables}
    assert [(names[r.from_table_id], names[r.to_table_id]) for r in chain] == [
        ("FactSales", "DimStore"),
        ("DimStore", "DimEmployee"),
    ]


def test_import_has_no_relationship_warning_when_every_join_matches(tmp_path, monkeypatch):
    rels = [rel("FactSales", "StoreKey", "DimStore", "StoreKey")]
    imported, _ = import_with(tmp_path, monkeypatch, rels)
    assert not any("relationships" in w for w in imported.warnings)
