"""Run by ``test_desktop_model`` under pbi-cli's own Python: every change kind of Studio's runner on
Microsoft's real Tabular Object Model, on an offline model (a Database object that is never
connected, so nothing is saved anywhere), each followed by its own undo.

Prints JSON: the TOM type loaded, "restored" per change when its undo gives back the model it
started from (collections compared regardless of order: a restored object is appended), and the
columns of the field parameter the runner builds.

Usage: python tom_offline_check.py <path to scripts/studio_tom_runner.py>
"""

from __future__ import annotations

import importlib.util
import json
import sys
from typing import Any

TEXT_META = ' meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]'


def _load(path: str) -> Any:
    spec = importlib.util.spec_from_file_location("studio_tom_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _norm(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    if isinstance(value, list):
        return sorted((_norm(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    return value


def main() -> None:
    runner_module = _load(sys.argv[1])
    tom, _server, _adomd = runner_module._load_tom()
    import Microsoft.AnalysisServices as analysis_services  # type: ignore[import-not-found]

    database = tom.Database()
    database.Name = database.ID = "Offline"
    # 1601: dynamic format strings of measures need it (a model that has them is at least this).
    database.CompatibilityLevel = 1601
    database.CompatibilityMode = analysis_services.CompatibilityMode.PowerBI
    model = tom.Model()
    database.Model = model

    def data_table(name: str, columns: list[tuple[str, str]]) -> Any:
        table = tom.Table()
        table.Name = name
        for column_name, data_type in columns:
            column = tom.DataColumn()
            column.Name = column_name
            column.DataType = getattr(tom.DataType, data_type)
            column.SourceColumn = column_name
            table.Columns.Add(column)
        partition = tom.Partition()
        partition.Name = name
        source = tom.MPartitionSource()
        source.Expression = "let Source = 1 in Source"
        partition.Source = source
        table.Partitions.Add(partition)
        model.Tables.Add(table)
        return table

    sales = data_table(
        "FactSales",
        [
            ("StoreKey", "Int64"),
            ("DateKey", "DateTime"),
            ("SalesAmount", "Double"),
            ("TotalCost", "Double"),
        ],
    )
    data_table(
        "Date",
        [("Date", "DateTime"), ("Year", "Int64"), ("Month", "String"), ("Month Number", "Int64")],
    )
    store = data_table("DimStore", [("StoreKey", "Int64"), ("StoreName", "String")])
    measures = data_table("# Measures", [])
    total = tom.Measure()
    total.Name = "Total Sales"
    total.Expression = "SUM(FactSales[SalesAmount])"
    measures.Measures.Add(total)
    relationship = tom.SingleColumnRelationship()
    relationship.Name = "r1"
    relationship.FromColumn = sales.Columns.Find("StoreKey")
    relationship.ToColumn = store.Columns.Find("StoreKey")
    relationship.FromCardinality = tom.RelationshipEndCardinality.Many
    relationship.ToCardinality = tom.RelationshipEndCardinality.One
    model.Relationships.Add(relationship)

    runner = runner_module.Runner(tom, model, None, lambda _event: None)

    def state() -> Any:
        return _norm(json.loads(str(tom.JsonSerializer.SerializeObject(model))))

    setup = [
        {
            "kind": "hierarchy",
            "action": "create",
            "path": {"table": "Date", "name": "Calendar"},
            "props": {
                "levels": [{"name": "Year", "column": "Year"}, {"name": "Month", "column": "Month"}]
            },
        },
        {
            "kind": "calculationGroup",
            "action": "create",
            "path": {"name": "Time Calc"},
            "props": {
                "precedence": 10,
                "items": [
                    {"name": "Current", "expression": "SELECTEDMEASURE()"},
                    {
                        "name": "YTD",
                        "expression": "SELECTEDMEASURE()",
                        "formatStringExpression": '"0.0"',
                    },
                ],
            },
        },
        {
            "kind": "role",
            "action": "create",
            "path": {"name": "Stores"},
            "props": {"tablePermissions": {"DimStore": "[StoreKey] = 1"}},
        },
        {
            "kind": "expression",
            "action": "create",
            "path": {"name": "Region"},
            "props": {"expression": '"West"' + TEXT_META},
        },
        {
            "kind": "fieldParameter",
            "action": "create",
            "path": {"name": "Metric"},
            "props": {"expression": "{\n    (\"Sales\", NAMEOF('# Measures'[Total Sales]), 0)\n}"},
        },
    ]
    for change in setup:
        runner.apply_change(change, tolerant=False)

    changes = [
        {
            "kind": "measure",
            "action": "create",
            "path": {"table": "# Measures", "name": "Margin"},
            "props": {
                "expression": "1",
                "formatString": "0.0%",
                "displayFolder": "KPI",
                "isHidden": True,
            },
        },
        {
            "kind": "measure",
            "action": "update",
            "path": {"table": "# Measures", "name": "Total Sales"},
            "props": {"expression": "2", "formatString": "#,0"},
        },
        {
            "kind": "measure",
            "action": "update",
            "path": {"table": "# Measures", "name": "Total Sales"},
            "props": {
                "formatStringExpression": '"#,0"',
                "detailRowsExpression": 'SELECTCOLUMNS(FactSales, "S", FactSales[SalesAmount])',
                "kpi": {"targetExpression": "100", "statusExpression": "1"},
            },
        },
        {
            "kind": "measure",
            "action": "rename",
            "path": {"table": "# Measures", "name": "Total Sales"},
            "newName": "Sales",
        },
        # Renames that only change letter case (TOM's Find ignores case: bug review #9).
        {
            "kind": "measure",
            "action": "rename",
            "path": {"table": "# Measures", "name": "Total Sales"},
            "newName": "total sales",
        },
        {
            "kind": "column",
            "action": "rename",
            "path": {"table": "Date", "name": "Year"},
            "newName": "year",
        },
        {"kind": "table", "action": "rename", "path": {"name": "DimStore"}, "newName": "Dimstore"},
        {
            "kind": "hierarchy",
            "action": "rename",
            "path": {"table": "Date", "name": "Calendar"},
            "newName": "calendar",
        },
        {
            "kind": "calculationItem",
            "action": "rename",
            "path": {"group": "Time Calc", "name": "YTD"},
            "newName": "ytd",
        },
        {"kind": "role", "action": "rename", "path": {"name": "Stores"}, "newName": "STORES"},
        {
            "kind": "measure",
            "action": "delete",
            "path": {"table": "# Measures", "name": "Total Sales"},
        },
        {
            "kind": "calculatedColumn",
            "action": "create",
            "path": {"table": "FactSales", "name": "Margin"},
            "props": {
                "expression": "FactSales[SalesAmount] - FactSales[TotalCost]",
                "dataType": "Double",
                "summarizeBy": "Sum",
                "sortByColumn": "StoreKey",
            },
        },
        {
            "kind": "column",
            "action": "update",
            "path": {"table": "Date", "name": "Month"},
            "props": {
                "sortByColumn": "Month Number",
                "dataCategory": "WebUrl",
                "summarizeBy": "None",
                "isHidden": True,
                "displayFolder": "x",
                "annotations": {"SummarizationSetBy": "User"},
            },
        },
        {
            "kind": "column",
            "action": "rename",
            "path": {"table": "DimStore", "name": "StoreName"},
            "newName": "Store",
        },
        {"kind": "column", "action": "delete", "path": {"table": "Date", "name": "Month Number"}},
        {
            "kind": "calculatedTable",
            "action": "create",
            "path": {"name": "Years"},
            "props": {"expression": "VALUES('Date'[Year])", "isHidden": True},
        },
        {
            "kind": "table",
            "action": "update",
            "path": {"name": "Date"},
            "props": {"isHidden": True, "description": "d"},
        },
        {"kind": "table", "action": "rename", "path": {"name": "DimStore"}, "newName": "Store"},
        {"kind": "table", "action": "delete", "path": {"name": "DimStore"}},
        {
            "kind": "dateTable",
            "action": "update",
            "path": {"name": "Date"},
            "props": {"column": "Date"},
        },
        {
            "kind": "relationship",
            "action": "create",
            "path": {
                "fromTable": "FactSales",
                "fromColumn": "DateKey",
                "toTable": "Date",
                "toColumn": "Date",
            },
            "props": {
                "fromCardinality": "Many",
                "toCardinality": "One",
                "crossFilteringBehavior": "OneDirection",
                "isActive": True,
                "relyOnReferentialIntegrity": True,
            },
        },
        {
            "kind": "relationship",
            "action": "update",
            "path": {
                "fromTable": "DimStore",
                "fromColumn": "StoreKey",
                "toTable": "FactSales",
                "toColumn": "StoreKey",
            },
            "props": {
                "crossFilteringBehavior": "BothDirections",
                "securityFilteringBehavior": "BothDirections",
                "isActive": False,
            },
        },
        {
            "kind": "relationship",
            "action": "delete",
            "path": {
                "fromTable": "FactSales",
                "fromColumn": "StoreKey",
                "toTable": "DimStore",
                "toColumn": "StoreKey",
            },
        },
        {
            "kind": "hierarchy",
            "action": "update",
            "path": {"table": "Date", "name": "Calendar"},
            "props": {
                "levels": [
                    {"name": "Month", "column": "Month"},
                    {"name": "Year", "column": "Year"},
                    {"name": "Day", "column": "Date"},
                ]
            },
        },
        {
            "kind": "hierarchy",
            "action": "rename",
            "path": {"table": "Date", "name": "Calendar"},
            "newName": "Cal",
        },
        {"kind": "hierarchy", "action": "delete", "path": {"table": "Date", "name": "Calendar"}},
        {
            "kind": "fieldParameter",
            "action": "update",
            "path": {"name": "Metric"},
            "props": {"expression": "{(\"A\", NAMEOF('# Measures'[Total Sales]), 0)}"},
        },
        {
            "kind": "calculationGroup",
            "action": "update",
            "path": {"name": "Time Calc"},
            "props": {"precedence": 20, "description": "g"},
        },
        {
            "kind": "calculationItem",
            "action": "create",
            "path": {"group": "Time Calc", "name": "PY"},
            "props": {"expression": "SELECTEDMEASURE()"},
        },
        {
            "kind": "calculationItem",
            "action": "update",
            "path": {"group": "Time Calc", "name": "YTD"},
            "props": {"formatStringExpression": "", "ordinal": 5},
        },
        {
            "kind": "calculationItem",
            "action": "rename",
            "path": {"group": "Time Calc", "name": "YTD"},
            "newName": "Year",
        },
        {
            "kind": "calculationItem",
            "action": "delete",
            "path": {"group": "Time Calc", "name": "Current"},
        },
        {
            "kind": "role",
            "action": "update",
            "path": {"name": "Stores"},
            "props": {
                "tablePermissions": {"DimStore": None, "FactSales": "[StoreKey] = 1"},
                "description": "r",
            },
        },
        {"kind": "role", "action": "rename", "path": {"name": "Stores"}, "newName": "Shops"},
        {"kind": "role", "action": "delete", "path": {"name": "Stores"}},
        {
            "kind": "partition",
            "action": "update",
            "path": {"table": "FactSales"},
            "props": {"mExpression": "let Source = 2 in Source"},
        },
        {
            "kind": "expression",
            "action": "update",
            "path": {"name": "Region"},
            "props": {"expression": '"East"' + TEXT_META},
        },
        {"kind": "expression", "action": "delete", "path": {"name": "Region"}},
        {
            "kind": "model",
            "action": "update",
            "path": {},
            "props": {
                "discourageImplicitMeasures": True,
                "culture": "en-GB",
                "annotations": {"__PBI_TimeIntelligenceEnabled": "0"},
            },
        },
    ]
    results: dict[str, str] = {}
    for index, change in enumerate(changes):
        label = f"{index:02d} {change['kind']}.{change['action']}"
        before = state()
        try:
            undo, _ = runner.apply_change(change, tolerant=False)
            if state() == before:
                results[label] = "no change"
                continue
            for step in undo:
                runner.apply_change(step, tolerant=False)
            results[label] = "restored" if state() == before else "not restored"
        except Exception as error:  # noqa: BLE001 - reported to the test
            results[label] = f"{type(error).__name__}: {error}"[:300]
    # A role's members never leave the runner and never come back with an undo (spec review #6).
    member_role = tom.ModelRole()
    member_role.Name = "Members"
    member_role.ModelPermission = tom.ModelPermission.Read
    member = tom.ExternalModelRoleMember()
    member.MemberName = "someone@example.com"
    member.IdentityProvider = "AzureAD"
    member_role.Members.Add(member)
    model.Roles.Add(member_role)
    described = json.dumps(runner.snapshot()["roles"])
    undo, before_image = runner.apply_change(
        {"kind": "role", "action": "delete", "path": {"name": "Members"}}, tolerant=False
    )
    printed = json.dumps([undo, before_image])
    for step in undo:
        runner.apply_change(step, tolerant=False)
    restored = model.Roles.Find("Members")
    members = {
        "in_snapshot": "someone@example.com" in described,
        "in_undo": "someone@example.com" in printed,
        "restored_role": restored is not None,
        "restored_members": len(list(restored.Members)) if restored is not None else None,
    }
    # The undo of a create deletes only the object it created (bug review #3).
    created, _ = runner.apply_change(
        {
            "kind": "measure",
            "action": "create",
            "path": {"table": "# Measures", "name": "Expect"},
            "props": {"expression": "1"},
        },
        tolerant=False,
    )
    measures.Measures.Find("Expect").Expression = "2"  # another object now has the name
    runner.apply_change(created[0], tolerant=True)
    kept_other = measures.Measures.Find("Expect") is not None
    measures.Measures.Find("Expect").Expression = "1"
    runner.apply_change(created[0], tolerant=True)
    expect = {
        "kept_another_object": kept_other,
        "deleted_its_own": measures.Measures.Find("Expect") is None,
    }
    parameter = next(
        t
        for t in json.loads(str(tom.JsonSerializer.SerializeObject(model)))["tables"]
        if t["name"] == "Metric"
    )
    print(
        json.dumps(
            {
                "tom": f"{type(model).__module__}.{type(model).__name__}",
                "changes": results,
                "members": members,
                "expect": expect,
                "field_parameter_columns": [
                    [
                        c["name"],
                        c.get("sourceColumn"),
                        c.get("sortByColumn"),
                        c.get("isHidden", False),
                    ]
                    for c in parameter["columns"]
                ],
            }
        )
    )


if __name__ == "__main__":
    main()
