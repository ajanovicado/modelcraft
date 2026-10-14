"""The Agents dashboard's model as the editor sees it, for tests on the structure twin and on the
real file.

The editor builds its ``ModelInfo`` with ``ReportEditor._model_info``: the import's tables and
measures plus the model's own data types (``pbix_extract.types``). A test that leaves the types
out treats every measure as a number, so an edit that Studio refuses on the real dashboard (a
color scale on a text measure) passes there. These helpers build the model the same way.

The twin's DataModel is filler that pbixray cannot open, so its types are kept here: the data
type of every field the twin's report uses, as the real model has it (names already in the
fixture and type ids, no data). ``test_report_edit_conditional`` checks that the list covers
every field the twin uses and, where the real dashboard is on this machine, that it agrees with
the real model.
"""

from __future__ import annotations

import functools
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ai_powerbi_analyst.studio import pbix_extract
from ai_powerbi_analyst.studio.report_edit_context import ModelInfo
from ai_powerbi_analyst.studio.report_editor import ReportEditor
from ai_powerbi_analyst.studio.report_import import ImportedReport

REAL_AGENTS = Path.home() / "Downloads" / "Agents Performance - Dashboard.pbix"
MEASURE_TABLE = "# Measures"

# Model data type ids: 2 text, 6 whole number, 8 decimal number, 9 date/time, 10 fixed decimal,
# 11 true/false, 20 variant (a measure that returns numbers or text).
TWIN_MEASURE_TYPES: dict[str, int] = {
    "CF Table": 2,
    "Employee Name": 2,
    "Employees AVG MTD Sales Target": 6,
    "Employees Avg MTD % Change": 8,
    "Employees Avg MTD % Change vs Previous Month": 2,
    "Employees Avg MTD % change PM": 8,
    "Employees Avg MTD % change Target": 8,
    "Employees Avg MTD Sales": 8,
    "Employees Avg MTD Sales PM": 8,
    "Employees Avg MTD Sales vs Previous Month": 2,
    "Employees Positive Change vs Previous Month": 2,
    "Employees with Positive Change %": 8,
    "KPI, Avg MTD Sales towards Target": 2,
    "KPI, Employees Avg MTD % Change towards Target": 2,
    "KPI, Number of Employees sold above 100K towards Target": 2,
    "KPI, Positive Change towards Target": 2,
    "Number of Employees selling more than 100K MTD": 6,
    "Number of Employees selling more than 100K PM": 6,
    "Number of Employees selling more than 100K Target": 6,
    "Number of Employees sold more than 100K vs Previous Month": 2,
    "Number of Employees with Positive change PM": 8,
    "Rank": 6,
    "Rank Filtering Dynamics": 6,
    "Rank Filtering Employyees MTD% CF": 2,
    "Rank Filtering Employyees Sales MTD CF": 2,
    "SVG 100% Stacked Bar Chart Employees Avg MTD % change": 2,
    "SVG 100% Stacked Bar Chart Employees Avg MTD Sales": 2,
    "SVG 100% Stacked Bar Chart Employees Positive Change": 2,
    "SVG 100% Stacked Bar Chart Number of Employees Selling more than 100K": 2,
    "Table MTD Sales": 2,  # formatted text: "⋮" for the middle rows
    "Target Employees Positive Change %": 8,
    "TopN/BottomN/ Avg MTD Sales": 8,
    "TopN/BottomN/ Avg Total Discount": 8,
    "TopN/BottomN/ MTD % Change": 8,
    "TopN/BottomN/ MTD Sales": 8,
    "dummy": 8,
    "vs PM MOM%": 20,
    "vs Previous Month": 8,
}
# (table, column) -> (data type id, summarize by)
TWIN_COLUMN_TYPES: dict[tuple[str, str], tuple[int, str]] = {
    ("Actuals-vs-PctChange", "Top-Bottom-Field"): (2, "none"),
    ("Clustered Employees", "Clustered Employees"): (2, "none"),
    ("DimChannel", "ChannelName"): (2, "none"),
    ("DimEmployee", "EmployeeKey"): (6, "none"),
    ("DimStore", "StoreName"): (2, "none"),
    ("Top-Bottom-N", "Top-Bottom-N"): (6, "none"),
}

# The import's pandas dtype for a model data type (report_import's rows carry these).
_PANDAS = {2: "object", 6: "int64", 8: "float64", 9: "datetime64[ns]", 10: "float64", 11: "bool"}


def twin_types() -> dict[str, Any]:
    """The twin's types payload, shaped like ``pbix_extract.types``."""
    return {
        "columns": [
            {"table": table, "name": name, "dataType": data_type, "summarizeBy": summarize}
            for (table, name), (data_type, summarize) in TWIN_COLUMN_TYPES.items()
        ],
        "measures": [
            {"table": MEASURE_TABLE, "name": name, "dataType": data_type}
            for name, data_type in TWIN_MEASURE_TYPES.items()
        ],
    }


def rows_of(types: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The import's table and measure rows for the fields of a types payload."""
    columns = types.get("columns")
    measures = types.get("measures")
    tables = [
        {
            "TableName": row["table"],
            "ColumnName": row["name"],
            "PandasDataType": _PANDAS.get(row.get("dataType"), "object"),
        }
        for row in (columns if isinstance(columns, list) else [])
    ]
    found = [
        {"Name": row["name"], "TableName": row["table"]}
        for row in (measures if isinstance(measures, list) else [])
    ]
    return tables, found


def model_info(
    tables: Iterable[Mapping[str, Any]],
    measures: Iterable[Mapping[str, Any]],
    types: Mapping[str, Any] | None,
) -> ModelInfo:
    """The ``ModelInfo`` the editor would build: ``ReportEditor._model_info`` itself, given the
    import's rows and the types payload (its calculated DAX too, as the editor's facts carry)."""
    table_rows = [dict(row) for row in tables]
    measure_rows = [dict(row) for row in measures]
    report = ImportedReport(
        id="agents",
        workspace_id="tests",
        name="Agents Performance",
        source_path="",
        archived_path="",
        source_hash="",
        base_hash="",
        format="pbix",
        pages=[],
        tables=table_rows,
        measures=measure_rows,
    )
    calculated = types.get("calculated") if types is not None else None
    return ReportEditor._model_info(
        report,
        measure_rows,
        (types, calculated if isinstance(calculated, Mapping) else None),
    )


def twin_model(
    extra_tables: Iterable[Mapping[str, Any]] = (),
    extra_types: Iterable[Mapping[str, Any]] = (),
) -> ModelInfo:
    """The twin's model with its real types, plus any synthetic columns a test adds (their
    import rows and their ``columns`` type rows)."""
    types = twin_types()
    types["columns"].extend(dict(row) for row in extra_types)
    tables, measures = rows_of(twin_types())
    return model_info([*tables, *extra_tables], measures, types)


@functools.cache
def real_types(path: Path = REAL_AGENTS) -> dict[str, Any]:
    """``pbix_extract.types`` of the real dashboard, read once per test run (read-only)."""
    return pbix_extract.types(path)


def real_model(path: Path = REAL_AGENTS) -> ModelInfo:
    """The real dashboard's model as the editor builds it from its types payload."""
    types = real_types(path)
    tables, measures = rows_of(types)
    return model_info(tables, measures, types)
