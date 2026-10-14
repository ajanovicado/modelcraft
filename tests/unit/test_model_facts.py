"""The model's own facts: read from the metadata database (``pbix_extract.read_model_facts``)
and kept by Studio (``model_facts``). Synthetic metadata databases pin every section and the
schema variants of older models; the real Agents Performance report pins the real shapes."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from ai_powerbi_analyst.studio import model_facts, pbir_expr, pbix_extract
from ai_powerbi_analyst.studio.pbix_extract import field_parameter_rows, metadata_facts

JAN_2024 = 133485408000000000  # 2024-01-01T00:00:00Z as a FILETIME
FEB_2024 = JAN_2024 + 31 * 86400 * 10**7
M_SALES = 'let Source = Csv.Document(File.Contents("C:\\data\\sales.csv")) in Source'
PICKER = (
    "{\n    (\"Total Sales\", NAMEOF('# Measures'[Total Sales]), 0),\n"
    '    ("Amount", NAMEOF(Sales[Amount]), 1)\n}'
)

SCHEMA: dict[str, list[str]] = {
    "Model": ["ID", "Name", "Culture", "DiscourageImplicitMeasures", "DefaultMode",
              "SourceQueryCulture"],
    "Table": ["ID", "ModelID", "Name", "Description", "DataCategory", "IsHidden", "IsPrivate",
              "ShowAsVariationsOnly", "SystemFlags", "CalculationGroupID", "LineageTag"],
    "Column": ["ID", "TableID", "ExplicitName", "InferredName", "ExplicitDataType",
               "InferredDataType", "DataCategory", "Description", "IsHidden", "IsKey",
               "SummarizeBy", "Type", "SourceColumn", "Expression", "FormatString",
               "SortByColumnID", "DisplayFolder", "LineageTag", "State", "ErrorMessage"],
    "Measure": ["ID", "TableID", "Name", "Description", "DataType", "Expression",
                "FormatString", "IsHidden", "DisplayFolder", "LineageTag",
                "FormatStringDefinitionID", "State", "ErrorMessage"],
    "FormatStringDefinition": ["ID", "ObjectID", "ObjectType", "Expression"],
    "Relationship": ["ID", "ModelID", "Name", "IsActive", "Type", "CrossFilteringBehavior",
                     "JoinOnDateBehavior", "RelyOnReferentialIntegrity", "FromTableID",
                     "FromColumnID", "FromCardinality", "ToTableID", "ToColumnID",
                     "ToCardinality", "SecurityFilteringBehavior"],
    "Hierarchy": ["ID", "TableID", "Name", "Description", "IsHidden", "DisplayFolder"],
    "Level": ["ID", "HierarchyID", "Ordinal", "Name", "ColumnID"],
    "Role": ["ID", "ModelID", "Name", "Description", "ModelPermission"],
    "TablePermission": ["ID", "RoleID", "TableID", "FilterExpression"],
    "RoleMembership": ["ID", "RoleID", "MemberName"],
    "CalculationGroup": ["ID", "TableID", "Description", "Precedence"],
    "CalculationItem": ["ID", "CalculationGroupID", "FormatStringDefinitionID", "Name",
                        "Description", "Expression", "Ordinal"],
    "CalculationExpression": ["ID", "CalculationGroupID", "Expression", "SelectionMode"],
    "Partition": ["ID", "TableID", "Name", "QueryDefinition", "Type", "Mode", "RefreshedTime",
                  "State", "ErrorMessage"],
    "Expression": ["ID", "ModelID", "Name", "Description", "Kind", "Expression"],
    "ExtendedProperty": ["ID", "ObjectID", "ObjectType", "Name", "Type", "Value"],
    "Annotation": ["ID", "ObjectID", "ObjectType", "Name", "Value"],
    "Variation": ["ID", "ColumnID", "Name", "Description", "RelationshipID",
                  "DefaultHierarchyID", "DefaultColumnID", "IsDefault"],
}  # fmt: skip

ROWS: dict[str, list[dict[str, Any]]] = {
    "Model": [
        {"ID": 1, "Name": "Model", "Culture": "en-US", "DiscourageImplicitMeasures": 1,
         "DefaultMode": 0, "SourceQueryCulture": "en-GB"},
    ],
    "Table": [
        {"ID": 1, "Name": "Sales", "Description": "Every sale", "IsHidden": 0, "SystemFlags": 0,
         "CalculationGroupID": 0, "LineageTag": "t-sales"},
        {"ID": 2, "Name": "Date", "DataCategory": "Time", "SystemFlags": 2, "LineageTag": "t-date"},
        {"ID": 3, "Name": "# Measures", "SystemFlags": 0},
        {"ID": 4, "Name": "Time Intelligence", "SystemFlags": 0, "CalculationGroupID": 40},
        {"ID": 5, "Name": "Metric Picker", "SystemFlags": 2},
        {"ID": 6, "Name": "Discount", "SystemFlags": 2},
        {"ID": 7, "Name": "H$Sales (1)$Amount (11)", "SystemFlags": 1, "IsHidden": 1},
        # Auto date/time: a local date table is shown only as its date column's variation;
        # the template it is copied from is private.
        {"ID": 8, "Name": "LocalDateTable_1", "IsHidden": 1, "IsPrivate": 0,
         "ShowAsVariationsOnly": 1, "SystemFlags": 2},
        {"ID": 9, "Name": "DateTableTemplate_1", "IsHidden": 1, "IsPrivate": 1,
         "ShowAsVariationsOnly": 0, "SystemFlags": 2},
    ],
    "Column": [
        {"ID": 10, "TableID": 1, "ExplicitName": "RowNumber-2662979B", "ExplicitDataType": 6,
         "InferredDataType": 19, "IsHidden": 1, "IsKey": 1, "SummarizeBy": 1, "Type": 3},
        {"ID": 11, "TableID": 1, "ExplicitName": "Amount", "ExplicitDataType": 8,
         "InferredDataType": 19, "SummarizeBy": 3, "Type": 1, "SourceColumn": "Amount",
         "FormatString": "#,0.00", "DisplayFolder": "Money", "Description": "Sale amount",
         "LineageTag": "c-amount"},
        {"ID": 12, "TableID": 1, "ExplicitName": "Region", "ExplicitDataType": 2,
         "InferredDataType": 19, "SummarizeBy": 2, "Type": 1, "SourceColumn": "Region",
         "DataCategory": "Country"},
        {"ID": 13, "TableID": 1, "ExplicitName": "DateKey", "ExplicitDataType": 9,
         "InferredDataType": 19, "SummarizeBy": 2, "Type": 1, "SourceColumn": "DateKey"},
        {"ID": 14, "TableID": 1, "ExplicitName": "Margin %", "ExplicitDataType": 1,
         "InferredDataType": 8, "SummarizeBy": 2, "Type": 2,
         "Expression": "[Amount] * 0.1", "State": 1},
        {"ID": 15, "TableID": 1, "ExplicitName": "Month", "ExplicitDataType": 2,
         "InferredDataType": 19, "Type": 1, "SourceColumn": "Month", "SortByColumnID": 16},
        {"ID": 16, "TableID": 1, "ExplicitName": "Month Number", "ExplicitDataType": 6,
         "InferredDataType": 19, "IsHidden": 1, "Type": 1, "SourceColumn": "MonthNumber"},
        {"ID": 17, "TableID": 1, "ExplicitName": "Broken", "ExplicitDataType": 1,
         "InferredDataType": 19, "Type": 2, "Expression": "[Nope]", "State": 5,
         "ErrorMessage": "The column 'Nope' cannot be found."},
        {"ID": 20, "TableID": 2, "InferredName": "Date", "ExplicitDataType": 1,
         "InferredDataType": 9, "IsKey": 1, "Type": 4, "SourceColumn": "[Date]",
         "FormatString": "m/d/yyyy"},
        {"ID": 21, "TableID": 2, "InferredName": "Year", "ExplicitDataType": 1,
         "InferredDataType": 6, "Type": 4, "SourceColumn": "[Year]"},
        {"ID": 40, "TableID": 4, "ExplicitName": "Name", "ExplicitDataType": 2, "Type": 1,
         "SourceColumn": "Name"},
        {"ID": 50, "TableID": 5, "ExplicitName": "Metric Picker", "InferredName": "Value1",
         "ExplicitDataType": 1, "InferredDataType": 2, "Type": 4, "SourceColumn": "[Value1]"},
        {"ID": 51, "TableID": 5, "ExplicitName": "Metric Picker Fields", "InferredName": "Value2",
         "ExplicitDataType": 1, "InferredDataType": 2, "IsHidden": 1, "Type": 4,
         "SourceColumn": "[Value2]"},
        {"ID": 52, "TableID": 5, "ExplicitName": "Metric Picker Order", "InferredName": "Value3",
         "ExplicitDataType": 1, "InferredDataType": 6, "IsHidden": 1, "Type": 4,
         "SourceColumn": "[Value3]"},
        {"ID": 60, "TableID": 6, "ExplicitName": "Discount", "InferredName": "Value",
         "ExplicitDataType": 1, "InferredDataType": 6, "Type": 4, "SourceColumn": "[Value]"},
        {"ID": 70, "TableID": 7, "ExplicitName": "Amount", "ExplicitDataType": 8, "Type": 1},
        {"ID": 80, "TableID": 8, "InferredName": "Date", "ExplicitDataType": 1,
         "InferredDataType": 9, "IsHidden": 1, "IsKey": 1, "Type": 4, "SourceColumn": "[Date]"},
        {"ID": 90, "TableID": 9, "InferredName": "Date", "ExplicitDataType": 1,
         "InferredDataType": 9, "IsHidden": 1, "Type": 4, "SourceColumn": "[Date]"},
    ],
    "Measure": [
        {"ID": 1, "TableID": 3, "Name": "Total Sales", "Description": "All sales",
         "DataType": 8, "Expression": "SUM(Sales[Amount])", "FormatString": "#,0",
         "IsHidden": 0, "DisplayFolder": "Sales", "LineageTag": "m-total",
         "FormatStringDefinitionID": 0},
        {"ID": 2, "TableID": 3, "Name": "Dynamic", "DataType": 20, "Expression": "[Total Sales]",
         "FormatStringDefinitionID": 90},
        {"ID": 3, "TableID": 1, "Name": "One", "DataType": 6, "Expression": "1", "IsHidden": 1},
        {"ID": 4, "TableID": 7, "Name": "Engine Measure", "DataType": 8, "Expression": "0"},
    ],
    "FormatStringDefinition": [
        {"ID": 90, "ObjectID": 2, "ObjectType": 8, "Expression": '"#,0" & " units"'},
        {"ID": 91, "ObjectID": 101, "ObjectType": 47, "Expression": '"0.0%"'},
    ],
    "Relationship": [
        {"ID": 1, "Name": "r-date", "IsActive": 1, "Type": 1, "CrossFilteringBehavior": 1,
         "JoinOnDateBehavior": 1, "RelyOnReferentialIntegrity": 1, "FromTableID": 1,
         "FromColumnID": 13, "FromCardinality": 2, "ToTableID": 2, "ToColumnID": 20,
         "ToCardinality": 1, "SecurityFilteringBehavior": 1},
        {"ID": 2, "Name": "r-local", "IsActive": 0, "Type": 1, "CrossFilteringBehavior": 3,
         "JoinOnDateBehavior": 2, "RelyOnReferentialIntegrity": 0, "FromTableID": 1,
         "FromColumnID": 13, "FromCardinality": 2, "ToTableID": 8, "ToColumnID": 80,
         "ToCardinality": 1, "SecurityFilteringBehavior": 3},
        {"ID": 3, "Name": "r-both", "IsActive": 1, "Type": 1, "CrossFilteringBehavior": 2,
         "JoinOnDateBehavior": 1, "RelyOnReferentialIntegrity": 0, "FromTableID": 1,
         "FromColumnID": 16, "FromCardinality": 1, "ToTableID": 2, "ToColumnID": 21,
         "ToCardinality": 1, "SecurityFilteringBehavior": 2},
        {"ID": 4, "Name": "r-engine", "IsActive": 1, "Type": 1, "CrossFilteringBehavior": 1,
         "FromTableID": 7, "FromColumnID": 70, "FromCardinality": 2, "ToTableID": 1,
         "ToColumnID": 11, "ToCardinality": 1},
    ],
    "Hierarchy": [
        {"ID": 1, "TableID": 2, "Name": "Calendar", "IsHidden": 0, "DisplayFolder": "Dates"},
        {"ID": 2, "TableID": 8, "Name": "Date Hierarchy", "IsHidden": 0},
    ],
    "Level": [
        {"ID": 2, "HierarchyID": 1, "Ordinal": 1, "Name": "Day", "ColumnID": 20},
        {"ID": 1, "HierarchyID": 1, "Ordinal": 0, "Name": "Year", "ColumnID": 21},
    ],
    "Role": [{"ID": 1, "Name": "Europe", "Description": "Only EU", "ModelPermission": 2}],
    "TablePermission": [
        {"ID": 1, "RoleID": 1, "TableID": 1, "FilterExpression": '[Region] = "EU"'},
    ],
    "RoleMembership": [{"ID": 1, "RoleID": 1, "MemberName": "someone@example.com"}],
    "CalculationGroup": [{"ID": 40, "TableID": 4, "Description": "Time", "Precedence": 10}],
    "CalculationItem": [
        {"ID": 101, "CalculationGroupID": 40, "Name": "YTD", "Ordinal": 1,
         "Expression": "CALCULATE(SELECTEDMEASURE(), DATESYTD('Date'[Date]))",
         "FormatStringDefinitionID": 91},
        {"ID": 100, "CalculationGroupID": 40, "Name": "Current", "Ordinal": 0,
         "Expression": "SELECTEDMEASURE()", "Description": "as is"},
    ],
    "CalculationExpression": [],
    "Partition": [
        {"ID": 1, "TableID": 1, "Name": "Sales-p", "QueryDefinition": M_SALES, "Type": 4,
         "Mode": 0, "RefreshedTime": JAN_2024},
        {"ID": 2, "TableID": 2, "Name": "Date", "Type": 2, "Mode": 0, "RefreshedTime": JAN_2024,
         "QueryDefinition": "CALENDAR(DATE(2020, 1, 1), DATE(2020, 12, 31))"},
        {"ID": 3, "TableID": 3, "Name": "# Measures", "Type": 4, "Mode": 0,
         "QueryDefinition": "let Source = 1 in Source", "RefreshedTime": 0},
        {"ID": 4, "TableID": 4, "Name": "Time Intelligence", "Type": 7, "Mode": 0},
        {"ID": 5, "TableID": 5, "Name": "Metric Picker", "Type": 2, "Mode": 0,
         "QueryDefinition": PICKER},
        {"ID": 6, "TableID": 6, "Name": "Discount", "Type": 2, "Mode": 0,
         "QueryDefinition": "GENERATESERIES(0, 10, 1)"},
        {"ID": 7, "TableID": 7, "Name": "H$", "Type": 3, "Mode": 0},
        {"ID": 8, "TableID": 8, "Name": "LocalDateTable_1", "Type": 2, "Mode": 0,
         "QueryDefinition": "Calendar(Date(2020, 1, 1), Date(2020, 12, 31))"},
        {"ID": 9, "TableID": 9, "Name": "DateTableTemplate_1", "Type": 2, "Mode": 0,
         "QueryDefinition": "Calendar(Date(2015, 1, 1), Date(2015, 1, 1))"},
    ],
    "Expression": [
        {"ID": 1, "Name": "Folder", "Description": "Where the files are", "Kind": 0,
         "Expression": '"C:\\data" meta [IsParameterQuery=true, Type="Text"]'},
        {"ID": 2, "Name": "Helper", "Kind": 0, "Expression": "let x = 1 in x"},
    ],
    "ExtendedProperty": [
        {"ID": 1, "ObjectID": 51, "ObjectType": 4, "Name": "ParameterMetadata", "Type": 1,
         "Value": '{"version":3,"kind":2}'},
        {"ID": 2, "ObjectID": 60, "ObjectType": 4, "Name": "ParameterMetadata", "Type": 1,
         "Value": '{"version":0}'},
        {"ID": 3, "ObjectID": 1, "ObjectType": 1, "Name": "SomeTool", "Type": 1, "Value": "{}"},
    ],
    "Annotation": [
        {"ID": 1, "ObjectID": 1, "ObjectType": 1, "Name": "__PBI_TimeIntelligenceEnabled",
         "Value": "1"},
        {"ID": 2, "ObjectID": 8, "ObjectType": 3, "Name": "__PBI_LocalDateTable", "Value": "true"},
        {"ID": 3, "ObjectID": 9, "ObjectType": 3, "Name": "__PBI_TemplateDateTable",
         "Value": "true"},
        {"ID": 4, "ObjectID": 13, "ObjectType": 4, "Name": "UnderlyingDateTimeDataType",
         "Value": "Date"},
        {"ID": 5, "ObjectID": 1, "ObjectType": 1, "Name": "PBI_QueryOrder", "Value": '["Sales"]'},
    ],
    "Variation": [
        {"ID": 1, "ColumnID": 13, "Name": "Variation", "RelationshipID": 2,
         "DefaultHierarchyID": 2, "IsDefault": 1},
    ],
}  # fmt: skip


def metadata_db(
    *,
    drop_tables: tuple[str, ...] = (),
    drop_columns: dict[str, tuple[str, ...]] | None = None,
    rename_columns: dict[str, dict[str, str]] | None = None,
    extra_rows: dict[str, list[dict[str, Any]]] | None = None,
) -> sqlite3.Connection:
    """The synthetic model's metadata database; the keyword arguments shape older schemas."""
    conn = sqlite3.connect(":memory:")
    for table, columns in SCHEMA.items():
        if table in drop_tables:
            continue
        renames = (rename_columns or {}).get(table, {})
        kept = [c for c in columns if c not in (drop_columns or {}).get(table, ())]
        conn.execute(
            f"CREATE TABLE [{table}] ("  # noqa: S608 - test fixture identifiers
            + ", ".join(f"[{renames.get(c, c)}]" for c in kept)
            + ")"
        )
        for row in ROWS.get(table, []) + (extra_rows or {}).get(table, []):
            values = {renames.get(k, k): v for k, v in row.items() if k in kept}
            if values:
                conn.execute(
                    f"INSERT INTO [{table}] ({', '.join(f'[{k}]' for k in values)}) "  # noqa: S608
                    f"VALUES ({', '.join('?' for _ in values)})",
                    tuple(values.values()),
                )
    return conn


class Reader:
    """Raises on SQL errors, unlike PBIXRay's reader (which answers with an empty frame)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.seen: list[str] = []

    def query(self, sql: str) -> pd.DataFrame:
        self.seen.append(sql)
        return pd.read_sql_query(sql, self.conn)


@pytest.fixture
def facts() -> dict[str, Any]:
    return metadata_facts(Reader(metadata_db()))


def by(items: list[dict[str, Any]], *keys: str) -> dict[Any, dict[str, Any]]:
    return {
        tuple(item[k] for k in keys) if len(keys) > 1 else item[keys[0]]: item for item in items
    }


# --- every section, exactly -----------------------------------------------------------------


def test_the_sections_of_a_full_model(facts: dict[str, Any]) -> None:
    assert list(facts) == [
        "version",
        "model",
        "tables",
        "columns",
        "measures",
        "relationships",
        "hierarchies",
        "variations",
        "roles",
        "calculationGroups",
        "fieldParameters",
        "whatIfParameters",
        "partitions",
        "expressions",
    ]
    assert facts["version"] == 1
    assert facts["model"] == {
        "culture": "en-US",
        "sourceQueryCulture": "en-GB",
        "discourageImplicitMeasures": True,
        "defaultMode": "import",
        "autoDateTime": True,  # the model annotation __PBI_TimeIntelligenceEnabled = "1"
    }
    assert "truncated" not in facts  # nothing was cut
    # Role members are identities and never read.
    assert "someone@example.com" not in json.dumps(facts)


def test_tables(facts: dict[str, Any]) -> None:
    tables = by(facts["tables"], "name")
    # Engine storage (SystemFlags bit 1, H$ names) is left out; calculated tables are kept.
    assert list(tables) == [
        "Sales",
        "Date",
        "# Measures",
        "Time Intelligence",
        "Metric Picker",
        "Discount",
        "LocalDateTable_1",
        "DateTableTemplate_1",
    ]
    assert tables["Sales"] == {
        "name": "Sales",
        "hidden": False,
        "private": False,
        "showAsVariationsOnly": False,
        "autoDateTable": None,
        "description": "Every sale",
        "dataCategory": None,
        "isDateTable": False,
        "dateColumn": None,
        "source": "m",
        "calculated": False,
        "expression": None,
        "calculationGroup": False,
        "lineageTag": "t-sales",
    }
    date = tables["Date"]
    assert (date["isDateTable"], date["dateColumn"], date["source"], date["calculated"]) == (
        True,
        "Date",
        "calculated",
        True,
    )
    assert date["expression"] == "CALENDAR(DATE(2020, 1, 1), DATE(2020, 12, 31))"
    group = tables["Time Intelligence"]
    assert (group["calculationGroup"], group["source"], group["calculated"]) == (
        True,
        "calculationGroup",
        False,
    )
    # Auto date/time's tables: a local date table is shown only as a variation, its template
    # is private; the annotations say which is which.
    local, template = tables["LocalDateTable_1"], tables["DateTableTemplate_1"]
    assert (local["autoDateTable"], local["showAsVariationsOnly"], local["private"]) == (
        "local",
        True,
        False,
    )
    assert (template["autoDateTable"], template["private"]) == ("template", True)
    assert tables["Date"]["autoDateTable"] is None


def test_auto_date_tables_are_told_apart_without_their_annotations() -> None:
    conn = metadata_db()
    conn.execute("DELETE FROM Annotation WHERE ObjectType = 3")
    tables = by(metadata_facts(Reader(conn))["tables"], "name")
    assert tables["LocalDateTable_1"]["autoDateTable"] == "local"  # ShowAsVariationsOnly
    assert tables["DateTableTemplate_1"]["autoDateTable"] == "template"  # IsPrivate
    assert tables["Metric Picker"]["autoDateTable"] is None


def test_columns(facts: dict[str, Any]) -> None:
    columns = by(facts["columns"], "table", "name")
    # The row-number column and the engine table's columns are left out.
    assert ("Sales", "RowNumber-2662979B") not in columns
    assert [key for key in columns if key[0] == "H$Sales (1)$Amount (11)"] == []
    assert columns[("Sales", "Amount")] == {
        "table": "Sales",
        "name": "Amount",
        "kind": "data",
        "dataType": 8,
        "dataTypeName": "double",
        "dateTimeType": None,
        "state": None,
        "expression": None,
        "formatString": "#,0.00",
        "sortByColumn": None,
        "dataCategory": None,
        "summarizeBy": "sum",
        "summarizeByCode": 3,
        "hidden": False,
        "key": False,
        "displayFolder": "Money",
        "description": "Sale amount",
        "sourceColumn": "Amount",
        "lineageTag": "c-amount",
        "error": None,
    }
    margin = columns[("Sales", "Margin %")]
    # An automatic explicit type falls back to the inferred type.
    assert (margin["kind"], margin["dataType"], margin["expression"]) == (
        "calculated",
        8,
        "[Amount] * 0.1",
    )
    assert columns[("Sales", "Month")]["sortByColumn"] == "Month Number"
    assert columns[("Sales", "Region")]["dataCategory"] == "Country"
    broken = columns[("Sales", "Broken")]
    assert (broken["dataType"], broken["state"], broken["error"]) == (
        None,
        "semanticError",
        "The column 'Nope' cannot be found.",
    )
    # Date or Date/Time behind a date/time column (the UnderlyingDateTimeDataType annotation).
    assert columns[("Sales", "DateKey")]["dateTimeType"] == "date"
    assert columns[("Date", "Date")]["dateTimeType"] is None  # the model does not say
    assert columns[("Sales", "Region")]["dateTimeType"] is None  # not a date/time column
    date = columns[("Date", "Date")]
    assert (date["kind"], date["dataType"], date["key"], date["sourceColumn"]) == (
        "calculatedTableColumn",
        9,
        True,
        "[Date]",
    )
    # A calculated-table column renamed in the model keeps its source column.
    picker = columns[("Metric Picker", "Metric Picker")]
    assert (picker["sourceColumn"], picker["dataTypeName"]) == ("[Value1]", "text")


def test_measures_with_dynamic_format_strings(facts: dict[str, Any]) -> None:
    measures = by(facts["measures"], "name")
    assert list(measures) == ["Total Sales", "Dynamic", "One"]  # not the engine table's
    assert measures["Total Sales"] == {
        "table": "# Measures",
        "name": "Total Sales",
        "expression": "SUM(Sales[Amount])",
        "formatString": "#,0",
        "formatStringExpression": None,
        "displayFolder": "Sales",
        "description": "All sales",
        "hidden": False,
        "dataType": 8,
        "dataTypeName": "double",
        "lineageTag": "m-total",
        "state": None,
        "error": None,
    }
    assert measures["Dynamic"]["formatStringExpression"] == '"#,0" & " units"'
    # a variant, as --types reports it
    assert (measures["Dynamic"]["dataType"], measures["Dynamic"]["dataTypeName"]) == (
        20,
        "variant",
    )
    assert (measures["One"]["table"], measures["One"]["hidden"]) == ("Sales", True)


def test_relationships(facts: dict[str, Any]) -> None:
    assert facts["relationships"] == [
        {
            "name": "r-date",
            "fromTable": "Sales",
            "fromColumn": "DateKey",
            "toTable": "Date",
            "toColumn": "Date",
            "fromCardinality": "many",
            "toCardinality": "one",
            "cardinality": "M:1",
            "crossFilter": "single",
            "active": True,
            "relyOnReferentialIntegrity": True,
            "securityFilter": "single",
            "joinOnDate": "dateAndTime",
        },
        {
            "name": "r-local",
            "fromTable": "Sales",
            "fromColumn": "DateKey",
            "toTable": "LocalDateTable_1",
            "toColumn": "Date",
            "fromCardinality": "many",
            "toCardinality": "one",
            "cardinality": "M:1",
            "crossFilter": "automatic",
            "active": False,
            "relyOnReferentialIntegrity": False,
            "securityFilter": "none",
            "joinOnDate": "datePartOnly",
        },
        {
            "name": "r-both",
            "fromTable": "Sales",
            "fromColumn": "Month Number",
            "toTable": "Date",
            "toColumn": "Year",
            "fromCardinality": "one",
            "toCardinality": "one",
            "cardinality": "1:1",
            "crossFilter": "both",
            "active": True,
            "relyOnReferentialIntegrity": False,
            "securityFilter": "both",
            "joinOnDate": "dateAndTime",
        },
    ]


def test_hierarchies_roles_groups_and_parameters(facts: dict[str, Any]) -> None:
    assert facts["hierarchies"] == [
        {
            "table": "Date",
            "name": "Calendar",
            "hidden": False,
            "displayFolder": "Dates",
            "description": None,
            "levels": [
                {"name": "Year", "column": "Year", "ordinal": 0},
                {"name": "Day", "column": "Date", "ordinal": 1},
            ],
        },
        {
            "table": "LocalDateTable_1",
            "name": "Date Hierarchy",
            "hidden": False,
            "displayFolder": None,
            "description": None,
            "levels": [],
        },
    ]
    # auto date/time: Sales[DateKey]'s hidden join to its local date table
    assert facts["variations"] == [
        {
            "table": "Sales",
            "column": "DateKey",
            "name": "Variation",
            "relationship": "r-local",
            "dateTable": "LocalDateTable_1",
            "defaultHierarchy": "Date Hierarchy",
            "isDefault": True,
        }
    ]
    assert facts["roles"] == [
        {
            "name": "Europe",
            "description": "Only EU",
            "permission": "read",
            "tables": [{"table": "Sales", "filter": '[Region] = "EU"'}],
        }
    ]
    assert facts["calculationGroups"] == [
        {
            "table": "Time Intelligence",
            "precedence": 10,
            "description": "Time",
            "items": [
                {
                    "name": "Current",
                    "expression": "SELECTEDMEASURE()",
                    "ordinal": 0,
                    "formatStringExpression": None,
                    "description": "as is",
                },
                {
                    "name": "YTD",
                    "expression": "CALCULATE(SELECTEDMEASURE(), DATESYTD('Date'[Date]))",
                    "ordinal": 1,
                    "formatStringExpression": '"0.0%"',
                    "description": None,
                },
            ],
            "selectionExpressions": [],
        }
    ]
    assert facts["fieldParameters"] == [
        {
            "table": "Metric Picker",
            "labelColumn": "Metric Picker",
            "fieldsColumn": "Metric Picker Fields",
            "orderColumn": "Metric Picker Order",
            "rows": [
                {"label": "Total Sales", "table": "# Measures", "name": "Total Sales",
                 "order": 0, "kind": "measure"},
                {"label": "Amount", "table": "Sales", "name": "Amount", "order": 1,
                 "kind": "column"},
            ],
        }
    ]  # fmt: skip
    # A GENERATESERIES table whose column says {"version":0} is a what-if parameter.
    assert facts["whatIfParameters"] == [
        {"table": "Discount", "column": "Discount", "expression": "GENERATESERIES(0, 10, 1)"}
    ]


def test_partitions_and_named_expressions(facts: dict[str, Any]) -> None:
    partitions = by(facts["partitions"], "table")
    assert "H$Sales (1)$Amount (11)" not in partitions
    assert partitions["Sales"] == {
        "table": "Sales",
        "name": "Sales-p",
        "source": "m",
        "mode": "import",
        "expression": M_SALES,
        "refreshedTime": "2024-01-01T00:00:00+00:00",
        "state": None,
        "error": None,
    }
    assert partitions["Date"]["source"] == "calculated"
    assert partitions["# Measures"]["refreshedTime"] is None  # never refreshed
    assert partitions["Time Intelligence"] == {
        "table": "Time Intelligence",
        "name": "Time Intelligence",
        "source": "calculationGroup",
        "mode": "import",
        "expression": None,
        "refreshedTime": None,
        "state": None,
        "error": None,
    }
    assert facts["expressions"] == [
        {
            "name": "Folder",
            "kind": "m",
            "expression": '"C:\\data" meta [IsParameterQuery=true, Type="Text"]',
            "description": "Where the files are",
            "parameter": True,
        },
        {
            "name": "Helper",
            "kind": "m",
            "expression": "let x = 1 in x",
            "description": None,
            "parameter": False,
        },
    ]


def test_a_selection_expression_of_a_calculation_group_is_read() -> None:
    # CalculationGroupSelectionMode, read from the Tabular object model: 1 multiple or empty
    # selection, 2 no selection.
    extra = {
        "CalculationExpression": [
            {"ID": 1, "CalculationGroupID": 40, "Expression": "SELECTEDMEASURE() * 0",
             "SelectionMode": 1},
            {"ID": 2, "CalculationGroupID": 40, "Expression": "SELECTEDMEASURE()",
             "SelectionMode": 2},
        ]
    }  # fmt: skip
    found = metadata_facts(Reader(metadata_db(extra_rows=extra)))
    assert found["calculationGroups"][0]["selectionExpressions"] == [
        {"expression": "SELECTEDMEASURE() * 0", "selectionMode": "multipleOrEmptySelection"},
        {"expression": "SELECTEDMEASURE()", "selectionMode": "noSelection"},
    ]


def test_type_names_agree_with_the_edit_layer() -> None:
    for code, name in pbix_extract._DATA_TYPE_NAMES.items():
        assert pbir_expr.data_type_name(code) in (name, None)
        if code in (2, 6, 8, 9, 10, 11):
            assert pbir_expr.data_type_name(code) == name


# --- schema variants ------------------------------------------------------------------------


def test_an_older_schema_reads_what_it_has() -> None:
    """Older models: FromEnd*/ToEnd* relationship ends, Level.Index, BindingType instead of
    Column.Type, no lineage tags, no calculation groups, format definitions, extended
    properties, annotations, variations or named expressions, and partitions without Type and
    Mode."""
    ends = {f"{s}{p}": f"{s}End{p}" for s in ("From", "To") for p in (
        "TableID", "ColumnID", "Cardinality"
    )}  # fmt: skip
    conn = metadata_db(
        drop_tables=(
            "CalculationGroup",
            "CalculationItem",
            "CalculationExpression",
            "FormatStringDefinition",
            "ExtendedProperty",
            "Expression",
            "Annotation",
            "Variation",
        ),
        drop_columns={
            "Table": ("LineageTag", "IsPrivate", "ShowAsVariationsOnly", "CalculationGroupID"),
            "Column": ("LineageTag",),
            "Measure": ("LineageTag", "FormatStringDefinitionID"),
            "Partition": ("Type", "Mode", "State", "ErrorMessage"),
        },
        rename_columns={
            "Relationship": ends,
            "Level": {"Ordinal": "Index"},
            "Column": {"Type": "BindingType"},
        },
    )
    found = metadata_facts(Reader(conn))
    assert {"calculationGroups", "expressions", "variations"}.isdisjoint(found)
    assert found["model"]["autoDateTime"] is None  # no annotation says
    assert by(found["tables"], "name")["LocalDateTable_1"]["autoDateTable"] is None
    # A partition without Type does not say its query is DAX, and no extended property says
    # a column is a parameter: neither kind of parameter can be told.
    assert found["fieldParameters"] == [] and found["whatIfParameters"] == []
    relationships = by(found["relationships"], "name")
    assert (relationships["r-date"]["fromTable"], relationships["r-date"]["toColumn"]) == (
        "Sales",
        "Date",
    )
    assert [level["name"] for level in found["hierarchies"][0]["levels"]] == ["Year", "Day"]
    columns = by(found["columns"], "table", "name")
    assert ("Sales", "RowNumber-2662979B") not in columns  # BindingType 3
    assert columns[("Sales", "Margin %")]["kind"] == "calculated"
    assert columns[("Sales", "Amount")]["lineageTag"] is None
    tables = by(found["tables"], "name")
    assert (tables["Date"]["source"], tables["Date"]["calculated"]) == (None, False)
    assert tables["Time Intelligence"]["calculationGroup"] is False
    assert by(found["partitions"], "table")["Sales"]["mode"] is None
    assert by(found["measures"], "name")["Dynamic"]["formatStringExpression"] is None


def test_each_section_is_left_out_when_its_metadata_is_missing() -> None:
    found = metadata_facts(
        Reader(metadata_db(drop_tables=("Relationship", "Hierarchy", "Role", "Measure")))
    )
    assert {"relationships", "hierarchies", "roles", "measures"}.isdisjoint(found)
    assert {"tables", "columns", "partitions"} <= set(found)
    # Without the Table table nothing can be named: the version alone, never a crash.
    assert metadata_facts(Reader(metadata_db(drop_tables=("Table",)))) == {"version": 1}
    assert metadata_facts(Reader(sqlite3.connect(":memory:"))) == {"version": 1}
    assert metadata_facts(SimpleNamespace(query=lambda _sql: pd.DataFrame())) == {"version": 1}

    class Exploding:
        def query(self, sql: str) -> pd.DataFrame:
            raise RuntimeError("the metadata is unreadable")

    assert metadata_facts(Exploding()) == {"version": 1}


def test_a_model_without_roles_hierarchies_or_groups_has_empty_sections() -> None:
    conn = metadata_db()
    for table in ("Role", "TablePermission", "Hierarchy", "Level", "Expression"):
        conn.execute(f"DELETE FROM [{table}]")  # noqa: S608 - fixture table names
    conn.execute("DELETE FROM CalculationGroup")
    emptied = metadata_facts(Reader(conn))
    assert (emptied["roles"], emptied["hierarchies"], emptied["expressions"]) == ([], [], [])
    assert emptied["calculationGroups"] == []  # the table exists and says "none"


@pytest.mark.parametrize(
    ("code", "mode"),
    [(1, "directQuery"), (2, "default"), (4, "dual"), (5, "directLake"), (99, "unknown")],
)
def test_partition_and_model_storage_codes_preserve_unknown_values(code: int, mode: str) -> None:
    conn = metadata_db()
    conn.execute("UPDATE Model SET DefaultMode = ?", (code,))
    conn.execute("UPDATE Partition SET Mode = ? WHERE TableID = 1", (code,))
    found = metadata_facts(Reader(conn))
    assert found["model"]["defaultMode"] == mode
    assert by(found["partitions"], "table")["Sales"]["mode"] == mode
    expected = model_facts.storage_mode(mode)
    if expected == "default":
        expected = "unknown"
    assert model_facts.table_storage_modes(found)[0]["mode"] == expected


def test_long_texts_are_cut_and_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pbix_extract, "_MAX_TEXT", 30)
    found = metadata_facts(Reader(metadata_db()))
    sales = by(found["partitions"], "table")["Sales"]
    assert sales["expression"] == M_SALES[:30] and sales["truncated"] is True
    discount = by(found["partitions"], "table")["Discount"]
    assert discount["expression"] == "GENERATESERIES(0, 10, 1)" and "truncated" not in discount
    # the top level names every section that lost text
    assert found["truncated"] == ["calculationGroups", "expressions", "partitions", "tables"]


def test_the_queries_are_fixed_single_table_selects() -> None:
    reader = Reader(metadata_db())
    metadata_facts(reader)
    selects = [sql for sql in reader.seen if sql.startswith("SELECT")]
    assert selects and all(" FROM [" in sql and " JOIN " not in sql for sql in selects)
    assert all("?" not in sql and "%" not in sql and "'" not in sql for sql in selects)
    tables = {sql.split(" FROM [")[1].split("]")[0] for sql in selects}
    assert "RoleMembership" not in tables and "DataSource" not in tables
    assert tables == {
        "Model", "Table", "Column", "Measure", "FormatStringDefinition", "Relationship",
        "Hierarchy", "Level", "Role", "TablePermission", "CalculationGroup", "CalculationItem",
        "CalculationExpression", "Partition", "Expression", "ExtendedProperty", "Annotation",
        "Variation",
    }  # fmt: skip
    # Annotations are read whole but only the auto date/time and date/time-type ones are kept:
    # a tool's own settings (PBI_QueryOrder here) are not facts.
    assert "PBI_QueryOrder" not in json.dumps(metadata_facts(Reader(metadata_db())))


def test_the_facts_stay_under_the_readers_limit_and_name_what_they_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child's output is read up to 16 MB: past the total cap the longest texts are cut
    step by step (Enter-data queries first in practice) rather than the read failing."""
    big = "let Source = " + "x" * 50_000 + " in Source"
    extra = {
        "Partition": [
            {"ID": 50 + n, "TableID": 1, "Name": f"Part {n}", "Type": 4, "Mode": 0,
             "QueryDefinition": big}
            for n in range(6)
        ]
    }  # fmt: skip
    monkeypatch.setattr(pbix_extract, "_MAX_TOTAL", 60_000)
    found = metadata_facts(Reader(metadata_db(extra_rows=extra)))
    assert len(json.dumps(found)) <= 60_000
    assert found["truncated"] == ["partitions"]
    cut = [p for p in found["partitions"] if p["name"].startswith("Part ")]
    assert all(p["truncated"] is True and len(p["expression"] or "") <= 2_000 for p in cut)
    assert by(found["partitions"], "table")["Discount"]["expression"] == (
        "GENERATESERIES(0, 10, 1)"  # short texts are kept whole
    )


def test_the_production_reader_runs_the_same_queries(facts: dict[str, Any]) -> None:
    reader_type = pytest.importorskip("pbixray.meta.sqlite_source")._SqliteReader
    reader = reader_type(metadata_db().serialize())
    try:
        found = metadata_facts(reader)
    finally:
        reader.close()
    assert found == facts


# --- field parameters -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dax", "rows"),
    [
        (
            PICKER,
            [
                {"label": "Total Sales", "table": "# Measures", "name": "Total Sales", "order": 0},
                {"label": "Amount", "table": "Sales", "name": "Amount", "order": 1},
            ],
        ),
        (
            '{ ("Say ""hi""", NAMEOF([It]]s]), 2.5) } // a comment',
            [{"label": 'Say "hi"', "table": None, "name": "It]s", "order": 2.5}],
        ),
        ("{(NAMEOF('A''s'[X]))}", [{"label": None, "table": "A's", "name": "X", "order": None}]),
        ("GENERATESERIES(1, 30, 1)", None),
        ('{ ("A", 1) }', None),  # no NAMEOF
        ('{ ("A", NAMEOF([X]), [Other]) }', None),  # not a literal
        ('{ ("A", NAMEOF([X]), 0)', None),  # unterminated
        ("{}", None),
    ],
)
def test_field_parameter_rows(dax: str, rows: list[dict[str, Any]] | None) -> None:
    assert field_parameter_rows(dax) == rows


# --- reading a PBIX ---------------------------------------------------------------------------


def _fake_pbixray(monkeypatch: pytest.MonkeyPatch, model: Any) -> None:
    class FakePBIXRay:
        def __init__(self, path: str, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> Any:
            return model

        def __exit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr(pbix_extract, "PBIXRay", FakePBIXRay)


def test_read_model_facts_reads_the_metadata_of_a_pbix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, facts: dict[str, Any]
) -> None:
    source = SimpleNamespace(_db=Reader(metadata_db()))
    _fake_pbixray(monkeypatch, SimpleNamespace(_metadata=SimpleNamespace(source=source)))
    assert pbix_extract.read_model_facts(tmp_path / "r.pbix") == facts
    source._db = None
    assert pbix_extract.read_model_facts(tmp_path / "r.pbix") == {"version": 1}


def test_the_import_extraction_keeps_the_facts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, facts: dict[str, Any]
) -> None:
    empty = pd.DataFrame()
    model = SimpleNamespace(
        _metadata=SimpleNamespace(source=SimpleNamespace(_db=Reader(metadata_db()))),
        tables=[],
        dax_measures=pd.DataFrame([{"TableName": "# Measures", "Name": "Total Sales"}]),
        relationships=empty,
        schema=pd.DataFrame([{"TableName": "Sales", "ColumnName": "Amount"}]),
    )
    _fake_pbixray(monkeypatch, model)
    pbix_extract.extract(tmp_path / "r.pbix", tmp_path)
    written = json.loads((tmp_path / "model.json").read_text(encoding="utf-8"))
    assert written["facts"] == facts
    assert written["relationships_source"] == "metadata"


AGENTS = Path.home() / "Downloads" / "Agents Performance - Dashboard.pbix"


@pytest.mark.skipif(not AGENTS.exists(), reason="the real Agents Performance report is absent")
def test_the_real_agents_report_facts() -> None:
    result = subprocess.run(  # noqa: S603 - the module's own CLI on a local file
        [sys.executable, str(Path(pbix_extract.__file__)), str(AGENTS), "--facts"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=True,
    )
    found = json.loads(result.stdout)
    counts = {k: len(v) for k, v in found.items() if isinstance(v, list)}
    assert counts == {
        "tables": 10,
        "columns": 68,
        "measures": 102,
        "relationships": 4,
        "hierarchies": 2,
        "variations": 0,
        "roles": 0,
        "calculationGroups": 0,
        "fieldParameters": 0,
        "whatIfParameters": 1,
        "partitions": 10,
        "expressions": 0,
    }
    # Auto date/time is off in this report (the model annotation says "0"); its date columns
    # are dates, not date/times.
    assert found["model"]["autoDateTime"] is False
    columns = by(found["columns"], "table", "name")
    assert columns[("FactSales", "DateKey")]["dateTimeType"] == "date"
    assert "truncated" not in found
    tables = by(found["tables"], "name")
    assert tables["Top-Bottom-N"]["expression"] == "GENERATESERIES(1, 30, 1)"
    assert tables["Date"]["isDateTable"] and tables["Date"]["dateColumn"] == "Date"
    assert tables["Date"]["expression"] == "DateAutoTemplate"
    assert found["whatIfParameters"] == [
        {
            "table": "Top-Bottom-N",
            "column": "Top-Bottom-N",
            "expression": "GENERATESERIES(1, 30, 1)",
        }
    ]
    top = by(found["columns"], "table", "name")[("Top-Bottom-N", "Top-Bottom-N")]
    assert (top["kind"], top["sourceColumn"], top["dataType"]) == (
        "calculatedTableColumn",
        "[Value]",
        6,
    )
    joins = {(r["fromTable"], r["fromColumn"], r["toTable"], r["cardinality"]) for r in found[
        "relationships"
    ]}  # fmt: skip
    assert ("FactSales", "DateKey", "Date", "M:1") in joins
    assert [lv["column"] for lv in by(found["hierarchies"], "table")["Date"]["levels"]] == [
        "Year",
        "Year Month",
        "Date",
    ]
    assert found["model"]["culture"] == "en-US"


# --- what Studio keeps ------------------------------------------------------------------------


def test_the_stored_facts_are_bounded(
    facts: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = model_facts.stored(facts)
    assert set(kept) == set(facts)
    # measure DAX lives in the report's measures rows; a calculated partition's in its table
    assert all("expression" not in m for m in kept["measures"])
    assert by(kept["measures"], "name")["Dynamic"]["formatStringExpression"] == '"#,0" & " units"'
    assert by(kept["partitions"], "table")["Date"]["expression"] is None
    assert by(kept["partitions"], "table")["Sales"]["expression"] == M_SALES
    assert by(kept["tables"], "name")["Date"]["expression"].startswith("CALENDAR(")
    monkeypatch.setattr(model_facts, "_TEXT", 10)
    monkeypatch.setitem(model_facts._LIMITS, "columns", 3)
    cut = model_facts.stored(facts)
    sales = by(cut["partitions"], "table")["Sales"]
    assert (sales["expression"], sales["truncated"]) == (M_SALES[:10], True)
    assert len(cut["columns"]) == 3
    # every section that lost items or text is named
    assert cut["truncated"] == ["columns", "expressions", "measures", "partitions", "roles"]
    assert facts["partitions"][0]["expression"] == M_SALES  # the input is not changed
    assert "truncated" not in kept


def test_rows_in_the_import_shapes(facts: dict[str, Any]) -> None:
    schema = model_facts.schema_rows(facts)
    assert schema is not None
    assert {"TableName": "Sales", "ColumnName": "Amount", "PandasDataType": "Float64"} in schema
    assert {"TableName": "Sales", "ColumnName": "Broken", "PandasDataType": "object"} in schema
    assert {"TableName": "Date", "ColumnName": "Date", "PandasDataType": "datetime64[ns]"} in schema
    assert len(schema) == len(facts["columns"])
    assert model_facts.relationship_rows(facts) == [
        {
            "FromTableName": "Sales",
            "FromColumnName": "DateKey",
            "ToTableName": "Date",
            "ToColumnName": "Date",
            "IsActive": 1,
            "Cardinality": "M:1",
            "CrossFilteringBehavior": "Single",
            "RelyOnReferentialIntegrity": 1,
            "JoinOnDateBehavior": "dateAndTime",
        },
        {
            "FromTableName": "Sales",
            "FromColumnName": "DateKey",
            "ToTableName": "LocalDateTable_1",
            "ToColumnName": "Date",
            "IsActive": 0,
            "Cardinality": "M:1",
            "CrossFilteringBehavior": "Single",
            "RelyOnReferentialIntegrity": 0,
            "JoinOnDateBehavior": "datePartOnly",
        },
        {
            "FromTableName": "Sales",
            "FromColumnName": "Month Number",
            "ToTableName": "Date",
            "ToColumnName": "Year",
            "IsActive": 1,
            "Cardinality": "1:1",
            "CrossFilteringBehavior": "Both",
            "RelyOnReferentialIntegrity": 0,
            "JoinOnDateBehavior": "dateAndTime",
        },
    ]
    assert model_facts.calculated(facts) == {
        "Date": "CALENDAR(DATE(2020, 1, 1), DATE(2020, 12, 31))",
        "Metric Picker": PICKER,
        "Discount": "GENERATESERIES(0, 10, 1)",
        "LocalDateTable_1": "Calendar(Date(2020, 1, 1), Date(2020, 12, 31))",
        "DateTableTemplate_1": "Calendar(Date(2015, 1, 1), Date(2015, 1, 1))",
        "Sales[Margin %]": "[Amount] * 0.1",
        "Sales[Broken]": "[Nope]",
    }
    for derive in (model_facts.schema_rows, model_facts.relationship_rows, model_facts.calculated):
        assert derive(None) is None and derive({"version": 1}) is None


def _renamed(facts: dict[str, Any]) -> dict[str, Any]:
    """The model after Desktop renamed Sales to Orders and Amount to Revenue, changed the
    margin's DAX, added a calculated column and refreshed the Sales partition."""
    head: dict[str, Any] = json.loads(json.dumps(facts))
    for table in head["tables"]:
        if table["name"] == "Sales":
            table["name"] = "Orders"
    for column in head["columns"]:
        if column["table"] == "Sales":
            column["table"] = "Orders"
        if column["name"] == "Amount":
            column["name"] = "Revenue"
        if column["name"] == "Margin %":
            column["expression"] = "[Revenue] * 0.2"
    head["columns"].append(
        {"table": "Orders", "name": "Big", "kind": "calculated", "expression": "[Revenue] > 9"}
    )
    for partition in head["partitions"]:
        if partition["table"] == "Sales":
            partition["table"] = "Orders"
            partition["refreshedTime"] = "2024-02-01T00:00:00+00:00"
    return head


def test_the_snapshot_digest_follows_renames_and_says_what_changed(facts: dict[str, Any]) -> None:
    digest = model_facts.digest(facts, "crc-1")
    assert digest["model_key"] == "crc-1" and digest["v"] == 1
    assert digest["measures"] == model_facts.measure_hashes(
        [{"Name": "Total Sales", "Expression": "SUM(Sales[Amount])"},
         {"Name": "Dynamic", "Expression": "[Total Sales]"}, {"Name": "One", "Expression": "1"}]
    )  # fmt: skip
    head = _renamed(facts)
    orders = by(head["tables"], "name")["Orders"]
    assert (model_facts.snapshot_table(digest, orders) or {}).get("name") == "Sales"  # lineage
    revenue = by(head["columns"], "table", "name")[("Orders", "Revenue")]
    assert (model_facts.snapshot_column(digest, orders, revenue) or {}).get("name") == "Amount"
    # Without lineage tags a data column is found by its source column, once.
    untagged = {**revenue, "lineageTag": None}
    assert (model_facts.snapshot_column(digest, orders, untagged) or {}).get("name") == "Amount"
    assert model_facts.changed_calculations(head, digest) == [
        {"table": "Orders", "column": "Margin %"},
        {"table": "Orders", "column": "Big"},
    ]
    assert model_facts.changed_calculations(facts, digest) == []
    status = model_facts.data_status(head, digest)
    assert (status["known"], status["stale"], status["tables"]) == (True, True, ["Orders"])
    assert status["detail"].startswith("Power BI Desktop refreshed or added Orders since")
    assert model_facts.data_status(facts, digest)["stale"] is False
    assert model_facts.data_status(facts, None) == {
        "known": False,
        "stale": False,
        "tables": [],
        "detail": None,
    }
    # A new data table is data the rows lack; a changed calculated table is not stale data.
    grown = json.loads(json.dumps(facts))
    grown["tables"].append({"name": "Targets", "source": "m"})
    grown["partitions"].append({"table": "Targets", "source": "m", "refreshedTime": None})
    grown["partitions"][1]["refreshedTime"] = "2025-01-01T00:00:00+00:00"  # the Date table
    assert model_facts.data_status(grown, digest)["tables"] == ["Targets"]


def test_a_new_object_with_an_old_name_is_not_the_old_one(facts: dict[str, Any]) -> None:
    """Two different lineage tags are two objects: Sales renamed Old Sales and a new Sales
    added in Power Query must not share rows; a column deleted and added again (a new tag, or
    another type) is not the stored one."""
    digest = model_facts.digest(facts, "crc-1")
    new_sales = {"name": "Sales", "lineageTag": "t-new-sales"}
    assert model_facts.snapshot_table(digest, new_sales) is None
    # the name decides only when a side has no tag
    assert (model_facts.snapshot_table(digest, {"name": "Sales"}) or {}).get("name") == "Sales"
    # # Measures has no tag in the rows' model: a tagged head table of that name is it
    assert (
        model_facts.snapshot_table(digest, {"name": "# Measures", "lineageTag": "t-m"}) or {}
    ).get("name") == "# Measures"
    sales = by(facts["tables"], "name")["Sales"]
    readded = {"table": "Sales", "name": "Amount", "kind": "data", "lineageTag": "c-new",
               "sourceColumn": "Amount", "dataType": 8}  # fmt: skip
    assert model_facts.snapshot_column(digest, sales, readded) is None
    as_text = {**by(facts["columns"], "table", "name")[("Sales", "Amount")], "dataType": 2}
    assert model_facts.snapshot_column(digest, sales, as_text) is None
    untyped = {**as_text, "dataType": None}  # a type the head does not say agrees
    assert (model_facts.snapshot_column(digest, sales, untyped) or {}).get("name") == "Amount"


def test_the_payload_notes(facts: dict[str, Any]) -> None:
    digest = model_facts.digest(facts, "crc-1")
    stored = model_facts.stored(_renamed(facts))
    found = model_facts.payload(stored, digest, status="current")
    assert found["status"] == "current" and found["tables"] == stored["tables"]
    notes = found["studio"]
    assert notes["rowLevelSecurity"] == model_facts.RLS_NOTE
    assert [(n["table"], n["kind"]) for n in notes["powerBiOnly"]] == [
        ("Time Intelligence", "calculationGroup"),
        ("Metric Picker", "fieldParameter"),
    ]
    assert notes["powerBiOnly"][1]["detail"] == (
        "Visuals that use the field parameter Metric Picker show in Power BI only; Studio's "
        "canvas does not switch fields."
    )
    assert notes["data"]["stale"] is True and notes["actions"] == ["reread_data"]
    assert notes["autoDateTime"] is True
    assert notes["changedCalculations"] == [
        {"table": "Orders", "column": "Margin %"},
        {"table": "Orders", "column": "Big"},
    ]
    for status in ("reading", "unknown", "none"):
        found = model_facts.payload(None, None, status=status)
        assert found["status"] == status
        assert found["storage"]["kind"] == ("no_local_model" if status == "none" else "unknown")
    # Facts that say nothing of the tables and columns are not "current": nothing in them rules
    # out a calculation group or a field parameter.
    unknown = model_facts.payload({"version": 1}, None, status="current")
    assert unknown["status"] == "unknown" and unknown["storage"]["kind"] == "unknown"
    assert model_facts.power_bi_only({"version": 1}) is None
    assert model_facts.power_bi_only(stored) == {
        "Time Intelligence": "calculationGroup",
        "Metric Picker": "fieldParameter",
    }
    # Without the rows' own model every calculated object is distrusted, and reading the data
    # again (which records it) is offered; a report without rows is offered nothing.
    blind = model_facts.payload(stored, None, status="current")["studio"]
    assert [(c["table"], c["column"]) for c in blind["changedCalculations"]] == [
        ("Date", None),
        ("Metric Picker", None),
        ("Discount", None),
        ("LocalDateTable_1", None),
        ("DateTableTemplate_1", None),
        ("Orders", "Margin %"),
        ("Orders", "Broken"),
        ("Orders", "Big"),
    ]
    assert blind["actions"] == ["reread_data"]
    rowless = model_facts.payload(stored, None, status="current", rows=False)["studio"]
    assert rowless["actions"] == []
    fresh = model_facts.payload(model_facts.stored(facts), digest, status="current")["studio"]
    assert (fresh["actions"], fresh["changedCalculations"]) == ([], [])


def test_the_engine_inputs_change_with_calculations_names_and_the_snapshot(
    facts: dict[str, Any],
) -> None:
    digest = model_facts.digest(facts, "crc-1")
    base = model_facts.engine_inputs(facts, digest)
    assert base == model_facts.engine_inputs(json.loads(json.dumps(facts)), digest)
    assert model_facts.engine_inputs(None, None) is None
    # a digest alone still decides whether the engine may answer
    assert model_facts.engine_inputs(None, digest) not in (None, base)
    assert model_facts.engine_inputs(_renamed(facts), digest) != base
    erring = json.loads(json.dumps(facts))
    erring["measures"][0]["error"] = "The column 'Amount' cannot be found."
    assert model_facts.engine_inputs(erring, digest) != base  # a measure now in error
    assert model_facts.engine_inputs(facts, model_facts.digest(facts, "crc-2")) != base
    described = json.loads(json.dumps(facts))
    described["columns"][0]["description"] = "Words only"
    assert model_facts.engine_inputs(described, digest) == base  # not an engine input
