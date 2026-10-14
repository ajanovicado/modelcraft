"""The model health check (``model_health``): each rule on small synthetic models, how the report's
use of the model is read (the structure twin and PBIR shapes built here), that every fix parses and
applies as a model-family or measure operation, and the real Agents Performance model (read-only,
skipped when absent)."""

from __future__ import annotations

import json
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from ai_powerbi_analyst.studio import model_facts, pbix_extract, report_edit_model, report_edits
from ai_powerbi_analyst.studio import model_health as mh
from ai_powerbi_analyst.studio.model_health import ColumnStats, ColumnUse, ObjectRef, Usage
from ai_powerbi_analyst.studio.pbir_document import PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage
from ai_powerbi_analyst.studio.report_edit_context import ModelInfo

TWIN = Path(__file__).resolve().parents[2] / "fixtures" / "imported" / "structure-twin.pbix"
AGENTS = Path.home() / "Downloads" / "Agents Performance - Dashboard.pbix"
PAGE = "ReportSection8454c812b60b8bb83b99"

# --- builders -----------------------------------------------------------------------------------


def table(name: str, **kw: Any) -> dict[str, Any]:
    return {
        "name": name,
        "hidden": False,
        "private": False,
        "showAsVariationsOnly": False,
        "autoDateTable": None,
        "description": "d",
        "dataCategory": None,
        "isDateTable": False,
        "dateColumn": None,
        "source": "m",
        "calculated": False,
        "expression": None,
        "calculationGroup": False,
        **kw,
    }


def column(tbl: str, name: str, data_type: int = 6, **kw: Any) -> dict[str, Any]:
    return {
        "table": tbl,
        "name": name,
        "kind": "data",
        "dataType": data_type,
        "dateTimeType": "date" if data_type == 9 else None,
        "state": None,
        "expression": None,
        "formatString": None,
        "sortByColumn": None,
        "dataCategory": None,
        "summarizeBy": "none",
        "hidden": False,
        "key": False,
        "displayFolder": None,
        "description": "d",
        "sourceColumn": name,
        "error": None,
        **kw,
    }


def measure(name: str, expression: str, tbl: str = "# Measures", **kw: Any) -> dict[str, Any]:
    return {
        "table": tbl,
        "name": name,
        "expression": expression,
        "formatString": "#,0.00",
        "formatStringExpression": None,
        "displayFolder": None,
        "description": "d",
        "hidden": False,
        "dataType": 8,
        "state": None,
        "error": None,
        **kw,
    }


def rel(ft: str, fc: str, tt: str, tc: str, **kw: Any) -> dict[str, Any]:
    return {
        "name": f"{ft}-{tt}",
        "fromTable": ft,
        "fromColumn": fc,
        "toTable": tt,
        "toColumn": tc,
        "fromCardinality": "many",
        "toCardinality": "one",
        "cardinality": "M:1",
        "crossFilter": "single",
        "active": True,
        "relyOnReferentialIntegrity": False,
        "securityFilter": "single",
        "joinOnDate": "dateAndTime",
        **kw,
    }


def sales_facts(**changes: Any) -> dict[str, Any]:
    """A small star: Sales many-to-one Customer and Calendar (marked, calculated), and a measure
    table. Every column is used by something unless a test says otherwise."""
    facts: dict[str, Any] = {
        "version": 1,
        "model": {"culture": "en-US", "autoDateTime": False},
        "tables": [
            table("Sales"),
            table("Customer"),
            table(
                "Calendar",
                source="calculated",
                calculated=True,
                isDateTable=True,
                dateColumn="Date",
                expression="CALENDARAUTO()",
                dataCategory="Time",
            ),
            table("# Measures"),
        ],  # fmt: skip
        "columns": [
            column("Sales", "CustomerKey", hidden=True),
            column("Sales", "OrderDate", 9, hidden=True),
            column("Sales", "Amount", 10),
            column("Customer", "CustomerKey", hidden=True),
            column("Customer", "Name", 2),
            column("Calendar", "Date", 9, kind="calculatedTableColumn", key=True),
            column("Calendar", "Year", 6, kind="calculatedTableColumn"),
        ],
        "measures": [measure("Total Amount", "SUM(Sales[Amount])")],
        "relationships": [
            rel("Sales", "CustomerKey", "Customer", "CustomerKey"),
            rel("Sales", "OrderDate", "Calendar", "Date"),
        ],
        "hierarchies": [],
        "variations": [],
        "roles": [],
        "calculationGroups": [],
        "fieldParameters": [],
        "whatIfParameters": [],
        "partitions": [],
        "expressions": [],
    }
    facts.update(changes)
    return facts


def described(facts: dict[str, Any], text: str | None = "d") -> dict[str, Any]:
    """The facts with ``text`` as every object's description (the builders describe every
    object, so the description rule stays quiet unless a test clears them)."""
    for section in ("tables", "columns", "measures"):
        for item in facts[section]:
            item["description"] = text
    return facts


USED = Usage(
    columns={("customer", "name"): ColumnUse(fields=1)}, measures=frozenset({"total amount"})
)


def ids(health: mh.Health) -> list[str]:
    return [f.id for f in health.findings]


def only(health: mh.Health, rule: str) -> list[mh.Finding]:
    return [f for f in health.findings if f.rule == rule]


# --- a healthy model --------------------------------------------------------------------------


def test_a_clean_star_has_nothing_to_fix() -> None:
    health = mh.check_model(described(sales_facts()), usage=USED)
    assert health.known is True
    assert health.findings == []
    assert health.checked == {"tables": 4, "columns": 7, "measures": 1, "relationships": 2}
    assert health.summary == {
        "problems": 0,
        "suggestions": 0,
        "by_category": {
            "correctness": 0,
            "performance": 0,
            "usability": 0,
            "formatting": 0,
            "naming": 0,
        },
        "by_rule": {},
    }
    assert health.notes == [
        "Studio has no statistics of the data, so it did not check relationship keys for blanks "
        "and repeats or columns for too many different values."
    ]
    assert mh.describe(health).splitlines()[0] == (
        "Studio checked 4 tables, 7 columns, 1 measure and 2 relationships: nothing to fix."
    )


def test_unknown_facts_check_nothing() -> None:
    for facts in (None, {"version": 1}, {"version": 1, "tables": []}):
        health = mh.check_model(facts, usage=USED)
        assert (health.known, health.findings, health.checked) == (False, [], {})
        assert mh.describe(health) == (
            "Studio could not read this model's tables and columns, so it checked nothing."
        )


# --- relationships ----------------------------------------------------------------------------


def test_two_way_and_many_to_many_relationships() -> None:
    facts = described(sales_facts())
    facts["relationships"][0]["crossFilter"] = "both"
    facts["relationships"].append(
        rel("Sales", "Amount", "Customer", "Name", cardinality="M:M", toCardinality="many",
            active=False)
    )  # fmt: skip
    facts["relationships"].append(
        rel("Customer", "Name", "Calendar", "Year", cardinality="1:1", fromCardinality="one",
            crossFilter="both", active=False)
    )  # fmt: skip
    facts["measures"].append(
        measure("Via name", "CALCULATE([Total Amount], USERELATIONSHIP(Customer[Name], "
                "Calendar[Year]))")
    )  # fmt: skip
    health = mh.check_model(facts, usage=USED)
    two_way = only(health, "two_way_relationship")
    assert [f.as_dict() for f in two_way] == [
        {
            "id": "two_way_relationship:Sales[CustomerKey]>Customer[CustomerKey]",
            "rule": "two_way_relationship",
            "category": "correctness",
            "severity": "suggestion",
            "objects": [
                {
                    "kind": "relationship",
                    "table": "Sales",
                    "name": "CustomerKey",
                    "to_table": "Customer",
                    "to_column": "CustomerKey",
                }
            ],
            "message": "The relationship from 'Sales'[CustomerKey] to 'Customer'[CustomerKey] "
            "filters in both directions.",
            "why": "Filters that flow both ways can reach a table by more than one path, which "
            "makes some totals hard to predict and every visual slower.",
            "fix": [
                {
                    "op": "update_relationship",
                    "from_table": "Sales",
                    "from_column": "CustomerKey",
                    "to_table": "Customer",
                    "to_column": "CustomerKey",
                    "cross_filter": "single",
                }
            ],
            "fix_summary": "Make it filter in one direction (from 'Customer' to 'Sales'); check "
            "the visuals that relied on the other direction, their numbers change.",
            "fix_needs_desktop": True,
            "fix_confirm": True,
            "fix_outside_supported": False,
        }
    ]
    # A one-to-one relationship always filters both ways in Power BI: not a finding.
    many = only(health, "many_to_many_relationship")
    assert [(f.id, f.message, f.fix) for f in many] == [
        (
            "many_to_many_relationship:Sales[Amount]>Customer[Name]",
            "The relationship from 'Sales'[Amount] to 'Customer'[Name] is many-to-many.",
            None,
        )
    ]
    # The inactive M:M join is used by no formula; the 1:1 one is used by USERELATIONSHIP.
    inactive = only(health, "unused_inactive_relationship")
    assert [(f.id, f.fix) for f in inactive] == [
        (
            "unused_inactive_relationship:Sales[Amount]>Customer[Name]",
            [
                {
                    "op": "delete_relationship",
                    "from_table": "Sales",
                    "from_column": "Amount",
                    "to_table": "Customer",
                    "to_column": "Name",
                }
            ],
        )
    ]
    assert inactive[0].message == (
        "The inactive relationship from 'Sales'[Amount] to 'Customer'[Name] is not turned on by "
        "any formula."
    )


def test_a_report_measure_can_turn_on_an_inactive_relationship() -> None:
    facts = described(sales_facts())
    facts["relationships"][1]["active"] = False
    used = Usage(
        columns=USED.columns,
        measures=USED.measures,
        dax=("CALCULATE([Total Amount], USERELATIONSHIP('Sales'[OrderDate], 'Calendar'[Date]))",),
    )
    assert only(mh.check_model(facts, usage=used), "unused_inactive_relationship") == []
    assert len(only(mh.check_model(facts, usage=USED), "unused_inactive_relationship")) == 1
    # Without the report, a report measure could use it: no verdict.
    assert only(mh.check_model(facts, usage=None), "unused_inactive_relationship") == []


def test_relationship_keys_with_statistics() -> None:
    stats = {
        ("customer", "customerkey"): ColumnStats(rows=10, distinct=8, blanks=1),
        ("calendar", "date"): ColumnStats(rows=365, distinct=365, blanks=0),
    }
    health = mh.check_model(described(sales_facts()), usage=USED, stats=stats)
    assert [(f.id, f.severity, f.message) for f in health.findings] == [
        (
            "relationship_key_duplicates:Sales[CustomerKey]>Customer[CustomerKey]:to",
            "problem",
            "'Customer'[CustomerKey] repeats values (8 different values in 9 rows) but is the one "
            "side of its relationship with 'Sales'.",
        ),
        (
            "relationship_key_blanks:Sales[CustomerKey]>Customer[CustomerKey]:to",
            "suggestion",
            "'Customer'[CustomerKey] has 1 blank value on the one side of its relationship with "
            "'Sales'.",
        ),
    ]
    assert health.notes == []


# --- columns ------------------------------------------------------------------------------------


def test_floating_point_money_and_quantities() -> None:
    facts = described(sales_facts())
    facts["columns"] += [
        column("Sales", "UnitPrice", 8),
        column("Sales", "OrderQty", 8),
        column("Sales", "Margin Amount", 8),  # "margin" may be a percentage: left alone
        column("Sales", "Unused Cost", 8),  # unused: the unused-column rule speaks for it
        column("Sales", "Net Price", 8, kind="calculated", expression="Sales[UnitPrice] * 0.9"),
    ]
    facts["measures"].append(
        measure("Units", "SUM(Sales[OrderQty]) + SUM(Sales[Margin Amount]) + "
                "SUMX(Sales, Sales[Net Price])")
    )  # fmt: skip
    health = mh.check_model(facts, usage=USED)
    found = only(health, "floating_point_column")
    shown = [(f.id, [o.name for o in f.objects], f.message, f.fix, f.fix_summary) for f in found]
    assert shown == [
        (
            "floating_point_column:Sales",
            ["Net Price"],
            "1 column of 'Sales' holds money as decimal (floating point) numbers: Net Price.",
            [{"op": "set_column_properties", "table": "Sales", "name": "Net Price",
              "data_type": "fixed_decimal"}],
            "Change the calculated column's type: Net Price to Fixed decimal number.",
        ),
        (
            "floating_point_column:Sales:by-hand",
            ["UnitPrice", "OrderQty"],
            "2 columns of 'Sales' hold money or quantities as decimal (floating point) numbers: "
            "UnitPrice and OrderQty.",
            None,
            "In Power Query (Transform data), change the type of: UnitPrice to Fixed decimal "
            "number and OrderQty to Whole number.",
        ),
    ]  # fmt: skip
    assert [o.name for f in only(health, "unused_column") for o in f.objects] == ["Unused Cost"]


def test_visible_keys_foreign_and_surrogate() -> None:
    facts = described(sales_facts())
    for item in facts["columns"]:
        if item["name"] in ("CustomerKey", "OrderDate"):
            item["hidden"] = False
    health = mh.check_model(facts, usage=USED)
    found = only(health, "visible_key_column")
    assert [(f.id, [o.name for o in f.objects], f.message, f.fix_summary) for f in found] == [
        (
            "visible_key_column:Customer",
            ["CustomerKey"],
            "1 key column of 'Customer' is visible in the field list: CustomerKey.",
            "Hide it (visuals that already use it keep working).",
        ),
        (
            "visible_key_column:Sales",
            ["CustomerKey", "OrderDate"],
            "2 key columns of 'Sales' are visible in the field list: CustomerKey and OrderDate.",
            "Hide them (visuals that already use them keep working).",
        ),
    ]
    assert found[0].fix == [
        {"op": "set_column_properties", "table": "Customer", "name": "CustomerKey", "hidden": True}
    ]
    # Calendar[Date] is a date, not a surrogate key; a key the report shows is left visible.
    shown = Usage(
        columns={**USED.columns, ("customer", "customerkey"): ColumnUse(fields=1)},
        measures=USED.measures,
    )
    assert ids(mh.check_model(facts, usage=shown)) == ["visible_key_column:Sales"]
    # Without the report Studio cannot tell whether a one-side key is shown: foreign keys only.
    assert [f.id for f in only(mh.check_model(facts, usage=None), "visible_key_column")] == [
        "visible_key_column:Sales"
    ]


def test_unused_columns_visible_hidden_and_calculated() -> None:
    facts = described(sales_facts())
    facts["columns"] += [
        column("Customer", "City", 2),
        column("Customer", "Region", 2),
        column("Customer", "Legacy Code", 2, hidden=True),
        column("Customer", "Tag", 2, hidden=True, kind="calculated", expression='"x"'),
        column("Calendar", "Month", 2, kind="calculatedTableColumn"),  # date tables are left alone
    ]
    health = mh.check_model(facts, usage=USED)
    assert [(f.id, [o.name for o in f.objects], f.message, f.fix, f.fix_summary)
            for f in health.findings] == [
        (
            "unused_column:Customer",
            ["City", "Region"],
            "2 columns of 'Customer' are not used by any visual, filter, measure, relationship or "
            "hierarchy: City and Region.",
            [
                {"op": "set_column_properties", "table": "Customer", "name": "City",
                 "hidden": True},
                {"op": "set_column_properties", "table": "Customer", "name": "Region",
                 "hidden": True},
            ],
            "Hide them; to also make the file smaller, remove them in Power Query (Home > "
            "Transform data) instead.",
        ),
        (
            "unused_hidden_column:Customer",
            ["Legacy Code"],
            "1 hidden column of 'Customer' is not used by any visual, filter, measure, "
            "relationship or hierarchy: Legacy Code.",
            None,
            "Remove them in Power Query (Home > Transform data) so Power BI stops loading them.",
        ),
        (
            "unused_calculated_column:Customer",
            ["Tag"],
            "1 hidden calculated column of 'Customer' is not used by any visual, filter, measure, "
            "relationship or hierarchy: Tag.",
            [{"op": "delete_column", "table": "Customer", "name": "Tag"}],
            "Delete it.",
        ),
    ]  # fmt: skip


def test_what_counts_as_using_a_column() -> None:
    facts = described(sales_facts())
    facts["tables"] += [
        table("Copy", source="calculated", calculated=True, expression="Customer"),
        table("Pick", source="calculated", calculated=True, expression="VALUES(Product[Name])"),
        table("Product"),
    ]
    facts["columns"] += [
        column("Customer", "Segment", 2),  # a hierarchy level
        column("Customer", "Segment Order", 6),  # sorts Segment
        column("Customer", "Score", 6),  # a bare [Score] inside an iterator
        column("Customer", "Band", 2),  # a role filter
        column("Customer", "Visited", 2),  # an automatic date hierarchy... via the report
        column("Copy", "Name", 2, kind="calculatedTableColumn", sourceColumn="Customer[Name]"),
        column("Product", "Name", 2),  # a calculated table's DAX
        column("Pick", "Name", 2, kind="calculatedTableColumn", sourceColumn="[Name]"),
    ]
    for item in facts["columns"]:
        if item["name"] == "Segment":
            item["sortByColumn"] = "Segment Order"
    facts["hierarchies"] = [
        {"table": "Customer", "name": "Segments", "hidden": False, "levels": [
            {"name": "Segment", "column": "Segment", "ordinal": 0}]}
    ]  # fmt: skip
    facts["roles"] = [{"name": "Banded", "tables": [{"table": "Customer", "filter": "[Band] = 1"}]}]
    facts["measures"].append(measure("Scored", "SUMX(Customer, [Score])"))
    used = Usage(
        columns={
            **USED.columns,
            ("customer", "visited"): ColumnUse(date_hierarchy=1),
            ("copy", "name"): ColumnUse(fields=1),
            ("pick", "name"): ColumnUse(fields=1),
        },
        measures=frozenset({"total amount", "scored"}),
    )
    assert only(mh.check_model(facts, usage=used), "unused_column") == []


def test_summarized_keys_years_and_orders() -> None:
    facts = described(sales_facts())
    for item in facts["columns"]:
        if item["name"] == "Year":
            item["summarizeBy"] = "sum"
    facts["columns"] += [
        column("Customer", "Sort Order", 6, summarizeBy="default"),
        column("Customer", "Visits", 6, summarizeBy="sum"),  # a real count: adding up is right
    ]
    facts["measures"].append(measure("Picks", "MAX(Customer[Sort Order]) + SUM(Customer[Visits])"))
    health = mh.check_model(facts, usage=USED)
    assert [(f.id, f.message, f.fix) for f in only(health, "summarized_key_column")] == [
        (
            "summarized_key_column:Calendar",
            "1 number column of 'Calendar' adds up when dropped into a visual although it is a "
            "key, a year or a sort order: Year.",
            [{"op": "set_column_properties", "table": "Calendar", "name": "Year",
              "summarize_by": "none"}],
        ),
        (
            "summarized_key_column:Customer",
            "1 number column of 'Customer' adds up when dropped into a visual although it is a "
            "key, a year or a sort order: Sort Order.",
            [{"op": "set_column_properties", "table": "Customer", "name": "Sort Order",
              "summarize_by": "none"}],
        ),
    ]  # fmt: skip


def test_high_cardinality_needs_statistics() -> None:
    facts = described(sales_facts())
    facts["columns"] += [
        column("Sales", "Comment", 2),
        column("Sales", "Stamp", 9, dateTimeType="dateTime"),
        column("Sales", "Day", 9),
    ]
    facts["measures"].append(
        measure("Last", "MAX(Sales[Stamp]) & MAX(Sales[Comment]) & MAX(Sales[Day])")
    )
    stats = {
        ("sales", "comment"): ColumnStats(rows=200_000, distinct=150_000, blanks=0),
        ("sales", "stamp"): ColumnStats(rows=200_000, distinct=40_000, blanks=0),
        ("sales", "day"): ColumnStats(rows=200_000, distinct=40_000, blanks=0),
    }
    usage = Usage(columns=USED.columns, measures=frozenset({"total amount", "last"}))
    health = mh.check_model(facts, usage=usage, stats=stats)
    assert [(f.id, f.message) for f in health.findings] == [
        (
            "high_cardinality_column:'Sales'[Comment]",
            "'Sales'[Comment] holds 150,000 different texts in 200,000 rows.",
        ),
        (
            "high_cardinality_column:'Sales'[Stamp]",
            "'Sales'[Stamp] holds dates with times of day (40,000 different values).",
        ),
    ]
    assert only(mh.check_model(facts, usage=usage), "high_cardinality_column") == []


def test_calculated_column_only_added_up() -> None:
    facts = described(sales_facts())
    facts["columns"].append(
        column("Sales", "Margin", 10, kind="calculated", expression="Sales[Amount] * 0.2")
    )
    facts["measures"].append(measure("Total Margin", "SUM(Sales[Margin])"))
    usage = Usage(
        columns={**USED.columns, ("sales", "margin"): ColumnUse(aggregated=2)},
        measures=frozenset({"total amount", "total margin"}),
    )
    found = only(mh.check_model(facts, usage=usage), "calculated_column_as_measure")
    assert [(f.id, f.message, f.fix) for f in found] == [
        (
            "calculated_column_as_measure:'Sales'[Margin]",
            "The calculated column 'Sales'[Margin] is only ever added up (3 places).",
            None,
        )
    ]
    # Grouping or filtering by it (a slicer) is a real column use.
    sliced = Usage(
        columns={**usage.columns, ("sales", "margin"): ColumnUse(fields=1, aggregated=2)},
        measures=usage.measures,
    )
    assert only(mh.check_model(facts, usage=sliced), "calculated_column_as_measure") == []


# --- measures and DAX -----------------------------------------------------------------------------


def test_dax_patterns() -> None:
    facts = described(sales_facts())
    facts["measures"] += [
        measure("Ratio", "[Total Amount] / COUNTROWS(Sales)"),
        measure("Half", "[Total Amount] / 2 + [Total Amount] / -4"),
        measure("Safe", "DIVIDE([Total Amount], COUNTROWS(Sales))"),
        measure("Guarded", "IFERROR([Total Amount] * 2, 0)"),
        measure("Big", "CALCULATE([Total Amount], FILTER(Sales, Sales[Amount] > 100))"),
        measure(
            "Big kept",
            "CALCULATE([Total Amount], KEEPFILTERS(FILTER('Sales', Sales[Amount] IN {1, 2})))",
        ),  # fmt: skip
        measure("All", "CALCULATE([Total Amount], FILTER(ALL(Sales), Sales[Amount] > 100))"),
        measure("By measure", "CALCULATE([Total Amount], FILTER(Sales, [Total Amount] > 1))"),
        measure(
            "Related", 'CALCULATE([Total Amount], FILTER(Sales, RELATED(Customer[Name]) = "A"))'
        ),  # fmt: skip
        measure(
            "Variable",
            'VAR Sales = FILTER(Customer, Customer[Name] = "A") RETURN '
            'CALCULATE([Total Amount], FILTER(Sales, Customer[Name] = "A"))',
        ),  # fmt: skip
        measure("Broken", "SUM(Sales[Amount]"),
    ]
    usage = Usage(
        columns=USED.columns, measures=frozenset(m["name"].casefold() for m in facts["measures"])
    )
    health = mh.check_model(facts, usage=usage)
    assert [(f.id, [o.name for o in f.objects], f.message, f.fix, f.fix_summary)
            for f in health.findings] == [
        (
            "dax_division:# Measures",
            ["Ratio"],
            "1 measure in '# Measures' divides with / instead of DIVIDE: Ratio.",
            None,
            "Ask Studio to rewrite it with DIVIDE.",
        ),
        (
            "dax_iferror:# Measures",
            ["Guarded"],
            "1 measure in '# Measures' uses IFERROR or ISERROR: Guarded.",
            None,
            "Ask Studio to rewrite it without IFERROR or ISERROR.",
        ),
        (
            "dax_filter_table:# Measures",
            ["Big", "Big kept"],
            "2 measures in '# Measures' filter a whole table inside CALCULATE where a column "
            "filter would do: Big and Big kept.",
            None,
            "Ask Studio to rewrite them with column filters.",
        ),
    ]  # fmt: skip
    assert health.notes[-1] == (
        "Studio could not read the formula of 1 measure (Broken), so the formula checks skipped it."
    )


def test_time_intelligence_on_an_unmarked_table() -> None:
    facts = described(sales_facts())
    calendar = facts["tables"][2]
    calendar.update(isDateTable=False, dateColumn=None, dataCategory=None)
    facts["measures"].append(measure("YTD", "TOTALYTD([Total Amount], 'Calendar'[Date])"))
    usage = Usage(columns=USED.columns, measures=frozenset({"total amount", "ytd"}))
    found = only(mh.check_model(facts, usage=usage), "date_table_not_marked")
    # Joined on its date column: the time intelligence works, marking is still right.
    assert [(f.severity, f.message, f.fix) for f in found] == [
        (
            "suggestion",
            "Measures use time intelligence on 'Calendar'[Date], but 'Calendar' is not marked as "
            "a date table.",
            [{"op": "mark_date_table", "table": "Calendar", "column": "Date"}],
        )
    ]
    assert found[0].fix_outside_supported is True
    assert found[0].fix_summary == (
        "Mark 'Calendar' as a date table on [Date] (Power BI Desktop checks that it holds each "
        "date once). Marking a date table is outside the changes Microsoft supports for tools "
        "like Studio; Studio makes it only where it has tested it with your Power BI Desktop "
        "version."
    )
    # Joined on an integer key, time intelligence gives wrong totals: a problem.
    facts["columns"] += [column("Sales", "DateKey", 6, hidden=True),
                         column("Calendar", "DateKey", 6, kind="calculatedTableColumn",
                                hidden=True)]  # fmt: skip
    facts["relationships"][1].update(fromColumn="DateKey", toColumn="DateKey")
    found = only(mh.check_model(facts, usage=usage), "date_table_not_marked")
    assert [(f.severity, f.why) for f in found] == [
        (
            "problem",
            "'Calendar' joins the other tables on DateKey, not on a date, so time intelligence "
            "returns wrong totals until the table is marked.",
        )
    ]
    # Over a fact table's dates there is nothing to mark.
    facts["measures"][-1]["expression"] = "CALCULATE([Total Amount], DATESYTD(Sales[OrderDate]))"
    found = only(mh.check_model(facts, usage=usage), "date_table_not_marked")
    assert [(f.id, f.severity, f.fix) for f in found] == [
        ("date_table_not_marked:Sales", "problem", None)
    ]


def test_unused_measures_visible_and_hidden() -> None:
    facts = described(sales_facts())
    facts["measures"] += [
        measure("Spare", "[Total Amount] * 2"),
        measure("Helper", "1", hidden=True),
        measure("Chain", "[Chain] + [Base]", hidden=True),  # names itself: not a use
        measure("Base", "2", hidden=True),
    ]
    health = mh.check_model(facts, usage=USED)
    assert [(f.id, [o.name for o in f.objects], f.message, f.fix) for f in health.findings] == [
        (
            "unused_measure:# Measures",
            ["Spare"],
            "1 measure in '# Measures' is not used by any visual, filter or other measure in "
            "this report: Spare.",
            None,
        ),
        (
            "unused_hidden_measure:# Measures",
            ["Helper", "Chain"],
            "2 hidden measures in '# Measures' are not used by any visual, filter or other "
            "measure in this report: Helper and Chain.",
            [{"op": "delete_measure", "name": "Helper"}, {"op": "delete_measure", "name": "Chain"}],
        ),
    ]


def test_formats_folders_and_descriptions() -> None:
    facts = sales_facts()
    facts["measures"] = [
        measure(f"M{i}", "1", displayFolder=None if i < 2 else "Base",
                formatString=None if i in (0, 3) else "0")
        for i in range(16)
    ] + [
        measure("Count", "1", dataType=6, formatString=None),  # whole numbers read fine
        measure("Dynamic", "1", formatString=None, formatStringExpression="\"0\""),
        measure("Hidden", "1", formatString=None, hidden=True),
    ]  # fmt: skip
    described(facts, None)
    usage = Usage(
        columns={**USED.columns, ("sales", "amount"): ColumnUse(fields=1)},
        measures=frozenset(m["name"].casefold() for m in facts["measures"]),
    )
    health = mh.check_model(facts, usage=usage)
    assert [(f.id, f.message) for f in health.findings] == [
        (
            "measure_folders:# Measures",
            "4 of the 18 measures in '# Measures' are not in a display folder: M0, M1, Count and "
            "1 more.",
        ),
        (
            "missing_descriptions",
            "4 tables, 4 columns and 18 measures that people see in the field list have no "
            "description.",
        ),
        (
            "measure_no_format:# Measures",
            "2 measures in '# Measures' have no format string: M0 and M3.",
        ),
    ]
    folders = only(health, "measure_folders")[0]
    assert (folders.category, folders.fix) == ("usability", None)
    # 15 visible measures or fewer: no folders needed.
    facts["measures"] = facts["measures"][:15]
    assert only(mh.check_model(facts, usage=usage), "measure_folders") == []


# --- model settings, names, errors -------------------------------------------------------------


def test_auto_date_time() -> None:
    facts = described(sales_facts())
    facts["model"]["autoDateTime"] = True
    facts["tables"] += [
        table("LocalDateTable_1", hidden=True, showAsVariationsOnly=True, autoDateTable="local",
              source="calculated", calculated=True),
        table("DateTableTemplate_1", hidden=True, private=True, autoDateTable="template",
              source="calculated", calculated=True),
    ]  # fmt: skip
    facts["columns"].append(column("LocalDateTable_1", "Date", 9, kind="calculatedTableColumn"))
    facts["relationships"].append(rel("Sales", "OrderDate", "LocalDateTable_1", "Date"))
    usage = Usage(
        columns=USED.columns,
        measures=USED.measures,
        date_hierarchy_places=("line chart 'Sales by date' on page 'Overview'",),
    )
    health = mh.check_model(facts, usage=usage)
    # Auto date/time's own join and table are not reported by the other rules.
    assert [(f.id, f.message, f.fix, f.fix_summary) for f in health.findings] == [
        (
            "auto_date_time",
            "Auto date/time is on, so Power BI keeps 1 hidden date table, one for each date "
            "column.",
            None,
            "In Power BI Desktop, open File > Options and settings > Options > Current file > "
            "Data load and clear Auto date/time. First move the 1 visual that use an automatic "
            "date hierarchy (line chart 'Sales by date' on page 'Overview') to a date table's "
            "columns.",
        )
    ]
    assert health.checked["relationships"] == 2


def test_names_with_spaces_and_case_clashes() -> None:
    facts = described(sales_facts())
    facts["measures"] += [measure("Total ", "1"), measure("amount", "2")]
    facts["columns"].append(column("Customer", "Name\t2", 2, hidden=True))
    facts["columns"].append(column("Customer", "Name 2", 2, hidden=True))
    facts["measures"].append(measure("Use", "[Total ] + [amount] + MAX(Customer[Name\t2]) & "
                                     "MAX(Customer[Name 2])"))  # fmt: skip
    usage = Usage(
        columns=USED.columns,
        measures=frozenset({"total amount", "total ", "amount", "use"}),
    )
    health = mh.check_model(facts, usage=usage)
    # 'Name\t2' would become 'Name 2', which another column already is: no automatic rename.
    assert [(f.id, f.message, f.fix) for f in health.findings] == [
        (
            "name_whitespace:# Measures",
            "1 name in '# Measures' starts or ends with spaces or holds tabs or line breaks: "
            "'Total '.",
            [{"op": "rename_measure", "name": "Total ", "new_name": "Total"}],
        ),
        (
            "name_whitespace:Customer",
            "1 name in 'Customer' starts or ends with spaces or holds tabs or line breaks: "
            "'Name\\t2'.",
            None,
        ),
        (
            "name_case_clash:amount",
            "[amount] and 'Sales'[Amount] have the same name spelled differently (amount, Amount).",
            None,
        ),
    ]


def test_objects_in_error_never_quote_the_error() -> None:
    facts = described(sales_facts())
    facts["measures"][0].update(state="semanticError", error="The syntax for 'SUMM(' is wrong")
    health = mh.check_model(facts, usage=USED)
    assert [(f.id, f.severity, f.message) for f in health.findings] == [
        (
            "broken_object:[Total Amount]",
            "problem",
            "The measure [Total Amount] is in an error state in Power BI Desktop.",
        )
    ]
    assert "SUMM" not in json.dumps(health.as_dict())


# --- what is not checked, order, chunks -------------------------------------------------------


def test_usage_rules_need_the_report_and_every_formula() -> None:
    facts = described(sales_facts())
    facts["columns"].append(column("Customer", "City", 2))
    assert ids(mh.check_model(facts, usage=USED)) == ["unused_column:Customer"]
    unread = mh.check_model(facts, usage=None)
    assert ids(unread) == []
    assert unread.notes[0] == (
        "Studio did not read the report, so it did not look for columns and measures that "
        "nothing uses."
    )
    assert ids(mh.check_model(facts, usage=Usage.unknown())) == []
    # The stored facts (ImportedReport.model) leave measure DAX out: no verdict either.
    stored = model_facts.stored(facts)
    health = mh.check_model(stored, usage=USED)
    assert ids(health) == []
    assert health.notes[0] == (
        "Some formulas were not available in full, so Studio did not look for columns, "
        "measures and inactive relationships that nothing uses."
    )


def test_long_fixes_are_split_into_batches() -> None:
    facts = described(sales_facts())
    facts["columns"] += [column("Customer", f"C{i:02}", 2) for i in range(60)]
    found = only(mh.check_model(facts, usage=USED), "unused_column")
    assert [(f.id, len(f.objects), len(f.fix or [])) for f in found] == [
        ("unused_column:Customer:1", 50, 50),
        ("unused_column:Customer:2", 10, 10),
    ]
    assert found[0].message.endswith("C00, C01, C02 and 47 more.")


def test_findings_come_problems_first_then_by_category() -> None:
    facts = sales_facts()
    facts["columns"].append(column("Customer", "City", 2))
    facts["measures"][0].update(state="dependencyError")
    facts["measures"].append(measure("Plain", "1", formatString=None))
    described(facts, None)
    health = mh.check_model(facts, usage=USED)
    assert ids(health) == [
        "broken_object:[Total Amount]",
        "unused_column:Customer",
        "unused_measure:# Measures",
        "missing_descriptions",
        "measure_no_format:# Measures",
    ]
    assert health.summary["by_category"] == {
        "correctness": 1,
        "performance": 1,
        "usability": 2,
        "formatting": 1,
        "naming": 0,
    }
    assert mh.describe(health, limit=2).splitlines() == [
        "Studio checked 4 tables, 8 columns, 2 measures and 2 relationships: 1 problem and 4 "
        "suggestions.",
        "- Problem: The measure [Total Amount] is in an error state in Power BI Desktop.",
        "- Suggestion: 1 column of 'Customer' is not used by any visual, filter, measure, "
        "relationship or hierarchy: City.",
        "...and 3 more in the model health list.",
        "Studio has no statistics of the data, so it did not check relationship keys for blanks "
        "and repeats or columns for too many different values.",
    ]
    assert [f.id for f in mh.findings_for(health, [ObjectRef("column", "customer", "CITY")])] == [
        "unused_column:Customer",
        "missing_descriptions",
    ]


def test_messages_never_carry_formulas() -> None:
    facts = described(sales_facts())
    secret = "SECRETTOKEN"
    facts["measures"] += [
        measure("Leaky", f"IFERROR([Total Amount] / COUNTROWS(Sales), \"{secret}\")",
                formatString=None),
    ]  # fmt: skip
    facts["columns"].append(column("Sales", "Calc", 8, kind="calculated", expression=f'"{secret}"'))
    text = json.dumps(mh.check_model(facts, usage=USED).as_dict())
    assert secret not in text and "COUNTROWS" not in text


def test_chat_triggers() -> None:
    for text in (
        "Check the model please",
        "run a model health check",
        "any best practice issues?",
        "What’s wrong with the model?",
        "BPA",
    ):
        assert mh.is_health_request(text), text
    for text in ("make the bars blue", "why is Total Sales 5?", "rename the page", "bpaX"):
        assert not mh.is_health_request(text), text


# --- statistics -----------------------------------------------------------------------------------


def test_column_statistics_from_frames_and_parquet(tmp_path: Path) -> None:
    frame = pd.DataFrame(
        {"Key": [1, 2, 2, None], "Name": ["a", "", None, "b"], "Other": [1, 1, 1, 1]}
    )
    assert mh.stats_from_frames({"Customer": frame}, wanted={"customer": ["key", "NAME"]}) == {
        ("customer", "key"): ColumnStats(rows=4, distinct=2, blanks=1),
        ("customer", "name"): ColumnStats(rows=4, distinct=2, blanks=2),
    }
    assert ColumnStats(4, 2, 1).duplicates is True
    assert ColumnStats(4, 2, 2).duplicates is False
    path = tmp_path / "customer.parquet"
    frame.to_parquet(path)
    files = {"Customer": path, "Skipped": path}
    assert mh.stats_from_parquet(files, wanted={"Customer": ["Key"]}) == {
        ("customer", "key"): ColumnStats(rows=4, distinct=2, blanks=1)
    }
    assert mh.stats_wanted(sales_facts()) == {
        "Customer": ["CustomerKey", "Name"],
        "Calendar": ["Date"],
        "Sales": ["OrderDate"],
    }


# --- how the report uses the model ------------------------------------------------------------


def test_report_usage_of_the_structure_twin() -> None:
    usage = mh.report_usage(PbirDocument(PbixPackage(TWIN)))
    assert len(usage.measures) == 38
    assert {"rank", "employee name", "dummy", "rank filtering dynamics"} <= usage.measures
    assert dict(usage.columns) == {
        ("dimemployee", "employeekey"): ColumnUse(fields=1),
        ("top-bottom-n", "top-bottom-n"): ColumnUse(fields=2),
        ("actuals-vs-pctchange", "top-bottom-field"): ColumnUse(fields=3),
        ("clustered employees", "clustered employees"): ColumnUse(fields=16),
        ("dimstore", "storename"): ColumnUse(fields=1),
        ("dimchannel", "channelname"): ColumnUse(fields=2),
    }
    assert (usage.hierarchies, usage.levels, usage.date_hierarchy_places) == (
        frozenset(),
        frozenset(),
        (),
    )
    assert len(usage.dax) == 1 and "EVALUATE" in usage.dax[0]


def _field(kind: str, entity: str, prop: str, source: bool = False) -> dict[str, Any]:
    ref = {"Source": entity} if source else {"Entity": entity}
    return {kind: {"Expression": {"SourceRef": ref}, "Property": prop}}


def _pbix(path: Path, visual: dict[str, Any], extra: dict[str, bytes]) -> Path:
    base = "Report/definition/"
    members = {
        base + "report.json": json.dumps({}).encode(),
        base + "pages/pages.json": json.dumps({"pageOrder": ["p1"]}).encode(),
        base + "pages/p1/page.json": json.dumps({"name": "p1", "displayName": "Overview",
                                                 "width": 1280, "height": 720}).encode(),
        base + "pages/p1/visuals/v1/visual.json": json.dumps(visual).encode(),
        **extra,
    }  # fmt: skip
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def test_report_usage_reads_every_shape(tmp_path: Path) -> None:
    variation = {
        "HierarchyLevel": {
            "Expression": {
                "Hierarchy": {
                    "Expression": {
                        "PropertyVariationSource": {
                            "Expression": {"SourceRef": {"Entity": "Sales"}},
                            "Name": "Variation",
                            "Property": "OrderDate",
                        }
                    },
                    "Hierarchy": "Date Hierarchy",
                }
            },
            "Level": "Year",
        }
    }
    level = {
        "HierarchyLevel": {
            "Expression": {
                "Hierarchy": {
                    "Expression": {"SourceRef": {"Entity": "Customer"}},
                    "Hierarchy": "Geography",
                }
            },
            "Level": "Country",
        }
    }
    aggregated = {"Aggregation": {"Expression": _field("Column", "Sales", "Amount"), "Function": 0}}
    visual = {
        "name": "v1",
        "position": {"x": 0, "y": 0, "width": 100, "height": 100},
        "visual": {
            "visualType": "lineChart",
            "query": {
                "queryState": {
                    "Category": {"projections": [{"field": variation}, {"field": level}]},
                    "Y": {"projections": [{"field": aggregated}]},
                }
            },
        },
        "filterConfig": {
            "filters": [
                {
                    "name": "f1",
                    "field": _field("Column", "Customer", "Segment"),
                    "filter": {
                        "Version": 2,
                        "From": [{"Name": "c", "Entity": "Customer", "Type": 0}],
                        "Where": [{"Condition": {"Comparison": {
                            "ComparisonKind": 0,
                            "Left": _field("Column", "c", "Segment", source=True),
                            "Right": {"Literal": {"Value": "'A'"}}}}}],
                    },
                }
            ]
        },
    }  # fmt: skip
    bookmark = {
        "name": "b1",
        "displayName": "Big",
        "explorationState": {"sections": {"p1": {"filters": {"byExpr": [
            {"name": "x", "expression": _field("Measure", "# Measures", "Total Amount")}]}}}},
    }  # fmt: skip
    extensions = {"entities": [{"name": "# Measures", "measures": [
        {"name": "Report only", "expression": "SUM(Sales[Tax])"}]}]}  # fmt: skip
    path = _pbix(
        tmp_path / "shapes.pbix",
        visual,
        {
            "Report/definition/bookmarks/b1.bookmark.json": json.dumps(bookmark).encode(),
            "Report/definition/reportExtensions.json": json.dumps(extensions).encode(),
            "DAXQueries/Query%201.dax": "EVALUATE VALUES(Customer[City])".encode("utf-16-le"),
        },
    )
    usage = mh.report_usage(PbirDocument(PbixPackage(path)))
    assert dict(usage.columns) == {
        ("sales", "orderdate"): ColumnUse(date_hierarchy=1),
        ("sales", "amount"): ColumnUse(aggregated=1),
        ("customer", "segment"): ColumnUse(fields=2),
    }
    assert usage.measures == frozenset({"total amount"})
    assert usage.hierarchies == frozenset({("customer", "geography")})
    assert usage.levels == frozenset({("customer", "geography", "country")})
    assert len(usage.date_hierarchy_places) == 1
    assert "Overview" in usage.date_hierarchy_places[0]
    assert usage.dax == ("SUM(Sales[Tax])", "EVALUATE VALUES(Customer[City])")
    # The level's column counts as used through the model's hierarchy.
    facts = described(sales_facts())
    facts["columns"] += [column("Customer", "Country", 2), column("Customer", "Segment", 2),
                         column("Sales", "Tax", 10), column("Customer", "City", 2)]  # fmt: skip
    facts["hierarchies"] = [{"table": "Customer", "name": "Geography", "levels": [
        {"name": "Country", "column": "Country", "ordinal": 0}]}]  # fmt: skip
    health = mh.check_model(facts, usage=usage)
    # Country (a level), Segment (a filter), Tax (a report measure), City (a DAX query) are
    # used; this report shows no customer name.
    assert [o.label() for f in only(health, "unused_column") for o in f.objects] == [
        "'Customer'[Name]"
    ]


# --- every fix is a real operation ------------------------------------------------------------


def _messy_facts() -> dict[str, Any]:
    facts = sales_facts()
    facts["relationships"][0]["crossFilter"] = "both"
    facts["relationships"].append(rel("Sales", "Amount", "Customer", "Name", active=False))
    facts["tables"][2].update(isDateTable=False, dateColumn=None)
    facts["columns"] += [
        column("Customer", "City", 2),
        column("Customer", "Tag", 2, hidden=True, kind="calculated", expression='"x"'),
        column("Customer", "Sort Order", 6, summarizeBy="sum"),
        column("Sales", "Price", 8, kind="calculated", expression="Sales[Amount] * 1"),
    ]
    for item in facts["columns"]:
        if item["name"] == "CustomerKey":
            item["hidden"] = False
    facts["measures"] += [
        measure("YTD", "TOTALYTD([Total Amount], 'Calendar'[Date]) + MAX(Customer[Sort Order])"
                " + SUM(Sales[Price])"),
        measure("Old", "1", hidden=True),
        measure("Total ", "2"),
    ]  # fmt: skip
    return facts


def test_every_fix_parses_and_the_model_family_accepts_it() -> None:
    facts = _messy_facts()
    usage = Usage(columns=USED.columns, measures=frozenset({"total amount", "ytd", "total "}))
    health = mh.check_model(facts, usage=usage)
    with_fix = [f for f in health.findings if f.fix]
    assert sorted(f.rule for f in with_fix) == [
        "date_table_not_marked",
        "floating_point_column",
        "name_whitespace",
        "summarized_key_column",
        "two_way_relationship",
        "unused_calculated_column",
        "unused_column",
        "unused_column",
        "unused_hidden_measure",
        "unused_inactive_relationship",
        "visible_key_column",
        "visible_key_column",
    ]
    document = PbirDocument(PbixPackage(TWIN))
    info = ModelInfo.from_report(
        model_facts.schema_rows(facts) or [],
        [{"Name": m["name"], "TableName": m["table"], "Expression": m["expression"]}
         for m in facts["measures"]],
        {"columns": facts["columns"], "measures": facts["measures"]},
        calculated=model_facts.calculated(facts),
    )  # fmt: skip
    report_edit_model.attach_structure(info, facts)
    measure_ops = {"update_measure", "delete_measure", "rename_measure"}
    for finding in with_fix:
        assert finding.fix_needs_desktop and finding.fix_confirm
        assert finding.fix is not None and len(finding.fix) <= mh.MAX_FIX_STEPS
        if {op["op"] for op in finding.fix} <= measure_ops:
            parsed = report_edits.parse_operations(finding.fix)
            assert [p.op for p in parsed] == [op["op"] for op in finding.fix]
            continue
        operations = [report_edit_model.parse_model_operation(op) for op in finding.fix]
        results = report_edit_model.apply_model_family(document, operations, info)
        assert len(results) == len(operations), finding.id
    assert [f.fix_outside_supported for f in with_fix if f.rule == "date_table_not_marked"] == [
        True
    ]


def test_it_is_fast() -> None:
    facts = _messy_facts()
    facts["measures"] += [
        measure(f"M{i}", f"CALCULATE([Total Amount], FILTER(ALL(Customer[Name]), [M{i - 1}] > 1))")
        for i in range(1, 300)
    ]
    started = time.perf_counter()
    mh.check_model(facts, usage=USED)
    assert time.perf_counter() - started < 2.0


# --- the real Agents Performance dashboard (read-only) -----------------------------------------

AGENTS_FINDINGS = {
    # id: number of objects
    "floating_point_column:FactSales": 3,
    "dax_division:# Measures": 2,
    "unused_column:Actuals-vs-PctChange": 1,
    "unused_column:DimEmployee": 9,
    "unused_column:DimStore": 8,
    "unused_column:FactSales": 6,
    "visible_key_column:DimChannel": 1,
    "visible_key_column:DimStore": 2,
    "visible_key_column:FactSales": 3,
    "summarized_key_column:Clustered Employees": 1,
    "unused_measure:# Measures": 23,
    "measure_folders:# Measures": 12,
    "missing_descriptions": 161,
    "measure_no_format:# Measures": 42,
    "name_case_clash:channelkey": 2,
}


@pytest.mark.skipif(not AGENTS.exists(), reason="the real Agents Performance report is absent")
def test_the_real_agents_dashboard(tmp_path: Path) -> None:
    copy = tmp_path / "agents.pbix"
    shutil.copyfile(AGENTS, copy)
    facts = pbix_extract.read_model_facts(copy)
    usage = mh.report_usage(PbirDocument(PbixPackage(copy)))
    started = time.perf_counter()
    health = mh.check_model(facts, usage=usage)
    assert time.perf_counter() - started < 1.0
    assert {f.id: len(f.objects) for f in health.findings} == AGENTS_FINDINGS
    assert ids(health) == list(AGENTS_FINDINGS)
    assert health.summary["problems"] == 0
    assert [o.name for o in only(health, "dax_division")[0].objects] == [
        "Employees Avg Total Discount - Adjusted",
        "MTD Discount per Transaction",
    ]
    assert [o.label() for o in only(health, "name_case_clash")[0].objects] == [
        "'FactSales'[channelKey]",
        "'DimChannel'[ChannelKey]",
    ]
    # Report-used measures are never called unused; neither is a measure other measures call.
    unused = {o.name for o in only(health, "unused_measure")[0].objects}
    assert not unused & {"Total Sales", "MTD Total Sales", "Rank", "dummy", "_ShowValueForDates"}
    # No formula text reaches a message.
    text = json.dumps(health.as_dict())
    for item in facts["measures"]:
        expression = str(item.get("expression") or "").strip()
        if len(expression) > 12:
            assert expression not in text
    # With the decoded rows' statistics the relationship keys are unique and nothing is
    # high-cardinality: the same findings.
    rows = tmp_path / "rows"
    rows.mkdir()
    pbix_extract.extract(copy, rows)
    meta = json.loads((rows / "model.json").read_text(encoding="utf-8"))
    files = {t["name"]: rows / t["file"] for t in meta["tables"]}
    stats = mh.stats_from_parquet(files, wanted=mh.stats_wanted(facts))
    assert stats[("dimstore", "storekey")] == ColumnStats(rows=306, distinct=306, blanks=0)
    with_stats = mh.check_model(facts, usage=usage, stats=stats)
    assert ids(with_stats) == list(AGENTS_FINDINGS)
    assert with_stats.notes == []
