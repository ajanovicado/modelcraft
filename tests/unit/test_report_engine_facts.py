"""The local engine follows the head's model facts: renames, deletes, new joins, calculated
columns and tables it calculates itself, and what it leaves to Power BI (unavailable) instead
of showing a stale or wrong number."""

from __future__ import annotations

import copy
import zipfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from ai_powerbi_analyst.studio import model_facts, report_engine
from ai_powerbi_analyst.studio.contracts import DaxEvaluateRequest
from ai_powerbi_analyst.studio.dax_engine import (
    DaxModelIncomplete,
    DaxUnsupported,
    Model,
    run_query,
)
from ai_powerbi_analyst.studio.report_engine import (
    LocalReportEngine,
    ReportModel,
    unavailable_fields,
    unavailable_reference,
)
from ai_powerbi_analyst.studio.report_import import ImportedPage, ImportedReport, model_key

SALES = pd.DataFrame(
    {
        "SaleKey": [1, 2, 3, 4, 5, 6],
        "StoreKey": [1, 1, 2, 2, 3, 3],
        "DateKey": pd.to_datetime(
            ["2024-01-01", "2024-01-02", "2024-01-02", "2024-01-05", "2024-01-09", "2024-01-10"]
        ),
        "Amount": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
        "Cost": [4.0, 5.0, 6.0, 7.0, 8.0, 9.0],
    }
)
# What Desktop stored for the calculated column Margin = Amount - Cost when the rows were read.
SALES["Margin"] = SALES["Amount"] - SALES["Cost"]
STORES = pd.DataFrame(
    {"StoreKey": [1, 2, 3], "StoreName": ["A", "B", "C"], "Region": ["East", "West", "East"]}
)
DATES = pd.DataFrame({"Date": pd.date_range("2024-01-01", "2024-01-10")})
TOP = pd.DataFrame({"Top N": [1, 2, 3]})
KINDS = pd.DataFrame({"Kind": ["Actual", "Change"], "Order": [1, 2]})

TABLE_ROWS = {"FactSales": SALES, "DimStore": STORES, "Date": DATES, "Top N": TOP, "Kinds": KINDS}


def _table(name: str, tag: str, source: str = "m", expression: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "hidden": False,
        "source": source,
        "calculated": source == "calculated",
        "expression": expression,
        "calculationGroup": False,
        "lineageTag": tag,
    }


def _column(
    table: str,
    name: str,
    data_type: int,
    *,
    kind: str = "data",
    expression: str | None = None,
    source: str | None = None,
    tag: str | None = None,
) -> dict[str, Any]:
    return {
        "table": table,
        "name": name,
        "kind": kind,
        "dataType": data_type,
        "expression": expression,
        "sourceColumn": source if source is not None else (name if kind == "data" else None),
        "lineageTag": tag or f"{table}.{name}",
    }


def _calc(table: str, name: str, data_type: int, dax: str) -> dict[str, Any]:
    return _column(table, name, data_type, kind="calculated", expression=dax)


def _relationship(
    source: tuple[str, str], target: tuple[str, str], *, cross: str = "single"
) -> dict[str, Any]:
    return {
        "fromTable": source[0],
        "fromColumn": source[1],
        "toTable": target[0],
        "toColumn": target[1],
        "cardinality": "M:1",
        "crossFilter": cross,
        "active": True,
    }


MARGIN = "FactSales[Amount] - FactSales[Cost]"
FACTS: dict[str, Any] = {
    "version": 1,
    "tables": [
        _table("FactSales", "t-sales"),
        _table("DimStore", "t-store"),
        _table("Date", "t-date", "calculated", "CALENDAR(DATE(2024, 1, 1), DATE(2024, 1, 10))"),
        _table("# Measures", "t-measures"),
        _table("Top N", "t-top", "calculated", "GENERATESERIES(1, 3, 1)"),
        _table(
            "Kinds",
            "t-kinds",
            "calculated",
            'DATATABLE("Kind", STRING, "Order", INTEGER, {{"Actual", 1}, {"Change", 2}})',
        ),
    ],
    "columns": [
        _column("FactSales", "SaleKey", 6),
        _column("FactSales", "StoreKey", 6),
        _column("FactSales", "DateKey", 9),
        _column("FactSales", "Amount", 8),
        _column("FactSales", "Cost", 8),
        _column("FactSales", "Margin", 8, kind="calculated", expression=MARGIN),
        _column("DimStore", "StoreKey", 6),
        _column("DimStore", "StoreName", 2),
        _column("DimStore", "Region", 2),
        _column("Date", "Date", 9, kind="calculatedTableColumn", source="[Date]"),
        _column("Top N", "Top N", 6, kind="calculatedTableColumn", source="[Value]"),
        _column("Kinds", "Kind", 2, kind="calculatedTableColumn", source="[Kind]"),
        _column("Kinds", "Order", 6, kind="calculatedTableColumn", source="[Order]"),
    ],
    "measures": [
        {"table": "# Measures", "name": "Total Sales", "expression": "SUM(FactSales[Amount])"},
        {"table": "# Measures", "name": "Total Margin", "expression": "SUM(FactSales[Margin])"},
    ],
    "relationships": [
        _relationship(("FactSales", "StoreKey"), ("DimStore", "StoreKey")),
        _relationship(("FactSales", "DateKey"), ("Date", "Date")),
    ],
    "partitions": [
        {"table": name, "source": "m", "mode": "import"}
        for name in ("FactSales", "DimStore", "# Measures")
    ],
}


@pytest.fixture(autouse=True)
def _empty_cache() -> Iterator[None]:
    report_engine._models.clear()
    yield
    report_engine._models.clear()


@pytest.fixture
def snapshot(tmp_path: Path) -> Any:
    """A snapshot of the rows as the import decoded them (the fields the engine reads)."""
    items = []
    for index, (name, frame) in enumerate(TABLE_ROWS.items()):
        path = tmp_path / f"table-{index}.parquet"
        frame.to_parquet(path, index=False)
        items.append(
            SimpleNamespace(
                id=f"t{index}",
                table=SimpleNamespace(
                    name=name,
                    row_count=len(frame),
                    columns=[
                        SimpleNamespace(name=c, id=f"c{index}_{j}")
                        for j, c in enumerate(frame.columns)
                    ],
                ),
                normalized_parquet_path=str(path),
            )
        )
    return SimpleNamespace(id="snap-1", tables=items)


def measures_of(facts: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"Name": m["name"], "TableName": m["table"], "Expression": m["expression"]}
        for m in facts["measures"]
    ]


def report_for(
    head: dict[str, Any], rows_from: dict[str, Any] | None = None, **more: Any
) -> ImportedReport:
    """A report whose head has the facts ``head``, its rows decoded from ``rows_from``."""
    rows_from = FACTS if rows_from is None else rows_from
    fields: dict[str, Any] = {
        "id": "report-1",
        "workspace_id": "ws",
        "name": "R",
        "source_path": "s.pbix",
        "archived_path": "o.pbix",
        "source_hash": "a" * 64,
        "base_hash": "a" * 64,
        "format": "PBIR",
        "pages": [ImportedPage(id="p", title="One")],
        "tables": model_facts.schema_rows(head),
        "measures": measures_of(head),
        "relationships": model_facts.relationship_rows(head),
        "relationships_version": 2,
        "snapshot_id": "snap-1",
        "model": model_facts.stored(head),
        "snapshot_model": model_facts.digest(rows_from, "crc-1"),
    }
    fields.update(more)
    return ImportedReport(**fields)


def changed(**sections: Any) -> dict[str, Any]:
    head = copy.deepcopy(FACTS)
    head.update(copy.deepcopy(sections))
    return head


def with_columns(*columns: dict[str, Any], replace: str | None = None) -> dict[str, Any]:
    kept = [
        c for c in FACTS["columns"] if replace is None or f"{c['table']}.{c['name']}" != replace
    ]
    return changed(columns=kept + list(columns))


def rows(model: Model, query: str) -> list[list[Any]]:
    return run_query(model, query).rows


BY_STORE = (
    'EVALUATE SUMMARIZECOLUMNS(DimStore[StoreName], "Sales", [Total Sales], '
    '"Margin", [Total Margin])'
)


# --- the head as the rows describe it ---------------------------------------------------------


def test_unchanged_facts_give_the_snapshot_models_answers(snapshot: Any) -> None:
    report = report_for(FACTS)
    head = report_engine._build(report, snapshot)
    plain = report_engine._snapshot_model(report.model_copy(update={"model": None}), snapshot)
    assert isinstance(head, ReportModel)
    assert (
        rows(head, BY_STORE)
        == rows(plain, BY_STORE)
        == [
            ["A", 30.0, 21.0],
            ["B", 70.0, 57.0],
            ["C", 110.0, 93.0],
        ]
    )
    # Every calculated column and table is the one the rows reflect: nothing recalculated,
    # and the DATATABLE the engine cannot evaluate is read from the rows.
    assert (head.computed, unavailable_fields(head)) == ([], {})
    assert rows(head, "EVALUATE Kinds") == [["Actual", 1], ["Change", 2]]
    # a table of measures only has no rows and is not missing anything
    assert rows(head, "EVALUATE ROW(\"s\", '# Measures'[Total Sales])") == [[210.0]]


def test_a_report_without_facts_is_built_from_the_snapshot_alone(snapshot: Any) -> None:
    report = report_for(FACTS, model=None, snapshot_model=None)
    built = report_engine._build(report, snapshot)
    assert not isinstance(built, ReportModel) and unavailable_fields(built) == {}
    assert rows(built, BY_STORE)[0] == ["A", 30.0, 21.0]


@pytest.mark.parametrize("facts", [None, {"version": 1}, {"version": 1, "tables": []}])
@pytest.mark.parametrize("read_version", [1, 2])
def test_a_current_import_without_readable_metadata_never_uses_saved_rows(
    snapshot: Any, facts: dict[str, Any] | None, read_version: int
) -> None:
    report = report_for(FACTS, model=facts, snapshot_model=None, model_read_version=read_version)
    with pytest.raises(report_engine.DaxUnavailable, match="current model's metadata"):
        report_engine.report_model(report, snapshot)


def test_a_failed_metadata_read_cannot_reuse_a_cached_legacy_engine(snapshot: Any) -> None:
    legacy = report_for(FACTS, model=None, snapshot_model=None)
    assert rows(report_engine.report_model(legacy, snapshot), BY_STORE)[0] == ["A", 30.0, 21.0]
    current = legacy.model_copy(update={"model_read_version": 1})
    with pytest.raises(report_engine.DaxUnavailable, match="current model's metadata"):
        report_engine.report_model(current, snapshot)


def test_a_current_import_with_known_import_metadata_still_uses_saved_rows(snapshot: Any) -> None:
    current = report_for(FACTS, model_read_version=1)
    assert rows(report_engine.report_model(current, snapshot), BY_STORE)[0] == ["A", 30.0, 21.0]


# --- calculated columns ------------------------------------------------------------------------


def test_new_calculated_columns_are_calculated_as_pandas_computes_them(snapshot: Any) -> None:
    head = with_columns(
        _calc("FactSales", "Rate", 8, "DIVIDE(FactSales[Margin], FactSales[Amount])"),
        _calc("FactSales", "Is Big", 11, "[Amount] >= 30"),
        _calc("FactSales", "Store", 2, "RELATED(DimStore[StoreName])"),
        _calc("DimStore", "Store Sales", 8, "[Total Sales]"),
        _calc("DimStore", "Label", 2, 'DimStore[StoreName] & "-" & DimStore[Region]'),
        _calc("DimStore", "Sales Count", 6, "COUNTROWS(RELATEDTABLE(FactSales))"),
    )
    model = report_engine._build(report_for(head), snapshot)
    assert isinstance(model, ReportModel) and unavailable_fields(model) == {}
    assert model.computed == [
        "'FactSales'[Rate]",
        "'FactSales'[Is Big]",
        "'FactSales'[Store]",
        "'DimStore'[Store Sales]",
        "'DimStore'[Label]",
        "'DimStore'[Sales Count]",
    ]
    sales = rows(
        model,
        'EVALUATE SELECTCOLUMNS(FactSales, "k", FactSales[SaleKey], "r", FactSales[Rate], '
        '"b", FactSales[Is Big], "s", FactSales[Store])',
    )
    expected = SALES.assign(
        Rate=SALES["Margin"] / SALES["Amount"],
        Big=SALES["Amount"] >= 30,
        Store=SALES["StoreKey"].map(
            dict(zip(STORES["StoreKey"], STORES["StoreName"], strict=True))
        ),
    )
    assert sales == [
        list(row)
        for row in zip(
            expected["SaleKey"].tolist(),
            expected["Rate"].tolist(),
            expected["Big"].tolist(),
            expected["Store"].tolist(),
            strict=True,
        )
    ]
    stores = rows(
        model,
        'EVALUATE SELECTCOLUMNS(DimStore, "n", DimStore[StoreName], "s", DimStore[Store Sales], '
        '"l", DimStore[Label], "c", DimStore[Sales Count])',
    )
    totals = SALES.groupby("StoreKey")["Amount"].sum()
    counts = SALES.groupby("StoreKey").size()
    assert stores == [
        [
            r.StoreName,
            float(totals[r.StoreKey]),
            f"{r.StoreName}-{r.Region}",
            int(counts[r.StoreKey]),
        ]
        for r in STORES.itertuples()
    ]
    # the new columns take the model's types: whole numbers, text, true/false
    columns = model.tables["dimstore"].columns
    assert (columns["sales count"].kind, columns["label"].kind) == ("integer", "string")
    assert model.tables["factsales"].columns["is big"].kind == "boolean"


def test_a_calculated_column_whose_dax_changed_is_calculated_again(snapshot: Any) -> None:
    head = with_columns(
        _column(
            "FactSales", "Margin", 8, kind="calculated", expression="FactSales[Amount] - 2 * [Cost]"
        ),
        replace="FactSales.Margin",
    )
    model = report_engine._build(report_for(head), snapshot)
    assert model.computed == ["'FactSales'[Margin]"]  # type: ignore[attr-defined]
    total = float((SALES["Amount"] - 2 * SALES["Cost"]).sum())
    assert rows(model, 'EVALUATE ROW("m", [Total Margin])') == [[total]]


def test_a_calculated_column_that_reads_a_changed_measure_is_calculated_again(
    tmp_path: Path, snapshot: Any
) -> None:
    store_sales = _column(
        "DimStore", "Store Sales", 8, kind="calculated", expression="[Total Sales]"
    )
    # The rows were read with Store Sales in them, as Desktop stored it.
    stored = STORES.assign(**{"Store Sales": [30.0, 70.0, 110.0]})
    path = tmp_path / "stores-with-sales.parquet"
    stored.to_parquet(path, index=False)
    item = next(i for i in snapshot.tables if i.table.name == "DimStore")
    item.normalized_parquet_path = str(path)
    before = with_columns(store_sales)
    unchanged = report_engine._build(report_for(before, before), snapshot)
    assert unchanged.computed == []  # type: ignore[attr-defined]
    # Desktop changed [Total Sales] to sum the cost: the stored column no longer holds.
    after = copy.deepcopy(before)
    after["measures"][0]["expression"] = "SUM(FactSales[Cost])"
    model = report_engine._build(report_for(after, before), snapshot)
    assert model.computed == ["'DimStore'[Store Sales]"]  # type: ignore[attr-defined]
    costs = SALES.groupby("StoreKey")["Cost"].sum()
    assert rows(model, 'EVALUATE SELECTCOLUMNS(DimStore, "s", DimStore[Store Sales])') == [
        [float(costs[k])] for k in STORES["StoreKey"]
    ]


@pytest.mark.parametrize(
    ("column", "why"),
    [
        (
            _calc("DimStore", "Path", 2, "PATH(DimStore[StoreKey], DimStore[StoreKey])"),
            "Studio cannot calculate the calculated column 'DimStore'[Path] (The function PATH",
        ),
        (
            # a whole-number column whose values are not whole
            _calc("DimStore", "Half", 6, "DimStore[StoreKey] / 2"),
            "Studio cannot calculate the calculated column 'DimStore'[Half] ([Half] gives values "
            "that are not the column's data type as Power BI stores it)",
        ),
        (
            # a fixed decimal needs rounding to four places, which is left to Power BI
            _calc("DimStore", "Third", 10, "DimStore[StoreKey] / 3"),
            "Studio cannot calculate the calculated column 'DimStore'[Third] ([Third] gives",
        ),
        (
            {**_calc("DimStore", "Long", 2, "1"), "truncated": True},
            "Studio cannot calculate the calculated column 'DimStore'[Long] (its DAX was not read",
        ),
    ],
)
def test_a_calculated_column_the_engine_cannot_calculate_needs_power_bi(
    snapshot: Any, column: dict[str, Any], why: str
) -> None:
    head = with_columns(column)
    reads = f"COUNTROWS(VALUES(DimStore[{column['name']}]))"
    head["measures"].append({"table": "# Measures", "name": "Uses It", "expression": reads})
    report = report_for(head)
    model = report_engine._build(report, snapshot)
    reason = unavailable_fields(model)[("dimstore", column["name"].casefold())]
    assert reason.startswith(why)
    assert reason.endswith("Power BI shows visuals that use it.")
    assert not model.has_column("DimStore", column["name"])  # never a wrong column
    visual = f'EVALUATE SUMMARIZECOLUMNS(DimStore[{column["name"]}], "S", [Total Sales])'
    with pytest.raises(DaxUnsupported) as raised:
        LocalReportEngine(model).query(report, visual)
    assert str(raised.value) == reason
    # a measure that reads it is refused too, in DAX Lab as in a visual
    assert unavailable_reference(model, 'EVALUATE ROW("v", [Uses It])') == reason
    report_engine._models.clear()
    with pytest.raises(DaxUnsupported, match="Studio cannot calculate"):
        report_engine.evaluate(
            report, snapshot, DaxEvaluateRequest(workspace_id="ws", expression="[Uses It]")
        )
    # everything else still answers
    assert LocalReportEngine(model).query(report, BY_STORE)["rows"][0]["DimStore[StoreName]"] == "A"


def test_context_transition_over_repeated_rows_is_left_to_power_bi(
    tmp_path: Path, snapshot: Any
) -> None:
    """Repeated rows in a disconnected table are a valid model, but native context
    transition counts every matching row rather than only the current row."""
    repeated = pd.concat([STORES, STORES.iloc[[0]]], ignore_index=True)
    path = tmp_path / "repeated.parquet"
    repeated.to_parquet(path, index=False)
    item = next(i for i in snapshot.tables if i.table.name == "DimStore")
    item.normalized_parquet_path, item.table.row_count = str(path), 4
    head = with_columns(
        _column("DimStore", "Matched Stores", 6, kind="calculated", expression="[Store Rows]"),
        _column("DimStore", "Upper", 2, kind="calculated", expression="UPPER(DimStore[StoreName])"),
    )
    # Keep this test about context transition. Duplicate values cannot be the
    # one-side key of an active relationship; that invariant is checked separately.
    head["relationships"] = [
        relationship
        for relationship in head["relationships"]
        if relationship["toTable"] != "DimStore"
    ]
    head["measures"].append(
        {"table": "# Measures", "name": "Store Rows", "expression": "COUNTROWS(DimStore)"}
    )
    model = report_engine._build(report_for(head), snapshot)
    assert model.computed == ["'DimStore'[Upper]"]  # type: ignore[attr-defined]
    assert unavailable_fields(model)[("dimstore", "matched stores")] == (
        "Studio cannot calculate the calculated column 'DimStore'[Matched Stores] (it filters rows "
        "of 'DimStore', which has repeated rows Power BI filters together); Power BI shows "
        "visuals that use it."
    )


# --- names, deletes, joins ---------------------------------------------------------------------


def test_renamed_and_deleted_columns_and_tables_follow_the_head(snapshot: Any) -> None:
    head = copy.deepcopy(FACTS)
    for table in head["tables"]:
        if table["name"] == "FactSales":
            table["name"] = "Sales"
    for partition in head["partitions"]:
        if partition["table"] == "FactSales":
            partition["table"] = "Sales"
    for column in head["columns"]:
        if column["table"] == "FactSales":
            column["table"] = "Sales"
        if (column["table"], column["name"]) == ("DimStore", "StoreName"):
            column["name"] = "Store"  # same lineage tag: a rename
    head["columns"] = [c for c in head["columns"] if c["name"] != "Region"]
    head["columns"] = [
        {**c, "expression": "Sales[Amount] - Sales[Cost]"} if c["name"] == "Margin" else c
        for c in head["columns"]
    ]
    head["measures"] = [
        {"table": "# Measures", "name": "Total Sales", "expression": "SUM(Sales[Amount])"},
        {"table": "# Measures", "name": "Total Margin", "expression": "SUM(Sales[Margin])"},
    ]
    head["relationships"] = [
        _relationship(("Sales", "StoreKey"), ("DimStore", "StoreKey")),
        _relationship(("Sales", "DateKey"), ("Date", "Date")),
    ]
    model = report_engine._build(report_for(head), snapshot)
    assert unavailable_fields(model) == {}
    # the old names are gone, the new ones answer from the same rows
    assert not model.has_column("DimStore", "StoreName") and not model.has_column(
        "DimStore", "Region"
    )
    assert "factsales" not in model.tables and model.has_column("Sales", "Amount")
    assert rows(model, 'EVALUATE SUMMARIZECOLUMNS(DimStore[Store], "Sales", [Total Sales])') == [
        ["A", 30.0],
        ["B", 70.0],
        ["C", 110.0],
    ]


def test_a_new_join_is_used_and_a_removed_one_is_not_kept(snapshot: Any) -> None:
    rows_model = changed(relationships=[FACTS["relationships"][1]])  # read without the store join
    joined = report_engine._build(report_for(FACTS, rows_model), snapshot)
    assert rows(joined, 'EVALUATE SUMMARIZECOLUMNS(DimStore[StoreName], "S", [Total Sales])') == [
        ["A", 30.0],
        ["B", 70.0],
        ["C", 110.0],
    ]
    unjoined = report_engine._build(report_for(rows_model), snapshot)
    # no join: every store sees every sale, as Power BI shows it without the relationship
    assert rows(
        unjoined, 'EVALUATE ADDCOLUMNS(VALUES(DimStore[StoreName]), "S", [Total Sales])'
    ) == [["A", 210.0], ["B", 210.0], ["C", 210.0]]
    both = changed(
        relationships=[
            _relationship(("FactSales", "StoreKey"), ("DimStore", "StoreKey"), cross="both"),
            FACTS["relationships"][1],
        ]
    )
    with pytest.raises(DaxUnsupported, match="two-way relationship between 'FactSales'"):
        report_engine._build(report_for(both), snapshot)


# --- calculated tables -------------------------------------------------------------------------


def test_a_new_calculated_table_is_calculated_and_one_the_engine_cannot_is_not(
    snapshot: Any,
) -> None:
    head = changed(
        tables=[
            *FACTS["tables"],
            _table("Bands", "t-bands", "calculated", "GENERATESERIES(0, 100, 50)"),
            _table("Labels", "t-labels", "calculated", 'DATATABLE("L", STRING, {{"x"}})'),
            _table("Copy", "t-copy", "calculated", "'Date'"),
        ],
        columns=[
            *FACTS["columns"],
            _column("Bands", "Band", 6, kind="calculatedTableColumn", source="[Value]"),
            _column("Labels", "L", 2, kind="calculatedTableColumn", source="[L]"),
            _column("Copy", "Day", 9, kind="calculatedTableColumn", source="Date[Date]"),
        ],
    )
    model = report_engine._build(report_for(head), snapshot)
    assert model.computed == ["'Bands'", "'Copy'"]  # type: ignore[attr-defined]
    assert rows(model, "EVALUATE Bands") == [[0], [50], [100]]
    assert rows(model, 'EVALUATE ROW("n", COUNTROWS(Copy), "d", MAX(Copy[Day]))') == [
        [10, pd.Timestamp("2024-01-10").to_pydatetime()]
    ]
    assert unavailable_fields(model)[("labels", None)].startswith(
        "Studio cannot calculate the calculated table 'Labels' (The function DATATABLE"
    )
    with pytest.raises(DaxUnsupported, match="'Labels'"):
        LocalReportEngine(model).query(report_for(head), "EVALUATE VALUES(Labels[L])")


def test_a_changed_calculated_table_is_calculated_again(snapshot: Any) -> None:
    head = copy.deepcopy(FACTS)
    head["tables"][4]["expression"] = "GENERATESERIES(1, 5, 1)"
    model = report_engine._build(report_for(head), snapshot)
    assert model.computed == ["'Top N'"]  # type: ignore[attr-defined]
    assert rows(model, "EVALUATE 'Top N'") == [[1], [2], [3], [4], [5]]


# --- what Studio's canvas leaves to Power BI ----------------------------------------------------


def test_field_parameters_and_calculation_groups_are_power_bi_only(snapshot: Any) -> None:
    head = changed(
        tables=[*FACTS["tables"], {**_table("Time", "t-time"), "calculationGroup": True}],
        fieldParameters=[{"table": "Kinds", "rows": None}],
        calculationGroups=[{"table": "Time", "items": [], "selectionExpressions": []}],
    )
    report = report_for(head)
    model = report_engine._build(report, snapshot)
    assert unavailable_fields(model) == {
        ("time", None): "'Time' is a calculation group, which Studio's canvas does not apply; "
        "Power BI shows visuals that use it.",
        ("kinds", None): "'Kinds' is a field parameter, which Studio's canvas does not switch; "
        "Power BI shows visuals that use it.",
    }
    with pytest.raises(DaxUnsupported, match="field parameter"):
        LocalReportEngine(model).query(
            report, 'EVALUATE SUMMARIZECOLUMNS(Kinds[Kind], "S", [Total Sales])'
        )
    # A selection expression changes every measure when nothing is selected: no local answer.
    selecting = changed(
        tables=head["tables"],
        calculationGroups=[{"table": "Time", "selectionExpressions": [{"expression": "0"}]}],
    )
    with pytest.raises(DaxUnsupported, match="changes every measure"):
        report_engine._build(report_for(selecting), snapshot)


def test_data_the_rows_lack_waits_for_the_data_to_be_read_again(snapshot: Any) -> None:
    head = changed(
        tables=[*FACTS["tables"], _table("Targets", "t-targets")],
        partitions=[*FACTS["partitions"], {"table": "Targets", "source": "m", "mode": "import"}],
        columns=[
            *FACTS["columns"],
            _column("FactSales", "Discount", 8),
            _column("Targets", "StoreKey", 6),
        ],
    )
    model = report_engine._build(report_for(head), snapshot)
    assert unavailable_fields(model) == {
        ("targets", None): "'Targets' was added or changed in the model after Studio read its "
        "data; read the data again, or use Power BI.",
        ("factsales", "discount"): "'FactSales'[Discount] was added or changed in the model "
        "after Studio read its data; read the data again, or use Power BI.",
    }
    # An active join to it would stop filters: nothing is answered locally.
    target = _relationship(("Targets", "StoreKey"), ("DimStore", "StoreKey"))
    joined = changed(
        tables=head["tables"],
        columns=head["columns"],
        relationships=[*FACTS["relationships"], target],
    )
    with pytest.raises(DaxModelIncomplete, match="'Targets' and 'DimStore'"):
        report_engine._build(report_for(joined), snapshot)


# --- the model cache -----------------------------------------------------------------------------


def test_a_changed_calculated_column_builds_a_new_model(snapshot: Any) -> None:
    before = report_for(FACTS)
    head = with_columns(
        _column("FactSales", "Margin", 8, kind="calculated", expression="FactSales[Amount]"),
        replace="FactSales.Margin",
    )
    after = report_for(head)
    # the schema rows alone would not tell them apart
    assert sorted(before.tables, key=str) == sorted(after.tables, key=str)
    first = report_engine.report_model(before, snapshot)
    second = report_engine.report_model(after, snapshot)
    assert first is not second
    assert rows(second, 'EVALUATE ROW("m", [Total Margin])') == [[210.0]]
    assert report_engine.report_model(before, snapshot) is first


# --- review repros (2026-09-29): never an old or guessed number ---------------------------------


def set_rows(snapshot: Any, tmp_path: Path, name: str, frame: pd.DataFrame) -> None:
    """Replace the rows the snapshot holds for ``name`` (as Desktop stored them then)."""
    path = tmp_path / f"rows-{name}.parquet"
    frame.to_parquet(path, index=False)
    item = next(i for i in snapshot.tables if i.table.name == name)
    item.normalized_parquet_path, item.table.row_count = str(path), len(frame)


def refused(model: Model, report: ImportedReport, query: str) -> str:
    with pytest.raises(report_engine.DaxUnavailable) as raised:
        LocalReportEngine(model).query(report, query)
    return str(raised.value)


def test_a_new_table_given_an_old_tables_name_never_gets_its_rows(snapshot: Any) -> None:
    """DimStore renamed OldStores in Desktop, and a new Power Query table named DimStore."""
    head = copy.deepcopy(FACTS)
    for table in head["tables"]:
        if table["name"] == "DimStore":
            table["name"] = "OldStores"
    for partition in head["partitions"]:
        if partition["table"] == "DimStore":
            partition["table"] = "OldStores"
    head["partitions"].append({"table": "DimStore", "source": "m", "mode": "import"})
    head["tables"].append(_table("DimStore", "t-new-store"))
    for column in head["columns"]:
        if column["table"] == "DimStore":
            column["table"] = "OldStores"
    head["columns"] += [
        _column("DimStore", "StoreKey", 6, tag="new.k"),
        _column("DimStore", "StoreName", 2, tag="new.n"),
    ]
    head["relationships"] = [
        _relationship(("FactSales", "StoreKey"), ("OldStores", "StoreKey")),
        FACTS["relationships"][1],
    ]
    report = report_for(head)
    model = report_engine._build(report, snapshot)
    assert refused(model, report, "EVALUATE DimStore") == (
        "'DimStore' was added or changed in the model after Studio read its data; read the "
        "data again, or use Power BI."
    )
    assert rows(model, 'EVALUATE SELECTCOLUMNS(OldStores, "n", OldStores[StoreName])') == [
        ["A"],
        ["B"],
        ["C"],
    ]


def test_a_column_deleted_and_added_again_as_text_is_not_the_stored_number(
    snapshot: Any,
) -> None:
    head = copy.deepcopy(FACTS)
    head["columns"] = [c for c in head["columns"] if c["name"] != "Amount"]
    head["columns"].append(_column("FactSales", "Amount", 2, tag="brand-new-tag"))
    report = report_for(head)
    model = report_engine._build(report, snapshot)
    assert not model.has_column("FactSales", "Amount")
    assert refused(model, report, 'EVALUATE ROW("s", [Total Sales])').startswith(
        "'FactSales'[Amount] was added or changed in the model"
    )


def test_values_of_a_one_side_table_follow_a_new_join(snapshot: Any, tmp_path: Path) -> None:
    """A new join gives DimStore a blank row (a sale of an unknown store): a calculated table
    VALUES(DimStore[Region]) stored without the join no longer holds."""
    sales = SALES.copy()
    sales.loc[5, "StoreKey"] = 4
    set_rows(snapshot, tmp_path, "FactSales", sales)
    before = copy.deepcopy(FACTS)
    before["relationships"] = [FACTS["relationships"][1]]
    before["tables"].append(
        _table("Regions", "t-regions", "calculated", "VALUES(DimStore[Region])")
    )
    before["columns"].append(
        _column("Regions", "Region", 2, kind="calculatedTableColumn", source="DimStore[Region]")
    )
    snapshot.tables.append(copy.deepcopy(snapshot.tables[1]))
    snapshot.tables[-1].table.name = "Regions"
    set_rows(snapshot, tmp_path, "Regions", pd.DataFrame({"Region": ["East", "West"]}))
    head = copy.deepcopy(before)
    head["relationships"] = copy.deepcopy(FACTS["relationships"])
    model = report_engine._build(report_for(head, before), snapshot)
    assert "'Regions'" in model.computed  # type: ignore[attr-defined]
    assert rows(model, "EVALUATE Regions") == [[None], ["East"], ["West"]]
    # unchanged joins: the stored rows are kept
    same = report_engine._build(report_for(before, before), snapshot)
    assert same.computed == []  # type: ignore[attr-defined]


def test_a_calculated_join_key_that_changed_recalculates_what_reads_through_it(
    snapshot: Any, tmp_path: Path
) -> None:
    set_rows(snapshot, tmp_path, "FactSales", SALES.assign(**{"Store Ref": SALES["StoreKey"]}))
    set_rows(snapshot, tmp_path, "DimStore", STORES.assign(**{"Store Sales": [30.0, 70.0, 110.0]}))
    before = with_columns(
        _calc("DimStore", "Store Sales", 8, "[Total Sales]"),
        _calc("FactSales", "Store Ref", 6, "FactSales[StoreKey]"),
    )
    before["relationships"] = [
        _relationship(("FactSales", "Store Ref"), ("DimStore", "StoreKey")),
        FACTS["relationships"][1],
    ]
    head = copy.deepcopy(before)
    for column in head["columns"]:
        if column["name"] == "Store Ref":
            column["expression"] = "4 - FactSales[StoreKey]"
    model = report_engine._build(report_for(head, before), snapshot)
    assert model.computed == ["'FactSales'[Store Ref]", "'DimStore'[Store Sales]"]  # type: ignore[attr-defined]
    stored = rows(model, 'EVALUATE SELECTCOLUMNS(DimStore, "s", DimStore[Store Sales])')
    live = rows(model, 'EVALUATE ADDCOLUMNS(VALUES(DimStore[StoreKey]), "s", [Total Sales])')
    assert stored == [[s] for _, s in live] == [[110.0], [70.0], [30.0]]


def test_a_join_key_calculated_later_is_built_before_what_reads_through_it(
    snapshot: Any,
) -> None:
    """DimStore listed first: its [Total Sales] column must not be calculated while the join
    on the not-yet-calculated FactSales[Store Ref] is missing (it would be the grand total)."""
    head = copy.deepcopy(FACTS)
    head["tables"] = sorted(head["tables"], key=lambda t: t["name"] != "DimStore")
    head["columns"] += [
        _calc("DimStore", "Store Sales", 8, "[Total Sales]"),
        _calc("FactSales", "Store Ref", 6, "FactSales[StoreKey]"),
    ]
    head["relationships"] = [
        _relationship(("FactSales", "Store Ref"), ("DimStore", "StoreKey")),
        FACTS["relationships"][1],
    ]
    model = report_engine._build(report_for(head), snapshot)
    assert model.computed == ["'FactSales'[Store Ref]", "'DimStore'[Store Sales]"]  # type: ignore[attr-defined]
    assert rows(model, 'EVALUATE SELECTCOLUMNS(DimStore, "s", DimStore[Store Sales])') == [
        [30.0],
        [70.0],
        [110.0],
    ]


def test_a_deleted_input_or_a_reported_error_is_never_the_stored_value(
    snapshot: Any, tmp_path: Path
) -> None:
    # Cost deleted in Desktop: Margin (same DAX) now errors there.
    head = copy.deepcopy(FACTS)
    head["columns"] = [c for c in head["columns"] if c["name"] != "Cost"]
    for column in head["columns"]:
        if column["name"] == "Margin":
            column["error"] = "The column 'Cost' cannot be found."
    report = report_for(head)
    model = report_engine._build(report, snapshot)
    assert refused(model, report, 'EVALUATE ROW("m", [Total Margin])') == (
        "Power BI reports a problem with the calculated column 'FactSales'[Margin] (The column "
        "'Cost' cannot be found.); Power BI shows visuals that use it."
    )
    # A calculated column reading a measure that was deleted: calculated again, and refused.
    set_rows(snapshot, tmp_path, "DimStore", STORES.assign(**{"Store Sales": [30.0, 70.0, 110.0]}))
    now = with_columns(_calc("DimStore", "Store Sales", 8, "[Old Measure]"))
    then = copy.deepcopy(now)
    then["measures"].append(
        {"table": "# Measures", "name": "Old Measure", "expression": "SUM(FactSales[Amount])"}
    )
    report = report_for(now, then)
    model = report_engine._build(report, snapshot)
    assert refused(
        model, report, 'EVALUATE SELECTCOLUMNS(DimStore, "s", DimStore[Store Sales])'
    ).startswith("Studio cannot calculate the calculated column 'DimStore'[Store Sales]")
    # A measure Power BI reports an error in is refused, not calculated from the rows.
    broken = copy.deepcopy(FACTS)
    broken["measures"][0]["error"] = "Circular dependency"
    report = report_for(broken)
    model = report_engine._build(report, snapshot)
    assert refused(model, report, 'EVALUATE ROW("s", [Total Sales])') == (
        "Power BI reports a problem with the measure [Total Sales] (Circular dependency); Power "
        "BI shows visuals that use it."
    )


def test_without_the_rows_own_model_every_calculation_is_calculated_again(
    snapshot: Any,
) -> None:
    head = with_columns(
        _calc("FactSales", "Margin", 8, "FactSales[Amount] - 2 * [Cost]"),
        replace="FactSales.Margin",
    )
    model = report_engine._build(report_for(head, snapshot_model=None), snapshot)
    assert rows(model, 'EVALUATE ROW("m", [Total Margin])') == [[132.0]]
    assert model.computed == ["'Date'", "'Top N'", "'FactSales'[Margin]"]  # type: ignore[attr-defined]
    # a DATATABLE the engine cannot evaluate: its stored rows are not trusted either
    assert unavailable_fields(model)[("kinds", None)].startswith(
        "Studio cannot calculate the calculated table 'Kinds'"
    )


def test_a_bare_column_in_a_row_context_is_refused_like_a_named_one(snapshot: Any) -> None:
    head = changed(columns=[*FACTS["columns"], _column("FactSales", "Discount", 8)])
    head["measures"].append(
        {"table": "# Measures", "name": "Disc", "expression": "SUMX(FactSales, [Discount])"}
    )
    report = report_for(head)
    model = report_engine._build(report, snapshot)
    for query in (
        'EVALUATE ROW("d", [Disc])',
        'EVALUATE ROW("d", SUMX(FactSales, [Discount]))',
        'EVALUATE ROW("d", SUMX(FactSales, FactSales[Discount]))',
    ):
        assert refused(model, report, query).startswith("'FactSales'[Discount] was added")


def test_a_failed_read_of_a_changed_model_refuses_rather_than_use_old_joins(
    snapshot: Any, tmp_path: Path
) -> None:
    """The head's facts could not be read after Desktop saved another model: the joins and
    columns Studio holds are the old model's, so the engine answers nothing."""
    after_failed_read = report_for(FACTS).model_copy(
        update={"model": None, "relationships_version": 0}
    )
    with pytest.raises(DaxModelIncomplete, match="could not read the model Power BI Desktop"):
        report_engine._build(after_failed_read, snapshot)
    # The same model as the rows' (its key matches the digest): the rows answer, but a
    # calculation group or a field parameter the rows' model had stays Power BI's.
    archive = tmp_path / "head.pbix"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("DataModel", b"the model")
    key = model_key(archive)
    rows_model = copy.deepcopy(FACTS)
    rows_model["tables"].append(_table("Picker", "t-p", "calculated", "{ NAMEOF([Total Sales]) }"))
    same = report_for(FACTS, rows_model).model_copy(
        update={
            "model": None,
            "archived_path": str(archive),
            "snapshot_model": {**model_facts.digest(rows_model, key)},
        }
    )
    model = report_engine._build(same, snapshot)
    assert unavailable_fields(model) == {
        ("picker", None): "'Picker' is a field parameter, which Studio's canvas does not "
        "switch; Power BI shows visuals that use it."
    }
    assert rows(model, BY_STORE)[0] == ["A", 30.0, 21.0]


def test_what_power_bi_fixed_or_formats_its_own_way_is_not_calculated(snapshot: Any) -> None:
    head = with_columns(
        _calc("DimStore", "Seen", 9, "TODAY()"),
        _calc("DimStore", "Label", 2, 'FORMAT(DimStore[StoreKey], "0.0")'),
        _calc("DimStore", "Fixed", 2, 'FORMAT(DimStore[StoreKey], "0.0", "en-US")'),
    )
    head["model"] = {"culture": "de-DE"}
    model = report_engine._build(report_for(head), snapshot)
    unavailable = unavailable_fields(model)
    assert unavailable[("dimstore", "seen")] == (
        "Studio cannot calculate the calculated column 'DimStore'[Seen] (it uses TODAY, whose "
        "value Power BI fixed when it calculated it); Power BI shows visuals that use it."
    )
    assert unavailable[("dimstore", "label")].startswith(
        "Studio cannot calculate the calculated column 'DimStore'[Label] (it uses FORMAT, "
        "whose result follows the model's culture (de-de)"
    )
    # an explicit en-US locale formats the same in Studio and Power BI
    assert rows(model, 'EVALUATE SELECTCOLUMNS(DimStore, "f", DimStore[Fixed])') == [
        ["1.0"],
        ["2.0"],
        ["3.0"],
    ]
    head["model"] = {"culture": "en-US"}
    english = report_engine._build(report_for(head), snapshot)
    assert ("dimstore", "label") not in unavailable_fields(english)


def test_live_tables_and_date_part_joins_are_power_bi_only(snapshot: Any, tmp_path: Path) -> None:
    head = copy.deepcopy(FACTS)
    head["partitions"] = [
        {"table": "FactSales", "source": "m", "mode": "import"},
        {"table": "# Measures", "source": "m", "mode": "import"},
        {"table": "Top N", "source": "calculated", "mode": "import"},
        {"table": "DimStore", "source": "m", "mode": "directQuery"},
    ]
    head["relationships"] = [FACTS["relationships"][1]]
    model = report_engine._build(report_for(head), snapshot)
    assert unavailable_fields(model)[("dimstore", None)] == (
        "'DimStore' is read live from its source (DirectQuery); Power BI shows visuals that use it."
    )
    # Auto date/time's join matches the date alone: with times of day in the column the date
    # table leaves the model; the rest still answers.
    timed = SALES.assign(DateKey=pd.to_datetime(SALES["DateKey"]) + pd.to_timedelta(9, unit="h"))
    set_rows(snapshot, tmp_path, "FactSales", timed)
    local = copy.deepcopy(FACTS)
    local["relationships"] = [
        FACTS["relationships"][0],
        {**_relationship(("FactSales", "DateKey"), ("Date", "Date")), "joinOnDate": "datePartOnly"},
    ]
    model = report_engine._build(report_for(local), snapshot)
    assert unavailable_fields(model)[("date", None)].startswith(
        "'Date' is joined to 'FactSales'[DateKey] on the date part only"
    )
    assert rows(model, BY_STORE)[0] == ["A", 30.0, 21.0]
    # midnights only: the engine's whole-value join is the same join
    set_rows(snapshot, tmp_path, "FactSales", SALES)
    exact = report_engine._build(report_for(local), snapshot)
    assert unavailable_fields(exact) == {}
