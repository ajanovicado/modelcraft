"""Isolated, read-only VertiPaq extraction. Invoked by report_import, never a shell."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
from pbixray import PBIXRay

# Attribute-hierarchy (H$), relationship-index (R$) and user-hierarchy (U$) storage
# tables are engine internals, never model tables a report author created.
_INTERNAL_PREFIXES = ("H$", "R$", "U$")
_RELATIONSHIP_ENDS = (
    ("From", "TableID"),
    ("From", "ColumnID"),
    ("From", "Cardinality"),
    ("To", "TableID"),
    ("To", "ColumnID"),
    ("To", "Cardinality"),
)


def _relationship_sql(columns: set[str]) -> str:
    # Older model schemas spell the endpoint columns FromEnd*/ToEnd*.
    infix = "End" if "FromColumnID" not in columns and "FromEndColumnID" in columns else ""
    name = {side + part: side + infix + part for side, part in _RELATIONSHIP_ENDS}
    # Fixed SQL: the only substitutions are the fixed identifiers above, no user
    # text. Column names fall back to InferredName because calculated-table
    # columns (e.g. a CALENDAR() date table) have no ExplicitName.
    return (
        "SELECT ft.Name AS FromTableName, ft.SystemFlags AS FromSystemFlags, "  # noqa: S608
        "COALESCE(fc.ExplicitName, fc.InferredName) AS FromColumnName, "
        "tt.Name AS ToTableName, tt.SystemFlags AS ToSystemFlags, "
        "COALESCE(tc.ExplicitName, tc.InferredName) AS ToColumnName, "
        f"r.IsActive, r.{name['FromCardinality']} AS FromCardinality, "
        f"r.{name['ToCardinality']} AS ToCardinality, "
        "r.CrossFilteringBehavior, r.RelyOnReferentialIntegrity "
        "FROM Relationship r "
        f"JOIN [Table] ft ON r.{name['FromTableID']} = ft.ID "
        f"JOIN [Column] fc ON r.{name['FromColumnID']} = fc.ID "
        f"JOIN [Table] tt ON r.{name['ToTableID']} = tt.ID "
        f"JOIN [Column] tc ON r.{name['ToColumnID']} = tc.ID "
        "ORDER BY r.ID"
    )


def _internal(name: object, flags: object) -> bool:
    # SystemFlags bit 1 marks engine-owned tables. Calculated tables carry bit 2 and
    # are real model tables (PBIXRay's own query drops them, losing Date joins).
    try:
        system = int(flags) if isinstance(flags, str | int | float) else 0
    except (TypeError, ValueError):
        system = 0
    return bool(system & 1) or not isinstance(name, str) or name.startswith(_INTERNAL_PREFIXES)


def metadata_relationships(db: Any) -> list[dict[str, Any]] | None:
    """Every author relationship, active or not; None when the metadata query fails."""
    try:
        info = db.query("PRAGMA table_info('Relationship')")
        columns = {str(c) for c in info["name"]} if "name" in info.columns else set()
        frame = db.query(_relationship_sql(columns))
    except Exception:  # noqa: BLE001 - any metadata failure falls back to PBIXRay's view
        return None
    # PBIXRay's reader returns a column-less frame for both SQL errors and no rows.
    if "FromTableName" not in frame.columns:
        return None
    relationships = []
    for row in json.loads(frame.to_json(orient="records")):
        if (
            _internal(row["FromTableName"], row["FromSystemFlags"])
            or _internal(row["ToTableName"], row["ToSystemFlags"])
            or not row["FromColumnName"]
            or not row["ToColumnName"]
        ):
            continue
        # Cardinality 1 = one, 2 = many, as in PBIXRay's own relationship view.
        ends = ("M" if row[f"{side}Cardinality"] == 2 else "1" for side in ("From", "To"))
        # 2 = both directions. 1 (single) and 3 (automatic) stay single, the
        # conservative choice for filter propagation.
        both = row["CrossFilteringBehavior"] == 2
        relationships.append(
            {
                "FromTableName": row["FromTableName"],
                "FromColumnName": row["FromColumnName"],
                "ToTableName": row["ToTableName"],
                "ToColumnName": row["ToColumnName"],
                "IsActive": 1 if row["IsActive"] else 0,
                "Cardinality": ":".join(ends),
                "CrossFilteringBehavior": "Both" if both else "Single",
                "RelyOnReferentialIntegrity": 1 if row["RelyOnReferentialIntegrity"] else 0,
            }
        )
    return relationships


def _metadata_relationships(model: Any) -> list[dict[str, Any]] | None:
    source = getattr(getattr(model, "_metadata", None), "source", None)
    db = getattr(source, "_db", None)
    return metadata_relationships(db) if db is not None else None


def relationships(model: Any) -> list[dict[str, Any]]:
    found = _metadata_relationships(model)
    if found is None:
        # Compatibility fallback: PBIXRay's view omits calculated-table ends.
        found = json.loads(model.relationships.to_json(orient="records"))
    return found


def read_relationships(source: Path) -> list[dict[str, Any]] | None:
    """Only the metadata relationships, for a report imported before they were kept."""
    import tempfile

    with (
        tempfile.TemporaryDirectory() as tmp,
        PBIXRay(str(source), on_disk=True, temp_dir=tmp) as model,
    ):
        return _metadata_relationships(model)


# Model type codes as the metadata database stores them, which are the Analysis Services
# (TOM) enum values. Read from the Agents Performance report on 2026-09-28:
# - DataType / ExplicitDataType / InferredDataType: 1 automatic (a calculated column's
#   ExplicitDataType, the real type is then in InferredDataType), 2 text, 6 whole number,
#   8 float, 9 date/time, 10 fixed decimal, 11 true/false, 17 binary, 19 unknown (an
#   imported column's InferredDataType) and 20 variant (a measure with no fixed type).
#   Data literals follow from it: 2 -> '..', 6 -> 45L, 8 -> 1.5D, 10 -> 1.5M, 9 ->
#   datetime'..', 11 -> true.
# - SummarizeBy: 1 default, 2 none, 3 sum, 4 min, 5 max, 6 count, 7 average, 8 distinct
#   count. Every FactSales column (keys and amounts alike) is 2 (none), the RowNumber
#   column of each table is 1, and the Order columns of the two calculated tables are 3.
# - Column Type: 1 data, 2 calculated, 3 row number (an engine column no field can
#   bind to, one per table), 4 calculated-table column.
_SUMMARIZE_BY = {
    1: "default",
    2: "none",
    3: "sum",
    4: "min",
    5: "max",
    6: "count",
    7: "average",
    8: "distinctCount",
}
_ROW_NUMBER_COLUMN = 3
# Ids that say nothing about the type: unset, automatic, unknown.
_NO_TYPE = (None, 0, 1, 19)


def _optional(columns: set[str], alias: str, name: str, table: str) -> str:
    # Older or newer model schemas may lack a column; the value is then NULL. Every
    # identifier substituted here is a constant of this module, never user text.
    return f"{table}.{name} AS {alias}" if name in columns else f"NULL AS {alias}"


def _types_sql(
    table_names: set[str], column_names: set[str], measure_names: set[str]
) -> tuple[str, str]:
    # Like the relationship query, names fall back to InferredName because calculated-table
    # columns have no ExplicitName.
    name_parts = [f"c.{n}" for n in ("ExplicitName", "InferredName") if n in column_names]
    if not name_parts:
        raise ValueError("The model metadata has no column names")
    # COALESCE needs two arguments.
    column_name = f"COALESCE({', '.join(name_parts)})" if len(name_parts) > 1 else name_parts[0]
    flags = _optional(table_names, "TableSystemFlags", "SystemFlags", "t")
    columns = (
        f"SELECT t.Name AS TableName, {flags}, "  # noqa: S608
        f"{column_name} AS ColumnName, "
        f"{_optional(column_names, 'ExplicitDataType', 'ExplicitDataType', 'c')}, "
        f"{_optional(column_names, 'InferredDataType', 'InferredDataType', 'c')}, "
        f"{_optional(column_names, 'SummarizeBy', 'SummarizeBy', 'c')}, "
        f"{_optional(column_names, 'IsHidden', 'IsHidden', 'c')}, "
        f"{_optional(column_names, 'IsKey', 'IsKey', 'c')}, "
        f"{_optional(column_names, 'ColumnType', 'Type', 'c')} "
        "FROM [Column] c JOIN [Table] t ON c.TableID = t.ID ORDER BY c.ID"
    )
    measures = (
        f"SELECT t.Name AS TableName, {flags}, "  # noqa: S608
        "m.Name AS MeasureName, "
        f"{_optional(measure_names, 'DataType', 'DataType', 'm')}, "
        f"{_optional(measure_names, 'IsHidden', 'IsHidden', 'm')} "
        "FROM Measure m JOIN [Table] t ON m.TableID = t.ID ORDER BY m.ID"
    )
    return columns, measures


def _code(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _data_type(*candidates: object) -> int | None:
    for candidate in candidates:
        code = _code(candidate)
        if code not in _NO_TYPE:
            return code
    return None


def _table_names(db: Any, table: str) -> set[str]:
    info = db.query(f"PRAGMA table_info('{table}')")  # fixed table names only
    return {str(c) for c in info["name"]} if "name" in info.columns else set()


def metadata_types(db: Any) -> dict[str, Any] | None:
    """Data types and default summarization of every author column, and measure types.

    Columns: table, name, dataType (ExplicitDataType, else InferredDataType, None when
    the model does not say), summarizeBy (the model's SummarizeBy as a name, with the raw
    code in summarizeByCode), hidden, key. Measures: table, name, dataType, hidden.
    Engine tables and row-number columns are left out. None when the metadata query
    fails, so the caller can tell "no types known" from "no columns".
    """
    try:
        column_sql, measure_sql = _types_sql(
            _table_names(db, "Table"), _table_names(db, "Column"), _table_names(db, "Measure")
        )
        column_frame = db.query(column_sql)
        measure_frame = db.query(measure_sql)
    except Exception:  # noqa: BLE001 - any metadata failure means the types are unknown
        return None
    # PBIXRay's reader returns a column-less frame for both SQL errors and no rows.
    if "TableName" not in column_frame.columns or "TableName" not in measure_frame.columns:
        return None
    columns = []
    for row in json.loads(column_frame.to_json(orient="records")):
        if (
            _internal(row["TableName"], row["TableSystemFlags"])
            or not row["ColumnName"]
            or _code(row["ColumnType"]) == _ROW_NUMBER_COLUMN
        ):
            continue
        summarize = _code(row["SummarizeBy"])
        columns.append(
            {
                "table": row["TableName"],
                "name": row["ColumnName"],
                "dataType": _data_type(row["ExplicitDataType"], row["InferredDataType"]),
                "summarizeBy": _SUMMARIZE_BY.get(summarize) if summarize is not None else None,
                "summarizeByCode": summarize,
                "hidden": bool(row["IsHidden"]),
                "key": bool(row["IsKey"]),
            }
        )
    measures = []
    for row in json.loads(measure_frame.to_json(orient="records")):
        if _internal(row["TableName"], row["TableSystemFlags"]) or not row["MeasureName"]:
            continue
        measures.append(
            {
                "table": row["TableName"],
                "name": row["MeasureName"],
                "dataType": _data_type(row["DataType"]),
                "hidden": bool(row["IsHidden"]),
            }
        )
    return {"columns": columns, "measures": measures}


def metadata_calculated(model: Any) -> dict[str, str] | None:
    """The DAX of the model's calculated tables and calculated columns.

    ``{"Table": dax}`` for a calculated table (the DAX that defines it, so a field parameter's
    ``NAMEOF`` list is in it) and ``{"Table[Column]": dax}`` for a calculated column. The edit
    layer reads them to see what a rename or delete of a measure would break. The columns of a
    calculated table are not listed on their own: they are defined by the table's DAX. None when
    PBIXRay cannot give them, so "no calculated objects" (an empty dict) is told from "unknown".
    """
    try:
        table_rows = json.loads(model.dax_tables.to_json(orient="records"))
        column_rows = json.loads(model.dax_columns.to_json(orient="records"))
    except Exception:  # noqa: BLE001 - any failure means the calculated objects are unknown
        return None
    found: dict[str, str] = {}
    for row in table_rows:
        name, dax = row.get("TableName"), row.get("Expression")
        if isinstance(name, str) and name and isinstance(dax, str) and not _internal(name, 0):
            found[name] = dax
    for row in column_rows:
        table, name, dax = row.get("TableName"), row.get("ColumnName"), row.get("Expression")
        if (
            isinstance(table, str)
            and isinstance(name, str)
            and name
            and isinstance(dax, str)
            and not _internal(table, 0)
        ):
            found[f"{table}[{name}]"] = dax
    return found


def types(source: Path) -> dict[str, Any]:
    """Column and measure data types for typing filter and color literals from the model, and
    the DAX of its calculated columns and tables (``calculated``, left out when unknown).

    Reads only embedded metadata, never table rows. A file PBIXRay cannot open raises, like
    ``formats``; a readable model whose metadata query fails gives empty lists.
    """
    import tempfile

    with (
        tempfile.TemporaryDirectory() as tmp,
        PBIXRay(str(source), on_disk=True, temp_dir=tmp) as model,
    ):
        db = getattr(getattr(model._metadata, "source", None), "_db", None)
        found = metadata_types(db) if db is not None else None
        result = found if found is not None else {"columns": [], "measures": []}
        calculated = metadata_calculated(model)
        return result if calculated is None else {**result, "calculated": calculated}


# --- model facts -------------------------------------------------------------------------------
#
# Everything the model says about itself except table rows, for Studio's view of a model that
# Power BI Desktop changed: tables, columns, measures, relationships, hierarchies, roles,
# calculation groups, field and what-if parameters, partitions, named expressions, settings.
# One single-table SELECT per metadata table, over fixed identifiers only (a field this schema
# lacks reads as NULL, found with PRAGMA table_info), joined here in Python. Every section is
# optional: one the metadata cannot give is left out, never a crash.

FACTS_VERSION = 1
# Longer texts are data in disguise (an "Enter data" table is base64 inside its Power Query).
_MAX_TEXT = 1_000_000
# Names follow pbir_expr.data_type_name; 17 binary and 20 variant exist only here.
_DATA_TYPE_NAMES = {
    2: "text",
    6: "integer",
    8: "double",
    9: "datetime",
    10: "decimal",
    11: "boolean",
    17: "binary",
    20: "variant",
}
# Column Type: 3 (the engine's row-number column) is never listed.
_COLUMN_KINDS = {1: "data", 2: "calculated", 4: "calculatedTableColumn"}
# Analysis Services enums as the metadata stores them: every map below was read on 2026-09-29
# from the Tabular object model assembly itself (Microsoft.AnalysisServices.Tabular.dll, by
# reflection: PartitionSourceType, ModeType, CrossFilteringBehavior, SecurityFilteringBehavior,
# DateTimeRelationshipBehavior, ModelPermission, CalculationGroupSelectionMode, ObjectState,
# ObjectType), and the Agents Performance report agrees (Partition.Type 4 Power Query, 2 a
# calculated table whose QueryDefinition is the DAX, Mode 0 import, Cardinality 1 one / 2 many).
_PARTITION_SOURCES = {
    1: "query",
    2: "calculated",
    3: "none",
    4: "m",
    5: "entity",
    6: "policyRange",
    7: "calculationGroup",
    8: "inferred",
    9: "parquet",
}
_MODES = {0: "import", 1: "directQuery", 2: "default", 3: "push", 4: "dual", 5: "directLake"}
_CROSS_FILTER = {1: "single", 2: "both", 3: "automatic"}
_SECURITY_FILTER = {1: "single", 2: "both", 3: "none"}
_JOIN_ON_DATE = {1: "dateAndTime", 2: "datePartOnly"}
_PERMISSIONS = {1: "none", 2: "read", 3: "readRefresh", 4: "refresh", 5: "administrator"}
_SELECTION_MODES = {1: "multipleOrEmptySelection", 2: "noSelection"}
# ObjectState: 1 Ready; anything else is what Desktop shows as a problem with the object.
_STATES = {
    3: "noData",
    4: "calculationNeeded",
    5: "semanticError",
    6: "evaluationError",
    7: "dependencyError",
    8: "incomplete",
    10: "forceCalculationNeeded",
}
# ExpressionKind has one value, 0 Power Query (M).
_EXPRESSION_KINDS = {0: "m"}
# ObjectType of the owner of an annotation, extended property or format string definition.
_MODEL_OBJECT = 1
_TABLE_OBJECT = 3
_COLUMN_OBJECT = 4
_MEASURE_OBJECT = 8
_CALCULATION_ITEM_OBJECT = 47
# Auto date/time: the model annotation says whether new date columns get a hidden date table
# ("0" on the Agents report), and Desktop marks the tables it made with annotations (a
# LocalDateTable per date column, shown only as variations, and a private DateTableTemplate).
# The table annotations were not in a Desktop-authored file here: they are Desktop's documented
# names.
_AUTO_DATE_TIME = "__PBI_TimeIntelligenceEnabled"
_AUTO_DATE_TABLES = {"__PBI_LocalDateTable": "local", "__PBI_TemplateDateTable": "template"}
# Date, DateTime or Time behind a date/time column (Agents: "Date" on FactSales[DateKey]).
_UNDERLYING_DATE_TIME = "UnderlyingDateTimeDataType"
_DATE_TIME_TYPES = {"date": "date", "datetime": "dateTime", "time": "time"}
# A what-if parameter's column carries {"version":0} (the Agents report's Top-Bottom-N); a
# field parameter's {"version":3,"kind":2}.
_PARAMETER_PROPERTY = "ParameterMetadata"
_FIELD_PARAMETER_KIND = 2
_PARAMETER_QUERY = re.compile(r"IsParameterQuery\s*=\s*true", re.IGNORECASE)
# Role members are identities (emails) and are never read.
_TABLE_FIELDS = (
    "ID",
    "Name",
    "Description",
    "DataCategory",
    "IsHidden",
    "IsPrivate",
    "ShowAsVariationsOnly",
    "SystemFlags",
    "CalculationGroupID",
    "LineageTag",
)
_COLUMN_FIELDS = (
    "ID",
    "TableID",
    "ExplicitName",
    "InferredName",
    "ExplicitDataType",
    "InferredDataType",
    "DataCategory",
    "Description",
    "IsHidden",
    "IsKey",
    "SummarizeBy",
    "Type",
    "BindingType",
    "SourceColumn",
    "Expression",
    "FormatString",
    "SortByColumnID",
    "DisplayFolder",
    "LineageTag",
    "State",
    "ErrorMessage",
)
_MEASURE_FIELDS = (
    "ID",
    "TableID",
    "Name",
    "Expression",
    "FormatString",
    "DisplayFolder",
    "Description",
    "IsHidden",
    "DataType",
    "LineageTag",
    "FormatStringDefinitionID",
    "State",
    "ErrorMessage",
)
_RELATIONSHIP_FIELDS = (
    "ID",
    "Name",
    "IsActive",
    "CrossFilteringBehavior",
    "JoinOnDateBehavior",
    "RelyOnReferentialIntegrity",
    "SecurityFilteringBehavior",
    *(side + part for side in ("From", "To", "FromEnd", "ToEnd") for part in (
        "TableID", "ColumnID", "Cardinality"
    )),
)  # fmt: skip
_HIERARCHY_FIELDS = ("ID", "TableID", "Name", "Description", "IsHidden", "DisplayFolder")
_LEVEL_FIELDS = ("ID", "HierarchyID", "Ordinal", "Index", "Name", "ColumnID")
_ROLE_FIELDS = ("ID", "Name", "Description", "ModelPermission")
_TABLE_PERMISSION_FIELDS = ("ID", "RoleID", "TableID", "FilterExpression")
_GROUP_FIELDS = ("ID", "TableID", "Precedence", "Description")
_ITEM_FIELDS = (
    "ID",
    "CalculationGroupID",
    "Name",
    "Expression",
    "Ordinal",
    "FormatStringDefinitionID",
    "Description",
)
_SELECTION_FIELDS = ("ID", "CalculationGroupID", "Expression", "SelectionMode")
_FORMAT_FIELDS = ("ID", "ObjectID", "ObjectType", "Expression")
_PARTITION_FIELDS = (
    "ID",
    "TableID",
    "Name",
    "Type",
    "Mode",
    "QueryDefinition",
    "RefreshedTime",
    "State",
    "ErrorMessage",
)
_EXPRESSION_FIELDS = ("ID", "Name", "Description", "Kind", "Expression")
_ANNOTATION_FIELDS = ("ID", "ObjectID", "ObjectType", "Name", "Value")
_VARIATION_FIELDS = ("ID", "ColumnID", "Name", "RelationshipID", "DefaultHierarchyID", "IsDefault")
_MODEL_FIELDS = (
    "ID",
    "Culture",
    "DiscourageImplicitMeasures",
    "DefaultMode",
    "SourceQueryCulture",
)
_PROPERTY_FIELDS = ("ID", "ObjectID", "ObjectType", "Name", "Value")


def _rows(
    db: Any, table: str, fields: tuple[str, ...], required: tuple[str, ...] = ("ID",)
) -> list[dict[str, Any]] | None:
    """Every row of one metadata table as dicts over ``fields``; None when the table or a
    required field is missing or the query fails. ``table`` and ``fields`` are constants."""
    try:
        present = _table_names(db, table)
        if not present or any(name not in present for name in required):
            return None
        select = ", ".join(f"[{f}]" if f in present else f"NULL AS [{f}]" for f in fields)
        order = " ORDER BY [ID]" if "ID" in present else ""
        frame = db.query(f"SELECT {select} FROM [{table}]{order}")  # noqa: S608 - constants
    except Exception:  # noqa: BLE001 - a metadata table this schema cannot give is unknown
        return None
    if not len(frame.columns):
        return []  # PBIXRay's reader answers "no rows" with a column-less frame
    rows: list[dict[str, Any]] = json.loads(frame.to_json(orient="records"))
    return rows


def _name(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _flag(value: object) -> bool:
    return bool(_code(value))


def _filetime(value: object) -> str | None:
    """A FILETIME (100 ns ticks since 1601) as an ISO UTC time; None when unset."""
    ticks = _code(value)
    if ticks is None or ticks <= 0:
        return None
    try:
        moment = datetime(1601, 1, 1, tzinfo=UTC) + timedelta(microseconds=ticks // 10)
    except OverflowError:
        return None
    return moment.isoformat(timespec="seconds")


def _bounded(item: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and len(value) > _MAX_TEXT:
            item[key] = value[:_MAX_TEXT]
            item["truncated"] = True
    return item


def _add(facts: dict[str, Any], key: str, build: Callable[[], object]) -> None:
    try:
        value = build()
    except Exception:  # noqa: BLE001 - a section the metadata cannot give is left out
        return
    if value is not None:
        facts[key] = value


# The table constructor a field parameter is defined by: {("Label", NAMEOF('T'[Field]), 0), ...}.
_DAX_TOKEN = re.compile(
    r"""
    (?P<ws>\s+|//[^\n]*|--[^\n]*|/\*.*?\*/)
  | (?P<str>"(?:[^"]|"")*")
  | (?P<table>'(?:[^']|'')*')
  | (?P<bracket>\[(?:[^\]]|\]\])*\])
  | (?P<num>-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<ident>[A-Za-z_][\w.]*)
  | (?P<op>[{}(),])
    """,
    re.S | re.X,
)


def _dax_tokens(text: str) -> list[tuple[str, str]] | None:
    tokens = []
    position = 0
    while position < len(text):
        match = _DAX_TOKEN.match(text, position)
        if match is None:
            return None
        kind = match.lastgroup or ""
        if kind != "ws":
            tokens.append((kind, match.group()))
        position = match.end()
    return tokens


def field_parameter_rows(dax: str) -> list[dict[str, Any]] | None:
    """The rows of a field parameter's DAX: ``[{"label", "table", "name", "order"}]`` in the
    order written (``table`` None for ``NAMEOF([Name])``); None when the DAX is not a table
    constructor of such rows."""
    tokens = _dax_tokens(dax)
    if not tokens or tokens[0] != ("op", "{") or tokens[-1] != ("op", "}"):
        return None
    try:
        return _constructor_rows(tokens)
    except IndexError:  # the text ended inside a row
        return None


def _constructor_rows(tokens: list[tuple[str, str]]) -> list[dict[str, Any]] | None:
    rows: list[dict[str, Any]] = []
    position = 1
    while position < len(tokens) - 1:
        if tokens[position] != ("op", "("):
            return None
        row: dict[str, Any] = {"label": None, "table": None, "name": None, "order": None}
        position += 1
        while True:
            kind, text = tokens[position]
            if kind == "str" and row["label"] is None:
                row["label"] = text[1:-1].replace('""', '"')
                position += 1
            elif kind == "num" and row["order"] is None:
                number = float(text)
                row["order"] = int(number) if number.is_integer() else number
                position += 1
            elif kind == "ident" and text.upper() == "NAMEOF" and row["name"] is None:
                end = _nameof(tokens, position + 1, row)
                if end is None:
                    return None
                position = end
            else:
                return None
            kind, text = tokens[position]
            position += 1
            if text == ")":
                break
            if text != ",":
                return None
        if row["name"] is None:
            return None
        rows.append(row)
        if tokens[position] == ("op", ","):
            position += 1
        elif position != len(tokens) - 1:
            return None
    return rows or None


def _nameof(tokens: list[tuple[str, str]], position: int, row: dict[str, Any]) -> int | None:
    """Read ``( 'Table'[Name] )`` from ``position`` into ``row``; the index after it."""
    if tokens[position] != ("op", "("):
        return None
    position += 1
    kind, text = tokens[position]
    if kind == "table":
        row["table"] = text[1:-1].replace("''", "'")
        position += 1
    elif kind == "ident":
        row["table"] = text
        position += 1
    kind, text = tokens[position]
    if kind != "bracket" or tokens[position + 1] != ("op", ")"):
        return None
    row["name"] = text[1:-1].replace("]]", "]")
    return position + 2


def _formats(db: Any) -> tuple[dict[int, str], dict[tuple[int, int], str]]:
    """Dynamic format string expressions by definition id and by (owner type, owner id)."""
    by_id: dict[int, str] = {}
    by_owner: dict[tuple[int, int], str] = {}
    for row in _rows(db, "FormatStringDefinition", _FORMAT_FIELDS) or []:
        expression, key = _name(row["Expression"]), _code(row["ID"])
        if expression is None or key is None:
            continue
        by_id[key] = expression
        owner, kind = _code(row["ObjectID"]), _code(row["ObjectType"])
        if owner is not None and kind is not None:
            by_owner[(kind, owner)] = expression
    return by_id, by_owner


def _format_of(
    formats: tuple[dict[int, str], dict[tuple[int, int], str]],
    definition: object,
    owner: tuple[int, object],
) -> str | None:
    by_id, by_owner = formats
    key, owner_id = _code(definition), _code(owner[1])
    if key:
        return by_id.get(key)
    return by_owner.get((owner[0], owner_id)) if owner_id is not None else None


def metadata_facts(db: Any) -> dict[str, Any]:
    """The model facts read from a metadata database reader (``db.query(sql)``).

    ``{"version": 1}`` alone when not even the model's tables can be read. The schema of every
    section is in the docstring of ``read_model_facts``. Never raises.
    """
    try:
        return _metadata_facts(db)
    except Exception:  # noqa: BLE001 - facts are extra; an unreadable model says nothing
        return {"version": FACTS_VERSION}


def _metadata_facts(db: Any) -> dict[str, Any]:
    facts: dict[str, Any] = {"version": FACTS_VERSION}
    raw_tables = _rows(db, "Table", _TABLE_FIELDS, ("ID", "Name"))
    if raw_tables is None:
        return facts
    tables: dict[int, dict[str, Any]] = {}
    for row in raw_tables:
        key = _code(row["ID"])
        if key is not None and not _internal(row["Name"], row["SystemFlags"]):
            tables[key] = row
    table_names = {key: str(row["Name"]) for key, row in tables.items()}
    raw_columns = _rows(db, "Column", _COLUMN_FIELDS, ("ID", "TableID")) or []
    columns: dict[int, dict[str, Any]] = {}
    for row in raw_columns:
        key, table = _code(row["ID"]), _code(row["TableID"])
        name = _name(row["ExplicitName"]) or _name(row["InferredName"])
        kind = _code(row["Type"]) if row["Type"] is not None else _code(row["BindingType"])
        if key is None or table not in table_names or name is None or kind == _ROW_NUMBER_COLUMN:
            continue
        columns[key] = {
            **row,
            "_table_id": table,
            "_table": table_names[table],
            "_name": name,
            "_kind": kind,
        }
    partitions = _rows(db, "Partition", _PARTITION_FIELDS, ("ID", "TableID")) or []
    by_table_partitions: dict[int, list[dict[str, Any]]] = {}
    for row in partitions:
        table = _code(row["TableID"])
        if table in table_names:
            by_table_partitions.setdefault(table, []).append(row)
    groups = _rows(db, "CalculationGroup", _GROUP_FIELDS, ("ID", "TableID")) or []
    group_tables = {_code(row["TableID"]) for row in groups}
    parameter_columns: dict[int, object] = {}
    for row in _rows(db, "ExtendedProperty", _PROPERTY_FIELDS, ("ObjectID", "Name")) or []:
        owner = _code(row["ObjectID"])
        if (
            row["Name"] == _PARAMETER_PROPERTY
            and _code(row["ObjectType"]) == _COLUMN_OBJECT
            and owner in columns
        ):
            try:
                parameter_columns[owner] = json.loads(str(row["Value"]))
            except ValueError:
                parameter_columns[owner] = None
    formats = _formats(db)
    annotations = _annotations(db)
    context = _Facts(tables, table_names, columns, by_table_partitions, group_tables, annotations)
    _add(facts, "model", lambda: _model_settings(db, annotations.get((_MODEL_OBJECT, 1), {})))
    _add(facts, "tables", context.tables)
    _add(facts, "columns", context.columns)
    _add(facts, "measures", lambda: _measures(db, table_names, formats))
    _add(facts, "relationships", lambda: _relationships(db, tables, columns))
    _add(facts, "hierarchies", lambda: _hierarchies(db, table_names, columns))
    _add(facts, "variations", lambda: _variations(db, tables, columns))
    _add(facts, "roles", lambda: _roles(db, table_names))
    _add(facts, "calculationGroups", lambda: _calculation_groups(db, groups, table_names, formats))
    _add(facts, "fieldParameters", lambda: context.field_parameters(parameter_columns, facts))
    _add(facts, "whatIfParameters", lambda: context.what_if_parameters(parameter_columns))
    _add(facts, "partitions", context.partitions)
    _add(facts, "expressions", lambda: _expressions(db))
    _fit(facts)
    return facts


# The child's output is read up to 16 MB (imported_editing._EXTRACT_MAX_BYTES): the facts stay
# well under it, cutting the longest texts first (Power Query "Enter data" rows are the usual
# culprits) rather than failing the read, and naming every section that lost text.
_MAX_TOTAL = 12_000_000
_FIT_STEPS = (200_000, 20_000, 2_000, 0)
_TEXT_KEYS = ("expression", "filter", "formatStringExpression", "description")


def _cut_texts(node: object, limit: int) -> None:
    if isinstance(node, list):
        for item in node:
            _cut_texts(item, limit)
    elif isinstance(node, dict):
        for key in _TEXT_KEYS:
            value = node.get(key)
            if isinstance(value, str) and len(value) > limit:
                node[key] = value[:limit] if limit else None
                node["truncated"] = True
        for value in node.values():
            if isinstance(value, list):
                _cut_texts(value, limit)


def _was_cut(node: object) -> bool:
    if isinstance(node, list):
        return any(_was_cut(item) for item in node)
    if isinstance(node, dict):
        return node.get("truncated") is True or any(
            _was_cut(value) for value in node.values() if isinstance(value, list)
        )
    return False


def _fit(facts: dict[str, Any]) -> None:
    """Keep the facts under ``_MAX_TOTAL`` characters of JSON and list, in ``truncated``, the
    sections whose texts were cut (an item that lost text says ``"truncated": true``)."""
    for limit in _FIT_STEPS:
        if len(json.dumps(facts)) <= _MAX_TOTAL:
            break
        for value in facts.values():
            if isinstance(value, list):
                _cut_texts(value, limit)
    cut = sorted(key for key, value in facts.items() if isinstance(value, list) and _was_cut(value))
    if cut:
        facts["truncated"] = cut


def _annotations(db: Any) -> dict[tuple[int, int], dict[str, str]]:
    """The annotations the facts use, by (owner type, owner id): auto date/time and the type
    behind a date/time column. Other annotations (tools' own settings) are not read."""
    wanted = {_AUTO_DATE_TIME, _UNDERLYING_DATE_TIME, *_AUTO_DATE_TABLES}
    found: dict[tuple[int, int], dict[str, str]] = {}
    rows = _rows(db, "Annotation", _ANNOTATION_FIELDS, ("ObjectID", "ObjectType", "Name"))
    for row in rows or []:
        owner, kind = _code(row["ObjectID"]), _code(row["ObjectType"])
        if row["Name"] in wanted and owner is not None and kind is not None:
            found.setdefault((kind, owner), {})[str(row["Name"])] = str(row["Value"] or "")
    return found


class _Facts:
    """The sections built from the tables, columns and partitions read once."""

    def __init__(
        self,
        tables: dict[int, dict[str, Any]],
        names: dict[int, str],
        columns: dict[int, dict[str, Any]],
        partitions: dict[int, list[dict[str, Any]]],
        group_tables: set[int | None],
        annotations: dict[tuple[int, int], dict[str, str]] | None = None,
    ) -> None:
        self.raw_tables, self.names, self.raw_columns = tables, names, columns
        self.raw_partitions, self.group_tables = partitions, group_tables
        self.annotations = annotations or {}

    def _auto_date_table(self, key: int, row: dict[str, Any], calculated: bool) -> str | None:
        """ "local" for a date table auto date/time made for one date column, "template" for the
        template they are copied from, None for any other table."""
        notes = self.annotations.get((_TABLE_OBJECT, key), {})
        for annotation, kind in _AUTO_DATE_TABLES.items():
            if notes.get(annotation, "").casefold() == "true":
                return kind
        if calculated and _flag(row["ShowAsVariationsOnly"]):
            return "local"
        if calculated and _flag(row["IsPrivate"]):
            return "template"
        return None

    def _source(self, table: int) -> tuple[str | None, str | None]:
        """(the first partition's source kind, a calculated table's DAX)."""
        for row in self.raw_partitions.get(table, []):
            source = _PARTITION_SOURCES.get(_code(row["Type"]) or 0)
            dax = _name(row["QueryDefinition"]) if source == "calculated" else None
            return source, dax
        return None, None

    def tables(self) -> list[dict[str, Any]]:
        # "Mark as date table" sets the table's DataCategory to Time and its date column's key.
        keys: dict[int, str] = {}
        for column in self.raw_columns.values():
            if _flag(column["IsKey"]):
                keys.setdefault(column["_table_id"], column["_name"])
        found = []
        for key, row in self.raw_tables.items():
            source, dax = self._source(key)
            category = _name(row["DataCategory"])
            date_table = bool(category and category.casefold() == "time" and key in keys)
            found.append(
                _bounded(
                    {
                        "name": self.names[key],
                        "hidden": _flag(row["IsHidden"]),
                        "private": _flag(row["IsPrivate"]),
                        "showAsVariationsOnly": _flag(row["ShowAsVariationsOnly"]),
                        "autoDateTable": self._auto_date_table(key, row, source == "calculated"),
                        "description": _name(row["Description"]),
                        "dataCategory": category,
                        "isDateTable": date_table,
                        "dateColumn": keys.get(key) if date_table else None,
                        "source": source,
                        "calculated": source == "calculated",
                        "expression": dax,
                        "calculationGroup": key in self.group_tables
                        or bool(_code(row["CalculationGroupID"])),
                        "lineageTag": _name(row["LineageTag"]),
                    },
                    "expression",
                )
            )
        return found

    def columns(self) -> list[dict[str, Any]]:
        found = []
        for row in self.raw_columns.values():
            summarize = _code(row["SummarizeBy"])
            sort_by = self.raw_columns.get(_code(row["SortByColumnID"]) or 0)
            data_type = _data_type(row["ExplicitDataType"], row["InferredDataType"])
            kind = _COLUMN_KINDS.get(row["_kind"] or 0) or (
                "calculated" if _name(row["Expression"]) else "data"
            )
            notes = self.annotations.get((_COLUMN_OBJECT, _code(row["ID"]) or 0), {})
            underlying = notes.get(_UNDERLYING_DATE_TIME, "").casefold()
            found.append(
                _bounded(
                    {
                        "table": row["_table"],
                        "name": row["_name"],
                        "kind": kind,
                        "dataType": data_type,
                        "dataTypeName": _DATA_TYPE_NAMES.get(data_type or 0),
                        # Desktop's Date, Date/Time or Time type behind a date/time column.
                        "dateTimeType": _DATE_TIME_TYPES.get(underlying)
                        if data_type == 9
                        else None,
                        "state": _STATES.get(_code(row["State"]) or 1),
                        "expression": _name(row["Expression"]) if kind == "calculated" else None,
                        "formatString": _name(row["FormatString"]),
                        "sortByColumn": sort_by["_name"] if sort_by is not None else None,
                        "dataCategory": _name(row["DataCategory"]),
                        "summarizeBy": _SUMMARIZE_BY.get(summarize) if summarize else None,
                        "summarizeByCode": summarize,
                        "hidden": _flag(row["IsHidden"]),
                        "key": _flag(row["IsKey"]),
                        "displayFolder": _name(row["DisplayFolder"]),
                        "description": _name(row["Description"]),
                        "sourceColumn": _name(row["SourceColumn"]),
                        "lineageTag": _name(row["LineageTag"]),
                        "error": _name(row["ErrorMessage"]),
                    },
                    "expression",
                )
            )
        return found

    def partitions(self) -> list[dict[str, Any]]:
        found = []
        for table, rows in self.raw_partitions.items():
            for row in rows:
                source = _PARTITION_SOURCES.get(_code(row["Type"]) or 0)
                mode = _code(row["Mode"])
                found.append(
                    _bounded(
                        {
                            "table": self.names[table],
                            "name": _name(row["Name"]),
                            "source": source,
                            "mode": _MODES.get(mode, "unknown") if mode is not None else None,
                            "expression": _name(row["QueryDefinition"]),
                            "refreshedTime": _filetime(row["RefreshedTime"]),
                            "state": _STATES.get(_code(row["State"]) or 1),
                            "error": _name(row["ErrorMessage"]),
                        },
                        "expression",
                    )
                )
        return found

    def _parameter_tables(self, parameters: dict[int, object]) -> dict[str, list[dict[str, Any]]]:
        found: dict[str, list[dict[str, Any]]] = {}
        for column, value in parameters.items():
            row = self.raw_columns[column]
            found.setdefault(row["_table"], []).append({"row": row, "metadata": value})
        return found

    def _dax_of(self, table: str) -> str | None:
        key = next((k for k, n in self.names.items() if n == table), None)
        return self._source(key)[1] if key is not None else None

    def _is_field_kind(self, value: object) -> bool:
        return isinstance(value, dict) and value.get("kind") == _FIELD_PARAMETER_KIND

    def _field_tables(self, parameters: dict[int, object]) -> set[str]:
        """Tables whose DAX uses NAMEOF (as ModelInfo.field_parameter_tables decides) or whose
        column says it is a field parameter."""
        found = set()
        for table, entries in self._parameter_tables(parameters).items():
            if any(self._is_field_kind(e["metadata"]) for e in entries):
                found.add(table)
        for key, name in self.names.items():
            dax = self._source(key)[1]
            if dax and re.search(r"\bNAMEOF\s*\(", dax, re.IGNORECASE):
                found.add(name)
        return found

    def field_parameters(
        self, parameters: dict[int, object], facts: dict[str, Any]
    ) -> list[dict[str, Any]]:
        measures = {
            (str(m["table"]).casefold(), str(m["name"]).casefold())
            for m in facts.get("measures", [])
        }
        measure_names = [str(n) for _, n in measures]
        columns = {
            (c["_table"].casefold(), c["_name"].casefold()) for c in self.raw_columns.values()
        }
        found = []
        for table in sorted(self._field_tables(parameters)):
            dax = self._dax_of(table) or ""
            rows = field_parameter_rows(dax)
            for row in rows or []:
                name = str(row["name"]).casefold()
                owner = str(row["table"] or "").casefold()
                if (owner, name) in measures or (not owner and measure_names.count(name) == 1):
                    row["kind"] = "measure"
                elif (owner, name) in columns:
                    row["kind"] = "column"
                else:
                    row["kind"] = None
            own = [c for c in self.raw_columns.values() if c["_table"] == table]
            by_source = {str(c["SourceColumn"] or "").casefold(): c for c in own}
            # The fields column is the one whose ParameterMetadata says "field parameter";
            # Desktop's own layout (label, NAMEOF, order as [Value1..3]) is the fallback.
            fields = next(
                (c for c in own if self._is_field_kind(parameters.get(_code(c["ID"]) or 0))),
                by_source.get("[value2]"),
            )
            label = by_source.get("[value1]") or next(
                (c for c in own if c is not fields and not _flag(c["IsHidden"])), None
            )
            order = self.raw_columns.get(_code(label["SortByColumnID"]) or 0) if label else None
            order = order or by_source.get("[value3]")
            found.append(
                {
                    "table": table,
                    "labelColumn": label["_name"] if label else None,
                    "fieldsColumn": fields["_name"] if fields else None,
                    "orderColumn": order["_name"] if order else None,
                    "rows": rows,
                }
            )
        return found

    def what_if_parameters(self, parameters: dict[int, object]) -> list[dict[str, Any]]:
        fields = self._field_tables(parameters)
        found = []
        for table, entries in sorted(self._parameter_tables(parameters).items()):
            if table in fields:
                continue
            found.append(
                {
                    "table": table,
                    "column": entries[0]["row"]["_name"],
                    "expression": self._dax_of(table),
                }
            )
        return found


def _model_settings(db: Any, notes: dict[str, str]) -> dict[str, Any] | None:
    rows = _rows(db, "Model", _MODEL_FIELDS)
    if not rows:
        return None
    row = rows[0]
    mode = _code(row["DefaultMode"])
    implicit = row["DiscourageImplicitMeasures"]
    auto = notes.get(_AUTO_DATE_TIME)
    return {
        "culture": _name(row["Culture"]),
        "sourceQueryCulture": _name(row["SourceQueryCulture"]),
        "discourageImplicitMeasures": None if implicit is None else _flag(implicit),
        "defaultMode": _MODES.get(mode, "unknown") if mode is not None else None,
        # None: the model does not say (Desktop writes "0" or "1").
        "autoDateTime": None if auto is None else auto.strip() == "1",
    }


def _variations(
    db: Any, tables: dict[int, dict[str, Any]], columns: dict[int, dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """Auto date/time's hidden joins: each date column's variation (its local date table, the
    relationship to it and the default hierarchy)."""
    rows = _rows(db, "Variation", _VARIATION_FIELDS, ("ID", "ColumnID"))
    if rows is None:
        return None
    joins = {
        _code(r["ID"]): r
        for r in _rows(db, "Relationship", ("ID", "Name", "ToTableID", "ToEndTableID")) or []
    }
    hierarchies = {
        _code(h["ID"]): h for h in _rows(db, "Hierarchy", ("ID", "TableID", "Name")) or []
    }
    found = []
    for row in rows:
        column = columns.get(_code(row["ColumnID"]) or 0)
        if column is None:
            continue
        join = joins.get(_code(row["RelationshipID"]))
        target = None
        if join is not None:
            # Older schemas spell the end ToEndTableID.
            end = join["ToTableID"] if join["ToTableID"] is not None else join["ToEndTableID"]
            target = tables.get(_code(end) or 0)
        hierarchy = hierarchies.get(_code(row["DefaultHierarchyID"]))
        found.append(
            {
                "table": column["_table"],
                "column": column["_name"],
                "name": _name(row["Name"]),
                "relationship": _name(join["Name"]) if join is not None else None,
                "dateTable": str(target["Name"]) if target is not None else None,
                "defaultHierarchy": _name(hierarchy["Name"]) if hierarchy is not None else None,
                "isDefault": _flag(row["IsDefault"]),
            }
        )
    return found


def _measures(
    db: Any,
    tables: dict[int, str],
    formats: tuple[dict[int, str], dict[tuple[int, int], str]],
) -> list[dict[str, Any]] | None:
    rows = _rows(db, "Measure", _MEASURE_FIELDS, ("ID", "TableID", "Name"))
    if rows is None:
        return None
    found = []
    for row in rows:
        table = _code(row["TableID"])
        if table not in tables or not _name(row["Name"]):
            continue
        found.append(
            _bounded(
                {
                    "table": tables[table],
                    "name": row["Name"],
                    "expression": _name(row["Expression"]),
                    "formatString": _name(row["FormatString"]),
                    "formatStringExpression": _format_of(
                        formats, row["FormatStringDefinitionID"], (_MEASURE_OBJECT, row["ID"])
                    ),
                    "displayFolder": _name(row["DisplayFolder"]),
                    "description": _name(row["Description"]),
                    "hidden": _flag(row["IsHidden"]),
                    "dataType": _data_type(row["DataType"]),
                    "dataTypeName": _DATA_TYPE_NAMES.get(_data_type(row["DataType"]) or 0),
                    "lineageTag": _name(row["LineageTag"]),
                    "state": _STATES.get(_code(row["State"]) or 1),
                    "error": _name(row["ErrorMessage"]),
                },
                "expression",
                "formatStringExpression",
            )
        )
    return found


def _relationships(
    db: Any, tables: dict[int, dict[str, Any]], columns: dict[int, dict[str, Any]]
) -> list[dict[str, Any]] | None:
    rows = _rows(db, "Relationship", _RELATIONSHIP_FIELDS, ("ID",))
    if rows is None:
        return None
    found = []
    for row in rows:
        ends = []
        for side in ("From", "To"):
            # Older model schemas spell the endpoint columns FromEnd*/ToEnd*.
            prefix = side if row[f"{side}TableID"] is not None else f"{side}End"
            table, column = (
                _code(row[f"{prefix}TableID"]),
                columns.get(_code(row[f"{prefix}ColumnID"]) or 0),
            )
            if table not in tables or column is None:
                break
            many = _code(row[f"{prefix}Cardinality"]) == 2
            ends.append((str(tables[table]["Name"]), column["_name"], many))
        if len(ends) != 2:
            continue  # an engine table's join, or one whose ends the metadata lacks
        (from_table, from_column, from_many), (to_table, to_column, to_many) = ends
        cross = _code(row["CrossFilteringBehavior"])
        security = _code(row["SecurityFilteringBehavior"])
        join = _code(row["JoinOnDateBehavior"])
        found.append(
            {
                "name": _name(row["Name"]),
                "fromTable": from_table,
                "fromColumn": from_column,
                "toTable": to_table,
                "toColumn": to_column,
                "fromCardinality": "many" if from_many else "one",
                "toCardinality": "many" if to_many else "one",
                "cardinality": f"{'M' if from_many else '1'}:{'M' if to_many else '1'}",
                "crossFilter": _CROSS_FILTER.get(cross or 0, "single"),
                "active": _flag(row["IsActive"]),
                "relyOnReferentialIntegrity": _flag(row["RelyOnReferentialIntegrity"]),
                "securityFilter": _SECURITY_FILTER.get(security or 0),
                "joinOnDate": _JOIN_ON_DATE.get(join or 0),
            }
        )
    return found


def _hierarchies(
    db: Any, tables: dict[int, str], columns: dict[int, dict[str, Any]]
) -> list[dict[str, Any]] | None:
    rows = _rows(db, "Hierarchy", _HIERARCHY_FIELDS, ("ID", "TableID", "Name"))
    if rows is None:
        return None
    levels: dict[int, list[dict[str, Any]]] = {}
    for row in _rows(db, "Level", _LEVEL_FIELDS, ("ID", "HierarchyID")) or []:
        owner = _code(row["HierarchyID"])
        column = columns.get(_code(row["ColumnID"]) or 0)
        # Older schemas keep the position in Index.
        ordinal = _code(row["Ordinal"]) if row["Ordinal"] is not None else _code(row["Index"])
        if owner is None:
            continue
        levels.setdefault(owner, []).append(
            {
                "name": _name(row["Name"]),
                "column": column["_name"] if column is not None else None,
                "ordinal": ordinal,
            }
        )
    found = []
    for row in rows:
        table = _code(row["TableID"])
        if table not in tables:
            continue
        ordered = sorted(
            levels.get(_code(row["ID"]) or 0, []),
            key=lambda level: (level["ordinal"] is None, level["ordinal"] or 0),
        )
        found.append(
            {
                "table": tables[table],
                "name": row["Name"],
                "hidden": _flag(row["IsHidden"]),
                "displayFolder": _name(row["DisplayFolder"]),
                "description": _name(row["Description"]),
                "levels": ordered,
            }
        )
    return found


def _roles(db: Any, tables: dict[int, str]) -> list[dict[str, Any]] | None:
    rows = _rows(db, "Role", _ROLE_FIELDS, ("ID", "Name"))
    if rows is None:
        return None
    filters: dict[int, list[dict[str, Any]]] = {}
    for row in _rows(db, "TablePermission", _TABLE_PERMISSION_FIELDS, ("RoleID", "TableID")) or []:
        role, table = _code(row["RoleID"]), _code(row["TableID"])
        if role is not None and table in tables:
            filters.setdefault(role, []).append(
                _bounded(
                    {"table": tables[table], "filter": _name(row["FilterExpression"])}, "filter"
                )
            )
    found = []
    for row in rows:
        permission = _code(row["ModelPermission"])
        found.append(
            {
                "name": row["Name"],
                "description": _name(row["Description"]),
                "permission": _PERMISSIONS.get(permission or 0),
                "tables": filters.get(_code(row["ID"]) or 0, []),
            }
        )
    return found


def _calculation_groups(
    db: Any,
    groups: list[dict[str, Any]],
    tables: dict[int, str],
    formats: tuple[dict[int, str], dict[tuple[int, int], str]],
) -> list[dict[str, Any]] | None:
    if not groups:
        return [] if _rows(db, "CalculationGroup", _GROUP_FIELDS) is not None else None
    items: dict[int, list[dict[str, Any]]] = {}
    for row in _rows(db, "CalculationItem", _ITEM_FIELDS, ("ID", "CalculationGroupID")) or []:
        owner = _code(row["CalculationGroupID"])
        if owner is None:
            continue
        items.setdefault(owner, []).append(
            _bounded(
                {
                    "name": _name(row["Name"]),
                    "expression": _name(row["Expression"]),
                    "ordinal": _code(row["Ordinal"]),
                    "formatStringExpression": _format_of(
                        formats,
                        row["FormatStringDefinitionID"],
                        (_CALCULATION_ITEM_OBJECT, row["ID"]),
                    ),
                    "description": _name(row["Description"]),
                },
                "expression",
                "formatStringExpression",
            )
        )
    selections: dict[int, list[dict[str, Any]]] = {}
    rows = _rows(db, "CalculationExpression", _SELECTION_FIELDS, ("ID", "CalculationGroupID"))
    for row in rows or []:
        owner = _code(row["CalculationGroupID"])
        if owner is not None and _name(row["Expression"]):
            selections.setdefault(owner, []).append(
                _bounded(
                    {
                        "expression": row["Expression"],
                        "selectionMode": _SELECTION_MODES.get(_code(row["SelectionMode"]) or 0),
                    },
                    "expression",
                )
            )
    found = []
    for row in groups:
        table, key = _code(row["TableID"]), _code(row["ID"]) or 0
        if table not in tables:
            continue
        ordered = sorted(
            items.get(key, []), key=lambda i: (i["ordinal"] is None, i["ordinal"] or 0)
        )
        found.append(
            {
                "table": tables[table],
                "precedence": _code(row["Precedence"]),
                "description": _name(row["Description"]),
                "items": ordered,
                "selectionExpressions": selections.get(key, []),
            }
        )
    return found


def _expressions(db: Any) -> list[dict[str, Any]] | None:
    rows = _rows(db, "Expression", _EXPRESSION_FIELDS, ("ID", "Name"))
    if rows is None:
        return None
    found = []
    for row in rows:
        text = _name(row["Expression"])
        kind = _code(row["Kind"])
        found.append(
            _bounded(
                {
                    "name": row["Name"],
                    "kind": _EXPRESSION_KINDS.get(kind) if kind is not None else "m",
                    "expression": text,
                    "description": _name(row["Description"]),
                    "parameter": bool(text and _PARAMETER_QUERY.search(text)),
                },
                "expression",
            )
        )
    return found


def read_model_facts(source: Path) -> dict[str, Any]:
    """Everything the model in ``source`` says about itself, without decoding a table row.

    Reads the embedded metadata only (never connections or credentials; role members are not
    read). A file PBIXRay cannot open raises, like ``types``. The result, camelCase keys, every
    section optional (absent: the metadata could not give it), every text at most 1,000,000
    characters and the whole at most 12,000,000 characters of JSON (an item whose text was cut
    carries ``"truncated": true``, and ``truncated`` at the top lists the sections that lost
    text)::

        version            1
        truncated          [section names] (only when a text was cut)
        model              {culture, sourceQueryCulture, discourageImplicitMeasures (bool|None),
                            defaultMode ("import"|"directQuery"|"dual"|...|None),
                            autoDateTime (bool|None)}
        tables             [{name, hidden, private, showAsVariationsOnly, autoDateTable
                             ("local"|"template"|None), description, dataCategory,
                             isDateTable, dateColumn, source ("m"|"calculated"|
                             "calculationGroup"|"entity"|"query"|...|None), calculated,
                             expression (a calculated table's DAX), calculationGroup,
                             lineageTag}]
        columns            [{table, name, kind ("data"|"calculated"|"calculatedTableColumn"),
                             dataType (model DataType id, as --types), dataTypeName,
                             dateTimeType ("date"|"dateTime"|"time"|None), state (None when
                             ready, else "semanticError", "calculationNeeded", ...),
                             expression (a calculated column's DAX), formatString, sortByColumn,
                             dataCategory, summarizeBy, summarizeByCode, hidden, key,
                             displayFolder, description, sourceColumn, lineageTag, error}]
        measures           [{table, name, expression, formatString, formatStringExpression,
                             displayFolder, description, hidden, dataType, dataTypeName,
                             lineageTag, state, error}]
        relationships      [{name, fromTable, fromColumn, toTable, toColumn, fromCardinality,
                             toCardinality ("one"|"many"), cardinality ("M:1"|"1:M"|"1:1"|
                             "M:M", From:To), crossFilter ("single"|"both"|"automatic"), active,
                             relyOnReferentialIntegrity, securityFilter, joinOnDate
                             ("dateAndTime"|"datePartOnly"|None)}]
        hierarchies        [{table, name, hidden, displayFolder, description,
                             levels: [{name, column, ordinal}] in ordinal order}]
        variations         [{table, column, name, relationship, dateTable, defaultHierarchy,
                             isDefault}]
        roles              [{name, description, permission, tables: [{table, filter}]}]
        calculationGroups  [{table, precedence, description, items: [{name, expression,
                             ordinal, formatStringExpression, description}],
                             selectionExpressions: [{expression, selectionMode
                             ("multipleOrEmptySelection"|"noSelection"|None)}]}]
        fieldParameters    [{table, labelColumn, fieldsColumn, orderColumn, rows: [{label,
                             table, name, order, kind ("measure"|"column"|None)}] | None}]
        whatIfParameters   [{table, column, expression}]
        partitions         [{table, name, source, mode, expression (Power Query M or DAX),
                             refreshedTime (ISO UTC), state, error}]
        expressions        [{name, kind ("m"|None), expression, description, parameter}]
    """
    import tempfile

    with (
        tempfile.TemporaryDirectory() as tmp,
        PBIXRay(str(source), on_disk=True, temp_dir=tmp) as model,
    ):
        db = getattr(getattr(model._metadata, "source", None), "_db", None)
        return metadata_facts(db) if db is not None else {"version": FACTS_VERSION}


def extract(source: Path, destination: Path) -> None:
    with PBIXRay(str(source), on_disk=True, temp_dir=str(destination)) as model:
        tables = list(model.tables)
        if len(tables) > 200:
            raise ValueError("This model exceeds the 200-table import limit")
        exported = []
        for index, name in enumerate(tables):
            path = destination / f"table-{index}.parquet"
            writer = None
            rows = 0
            try:
                for frame in model.iter_table(name, chunk_size=50000, strings_as_categorical=False):
                    if len(frame.columns) > 500:
                        raise ValueError("A model table exceeds the 500-column import limit")
                    rows += len(frame)
                    if rows > 50_000_000:
                        raise ValueError("A model table exceeds the 50-million-row import limit")
                    table = pa.Table.from_pandas(frame, preserve_index=False)
                    if writer is None:
                        writer = pq.ParquetWriter(path, table.schema)
                    writer.write_table(table.cast(writer.schema))
            finally:
                if writer is not None:
                    writer.close()
            if writer is not None:
                exported.append({"name": str(name), "file": path.name, "rows": rows})
        # Never export connection credentials (the DataSource table is not read). Power Query
        # text is only in the facts, for the model editor; it is not chat context.
        found = _metadata_relationships(model)
        metadata = {
            "tables": exported,
            "measures": json.loads(model.dax_measures.to_json(orient="records")),
            "relationships": found
            if found is not None
            else json.loads(model.relationships.to_json(orient="records")),
            # PBIXRay's fallback view omits calculated-table ends (a CALENDAR Date join).
            "relationships_source": "metadata" if found is not None else "pbixray",
            "schema": json.loads(model.schema.to_json(orient="records")),
        }
        # The model facts of the file the rows come from, read from the metadata already open
        # (Studio keeps them with the snapshot: they say which calculated columns, names and
        # refreshes the rows reflect). Left out when the metadata cannot give them.
        db = getattr(getattr(model._metadata, "source", None), "_db", None)
        if db is not None:
            # metadata_facts never raises: a section it cannot read is left out.
            metadata["facts"] = metadata_facts(db)
        (destination / "model.json").write_text(json.dumps(metadata), encoding="utf-8")


def formats(source: Path) -> list[dict[str, Any]]:
    """Read only display metadata; never decode fact rows or export connections."""
    import tempfile

    with (
        tempfile.TemporaryDirectory() as tmp,
        PBIXRay(str(source), on_disk=True, temp_dir=tmp) as model,
    ):
        # PBIXRay's public measure view omits FormatString. Keep this compatibility
        # adapter isolated, with fixed SQL over the embedded, read-only metadata.
        db = getattr(model._metadata.source, "_db", None)
        if db is None:
            return []
        frame = db.query(
            "SELECT t.Name AS TableName, m.Name, m.FormatString "
            "FROM Measure m JOIN [Table] t ON m.TableID=t.ID"
        )
        return cast(list[dict[str, Any]], json.loads(frame.to_json(orient="records")))


def measures(source: Path) -> list[dict[str, Any]]:
    """The model's measures (no table data): after a measure edit Studio re-reads these
    from the saved revision instead of decoding every table again."""
    import tempfile

    with (
        tempfile.TemporaryDirectory() as tmp,
        PBIXRay(str(source), on_disk=True, temp_dir=tmp) as model,
    ):
        return cast(list[dict[str, Any]], json.loads(model.dax_measures.to_json(orient="records")))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[2] == "--formats":
        print(json.dumps(formats(Path(sys.argv[1]))))
    elif len(sys.argv) == 3 and sys.argv[2] == "--measures":
        print(json.dumps(measures(Path(sys.argv[1]))))
    elif len(sys.argv) == 3 and sys.argv[2] == "--types":
        print(json.dumps(types(Path(sys.argv[1]))))
    elif len(sys.argv) == 3 and sys.argv[2] == "--relationships":
        print(json.dumps(read_relationships(Path(sys.argv[1]))))
    elif len(sys.argv) == 3 and sys.argv[2] == "--facts":
        print(json.dumps(read_model_facts(Path(sys.argv[1]))))
    else:
        extract(Path(sys.argv[1]), Path(sys.argv[2]))
