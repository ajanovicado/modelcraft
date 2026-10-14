from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

import pytest

from ai_powerbi_analyst.studio import pbir_expr as expr
from ai_powerbi_analyst.studio.pbir_expr import ExpressionError


def lit(text: str) -> dict[str, Any]:
    return {"expr": {"Literal": {"Value": text}}}


def value_of(node: dict[str, Any]) -> str:
    return str(node["expr"]["Literal"]["Value"])


def measure_color(name: str) -> dict[str, Any]:
    """A conditional font color as Desktop writes it: the color is a Measure expression."""
    measure = {"Measure": {"Expression": {"SourceRef": {"Entity": "CF"}}, "Property": name}}
    return {"solid": {"color": {"expr": measure}}}


# --- catalog ------------------------------------------------------------------------


def test_catalog_loads_with_every_visual_type_and_subtypes():
    types = expr.visual_types()
    assert len(types) == 57
    assert {"clusteredColumnChart", "scatterChart", "cardVisual", "kpi", "gauge"} <= types
    spec = expr.property_spec("clusteredColumnChart", "categoryAxis", "labelDisplayUnits")
    assert spec == (
        "objects",
        {
            "type": "formatting",
            "displayName": "Display units",
            "description": "Select the units (millions, billions, etc.)",
            "subtype": "labelDisplayUnits",
        },
    )
    assert expr.property_spec("clusteredColumnChart", "title", "fontFamily")[1]["subtype"] == (
        "fontFamily"
    )
    assert expr.property_spec("clusteredColumnChart", "title", "alignment")[1]["subtype"] == (
        "alignment"
    )
    assert expr.property_spec("clusteredColumnChart", "general", "altText")[1]["subtype"] == (
        "altText"
    )


def test_every_formatting_property_in_the_catalog_has_a_subtype():
    missing = [
        (visual, name, prop)
        for visual, entry in expr.catalog()["visualTypes"].items()
        for name, properties in entry["objects"].items()
        for prop, spec in properties.items()
        if spec["type"] == "formatting" and "subtype" not in spec
    ]
    assert missing == []
    shared = [
        (name, prop)
        for name, properties in expr.catalog()["containerObjects"].items()
        for prop, spec in properties.items()
        if spec["type"] == "formatting" and "subtype" not in spec
    ]
    assert shared == []


def test_roles_keep_the_authoring_order():
    assert expr.role_order("clusteredColumnChart") == [
        "Category",
        "Series",
        "Y",
        "Rows",
        "Tooltips",
    ]
    assert expr.role_order("kpi") == ["Indicator", "TrendLine", "Goal"]
    assert expr.role_order("gauge")[0] == "Y"
    # the name mapping older callers use still works and follows the same order
    roles = expr.roles("clusteredColumnChart")["roles"]
    assert list(roles) == expr.role_order("clusteredColumnChart")
    assert roles["Category"] == {"kind": "Grouping", "displayName": "X-axis"}
    assert roles["Y"]["kind"] == "Measure"


def test_scatter_axes_take_a_grouping_or_a_measure():
    roles = expr.roles("scatterChart")["roles"]
    assert roles["X"]["kind"] == "GroupingOrMeasure"
    assert roles["Y"]["kind"] == "GroupingOrMeasure"
    assert expr.role_order("scatterChart") == [
        "Category",
        "Series",
        "X",
        "Y",
        "Size",
        "Play",
        "Tooltips",
    ]


def test_role_order_of_an_unknown_visual_type_raises():
    with pytest.raises(ExpressionError):
        expr.role_order("noSuchVisual")


# --- encoding rules -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("subtype", "value", "literal"),
    [
        ("fontSize", 34, "34D"),
        ("fontSize", 12.5, "12.5D"),
        ("fontSize", "16", "16D"),
        ("labelDisplayUnits", 1000, "1000D"),
        ("labelDisplayUnits", "1000000", "1000000D"),
        ("displayUnitsWithoutAuto", 0, "0D"),
        ("alignment", "Center", "'Center'"),
        ("formatString", "#,0.00", "'#,0.00'"),
        ("altText", "It's a chart", "'It''s a chart'"),
    ],
)
def test_formatting_subtypes_choose_the_literal(subtype, value, literal):
    spec = {"type": "formatting", "subtype": subtype}
    assert expr.encode_property(spec, "anything", value) == lit(literal)


def test_real_catalog_properties_encode_by_subtype():
    _, units = expr.property_spec("clusteredColumnChart", "categoryAxis", "labelDisplayUnits")
    # The old encoder wrote this one as the string '1000'.
    assert expr.encode_property(units, "labelDisplayUnits", "1000") == lit("1000D")
    _, size = expr.property_spec("clusteredColumnChart", "title", "fontSize")
    assert expr.encode_property(size, "fontSize", 34) == lit("34D")
    _, alignment = expr.property_spec("clusteredColumnChart", "title", "alignment")
    assert expr.encode_property(alignment, "alignment", "left") == lit("'left'")


def test_font_family_writes_desktops_stack_or_the_plain_name():
    spec = {"type": "formatting", "subtype": "fontFamily"}
    assert value_of(expr.encode_property(spec, "fontFamily", "Segoe UI Bold")) == (
        "'''Segoe UI Bold'', wf_segoe-ui_bold, helvetica, arial, sans-serif'"
    )
    assert expr.encode_property(spec, "fontFamily", "Arial") == lit("'Arial'")
    assert expr.encode_property(spec, "fontFamily", "Comic Neue") == lit("'Comic Neue'")
    with pytest.raises(ExpressionError):
        expr.encode_property(spec, "fontFamily", 12)


def test_unseen_formatting_subtypes_are_refused_not_guessed():
    for subtype in ("dashArray", "stylePreset"):
        with pytest.raises(ExpressionError, match="cannot set"):
            expr.encode_property({"type": "formatting", "subtype": subtype}, "p", "x")


def test_a_catalog_without_subtypes_still_encodes_by_name():
    spec = {"type": "formatting"}
    assert expr.encode_property(spec, "textSize", 11) == lit("11D")
    assert expr.encode_property(spec, "fontFamily", "Arial") == lit("'Arial'")


def test_existing_number_suffix_wins_over_the_catalog():
    # Desktop writes accentBar.width as 6L where the catalog says numeric.
    _, width = expr.property_spec("advancedSlicerVisual", "accentBar", "width")
    assert width["type"] == "numeric"
    assert expr.encode_property(width, "width", 8) == lit("8D")
    assert expr.encode_property(width, "width", 8, existing=lit("6L")) == lit("8L")
    assert expr.encode_property(width, "width", 8, existing=lit("6D")) == lit("8D")
    assert expr.encode_property(width, "width", 8, existing="3L") == lit("8L")
    with pytest.raises(ExpressionError, match="whole number"):
        expr.encode_property(width, "width", 8.5, existing=lit("6L"))


def test_existing_suffix_applies_to_integers_enums_and_subtypes():
    assert expr.encode_property({"type": "integer"}, "padding", 4) == lit("4L")
    assert expr.encode_property({"type": "integer"}, "padding", 4, existing=lit("2D")) == lit("4D")
    orientation = {"type": "enum", "values": ["2", "1", "0"]}
    assert expr.encode_property(orientation, "orientation", 2) == lit("2D")
    assert expr.encode_property(orientation, "orientation", "1", existing=lit("2L")) == lit("1L")
    with pytest.raises(ExpressionError, match="one of"):
        expr.encode_property(orientation, "orientation", 7)
    size = {"type": "formatting", "subtype": "fontSize"}
    assert expr.encode_property(size, "fontSize", 12, existing=lit("34D")) == lit("12D")
    # text properties ignore the existing literal
    assert expr.encode_property({"type": "text"}, "text", "Hi", existing=lit("6L")) == lit("'Hi'")


def test_plain_types_still_encode():
    assert expr.encode_property({"type": "bool"}, "show", True) == lit("true")
    assert expr.encode_property({"type": "numeric"}, "n", 1.5) == lit("1.5D")
    assert expr.encode_property({"type": "fill"}, "c", "#1e90ff") == expr.color("#1E90FF")
    themed = expr.encode_property({"type": "fill"}, "c", {"theme": 1, "percent": -0.2})
    assert themed == {
        "solid": {"color": {"expr": {"ThemeDataColor": {"ColorId": 1, "Percent": -0.2}}}}
    }
    with pytest.raises(ExpressionError, match="cannot set"):
        expr.encode_property({"type": "unknown"}, "p", 1)


# --- decoding -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("34D", 34.0),
        ("34L", 34),
        ("-0.5D", -0.5),
        ("1.5M", 1.5),
        ("19M", 19.0),
        ("0.30000000000000004D", 0.30000000000000004),
        ("1E-05D", 1e-05),
        ("'O''Neil'", "O'Neil"),
        ("''", ""),
        ("true", True),
        ("false", False),
        ("datetime'2019-01-01T00:00:00'", datetime(2019, 1, 1)),
        ("datetime'2019-03-04T05:06:07'", datetime(2019, 3, 4, 5, 6, 7)),
    ],
)
def test_decode_reads_every_literal_shape(text, value):
    decoded = expr.decode(lit(text))
    assert decoded == value
    assert type(decoded) is type(value)


def test_decode_treats_d_and_l_alike_and_keeps_the_spelling_readable():
    assert expr.decode(lit("6L")) == expr.decode(lit("6D")) == 6
    assert [expr.number_suffix(lit(t)) for t in ("6L", "6D", "6.5M", "'x'", "true")] == [
        "L",
        "D",
        "M",
        None,
        None,
    ]
    assert expr.number_suffix(None) is None
    assert expr.number_suffix({"expr": {"Measure": {}}}) is None


def test_decode_leaves_what_it_cannot_read():
    assert expr.decode(None) is None
    assert expr.decode({"expr": {"Measure": {"Property": "x"}}}) is None
    assert expr.decode(lit("null")) is None
    assert expr.decode(lit("datetime'garbage'")) is None
    assert expr.decode(lit("6X")) is None
    assert expr.decode(expr.color("#abcdef")) == {"hex": "#ABCDEF"}
    assert expr.decode(expr.theme_color(0, -0.2)) == {"theme": 0, "percent": -0.2}


@pytest.mark.parametrize("value", [16, 16.5, "O'Neil", True, False, "plain"])
def test_round_trip_of_the_writers(value):
    if isinstance(value, bool):
        node = expr.boolean(value)
    elif isinstance(value, str):
        node = expr.string(value)
    else:
        node = expr.double(value)
    assert expr.decode(node) == value


def test_data_literals_round_trip_through_decode():
    assert expr.decode(lit(expr.data_literal("2019", "integer"))) == 2019
    assert expr.decode(lit(expr.data_literal("19.5", "decimal"))) == 19.5
    assert expr.decode(lit(expr.data_literal("2019-02-03", "datetime"))) == datetime(2019, 2, 3)
    assert expr.data_literal(2019, "text") == "'2019'"
    assert expr.data_literal(2019, 6) == "2019L"
    assert expr.data_literal("19.5", 10) == "19.5M"
    assert expr.data_literal(1.5, 8) == "1.5D"


# --- selectors ----------------------------------------------------------------------

REFERENCE_LABEL = {
    "data": [{"dataViewWildcard": {"matchingOption": 1}}],
    "id": "field-0d9f3a2c-1111-4222-8333-444455556666",
    "metadata": "Measures.Target",
    "order": 1,
}
SCOPE_2019 = expr.category_selector("Date", "Year", 2019)


@pytest.mark.parametrize(
    ("selector", "kind"),
    [
        (None, "none"),
        ({}, "none"),
        ({"id": "default"}, "default"),
        ({"id": "selection:selected"}, "state"),
        ({"metadata": "Sales.Amount"}, "metadata"),
        ({"data": [{"dataViewWildcard": {"matchingOption": 1}}]}, "data"),
        (SCOPE_2019, "data"),
        ({**SCOPE_2019, "metadata": "Sum(Sales.Amount)"}, "data+metadata"),
        (REFERENCE_LABEL, "field"),
        ({"order": 3}, "none"),
    ],
)
def test_selector_kind(selector, kind):
    assert expr.selector_kind(selector) == kind


def test_selectors_are_preserved_verbatim():
    objects: dict[str, Any] = {}
    expr.set_property(objects, "referenceLabel", "color", expr.color("#00FF00"), REFERENCE_LABEL)
    stored = objects["referenceLabel"][0]["selector"]
    assert stored == REFERENCE_LABEL
    assert stored is not REFERENCE_LABEL
    expr.set_property(objects, "fill", "color", expr.color("#000080"), {"id": "selection:selected"})
    assert objects["fill"] == [
        {
            "properties": {"color": expr.color("#000080")},
            "selector": {"id": "selection:selected"},
        }
    ]
    assert expr.get_property(objects, "referenceLabel", "color", REFERENCE_LABEL) == (
        expr.color("#00FF00")
    )


def test_state_ids_and_same_selector():
    assert expr.STATE_SELECTED == "selection:selected"
    assert expr.STATE_DEFAULT == "default"
    assert expr.state_selector(expr.STATE_SELECTED) == {"id": "selection:selected"}
    assert expr.same_selector({"a": 1, "b": 2}, {"b": 2, "a": 1})
    assert expr.same_selector(None, {})
    assert not expr.same_selector(None, {"id": "default"})
    assert expr._same_selector is expr.same_selector


def test_category_selector_types_the_literal_from_the_model():
    untyped = expr.category_selector("Date", "Year", 2019)
    assert untyped["data"][0]["scopeId"]["Comparison"]["Right"] == {"Literal": {"Value": "2019L"}}
    as_text = expr.category_selector("Date", "Year", 2019, data_type=2)
    assert as_text["data"][0]["scopeId"]["Comparison"]["Right"] == {"Literal": {"Value": "'2019'"}}
    as_decimal = expr.category_selector("Sales", "Price", "19.5", data_type="decimal")
    assert as_decimal["data"][0]["scopeId"]["Comparison"]["Right"] == {
        "Literal": {"Value": "19.5M"}
    }
    with pytest.raises(ExpressionError):
        expr.category_selector("Date", "Year", "abc", data_type=6)


# --- effective value ----------------------------------------------------------------


def card_value_objects() -> dict[str, Any]:
    """The new card: a field-scoped entry and the default entry (visual fb4c5aa3's shape)."""
    return {
        "value": [
            {
                "selector": {"metadata": "Measures.Total Sales"},
                "properties": {"fontColor": expr.color("#FF0000"), "fontSize": lit("34D")},
            },
            {
                "selector": {"id": "default"},
                "properties": {"fontColor": expr.color("#0000FF")},
            },
        ]
    }


def test_metadata_entry_beats_the_default_entry_for_its_field():
    objects = card_value_objects()
    found = expr.effective_property(
        objects, "value", "fontColor", {"metadata": "Measures.Total Sales"}
    )
    assert found is not None
    assert found.value == {"hex": "#FF0000"}
    assert found.kind == "metadata"
    assert found.entry is objects["value"][0]
    other = expr.effective_property(objects, "value", "fontColor", {"metadata": "Measures.Other"})
    assert other is not None
    assert other.value == {"hex": "#0000FF"}
    assert other.kind == "default"
    assert expr.effective_property(objects, "value", "fontColor", None).value == {"hex": "#0000FF"}
    # a property only the narrower entry sets is not read from the default entry
    assert expr.effective_property(
        objects, "value", "fontSize", {"metadata": "Measures.Other"}
    ) is (None)
    assert (
        expr.effective_property(
            objects, "value", "fontSize", {"metadata": "Measures.Total Sales"}
        ).value
        == 34.0
    )


def wildcard() -> dict[str, Any]:
    return {"data": [{"dataViewWildcard": {"matchingOption": 1}}]}


def chart_data_points() -> dict[str, Any]:
    """A chart with a plain entry, a wildcard, and per-category scopeId entries."""
    top = expr.category_selector("Employees", "Rank Group", "TopN")
    bottom = expr.category_selector("Employees", "Rank Group", "BottomN")
    return {
        "dataPoint": [
            {"properties": {"fill": expr.color("#111111")}},
            {"selector": {"id": "default"}, "properties": {"fill": expr.color("#222222")}},
            {
                "selector": {"metadata": "Sum(Sales.Amount)"},
                "properties": {"fill": expr.color("#333333")},
            },
            {"selector": wildcard(), "properties": {"fill": expr.color("#444444")}},
            {"selector": top, "properties": {"fill": expr.color("#0000FF")}},
            {"selector": bottom, "properties": {"fill": expr.color("#FF0000")}},
            {
                "selector": {**top, "metadata": "Sum(Sales.Amount)"},
                "properties": {"fill": expr.color("#00FF00")},
            },
        ]
    }


def fill_of(objects: dict[str, Any], target: dict[str, Any] | None) -> Any:
    found = expr.effective_property(objects, "dataPoint", "fill", target)
    return None if found is None else found.value["hex"]


def test_ranking_is_data_and_metadata_then_data_then_metadata_then_default_then_none():
    objects = chart_data_points()
    top = "'TopN'"
    measure = "Sum(Sales.Amount)"
    assert fill_of(objects, {"data": [top], "metadata": measure}) == "#00FF00"
    assert fill_of(objects, {"data": [top]}) == "#0000FF"
    assert fill_of(objects, {"data": ["'BottomN'"], "metadata": measure}) == "#FF0000"
    # an instance no scopeId names falls to the wildcard, which ranks as data
    assert fill_of(objects, {"data": ["'Others'"], "metadata": measure}) == "#444444"
    assert fill_of(objects, {"metadata": measure}) == "#333333"
    assert fill_of(objects, {"metadata": "Sum(Other)"}) == "#222222"
    assert fill_of(objects, None) == "#222222"
    plain = {"dataPoint": [objects["dataPoint"][0]]}
    assert fill_of(plain, {"metadata": measure}) == "#111111"


def test_covering_entries_come_winner_first_with_kinds():
    objects = chart_data_points()
    covering = expr.entries_covering(
        objects, "dataPoint", {"data": ["'TopN'"], "metadata": "Sum(Sales.Amount)"}
    )
    assert [c.kind for c in covering] == [
        "data+metadata",
        "data",
        "data",
        "metadata",
        "default",
        "none",
    ]
    assert [c.position for c in covering] == [6, 4, 3, 2, 1, 0]
    assert all(isinstance(c.rank, int) for c in covering)
    only_fill = expr.entries_covering(objects, "dataPoint", {}, prop="fill")
    assert [c.kind for c in only_fill] == ["default", "none"]
    assert expr.entries_covering(objects, "missing", {}) == []


def test_equal_rank_resolves_to_the_later_entry():
    objects = {
        "dataPoint": [
            {"selector": {"metadata": "M"}, "properties": {"fill": expr.color("#AAAAAA")}},
            {"selector": {"metadata": "M"}, "properties": {"fill": expr.color("#BBBBBB")}},
        ]
    }
    assert fill_of(objects, {"metadata": "M"}) == "#BBBBBB"


def test_numbers_match_across_d_and_l_and_raw_items_match_too():
    year = expr.category_selector("Date", "Year", 2019)
    objects = {"dataPoint": [{"selector": year, "properties": {"fill": expr.color("#ABCDEF")}}]}
    assert fill_of(objects, {"data": ["2019D"]}) == "#ABCDEF"
    assert fill_of(objects, {"data": ["2019L"]}) == "#ABCDEF"
    assert fill_of(objects, {"data": ["2020L"]}) is None
    assert fill_of(objects, {"data": [year["data"][0]]}) == "#ABCDEF"
    assert fill_of(objects, {"metadata": "M"}) is None


def test_totals_only_wildcard_paints_no_instance():
    totals = {"data": [{"dataViewWildcard": {"matchingOption": 2}}]}
    objects = {"dataPoint": [{"selector": totals, "properties": {"fill": expr.color("#010101")}}]}
    assert fill_of(objects, {"data": ["'x'"]}) is None


def test_state_entries_cover_only_a_target_in_that_state():
    objects = {
        "fill": [
            {"selector": {"id": "default"}, "properties": {"color": expr.color("#EEEEEE")}},
            {
                "selector": {"id": "selection:selected"},
                "properties": {"color": expr.color("#000080")},
            },
        ]
    }
    selected = expr.effective_property(objects, "fill", "color", {"state": "selection:selected"})
    assert selected is not None
    assert (selected.value, selected.kind) == ({"hex": "#000080"}, "state")
    assert expr.effective_property(objects, "fill", "color", {}).value == {"hex": "#EEEEEE"}
    assert expr.effective_property(objects, "fill", "color", {"state": "hover"}).value == {
        "hex": "#EEEEEE"
    }


def test_reference_labels_are_addressed_by_their_field_id():
    other = {**REFERENCE_LABEL, "id": "field-99999999-1111-4222-8333-444455556666", "order": 2}
    objects = {
        "referenceLabel": [
            {"selector": {"metadata": "Measures.Target"}, "properties": {"color": lit("'a'")}},
            {"selector": REFERENCE_LABEL, "properties": {"color": lit("'b'")}},
            {"selector": other, "properties": {"color": lit("'c'")}},
        ]
    }
    found = expr.effective_property(
        objects, "referenceLabel", "color", {"state": REFERENCE_LABEL["id"]}
    )
    assert found is not None
    assert (found.value, found.kind) == ("b", "field")
    assert (
        expr.effective_property(
            objects,
            "referenceLabel",
            "color",
            {"state": other["id"], "metadata": "Measures.Target"},
        ).value
        == "c"
    )
    # the metadata scope alone reaches the metadata-only entry, not the labelled ones
    assert (
        expr.effective_property(
            objects, "referenceLabel", "color", {"metadata": "Measures.Target"}
        ).value
        == "a"
    )


# --- write everywhere ---------------------------------------------------------------


def test_write_everywhere_on_the_card_shape_writes_both_entries():
    objects = card_value_objects()
    result = expr.write_everywhere(objects, "value", "fontColor", expr.color("#123456"))
    assert result.written == 2
    assert result.protected == []
    assert result.replaced == [
        expr.ReplacedEntry({"metadata": "Measures.Total Sales"}, {"hex": "#FF0000"})
    ]
    assert [e["properties"]["fontColor"] for e in objects["value"]] == [expr.color("#123456")] * 2
    # the other property of the metadata entry is kept
    assert objects["value"][0]["properties"]["fontSize"] == lit("34D")
    # entries do not share one dict, so editing one later cannot change the other
    assert (
        objects["value"][0]["properties"]["fontColor"]
        is not (objects["value"][1]["properties"]["fontColor"])
    )


def table_values() -> dict[str, Any]:
    """The table's values object: four fontColor entries are conditional (Measure) formats."""
    return {
        "values": [
            {"properties": {"fontSize": lit("12D"), "fontColor": expr.color("#252423")}},
            *(
                {
                    "selector": {"metadata": f"Employees.Col{n}"},
                    "properties": {"fontColor": measure_color(f"CF Table {n}")},
                }
                for n in range(4)
            ),
        ]
    }


def test_write_everywhere_keeps_conditional_formatting():
    objects = table_values()
    before = copy.deepcopy(objects)
    result = expr.write_everywhere(objects, "values", "fontColor", expr.color("#000080"))
    assert result.written == 1
    assert result.replaced == []
    assert [(p.selector, p.kind) for p in result.protected] == [
        ({"metadata": f"Employees.Col{n}"}, "measure") for n in range(4)
    ]
    assert objects["values"][0]["properties"]["fontColor"] == expr.color("#000080")
    assert objects["values"][1:] == before["values"][1:]


def test_write_everywhere_can_replace_conditional_formatting_when_asked():
    objects = table_values()
    result = expr.write_everywhere(
        objects, "values", "fontColor", expr.color("#000080"), replace_conditional=True
    )
    assert result.written == 5
    assert result.protected == []
    assert [r.selector for r in result.replaced] == [
        {"metadata": f"Employees.Col{n}"} for n in range(4)
    ]
    # a Measure expression has no plain value, so the expression itself is reported
    assert result.replaced[0].previous == measure_color("CF Table 0")
    assert all(e["properties"]["fontColor"] == expr.color("#000080") for e in objects["values"])


def test_write_everywhere_reports_the_per_category_colors_it_replaces():
    objects = chart_data_points()
    result = expr.write_everywhere(objects, "dataPoint", "fill", expr.color("#00AA00"))
    assert result.protected == []
    assert result.written == 7
    replaced = {json_key(r.selector): r.previous for r in result.replaced}
    assert replaced[json_key(expr.category_selector("Employees", "Rank Group", "TopN"))] == {
        "hex": "#0000FF"
    }
    assert replaced[json_key(expr.category_selector("Employees", "Rank Group", "BottomN"))] == {
        "hex": "#FF0000"
    }
    # the plain and default entries are the "whole visual" write, not replacements
    assert None not in [r.selector for r in result.replaced]
    assert {"id": "default"} not in [r.selector for r in result.replaced]
    assert len(result.replaced) == 5
    assert all(e["properties"]["fill"] == expr.color("#00AA00") for e in objects["dataPoint"])


def json_key(selector: Any) -> str:
    import json

    return json.dumps(selector, sort_keys=True)


def test_write_everywhere_adds_an_entry_and_ignores_entries_that_do_not_set_it():
    objects: dict[str, Any] = {}
    result = expr.write_everywhere(objects, "labels", "color", expr.color("#111111"))
    assert (result.written, result.replaced, result.protected) == (1, [], [])
    assert objects == {"labels": [{"properties": {"color": expr.color("#111111")}}]}
    with_ids = {
        "value": [{"selector": {"id": "selection:selected"}, "properties": {"size": lit("1D")}}]
    }
    result = expr.write_everywhere(with_ids, "value", "color", expr.color("#111111"))
    assert result.written == 1
    assert with_ids["value"][1] == {
        "selector": {"id": "default"},
        "properties": {"color": expr.color("#111111")},
    }
    assert "color" not in with_ids["value"][0]["properties"]


def test_write_everywhere_does_not_list_an_entry_that_already_has_the_value():
    value = expr.color("#123456")
    objects = {
        "value": [
            {"selector": {"metadata": "M"}, "properties": {"c": copy.deepcopy(value)}},
            {"selector": {"id": "default"}, "properties": {"c": expr.color("#000000")}},
        ]
    }
    result = expr.write_everywhere(objects, "value", "c", value)
    assert result.written == 2
    assert result.replaced == []


def test_protected_kind_names_what_the_value_is():
    assert expr.protected_kind(expr.color("#000000")) is None
    assert expr.protected_kind(expr.theme_color(1)) is None
    assert expr.protected_kind(lit("12D")) is None
    assert expr.protected_kind(measure_color("x")) == "measure"
    assert expr.protected_kind({"expr": {"Measure": {}}}) == "measure"
    assert expr.protected_kind({"solid": {"color": {"expr": {"FillRule": {}}}}}) == "gradient"
    assert expr.protected_kind({"expr": {"Conditional": {}}}) == "conditional"
    assert expr.protected_kind({"expr": {"Aggregation": {}}}) == "aggregation"
    assert expr.protected_kind({"expr": {"Other": {}}}) == "other"
    assert expr.protected_kind("weird") == "unknown"


def test_set_everywhere_is_unchanged():
    objects = table_values()
    expr.set_everywhere(objects, "values", "fontColor", expr.color("#000080"))
    assert all(e["properties"]["fontColor"] == expr.color("#000080") for e in objects["values"])


# --- theme colors and fonts ---------------------------------------------------------


def test_shade_matches_power_bi_including_the_half_rounding_case():
    # white shaded 20% is the grey "Others" bars: the evidence for ColorId 0 at -0.2
    assert expr.shade("#FFFFFF", -0.2) == "#CCCCCC"
    # 255 * 0.7 = 178.5: Power BI gives 179 (B3), Python's round() gives 178 (B2)
    assert round(255 * (1 - 0.3)) == 178
    assert expr.shade("#FFFFFF", -0.3) == "#B3B3B3"
    # 2 + 253 * 0.5 = 128.5 lightens to 129
    assert expr.shade("#020202", 0.5) == "#818181"
    assert expr.shade("#000000", 0.4) == "#666666"
    assert expr.shade("#123456", 0) == "#123456"
    assert expr.shade("#FFFFFF", -1) == "#000000"
    assert expr.shade("#FFFFFF", 1) == "#FFFFFF"
    assert expr.shade("1e90ff", 0) == "#1E90FF"


def test_theme_palette_and_resolution():
    theme = {"background": "#203040", "foreground": "#F0E0D0", "dataColors": ["#118DFF", "#12239E"]}
    # measured in Desktop: 0 is white and 1 black under any theme, 2.. are the data colors
    assert expr.theme_palette(theme) == ["#FFFFFF", "#000000", "#118DFF", "#12239E"]
    assert expr.theme_palette({}) == ["#FFFFFF", "#000000"]
    assert expr.resolve_theme_color(theme, 0, -0.2) == "#CCCCCC"
    assert expr.resolve_theme_color(theme, 0) == "#FFFFFF"
    assert expr.resolve_theme_color(theme, 1) == "#000000"
    assert expr.resolve_theme_color(theme, 2, 0.5) == expr.shade("#118DFF", 0.5)
    with pytest.raises(ExpressionError, match="no color number 9"):
        expr.resolve_theme_color(theme, 9)
    assert expr.resolve_theme_color(theme, 3, verified_only=True) == "#12239E"


def test_theme_palette_uses_only_the_first_eight_data_colors():
    colors = [f"#00000{i:X}" for i in range(12)]
    palette = expr.theme_palette({"dataColors": colors})
    assert palette == ["#FFFFFF", "#000000", *colors[:8]]
    assert expr.resolve_theme_color({"dataColors": colors}, 9) == "#000007"
    # ids of 10 and up are invalid in Desktop, so they never resolve
    with pytest.raises(ExpressionError):
        expr.resolve_theme_color({"dataColors": colors}, 10)
    # a data color that is not a hex leaves its slot empty and does not shift later ids
    gap = expr.theme_palette({"dataColors": ["#111111", "nope", "#333333"]})
    assert gap == ["#FFFFFF", "#000000", "#111111", "", "#333333"]
    with pytest.raises(ExpressionError, match="no color number 3"):
        expr.resolve_theme_color({"dataColors": ["#111111", "nope"]}, 3)


def test_theme_color_ids_zero_to_nine_are_the_writable_ones():
    assert set(expr.VERIFIED_THEME_COLOR_IDS) == set(range(10))
    assert expr.theme_color(0, -0.2, verified_only=True) == {
        "solid": {"color": {"expr": {"ThemeDataColor": {"ColorId": 0, "Percent": -0.2}}}}
    }
    for color_id in range(10):
        written = expr.theme_color(color_id, 0.4, verified_only=True)
        assert written["solid"]["color"]["expr"]["ThemeDataColor"]["ColorId"] == color_id
    for bad in (10, 11, 41, 42, -1, True):
        with pytest.raises(ExpressionError, match="0 to 9"):
            expr.theme_color(bad)  # type: ignore[arg-type]
    with pytest.raises(ExpressionError):
        expr.theme_color(0, 1.5)


def test_wildcard_selector_covers_a_column_only_target():
    # the real table "values" rule shape: a wildcard plus the column's queryRef
    rule = expr.color("#AA0000")
    objects = {
        "values": [
            {"properties": {"fontColor": expr.color("#111111")}},
            {
                "selector": {
                    "data": [{"dataViewWildcard": {"matchingOption": 1}}],
                    "metadata": "# Measures.Rank",
                },
                "properties": {"fontColor": rule},
            },
        ]
    }
    found = expr.effective_property(objects, "values", "fontColor", {"metadata": "# Measures.Rank"})
    assert found is not None and found.value == {"hex": "#AA0000"}
    # a different column does not match the metadata, so the plain entry wins
    other = expr.effective_property(objects, "values", "fontColor", {"metadata": "Agents.Name"})
    assert other is not None and other.value == {"hex": "#111111"}
    # a totals-only wildcard still paints no ordinary instance
    objects["values"][1]["selector"]["data"] = [{"dataViewWildcard": {"matchingOption": 2}}]
    column = {"metadata": "# Measures.Rank"}
    totals = expr.effective_property(objects, "values", "fontColor", column)
    assert totals is not None and totals.value == {"hex": "#111111"}
    # a bare wildcard (no column) does not outrank the entries for a column-only target
    objects["values"][1]["selector"] = {"data": [{"dataViewWildcard": {"matchingOption": 1}}]}
    bare = expr.effective_property(objects, "values", "fontColor", column)
    assert bare is not None and bare.value == {"hex": "#111111"}


def test_large_integer_literals_are_exact():
    big, neighbour = "9007199254740993L", "9007199254740992L"
    assert expr.decode(expr._literal(big)) == 9007199254740993
    assert expr._normalized_literal(big) != expr._normalized_literal(neighbour)
    assert expr._normalized_literal("2019L") == expr._normalized_literal("2019D")
    assert expr._normalized_literal("2019L") == 2019


def test_commas_are_thousands_separators_only():
    prop = {"type": "formatting", "subtype": "fontSize"}
    with pytest.raises(ExpressionError):
        expr.encode_property(prop, "fontSize", "1,5")
    with pytest.raises(ExpressionError):
        expr.data_literal("1,5", "integer")
    assert expr.data_literal("1,234", "integer") == "1234L"
    assert expr.data_literal("-1,234,567.5", "double") == "-1234567.5D"
    with pytest.raises(ExpressionError):
        expr.data_literal("12,34", "integer")


def test_font_literals_use_the_stacks_desktop_wrote():
    # the three distinct fontFamily values of the Agents report's 14 literals
    assert expr.font_literal("Segoe UI") == expr.FontLiteral(
        "'''Segoe UI'', wf_segoe-ui_normal, helvetica, arial, sans-serif'",
        True,
        "Segoe UI",
        True,
    )
    assert expr.font_literal("Segoe UI Semibold").text == (
        "'''Segoe UI Semibold'', wf_segoe-ui_semibold, helvetica, arial, sans-serif'"
    )
    assert expr.font_literal("Segoe UI Bold").text == (
        "'''Segoe UI Bold'', wf_segoe-ui_bold, helvetica, arial, sans-serif'"
    )
    assert expr.decode(lit(expr.font_literal("Segoe UI Bold").text)) == (
        "'Segoe UI Bold', wf_segoe-ui_bold, helvetica, arial, sans-serif"
    )


def test_font_literals_for_the_rest_of_the_picker_and_unknown_families():
    assert expr.font_literal("Segoe UI Light") == expr.FontLiteral(
        "'''Segoe UI Light'', wf_segoe-ui_light, helvetica, arial, sans-serif'",
        True,
        "Segoe UI Light",
        False,
    )
    for family in ("Arial", "Calibri", "Cambria", "Consolas", "Courier New", "DIN", "Georgia"):
        assert expr.font_literal(family) == expr.FontLiteral(f"'{family}'", True, family, False)
    for family in ("Tahoma", "Times New Roman", "Trebuchet MS", "Verdana"):
        assert expr.font_literal(family.lower()).text == f"'{family}'"
    assert expr.font_literal("  georgia ").family == "Georgia"
    unknown = expr.font_literal("Comic Neue")
    assert (unknown.text, unknown.known, unknown.verified) == ("'Comic Neue'", False, False)
    assert expr.font_literal("O'Hare Sans").text == "'O''Hare Sans'"
    stack = "'Segoe UI Bold', wf_segoe-ui_bold, helvetica, arial, sans-serif"
    assert expr.font_literal(stack).known
    assert expr.font_literal(stack).family == "Segoe UI Bold"
    for bad in ("", "   "):
        with pytest.raises(ExpressionError):
            expr.font_literal(bad)


# --- existing behavior kept ---------------------------------------------------------


def test_existing_writers_and_editors_are_unchanged():
    objects: dict[str, Any] = {}
    expr.set_property(objects, "title", "text", expr.string("Hi"))
    expr.set_property(objects, "title", "show", expr.boolean(True))
    assert objects == {"title": [{"properties": {"text": lit("'Hi'"), "show": lit("true")}}]}
    assert expr.get_property(objects, "title", "text") == lit("'Hi'")
    assert expr.remove_property(objects, "title", "text")
    assert expr.remove_property(objects, "title", "show")
    assert objects == {}
    assert expr.data_type_name(8) == "double"
    assert expr.literal_value(2019) == "2019L"
    assert expr.literal_value("x") == "'x'"
    assert expr.metadata_selector("T.C") == {"metadata": "T.C"}
    assert expr.double(16) == lit("16D")
    assert expr.integer(45) == lit("45L")


# --- instance selectors ---------------------------------------------------------------


def test_instance_selectors_come_from_the_schema_list_in_the_catalog():
    assert expr.instance_selectors("shape") == {
        "fill": ["default"],
        "outline": ["default"],
        "shape": ["default"],
    }
    assert expr.instance_selectors("actionButton")["fill"] == [
        "default",
        "hover",
        "selected",
        "disabled",
    ]
    assert expr.instance_selectors("cardVisual")["value"] == ["default"]
    assert expr.instance_selectors("columnChart") == {}
    assert expr.instance_selectors("no such visual") == {}
    # the advanced slicer keeps its state entries on fillCustom and value (the Agents report)
    tiles = expr.instance_selectors("advancedSlicerVisual")
    states = ["default", "hover", "press", "selected", "mixed"]
    assert tiles["fillCustom"] == tiles["fill"] == states
    assert tiles["value"] == tiles["text"]


def test_a_default_selector_is_needed_only_where_the_schema_lists_default():
    assert expr.needs_default_selector("shape", "fill")
    assert expr.needs_default_selector("cardVisual", "label")
    assert expr.needs_default_selector("actionButton", "text")
    # a matrix keeps its grid and header settings selector-less; its totals have Row/Column
    assert not expr.needs_default_selector("pivotTable", "grid")
    assert expr.instance_selectors("pivotTable") == {"subTotals": ["Row", "Column"]}
    assert not expr.needs_default_selector("pivotTable", "subTotals")
    assert not expr.needs_default_selector("shape", "rotation")
    assert not expr.needs_default_selector("columnChart", "dataPoint")
    assert not expr.needs_default_selector(None, "fill")


def test_writers_put_an_object_that_needs_it_under_the_default_selector():
    color = expr.color("#123456")
    objects: dict[str, Any] = {}
    expr.set_property(objects, "fill", "fillColor", color, visual_type="shape")
    default = {"id": "default"}
    assert objects == {"fill": [{"properties": {"fillColor": color}, "selector": default}]}
    # not for an object the type does not list, nor without a visual type
    plain: dict[str, Any] = {}
    expr.set_property(plain, "rotation", "angle", expr.double(5), visual_type="shape")
    expr.set_property(plain, "fill", "fillColor", color)
    assert plain == {
        "rotation": [{"properties": {"angle": lit("5D")}}],
        "fill": [{"properties": {"fillColor": color}}],
    }
    legacy: dict[str, Any] = {}
    expr.set_everywhere(legacy, "fill", "fillColor", color, visual_type="shape")
    assert legacy == objects


def test_write_everywhere_adds_the_default_entry_even_when_nothing_has_an_id():
    color = expr.color("#123456")
    objects = {"fill": [{"properties": {"show": lit("true")}}]}
    result = expr.write_everywhere(objects, "fill", "fillColor", color, visual_type="shape")
    assert result.written == 1
    # the selector-less entry does not set the property, so it is not written to
    assert objects == {
        "fill": [
            {"properties": {"show": lit("true")}},
            {"properties": {"fillColor": color}, "selector": {"id": "default"}},
        ]
    }
    # a selector-less entry that already sets it is kept in step; the default entry is added
    has_it = {"fill": [{"properties": {"fillColor": expr.color("#000000")}}]}
    result = expr.write_everywhere(has_it, "fill", "fillColor", color, visual_type="shape")
    assert [e.get("selector") for e in has_it["fill"]] == [None, {"id": "default"}]
    assert all(e["properties"]["fillColor"] == color for e in has_it["fill"])
    assert result.written == 2


def test_a_whole_visual_write_leaves_the_state_entries_alone():
    objects = {
        "fillCustom": [
            {
                "properties": {"fillColor": expr.color("#FFFFFF")},
                "selector": {"id": "selection:selected"},
            },
            {"properties": {"fillColor": expr.color("#F1F2F2")}, "selector": {"id": "default"}},
        ]
    }
    result = expr.write_everywhere(
        objects,
        "fillCustom",
        "fillColor",
        expr.color("#0000FF"),
        visual_type="advancedSlicerVisual",
    )
    assert result.written == 1
    assert result.kept_states == [{"id": "selection:selected"}]
    assert objects["fillCustom"][0]["properties"]["fillColor"] == expr.color("#FFFFFF")
    assert objects["fillCustom"][1]["properties"]["fillColor"] == expr.color("#0000FF")
    assert result.replaced == []


def test_state_selector_for_reuses_the_id_desktop_wrote():
    objects = {
        "fillCustom": [
            {"properties": {}, "selector": {"id": "selection:selected"}},
            {"properties": {}, "selector": {"id": "default"}},
        ]
    }
    assert expr.state_selector_for("advancedSlicerVisual", "fillCustom", "selected", objects) == {
        "id": "selection:selected"
    }
    assert expr.state_selector_for("advancedSlicerVisual", "fillCustom", "hover", objects) == {
        "id": "hover"
    }
    assert expr.state_selector_for("actionButton", "fill", "hover") == {"id": "hover"}
    with pytest.raises(ExpressionError) as only:
        expr.state_selector_for("shape", "fill", "hover")
    assert str(only.value) == "A shape fill has only the state 'default', not 'hover'"
    with pytest.raises(ExpressionError) as several:
        expr.state_selector_for("actionButton", "fill", "pressed")
    assert str(several.value) == (
        "An actionButton fill has the states default, hover, selected, disabled, not 'pressed'"
    )
    with pytest.raises(ExpressionError) as none:
        expr.state_selector_for("columnChart", "dataPoint", "hover")
    assert str(none.value) == "A columnChart has no separate states for dataPoint"


def test_an_equivalent_category_selector_is_found_whichever_way_the_literal_is_spelled():
    objects = {
        "dataPoint": [
            {"properties": {}, "selector": expr.category_selector("T", "Year", 2019, "double")},
            {"properties": {}, "selector": expr.category_selector("T", "Year", "2020")},
        ]
    }
    wanted = expr.category_selector("T", "Year", 2019, "integer")
    assert wanted != objects["dataPoint"][0]["selector"]
    assert (
        expr.find_equivalent_selector(objects, "dataPoint", wanted)
        == objects["dataPoint"][0]["selector"]
    )
    # a text 2020 is not the number 2020 on another column, and a metadata selector has no key
    assert (
        expr.find_equivalent_selector(
            objects, "dataPoint", expr.category_selector("T", "Other", "2020")
        )
        is None
    )
    assert expr.find_equivalent_selector(objects, "dataPoint", {"metadata": "T.Year"}) is None
    assert expr.find_equivalent_selector(objects, "fill", wanted) is None


def test_a_custom_visual_still_has_the_container_objects():
    found = expr.property_spec("acme.customVisual", "title", "text")
    assert found is not None and found[0] == "visualContainerObjects"
    assert expr.property_spec("acme.customVisual", "labels", "show") is None


# --- color names and resolution ---------------------------------------------------------


@pytest.mark.parametrize(
    ("hex_value", "family"),
    [
        ("#000000", "black"),
        ("#FFFFFF", "white"),
        ("#CCCCCC", "grey"),
        ("#808080", "grey"),
        ("#1E90FF", "blue"),
        ("#118DFF", "blue"),
        ("#A30024", "red"),
        ("#DC143C", "red"),
        ("#FF8800", "orange"),
        ("#8B4513", "brown"),
        ("#FFFF00", "yellow"),
        ("#00A651", "green"),
        ("#0B6E4F", "green"),
        ("#008080", "teal"),
        ("#6B007B", "purple"),
        ("#744EC2", "purple"),  # Power BI's own purple, theme data color 7 (hue 259.7)
        ("#800080", "purple"),  # CSS purple (hue 300)
        ("#FF00FF", "pink"),  # magenta: same hue band, light
        ("#E044A7", "pink"),
        ("#FFC0CB", "pink"),
        ("1e90ff", "blue"),
    ],
)
def test_hue_family_names_a_color_the_way_a_person_would(hex_value, family):
    assert expr.hue_family(hex_value) == family


def test_hue_family_rejects_what_is_not_a_color():
    with pytest.raises(ExpressionError):
        expr.hue_family("blue")


def test_write_everywhere_can_derive_each_entry_from_its_own_value():
    objects = {
        "labels": [
            {"properties": {"fontSize": lit("10D")}},
            {"properties": {"fontSize": lit("20D")}, "selector": {"metadata": "T.M"}},
            {"properties": {}, "selector": {"id": "default"}},
        ]
    }
    seen = []

    def plus_two(current):
        seen.append(current)
        return expr.double((expr.decode(current) if current else 10) + 2)

    result = expr.write_everywhere(objects, "labels", "fontSize", plus_two)
    assert [e["properties"]["fontSize"] for e in objects["labels"]] == [
        lit("12D"),
        lit("22D"),
        lit("12D"),
    ]
    assert seen == [lit("10D"), None, lit("20D")]
    assert result.written == 3
    assert [(r.selector, r.previous) for r in result.replaced] == [({"metadata": "T.M"}, 20.0)]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (
            1e308,
            "1e+308 is out of range: numbers in Power BI formatting stay within "
            "1,000,000,000,000,000",
        ),
        (10**400, "Expected a finite number"),
    ],
)
def test_a_double_beyond_a_sane_size_is_refused(value, message):
    with pytest.raises(ExpressionError) as raised:
        expr.double(value)
    assert str(raised.value) == message


def test_a_whole_number_beyond_64_bits_is_refused_everywhere_it_is_written():
    too_big = "9223372036854775808 is too large to be a whole number in Power BI"
    for write in (
        lambda: expr.integer(2**63),
        lambda: expr.literal_value(2**63),
        lambda: expr.data_literal(2**63, "integer"),
        lambda: expr.category_selector("T", "C", 2**63, "integer"),
    ):
        with pytest.raises(ExpressionError) as raised:
            write()
        assert str(raised.value) == too_big
    assert expr.data_literal(2**63 - 1, "integer") == "9223372036854775807L"
    assert expr.integer(-(2**63)) == lit("-9223372036854775808L")


def test_a_text_with_a_lone_surrogate_is_refused():
    with pytest.raises(ExpressionError) as raised:
        expr.quote("a\ud800b")
    assert str(raised.value) == "Text values cannot contain unpaired surrogate characters"
    assert expr.quote("café \U0001f600") == "'café \U0001f600'"


def test_color_hex_resolves_literals_and_theme_colors():
    palette = expr.theme_palette({"dataColors": ["#118DFF", "#12239E"]})
    assert expr.color_hex(expr.color("#1e90ff")) == "#1E90FF"
    assert expr.color_hex(expr.theme_color(2), palette) == "#118DFF"
    assert expr.color_hex(expr.theme_color(0, -0.2), palette) == "#CCCCCC"
    assert expr.color_hex(expr.theme_color(2)) is None  # no palette to resolve it
    assert expr.color_hex(expr.theme_color(5), palette) is None  # the theme has no color 5
    assert expr.color_hex(measure_color("CF Table"), palette) is None
    assert expr.color_hex(lit("'red'")) is None
    assert expr.color_hex(expr.color("#123456"), None) == "#123456"
