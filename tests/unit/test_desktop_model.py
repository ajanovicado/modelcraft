"""Studio's TOM runner and its Python side: request building, the runner's transaction and undo,
the pinned environment, the process protocol, and the Desktop controller around them.

The runner script itself runs in-process over ``fake_tom`` (the fake keeps TOM's collections,
references, save and undo semantics); two tests at the end run it under pbi-cli's own Python
against Microsoft's real libraries without connecting to anything (skipped when pbi-cli is not
installed).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fake_tom import (
    KPI,
    RUNNER_PATH,
    DetailRowsDefinition,
    FormatStringDefinition,
    Model,
    Server,
    agents_state,
    load_runner,
    snapshot_of,
)

from ai_powerbi_analyst.studio import desktop_model, desktop_session
from ai_powerbi_analyst.studio.desktop_model import (
    ModelChangeFailed,
    ModelOutcomeUnknown,
    ModelRefreshFailed,
    ModelRunnerUnavailable,
    RunnerAnswer,
    apply_model_operations,
    parse_events,
    plan_changes,
    runner_environment,
)
from ai_powerbi_analyst.studio.desktop_session import (
    DesktopController,
    DesktopInstance,
    DesktopRefreshFailed,
    DesktopSessionError,
)
from ai_powerbi_analyst.studio.report_edit_model import (
    OUTSIDE_SUPPORT,
    CalculationItemSpec,
    CreateCalculatedColumn,
    CreateCalculatedTable,
    CreateCalculationGroup,
    CreateFieldParameter,
    CreateHierarchy,
    CreateRelationship,
    CreateRole,
    DeleteColumn,
    DeleteNamedExpression,
    DeleteTable,
    HierarchyLevel,
    MarkDateTable,
    ParameterField,
    RefreshData,
    RenameColumn,
    RenameTable,
    RoleFilter,
    SetCalculationItem,
    SetColumnProperties,
    SetModelOptions,
    SetNamedExpression,
    SetQueryParameter,
    SetTableQuery,
    UpdateHierarchy,
    UpdateRelationship,
    UpdateRole,
)
from ai_powerbi_analyst.studio.report_edit_ops import CreateMeasure, RenameMeasure

SERVER = "localhost:51234"
NUMBER_META = ' meta [IsParameterQuery=true, Type="Number", IsParameterQueryRequired=true]'
PBI_TOOL = Path(os.environ.get("APPDATA", "")) / "uv" / "tools" / "pbi-cli-tool"
PBI_PYTHON = PBI_TOOL / "Scripts" / "python.exe"


def fresh() -> tuple[Model, Server]:
    model = Model.from_state(agents_state())
    return model, Server(model)


def normalized(value: Any) -> Any:
    """A model dump with collections in a fixed order: a restored object comes back at the end
    of its collection, which is not a difference Desktop shows."""
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items()}
    if isinstance(value, list):
        return sorted((normalized(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    return value


def measure(model: Model, name: str) -> Any:
    return next(m for t in model.Tables for m in t.Measures if m.Name == name)


# --- the request: operations into runner changes --------------------------------------------------


def plan(*operations: Any, state: dict[str, Any] | None = None) -> desktop_model.ModelPlan:
    return plan_changes(list(operations), snapshot_of(Model.from_state(state or agents_state())))


def test_a_calculated_column_becomes_one_change_with_a_probe() -> None:
    made = plan(
        CreateCalculatedColumn(
            table="factsales",
            name="Margin",
            expression="FactSales[SalesAmount] - FactSales[TotalCost]",
            format_string="#,0.00",
            data_type="decimal_number",
            summarize_by="sum",
            hidden=False,
        )
    )
    assert made.changes == [
        {
            "id": "c1",
            "kind": "calculatedColumn",
            "action": "create",
            "path": {"table": "FactSales", "name": "Margin"},
            "props": {
                "expression": "FactSales[SalesAmount] - FactSales[TotalCost]",
                "formatString": "#,0.00",
                "dataType": "Double",
                "summarizeBy": "Sum",
                "isHidden": False,
                # Desktop's column tools write it with a summarization (spec review #8).
                "annotations": {"SummarizationSetBy": "User"},
            },
        }
    ]
    assert made.probes == [
        {
            "id": "p1",
            "label": "The check of FactSales[Margin]",
            "query": "EVALUATE TOPN(1, SELECTCOLUMNS('FactSales', \"value\", 'FactSales'[Margin]))",
        }
    ]
    assert (made.refresh, made.on_refresh_failure, made.fixups) == ([], "rollback", [])


def test_column_properties_use_the_models_own_names_and_toms_values() -> None:
    made = plan(
        SetColumnProperties(
            table="date",
            name="month",
            sort_by="month number",
            data_category="web_url",
            summarize_by="none",
            folder="Calendar",
            key=False,
        )
    )
    assert made.changes[0]["path"] == {"table": "Date", "name": "Month"}
    assert made.changes[0]["props"] == {
        "sortByColumn": "Month Number",
        "dataCategory": "WebUrl",
        "summarizeBy": "None",
        "displayFolder": "Calendar",
        "isKey": False,
        "annotations": {"SummarizationSetBy": "User"},
    }
    cleared = plan(
        SetColumnProperties(table="Date", name="Month", sort_by="", data_category="uncategorized")
    )
    assert cleared.changes[0]["props"] == {"sortByColumn": "", "dataCategory": ""}


def test_a_column_rename_rewrites_every_formula_that_names_it_in_the_same_save() -> None:
    state = agents_state()
    fact = state["tables"][0]
    fact["columns"].append(
        {
            "name": "Margin",
            "type": "calculated",
            "dataType": "Double",
            "expression": "[SalesAmount] - [TotalCost]",
        }
    )
    state["roles"] = [
        {
            "name": "Big",
            "tablePermissions": [
                {"name": "FactSales", "filterExpression": "FactSales[SalesAmount] > 10"}
            ],
        }
    ]
    state["tables"].append(
        {
            "name": "Time Calc",
            "columns": [
                {"name": "Name", "type": "data", "dataType": "String", "sourceColumn": "Name"}
            ],
            "measures": [],
            "hierarchies": [],
            "partitions": [{"name": "Time Calc", "source": "CalculationGroup"}],
            "calculationGroup": {
                "precedence": 1,
                "items": [
                    {
                        "name": "Plus",
                        "expression": "SELECTEDMEASURE() + SUM('FactSales'[SalesAmount])",
                        "ordinal": 0,
                    }
                ],
            },
        }
    )
    made = plan(
        RenameColumn(table="FactSales", name="SalesAmount", new_name="Sales Amount"), state=state
    )
    assert [(c["kind"], c["action"], c["path"]) for c in made.changes] == [
        ("column", "rename", {"table": "FactSales", "name": "SalesAmount"}),
        ("measure", "update", {"table": "# Measures", "name": "Employees Avg MTD Sales"}),
        ("column", "update", {"table": "FactSales", "name": "Margin"}),
        ("role", "update", {"name": "Big"}),
        ("calculationItem", "update", {"group": "Time Calc", "name": "Plus"}),
    ]
    assert made.changes[0]["newName"] == "Sales Amount"
    assert made.changes[1]["props"] == {"expression": "SUM(FactSales[Sales Amount])"}
    # A bare [SalesAmount] in a calculated column of the same table is the column.
    assert made.changes[2]["props"] == {"expression": "[Sales Amount] - [TotalCost]"}
    assert made.changes[3]["props"] == {
        "tablePermissions": {"FactSales": "FactSales[Sales Amount] > 10"}
    }
    assert made.changes[4]["props"] == {
        "expression": "SELECTEDMEASURE() + SUM('FactSales'[Sales Amount])"
    }
    assert made.fixups == [
        "the measure [Employees Avg MTD Sales]",
        "the calculated column FactSales[Margin]",
        "the filters of the role 'Big'",
        "the calculation item 'Plus' of 'Time Calc'",
    ]


def test_a_table_rename_rewrites_formulas_and_follows_the_tables_own_measures() -> None:
    state = agents_state()
    state["tables"][5]["measures"].append({"name": "Stores", "expression": "COUNTROWS(DimStore)"})
    made = plan(
        RenameTable(name="DimChannel", new_name="Channel"),
        RenameTable(name="DimStore", new_name="Store List"),
        state=state,
    )
    assert [(c["kind"], c["action"], c["path"], c.get("props")) for c in made.changes] == [
        ("table", "rename", {"name": "DimChannel"}, None),
        (
            "measure",
            "update",
            {"table": "# Measures", "name": "Rank"},
            {"expression": "RANKX(ALL('Channel'), 1)"},
        ),
        ("table", "rename", {"name": "DimStore"}, None),
        (
            "measure",
            "update",
            {"table": "# Measures", "name": "Stores"},
            {"expression": "COUNTROWS('Store List')"},
        ),
    ]


def test_deleting_a_table_first_removes_the_role_filters_on_it() -> None:
    state = agents_state()
    state["roles"] = [
        {
            "name": "R",
            "tablePermissions": [
                {"name": "DimEmployee", "filterExpression": "TRUE()"},
                {"name": "DimStore", "filterExpression": '[StoreType] = "Store"'},
            ],
        }
    ]
    made = plan(DeleteTable(name="dimemployee"), state=state)
    assert [(c["kind"], c["action"], c["path"], c.get("props")) for c in made.changes] == [
        ("role", "update", {"name": "R"}, {"tablePermissions": {"DimEmployee": None}}),
        ("table", "delete", {"name": "DimEmployee"}, None),
    ]
    assert made.warnings == [
        "The role 'R' loses its filter on 'DimEmployee'; its other filters stay."
    ]


def test_deleting_the_table_that_holds_a_roles_only_filter_is_refused() -> None:
    """Bug review #7: the role would silently see every row."""
    state = agents_state()
    state["roles"] = [
        {"name": "R", "tablePermissions": [{"name": "DimEmployee", "filterExpression": "TRUE()"}]}
    ]
    with pytest.raises(ModelChangeFailed, match="'DimEmployee' holds the only filter of the role"):
        plan(DeleteTable(name="dimemployee"), state=state)


def test_relationship_changes_map_cardinality_and_direction_to_tom() -> None:
    made = plan(
        CreateRelationship(
            from_table="DimStore",
            from_column="StoreKey",
            to_table="DimEmployee",
            to_column="EmployeeKey",
            cardinality="one_to_one",
            cross_filter="both",
            active=False,
        ),
        UpdateRelationship(
            from_table="DimStore",
            from_column="StoreKey",
            to_table="FactSales",
            to_column="StoreKey",
            cross_filter="both",
        ),
    )
    assert made.changes[0]["props"] == {
        "fromCardinality": "One",
        "toCardinality": "One",
        "crossFilteringBehavior": "BothDirections",
        "securityFilteringBehavior": "OneDirection",
        "isActive": False,
        "relyOnReferentialIntegrity": False,
    }
    assert made.changes[1]["path"] == {
        "fromTable": "DimStore",
        "fromColumn": "StoreKey",
        "toTable": "FactSales",
        "toColumn": "StoreKey",
    }
    assert made.changes[1]["props"] == {"crossFilteringBehavior": "BothDirections"}
    both = plan(
        UpdateRelationship(
            from_table="FactSales",
            from_column="StoreKey",
            to_table="DimStore",
            to_column="StoreKey",
            cross_filter="both",
            security_filter="both",
        ),
        UpdateRelationship(
            from_table="FactSales",
            from_column="channelKey",
            to_table="DimChannel",
            to_column="ChannelKey",
            cross_filter="single",
        ),
    )
    assert both.changes[0]["props"] == {
        "crossFilteringBehavior": "BothDirections",
        "securityFilteringBehavior": "BothDirections",
    }
    # Cross filtering leaves an unrequested security setting unchanged.
    assert both.changes[1]["props"] == {
        "crossFilteringBehavior": "OneDirection",
    }


def test_a_second_active_path_is_refused_against_the_live_model() -> None:
    state = agents_state()
    state["tables"][4]["columns"].append({"name": "StoreKey", "type": "data", "dataType": "Int64"})
    state["relationships"].append(
        {
            "name": "r4",
            "fromTable": "DimEmployee",
            "fromColumn": "StoreKey",
            "toTable": "DimStore",
            "toColumn": "StoreKey",
        }
    )
    with pytest.raises(
        ModelChangeFailed,
        match=r"'FactSales' and 'DimEmployee' are already connected through active relationships "
        r"\(FactSales - DimStore - DimEmployee\).*unambiguous active path",
    ):
        plan(
            CreateRelationship(
                from_table="FactSales",
                from_column="SalesKey",
                to_table="DimEmployee",
                to_column="EmployeeKey",
            ),
            state=state,
        )


def test_hierarchies_field_parameters_and_calculation_groups() -> None:
    made = plan(
        CreateHierarchy(
            table="Date",
            name="Calendar",
            levels=[HierarchyLevel(column="year"), HierarchyLevel(column="Month", name="M")],
        ),
        UpdateHierarchy(
            table="Date", name="Calendar", levels=[HierarchyLevel(column="Month")], new_name="Cal"
        ),
        CreateFieldParameter(
            name="Metric",
            fields=[
                ParameterField(table="# Measures", name="rank", label='The "rank"'),
                ParameterField(table="DimStore", name="StoreName"),
            ],
        ),
        CreateCalculationGroup(
            name="Time Calc",
            items=[
                CalculationItemSpec(name="Current", expression="SELECTEDMEASURE()"),
                CalculationItemSpec(
                    name="Share",
                    expression="SELECTEDMEASURE() / 2",
                    format_string_expression='"0.0%"',
                ),
            ],
            precedence=3,
        ),
        SetCalculationItem(group="Time Calc", name="Current", expression="SELECTEDMEASURE() * 1"),
        SetCalculationItem(group="Time Calc", name="PY", expression="SELECTEDMEASURE()", ordinal=5),
    )
    kinds = [(c["kind"], c["action"]) for c in made.changes]
    assert kinds == [
        ("hierarchy", "create"),
        ("hierarchy", "update"),
        ("hierarchy", "rename"),
        ("fieldParameter", "create"),
        ("model", "update"),  # a calculation group needs implicit measures discouraged
        ("calculationGroup", "create"),
        ("calculationItem", "update"),
        ("calculationItem", "create"),
    ]
    assert made.changes[0]["props"] == {
        "levels": [{"name": "Year", "column": "Year"}, {"name": "M", "column": "Month"}]
    }
    assert made.changes[3]["props"] == {
        "expression": (
            "{\n"
            '    ("The ""rank""", NAMEOF(\'# Measures\'[Rank]), 0),\n'
            "    (\"StoreName\", NAMEOF('DimStore'[StoreName]), 1)\n"
            "}"
        ),
        "columns": {"label": "Metric", "fields": "Metric Fields", "order": "Metric Order"},
    }
    assert made.changes[4]["props"] == {"discourageImplicitMeasures": True}
    assert made.changes[5]["props"]["items"] == [
        {"name": "Current", "expression": "SELECTEDMEASURE()"},
        {
            "name": "Share",
            "expression": "SELECTEDMEASURE() / 2",
            "formatStringExpression": '"0.0%"',
        },
    ]
    assert made.changes[7]["props"] == {"expression": "SELECTEDMEASURE()", "ordinal": 5}


def test_roles_queries_parameters_refresh_and_settings() -> None:
    state = agents_state()
    state["expressions"] = [
        {
            "name": "Region",
            "kind": "M",
            "expression": "5" + NUMBER_META,
        }
    ]
    made = plan(
        CreateRole(
            name="Stores",
            filters=[RoleFilter(table="dimstore", expression='[StoreType] = "Store"')],
        ),
        UpdateRole(
            name="Stores", filters=[RoleFilter(table="DimStore", expression="")], new_name="All"
        ),
        SetTableQuery(
            table="FactSales", expression="let Source = 1 in Source", if_refresh_fails="keep"
        ),
        SetNamedExpression(name="Folder", expression='"F:\\Data"'),
        SetQueryParameter(name="region", value=7),
        RefreshData(),
        SetModelOptions(discourage_implicit_measures=True),
        state=state,
    )
    assert [
        (c["kind"], c["action"], c["path"], c.get("props"), c.get("newName")) for c in made.changes
    ] == [
        (
            "role",
            "create",
            {"name": "Stores"},
            {"tablePermissions": {"DimStore": '[StoreType] = "Store"'}},
            None,
        ),
        ("role", "update", {"name": "Stores"}, {"tablePermissions": {"DimStore": None}}, None),
        ("role", "rename", {"name": "Stores"}, None, "All"),
        (
            "partition",
            "update",
            {"table": "FactSales"},
            {"mExpression": "let Source = 1 in Source"},
            None,
        ),
        ("expression", "create", {"name": "Folder"}, {"expression": '"F:\\Data"'}, None),
        (
            "expression",
            "update",
            {"name": "Region"},
            {"expression": "7" + NUMBER_META},
            None,
        ),
        ("model", "update", {}, {"discourageImplicitMeasures": True}, None),
    ]
    assert made.refresh == [{"table": "FactSales"}, {"model": True}]
    assert made.on_refresh_failure == "keep" and made.asks_on_refresh_failure is False
    # The role ends with no filter, so there is nothing to view it by.
    assert made.role_views == [] and made.warnings == []


def test_auto_date_time_and_culture_are_never_written() -> None:
    """Spec review #2: refused with Desktop's own steps, on the runner path too."""
    with pytest.raises(ModelChangeFailed, match="Current File > Data Load"):
        plan(SetModelOptions(auto_date_time=False))
    with pytest.raises(ModelChangeFailed, match="model's language"):
        plan(SetModelOptions(culture="en-GB"))


def test_a_query_parameter_keeps_its_type_list_and_flag_on_the_live_model() -> None:
    """Bug review #4 against the live model's M."""
    state = agents_state()
    meta = (
        ' meta [IsParameterQuery=true, List={"A", "B"}, Type="Text", '
        "IsParameterQueryRequired=false]"
    )
    state["expressions"] = [
        {
            "name": "RangeStart",
            "kind": "M",
            "expression": "#datetime(2024, 1, 1, 0, 0, 0) meta [IsParameterQuery=true, "
            'Type="DateTime", IsParameterQueryRequired=true]',
        },
        {"name": "Pick", "kind": "M", "expression": '"A"' + meta},
    ]
    made = plan(
        SetQueryParameter(name="RangeStart", value="2025-02-03"),
        SetQueryParameter(name="Pick", value="B"),
        state=state,
    )
    assert [c["props"]["expression"] for c in made.changes] == [
        '#datetime(2025, 2, 3, 0, 0, 0) meta [IsParameterQuery=true, Type="DateTime", '
        "IsParameterQueryRequired=true]",
        '"B"' + meta,
    ]
    with pytest.raises(ModelChangeFailed, match="takes one of its listed values"):
        plan(SetQueryParameter(name="Pick", value="C"), state=state)


def test_a_new_query_joins_desktops_query_order() -> None:
    state = agents_state()
    state["annotations"].append({"name": "PBI_QueryOrder", "value": '["FactSales","Old"]'})
    state["expressions"] = [{"name": "Old", "kind": "M", "expression": "1"}]
    made = plan(
        SetNamedExpression(name="Folder", expression='"F:\\Data"'),
        SetQueryParameter(name="Top", value=5),
        DeleteNamedExpression(name="Old"),
        state=state,
    )
    assert made.changes[-1] == {
        "id": f"c{len(made.changes)}",
        "kind": "model",
        "action": "update",
        "path": {},
        "props": {"annotations": {"PBI_QueryOrder": '["FactSales", "Folder", "Top"]'}},
    }
    assert plan(SetNamedExpression(name="X", expression="1")).changes[-1]["kind"] == "expression"


def test_refresh_targets_follow_a_later_rename() -> None:
    """Bug review #6: a refresh planned before a rename named the old table."""
    made = plan(
        SetTableQuery(table="FactSales", expression="let a = 1 in a", if_refresh_fails="keep"),
        RenameTable(name="FactSales", new_name="Sales"),
    )
    assert made.refresh == [{"table": "Sales"}]


def test_rename_fixups_cover_format_strings_detail_rows_kpis_and_measure_renames() -> None:
    """Bug review #5 and spec review #9."""
    model = Model.from_state(agents_state())
    rank = measure(model, "Rank")
    rank.FormatStringDefinition = FormatStringDefinition()
    rank.FormatStringDefinition.Expression = 'IF(ISFILTERED(DimChannel), "0", "0.0")'
    rank.DetailRowsDefinition = DetailRowsDefinition()
    rank.DetailRowsDefinition.Expression = 'SELECTCOLUMNS(DimChannel, "C", [ChannelName])'
    rank.KPI = KPI()
    rank.KPI.TargetExpression = "COUNTROWS(DimChannel)"
    rank.KPI.StatusExpression = "[dummy]"
    made = plan_changes(
        [
            RenameTable(name="DimChannel", new_name="Channel"),
            RenameMeasure(name="dummy", new_name="Blank"),
        ],
        snapshot_of(model),
    )
    fix = next(c for c in made.changes if c["kind"] == "measure" and c["action"] == "update")
    assert fix["props"]["expression"] == "RANKX(ALL('Channel'), 1)"
    assert fix["props"]["formatStringExpression"] == 'IF(ISFILTERED(\'Channel\'), "0", "0.0")'
    assert fix["props"]["detailRowsExpression"] == "SELECTCOLUMNS('Channel', \"C\", [ChannelName])"
    assert fix["props"]["kpi"]["targetExpression"] == "COUNTROWS('Channel')"
    status = [c for c in made.changes if c["kind"] == "measure" and c["path"]["name"] == "Rank"][-1]
    assert status["props"]["kpi"]["statusExpression"] == "[Blank]"
    assert "the measure [Rank]" in made.fixups


def test_a_role_is_viewed_as_the_role_after_the_change() -> None:
    """Spec review #3/#9: a second connection with Roles=<role> counts what the role sees."""
    model, server = fresh()
    kept = apply_model_operations(
        [
            CreateRole(
                name="Half", filters=[RoleFilter(table="DimStore", expression="[StoreKey] > 1")]
            )
        ],
        server=SERVER,
        runner=server,
    )
    assert [p.get("role") for p in server.calls[-1]["probes"] if p.get("informational")] == [
        None,
        "Half",
    ]
    assert kept.warnings == []
    model, server = fresh()
    noop = apply_model_operations(
        [CreateRole(name="Nothing", filters=[RoleFilter(table="DimStore", expression="FALSE()")])],
        server=SERVER,
        runner=server,
    )
    assert noop.warnings == [
        "Viewed as the role 'Nothing', 'DimStore' shows none of its rows: check the role's filter."
    ]


def test_the_refresh_policy_comes_from_the_new_queries() -> None:
    ask = plan(SetTableQuery(table="FactSales", expression="let a = 1 in a"))
    assert (ask.on_refresh_failure, ask.asks_on_refresh_failure) == ("rollback", True)
    plain = plan(RefreshData(table="DimStore"))
    assert (plain.refresh, plain.on_refresh_failure, plain.asks_on_refresh_failure) == (
        [{"table": "DimStore"}],
        "rollback",
        False,
    )


def test_a_name_the_live_model_does_not_have_is_refused_before_anything_is_sent() -> None:
    model, server = fresh()
    with pytest.raises(ModelChangeFailed, match="has no column 'Nope'"):
        apply_model_operations(
            [DeleteColumn(table="FactSales", name="Nope")], server=SERVER, runner=server
        )
    assert [c["action"] for c in server.calls] == ["snapshot"]


# --- the transaction, end to end over the fake engine ---------------------------------------------


def test_a_batch_is_applied_in_one_save_with_probes_and_before_images() -> None:
    model, server = fresh()
    saves = model.saves
    result = apply_model_operations(
        [
            CreateCalculatedColumn(
                table="FactSales",
                name="Margin",
                expression="FactSales[SalesAmount] - FactSales[TotalCost]",
            ),
            CreateMeasure(
                table="# Measures", name="Total Margin", expression="SUM(FactSales[Margin])"
            ),
            MarkDateTable(table="Date", column="Date"),
        ],
        server=SERVER,
        runner=server,
    )
    assert model.saves == saves + 1  # one save for the whole batch
    assert result.done == ["create calculatedColumn", "create measure", "update dateTable"]
    # The undo of a create deletes only the object it created (bug review #3).
    assert result.results[0]["undo"][0]["expect"] == {
        "type": "Calculated",
        "expression": "FactSales[SalesAmount] - FactSales[TotalCost]",
    }
    assert result.results[1]["undo"][0]["expect"] == {"expression": "SUM(FactSales[Margin])"}
    undos = [
        [{k: v for k, v in u.items() if k != "expect"} for u in r["undo"]] for r in result.results
    ]
    assert undos == [
        [{"kind": "column", "action": "delete", "path": {"table": "FactSales", "name": "Margin"}}],
        [
            {
                "kind": "measure",
                "action": "delete",
                "path": {"table": "# Measures", "name": "Total Margin"},
            }
        ],
        [
            {
                "kind": "dateTable",
                "action": "update",
                "path": {"name": "Date"},
                "props": {"column": None, "dataCategory": "", "keys": []},
            }
        ],
    ]
    assert [p["ok"] for p in result.probes] == [True, True]
    date = model.Tables.Find("Date")
    assert (date.DataCategory, date.Columns.Find("Date").IsKey) == ("Time", True)
    assert measure(model, "Total Margin").Expression == "SUM(FactSales[Margin])"
    assert result.snapshot_before["tables"][0]["name"] == "FactSales"


def test_every_before_image_puts_the_model_back() -> None:
    model, server = fresh()
    original = normalized(model.dump())
    result = apply_model_operations(
        [
            CreateCalculatedColumn(
                table="FactSales",
                name="Margin",
                expression="FactSales[SalesAmount] - FactSales[TotalCost]",
            ),
            SetColumnProperties(table="Date", name="Month", sort_by="Month Number", hidden=True),
            RenameColumn(table="FactSales", name="SalesAmount", new_name="Sales Amount"),
            RenameTable(name="DimChannel", new_name="Channel"),
            CreateCalculatedTable(name="Years", expression="DISTINCT('Date'[Year])"),
            CreateHierarchy(
                table="Date",
                name="Calendar",
                levels=[HierarchyLevel(column="Year"), HierarchyLevel(column="Month")],
            ),
            CreateRelationship(
                from_table="DimStore",
                from_column="StoreKey",
                to_table="DimEmployee",
                to_column="EmployeeKey",
                active=False,
            ),
            UpdateRelationship(
                from_table="FactSales",
                from_column="StoreKey",
                to_table="DimStore",
                to_column="StoreKey",
                cross_filter="both",
            ),
            CreateRole(
                name="Stores",
                filters=[RoleFilter(table="DimStore", expression='[StoreType] = "Store"')],
            ),
            SetNamedExpression(name="Folder", expression='"F:\\Data"'),
            SetModelOptions(discourage_implicit_measures=True),
            DeleteColumn(table="DimEmployee", name="FirstName"),
            DeleteTable(name="DimEmployee"),
        ],
        server=SERVER,
        runner=server,
    )
    assert normalized(model.dump()) != original
    undo = [step for record in reversed(result.results) for step in record["undo"]]
    request = {"version": 1, "action": "apply", "server": SERVER, "changes": undo, "tolerant": True}
    assert server(request, 60).result is not None
    assert normalized(model.dump()) == original


def test_a_rename_without_its_fixups_breaks_a_measure_and_the_run_is_undone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model, server = fresh()
    original = normalized(model.dump())
    monkeypatch.setattr(desktop_model._Planner, "_rewrite", lambda self, rename: None)
    with pytest.raises(ModelChangeFailed) as caught:
        apply_model_operations(
            [RenameColumn(table="FactSales", name="SalesAmount", new_name="Sales Amount")],
            server=SERVER,
            runner=server,
        )
    assert "measure # Measures[Employees Avg MTD Sales]" in str(caught.value)
    assert "cannot be found" in str(caught.value)
    assert (caught.value.stage, caught.value.rolled_back) == ("validate", True)
    assert normalized(model.dump()) == original  # compensated in a second save


def test_objects_that_were_broken_before_the_batch_are_not_blamed_on_it() -> None:
    state = agents_state()
    state["tables"][5]["measures"].append({"name": "Old", "expression": "SUM(Gone[Column])"})
    model = Model.from_state(state)
    server = Server(model)
    result = apply_model_operations(
        [CreateCalculatedTable(name="Years", expression="DISTINCT('Date'[Year])")],
        server=SERVER,
        runner=server,
    )
    assert result.done == ["create calculatedTable"]
    assert model.Tables.Find("Years") is not None


def test_a_save_the_engine_refuses_changes_nothing() -> None:
    model, server = fresh()
    original = normalized(model.dump())
    model.fail_saves = ["Ambiguous paths between 'FactSales' and 'DimStore'"]
    with pytest.raises(
        ModelChangeFailed, match="Power BI rejected the change: Ambiguous paths"
    ) as caught:
        apply_model_operations(
            [CreateCalculatedTable(name="Years", expression="DISTINCT('Date'[Year])")],
            server=SERVER,
            runner=server,
        )
    assert (caught.value.stage, caught.value.rolled_back) == ("save", True)
    assert normalized(model.dump()) == original


def test_a_failing_probe_undoes_the_run() -> None:
    model = Model.from_state(agents_state())
    server = Server(model, fail_queries=("'Years'",))
    original = normalized(model.dump())
    with pytest.raises(ModelChangeFailed, match="The check of the table 'Years': Query failed"):
        apply_model_operations(
            [CreateCalculatedTable(name="Years", expression="DISTINCT('Date'[Year])")],
            server=SERVER,
            runner=server,
        )
    assert normalized(model.dump()) == original


def test_a_refresh_that_fails_undoes_the_new_query_unless_it_is_kept() -> None:
    model, server = fresh()
    original = normalized(model.dump())
    query = 'let Source = Csv.Document(File.Contents("UNREACHABLE")) in Source'
    with pytest.raises(ModelRefreshFailed) as caught:
        apply_model_operations(
            [SetTableQuery(table="FactSales", expression=query)], server=SERVER, runner=server
        )
    assert "could not be reached" in caught.value.reason
    assert str(caught.value).startswith(
        "Power BI could not reload the data (The data source of 'FactSales'"
    )
    assert normalized(model.dump()) == original
    kept = apply_model_operations(
        [SetTableQuery(table="FactSales", expression=query, if_refresh_fails="keep")],
        server=SERVER,
        runner=server,
    )
    assert kept.kept_without_refresh is True and kept.refreshed is False
    assert "could not be reached" in (kept.refresh_error or "")
    assert any(
        "The new query is saved and takes effect at the next successful refresh" in w
        for w in kept.warnings
    )
    assert model.Tables.Find("FactSales").Partitions.Find("FactSales").Source.Expression == query


def test_a_runner_that_never_answers_is_compensated_from_what_it_printed() -> None:
    model, server = fresh()
    original = normalized(model.dump())
    server.hang_on_saved = True  # the changes reached the model, then the process died
    with pytest.raises(ModelOutcomeUnknown) as caught:
        apply_model_operations(
            [
                CreateCalculatedTable(name="Years", expression="DISTINCT('Date'[Year])"),
                SetColumnProperties(table="Date", name="Month", hidden=True),
            ],
            server=SERVER,
            runner=server,
        )
    assert caught.value.rolled_back is True
    assert str(caught.value).startswith("Power BI Desktop's model did not answer in time")
    undo = next(c for c in reversed(server.calls) if c.get("tolerant"))
    assert [(c["kind"], c["action"]) for c in undo["changes"]] == [
        ("column", "update"),
        ("table", "delete"),
    ]
    assert server.calls[-1]["action"] == "snapshot"  # rolled_back is what the model shows
    assert normalized(model.dump()) == original


def test_a_runner_that_dies_before_saving_leaves_nothing_to_undo() -> None:
    model, server = fresh()
    original = normalized(model.dump())
    server.hang_after = 1
    with pytest.raises(ModelOutcomeUnknown) as caught:
        apply_model_operations(
            [CreateCalculatedTable(name="Years", expression="DISTINCT('Date'[Year])")],
            server=SERVER,
            runner=server,
        )
    assert caught.value.rolled_back is True
    assert not any(c.get("tolerant") for c in server.calls)
    assert normalized(model.dump()) == original


def test_no_replay_after_a_run_that_never_saved_so_the_original_survives() -> None:
    """Bug review #3: rename [Rank] away, create a new [Rank], the runner dies before its save.
    Replaying the printed undo would have deleted the ORIGINAL [Rank]."""
    model, server = fresh()
    server.hang_after = 2
    with pytest.raises(ModelOutcomeUnknown) as caught:
        apply_model_operations(
            [
                RenameMeasure(name="Rank", new_name="Rank (old)"),
                CreateMeasure(table="# Measures", name="Rank", expression="1"),
                CreateCalculatedTable(name="Years", expression="DISTINCT('Date'[Year])"),
            ],
            server=SERVER,
            runner=server,
        )
    assert caught.value.rolled_back is True
    assert measure(model, "Rank").Expression == "RANKX(ALL('DimChannel'), 1)"
    # Even a replay of those steps leaves the original alone: a delete checks what it deletes.
    undo = [
        {
            "kind": "measure",
            "action": "delete",
            "path": {"table": "# Measures", "name": "Rank"},
            "expect": {"expression": "1"},
        },
    ]
    server(
        {"version": 1, "action": "apply", "server": SERVER, "changes": undo, "tolerant": True}, 60
    )
    assert measure(model, "Rank").Expression == "RANKX(ALL('DimChannel'), 1)"


def test_a_crash_is_told_apart_from_a_timeout() -> None:
    crashed = RunnerAnswer([], None, finished=True, exit_code=1, stderr="Traceback ...")
    assert crashed.why_no_result() == "Studio's model tool stopped unexpectedly (exit 1)"
    late = RunnerAnswer([], None, finished=False)
    assert late.why_no_result() == "Power BI Desktop's model did not answer in time"
    with pytest.raises(ModelChangeFailed, match=r"stopped unexpectedly \(exit 1\)$"):
        desktop_model.snapshot(lambda request, timeout: crashed, SERVER)


def test_measure_operations_can_go_through_the_runner_too() -> None:
    model, server = fresh()
    apply_model_operations(
        [
            RenameMeasure(name="rank", new_name="Agent Rank"),
            CreateCalculatedTable(name="Years", expression="DISTINCT('Date'[Year])"),
        ],
        server=SERVER,
        runner=server,
    )
    assert measure(model, "Agent Rank").Expression == "RANKX(ALL('DimChannel'), 1)"


def test_a_field_parameter_is_built_the_way_desktop_builds_one() -> None:
    model, server = fresh()
    apply_model_operations(
        [
            CreateFieldParameter(
                name="Metric",
                fields=[
                    ParameterField(table="# Measures", name="Rank"),
                    ParameterField(table="DimStore", name="StoreName"),
                ],
            )
        ],
        server=SERVER,
        runner=server,
    )
    table = model.Tables.Find("Metric")
    columns = {c.Name: c for c in table.Columns}
    assert [(c.Name, c.SourceColumn, c.IsHidden, c.DataType) for c in table.Columns] == [
        ("Metric", "[Value1]", False, "String"),
        ("Metric Fields", "[Value2]", True, "String"),
        ("Metric Order", "[Value3]", True, "Int64"),
    ]
    assert columns["Metric"].SortByColumn is columns["Metric Order"]
    assert columns["Metric Fields"].SortByColumn is columns["Metric Order"]
    grouping = list(columns["Metric"].RelatedColumnDetails.GroupByColumns)
    assert [g.GroupingColumn for g in grouping] == [columns["Metric Fields"]]
    assert [(p.Name, p.Value) for p in columns["Metric Fields"].ExtendedProperties] == [
        ("ParameterMetadata", '{"version":3,"kind":2}')
    ]
    snap = snapshot_of(model)
    assert next(t for t in snap["tables"] if t["name"] == "Metric")["fieldParameter"] is True


def test_a_calculation_group_is_a_table_with_its_items_sort_column_and_source() -> None:
    model, server = fresh()
    apply_model_operations(
        [
            CreateCalculationGroup(
                name="Time Calc",
                items=[
                    CalculationItemSpec(name="Current", expression="SELECTEDMEASURE()"),
                    CalculationItemSpec(
                        name="YTD", expression="SELECTEDMEASURE()", format_string_expression='"0.0"'
                    ),
                ],
                precedence=7,
            )
        ],
        server=SERVER,
        runner=server,
    )
    table = model.Tables.Find("Time Calc")
    assert model.DiscourageImplicitMeasures is True
    assert table.CalculationGroup.Precedence == 7
    items = list(table.CalculationGroup.CalculationItems)
    assert [
        (
            i.Name,
            i.Ordinal,
            i.FormatStringDefinition.Expression if i.FormatStringDefinition else None,
        )
        for i in items
    ] == [
        ("Current", 0, None),
        ("YTD", 1, '"0.0"'),
    ]
    assert [(c.Name, c.SourceColumn, c.IsHidden) for c in table.Columns] == [
        ("Name", "Name", False),
        ("Ordinal", "Ordinal", True),
    ]
    assert table.Columns.Find("Name").SortByColumn is table.Columns.Find("Ordinal")
    assert [p.SourceType for p in table.Partitions] == ["CalculationGroup"]


# --- the runner script's own guards --------------------------------------------------------------


def test_the_runner_refuses_requests_it_does_not_understand_without_loading_anything() -> None:
    run = load_runner().run
    assert run({"version": 2})["error"] == "Unknown protocol"
    assert run({"version": 1, "server": "example.com:80"})["error"] == "Not a local model"
    assert run({"version": 1, "server": "localhost:abc"})["error"] == "Not a local model"


def test_a_change_of_an_unknown_kind_is_undone_locally() -> None:
    model = Model.from_state(agents_state())
    runner = load_runner().Runner(__import__("fake_tom").TOM, model, None, lambda _e: None)
    result = runner.apply(
        {"changes": [{"kind": "perspective", "action": "create", "path": {"name": "P"}}]}
    )
    assert (result["ok"], result["stage"], result["rolled_back"]) == (False, "apply", True)
    assert result["error"] == "Studio does not know how to create a perspective"


def _runner_over(model: Model, rows: Any = None) -> Any:
    import fake_tom

    return load_runner().Runner(fake_tom.TOM, model, rows, lambda _e: None)


def test_a_rename_that_only_changes_letter_case_works_like_in_tom() -> None:
    """Bug review #9: TOM's Find ignores case, so the "already exists" checks found the object
    itself. The fake's Find ignores case now, as TOM's does."""
    model = Model.from_state(agents_state())
    assert model.Tables.Find("factsales") is model.Tables.Find("FactSales")
    runner = _runner_over(model)
    changes: list[dict[str, Any]] = [
        {"kind": "table", "action": "rename", "path": {"name": "DimStore"}, "newName": "Dimstore"},
        {
            "kind": "column",
            "action": "rename",
            "path": {"table": "Dimstore", "name": "StoreName"},
            "newName": "storename",
        },
        {
            "kind": "measure",
            "action": "rename",
            "path": {"table": "# Measures", "name": "Rank"},
            "newName": "RANK",
        },
    ]
    for change in changes:
        runner.apply_change(change, tolerant=False)
    assert model.Tables.Find("Dimstore").Name == "Dimstore"
    assert model.Tables.Find("Dimstore").Columns.Find("storename").Name == "storename"
    assert measure(model, "RANK").Name == "RANK"
    taken = load_runner().Exists
    with pytest.raises(taken):
        runner.apply_change(
            {"kind": "table", "action": "rename", "path": {"name": "Date"}, "newName": "factsales"},
            tolerant=False,
        )
    with pytest.raises(taken):
        runner.apply_change(
            {
                "kind": "measure",
                "action": "rename",
                "path": {"table": "# Measures", "name": "dummy"},
                "newName": "rank",
            },
            tolerant=False,
        )


def test_a_roles_members_are_never_read_printed_or_restored() -> None:
    """Spec review #6: a deleted role's TMSL carried its members' names to stdout and back."""
    import fake_tom

    state = agents_state()
    state["roles"] = [
        {
            "name": "R",
            "tablePermissions": [{"name": "DimStore", "filterExpression": "TRUE()"}],
            "members": [{"memberName": "someone@example.com"}],
        }
    ]
    model = Model.from_state(state)
    assert len(model.Roles.Find("R").Members) == 1
    runner = _runner_over(model)
    described = runner.snapshot()["roles"][0]
    assert "members" not in described and "someone" not in json.dumps(described)
    undo, before = runner.apply_change(
        {"kind": "role", "action": "delete", "path": {"name": "R"}}, tolerant=False
    )
    assert "someone" not in json.dumps(undo) and "someone" not in json.dumps(before)
    runner.apply_change(undo[0], tolerant=False)
    assert len(model.Roles.Find("R").Members) == 0
    assert model.Roles.Find("R").TablePermissions.Find("DimStore") is not None
    assert fake_tom.to_tmsl(model.Roles.Find("R")).get("members") is None


def test_unreadable_error_states_after_the_save_are_a_warning_not_a_pass() -> None:
    """Spec review #4: checked_states stayed true when the views failed after the save."""
    import fake_tom

    model = Model.from_state(agents_state())
    engine = fake_tom.engine(model)
    saved = {"done": False}

    def rows(query: str) -> list[dict[str, Any]]:
        if saved["done"] and "TMSCHEMA" in query:
            raise RuntimeError("view unavailable")
        return engine(query)

    runner = load_runner().Runner(
        fake_tom.TOM,
        model,
        rows,
        lambda event: saved.update(done=saved["done"] or event.get("event") == "saved"),
    )
    result = runner.apply(
        {
            "changes": [
                {
                    "kind": "measure",
                    "action": "create",
                    "path": {"table": "# Measures", "name": "One"},
                    "props": {"expression": "1"},
                }
            ]
        }
    )
    assert result["ok"] is True and result["checked_states"] is False
    assert result["warnings"] == [
        "Studio could not read which model objects have errors after the change, so only its own "
        "checks confirmed it"
    ]


def test_a_query_as_a_role_uses_a_second_connection_with_the_role() -> None:
    runner_module = load_runner()
    assert runner_module.role_connection_text("Provider=MSOLAP;Data Source=x", 'My "R"') == (
        'Provider=MSOLAP;Data Source=x;Roles="My ""R"""'
    )
    with pytest.raises(runner_module.ChangeError):
        runner_module.role_connection_text("Provider=MSOLAP", "A,B")
    state = agents_state()
    state["roles"] = [
        {"name": "Half", "tablePermissions": [{"name": "DimStore", "filterExpression": "[x] > 1"}]}
    ]
    server = Server(Model.from_state(state))
    count = {"n": "EVALUATE ROW(\"Rows\", COUNTROWS('DimStore'))"}
    everyone = desktop_model.query(server, SERVER, count)
    viewed = desktop_model.query(server, SERVER, count, role="Half")
    assert everyone["n"]["rows"] == [{"[Rows]": 10}] and viewed["n"]["rows"] == [{"[Rows]": 5}]
    with pytest.raises(ModelChangeFailed, match="has no role 'Nope'"):
        desktop_model.query(server, SERVER, count, role="Nope")


def test_the_runner_has_no_way_to_end_a_process() -> None:
    forbidden = re.compile(
        r"\.kill\(|\.terminate\(|Stop-Process|TerminateProcess|taskkill|os\.kill"
    )
    for path in (RUNNER_PATH, Path(desktop_model.__file__ or "")):
        assert not forbidden.search(path.read_text(encoding="utf-8")), path


# --- the process protocol ------------------------------------------------------------------------


def test_events_are_read_line_by_line_and_a_cut_line_is_ignored() -> None:
    answer = parse_events(
        'noise\n{"event": "connected"}\n'
        '{"event": "applied", "id": "c1", "undo": []}\n'
        '{"event": "res'
    )
    assert answer.result is None
    assert [e["event"] for e in answer.events] == ["connected", "applied"]
    done = parse_events('{"event": "saved"}\n{"event": "result", "ok": true}\n')
    assert done.result == {"event": "result", "ok": True}


def test_the_runner_process_is_the_pinned_python_on_studios_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(
            argv, 0, stdout='{"event": "result", "ok": true}\n', stderr=""
        )

    monkeypatch.setattr(desktop_model.subprocess, "run", run)
    environment = desktop_model.RunnerEnvironment(tmp_path / "python.exe", tmp_path / "runner.py")
    answer = desktop_model.subprocess_runner(environment)({"version": 1}, 30)
    assert answer.result == {"event": "result", "ok": True}
    call = calls[0]
    # -I ignores PYTHONIOENCODING: UTF-8 mode is asked for on the command line (bug review #1).
    assert call["argv"] == [
        str(tmp_path / "python.exe"),
        "-I",
        "-X",
        "utf8",
        str(tmp_path / "runner.py"),
    ]
    assert (answer.finished, answer.exit_code) == (True, 0)
    assert json.loads(call["input"]) == {"version": 1}
    assert call["timeout"] == 30 and call["check"] is False and "shell" not in call
    assert call["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_a_timeout_keeps_what_the_runner_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(argv: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(
            argv, 5, output=b'{"event": "applied", "id": "c1", "undo": [{"kind": "table"}]}\n'
        )

    monkeypatch.setattr(desktop_model.subprocess, "run", run)
    environment = desktop_model.RunnerEnvironment(tmp_path / "python.exe", tmp_path / "runner.py")
    answer = desktop_model.subprocess_runner(environment)({}, 5)
    assert answer.result is None and answer.events[0]["undo"] == [{"kind": "table"}]
    assert answer.finished is False
    assert answer.why_no_result() == "Power BI Desktop's model did not answer in time"


# A runner process over the fake engine: the real script's main() in a real child process, with
# only its connection replaced (run() answers from fake_tom). Written by the tests below.
HARNESS = """
import sys
sys.path[:0] = [{unit!r}, {src!r}]
import fake_tom

module = fake_tom.load_runner()
state = fake_tom.agents_state()
state["tables"][5]["measures"].append(
    {{"name": "Co\\u00fbt \\u20ac \\u2265 \\u22ee", "expression": "SUM(FactSales[SalesAmount])"}}
)
if {crash!r}:
    raise RuntimeError("the runner broke")


def run(request, emit=module._write):
    return fake_tom.Server(fake_tom.Model.from_state(state))(request, 60).result


module.run = run
sys.exit(module.main())
"""


def _harness(tmp_path: Path, *, crash: bool = False) -> desktop_model.RunnerEnvironment:
    unit = Path(__file__).resolve().parent
    script = tmp_path / "harness.py"
    script.write_text(
        HARNESS.format(unit=str(unit), src=str(unit.parents[1] / "src"), crash=crash),
        encoding="utf-8",
        newline="\n",
    )
    return desktop_model.RunnerEnvironment(Path(sys.executable), script)


def test_non_ascii_names_cross_the_real_process_boundary_intact(tmp_path: Path) -> None:
    """Bug review #1 / live run 1: under -I the pipes used the ANSI code page; a name like '⋮'
    crashed the runner and 'é' or '€' came back as U+FFFD. Runs a real child process."""
    runner = desktop_model.subprocess_runner(_harness(tmp_path))
    found = desktop_model.snapshot(runner, SERVER)
    names = [m["name"] for t in found["tables"] for m in t["measures"]]
    assert "Coût € ≥ ⋮" in names
    answers = desktop_model.query(runner, SERVER, {"qé": 'EVALUATE ROW("é", 1)'})
    assert answers["qé"]["ok"] is True


def test_a_crashed_runner_says_so_with_its_exit_code(tmp_path: Path) -> None:
    runner = desktop_model.subprocess_runner(_harness(tmp_path, crash=True))
    answer = runner({"version": 1, "action": "snapshot", "server": SERVER}, 60)
    assert (answer.result, answer.finished, answer.exit_code) == (None, True, 1)
    assert "the runner broke" in answer.stderr  # kept for the local log only
    with pytest.raises(ModelChangeFailed) as caught:
        desktop_model.snapshot(runner, SERVER)
    assert str(caught.value) == "Studio's model tool stopped unexpectedly (exit 1)"
    assert "Traceback" not in str(caught.value)


# --- the pinned environment ----------------------------------------------------------------------


def fake_tool(root: Path, *, version: str = "3.11.1") -> tuple[Path, dict[str, str]]:
    """A uv tool folder like pbi-cli-tool's: its python, pbi_cli's loader and libraries."""
    python = root / "pbi-cli-tool" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    site = root / "pbi-cli-tool" / "Lib" / "site-packages"
    pinned: dict[str, str] = {}
    for relative, body in (
        ("core/dotnet_loader.py", b"loader"),
        ("dlls/Microsoft.AnalysisServices.Tabular.dll", b"tom"),
    ):
        path = site / "pbi_cli" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        pinned[relative] = desktop_model.hashlib.sha256(body).hexdigest()
    meta = site / f"pbi_cli_tool-{version}.dist-info" / "METADATA"
    meta.parent.mkdir(parents=True)
    meta.write_text(f"Name: pbi-cli-tool\nVersion: {version}\n", encoding="utf-8")
    return python, pinned


def test_the_environment_is_found_through_the_uv_launcher_and_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "elsewhere"))
    python, pinned = fake_tool(tmp_path / "tools")
    launcher = tmp_path / "bin" / "pbi.exe"
    launcher.parent.mkdir()
    launcher.write_bytes(b"MZ....UV_PYTHON_PATH" + str(python).encode("utf-8") + b"PK\x03\x04")
    script = tmp_path / "studio_tom_runner.py"
    script.write_text("#", encoding="utf-8")
    found = runner_environment(str(launcher), script=script, pinned=pinned)
    assert found == desktop_model.RunnerEnvironment(python, script)


def test_a_changed_library_or_version_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "tools"))
    python, pinned = fake_tool(tmp_path / "tools")
    script = tmp_path / "runner.py"
    script.write_text("#", encoding="utf-8")
    assert runner_environment("", script=script, pinned=pinned).python == python
    changed = {**pinned, "core/dotnet_loader.py": "0" * 64}
    with pytest.raises(ModelRunnerUnavailable, match=r"libraries changed .*core/dotnet_loader\.py"):
        runner_environment("", script=script, pinned=changed)
    with pytest.raises(
        ModelRunnerUnavailable, match="version 3.11.1; Studio was checked with 9.9.9"
    ):
        runner_environment("", script=script, pinned=pinned, version="9.9.9")
    with pytest.raises(ModelRunnerUnavailable, match="missing from this installation"):
        runner_environment("", script=tmp_path / "gone.py", pinned=pinned)
    missing = {**pinned, "dlls/Nope.dll": "0" * 64}
    with pytest.raises(ModelRunnerUnavailable, match=r"incomplete \(dlls/Nope\.dll is missing\)"):
        runner_environment("", script=script, pinned=missing)


def test_no_python_anywhere_is_said_plainly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    script = tmp_path / "runner.py"
    script.write_text("#", encoding="utf-8")
    with pytest.raises(ModelRunnerUnavailable, match="own Python"):
        runner_environment(str(tmp_path / "pbi.exe"), script=script)


# --- the Desktop controller ----------------------------------------------------------------------


def controller(
    runner: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[DesktopController, DesktopInstance]:
    c = DesktopController(["node", "bridge.js"], "pbi", "PBIDesktop.exe", model_runner=runner)
    monkeypatch.setattr(c, "_engine_port", lambda instance: "51234")
    return c, DesktopInstance(4242, 1000.0, Path("Report.pbix"))


def test_the_controller_runs_the_batch_on_its_own_instances_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model, server = fresh()
    c, instance = controller(server, monkeypatch)
    result = c.apply_model_operations(
        instance, [CreateCalculatedTable(name="Years", expression="DISTINCT('Date'[Year])")]
    )
    assert result.done == ["create calculatedTable"]
    assert {call["server"] for call in server.calls} == {"localhost:51234"}
    assert c.model_snapshot(instance)["tables"][-1]["name"] == "Years"
    assert c.model_runner_problem() is None


def test_the_controller_turns_failures_into_desktop_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    model, server = fresh()
    c, instance = controller(server, monkeypatch)
    query = 'let Source = Csv.Document(File.Contents("UNREACHABLE")) in Source'
    with pytest.raises(DesktopRefreshFailed) as refused:
        c.apply_model_operations(instance, [SetTableQuery(table="FactSales", expression=query)])
    assert "could not be reached" in refused.value.reason

    def unsafe(request: dict[str, Any], timeout: float) -> RunnerAnswer:
        if request["action"] == "snapshot":
            return server(request, timeout)
        return RunnerAnswer(
            [],
            {
                "event": "result",
                "ok": False,
                "stage": "validate",
                "error": "x",
                "rollback_errors": ["y"],
            },
        )

    c2, _ = controller(unsafe, monkeypatch)
    with pytest.raises(
        DesktopSessionError,
        match="do not save the report. Close the report in Power BI Desktop and choose Don't save",
    ):
        c2.apply_model_operations(instance, [CreateCalculatedTable(name="Years", expression="1")])

    def silent(request: dict[str, Any], timeout: float) -> RunnerAnswer:
        if request["action"] == "snapshot":
            return server(request, timeout)
        return RunnerAnswer(finished=False)

    c3, _ = controller(silent, monkeypatch)
    with pytest.raises(
        DesktopSessionError,
        match="^Power BI Desktop's model did not answer in time; the change did not reach",
    ) as unknown:
        c3.apply_model_operations(instance, [CreateCalculatedTable(name="Years", expression="1")])
    assert isinstance(unknown.value, desktop_session._OutcomeUnknown)


def test_the_controller_reads_queries_and_undoes_applied_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model, server = fresh()
    original = normalized(model.dump())
    c, instance = controller(server, monkeypatch)
    first = c.apply_model_operations(
        instance, [CreateCalculatedTable(name="Years", expression="DISTINCT('Date'[Year])")]
    )
    second = c.apply_model_operations(
        instance, [RenameColumn(table="FactSales", name="SalesAmount", new_name="Amount")]
    )
    answers = c.model_query(
        instance,
        {
            "years": "EVALUATE TOPN(1, 'Years')",
            "old": 'EVALUATE ROW("v", SUM(FactSales[SalesAmount]))',
            "no": "DROP TABLE x",
        },
    )
    assert answers == {
        "years": {"ok": True, "rows": [{"value": 1}]},
        "old": {
            "ok": False,
            "error": "The column 'SalesAmount' in table 'FactSales' cannot be found",
        },
        "no": {"ok": False, "error": "Only EVALUATE queries are read"},
    }
    c.undo_model_operations(instance, [first, second])
    assert normalized(model.dump()) == original


def test_hiding_a_measure_is_refused_by_the_controller_too(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_powerbi_analyst.studio.report_edit_ops import UpdateMeasure

    model, server = fresh()
    c, instance = controller(server, monkeypatch)
    with pytest.raises(DesktopSessionError, match="cannot hide or show a measure"):
        c.apply_model_operations(instance, [UpdateMeasure(name="Rank", hidden=True)])
    assert server.calls == []


def test_readiness_reports_the_runner_separately_from_measure_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    pbi = tmp_path / "pbi.exe"
    pbi.write_bytes(b"")
    c = DesktopController(
        ["node"], str(pbi), str(tmp_path / "PBIDesktop.exe"), receipts_dir=tmp_path
    )
    ready = c.readiness()
    assert ready.runner_ok is False and ready.can_edit_model_objects is False
    assert "own Python" in (ready.runner_reason or "")
    assert "own Python" not in " ".join(ready.reasons)  # measure edits are not blamed on it


# --- the live probe's batches, against the fake engine -------------------------------------------


def test_the_live_probe_batches_apply_to_an_agents_like_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.util

    from ai_powerbi_analyst.studio.report_edit_model import saved_model_problems

    path = Path(__file__).resolve().parents[2] / "evals" / "imported_edit" / "model_probe.py"
    spec = importlib.util.spec_from_file_location("model_probe_under_test", path)
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "model_probe_under_test", probe)  # dataclasses look it up
    spec.loader.exec_module(probe)
    state = agents_state()
    state["tables"][0]["partitions"][0]["expression"] = (
        'let Source = Csv.Document(File.Contents("UNREACHABLE")) in Source'
    )
    model = Model.from_state(state)
    server = Server(model)

    class Controller:
        """The part of DesktopController the probe's steps use, over the fake engine."""

        def model_snapshot(self, instance: Any) -> dict[str, Any]:
            return desktop_model.snapshot(server, SERVER)

        def model_query(
            self, instance: Any, queries: dict[str, str], role: str | None = None
        ) -> dict[str, Any]:
            return desktop_model.query(server, SERVER, queries, role=role)

        def apply_model_operations(self, instance: Any, operations: list[Any]) -> Any:
            try:
                return apply_model_operations(operations, server=SERVER, runner=server)
            except ModelRefreshFailed as error:
                raise DesktopRefreshFailed(str(error), error.reason) from error

    runner = probe.Probe(Controller(), Path("."))  # type: ignore[arg-type]
    batches = probe.plan_batches(snapshot_of(model))
    names = [batch.name for batch in batches]
    assert len(names) == 20 and names[-1].startswith("table rename")
    assert {k for batch in batches for k in batch.covers} == set(OUTSIDE_SUPPORT) - {
        "delete_data_table"  # the Agents model has no table nothing uses
    }
    for batch in batches:
        # The fake engine answers COUNTROWS, not a field parameter's rows or a calculation item.
        if batch.name.startswith(("field parameter", "calculation group")):
            batch.checks = []
        step = runner.run(
            batch.name,
            lambda s, b=batch: probe.apply_batch(runner, None, b, s),
            covers=batch.covers,
        )
        assert step.status == "passed", (batch.name, step.observed, step.error)
    assert measure(model, "Rank").Expression == "RANKX(ALL('Studio Channel'), 1)"
    assert "Online" not in json.dumps(runner.steps[0].operations)
    role_step = next(s for s in runner.steps if s.name == "row-level security role")
    assert role_step.operations[0]["filters"][0]["expression"]["length"] == 25  # never the text
    everything = [op for operations in runner.batches for op in operations]
    assert saved_model_problems(everything, snapshot_of(model))[0] == []
    desktop_model.undo_applied(runner.applied, server=SERVER, runner=server)
    assert normalized(snapshot_of(model)) == normalized(snapshot_of(Model.from_state(state)))
    passed = [probe.Step(n, status="passed") for n in (probe.SAVE_STEP, probe.UNDO_STEP)]
    assert probe.verified_ops(runner.steps + passed) == sorted(
        set(OUTSIDE_SUPPORT) - {"delete_data_table"}
    )
    assert probe.verified_ops(runner.steps) == []


def test_the_live_probe_refuses_a_work_folder_inside_the_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "evals" / "imported_edit" / "model_probe.py"
    spec = importlib.util.spec_from_file_location("model_probe_refusal", path)
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "model_probe_refusal", probe)
    spec.loader.exec_module(probe)
    source = tmp_path / "a.pbix"
    source.write_bytes(b"x")
    monkeypatch.setattr(probe, "tools", lambda args: (["node"], "pbi", "desktop"))
    monkeypatch.setattr(
        probe,
        "DesktopController",
        lambda *a: type("C", (), {"model_runner_problem": lambda s: None})(),
    )
    inside = Path(__file__).resolve().parent / "probe-work"
    assert probe.main(["--pbix", str(source), "--work-dir", str(inside)]) == 2
    assert not inside.exists()
    assert probe.redacted({"expression": "let a = 1 in a", "note": r"F:\Work\x.csv failed"}) == {
        "expression": probe.digest("let a = 1 in a"),
        "note": "<path> failed",
    }


# --- the real libraries, offline (no Desktop, no server) -----------------------------------------


needs_pbi_cli = pytest.mark.skipif(not PBI_PYTHON.is_file(), reason="pbi-cli is not installed here")


@needs_pbi_cli
def test_the_installed_pbi_cli_matches_the_pinned_hashes() -> None:
    environment = runner_environment(str(PBI_TOOL / "Scripts" / "pbi.exe"))
    assert environment.python == PBI_PYTHON
    assert environment.script == RUNNER_PATH


@needs_pbi_cli
def test_every_change_kind_works_on_microsofts_own_object_model() -> None:
    """Loads the pinned TOM libraries under pbi-cli's Python and applies every change kind to an
    offline model (a Database object with no server), then its undo: the model must come back."""
    check = Path(__file__).with_name("tom_offline_check.py")
    completed = subprocess.run(  # noqa: S603 - pbi-cli's pinned Python on a test script
        [str(PBI_PYTHON), "-I", str(check), str(RUNNER_PATH)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    report = json.loads(completed.stdout)
    assert report["tom"] == "Microsoft.AnalysisServices.Tabular.Model"
    failures = {k: v for k, v in report["changes"].items() if v != "restored"}
    assert failures == {}
    assert len(report["changes"]) == 39  # incl. case-only renames and the new properties
    assert report["members"] == {
        "in_snapshot": False,
        "in_undo": False,
        "restored_role": True,
        "restored_members": 0,
    }
    assert report["expect"] == {"kept_another_object": True, "deleted_its_own": True}
    assert report["field_parameter_columns"] == [
        ["Metric", "[Value1]", "Metric Order", False],
        ["Metric Fields", "[Value2]", "Metric Order", True],
        ["Metric Order", "[Value3]", None, True],
    ]
