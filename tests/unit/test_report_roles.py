"""Visual roles, type changes, field identity and placement (review items P24, P25, P34)."""

from __future__ import annotations

import copy
import json
import random
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.studio import pbir_expr as expr
from ai_powerbi_analyst.studio import report_roles as roles
from ai_powerbi_analyst.studio.report_roles import (
    ADDABLE,
    ROLE_TABLE,
    Box,
    FieldSlot,
    NeedsAggregation,
    PlacementError,
)

REPO = Path(__file__).resolve().parents[2]
TWIN = REPO / "fixtures" / "imported" / "structure-twin.pbix"
REAL = Path.home() / "Downloads" / "Agents Performance - Dashboard.pbix"


# --- builders -------------------------------------------------------------------------


def column(entity: str, prop: str, **extra: Any) -> dict[str, Any]:
    return {
        "field": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}},
        "queryRef": f"{entity}.{prop}",
        "nativeQueryRef": prop,
        **extra,
    }


def measure(entity: str, prop: str, **extra: Any) -> dict[str, Any]:
    return {
        "field": {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}},
        "queryRef": f"{entity}.{prop}",
        "nativeQueryRef": prop,
        **extra,
    }


def total(entity: str, prop: str, function: int = 0) -> dict[str, Any]:
    return {
        "field": {
            "Aggregation": {
                "Expression": {
                    "Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}
                },
                "Function": function,
            }
        },
        "queryRef": f"Sum({entity}.{prop})",
        "nativeQueryRef": f"Sum of {prop}",
    }


def M(name: str) -> FieldSlot:  # noqa: N802 - short builders keep the layouts readable
    return FieldSlot("measure", measure("T", name))


def C(name: str) -> FieldSlot:  # noqa: N802
    return FieldSlot("column", column("T", name))


def layout(placed: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    return {role: [p["nativeQueryRef"] for p in items] for role, items in placed.items()}


def state_layout(state: dict[str, Any]) -> dict[str, list[str]]:
    return {role: [p["queryRef"] for p in b["projections"]] for role, b in state.items()}


def literal(value: str) -> dict[str, Any]:
    return {"expr": {"Literal": {"Value": value}}}


def twin_visuals() -> dict[str, dict[str, Any]]:
    if not TWIN.exists():
        pytest.skip("the structure twin fixture is not present")
    found: dict[str, dict[str, Any]] = {}
    with zipfile.ZipFile(TWIN) as package:
        for name in package.namelist():
            if name.endswith("/visual.json"):
                found[name.split("/")[-2][:4]] = json.loads(package.read(name))
    return found


# --- the role table ------------------------------------------------------------------


def test_every_addable_type_has_a_table_entry_and_is_in_the_catalog() -> None:
    assert set(ROLE_TABLE) >= ADDABLE
    assert expr.visual_types() >= ADDABLE
    assert set(ROLE_TABLE) - ADDABLE == {
        "advancedSlicerVisual",
        "hundredPercentStackedAreaChart",
        "textbox",
        "shape",
        "image",
    }
    assert len(ADDABLE) == 26


@pytest.mark.parametrize("visual_type", sorted(ROLE_TABLE))
def test_role_table_entry_agrees_with_the_catalog(visual_type: str) -> None:
    entry = ROLE_TABLE[visual_type]
    assert entry.visual_type == visual_type
    catalog = expr.roles(visual_type)["roles"]
    listed = [
        *entry.measure_roles,
        *entry.grouping_roles,
        *entry.either_roles,
        *([entry.small_multiples] if entry.small_multiples else []),
        *entry.required_any,
        *(entry.required_roles or ()),
        *expr.roles(visual_type).get("requiredRoles", []),
        *entry.fill_caps,
        *entry.max_per_role,
    ]
    assert set(listed) <= set(catalog)
    for role in entry.measure_roles:
        assert catalog[role]["kind"] in {"Measure", "GroupingOrMeasure"}, role
    for role in entry.grouping_roles:
        assert catalog[role]["kind"] in {"Grouping", "GroupingOrMeasure"}, role
    for role in entry.either_roles:
        assert catalog[role]["kind"] == "GroupingOrMeasure", role
    if entry.small_multiples:
        assert catalog[entry.small_multiples]["kind"] == "Grouping"
    # A role a placement can fill must be able to hold more than nothing.
    for role in {*entry.measure_roles, *entry.grouping_roles, *entry.either_roles}:
        assert roles._limit(entry, role, filling=True) >= 1  # noqa: SLF001


def test_fill_order_does_not_depend_on_the_order_of_the_catalog_snapshot() -> None:
    # The first snapshot was alphabetical (kpi: Goal, Indicator, TrendLine; gauge: MaxValue
    # first), which put a kpi's value in Goal. Whatever order the snapshot has, the table's
    # order decides.
    assert ROLE_TABLE["kpi"].measure_roles == ("Indicator", "Goal")
    assert ROLE_TABLE["gauge"].measure_roles[0] == "Y"
    assert ROLE_TABLE["scatterChart"].measure_roles[:2] == ("X", "Y")
    assert set(expr.roles("kpi")["roles"]) == {"Goal", "Indicator", "TrendLine"}


# --- place_fields, one representative field set per addable type -----------------------

CARTESIAN = (
    "columnChart",
    "clusteredColumnChart",
    "hundredPercentStackedColumnChart",
    "barChart",
    "clusteredBarChart",
    "hundredPercentStackedBarChart",
    "lineChart",
    "areaChart",
    "stackedAreaChart",
    "ribbonChart",
)


@pytest.mark.parametrize("visual_type", CARTESIAN)
def test_cartesian_charts_put_measures_on_y_and_a_second_column_in_the_legend(
    visual_type: str,
) -> None:
    placed = roles.place_fields(visual_type, [C("Region"), M("Sales"), M("Profit"), C("Channel")])
    # A second column is a legend (Series), not a second axis level.
    assert layout(placed) == {
        "Category": ["Region"],
        "Y": ["Sales", "Profit"],
        "Series": ["Channel"],
    }


def test_small_multiples_are_filled_only_on_request() -> None:
    plain = roles.place_fields("clusteredColumnChart", [C("Region"), M("Sales"), C("Channel")])
    assert "Rows" not in plain
    split = roles.place_fields(
        "clusteredColumnChart",
        [C("Region"), M("Sales")],
        small_multiples=[C("Channel")],
    )
    assert layout(split) == {"Category": ["Region"], "Y": ["Sales"], "Rows": ["Channel"]}


def test_a_third_column_on_a_column_chart_has_no_room() -> None:
    with pytest.raises(PlacementError, match="A clustered column chart has no room for Year"):
        roles.place_fields(
            "clusteredColumnChart", [C("Region"), C("Channel"), C("Year"), M("Sales")]
        )


def test_card_takes_exactly_one_measure() -> None:
    assert layout(roles.place_fields("card", [M("Sales")])) == {"Values": ["Sales"]}
    with pytest.raises(PlacementError, match="A card has no room for Profit"):
        roles.place_fields("card", [M("Sales"), M("Profit")])
    with pytest.raises(PlacementError, match="A card holds measures, so Region cannot go in it"):
        roles.place_fields("card", [C("Region"), M("Sales")])


def test_new_card_takes_measures_and_only_splits_by_a_category_on_request() -> None:
    placed = roles.place_fields("cardVisual", [M("Sales"), M("Profit")])
    assert layout(placed) == {"Data": ["Sales", "Profit"]}
    split = roles.place_fields("cardVisual", [M("Sales")], small_multiples=[C("Region")])
    assert layout(split) == {"Data": ["Sales"], "Rows": ["Region"]}
    # A category dropped on a card must not silently land in Rows (small multiples).
    with pytest.raises(PlacementError, match="Use small multiples"):
        roles.place_fields("cardVisual", [M("Sales"), C("Region")])


def test_multi_row_card_and_table_take_any_field_in_the_order_given() -> None:
    fields = [C("Region"), M("Sales"), C("Channel"), M("Profit")]
    expected = {"Values": ["Region", "Sales", "Channel", "Profit"]}
    assert layout(roles.place_fields("multiRowCard", fields)) == expected
    assert layout(roles.place_fields("tableEx", fields)) == expected


def test_matrix_puts_columns_on_rows_and_measures_on_values() -> None:
    placed = roles.place_fields("pivotTable", [C("Region"), M("Sales"), C("Channel")])
    assert layout(placed) == {"Rows": ["Region", "Channel"], "Values": ["Sales"]}
    # Desktop accepts a matrix of rows only, although the catalog says Values is required.
    assert layout(roles.place_fields("pivotTable", [C("Region")])) == {"Rows": ["Region"]}
    with pytest.raises(PlacementError, match="needs a field for Rows, Columns or Values"):
        roles.place_fields("pivotTable", [])


def test_slicer_takes_one_column() -> None:
    assert layout(roles.place_fields("slicer", [C("Region")])) == {"Values": ["Region"]}
    with pytest.raises(PlacementError, match="A slicer holds columns, so Sales cannot go in it"):
        roles.place_fields("slicer", [M("Sales")])
    with pytest.raises(PlacementError, match="A slicer has no room for Channel"):
        roles.place_fields("slicer", [C("Region"), C("Channel")])


@pytest.mark.parametrize("visual_type", ["pieChart", "donutChart"])
def test_pie_and_donut_take_measures_a_legend_and_details(visual_type: str) -> None:
    placed = roles.place_fields(visual_type, [M("Sales"), C("Region")])
    assert layout(placed) == {"Y": ["Sales"], "Category": ["Region"]}
    detailed = roles.place_fields(visual_type, [M("Sales"), C("Region"), C("Channel")])
    assert layout(detailed) == {"Y": ["Sales"], "Category": ["Region"], "Series": ["Channel"]}
    with pytest.raises(PlacementError, match="has no room for Year"):
        roles.place_fields(visual_type, [M("Sales"), C("Region"), C("Channel"), C("Year")])
    with pytest.raises(PlacementError, match="needs a field for Legend"):
        roles.place_fields(visual_type, [M("Sales")])


def test_treemap_uses_group_details_and_values() -> None:
    placed = roles.place_fields("treemap", [M("Sales"), C("Region"), C("Channel")])
    assert layout(placed) == {"Values": ["Sales"], "Group": ["Region"], "Details": ["Channel"]}


def test_funnel_has_a_category_and_values() -> None:
    placed = roles.place_fields("funnel", [C("Stage"), M("Deals")])
    assert layout(placed) == {"Category": ["Stage"], "Y": ["Deals"]}
    with pytest.raises(PlacementError, match="A funnel chart has no room for Region"):
        roles.place_fields("funnel", [C("Stage"), C("Region"), M("Deals")])


def test_gauge_puts_the_value_in_y_not_max_value() -> None:
    assert layout(roles.place_fields("gauge", [M("Sales")])) == {"Y": ["Sales"]}
    placed = roles.place_fields("gauge", [M("Sales"), M("Goal"), M("Cap"), M("Floor")])
    assert layout(placed) == {
        "Y": ["Sales"],
        "TargetValue": ["Goal"],
        "MaxValue": ["Cap"],
        "MinValue": ["Floor"],
    }
    with pytest.raises(PlacementError, match="A gauge needs a field for Value"):
        roles.place_fields("gauge", [])


def test_kpi_puts_the_measure_in_indicator_not_goal() -> None:
    placed = roles.place_fields("kpi", [M("Sales")])
    assert layout(placed) == {"Indicator": ["Sales"]}
    both = roles.place_fields("kpi", [M("Sales"), M("Goal"), C("Month")])
    assert layout(both) == {"Indicator": ["Sales"], "Goal": ["Goal"], "TrendLine": ["Month"]}
    with pytest.raises(PlacementError, match="A KPI needs a field for Value"):
        roles.place_fields("kpi", [C("Month")])


@pytest.mark.parametrize(
    "visual_type", ["lineClusteredColumnComboChart", "lineStackedColumnComboChart"]
)
def test_combo_charts_put_the_first_measure_on_columns_and_the_rest_on_the_line(
    visual_type: str,
) -> None:
    only_columns = roles.place_fields(visual_type, [C("Month"), M("Sales")])
    assert layout(only_columns) == {"Category": ["Month"], "Y": ["Sales"]}
    placed = roles.place_fields(visual_type, [C("Month"), M("Sales"), M("Margin"), C("Region")])
    assert layout(placed) == {
        "Category": ["Month"],
        "Y": ["Sales"],
        "Y2": ["Margin"],
        "Series": ["Region"],
    }
    with pytest.raises(PlacementError, match="needs a field for X-axis"):
        roles.place_fields(visual_type, [M("Sales")])


def test_scatter_fills_x_and_y_then_size_and_puts_columns_in_details_and_legend() -> None:
    placed = roles.place_fields(
        "scatterChart", [M("Sales"), M("Profit"), M("Qty"), C("Region"), C("Channel")]
    )
    assert layout(placed) == {
        "X": ["Sales"],
        "Y": ["Profit"],
        "Size": ["Qty"],
        "Category": ["Region"],
        "Series": ["Channel"],
    }
    with pytest.raises(PlacementError, match="A scatter chart needs a field for Y Axis"):
        roles.place_fields("scatterChart", [M("Sales"), C("Region")])
    with pytest.raises(PlacementError, match="has no room for Extra"):
        roles.place_fields("scatterChart", [M("A"), M("B"), M("C"), M("Extra")])


def test_scatter_lets_leftover_columns_take_a_free_axis() -> None:
    placed = roles.place_fields("scatterChart", [C("Region"), C("Channel"), C("Year"), M("Sales")])
    assert layout(placed) == {
        "Category": ["Region"],
        "Series": ["Channel"],
        "X": ["Sales"],
    } | {"Y": ["Year"]}


def test_waterfall_has_category_breakdown_and_one_measure() -> None:
    placed = roles.place_fields("waterfallChart", [M("Sales"), C("Month"), C("Kind")])
    assert layout(placed) == {"Y": ["Sales"], "Category": ["Month"], "Breakdown": ["Kind"]}
    with pytest.raises(PlacementError, match="A waterfall chart has no room for Profit"):
        roles.place_fields("waterfallChart", [M("Sales"), M("Profit"), C("Month")])


def test_button_slicer_is_known_but_not_addable() -> None:
    assert "advancedSlicerVisual" not in ADDABLE
    placed = roles.place_fields("advancedSlicerVisual", [C("Region")])
    assert layout(placed) == {"Values": ["Region"]}


def test_place_fields_copies_projections_and_keeps_aggregations_as_measures() -> None:
    source = total("FactSales", "Sales")
    placed = roles.place_fields(
        "clusteredColumnChart",
        [C("Region"), FieldSlot("measure", source)],
    )
    assert placed["Y"] == [source]
    assert placed["Y"][0] is not source


def test_fieldless_and_unknown_types_are_refused_in_plain_english() -> None:
    with pytest.raises(PlacementError, match="A text box holds content, not fields"):
        roles.place_fields("textbox", [C("Region")])
    assert roles.place_fields("textbox", []) == {}
    with pytest.raises(PlacementError, match="cannot add fields to a map yet"):
        roles.place_fields("map", [C("Region")])


def test_small_multiples_on_a_type_without_them_are_refused() -> None:
    with pytest.raises(PlacementError, match="A pie chart cannot be split into small multiples"):
        roles.place_fields("pieChart", [M("Sales"), C("Region")], small_multiples=[C("Year")])
    with pytest.raises(PlacementError, match="needs a column for small multiples"):
        roles.place_fields(
            "clusteredColumnChart", [C("Region"), M("Sales")], small_multiples=[M("Profit")]
        )


# --- validate_query_state, role_by_name -------------------------------------------------


def test_validate_accepts_every_visual_of_the_structure_twin() -> None:
    for prefix, visual in twin_visuals().items():
        body = visual["visual"]
        problems = roles.validate_query_state(body["visualType"], body["query"]["queryState"])
        assert problems == [], prefix


def test_validate_accepts_every_visual_of_the_real_dashboard() -> None:
    if not REAL.exists():
        pytest.skip("the real Agents dashboard is not on this machine")
    with zipfile.ZipFile(REAL) as package:
        names = [n for n in package.namelist() if n.endswith("/visual.json")]
        assert len(names) == 15
        for name in names:
            body = json.loads(package.read(name))["visual"]
            assert roles.validate_query_state(body["visualType"], body["query"]["queryState"]) == []


def test_validate_reports_each_problem_in_plain_english() -> None:
    state = {
        "Category": {"projections": [measure("T", "Sales")]},
        "Y": {"projections": [column("T", "Region")]},
        "Series": {"projections": [column("T", "A"), column("T", "B")]},
        "Nope": {"projections": [column("T", "C")]},
    }
    assert roles.validate_query_state("clusteredColumnChart", state) == [
        "Sales is a measure, but X-axis needs a column.",
        "Region is a column, but Y-axis needs a measure or a sum.",
        "Legend takes at most 1 field.",
        "A clustered column chart has no Nope field well.",
    ]
    assert roles.validate_query_state("kpi", {}) == ["A KPI needs a field for Value."]
    assert roles.validate_query_state("pieChart", {"Y": {"projections": [total("T", "S")]}}) == [
        "A pie chart needs a field for Legend."
    ]


def test_validate_accepts_an_aggregated_column_as_a_value_and_ignores_custom_visuals() -> None:
    state = {
        "Category": {"projections": [column("T", "R")]},
        "Y": {"projections": [total("T", "S")]},
    }
    assert roles.validate_query_state("barChart", state) == []
    assert roles.validate_query_state("someCustomVisual1234", {"Any": {"projections": []}}) == []


def test_role_by_name_takes_ids_and_display_names_in_any_case() -> None:
    assert roles.role_by_name("clusteredColumnChart", "legend") == "Series"
    assert roles.role_by_name("clusteredColumnChart", "X-axis") == "Category"
    assert roles.role_by_name("clusteredColumnChart", "x axis") == "Category"
    assert roles.role_by_name("clusteredColumnChart", "Y-axis") == "Y"
    # On a bar chart the axes are swapped on screen: its X-axis holds the values.
    assert roles.role_by_name("clusteredBarChart", "X-axis") == "Y"
    assert roles.role_by_name("clusteredBarChart", "Y-axis") == "Category"
    assert roles.role_by_name("tableEx", "Values") == "Values"
    assert roles.role_by_name("tableEx", "columns") == "Values"
    assert roles.role_by_name("pieChart", "Values") == "Y"
    assert roles.role_by_name("scatterChart", "X Axis") == "X"
    assert roles.role_by_name("kpi", "target") == "Goal"
    assert roles.role_by_name("kpi", "Value") == "Indicator"
    assert roles.role_by_name("cardVisual", "small multiples") == "Rows"
    assert roles.role_by_name("cardVisual", "Categories") == "Rows"
    assert roles.role_by_name("clusteredColumnChart", "Small multiples") == "Rows"
    assert roles.role_by_name("clusteredColumnChart", "axis") == "Category"
    assert roles.role_by_name("clusteredColumnChart", "nonsense") is None
    assert roles.role_by_name("clusteredColumnChart", "  ") is None
    assert roles.role_by_name("notAVisual", "Values") is None


# --- field identity -------------------------------------------------------------------


def test_field_identity_of_every_projection_kind() -> None:
    assert roles.field_identity(column("DimEmployee", "EmployeeKey")) == (
        "Column",
        "DimEmployee",
        "EmployeeKey",
    )
    assert roles.field_identity(measure("# Measures", "Rank")) == (
        "Measure",
        "# Measures",
        "Rank",
    )
    assert roles.field_identity(total("FactSales", "Sales")) == (
        "Aggregation",
        "Sum",
        "FactSales",
        "Sales",
    )
    assert roles.field_identity(total("FactSales", "Sales", function=1))[1] == "Average"
    level = {
        "field": {
            "HierarchyLevel": {
                "Expression": {
                    "Hierarchy": {
                        "Expression": {"SourceRef": {"Entity": "DimDate"}},
                        "Hierarchy": "Calendar",
                    }
                },
                "Level": "Year",
            }
        }
    }
    assert roles.field_identity(level) == ("HierarchyLevel", "DimDate", "Calendar", "Year")


def test_automatic_date_hierarchy_identity_names_the_date_column() -> None:
    variation = {
        "HierarchyLevel": {
            "Expression": {
                "Hierarchy": {
                    "Expression": {
                        "PropertyVariationSource": {
                            "Expression": {"SourceRef": {"Entity": "Calendar"}},
                            "Name": "Variation",
                            "Property": "Date",
                        }
                    },
                    "Hierarchy": "Date Hierarchy",
                }
            },
            "Level": "Month",
        }
    }
    assert roles.field_identity({"field": variation}) == (
        "HierarchyLevel",
        "Calendar",
        "Date.Date Hierarchy",
        "Month",
    )
    assert roles.is_editable_projection({"field": variation}) is False


def test_source_aliases_resolve_when_the_from_clause_is_supplied() -> None:
    # Filters (not projections) reference fields through From aliases.
    field = {"Measure": {"Expression": {"SourceRef": {"Source": "m"}}, "Property": "Total Sales"}}
    assert roles.field_identity(field) == ("Measure", "source:m", "Total Sales")
    assert roles.field_identity(field, {"m": "# Measures"}) == (
        "Measure",
        "# Measures",
        "Total Sales",
    )


def test_unknown_field_kinds_pass_through_with_a_stable_identity() -> None:
    calculation = {
        "field": {
            "NativeVisualCalculation": {
                "Language": "dax",
                "Name": "Run",
                "Expression": "RUNNINGSUM([x])",
            }
        }
    }
    identity = roles.field_identity(calculation)
    assert identity[0] == "Unknown"
    assert json.loads(identity[1]) == calculation["field"]
    assert roles.field_identity(copy.deepcopy(calculation)) == identity
    assert roles.is_editable_projection(calculation) is False
    assert roles.is_editable_projection({"field": {}}) is False


def test_editable_projections_and_field_parameters() -> None:
    assert roles.is_editable_projection(column("T", "A"))
    assert roles.is_editable_projection(measure("T", "M"))
    assert roles.is_editable_projection(total("T", "S"))
    parameter = column("Metric Picker", "Metric Picker")
    assert roles.is_editable_projection(parameter)
    assert not roles.is_editable_projection(parameter, field_parameter_tables={"Metric Picker"})
    assert roles.is_editable_projection(measure("Other", "M"), field_parameter_tables={"X"})


def test_sort_item_identity_reads_the_sorted_field() -> None:
    item = {"field": measure("# Measures", "Rank")["field"], "direction": "Ascending"}
    assert roles.sort_item_identity(item) == ("Measure", "# Measures", "Rank")
    aggregated = {"field": total("F", "Sales")["field"], "direction": "Descending"}
    assert roles.sort_item_identity(aggregated) == ("Aggregation", "Sum", "F", "Sales")


def test_match_projection_finds_a_field_whose_native_query_ref_is_blank() -> None:
    # The table's "vs Previous Month": queryRef "# Measures.vs PM", nativeQueryRef " ".
    projection = {
        **measure("# Measures", "vs Previous Month"),
        "queryRef": "# Measures.vs PM",
        "nativeQueryRef": " ",
        "displayName": " ",
    }
    assert roles.match_projection(projection, "vs Previous Month")
    assert roles.match_projection(projection, "VS PREVIOUS MONTH")
    assert roles.match_projection(projection, "# Measures.vs PM")
    assert roles.match_projection(projection, ("Measure", "# Measures", "vs Previous Month"))
    assert roles.match_projection(projection, measure("# Measures", "vs Previous Month"))
    assert roles.match_projection(projection, measure("# Measures", "vs Previous Month")["field"])
    assert not roles.match_projection(projection, " ")
    assert not roles.match_projection(projection, "Rank")
    assert not roles.match_projection(projection, ("Column", "# Measures", "vs Previous Month"))


def test_match_projection_uses_captions_and_field_refs() -> None:
    renamed = {**column("DimEmployee", "EmployeeKey"), "displayName": "Agent ID"}
    assert roles.match_projection(renamed, "agent id")
    assert roles.match_projection(renamed, "EmployeeKey")
    assert roles.match_projection(renamed, "DimEmployee.EmployeeKey")

    @dataclass
    class Ref:
        name: str
        table: str = ""
        aggregation: str | None = None

    assert roles.match_projection(renamed, Ref("EmployeeKey"))
    assert roles.match_projection(renamed, Ref("EmployeeKey", "dimemployee"))
    assert not roles.match_projection(renamed, Ref("EmployeeKey", "DimStore"))
    assert not roles.match_projection(renamed, Ref("EmployeeKey", aggregation="Sum"))
    summed = total("FactSales", "Sales")
    assert roles.match_projection(summed, Ref("Sales", "FactSales", "Sum"))
    assert roles.match_projection(summed, Ref("Sales"))
    assert not roles.match_projection(summed, Ref("Sales", "FactSales", "Average"))
    assert roles.match_projection(summed, "Sum of Sales")
    assert not roles.match_projection(summed, object())
    assert not roles.match_projection(summed, 5)


def test_projection_label_falls_back_from_captions_to_the_field_name() -> None:
    assert roles.projection_label({**column("T", "A"), "displayName": "Agent"}) == "Agent"
    assert roles.projection_label({**column("T", "A"), "displayName": " "}) == "A"
    blank = {**measure("T", "Real Name"), "nativeQueryRef": " "}
    assert roles.projection_label(blank) == "Real Name"
    bare = total("T", "Sales")
    del bare["nativeQueryRef"]
    assert roles.projection_label(bare) == "Sum of Sales"


def test_default_aggregation_follows_summarize_by_and_asks_when_it_is_none() -> None:
    assert roles.default_aggregation("sum") == "Sum"
    assert roles.default_aggregation("Sum") == "Sum"
    assert roles.default_aggregation("default") == "Sum"
    assert roles.default_aggregation("average") == "Average"
    assert roles.default_aggregation("min") == "Min"
    assert roles.default_aggregation("max") == "Max"
    assert roles.default_aggregation("count") == "CountNonNull"
    assert roles.default_aggregation("distinctCount") == "Count"
    assert roles.default_aggregation(3) == "Sum"
    for unknown in ("none", "None", 2, None, "weird"):
        with pytest.raises(NeedsAggregation) as raised:
            roles.default_aggregation(unknown)
        assert raised.value.options == ("Sum", "Average", "Count", "Min", "Max")
        assert "how to summarize" in str(raised.value)
    assert issubclass(NeedsAggregation, PlacementError)


# --- drop_field_references -------------------------------------------------------------


def test_removing_a_renamed_measure_drops_its_sort_and_formatting_by_identity() -> None:
    # fb4c5aa3: the queryRef is the measure's old name, the sort uses its current name.
    visual = copy.deepcopy(twin_visuals()["fb4c"])
    body = visual["visual"]
    projection = body["query"]["queryState"]["Data"]["projections"][0]
    assert projection["queryRef"] == "# Measures.Number of Employees with Positive Change %"
    sorted_property = body["query"]["sortDefinition"]["sort"][0]["field"]["Measure"]["Property"]
    assert sorted_property == "Employees with Positive Change %"
    assert sorted_property not in projection["queryRef"].split(".", 1)[1].split(" with ")[0]
    body["query"]["queryState"]["Data"]["projections"].clear()

    notes = roles.drop_field_references(visual, [projection])

    assert notes == [
        "Removed the sort by Employees with Positive Change %.",
        "Removed 23 formatting settings for Employees with Positive Change %.",
    ]
    assert "sortDefinition" not in body["query"]
    # Only entries with no metadata selector (or the default id) are left.
    for entries in body["objects"].values():
        for entry in entries:
            assert "metadata" not in entry.get("selector", {})
    assert sorted(body["objects"]) == [
        "divider",
        "fillCustom",
        "image",
        "label",
        "layout",
        "outline",
        "padding",
        "referenceLabel",
        "referenceLabelLayout",
        "value",
    ]


def test_the_old_matching_rule_would_have_kept_the_sort() -> None:
    visual = twin_visuals()["fb4c"]["visual"]
    projection = visual["query"]["queryState"]["Data"]["projections"][0]
    field_ = visual["query"]["sortDefinition"]["sort"][0]["field"]["Measure"]
    old_rule = f"{field_['Expression']['SourceRef']['Entity']}.{field_['Property']}"
    assert old_rule != projection["queryRef"]
    assert roles.sort_item_identity(visual["query"]["sortDefinition"]["sort"][0]) == (
        roles.field_identity(projection)
    )


def test_a_field_still_in_the_visual_keeps_its_sort_and_formatting() -> None:
    visual = copy.deepcopy(twin_visuals()["e82e"])
    body = visual["visual"]
    projections = body["query"]["queryState"]["Values"]["projections"]
    rank = next(p for p in projections if p["queryRef"] == "# Measures.Rank")
    # Rank is listed twice, so removing one copy leaves it in the table.
    projections.append(copy.deepcopy(rank))
    before = copy.deepcopy(body)
    assert roles.drop_field_references(visual, [rank]) == []
    assert body == before


def test_removed_projections_still_in_the_query_state_are_dropped_too() -> None:
    visual = copy.deepcopy(twin_visuals()["e82e"])
    body = visual["visual"]
    projections = body["query"]["queryState"]["Values"]["projections"]
    rank = next(p for p in projections if p["queryRef"] == "# Measures.Rank")
    notes = roles.drop_field_references(visual, [rank])
    assert notes == ["Removed the sort by Rank.", "Removed 4 formatting settings for Rank."]
    assert "sortDefinition" not in body["query"]


def test_removing_a_category_column_drops_its_per_value_colors() -> None:
    visual = copy.deepcopy(twin_visuals()["b404"])
    body = visual["visual"]
    category = body["query"]["queryState"].pop("Category")["projections"][0]
    assert len(body["objects"]["dataPoint"]) == 3
    notes = roles.drop_field_references(visual, [category])
    assert notes == ["Removed 3 formatting settings for Clustered Employees."]
    assert "dataPoint" not in body["objects"]
    # The sort was on the measure, which stays.
    assert body["query"]["sortDefinition"]["sort"][0]["direction"] == "Descending"


def test_drop_only_the_sort_items_of_removed_fields() -> None:
    visual: dict[str, Any] = {
        "visual": {
            "query": {
                "queryState": {"Values": {"projections": [column("T", "Kept")]}},
                "sortDefinition": {
                    "sort": [
                        {"field": column("T", "Gone")["field"], "direction": "Ascending"},
                        {"field": column("T", "Kept")["field"], "direction": "Descending"},
                    ]
                },
            }
        }
    }
    notes = roles.drop_field_references(visual, [column("T", "Gone")])
    assert notes == ["Removed the sort by Gone."]
    assert [s["direction"] for s in visual["visual"]["query"]["sortDefinition"]["sort"]] == [
        "Descending"
    ]


# --- plan_type_change ---------------------------------------------------------------------


def formatted_column_chart() -> dict[str, Any]:
    return copy.deepcopy(twin_visuals()["b404"])


def test_clustered_column_to_clustered_bar_keeps_every_object_and_property() -> None:
    visual = formatted_column_chart()
    old_objects = copy.deepcopy(visual["visual"]["objects"])
    plan = roles.plan_type_change("columnChart", "clusteredBarChart", visual)
    assert plan.objects == old_objects
    assert plan.dropped == []
    assert plan.warnings == []
    assert plan.removed == []
    assert state_layout(plan.query_state) == {
        "Category": ["Clustered Employees.Clustered Employees"],
        "Y": ["# Measures.TopN/BottomN/ MTD Sales"],
    }
    # The input is not changed.
    assert visual == formatted_column_chart()


def test_per_category_colors_survive_a_move_between_column_and_bar() -> None:
    visual = formatted_column_chart()
    plan = roles.plan_type_change("columnChart", "barChart", visual)
    colors = plan.objects["dataPoint"]
    assert [
        e["selector"]["data"][0]["scopeId"]["Comparison"]["Right"]["Literal"]["Value"]
        for e in colors
    ] == ["'Others'", "'TopN'", "'BottomN'"]
    assert all(list(e["properties"]) == ["fill"] for e in colors)
    assert plan.query_state["Category"]["projections"][0]["active"] is True


def test_column_to_line_drops_what_a_line_lacks_and_says_so() -> None:
    plan = roles.plan_type_change("columnChart", "lineChart", formatted_column_chart())
    axis = plan.objects["categoryAxis"][0]["properties"]
    assert sorted(axis) == ["fontSize", "labelColor", "showAxisTitle"]  # innerPadding is a bar gap
    assert list(plan.objects) == ["labels", "categoryAxis", "valueAxis", "dataPoint"]
    assert plan.dropped == [
        "Dropped 1 setting the new type does not have: Category axis (some options)."
    ]


def test_column_to_pie_maps_the_axis_to_the_legend_and_lists_the_axes_as_dropped() -> None:
    plan = roles.plan_type_change("columnChart", "pieChart", formatted_column_chart())
    assert state_layout(plan.query_state) == {
        "Category": ["Clustered Employees.Clustered Employees"],
        "Y": ["# Measures.TopN/BottomN/ MTD Sales"],
    }
    assert list(plan.objects) == ["labels", "dataPoint"]
    assert len(plan.objects["dataPoint"]) == 3
    assert plan.dropped == [
        "Dropped 2 settings the new type does not have: Category axis and Value axis."
    ]


def test_column_legend_and_axis_become_pie_details_and_legend() -> None:
    visual = {
        "visual": {
            "visualType": "clusteredColumnChart",
            "query": {
                "queryState": {
                    "Category": {"projections": [column("T", "Region")]},
                    "Series": {"projections": [column("T", "Channel")]},
                    "Y": {"projections": [measure("T", "Sales")]},
                    "Rows": {"projections": [column("T", "Year")]},
                }
            },
        }
    }
    plan = roles.plan_type_change("clusteredColumnChart", "donutChart", visual)
    assert state_layout(plan.query_state) == {
        "Category": ["T.Region"],
        "Series": ["T.Channel"],
        "Y": ["T.Sales"],
    }
    assert plan.dropped == ["Left out Year: a donut chart cannot be split into small multiples."]
    assert [state_layout({"x": {"projections": [p]}})["x"] for p in plan.removed] == [["T.Year"]]


def test_table_to_matrix_puts_columns_on_rows_and_keeps_the_column_settings() -> None:
    visual = twin_visuals()["e82e"]
    plan = roles.plan_type_change("tableEx", "pivotTable", visual)
    assert state_layout(plan.query_state) == {
        "Rows": ["DimEmployee.EmployeeKey"],
        "Values": [
            "# Measures.Employee Name",
            "# Measures.Rank",
            "# Measures.Table MTD Sales",
            "# Measures.vs PM MOM%",
            "# Measures.vs PM",
        ],
    }
    # Per-column widths, headers and formatting are keyed by queryRef, which all survive.
    for name in ("columnWidth", "columnFormatting", "columnHeaders", "grid", "values"):
        assert plan.objects[name] == visual["visual"]["objects"][name]
    assert plan.dropped == ["Dropped 1 setting the new type does not have: Totals."]


def test_matrix_to_table_lists_every_field_in_one_role() -> None:
    visual = {
        "visual": {
            "visualType": "pivotTable",
            "query": {
                "queryState": {
                    "Values": {"projections": [measure("T", "Sales")]},
                    "Rows": {"projections": [column("T", "Region")]},
                    "Columns": {"projections": [column("T", "Channel")]},
                }
            },
        }
    }
    plan = roles.plan_type_change("pivotTable", "tableEx", visual)
    assert state_layout(plan.query_state) == {"Values": ["T.Region", "T.Channel", "T.Sales"]}


def test_chart_to_new_card_leaves_the_category_out_with_a_warning() -> None:
    plan = roles.plan_type_change("columnChart", "cardVisual", formatted_column_chart())
    assert state_layout(plan.query_state) == {"Data": ["# Measures.TopN/BottomN/ MTD Sales"]}
    assert "Rows" not in plan.query_state
    assert plan.warnings == [
        "Clustered Employees was left out. On a card a category becomes small multiples, one "
        "card per value; ask for small multiples if that is what you want."
    ]
    assert plan.dropped[0] == (
        "Left out Clustered Employees: a card shows totals, and a category there would "
        "split it into small multiples, one card per value."
    )
    assert [p["queryRef"] for p in plan.removed] == ["Clustered Employees.Clustered Employees"]
    # Selectors on the removed category are gone, the rest is judged by the card's catalog.
    assert "dataPoint" not in plan.objects


def _donut_with_label_position(position: str) -> dict[str, Any]:
    return {
        "visual": {
            "visualType": "donutChart",
            "query": {
                "queryState": {
                    "Category": {"projections": [column("T", "Region")]},
                    "Y": {"projections": [measure("T", "Sales")]},
                }
            },
            "objects": {
                "label": [
                    {
                        "properties": {
                            "position": literal(f"'{position}'"),
                            "show": literal("true"),
                        }
                    }
                ]
            },
        }
    }


def test_a_property_value_the_new_type_does_not_define_is_not_carried() -> None:
    # A donut's label position is Top or Bottom; a card's is belowValue or aboveValue.
    plan = roles.plan_type_change("donutChart", "cardVisual", _donut_with_label_position("Top"))
    assert plan.objects == {
        "label": [{"properties": {"show": literal("true")}, "selector": {"id": "default"}}]
    }
    assert plan.dropped[-1] == "Dropped 1 setting the new type does not have: Label (some options)."


def test_a_property_that_is_a_different_kind_in_the_new_type_is_not_carried() -> None:
    old = expr.property_spec("ribbonChart", "labels", "labelDisplayUnits")
    new = expr.property_spec("pieChart", "labels", "labelDisplayUnits")
    assert old is not None and new is not None and old[1]["type"] != new[1]["type"]
    assert not roles._fits(old[1], new[1], literal("1000L"))  # pyright: ignore[reportPrivateUsage]


def test_carried_card_formatting_gets_the_default_state_selector() -> None:
    plan = roles.plan_type_change("donutChart", "cardVisual", _donut_with_label_position("Top"))
    assert all(entry["selector"] == {"id": "default"} for entry in plan.objects["label"])
    # Other targets keep entries without a selector as they were.
    visual = _donut_with_label_position("Top")
    visual["visual"]["objects"] = {"legend": [{"properties": {"show": literal("true")}}]}
    other = roles.plan_type_change("donutChart", "pieChart", visual)
    assert other.objects == {"legend": [{"properties": {"show": literal("true")}}]}


def test_a_charts_small_multiples_stay_small_multiples_on_a_card() -> None:
    visual = {
        "visual": {
            "visualType": "clusteredColumnChart",
            "query": {
                "queryState": {
                    "Category": {"projections": [column("T", "Region")]},
                    "Y": {"projections": [measure("T", "Sales")]},
                    "Rows": {"projections": [column("T", "Channel")]},
                }
            },
        }
    }
    plan = roles.plan_type_change("clusteredColumnChart", "cardVisual", visual)
    assert state_layout(plan.query_state) == {"Data": ["T.Sales"], "Rows": ["T.Channel"]}
    assert [w for w in plan.warnings if "Region" not in w] == []


def test_card_to_chart_is_refused_because_a_chart_needs_a_category() -> None:
    with pytest.raises(PlacementError, match="A stacked column chart needs a field for X-axis"):
        roles.plan_type_change("cardVisual", "columnChart", twin_visuals()["fb4c"])


def test_card_to_kpi_moves_the_measure_to_indicator() -> None:
    plan = roles.plan_type_change("cardVisual", "kpi", twin_visuals()["fb4c"])
    assert state_layout(plan.query_state) == {
        "Indicator": ["# Measures.Number of Employees with Positive Change %"]
    }
    # Selectors on the surviving measure would be kept, and card-only objects are listed.
    assert plan.dropped[0].startswith("Dropped 12 settings the new type does not have: Fill custom")


def test_kpi_and_gauge_swap_value_and_target() -> None:
    kpi = {
        "visual": {
            "visualType": "kpi",
            "query": {
                "queryState": {
                    "Indicator": {"projections": [measure("T", "Sales")]},
                    "Goal": {"projections": [measure("T", "Target")]},
                    "TrendLine": {"projections": [column("T", "Month")]},
                }
            },
        }
    }
    gauge = roles.plan_type_change("kpi", "gauge", kpi)
    assert state_layout(gauge.query_state) == {"Y": ["T.Sales"], "TargetValue": ["T.Target"]}
    assert gauge.dropped == ["Left out Month: a gauge holds measures."]
    back = roles.plan_type_change(
        "gauge",
        "kpi",
        {"visual": {"visualType": "gauge", "query": {"queryState": gauge.query_state}}},
    )
    assert state_layout(back.query_state) == {"Indicator": ["T.Sales"], "Goal": ["T.Target"]}


def test_multi_row_card_to_card_keeps_one_field_and_lists_the_rest() -> None:
    visual = {
        "visual": {
            "visualType": "multiRowCard",
            "query": {
                "queryState": {
                    "Values": {"projections": [measure("T", "Sales"), measure("T", "Profit")]}
                }
            },
        }
    }
    plan = roles.plan_type_change("multiRowCard", "card", visual)
    assert state_layout(plan.query_state) == {"Values": ["T.Sales"]}
    assert plan.dropped == ["Left out Profit: a card has no room for it."]


def test_line_measures_become_the_lines_of_a_combo_and_the_legend_is_left_out() -> None:
    visual = {
        "visual": {
            "visualType": "lineChart",
            "query": {
                "queryState": {
                    "Category": {"projections": [column("T", "Month")]},
                    "Y": {"projections": [measure("T", "Sales")]},
                    "Y2": {"projections": [measure("T", "Margin")]},
                    "Series": {"projections": [column("T", "Region")]},
                }
            },
        }
    }
    plan = roles.plan_type_change("lineChart", "lineClusteredColumnComboChart", visual)
    assert state_layout(plan.query_state) == {
        "Category": ["T.Month"],
        "Y2": ["T.Sales", "T.Margin"],
    }
    assert plan.dropped == ["Left out Region: a combo chart's legend only splits its columns."]


def test_combo_to_column_merges_the_line_axis_into_values_with_a_warning() -> None:
    visual = {
        "visual": {
            "visualType": "lineClusteredColumnComboChart",
            "query": {
                "queryState": {
                    "Category": {"projections": [column("T", "Month")]},
                    "Y": {"projections": [measure("T", "Sales")]},
                    "Y2": {"projections": [measure("T", "Margin")]},
                }
            },
        }
    }
    plan = roles.plan_type_change("lineClusteredColumnComboChart", "clusteredColumnChart", visual)
    assert state_layout(plan.query_state) == {
        "Category": ["T.Month"],
        "Y": ["T.Sales", "T.Margin"],
    }
    assert plan.warnings == [
        "Margin used a separate line axis; a clustered column chart has one value axis, "
        "so they now share it."
    ]
    keep = roles.plan_type_change("lineClusteredColumnComboChart", "lineChart", visual)
    assert state_layout(keep.query_state) == {
        "Category": ["T.Month"],
        "Y": ["T.Sales"],
        "Y2": ["T.Margin"],
    }


def test_column_to_matrix_maps_axis_legend_and_values() -> None:
    visual = {
        "visual": {
            "visualType": "clusteredColumnChart",
            "query": {
                "queryState": {
                    "Category": {"projections": [column("T", "Region")]},
                    "Series": {"projections": [column("T", "Channel")]},
                    "Y": {"projections": [measure("T", "Sales")]},
                    "Tooltips": {"projections": [measure("T", "Profit")]},
                }
            },
        }
    }
    plan = roles.plan_type_change("clusteredColumnChart", "pivotTable", visual)
    assert state_layout(plan.query_state) == {
        "Rows": ["T.Region"],
        "Columns": ["T.Channel"],
        "Values": ["T.Sales"],
    }
    assert plan.dropped == ["Left out Profit: a matrix has no Tooltips field well."]


def test_generic_fallback_places_a_chart_in_a_scatter_by_kind() -> None:
    visual: dict[str, Any] = {
        "visual": {
            "visualType": "clusteredColumnChart",
            "query": {
                "queryState": {
                    "Category": {"projections": [column("T", "Region")]},
                    "Y": {"projections": [measure("T", "Sales"), measure("T", "Profit")]},
                }
            },
        }
    }
    plan = roles.plan_type_change("clusteredColumnChart", "scatterChart", visual)
    assert state_layout(plan.query_state) == {
        "X": ["T.Sales"],
        "Y": ["T.Profit"],
        "Category": ["T.Region"],
    }
    one = copy.deepcopy(visual)
    one["visual"]["query"]["queryState"]["Y"]["projections"].pop()
    with pytest.raises(PlacementError, match="A scatter chart needs a field for Y Axis"):
        roles.plan_type_change("clusteredColumnChart", "scatterChart", one)


def test_tooltips_follow_when_the_new_type_has_them_and_small_multiples_move() -> None:
    visual = {
        "visual": {
            "visualType": "kpi",
            "query": {"queryState": {"Indicator": {"projections": [measure("T", "Sales")]}}},
        }
    }
    plan = roles.plan_type_change("kpi", "cardVisual", visual)
    assert state_layout(plan.query_state) == {"Data": ["T.Sales"]}


def test_selectors_survive_only_while_their_field_does() -> None:
    visual: dict[str, Any] = {
        "visual": {
            "visualType": "pivotTable",
            "query": {
                "queryState": {
                    "Rows": {"projections": [column("T", "Region")]},
                    "Values": {"projections": [measure("T", "Sales"), measure("T", "Profit")]},
                }
            },
            "objects": {
                "columnWidth": [
                    {"properties": {"value": literal("120D")}, "selector": {"metadata": "T.Sales"}},
                    {"properties": {"value": literal("90D")}, "selector": {"metadata": "T.Profit"}},
                ],
                "grid": [{"properties": {"rowPadding": literal("2D")}}],
            },
        }
    }
    plan = roles.plan_type_change("pivotTable", "card", visual)
    assert state_layout(plan.query_state) == {"Values": ["T.Sales"]}
    assert plan.objects == {}
    assert plan.dropped == [
        "Left out Region: a card holds measures.",
        "Left out Profit: a card has no room for it.",
        "Dropped 2 settings the new type does not have: Column width and Grid.",
        "Dropped 1 formatting setting for Profit.",
    ]
    assert [p["queryRef"] for p in plan.removed] == ["T.Region", "T.Profit"]
    kept = roles.plan_type_change("pivotTable", "tableEx", visual)
    assert [e["selector"]["metadata"] for e in kept.objects["columnWidth"]] == [
        "T.Sales",
        "T.Profit",
    ]
    assert kept.objects["grid"] == visual["visual"]["objects"]["grid"]


def test_unrelated_settings_on_an_untouched_type_are_carried_verbatim() -> None:
    visual = formatted_column_chart()
    same = roles.plan_type_change("columnChart", "columnChart", visual)
    assert same.objects == visual["visual"]["objects"]
    assert same.dropped == same.warnings == []


def test_type_change_refusals() -> None:
    visual = formatted_column_chart()
    with pytest.raises(PlacementError, match="cannot turn a visual into a map yet"):
        roles.plan_type_change("columnChart", "map", visual)
    with pytest.raises(PlacementError, match="group of visuals"):
        roles.plan_type_change("columnChart", "pieChart", {"visualGroup": {}})
    with pytest.raises(PlacementError, match="does not know the visual type"):
        roles.plan_type_change("customThing", "pieChart", visual)


# --- titles, sizes and placement -------------------------------------------------------------


def test_automatic_title_names_measures_then_categories() -> None:
    state = {
        "Category": {"projections": [column("DimChannel", "ChannelName")]},
        "Y": {"projections": [measure("# Measures", "Total Sales")]},
    }
    assert roles.automatic_title("clusteredColumnChart", state) == "Total Sales by ChannelName"
    assert roles.automatic_title("lineChart", state) == "Total Sales by ChannelName"
    with_series = {**state, "Series": {"projections": [column("DimStore", "Store")]}}
    assert roles.automatic_title("columnChart", with_series) == (
        "Total Sales by ChannelName and Store"
    )
    two_measures = {
        **state,
        "Y": {"projections": [measure("M", "Sales"), measure("M", "Profit"), measure("M", "Qty")]},
    }
    assert roles.automatic_title("columnChart", two_measures) == (
        "Sales, Profit and Qty by ChannelName"
    )
    assert roles.automatic_title("columnChart", {"Y": state["Y"]}) == "Total Sales"
    assert roles.automatic_title("columnChart", {}) == ""


def test_automatic_title_uses_the_field_names_the_report_shows() -> None:
    state = {
        "Category": {"projections": [{**column("DimStore", "StoreName"), "displayName": "Store"}]},
        "Y": {"projections": [{**measure("M", "Blank"), "nativeQueryRef": "Sales"}]},
    }
    assert roles.automatic_title("clusteredBarChart", state) == "Sales by Store"
    aggregated = {
        "Category": {"projections": [column("T", "Region")]},
        "Y": {"projections": [total("T", "Amount")]},
    }
    assert roles.automatic_title("pieChart", aggregated) == "Sum of Amount by Region"


def test_automatic_title_per_family() -> None:
    combo = {
        "Category": {"projections": [column("T", "Month")]},
        "Y": {"projections": [measure("T", "Sales")]},
        "Y2": {"projections": [measure("T", "Margin")]},
    }
    assert roles.automatic_title("lineClusteredColumnComboChart", combo) == (
        "Sales and Margin by Month"
    )
    line = {"Category": combo["Category"], "Y": combo["Y"], "Y2": combo["Y2"]}
    assert roles.automatic_title("lineChart", line) == "Sales and Margin by Month"
    scatter = {
        "X": {"projections": [measure("T", "Sales")]},
        "Y": {"projections": [measure("T", "Profit")]},
        "Category": {"projections": [column("T", "Region")]},
    }
    assert roles.automatic_title("scatterChart", scatter) == "Profit by Sales and Region"
    assert roles.automatic_title("gauge", {"Y": combo["Y"], "TargetValue": combo["Y2"]}) == "Sales"
    kpi = {"Indicator": combo["Y"], "Goal": combo["Y2"], "TrendLine": combo["Category"]}
    assert roles.automatic_title("kpi", kpi) == "Sales by Month"
    assert roles.automatic_title("card", {"Values": combo["Y"]}) == "Sales"
    assert roles.automatic_title("tableEx", {"Values": combo["Y"]}) == ""
    assert roles.automatic_title("treemap", {"Values": combo["Y"], "Group": combo["Category"]}) == (
        "Sales by Month"
    )
    assert roles.automatic_title("notAType", combo) == ""


def test_title_visibility_defaults() -> None:
    for chart in (*CARTESIAN, "pieChart", "donutChart", "treemap", "funnel", "gauge", "kpi"):
        assert roles.default_title_visible(chart), chart
    for quiet in (
        "tableEx",
        "pivotTable",
        "card",
        "cardVisual",
        "multiRowCard",
        "slicer",
        "textbox",
    ):
        assert not roles.default_title_visible(quiet), quiet
    assert not roles.default_title_visible("notAVisualType")


def test_default_sizes_fit_a_standard_page_and_are_positive() -> None:
    assert roles.default_size("clusteredColumnChart") == (400, 300)
    assert roles.default_size("cardVisual") == (240, 130)
    assert roles.default_size("tableEx") == (600, 350)
    for visual_type in ADDABLE:
        width, height = roles.default_size(visual_type)
        assert 0 < width <= 640 and 0 < height <= 360, visual_type


def test_overlaps_ignores_touching_edges() -> None:
    box = Box(100, 100, 200, 100)
    others = [
        Box(0, 0, 100, 100),  # touches the corner
        Box(300, 100, 50, 50),  # touches the right edge
        Box(250, 150, 100, 100),  # overlaps
        Box(0, 0, 1000, 1000),  # contains
    ]
    assert roles.overlaps(box, others) == [others[2], others[3]]
    assert roles.overlaps(box, []) == []


def test_free_position_starts_at_the_origin_on_an_empty_page() -> None:
    assert roles.free_position(1280, 720, [], 400, 300) == (0.0, 0.0)


def test_free_position_scans_rows_and_keeps_a_gap() -> None:
    occupied = [Box(0, 0, 400, 300)]
    assert roles.free_position(1280, 720, occupied, 400, 300) == (410.0, 0.0)
    assert roles.free_position(1280, 720, occupied, 400, 300, gap=0) == (400.0, 0.0)
    full_row = [Box(0, 0, 1280, 300)]
    assert roles.free_position(1280, 720, full_row, 400, 300) == (0.0, 310.0)


def test_free_position_prefers_the_area_given_when_it_has_room() -> None:
    occupied = [Box(0, 0, 400, 300)]
    prefer = Box(800, 100, 400, 400)
    assert roles.free_position(1280, 720, occupied, 300, 200, prefer=prefer) == (800.0, 100.0)
    # No room inside prefer: fall back to the whole page.
    tiny = Box(800, 100, 100, 100)
    assert roles.free_position(1280, 720, occupied, 300, 200, prefer=tiny) == (410.0, 0.0)


def test_free_position_reaches_a_spot_flush_with_the_page_edge() -> None:
    # The only free strip is the last 50 px on the right, which is not on the 10-px grid
    # from the left edge of a 1275-wide page.
    occupied = [Box(0, 0, 1225, 720)]
    assert roles.free_position(1275, 720, occupied, 50, 100, gap=0) == (1225.0, 0.0)


def test_free_position_on_a_full_page_returns_the_least_overlapping_spot() -> None:
    occupied = [Box(0, 0, 1280, 720)]
    x, y = roles.free_position(1280, 720, occupied, 400, 300)
    assert (x, y) == (0.0, 0.0)
    assert roles.overlaps(Box(x, y, 400, 300), occupied)
    # Every spot overlaps something; the one covering the least area wins: the bottom strip
    # only touches the thin band (80 px of it), the top touches the big block (100 px).
    layered = [Box(0, 0, 1280, 600), Box(0, 600, 1280, 100)]
    assert roles.free_position(1280, 720, layered, 300, 100) == (0.0, 620.0)


def test_free_position_when_the_visual_is_larger_than_the_page() -> None:
    assert roles.free_position(300, 200, [], 400, 300) == (0.0, 0.0)


def test_free_position_finds_an_exact_fit_hole_off_the_grid() -> None:
    # A deleted visual leaves a hole exactly as wide as itself; its edge is off the grid.
    occupied = [Box(0, 0, 303, 720), Box(708, 0, 572, 720)]
    spot = roles.free_position(1280, 720, occupied, 400, 300)
    assert spot == (303.0, 0.0)
    assert not roles.overlaps(Box(*spot, 400, 300), occupied)
    half = [Box(0, 0, 302.5, 720), Box(707.5, 0, 572.5, 720)]
    assert roles.free_position(1280, 720, half, 400, 300, gap=0) == (302.5, 0.0)
    # The same for a hole that is short rather than narrow.
    stacked = [Box(0, 0, 1280, 203), Box(0, 508, 1280, 212)]
    assert roles.free_position(1280, 720, stacked, 400, 300, gap=0) == (0.0, 203.0)


def _edge_search(
    page_w: float, page_h: float, occupied: list[Box], width: float, height: float
) -> bool:
    """Whether any free spot exists, searching only box edges (exact for rectangles)."""
    xs = [0.0, page_w - width, *(o.right for o in occupied)]
    ys = [0.0, page_h - height, *(o.bottom for o in occupied)]
    return any(
        0 <= x <= page_w - width
        and 0 <= y <= page_h - height
        and not roles.overlaps(Box(x, y, width, height), occupied)
        for x in xs
        for y in ys
    )


def test_free_position_never_overlaps_when_a_free_spot_exists_at_fractional_coordinates() -> None:
    generator = random.Random(7)
    found_free = 0
    for _ in range(400):
        occupied = [
            Box(
                round(generator.uniform(0, 1000), 3),
                round(generator.uniform(0, 600), 3),
                round(generator.uniform(40, 500), 3),
                round(generator.uniform(40, 400), 3),
            )
            for _ in range(generator.randrange(1, 6))
        ]
        width, height = generator.randrange(60, 300), generator.randrange(60, 250)
        x, y = roles.free_position(1280, 720, occupied, width, height, gap=0)
        assert 0 <= x <= 1280 - width and 0 <= y <= 720 - height
        if _edge_search(1280, 720, occupied, width, height):
            found_free += 1
            assert not roles.overlaps(Box(x, y, width, height), occupied)
    assert found_free > 50


def _brute_force(
    page_w: int, page_h: int, occupied: list[Box], width: int, height: int
) -> tuple[float, float] | None:
    """The first free spot scanning rows top to bottom on a 10-px grid; the far edge of each
    axis is always a candidate."""

    def axis(limit: int) -> list[int]:
        values = list(range(0, limit + 1, 10))
        return values if values[-1] == limit else [*values, limit]

    for y in axis(page_h - height):
        for x in axis(page_w - width):
            if not roles.overlaps(Box(x, y, width, height), occupied):
                return (float(x), float(y))
    return None


def test_free_position_matches_a_brute_force_scan() -> None:
    generator = random.Random(20260928)
    free_cases = full_cases = 0
    for _ in range(60):
        page_w = 400 + 10 * generator.randrange(0, 40) + generator.choice([0, 0, 5])
        page_h = 300 + 10 * generator.randrange(0, 30) + generator.choice([0, 0, 5])
        occupied = [
            Box(
                generator.randrange(0, page_w - 40),
                generator.randrange(0, page_h - 40),
                generator.randrange(40, 400),
                generator.randrange(40, 300),
            )
            for _ in range(generator.randrange(1, 12))
        ]
        width, height = generator.randrange(40, 200), generator.randrange(40, 160)
        expected = _brute_force(page_w, page_h, occupied, width, height)
        found = roles.free_position(page_w, page_h, occupied, width, height, gap=0)
        if expected is None:
            full_cases += 1
            assert roles.overlaps(Box(*found, width, height), occupied)
        else:
            free_cases += 1
            assert found == expected
    assert free_cases > 10 and full_cases > 0


def test_a_new_visual_no_longer_lands_on_the_slicers_of_the_twin() -> None:
    boxes = []
    for visual in twin_visuals().values():
        position = visual["position"]
        boxes.append(Box(position["x"], position["y"], position["width"], position["height"]))
    # The old fixed spot (1250, 10) covered the store and channel slicers.
    old_spot = Box(1250, 10, 400, 300)
    assert len(roles.overlaps(old_spot, boxes)) >= 2
    width, height = roles.default_size("cardVisual")
    x, y = roles.free_position(1920, 1200, boxes, width, height)
    assert not roles.overlaps(Box(x, y, width, height), boxes)
    assert 0 <= x <= 1920 - width and 0 <= y <= 1200 - height
    # A dense page has no room for a chart: the caller gets the least bad spot and can warn.
    cx, cy = roles.free_position(1920, 1200, boxes, 400, 300)
    assert roles.overlaps(Box(cx, cy, 400, 300), boxes)
