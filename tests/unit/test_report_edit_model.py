"""The model family: operations, their checks against ModelInfo and the model's structure, the
report reference rewrites of renames, the saved-model diff, and the tool cards."""

from __future__ import annotations

import copy
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from editing_support import TWIN
from fake_tom import Model, agents_state, snapshot_of
from pydantic import ValidationError

from ai_powerbi_analyst.studio import report_edit_model as rem
from ai_powerbi_analyst.studio import report_edits
from ai_powerbi_analyst.studio.edit_tools import ToolCard, tool_cards
from ai_powerbi_analyst.studio.pbir_document import DocumentError, PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage
from ai_powerbi_analyst.studio.report_edit_context import ModelInfo
from ai_powerbi_analyst.studio.report_edit_model import (
    MODEL_FAMILY_OPERATIONS,
    MODEL_HANDLERS,
    MODEL_REPORT_HANDLERS,
    TOOL_CARDS,
    ModelStructure,
    RelationshipFact,
    apply_model_family,
    check_model_operation,
    example_model,
    model_structure,
    parse_model_operation,
    saved_model_problems,
)

TABLE_VISUAL = "e82e02f93055d6b0b544"  # a table of DimEmployee[EmployeeKey]
SLICER_N = "953a59a3cad98b0b5a39"  # Top-Bottom-N, with a From-alias filter
SLICER_FIELD = "29ba010b20b2162d1536"  # Actuals-vs-PctChange, with a sort and a From-alias filter
STORE_VISUAL = "5b0dbccd61c6c4adb415"  # DimStore[StoreName]
PAGE = "ReportSection8454c812b60b8bb83b99"


@pytest.fixture(autouse=True)
def _m_checker(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The Power Query parser is a node process; the handlers get a recording stand-in."""
    seen: list[str] = []

    def check(text: str) -> list[str] | None:
        seen.append(text)
        return ["Invalid M syntax: Expected 'in'"] if "BROKEN" in text else []

    monkeypatch.setattr(rem, "M_CHECKER", check)
    return seen


def twin() -> PbirDocument:
    return PbirDocument(PbixPackage(TWIN))


def run(
    op: dict[str, Any], model: ModelInfo | None = None, document: PbirDocument | None = None
) -> Any:
    return check_model_operation(
        document or twin(),
        parse_model_operation(op),
        model if model is not None else example_model(),
    )


def refuse(op: dict[str, Any], match: str, model: ModelInfo | None = None) -> None:
    with pytest.raises(DocumentError, match=match):
        run(op, model)


def visual(document: PbirDocument, name: str) -> dict[str, Any]:
    return document.pages[PAGE].visuals[name]


# --- the family's tables and cards ---------------------------------------------------------------


def test_every_operation_has_a_handler_and_a_distinct_name() -> None:
    names = [cls.model_fields["op"].default for cls in MODEL_FAMILY_OPERATIONS]
    assert len(names) == 30 and len(set(names)) == 30
    assert sorted(MODEL_HANDLERS) == sorted(names)
    assert sorted(MODEL_REPORT_HANDLERS) == ["rename_column", "rename_table"]
    existing = {cls.model_fields["op"].default for cls in report_edits.REPORT_OPERATIONS}
    assert set(names) <= existing  # registered in the facade (round 3 integration)


def test_every_operation_has_exactly_one_card_that_needs_desktop_and_a_yes() -> None:
    by_op = {card.op: card for card in TOOL_CARDS}
    assert len(by_op) == len(TOOL_CARDS) == len(MODEL_FAMILY_OPERATIONS)
    assert set(by_op) == set(MODEL_HANDLERS)
    for card in TOOL_CARDS:
        assert (card.family, card.needs_desktop, card.confirm) == ("model", True, True), card.op
        assert card.cues and card.examples and card.summary.endswith(".")
    assert {c.op for c in tool_cards(("report_edit_model",))} == set(by_op)


@pytest.mark.parametrize(
    "card, example",
    [(card, example) for card in TOOL_CARDS for example in card.examples],
    ids=lambda value: value.op if isinstance(value, ToolCard) else value[0][:40],
)
def test_every_card_example_parses_and_applies_to_the_twin(card: ToolCard, example: Any) -> None:
    _, operation = example
    assert operation["op"] == card.op
    result = run(dict(operation))
    assert result.summary.endswith(".")


def test_cards_fill_column_slots_in_any_shape() -> None:
    card = next(c for c in TOOL_CARDS if c.op == "set_column_properties")
    assert card.to_operation(
        {"column": {"table": "Date", "name": "Month"}, "sort_by": "Month Number"}
    ) == {
        "op": "set_column_properties",
        "table": "Date",
        "name": "Month",
        "sort_by": "Month Number",
    }
    assert card.to_operation({"column": "'Date'[Month]", "hidden": True}) == {
        "op": "set_column_properties",
        "table": "Date",
        "name": "Month",
        "hidden": True,
    }
    rename = next(c for c in TOOL_CARDS if c.op == "rename_column")
    assert rename.to_operation(
        {"table": "DimStore", "column": "StoreName", "new_name": "Store"}
    ) == {
        "op": "rename_column",
        "table": "DimStore",
        "name": "StoreName",
        "new_name": "Store",
    }
    levels = next(c for c in TOOL_CARDS if c.op == "create_hierarchy")
    assert levels.to_operation(
        {
            "name": "Calendar",
            "levels": [{"table": "Date", "name": "Year"}, "'Date'[Month]", {"column": "Day"}],
        }
    ) == {
        "op": "create_hierarchy",
        "table": "Date",
        "name": "Calendar",
        "levels": [{"column": "Year"}, {"column": "Month"}, {"column": "Day"}],
    }
    plain = next(c for c in TOOL_CARDS if c.op == "create_relationship")
    assert plain.to_operation(
        {
            "from_table": "A",
            "from_column": "a",
            "to_table": "B",
            "to_column": "b",
            "cross_filter": "both",
        }
    ) == {
        "op": "create_relationship",
        "from_table": "A",
        "from_column": "a",
        "to_table": "B",
        "to_column": "b",
        "cross_filter": "both",
    }


def test_a_column_slot_never_moves_the_change_to_another_table() -> None:
    """Live bug: "hide the StoreKey column of DimStore in the model" hid FactSales[StoreKey]
    (both tables have StoreKey): the column's own table won over the table the user named."""
    cards = {card.op: card for card in TOOL_CARDS}
    elsewhere = {"table": "FactSales", "name": "StoreKey"}
    wrong_table = r"\[StoreKey\] you named is in 'FactSales', but the request says 'DimStore'"
    for op in ("set_column_properties", "rename_column", "delete_column"):
        with pytest.raises(DocumentError, match=wrong_table):
            cards[op].to_operation(
                {"table": "DimStore", "column": elsewhere, "hidden": True, "new_name": "K"}
            )
        with pytest.raises(DocumentError, match=wrong_table):
            cards[op].to_operation({"table": "DimStore", "column": "FactSales[StoreKey]"})
        with pytest.raises(DocumentError, match=r"^Say which table's \[StoreKey\] column"):
            cards[op].to_operation({"column": "StoreKey"})
    hidden = cards["set_column_properties"].to_operation(
        {"table": "DimStore", "column": {"table": "dimstore", "name": "StoreKey"}, "hidden": True}
    )
    assert (hidden["table"], hidden["name"]) == ("DimStore", "StoreKey")
    assert cards["set_column_properties"].to_operation({"column": elsewhere})["table"] == (
        "FactSales"
    )
    with pytest.raises(DocumentError, match="sort-by column \\[Month Number\\] you named is in"):
        cards["set_column_properties"].to_operation(
            {
                "table": "Date",
                "column": "Month",
                "sort_by": {"table": "FactSales", "name": "Month Number"},
            }
        )
    # The date table's date column and every hierarchy level belong to the named table.
    with pytest.raises(DocumentError, match="\\[DateKey\\] you named is in 'FactSales'"):
        cards["mark_date_table"].to_operation(
            {"table": "Date", "date_column": {"table": "FactSales", "name": "DateKey"}}
        )
    assert cards["mark_date_table"].to_operation(
        {"table": "Date", "date_column": {"table": "Date", "name": "Date"}}
    ) == {"op": "mark_date_table", "table": "Date", "column": "Date"}
    with pytest.raises(DocumentError, match="\\[StoreKey\\] you named is in 'FactSales'"):
        cards["create_hierarchy"].to_operation(
            {"table": "DimStore", "name": "H", "levels": ["StoreName", elsewhere]}
        )
    # A relationship's ends keep their own tables; a table slot that disagrees is refused.
    made = cards["create_relationship"].to_operation(
        {"from": elsewhere, "to": {"table": "DimStore", "name": "StoreKey"}}
    )
    assert (made["from_table"], made["from_column"], made["to_table"], made["to_column"]) == (
        "FactSales",
        "StoreKey",
        "DimStore",
        "StoreKey",
    )
    for op in ("create_relationship", "update_relationship", "delete_relationship"):
        with pytest.raises(DocumentError, match="you named is in 'FactSales'"):
            cards[op].to_operation(
                {"from": elsewhere, "from_table": "DimStore", "to": "DimStore[StoreKey]"}
            )
        with pytest.raises(DocumentError, match=r"Say which table's \[StoreKey\]"):
            cards[op].to_operation({"from": "StoreKey", "to": "DimStore[StoreKey]"})


def test_a_bare_column_name_several_tables_have_is_refused_with_their_names() -> None:
    model = example_model()
    with pytest.raises(
        DocumentError, match="^FactSales and DimStore each have StoreKey; say which"
    ):
        rem.column_owner(model, "StoreKey")
    assert rem.column_owner(model, "storename") == ("DimStore", "StoreName")
    assert rem.column_owner(model, "StoreKey", "dimstore") == ("DimStore", "StoreKey")
    with pytest.raises(DocumentError, match="no column 'Nope'"):
        rem.column_owner(model, "Nope")
    # A handler given a table that lacks the column says which tables have it.
    refuse(
        {"op": "set_column_properties", "table": "DimChannel", "name": "StoreKey", "hidden": True},
        r"The table DimChannel has no column 'StoreKey' \(FactSales and DimStore have one\)",
    )


def test_operation_fields_are_validated_before_any_handler() -> None:
    with pytest.raises(ValidationError):
        parse_model_operation(
            {
                "op": "create_relationship",
                "from_table": "A",
                "from_column": "a",
                "to_table": "B",
                "to_column": "b",
                "cardinality": "one_to_many",
            }
        )
    with pytest.raises(ValidationError):
        parse_model_operation({"op": "set_model_options", "culture": "English"})
    with pytest.raises(ValidationError):
        parse_model_operation(
            {"op": "create_hierarchy", "table": "Date", "name": "H", "levels": []}
        )
    with pytest.raises(ValidationError):
        parse_model_operation({"op": "delete_table", "name": "X", "cascade": True})


# --- columns -------------------------------------------------------------------------------------


def test_a_calculated_column_is_checked_and_joins_the_model_view() -> None:
    model = example_model()
    result = run(
        {
            "op": "create_calculated_column",
            "table": "factsales",
            "name": "Margin",
            "expression": "FactSales[SalesAmount] - FactSales[Cost]",
        },
        model,
    )
    assert result.summary == "Created the calculated column FactSales[Margin]."
    assert result.warnings == [
        "The formula uses FactSales[Cost], which Studio cannot find in the model. Power BI "
        "checks it, "
        "and the change is not kept if Power BI rejects it."
    ]
    assert "Margin" in model.columns["FactSales"]
    assert model.calculated["FactSales[Margin]"] == "FactSales[SalesAmount] - FactSales[Cost]"
    refuse(
        {
            "op": "create_calculated_column",
            "table": "FactSales",
            "name": "SalesAmount",
            "expression": "1",
        },
        "already has a column called 'SalesAmount'",
    )
    refuse(
        {"op": "create_calculated_column", "table": "FactSales", "name": "Rank", "expression": "1"},
        r"already has a measure called 'Rank' \(in # Measures\)",
    )
    refuse(
        {
            "op": "create_calculated_column",
            "table": "FactSales",
            "name": "M",
            "expression": "SUM(1",
        },
        "parenthesis that is never closed",
    )
    refuse(
        {
            "op": "create_calculated_column",
            "table": "FactSales",
            "name": "M",
            "expression": "M = 1",
        },
        "without the name and equals sign",
    )
    refuse(
        {"op": "create_calculated_column", "table": "Nope", "name": "M", "expression": "1"},
        "has no table 'Nope'",
    )


def test_column_properties_refuse_what_desktop_would_not_do() -> None:
    result = run(
        {
            "op": "set_column_properties",
            "table": "Date",
            "name": "Month",
            "sort_by": "Month Number",
            "summarize_by": "none",
            "hidden": True,
        }
    )
    assert result.summary == "Changed the column Date[Month] (sort by, summarize by, hidden)."
    refuse(
        {"op": "set_column_properties", "table": "Date", "name": "Month"},
        "Say what to change in the column",
    )
    refuse(
        {"op": "set_column_properties", "table": "Date", "name": "Month", "sort_by": "month"},
        "cannot be sorted by itself",
    )
    refuse(
        {
            "op": "set_column_properties",
            "table": "Date",
            "name": "Month",
            "data_type": "whole_number",
        },
        "set in Power Query",
    )
    refuse(
        {"op": "set_column_properties", "table": "Date", "name": "Month", "expression": "1"},
        "has no formula",
    )
    refuse(
        {
            "op": "set_column_properties",
            "table": "DimStore",
            "name": "StoreName",
            "summarize_by": "sum",
        },
        "holds text, so it can be counted but not summarized by sum",
    )
    model = example_model()
    model_structure(model).columns[("date", "month number")]["sort_by"] = "Month"
    refuse(
        {
            "op": "set_column_properties",
            "table": "Date",
            "name": "Month",
            "sort_by": "Month Number",
        },
        "would sort by each other",
        model,
    )
    model.calculated["FactSales[Margin]"] = "1"
    model.columns["FactSales"]["Margin"] = ""
    changed = run(
        {
            "op": "set_column_properties",
            "table": "FactSales",
            "name": "Margin",
            "data_type": "decimal_number",
            "expression": "2",
        },
        model,
    )
    assert changed.summary == "Changed the column FactSales[Margin] (data type, expression)."
    assert model.calculated["FactSales[Margin]"] == "2"


def test_a_column_rename_rewrites_fields_sorts_and_from_alias_filters() -> None:
    document = twin()
    model = example_model()
    before = json.dumps(visual(document, SLICER_FIELD))
    assert before.count('"Property": "Top-Bottom-Field"') == 3
    result = run(
        {
            "op": "rename_column",
            "table": "Actuals-vs-PctChange",
            "name": "top-bottom-field",
            "new_name": "Metric Choice",
        },
        model,
        document,
    )
    assert result.summary == (
        "Renamed the column Actuals-vs-PctChange[Top-Bottom-Field] to [Metric Choice] and updated "
        "the visuals, filters and formulas that use it."
    )
    data = visual(document, SLICER_FIELD)
    text = json.dumps(data)
    assert text.count('"Property": "Top-Bottom-Field"') == 0
    assert text.count('"Property": "Metric Choice"') == 3
    projection = data["visual"]["query"]["queryState"]["Values"]["projections"][0]
    assert projection["queryRef"] == "Actuals-vs-PctChange.Top-Bottom-Field"  # kept stable
    assert projection["nativeQueryRef"] == "Metric Choice"
    where = data["visual"]["objects"]["general"][0]["properties"]["filter"]["filter"]["Where"][0]
    left = where["Condition"]["In"]["Expressions"][0]["Column"]
    assert left == {"Expression": {"SourceRef": {"Source": "a"}}, "Property": "Metric Choice"}
    assert "Metric Choice" in model.columns["Actuals-vs-PctChange"]
    assert "Top-Bottom-Field" not in model.columns["Actuals-vs-PctChange"]
    refuse(
        {"op": "rename_column", "table": "DimStore", "name": "StoreName", "new_name": "StoreType"},
        "already has a column called 'StoreType'",
    )
    refuse(
        {"op": "rename_column", "table": "DimStore", "name": "StoreName", "new_name": "StoreName"},
        "already called",
    )


def test_the_report_half_of_a_rename_rewrites_the_saved_file() -> None:
    document = twin()
    handler = MODEL_REPORT_HANDLERS["rename_column"]
    result = handler(
        document,
        rem.RenameColumn(table="DimStore", name="StoreName", new_name="Store"),
        example_model(),
    )
    assert result.summary.startswith("Renamed the column DimStore[StoreName] to [Store]")
    field = visual(document, STORE_VISUAL)["visual"]["query"]["queryState"]["Values"][
        "projections"
    ][0]["field"]
    assert field["Column"]["Property"] == "Store"
    table_half = MODEL_REPORT_HANDLERS["rename_table"](
        document, rem.RenameTable(name="DimStore", new_name="Stores"), example_model()
    )
    assert table_half.summary.startswith("Renamed the table 'DimStore' to 'Stores'")
    assert field["Column"]["Expression"]["SourceRef"]["Entity"] == "Stores"


def test_a_column_that_anything_uses_cannot_be_deleted() -> None:
    refuse(
        {"op": "delete_column", "table": "DimStore", "name": "StoreName"},
        r"DimStore\[StoreName\] is used by .*on page '1'",
    )
    refuse({"op": "delete_column", "table": "Top-Bottom-N", "name": "Top-Bottom-N"}, "is used by")
    refuse(
        {"op": "delete_column", "table": "FactSales", "name": "SalesAmount"},
        r"used in the formula of the measure \[Table MTD Sales\]",
    )
    refuse(
        {"op": "delete_column", "table": "FactSales", "name": "StoreKey"},
        "used by the relationship between FactSales",
    )
    refuse(
        {"op": "delete_column", "table": "Date", "name": "Month"},
        "a level of the hierarchy 'calendar'",
    )
    model = example_model()
    model_structure(model).columns[("date", "month")]["sort_by"] = "Month Number"
    refuse(
        {"op": "delete_column", "table": "Date", "name": "Month Number"},
        r"the sort order of \[Month\]",
        model,
    )
    result = run({"op": "delete_column", "table": "DimStore", "name": "Status"}, model)
    assert result.summary == "Deleted the column DimStore[Status]." and result.warnings == []
    assert "Status" not in model.columns["DimStore"]
    bare = ModelInfo.from_report([{"TableName": "DimStore", "ColumnName": "Status"}], [])
    unknown = run({"op": "delete_column", "table": "DimStore", "name": "Status"}, bare)
    assert unknown.warnings == [
        "Studio could not read the model's relationships, so Power BI Desktop checks this "
        "change instead.",
        "Studio could not read the model's hierarchies and sort orders, so Power BI Desktop "
        "checks this change instead.",
    ]


# --- tables --------------------------------------------------------------------------------------


def test_a_table_rename_rewrites_entities_from_lists_and_selectors_and_moves_its_measures() -> None:
    document = twin()
    model = example_model()
    result = run(
        {"op": "rename_table", "name": "clustered employees", "new_name": "Employee Groups"},
        model,
        document,
    )
    assert result.summary == (
        "Renamed the table 'Clustered Employees' to 'Employee Groups' and updated the visuals, "
        "filters and formulas that use it."
    )
    chart = json.dumps(visual(document, "b404a461ede7604a2774"))
    # The projection and the three data-point selectors (scopeId comparisons) name the table.
    assert chart.count('"Entity": "Employee Groups"') == 4
    assert chart.count('"Entity": "Clustered Employees"') == 0
    renamed = run(
        {"op": "rename_table", "name": "Top-Bottom-N", "new_name": "TopN"}, model, document
    )
    assert renamed.summary.startswith("Renamed the table 'Top-Bottom-N' to 'TopN'")
    slicer = visual(document, SLICER_N)
    sources = slicer["visual"]["objects"]["general"][0]["properties"]["filter"]["filter"]["From"]
    assert sources[0]["Entity"] == "TopN"
    assert (
        slicer["visual"]["query"]["queryState"]["Values"]["projections"][0]["queryRef"]
        == "Top-Bottom-N.Top-Bottom-N"
    )
    run({"op": "rename_table", "name": "# Measures", "new_name": "Measures"}, model, document)
    assert model.measure_table("Rank") == "Measures"
    refuse(
        {"op": "rename_table", "name": "DimStore", "new_name": "DimChannel"},
        "already has a table called 'DimChannel'",
    )


def test_tables_are_only_deleted_when_nothing_uses_them() -> None:
    refuse({"op": "delete_table", "name": "DimStore"}, "'DimStore' is used by")
    refuse(
        {"op": "delete_table", "name": "DimEmployee"},
        r"^'DimEmployee' is used by the table of Agent ID on page '1'; used in the formula of the "
        r"measure \[Employees Avg MTD Sales\]\. Remove or change those first\.$",
    )
    model = example_model()
    result = run({"op": "delete_table", "name": "DimPromotion"}, model)
    assert result.summary == "Deleted the table 'DimPromotion'."
    assert model.table("DimPromotion") is None
    state = example_model()
    model_structure(state).relationships.append(
        RelationshipFact("FactSales", "PromotionKey", "DimPromotion", "PromotionKey")
    )
    assert run({"op": "delete_table", "name": "DimPromotion"}, state).summary == (
        "Deleted the table 'DimPromotion' and its 1 relationship."
    )
    assert (
        run({"op": "delete_table", "name": "Time Calc"}).summary
        == "Deleted the calculation group 'Time Calc'."
    )
    assert (
        run({"op": "delete_table", "name": "Metric"}).summary
        == "Deleted the field parameter 'Metric'."
    )


def test_table_properties_and_calculated_tables() -> None:
    model = example_model()
    assert run(
        {"op": "create_calculated_table", "name": "Years", "expression": "DISTINCT('Date'[Year])"},
        model,
    ).summary == ("Created the calculated table 'Years'.")
    assert model.table("Years") == "Years" and model.calculated["Years"] == "DISTINCT('Date'[Year])"
    refuse(
        {"op": "create_calculated_table", "name": "Date", "expression": "1"},
        "already has a table called 'Date'",
    )
    assert run({"op": "set_table_properties", "table": "DimChannel", "hidden": True}).summary == (
        "Changed the table 'DimChannel' (hidden)."
    )
    refuse(
        {"op": "set_table_properties", "table": "DimStore", "expression": "1"},
        "is not a calculated table",
    )
    refuse({"op": "set_table_properties", "table": "DimStore"}, "Say what to change in the table")


def test_marking_a_date_table() -> None:
    assert run({"op": "mark_date_table", "table": "Date", "column": "date"}).summary == (
        "Marked 'Date' as a date table using [Date]."
    )
    refuse({"op": "mark_date_table", "table": "Date", "column": "Month"}, "does not hold dates")
    assert (
        run({"op": "mark_date_table", "table": "Date"}).summary
        == "'Date' is no longer marked as a date table."
    )
    known = example_model()
    model_structure(known).tables["dimstore"]["date_table"] = False
    refuse(
        {"op": "mark_date_table", "table": "DimStore"},
        "'DimStore' is not marked as a date table",
        known,
    )
    # Facts that do not say whether a table is marked leave the check to Desktop.
    assert run({"op": "mark_date_table", "table": "DimStore"}).summary == (
        "'DimStore' is no longer marked as a date table."
    )


# --- relationships -------------------------------------------------------------------------------


def test_relationships_are_checked_for_duplicates_ambiguity_types_and_direction() -> None:
    model = example_model()
    result = run(
        {
            "op": "create_relationship",
            "from_table": "DimStore",
            "from_column": "EmployeeKey",
            "to_table": "DimEmployee",
            "to_column": "EmployeeKey",
        },
        model,
    )
    assert result.summary == (
        "Created a many-to-one relationship from DimStore[EmployeeKey] to DimEmployee[EmployeeKey] "
        "(single direction, active)."
    )
    assert (
        model_structure(model).relationship(
            ("DimEmployee", "EmployeeKey"), ("DimStore", "EmployeeKey")
        )
        is not None
    )
    refuse(
        {
            "op": "create_relationship",
            "from_table": "DimStore",
            "from_column": "StoreKey",
            "to_table": "FactSales",
            "to_column": "StoreKey",
        },
        "are already related",
    )
    refuse(
        {
            "op": "create_relationship",
            "from_table": "FactSales",
            "from_column": "PromotionKey",
            "to_table": "DimStore",
            "to_column": "StoreKey",
        },
        r"already connected through active relationships \(FactSales - DimStore\)"
        r".*unambiguous active path",
    )
    inactive = run(
        {
            "op": "create_relationship",
            "from_table": "FactSales",
            "from_column": "PromotionKey",
            "to_table": "DimStore",
            "to_column": "StoreKey",
            "active": False,
        }
    )
    assert inactive.summary.endswith("(single direction, inactive).")
    refuse(
        {
            "op": "create_relationship",
            "from_table": "FactSales",
            "from_column": "DateKey",
            "to_table": "DimPromotion",
            "to_column": "PromotionKey",
        },
        "holds date values and DimPromotion\\[PromotionKey\\] holds number values",
    )
    refuse(
        {
            "op": "create_relationship",
            "from_table": "DimStore",
            "from_column": "EmployeeKey",
            "to_table": "DimEmployee",
            "to_column": "EmployeeKey",
            "cardinality": "one_to_one",
        },
        "always filters in both directions",
    )
    refuse(
        {
            "op": "create_relationship",
            "from_table": "DimStore",
            "from_column": "StoreKey",
            "to_table": "DimStore",
            "to_column": "EmployeeKey",
        },
        "joins two different tables",
    )


def test_updating_and_deleting_relationships() -> None:
    model = example_model()
    model_structure(model).relationships.append(
        RelationshipFact("FactSales", "PromotionKey", "DimPromotion", "PromotionKey", active=False)
    )
    assert run(
        {
            "op": "update_relationship",
            "from_table": "DimPromotion",
            "from_column": "PromotionKey",
            "to_table": "FactSales",
            "to_column": "PromotionKey",
            "active": True,
            "cross_filter": "both",
        },
        model,
    ).summary == (
        "Changed the relationship between DimPromotion[PromotionKey] and FactSales[PromotionKey] "
        "(both directions, active)."
    )
    refuse(
        {
            "op": "update_relationship",
            "from_table": "DimStore",
            "from_column": "EmployeeKey",
            "to_table": "DimEmployee",
            "to_column": "EmployeeKey",
            "active": True,
        },
        r"no relationship between DimStore\[EmployeeKey\]",
    )
    refuse(
        {
            "op": "update_relationship",
            "from_table": "FactSales",
            "from_column": "StoreKey",
            "to_table": "DimStore",
            "to_column": "StoreKey",
        },
        "Say what to change in the relationship",
    )
    blocked = example_model()
    model_structure(blocked).relationships.append(
        RelationshipFact("DimStore", "EmployeeKey", "FactSales", "SalesKey", active=False)
    )
    refuse(
        {
            "op": "update_relationship",
            "from_table": "DimStore",
            "from_column": "EmployeeKey",
            "to_table": "FactSales",
            "to_column": "SalesKey",
            "active": True,
        },
        r"already connected through active relationships \(DimStore - FactSales\)"
        r".*unambiguous active path",
        blocked,
    )
    used = example_model()
    used.measure_expressions["Table MTD Sales"] = (
        "CALCULATE(SUM(FactSales[SalesAmount]), "
        "USERELATIONSHIP(FactSales[StoreKey], 'DimStore'[StoreKey]))"
    )
    refuse(
        {
            "op": "delete_relationship",
            "from_table": "DimStore",
            "from_column": "StoreKey",
            "to_table": "FactSales",
            "to_column": "StoreKey",
        },
        r"USERELATIONSHIP or CROSSFILTER in the measure \[Table MTD Sales\]",
        used,
    )
    assert run(
        {
            "op": "delete_relationship",
            "from_table": "FactSales",
            "from_column": "channelKey",
            "to_table": "DimChannel",
            "to_column": "ChannelKey",
        }
    ).summary == (
        "Deleted the relationship between FactSales[channelKey] and DimChannel[ChannelKey]."
    )


# --- hierarchies, field parameters, calculation groups -------------------------------------------


def hierarchy_visual(document: PbirDocument) -> None:
    """Put a Date > Calendar > Year level on a visual, the way Desktop writes a hierarchy field."""
    state = visual(document, TABLE_VISUAL)["visual"]["query"]["queryState"]
    state["Values"]["projections"].append(
        {
            "field": {
                "HierarchyLevel": {
                    "Expression": {
                        "Hierarchy": {
                            "Expression": {"SourceRef": {"Entity": "Date"}},
                            "Hierarchy": "Calendar",
                        }
                    },
                    "Level": "Year",
                }
            },
            "queryRef": "Date.Calendar.Year",
            "nativeQueryRef": "Calendar Year",
        }
    )


def test_hierarchies_and_the_visuals_that_use_them() -> None:
    assert run(
        {
            "op": "create_hierarchy",
            "table": "Date",
            "name": "Fiscal",
            "levels": [{"column": "Year"}, {"column": "Month", "name": "Fiscal Month"}],
        }
    ).summary == ("Created the hierarchy 'Fiscal' in Date (Year > Fiscal Month).")
    refuse(
        {
            "op": "create_hierarchy",
            "table": "Date",
            "name": "Calendar",
            "levels": [{"column": "Year"}],
        },
        "already has a hierarchy called 'Calendar'",
    )
    refuse(
        {
            "op": "create_hierarchy",
            "table": "Date",
            "name": "H",
            "levels": [{"column": "Year"}, {"column": "year"}],
        },
        "two levels called 'Year'",
    )
    refuse(
        {
            "op": "create_hierarchy",
            "table": "Date",
            "name": "H",
            "levels": [{"column": "Year", "name": "A"}, {"column": "year", "name": "B"}],
        },
        r"\[Year\] is used by two levels",
    )
    document = twin()
    hierarchy_visual(document)
    with pytest.raises(
        DocumentError,
        match=r"levels 'Quarter' of 'Calendar' are removed or renamed, but the hierarchy is used "
        r"by the table of Agent ID on page '1'",
    ):
        check_model_operation(
            document,
            parse_model_operation(
                {
                    "op": "update_hierarchy",
                    "table": "Date",
                    "name": "Calendar",
                    "levels": [{"column": "Year"}, {"column": "Month"}],
                }
            ),
            example_model(),
        )
    with pytest.raises(DocumentError, match="cannot point visuals at a renamed hierarchy"):
        check_model_operation(
            document,
            parse_model_operation(
                {"op": "update_hierarchy", "table": "Date", "name": "Calendar", "new_name": "Cal"}
            ),
            example_model(),
        )
    with pytest.raises(DocumentError, match="is used by .* Remove it from those first"):
        check_model_operation(
            document,
            parse_model_operation({"op": "delete_hierarchy", "table": "Date", "name": "Calendar"}),
            example_model(),
        )
    added = check_model_operation(
        document,
        parse_model_operation(
            {
                "op": "update_hierarchy",
                "table": "Date",
                "name": "Calendar",
                "levels": [
                    {"column": "Year"},
                    {"column": "Quarter"},
                    {"column": "Month"},
                    {"column": "Date"},
                ],
            }
        ),
        example_model(),
    )
    assert (
        added.summary
        == "Changed the hierarchy 'Calendar' in Date (levels Year > Quarter > Month > Date)."
    )


def test_field_parameters_list_fields_in_desktops_layout() -> None:
    model = example_model()
    fields = [
        rem.ParameterField(table="# Measures", name="rank", label="Agent rank"),
        rem.ParameterField(table="DimStore", name="StoreName"),
    ]
    assert rem.field_parameter_dax(model, fields) == (
        "{\n"
        "    (\"Agent rank\", NAMEOF('# Measures'[Rank]), 0),\n"
        "    (\"StoreName\", NAMEOF('DimStore'[StoreName]), 1)\n"
        "}"
    )
    result = run(
        {
            "op": "create_field_parameter",
            "name": "Picker",
            "fields": [{"table": "# Measures", "name": "Rank"}],
        },
        model,
    )
    assert result.summary == "Created the field parameter 'Picker' with 1 field."
    assert set(model.columns["Picker"]) == {"Picker", "Picker Fields", "Picker Order"}
    refuse(
        {
            "op": "create_field_parameter",
            "name": "P",
            "fields": [
                {"table": "# Measures", "name": "Rank"},
                {"table": "# Measures", "name": "rank"},
            ],
        },
        "in the list twice",
    )
    refuse(
        {
            "op": "create_field_parameter",
            "name": "P",
            "fields": [
                {"table": "# Measures", "name": "Rank", "label": "X"},
                {"table": "DimStore", "name": "StoreName", "label": "x"},
            ],
        },
        "Two fields would show as 'x'",
    )
    refuse(
        {
            "op": "update_field_parameter",
            "name": "DimStore",
            "fields": [{"table": "# Measures", "name": "Rank"}],
        },
        "'DimStore' is not a field parameter",
    )


def test_calculation_groups_items_and_precedence() -> None:
    model = example_model()
    result = run(
        {
            "op": "create_calculation_group",
            "name": "Scenarios",
            "items": [{"name": "Base", "expression": "SELECTEDMEASURE()"}],
            "precedence": 6,
        },
        model,
    )
    assert result.summary == "Created the calculation group 'Scenarios' with 1 item."
    assert result.warnings == []  # implicit measures are already discouraged in this model
    refuse(
        {
            "op": "create_calculation_group",
            "name": "S",
            "items": [{"name": "A", "expression": "1"}],
            "precedence": 5,
        },
        "'Time Calc' already has precedence 5",
    )
    refuse(
        {
            "op": "create_calculation_group",
            "name": "S",
            "items": [{"name": "A", "expression": "1"}, {"name": "a", "expression": "2"}],
            "precedence": 9,
        },
        "Two calculation items are called 'a'",
    )
    refuse(
        {
            "op": "create_calculation_group",
            "name": "S",
            "items": [{"name": "A", "expression": "1"}],
            "precedence": 9,
            "column": "Ordinal",
        },
        "sort column",
    )
    loose = example_model()
    model_structure(loose).settings["discourageImplicitMeasures"] = False
    warned = run(
        {
            "op": "create_calculation_group",
            "name": "S",
            "items": [{"name": "A", "expression": "1"}],
            "precedence": 9,
        },
        loose,
    )
    assert warned.warnings[0].startswith("Power BI needs 'discourage implicit measures' on")
    assert run(
        {
            "op": "set_calculation_item",
            "group": "Time Calc",
            "name": "YTD",
            "format_string_expression": '"0.0"',
            "new_name": "Year to date",
        }
    ).summary == (
        "Changed the calculation item 'YTD' of 'Time Calc' (format string, renamed to "
        "'Year to date')."
    )
    refuse(
        {"op": "set_calculation_item", "group": "Time Calc", "name": "QTD"},
        "give a formula to add it",
    )
    refuse(
        {"op": "set_calculation_item", "group": "DimStore", "name": "A", "expression": "1"},
        "'DimStore' is not a calculation group",
    )
    refuse(
        {"op": "set_calculation_item", "group": "Time Calc", "name": "YTD", "new_name": "PY"},
        "already has an item called 'PY'",
    )
    refuse(
        {"op": "delete_calculation_item", "group": "Time Calc", "name": "QTD"},
        "has no item called 'QTD'",
    )
    single = example_model()
    model_structure(single).groups["time calc"]["items"] = {
        "current": {"name": "Current", "expression": "SELECTEDMEASURE()"}
    }
    refuse(
        {"op": "delete_calculation_item", "group": "Time Calc", "name": "Current"},
        "delete the calculation group instead",
        single,
    )
    refuse({"op": "update_calculation_group", "name": "Time Calc"}, "Say what to change")


# --- roles, Power Query, settings ----------------------------------------------------------------


def test_roles_and_why_members_are_not_set_here() -> None:
    model = example_model()
    created = run(
        {
            "op": "create_role",
            "name": "Online",
            "filters": [{"table": "DimChannel", "expression": '[ChannelName] = "Online"'}],
        },
        model,
    )
    assert created.summary == "Created the role 'Online' with a filter on DimChannel."
    assert created.warnings == [
        "After the change Studio counts the rows the role sees in each filtered table and says "
        "so when a filter keeps every row or none; Modeling > View as in Power BI Desktop shows "
        "the whole report as that role."
    ]
    assert model_structure(model).roles["online"] == {"DimChannel": '[ChannelName] = "Online"'}
    refuse(
        {"op": "create_role", "name": "R", "members": ["someone@contoso.com"]},
        "added to a role in the Power BI service after the report is published",
    )
    refuse({"op": "update_role", "name": "Auditors", "members": ["x"]}, "Power BI service")
    refuse({"op": "create_role", "name": "auditors"}, "already has a role called 'auditors'")
    refuse(
        {"op": "create_role", "name": "R", "filters": [{"table": "Nope", "expression": "TRUE()"}]},
        "has no table 'Nope'",
    )
    refuse(
        {
            "op": "create_role",
            "name": "R",
            "filters": [{"table": "DimStore", "expression": '[A] = "x'}],
        },
        "text that is never closed",
    )
    changed = run(
        {
            "op": "update_role",
            "name": "Regional Managers",
            "filters": [{"table": "DimStore", "expression": ""}],
            "new_name": "Region",
        },
        model,
    )
    assert (
        changed.summary
        == "Changed the role 'Regional Managers' (filters on DimStore; renamed to 'Region')."
    )
    assert model_structure(model).roles["region"] == {}
    refuse({"op": "delete_role", "name": "Nobody"}, "has no role called 'Nobody'")
    assert run({"op": "create_role", "name": "Everyone"}).summary == (
        "Created the role 'Everyone' with no filters (it sees every row)."
    )


def test_power_query_changes_are_checked_by_the_parser(_m_checker: list[str]) -> None:
    result = run(
        {"op": "set_table_query", "table": "FactSales", "expression": "let Source = 1 in Source"}
    )
    assert (
        result.summary
        == "Changed the Power Query query of 'FactSales' and asked Power BI to reload its data."
    )
    assert _m_checker == ["let Source = 1 in Source"]
    refuse(
        {"op": "set_table_query", "table": "FactSales", "expression": "let BROKEN"},
        "The Power Query formula is not valid: Invalid M syntax",
    )
    refuse(
        {"op": "set_table_query", "table": "Date", "expression": "let a = 1 in a"},
        "calculated with DAX",
    )
    kept = run(
        {
            "op": "set_table_query",
            "table": "FactSales",
            "expression": "let a = 1 in a",
            "if_refresh_fails": "keep",
        }
    )
    assert kept.warnings[-1].startswith(
        "If Power BI cannot reach the data source, the new query is kept"
    )
    assert (
        run(
            {"op": "set_named_expression", "name": "Source Folder", "expression": '"G:\\\\Data"'}
        ).summary
        == "Changed the query 'Source Folder'."
    )
    assert (
        run({"op": "set_named_expression", "name": "Other", "expression": "1"}).summary
        == "Created the query 'Other'."
    )
    refuse(
        {"op": "set_named_expression", "name": "DimStore", "expression": "1"},
        "is the name of a table",
    )
    used = example_model()
    model_structure(used).partitions["factsales"] = [
        {
            "name": "FactSales",
            "kind": "m",
            "expression": 'let Source = Folder.Files(#"Source Folder") in Source',
        }
    ]
    refuse(
        {"op": "delete_named_expression", "name": "Source Folder"},
        "is used by the query of 'factsales'",
        used,
    )
    refuse(
        {"op": "delete_named_expression", "name": "Nothing"},
        "no query or parameter called 'Nothing'",
    )


def test_an_unavailable_parser_is_a_warning_not_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rem, "M_CHECKER", lambda text: None)
    result = run({"op": "set_named_expression", "name": "Other", "expression": "1"})
    assert result.warnings == [
        "Studio could not check the Power Query formula; Power BI checks it when it loads the data."
    ]


def test_parameters_refresh_and_model_settings() -> None:
    assert (
        rem.parameter_m("West", "text")
        == '"West" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]'
    )
    assert (
        rem.parameter_m(12, None)
        == '12 meta [IsParameterQuery=true, Type="Number", IsParameterQueryRequired=true]'
    )
    assert (
        rem.parameter_m(True, None)
        == 'true meta [IsParameterQuery=true, Type="Logical", IsParameterQueryRequired=true]'
    )
    assert rem.parameter_m('say "hi"', None).startswith('"say ""hi""" meta')
    assert rem.parameter_m("2024-01-31", "date").startswith("#date(2024, 1, 31) meta")
    assert rem.parameter_m("2024-01-31 08:30", "date_time").startswith(
        '#datetime(2024, 1, 31, 8, 30, 0) meta [IsParameterQuery=true, Type="DateTime"'
    )
    assert rem.parameter_m("08:30", "time").startswith("#time(8, 30, 0) meta")
    result = run({"op": "set_query_parameter", "name": "Region", "value": "West"})
    assert (result.summary, result.warnings) == (
        "Set the parameter 'Region' to \"West\".",
        ["The new value is used the next time the data is refreshed."],
    )
    refuse(
        {"op": "set_query_parameter", "name": "N", "value": "abc", "type": "number"},
        "'abc' is not a number",
    )
    assert (
        run({"op": "refresh_data"}).summary == "Asked Power BI to refresh the data of every table."
    )
    assert (
        run({"op": "refresh_data", "table": "factsales"}).summary
        == "Asked Power BI to refresh the data of 'FactSales'."
    )
    settings = run({"op": "set_model_options", "discourage_implicit_measures": True})
    assert settings.summary == "Changed the model settings (implicit measures discouraged)."
    refuse(
        {"op": "set_model_options", "discourage_implicit_measures": False},
        "must discourage implicit measures",
    )
    refuse({"op": "set_model_options"}, "Say which model setting to change")


def test_an_existing_parameter_keeps_its_type_list_and_required_flag() -> None:
    """Bug review #4: a DateTime RangeStart set to "2024-01-01" became Text; List= values were
    dropped and IsParameterQueryRequired forced to true."""
    model = example_model()
    structure = model_structure(model)
    run({"op": "set_query_parameter", "name": "RangeStart", "value": "2025-01-01"}, model)
    assert structure.expressions["rangestart"]["expression"] == (
        '#datetime(2025, 1, 1, 0, 0, 0) meta [IsParameterQuery=true, Type="DateTime", '
        "IsParameterQueryRequired=true]"
    )
    run({"op": "set_query_parameter", "name": "Region", "value": "East"}, model)
    assert structure.expressions["region"]["expression"] == (
        '"East" meta [IsParameterQuery=true, List={"West", "East", "North"}, '
        'DefaultValue="West", Type="Text", IsParameterQueryRequired=true]'
    )
    run({"op": "set_query_parameter", "name": "Top N", "value": "25"}, model)
    assert structure.expressions["top n"]["expression"] == (
        '25 meta [IsParameterQuery=true, Type="Number", IsParameterQueryRequired=false]'
    )
    refuse(
        {"op": "set_query_parameter", "name": "Region", "value": "South"},
        'takes one of its listed values: "West", "East", "North"',
    )
    refuse(
        {"op": "set_query_parameter", "name": "RangeStart", "value": "x", "type": "text"},
        "holds a date and time; Studio keeps a parameter's type",
    )
    refuse(
        {"op": "set_query_parameter", "name": "RangeStart", "value": "soon"},
        "is not a date; write it as 2024-01-31 08:30",
    )
    refuse({"op": "set_query_parameter", "name": "Source Folder", "value": "G:"}, "is a query")
    odd = example_model()
    model_structure(odd).expressions["span"] = {
        "name": "Span",
        "expression": '#duration(1, 0, 0, 0) meta [IsParameterQuery=true, Type="Duration", '
        "IsParameterQueryRequired=true]",
    }
    refuse(
        {"op": "set_query_parameter", "name": "Span", "value": "2"},
        "holds Duration, which Studio does not change.*Edit parameters",
        odd,
    )
    settings = rem.parameter_settings('"a, b" meta [IsParameterQuery=true, List={"a, b", "c"}]')
    assert settings is not None and settings.choices == ['"a, b"', '"c"']
    assert rem.parameter_settings("let x = 1 in x") is None


def test_auto_date_time_and_the_model_language_get_desktops_steps() -> None:
    """Spec review #2: Desktop's Auto date/time toggle also adds or removes the hidden
    LocalDateTable tables; the model language cannot be changed in Desktop for a file."""
    refuse(
        {"op": "set_model_options", "auto_date_time": False},
        r"File > Options and settings > Options > Current File > Data Load",
    )
    refuse(
        {"op": "set_model_options", "discourage_implicit_measures": True, "culture": "en-GB"},
        "does not change the model's language",
    )


def test_the_familys_dispatcher_names_the_failing_step() -> None:
    operations = [
        parse_model_operation(
            {"op": "create_calculated_table", "name": "Years", "expression": "1"}
        ),
        parse_model_operation({"op": "delete_table", "name": "DimStore"}),
    ]
    with pytest.raises(DocumentError, match="^Step 2: 'DimStore' is used by"):
        apply_model_family(twin(), operations, example_model())


def test_the_refresh_policy_of_a_batch() -> None:
    query = rem.SetTableQuery(table="T", expression="let a = 1 in a")
    assert rem.refresh_policy([]) is None
    assert rem.refresh_policy([rem.RefreshData()]) == "undo"
    assert rem.refresh_policy([query]) == "ask"
    assert (
        rem.refresh_policy(
            [query.model_copy(update={"if_refresh_fails": "keep"}), rem.RefreshData()]
        )
        == "keep"
    )
    assert (
        rem.refresh_policy([query, query.model_copy(update={"if_refresh_fails": "undo"})]) == "undo"
    )


# --- the structure the checks read ---------------------------------------------------------------


def test_the_structure_reads_the_live_snapshot() -> None:
    state = agents_state()
    state["roles"] = [
        {"name": "R", "tablePermissions": [{"name": "DimStore", "filterExpression": "TRUE()"}]}
    ]
    state["tables"][1]["hierarchies"] = [
        {"name": "Calendar", "levels": [{"name": "Y", "ordinal": 0, "column": "Year"}]}
    ]
    state["expressions"] = [{"name": "P", "kind": "M", "expression": "1"}]
    structure = ModelStructure.from_facts(snapshot_of(Model.from_state(state)))
    assert structure.known == {
        "tables",
        "columns",
        "relationships",
        "hierarchies",
        "roles",
        "calculation_groups",
        "field_parameters",
        "partitions",
        "expressions",
        "settings",
    }
    assert (
        structure.table_kind("Date") == "calculated" and structure.table_kind("factsales") == "data"
    )
    assert structure.hierarchies[("date", "calendar")] == [("Y", "Year")]
    assert structure.roles["r"] == {"DimStore": "TRUE()"} and structure.role_names["r"] == "R"
    assert structure.partitions["factsales"][0]["kind"] == "m"
    assert structure.relationship(
        ("DimStore", "StoreKey"), ("FactSales", "StoreKey")
    ) == RelationshipFact(
        "FactSales",
        "StoreKey",
        "DimStore",
        "StoreKey",
        active=True,
        both=False,
        from_many=True,
        to_many=False,
        cardinality_known=True,
        rely=False,
        security_both=False,
    )
    assert structure.active_path("DimChannel", "DimStore") == [
        "DimChannel",
        "FactSales",
        "DimStore",
    ]


def test_the_structure_reads_flat_facts_and_tolerates_junk() -> None:
    structure = ModelStructure.from_facts(
        {
            "relationships": [
                {
                    "FromTableName": "A",
                    "FromColumnName": "k",
                    "ToTableName": "B",
                    "ToColumnName": "k",
                    "IsActive": 0,
                    "Cardinality": "M:1",
                    "CrossFilteringBehavior": "Both",
                },
                {"FromTableName": "A"},
                "junk",
            ],
            "hierarchies": [{"table": "B", "name": "H", "levels": ["x", "y"]}],
            "calculation_groups": [
                {"table": "G", "precedence": 2, "items": [{"name": "I", "expression": "1"}]}
            ],
            "fieldParameters": ["P"],
            "partitions": [{"table": "A", "kind": "M", "expression": "let"}],
            "settings": {"culture": "en-US"},
            "tables": "not a list",
        }
    )
    assert structure.relationships == [
        RelationshipFact(
            "A",
            "k",
            "B",
            "k",
            active=False,
            both=True,
            from_many=True,
            to_many=False,
            cardinality_known=True,
        )
    ]
    assert structure.hierarchies[("b", "h")] == [("x", "x"), ("y", "y")]
    assert (
        structure.groups["g"]["precedence"] == 2 and structure.table_kind("P") == "fieldParameter"
    )
    assert "tables" not in structure.known and ModelStructure.from_facts(None).known == set()


# The shape ``pbix_extract.read_model_facts`` documents (``--facts``), trimmed to what matters here.
SAVED_FACTS: dict[str, Any] = {
    "version": 1,
    "model": {"culture": "en-GB", "discourageImplicitMeasures": True},
    "tables": [
        {
            "name": "FactSales",
            "hidden": False,
            "dataCategory": None,
            "isDateTable": False,
            "source": "m",
            "calculated": False,
            "calculationGroup": False,
        },
        {
            "name": "Date",
            "hidden": True,
            "dataCategory": "Time",
            "isDateTable": True,
            "dateColumn": "Date",
            "source": "calculated",
            "calculated": True,
            "expression": "CALENDARAUTO()",
            "calculationGroup": False,
        },
        {
            "name": "Metric",
            "hidden": False,
            "source": "calculated",
            "calculated": True,
            "expression": "{(\"a\", NAMEOF('# Measures'[Rank]), 0)}",
            "calculationGroup": False,
        },
        {
            "name": "Time Calc",
            "hidden": False,
            "source": "calculationGroup",
            "calculated": False,
            "calculationGroup": True,
        },
    ],
    "columns": [
        {
            "table": "FactSales",
            "name": "Margin",
            "kind": "calculated",
            "dataType": 8,
            "expression": "[SalesAmount] - [TotalCost]",
            "sortByColumn": None,
            "hidden": False,
        },
        {
            "table": "Date",
            "name": "Month",
            "kind": "data",
            "dataType": 2,
            "sortByColumn": "Month Number",
            "hidden": True,
        },
    ],
    "relationships": [
        {
            "name": "x",
            "fromTable": "FactSales",
            "fromColumn": "StoreKey",
            "toTable": "DimStore",
            "toColumn": "StoreKey",
            "fromCardinality": "many",
            "toCardinality": "one",
            "cardinality": "M:1",
            "crossFilter": "both",
            "active": True,
        },
        {
            "name": "y",
            "fromTable": "FactSales",
            "fromColumn": "DateKey",
            "toTable": "Date",
            "toColumn": "Date",
            "cardinality": "M:1",
            "crossFilter": "single",
            "active": False,
        },
    ],
    "hierarchies": [
        {
            "table": "Date",
            "name": "Calendar",
            "levels": [
                {"name": "Year", "column": "Year", "ordinal": 0},
                {"name": "Month", "column": "Month", "ordinal": 1},
            ],
        }
    ],
    "roles": [
        {
            "name": "Stores",
            "description": None,
            "permission": "read",
            "tables": [{"table": "DimStore", "filter": "[StoreKey] = 1"}],
        }
    ],
    "calculationGroups": [
        {
            "table": "Time Calc",
            "precedence": 2,
            "items": [{"name": "Current", "expression": "SELECTEDMEASURE()", "ordinal": 0}],
        }
    ],
    "fieldParameters": [
        {
            "table": "Metric",
            "labelColumn": "Metric",
            "fieldsColumn": "Metric Fields",
            "orderColumn": "Metric Order",
            "rows": [],
        }
    ],
    "partitions": [
        {
            "table": "FactSales",
            "name": "FactSales",
            "source": "m",
            "mode": "import",
            "expression": "let Source = 1 in Source",
        }
    ],
    "expressions": [
        {"name": "Folder", "kind": "m", "expression": '"F:\\\\Data"', "parameter": False}
    ],
}


def test_the_structure_reads_the_facts_pbix_extract_writes() -> None:
    structure = ModelStructure.from_facts(SAVED_FACTS)
    assert {
        "tables",
        "columns",
        "relationships",
        "hierarchies",
        "roles",
        "calculation_groups",
        "field_parameters",
        "partitions",
        "expressions",
        "settings",
    } <= structure.known
    assert [structure.table_kind(t) for t in ("FactSales", "Date", "Metric", "Time Calc")] == [
        "data",
        "calculated",
        "fieldParameter",
        "calculationGroup",
    ]
    assert (
        structure.tables["date"]["date_table"] is True
        and structure.tables["date"]["hidden"] is True
    )
    assert structure.columns[("factsales", "margin")]["sort_by"] == ""
    assert structure.columns[("date", "month")]["sort_by"] == "Month Number"
    found = structure.relationship(("FactSales", "StoreKey"), ("DimStore", "StoreKey"))
    assert found is not None and found == replace(
        found,
        from_table="FactSales",
        from_column="StoreKey",
        to_table="DimStore",
        to_column="StoreKey",
        active=True,
        both=True,
        from_many=True,
        to_many=False,
        cardinality_known=True,
    )
    assert structure.roles["stores"] == {"DimStore": "[StoreKey] = 1"}
    assert structure.partitions["factsales"] == [
        {
            "name": "FactSales",
            "kind": "m",
            "expression": "let Source = 1 in Source",
            "mode": "import",
        }
    ]
    assert structure.hierarchies[("date", "calendar")] == [("Year", "Year"), ("Month", "Month")]
    batch = [
        rem.CreateCalculatedColumn(
            table="FactSales", name="Margin", expression="[SalesAmount] - [TotalCost]"
        ),
        rem.SetColumnProperties(table="Date", name="Month", sort_by="Month Number", hidden=True),
        rem.UpdateRelationship(
            from_table="DimStore",
            from_column="StoreKey",
            to_table="FactSales",
            to_column="StoreKey",
            cross_filter="both",
        ),
        rem.MarkDateTable(table="Date", column="Date"),
        rem.CreateRole(
            name="Stores", filters=[rem.RoleFilter(table="DimStore", expression="[StoreKey]  =  1")]
        ),
        rem.SetTableQuery(table="FactSales", expression="let Source = 1 in Source"),
        rem.CreateFieldParameter(
            name="Metric", fields=[rem.ParameterField(table="# Measures", name="Rank")]
        ),
        rem.SetCalculationItem(group="Time Calc", name="Current", expression="SELECTEDMEASURE()"),
        rem.SetModelOptions(discourage_implicit_measures=True),
    ]
    assert saved_model_problems(batch, SAVED_FACTS) == ([], [])
    wrong = [
        rem.UpdateRelationship(
            from_table="FactSales",
            from_column="DateKey",
            to_table="Date",
            to_column="Date",
            active=True,
        ),
        rem.SetColumnProperties(table="FactSales", name="Margin", sort_by="StoreKey"),
    ]
    assert saved_model_problems(wrong, SAVED_FACTS)[0] == [
        "the relationship FactSales[DateKey] and Date[Date] is not active",
        "FactSales[Margin] is not sorted by [StoreKey]",
    ]


def test_a_model_view_keeps_its_structure_and_an_unknown_one_starts_empty() -> None:
    model = ModelInfo()
    assert model_structure(model).known == set()
    model_structure(model).known.add("roles")
    assert model_structure(model).known == {"roles"}
    rem.attach_structure(model, {"roles": []})
    assert model_structure(model).known == {"roles"} and model_structure(model).roles == {}


# --- DAX helpers ---------------------------------------------------------------------------------


def test_table_renames_in_dax_follow_every_spelling_but_not_functions_or_variables() -> None:
    text = (
        "VAR DimStore = 1 RETURN COUNTROWS(DimStore) + COUNTROWS('DimStore') + "
        "SUM(DimStore[StoreKey]) + DIMSTORE(1)"
    )
    renamed, count = rem.rename_dax_table(text, "dimstore", "Store's List")
    assert count == 2
    assert renamed == (
        "VAR DimStore = 1 RETURN COUNTROWS(DimStore) + COUNTROWS('Store''s List') + "
        "SUM('Store''s List'[StoreKey]) + DIMSTORE(1)"
    )
    assert rem.rename_dax_table("CALCULATE([M], ALL(Date))", "Date", "Calendar") == (
        "CALCULATE([M], ALL('Calendar'))",
        1,
    )
    assert rem.dax_uses_table("SUM('Top-Bottom-N'[Top-Bottom-N])", "top-bottom-n")
    assert not rem.dax_uses_table('"DimStore" & [x] // DimStore', "DimStore")


def test_column_renames_in_dax_only_touch_that_tables_column() -> None:
    text = "SUM(FactSales[Amount]) + SUM(Other[Amount]) + [Amount]"
    assert rem.rename_dax_column(text, "FactSales", "Amount", "Sales") == (
        "SUM(FactSales[Sales]) + SUM(Other[Amount]) + [Amount]",
        1,
    )
    assert rem.rename_dax_column(text, "FactSales", "Amount", "Sales", own_table="factsales") == (
        "SUM(FactSales[Sales]) + SUM(Other[Amount]) + [Sales]",
        2,
    )
    # In a table that also has a measure of that name, a bare name is the measure.
    assert (
        rem.rename_dax_column(
            "[Amount]",
            "FactSales",
            "Amount",
            "Sales",
            own_table="FactSales",
            measures=frozenset({"amount"}),
        )[1]
        == 0
    )


def test_userelationship_and_crossfilter_name_a_relationship() -> None:
    rel = RelationshipFact("FactSales", "DateKey", "Date", "Date")
    assert rem.dax_uses_relationship(
        "CALCULATE([S], USERELATIONSHIP('Date'[Date], FactSales[DateKey]))", rel
    )
    assert rem.dax_uses_relationship(
        "CALCULATE([S], CROSSFILTER(FactSales[DateKey], 'Date'[Date], BOTH))", rel
    )
    assert not rem.dax_uses_relationship(
        "CALCULATE([S], USERELATIONSHIP('Date'[Date], FactSales[ShipDate]))", rel
    )
    assert not rem.dax_uses_relationship("SUM(FactSales[DateKey]) + COUNTROWS('Date')", rel)


AUTO_DATE = {
    "field": {
        "HierarchyLevel": {
            "Expression": {
                "Hierarchy": {
                    "Expression": {
                        "PropertyVariationSource": {
                            "Expression": {"SourceRef": {"Entity": "FactSales"}},
                            "Name": "Variation",
                            "Property": "OrderDate",
                        }
                    },
                    "Hierarchy": "Date Hierarchy",
                }
            },
            "Level": "Year",
        }
    },
    "queryRef": "FactSales.OrderDate.Variation.Date Hierarchy.Year",
    "nativeQueryRef": "OrderDate Year",
    "active": True,
}


def auto_date_document() -> PbirDocument:
    """The twin with a visual on FactSales[OrderDate]'s automatic date hierarchy only."""
    document = twin()
    projections = visual(document, STORE_VISUAL)["visual"]["query"]["queryState"]["Values"]
    projections["projections"].append(copy.deepcopy(AUTO_DATE))
    return document


def auto_date_model() -> ModelInfo:
    model = example_model()
    model.columns["FactSales"]["OrderDate"] = ""
    model_structure(model).columns[("factsales", "orderdate")] = {
        "table": "FactSales",
        "name": "OrderDate",
        "data_type": 9,
    }
    return model


def variation_of(document: PbirDocument) -> dict[str, Any]:
    field = visual(document, STORE_VISUAL)["visual"]["query"]["queryState"]["Values"][
        "projections"
    ][-1]["field"]
    found: dict[str, Any] = field["HierarchyLevel"]["Expression"]["Hierarchy"]["Expression"][
        "PropertyVariationSource"
    ]
    return found


def test_an_automatic_date_hierarchy_counts_as_a_use_of_its_column() -> None:
    """Bug review #2 (r1/r7): HierarchyLevel > Hierarchy > PropertyVariationSource was invisible to
    renames, delete guards and the after-save check."""
    document = auto_date_document()
    assert ("Column", "FactSales", "OrderDate") in rem.field_references(document)
    assert rem.report_uses(
        document,
        lambda kind, body, entity: (
            kind == "Column" and entity == "FactSales" and body.get("Property") == "OrderDate"
        ),
    )
    with pytest.raises(DocumentError, match=r"FactSales\[OrderDate\] is used by"):
        check_model_operation(
            auto_date_document(),
            parse_model_operation(
                {"op": "delete_column", "table": "FactSales", "name": "OrderDate"}
            ),
            auto_date_model(),
        )
    renamed = auto_date_document()
    check_model_operation(
        renamed,
        parse_model_operation(
            {
                "op": "rename_column",
                "table": "FactSales",
                "name": "OrderDate",
                "new_name": "Ordered",
            }
        ),
        auto_date_model(),
    )
    assert variation_of(renamed)["Property"] == "Ordered"
    assert (
        rem.rename_column_references(auto_date_document(), "FactSales", "OrderDate", "Ordered") == 1
    )
    moved = auto_date_document()
    assert rem.rename_table_references(moved, "FactSales", "Sales") >= 1
    assert variation_of(moved)["Expression"]["SourceRef"]["Entity"] == "Sales"


def test_a_table_used_only_through_its_date_hierarchy_cannot_be_deleted() -> None:
    document = twin()
    projections = visual(document, STORE_VISUAL)["visual"]["query"]["queryState"]["Values"]
    only_date = copy.deepcopy(AUTO_DATE)
    variation = only_date["field"]["HierarchyLevel"]["Expression"]["Hierarchy"]["Expression"]
    variation["PropertyVariationSource"]["Expression"]["SourceRef"]["Entity"] = "DimPromotion"
    projections["projections"].append(only_date)
    with pytest.raises(DocumentError, match="'DimPromotion' is used by"):
        check_model_operation(
            document,
            parse_model_operation({"op": "delete_table", "name": "DimPromotion"}),
            example_model(),
        )


def test_deleting_the_table_that_holds_a_roles_only_filter_is_refused_before_desktop() -> None:
    model = example_model()
    model_structure(model).roles["guard"] = {"DimPromotion": "[PromotionKey] = 1"}
    model_structure(model).role_names["guard"] = "Guard"
    refuse(
        {"op": "delete_table", "name": "DimPromotion"},
        "holds the only filter of the role 'Guard'",
        model,
    )


def test_security_filter_and_referential_integrity_follow_desktops_rules() -> None:
    ends = {
        "from_table": "FactSales",
        "from_column": "PromotionKey",
        "to_table": "DimPromotion",
        "to_column": "PromotionKey",
    }
    refuse({"op": "create_relationship", **ends, "security_filter": "both"}, "in both directions")
    model = example_model()
    model_structure(model).partitions["dimpromotion"] = [
        {"name": "DimPromotion", "kind": "m", "expression": "", "mode": "import"}
    ]
    refuse(
        {"op": "create_relationship", **ends, "assume_referential_integrity": True},
        "not DirectQuery",
        model,
    )
    for table in ("factsales", "dimpromotion"):
        model_structure(model).partitions[table] = [
            {"name": table, "kind": "m", "expression": "", "mode": "directquery"}
        ]
    refuse(
        {"op": "create_relationship", **ends, "assume_referential_integrity": True},
        "cannot verify the same DirectQuery source group or nonblank, matching",
        model,
    )


def test_marking_a_date_table_says_its_automatic_hierarchies_stay() -> None:
    result = run({"op": "mark_date_table", "table": "Date", "column": "Date"})
    assert any("automatic date hierarchy" in w for w in result.warnings)
    model = example_model()
    model_structure(model).known.add("variations")
    assert not run({"op": "mark_date_table", "table": "Date", "column": "Date"}, model).warnings


def test_every_card_has_three_examples_a_capability_and_list_slots_build_lists() -> None:
    """Spec review #7."""
    for card in TOOL_CARDS:
        assert len(card.examples) >= 3, card.op
        assert card.capability, card.op
    by_op = {card.op: card for card in TOOL_CARDS}
    assert by_op["delete_column"].capability == "model"
    assert by_op["create_role"].capability == "rls"
    assert (by_op["set_table_query"].planner, by_op["set_named_expression"].planner) == (
        False,
        False,
    )
    group = by_op["create_calculation_group"].to_operation(
        {"name": "TI", "items": "Current, YTD and PY", "date": {"table": "Date", "name": "Date"}}
    )
    assert group["items"] == [
        {"name": "Current", "expression": "SELECTEDMEASURE()"},
        {"name": "YTD", "expression": "CALCULATE(SELECTEDMEASURE(), DATESYTD('Date'[Date]))"},
        {
            "name": "PY",
            "expression": "CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR('Date'[Date]))",
        },
    ]
    parse_model_operation(group)
    undated = by_op["create_calculation_group"].to_operation({"name": "TI", "items": "YTD"})
    with pytest.raises(ValidationError):  # a time item without a date column is not guessed
        parse_model_operation(undated)
    role = by_op["create_role"].to_operation(
        {"name": "Stores", "filter": "'DimStore'[StoreType] = \"Store\""}
    )
    assert role == {
        "op": "create_role",
        "name": "Stores",
        "filters": [{"table": "DimStore", "expression": "'DimStore'[StoreType] = \"Store\""}],
    }
    assert by_op["update_role"].to_operation(
        {"name": "R", "table": "DimChannel", "filter": ""}
    ) == {"op": "update_role", "name": "R", "filters": [{"table": "DimChannel", "expression": ""}]}
    source = by_op["set_named_expression"].examples[0][1]
    assert source["expression"] == '"F:\\Data"'  # the M text F:\Data (M has no escapes)


def test_field_references_resolve_from_aliases() -> None:
    found = rem.field_references(twin())
    assert ("Column", "Top-Bottom-N", "Top-Bottom-N") in found
    assert found.count(("Column", "Actuals-vs-PctChange", "Top-Bottom-Field")) == 3
    assert ("Measure", "# Measures", "Rank") in found


# --- did Desktop keep it -------------------------------------------------------------------------


def saved_after(operations: list[Any]) -> dict[str, Any]:
    """The facts of a model the batch was applied to, through the runner over the fake engine."""
    from fake_tom import Server

    from ai_powerbi_analyst.studio.desktop_model import apply_model_operations

    model = Model.from_state(agents_state())
    apply_model_operations(operations, server="localhost:1", runner=Server(model))
    return snapshot_of(model)


BATCH = [
    rem.CreateCalculatedColumn(
        table="FactSales", name="Margin", expression="FactSales[SalesAmount] - FactSales[TotalCost]"
    ),
    rem.SetColumnProperties(table="Date", name="Month", sort_by="Month Number", hidden=True),
    rem.RenameColumn(table="DimStore", name="StoreType", new_name="Type"),
    rem.CreateRelationship(
        from_table="DimStore",
        from_column="StoreKey",
        to_table="DimEmployee",
        to_column="EmployeeKey",
        active=False,
    ),
    rem.UpdateRelationship(
        from_table="FactSales",
        from_column="StoreKey",
        to_table="DimStore",
        to_column="StoreKey",
        cross_filter="both",
    ),
    rem.CreateHierarchy(
        table="Date",
        name="Calendar",
        levels=[rem.HierarchyLevel(column="Year"), rem.HierarchyLevel(column="Month")],
    ),
    rem.CreateRole(
        name="Stores", filters=[rem.RoleFilter(table="DimStore", expression="[StoreKey] = 1")]
    ),
    rem.CreateCalculationGroup(
        name="Time Calc",
        items=[rem.CalculationItemSpec(name="Current", expression="SELECTEDMEASURE()")],
        precedence=2,
    ),
    rem.SetNamedExpression(name="Folder", expression='"F:\\Data"'),
    rem.SetModelOptions(discourage_implicit_measures=True),
    rem.DeleteTable(name="DimChannel"),
]


def test_a_saved_model_that_kept_the_batch_passes_the_diff() -> None:
    batch = [op for op in BATCH if not isinstance(op, rem.DeleteTable)] + [
        rem.RenameTable(name="DimEmployee", new_name="Employees")
    ]
    facts = saved_after(copy.deepcopy(batch))
    assert saved_model_problems(batch, facts) == ([], [])


def test_a_saved_model_without_the_batch_names_what_is_missing() -> None:
    unchanged = snapshot_of(Model.from_state(agents_state()))
    problems, unchecked = saved_model_problems(BATCH, unchanged)
    assert unchecked == []
    assert problems == [
        "the column FactSales[Margin] is missing",
        "Date[Month] is not hidden",
        "Date[Month] is not sorted by [Month Number]",
        "the column DimStore[Type] is missing",
        "the column DimStore[StoreType] is still there",
        "the relationship between DimStore[StoreKey] and DimEmployee[EmployeeKey] is missing",
        "the relationship FactSales[StoreKey] and DimStore[StoreKey] does not filter as asked",
        "the hierarchy 'Calendar' of Date is missing",
        "the role 'Stores' is missing",
        "the table 'Time Calc' is missing",
        "the query 'Folder' is missing",
        "the implicit measures setting was not kept",
        "the table 'DimChannel' is still there",
    ]


def test_sections_the_facts_lack_are_reported_as_unchecked_not_failed() -> None:
    problems, unchecked = saved_model_problems(
        BATCH, {"columns": [{"table": "FactSales", "name": "Margin"}]}
    )
    assert problems == ["the column Date[Month] is missing", "the column DimStore[Type] is missing"]
    assert unchecked == [
        "formulas",
        "relationships",
        "hierarchies",
        "roles",
        "tables",
        "calculation groups",
        "queries and parameters",
        "settings",
    ]


def test_a_step_a_later_step_renames_is_checked_under_its_new_name() -> None:
    batch = [
        rem.CreateCalculatedColumn(table="FactSales", name="Margin", expression="1"),
        rem.RenameColumn(table="FactSales", name="Margin", new_name="Profit"),
    ]
    facts = {"columns": [{"table": "FactSales", "name": "Profit", "expression": "1"}]}
    assert saved_model_problems(batch, facts) == ([], [])
    kept_name = {"columns": [{"table": "FactSales", "name": "Margin", "expression": "1"}]}
    assert saved_model_problems(batch, kept_name)[0] == [
        "the column FactSales[Profit] is missing",
        "the column FactSales[Margin] is still there",
    ]


def test_names_are_carried_through_later_renames_of_their_table() -> None:
    """The live probe's false alarm (run 2): a role created on DimChannel, then DimChannel renamed
    in a later batch; the saved role filter sits on 'Studio Channel'."""
    batch = [
        rem.CreateRole(
            name="Probe", filters=[rem.RoleFilter(table="DimChannel", expression='[C] <> "x"')]
        ),
        rem.CreateCalculatedColumn(table="DimChannel", name="Up", expression="UPPER([C])"),
        rem.CreateHierarchy(table="DimChannel", name="H", levels=[rem.HierarchyLevel(column="C")]),
        rem.CreateRelationship(
            from_table="FactSales",
            from_column="channelKey",
            to_table="DimChannel",
            to_column="K",
        ),
        rem.RenameColumn(table="DimChannel", name="C", new_name="Channel"),
        rem.RenameTable(name="DimChannel", new_name="Studio Channel"),
    ]
    facts = {
        "tables": [{"name": "Studio Channel", "hidden": False}, {"name": "FactSales"}],
        "columns": [
            {"table": "Studio Channel", "name": "Channel"},
            {"table": "Studio Channel", "name": "K"},
            {"table": "Studio Channel", "name": "Up", "expression": "UPPER([Channel])"},
        ],
        "relationships": [
            {
                "fromTable": "FactSales",
                "fromColumn": "channelKey",
                "toTable": "Studio Channel",
                "toColumn": "K",
                "crossFilter": "single",
                "active": True,
                "relyOnReferentialIntegrity": False,
                "securityFilter": "single",
                "fromCardinality": "many",
                "toCardinality": "one",
            }
        ],
        "hierarchies": [
            {"table": "Studio Channel", "name": "H", "levels": [{"name": "C", "column": "Channel"}]}
        ],
        "roles": [
            {"name": "Probe", "tables": [{"table": "Studio Channel", "filter": '[Channel] <> "x"'}]}
        ],
    }
    assert saved_model_problems(batch, facts) == ([], [])
    facts["roles"] = [{"name": "Probe", "tables": [{"table": "DimChannel", "filter": "x"}]}]
    assert saved_model_problems(batch, facts)[0] == [
        "the filter of 'Probe' on Studio Channel was not kept"
    ]


def test_every_property_a_step_sets_is_compared() -> None:
    """Bug review #8 / spec #4: format, summarize-by, category, folder, key, type, cardinality,
    referential integrity, security filter, precedence, item formulas and format strings,
    fields, parameter values, descriptions."""
    batch = [
        rem.SetColumnProperties(
            table="T",
            name="C",
            format_string="0.0",
            summarize_by="none",
            data_category="web_url",
            folder="F",
            key=True,
            description="d",
        ),
        rem.CreateCalculatedColumn(table="T", name="N", expression="1", data_type="whole_number"),
        rem.CreateRelationship(
            from_table="T",
            from_column="C",
            to_table="U",
            to_column="K",
            cardinality="one_to_one",
            cross_filter="both",
            security_filter="both",
        ),
        rem.CreateCalculationGroup(
            name="G",
            precedence=3,
            items=[
                rem.CalculationItemSpec(
                    name="Double",
                    expression="SELECTEDMEASURE() * 2",
                    format_string_expression='"0"',
                )
            ],
        ),
        rem.UpdateFieldParameter(
            name="P",
            fields=[
                rem.ParameterField(table="T", name="C"),
                rem.ParameterField(table="U", name="K"),
            ],
        ),
        rem.SetQueryParameter(name="Start", value="2025-01-01"),
        rem.SetTableProperties(table="U", description="the u table"),
    ]
    wrong = {
        "tables": [
            {"name": "T"},
            {"name": "U", "description": "old"},
            {"name": "G", "calculationGroup": True},
            {"name": "P"},
        ],
        "columns": [
            {
                "table": "T",
                "name": "C",
                "formatString": "0.00",
                "summarizeBy": "sum",
                "dataCategory": "ImageUrl",
                "displayFolder": "",
                "key": False,
                "description": "",
            },
            {"table": "T", "name": "N", "expression": "1", "dataType": 8},
        ],
        "relationships": [
            {
                "fromTable": "T",
                "fromColumn": "C",
                "toTable": "U",
                "toColumn": "K",
                "crossFilter": "both",
                "fromCardinality": "many",
                "toCardinality": "one",
                "relyOnReferentialIntegrity": False,
                "securityFilter": "single",
            }
        ],
        "calculationGroups": [
            {
                "table": "G",
                "precedence": 4,
                "items": [
                    {
                        "name": "Double",
                        "expression": "SELECTEDMEASURE()",
                        "formatStringExpression": "",
                    }
                ],
            }
        ],
        "fieldParameters": [{"table": "P", "rows": [{"table": "T", "name": "C", "label": "C"}]}],
        "expressions": [
            {
                "name": "Start",
                "expression": "#datetime(2024, 1, 1, 0, 0, 0) meta [IsParameterQuery=true, "
                'Type="DateTime", IsParameterQueryRequired=true]',
            }
        ],
    }
    problems, unchecked = saved_model_problems(batch, wrong)
    assert problems == [
        "the format string of T[C] was not kept",
        "the data category of T[C] was not kept",
        "the summarization of T[C] was not kept",
        "the folder of T[C] was not kept",
        "the description of T[C] was not kept",
        "T[C] is not a key column",
        "the data type of T[N] was not kept",
        "the relationship T[C] and U[K] is not one-to-one",
        "the relationship T[C] and U[K] does not apply the security filter as asked",
        "the precedence of 'G' is not 3",
        "the formula of the calculation item 'Double' was not kept",
        "the format string of the calculation item 'Double' was not kept",
        "the field parameter 'P' does not list the fields asked for",
        "the parameter 'Start' does not hold the new value",
        "the description of 'U' was not kept",
    ]
    assert unchecked == []
    # A property the facts do not show is unchecked, never passed.
    silent = copy.deepcopy(wrong)
    silent["relationships"][0].pop("securityFilter")
    silent["relationships"][0].pop("relyOnReferentialIntegrity")
    for column in silent["columns"]:
        column.pop("formatString", None)
    _, unchecked = saved_model_problems(batch, silent)
    assert unchecked == ["column formats", "referential integrity", "security filter directions"]


def test_a_later_delete_or_a_later_change_of_the_same_property_wins() -> None:
    batch = [
        rem.SetColumnProperties(table="T", name="C", hidden=True, format_string="0"),
        rem.SetColumnProperties(table="T", name="C", hidden=False),
        rem.CreateCalculatedColumn(table="T", name="Gone", expression="1"),
        rem.DeleteColumn(table="T", name="Gone"),
        rem.RenameColumn(table="T", name="C", new_name="Old"),
        rem.CreateCalculatedColumn(table="T", name="C", expression="2"),
    ]
    facts = {
        "columns": [
            {"table": "T", "name": "Old", "hidden": False, "formatString": "0"},
            {"table": "T", "name": "C", "expression": "2"},
        ]
    }
    assert saved_model_problems(batch, facts) == ([], [])


# --- the real Power Query parser -----------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed here")
def test_the_real_parser_accepts_m_and_names_a_syntax_error() -> None:
    assert (
        rem.check_m_with_validator(
            'let Source = Csv.Document(File.Contents("F:\\Data\\a.csv")) in Source'
        )
        == []
    )
    problems = rem.check_m_with_validator("let Source = in")
    assert problems and problems[0].startswith("Invalid M syntax")


def test_the_parser_script_is_where_the_checker_looks() -> None:
    assert (Path(rem.__file__).resolve().parents[1] / "llm" / "m_validation.cjs").is_file()
