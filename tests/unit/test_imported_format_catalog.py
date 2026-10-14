"""The Format pane's contents for a visual type: sections, flags, theme defaults."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from agents_twin_model import twin_model

from ai_powerbi_analyst.studio import pbir_expr, report_edits
from ai_powerbi_analyst.studio.imported_format_catalog import (
    UnknownVisualType,
    format_catalog,
)
from ai_powerbi_analyst.studio.pbir_document import PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage
from ai_powerbi_analyst.studio.report_theme import DEFAULT_THEME


def controls(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for s in catalog["sections"] for c in s["controls"]}


def sections(catalog: dict[str, Any]) -> list[str]:
    return [s["id"] for s in catalog["sections"]]


def test_clustered_column_chart_has_every_curated_section_in_pane_order() -> None:
    catalog = format_catalog("clusteredColumnChart")
    assert catalog["visual_type"] == "clusteredColumnChart"
    assert catalog["family"] == "chart"
    assert catalog["studio_draws"] is True
    assert catalog["instance_selector"] is None
    assert sections(catalog) == [
        "size_position",
        "number_format",
        "conditional_formatting",
    ]
    titles = {s["id"]: s["title"] for s in catalog["sections"]}
    assert titles["size_position"] == "Size & position"
    assert titles["number_format"] == "Number format"
    found = controls(catalog)
    assert {"position.x", "labels.labelDisplayUnits", "cf.dataColors.minColor"} <= set(found)
    names = {op.model_fields["op"].default for op in report_edits.REPORT_OPERATIONS}
    assert all(c["op"]["name"] in names for c in found.values())


def test_control_ids_are_unique_and_every_property_exists_in_the_catalog() -> None:
    cat = pbir_expr.catalog()
    for visual_type in cat["visualTypes"]:
        catalog = format_catalog(visual_type)
        ids = [c["id"] for s in catalog["sections"] for c in s["controls"]]
        assert len(ids) == len(set(ids)), visual_type
        for control in (c for s in catalog["sections"] for c in s["controls"]):
            # The visual's box and its query (a column's header, place or format) are not
            # catalog properties.
            if control["where"] in ("position", "query"):
                continue
            entry = cat["visualTypes"][visual_type]
            holder = (
                cat["containerObjects"]
                if control["where"] == "visualContainerObjects"
                else {o: p for o, p in entry["objects"].items()}
            )
            assert control["property"] in holder[control["object"]], (visual_type, control["id"])


def test_every_catalog_option_parses_with_its_own_operation() -> None:
    """The pane sends option values directly, including field-scoped controls."""
    checked: set[tuple[str, str, str]] = set()
    names = {op.model_fields["op"].default for op in report_edits.REPORT_OPERATIONS}
    for visual_type in pbir_expr.catalog()["visualTypes"]:
        for control in controls(format_catalog(visual_type)).values():
            recipe = control["op"]
            assert recipe["name"] in names
            for option in control["options"] or []:
                operation = {
                    "op": recipe["name"],
                    "visual": "probe",
                    **recipe["fixed"],
                    recipe["field"]: option["value"],
                }
                if control.get("scope") == "field":
                    operation[control["field_arg"]] = "MTD Sales"
                parsed = report_edits.parse_operations([operation])
                assert parsed[0].op == recipe["name"], (visual_type, control["id"], option)
                checked.add((recipe["name"], recipe["field"], str(option["value"])))
    assert ("set_number_format", "display_units", "thousands") in checked
    assert ("set_table_layout", "layout", "tabular") in checked


def test_number_format_units_and_theme_defaults_use_the_operation_vocabulary() -> None:
    theme = {
        **DEFAULT_THEME,
        "visualStyles": {
            "columnChart": {"*": {"labels": [{"labelDisplayUnits": 1000}]}},
        },
    }
    found = controls(format_catalog("columnChart", theme))
    units = found["labels.labelDisplayUnits"]
    assert units["op"] == {
        "name": "set_number_format",
        "field": "display_units",
        "fixed": {"target": "data_labels"},
    }
    assert [o["value"] for o in units["options"]] == [
        "auto",
        "none",
        "thousands",
        "millions",
        "billions",
        "trillions",
    ]
    assert units["theme_default"] == "thousands"
    assert found["labels.labelPrecision"]["min"] == 0
    assert found["labels.labelPrecision"]["max"] == 15
    assert units["studio_render"] is True
    table_units = controls(format_catalog("tableEx"))["columnFormatting.labelDisplayUnits"]
    assert "auto" not in {o["value"] for o in table_units["options"]}


def test_styling_sections_and_generic_writes_are_absent_for_every_type() -> None:
    allowed = {
        "size_position",
        "number_format",
        "conditional_formatting",
        "columns",
        "table_layout",
    }
    for visual_type in pbir_expr.catalog()["visualTypes"]:
        catalog = format_catalog(visual_type)
        assert set(sections(catalog)) <= allowed
        assert "all_properties" not in catalog
        assert all("generic_op" not in c for c in controls(catalog).values())


def test_size_and_position_are_always_offered_with_their_own_operations() -> None:
    for visual_type in ("clusteredColumnChart", "textbox", "gauge", "shape"):
        found = controls(format_catalog(visual_type))
        assert found["position.x"]["op"] == {"name": "set_position", "field": "x", "fixed": {}}
        assert found["position.width"]["min"] == 10
        assert found["position.z"]["op"]["name"] == "arrange"
        assert [o["value"] for o in found["position.z"]["options"]] == [
            "front",
            "forward",
            "backward",
            "back",
        ]
        assert found["position.hidden"]["op"]["name"] == "set_visual_hidden"


def test_unknown_visual_type_is_a_key_error_with_a_plain_message() -> None:
    with pytest.raises(KeyError) as raised:
        format_catalog("notAVisual")
    assert isinstance(raised.value, UnknownVisualType)
    assert str(raised.value) == "Studio has no formatting information for 'notAVisual' visuals"


# --- every control writes where it says ---------------------------------------------------------

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "imported"
ROUND_TRIP_TYPES = (
    "clusteredColumnChart",
    "lineChart",
    "cardVisual",
    "card",
    "slicer",
    "advancedSlicerVisual",
    "tableEx",
    "textbox",
    "shape",
    "gauge",
)


def _written(visual: dict[str, Any]) -> dict[tuple[str, str, str], str]:
    found: dict[tuple[str, str, str], str] = {}
    for holder in ("objects", "visualContainerObjects"):
        for name, entries in (visual["visual"].get(holder) or {}).items():
            for entry in entries:
                for prop, value in (entry.get("properties") or {}).items():
                    key = (holder, name, prop, json.dumps(entry.get("selector"), sort_keys=True))
                    found[key[:3]] = found.get(key[:3], "") + json.dumps(value, sort_keys=True)
    return found


def _sample(control: dict[str, Any]) -> Any:
    kind = control["kind"]
    if kind == "toggle":
        return False
    if kind == "number":
        return max(control.get("min", 0), 14) if control.get("max", 100) >= 14 else 0
    if kind == "color":
        return "#112233"
    if kind == "select":
        return control["options"][-1]["value"] if control["options"] else None
    if kind == "font":
        return "Arial"
    return "Sample"


# --- the conditional family's sections ---------------------------------------------------------

# The twin's model as the editor builds it, with the real model's data types.
_CF_MODEL = twin_model()


def _new_controls(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for s in catalog["sections"] for c in s["controls"] if "scope" in c]


def test_a_table_has_conditional_column_and_layout_sections_after_the_old_ones() -> None:
    catalog = format_catalog("tableEx")
    assert sections(catalog)[-4:] == [
        "number_format",
        "conditional_formatting",
        "columns",
        "table_layout",
    ]
    titles = {s["id"]: s["title"] for s in catalog["sections"]}
    assert (titles["conditional_formatting"], titles["columns"], titles["table_layout"]) == (
        "Conditional formatting",
        "Columns",
        "Table layout",
    )
    found = controls(catalog)
    # The Number format section keeps its controls first; the family's two come after them.
    number = next(s for s in catalog["sections"] if s["id"] == "number_format")["controls"]
    assert [c["id"] for c in number] == [
        "columnFormatting.labelDisplayUnits",
        "columnFormatting.labelPrecision",
        "numberFormat.preset",
        "numberFormat.code",
    ]
    assert found["numberFormat.preset"]["op"] == {
        "name": "set_number_format",
        "field": "preset",
        "fixed": {"target": "column"},
    }
    assert found["numberFormat.code"]["where"] == "query"
    assert found["numberFormat.code"]["studio_render"] is False
    # A column's controls need the pane to name the column: listed, not offered yet.
    for control_id in ("cf.background.minColor", "cf.dataBars", "cf.icons", "columns.headerText"):
        assert found[control_id]["scope"] == "field"
        assert found[control_id]["field_arg"] == "field"
        assert found[control_id]["writable"] is False
    # A header's text is the field's display name, which the extras family writes as Desktop
    # does: {"op": "set_field_display_name", "visual", "field", "name"}.
    assert found["columns.headerText"]["op"] == {
        "name": "set_field_display_name",
        "field": "name",
        "fixed": {},
    }
    assert found["columns.headerText"]["field_arg"] == "field"
    assert found["columns.headerText"]["studio_render"] is True
    assert found["columns.position"]["op"]["name"] == "move_column"
    assert found["cf.icons"]["options"][0] == {
        "value": "ThreeColoredShapes",
        "label": "Three colored shapes",
    }
    assert found["tableLayout.totals"] == {
        **found["tableLayout.totals"],
        "scope": "visual",
        "writable": True,
        "object": "total",
        "property": "totals",
        "op": {"name": "set_table_layout", "field": "totals", "fixed": {}},
    }
    assert "tableLayout.rowSubtotals" not in found
    assert "columns.width" not in found
    assert "all_properties" not in catalog


def test_a_matrix_a_chart_and_a_card_get_their_own_family_controls() -> None:
    matrix = controls(format_catalog("pivotTable"))
    assert {"tableLayout.rowSubtotals", "tableLayout.layout", "tableLayout.valuesOnRows"} <= set(
        matrix
    )
    assert "tableLayout.totals" not in matrix
    assert [o["value"] for o in matrix["tableLayout.layout"]["options"]] == [
        "compact",
        "outline",
        "tabular",
    ]
    chart = format_catalog("columnChart")
    found = controls(chart)
    assert found["cf.dataColors.minColor"]["op"] == {
        "name": "set_conditional_format",
        "field": "min_color",
        "fixed": {"style": "gradient", "target": "data_colors"},
    }
    assert found["cf.dataColors.minColor"]["writable"] is True
    assert found["numberFormat.code"]["property"] == "valueCustomFormatString"
    assert "columns" not in sections(chart) and "table_layout" not in sections(chart)
    card = controls(format_catalog("cardVisual"))
    assert (card["cf.valueColor.measure"]["object"], card["cf.valueColor.measure"]["property"]) == (
        "value",
        "fontColor",
    )
    assert card["cf.valueColor.measure"]["instance_selector"] == {"id": "default"}
    assert card["numberFormat.preset"]["op"]["fixed"] == {"target": "card_value"}
    # Any visual can take its title color and background from a measure.
    slicer = controls(format_catalog("slicer"))
    assert slicer["cf.titleColor.measure"]["where"] == "visualContainerObjects"
    assert slicer["cf.background.measure"]["op"]["fixed"] == {
        "style": "field_value",
        "target": "visual_background",
    }


def _sample_for(control: dict[str, Any]) -> Any:
    if control["kind"] == "color":
        return "#112233"
    if control["kind"] == "toggle":
        return True
    if control["kind"] == "select":
        return next(o["value"] for o in control["options"] if o["value"] != "general")
    if control["op"]["field"] == "measure":
        return "CF Table"
    if control["op"]["field"] == "format":
        return "#,0.0"
    return 14


def _values(visual: dict[str, Any]) -> dict[tuple[str, str, str], str]:
    found = dict(_written(visual))
    found[("query", "query", "format")] = json.dumps(visual["visual"].get("query"), sort_keys=True)
    return found


def test_every_offered_family_control_writes_where_it_says() -> None:
    """Each writable control of the new sections, sent the way the pane sends it, changes the
    object and property it names on a real visual of the twin."""
    from ai_powerbi_analyst.studio import report_edit_conditional as cf

    package = PbixPackage(FIXTURES / "structure-twin.pbix")
    visuals = {
        "tableEx": "e82e02f93055d6b0b544",
        "columnChart": "b404a461ede7604a2774",
        "cardVisual": "fb4c5aa34e0b084b62b9",
        "slicer": "54be5cf0783e66471540",
    }
    landed: set[tuple[str, str]] = set()
    offered: set[tuple[str, str]] = set()
    for visual_type, visual_id in visuals.items():
        for control in _new_controls(format_catalog(visual_type)):
            assert (
                control["op"]["name"] in cf.CONDITIONAL_HANDLERS
                or control["op"]["name"] == "set_field_display_name"
            ), control["id"]
            if not control["writable"]:
                continue
            offered.add((visual_type, control["id"]))
            document = PbirDocument(package)
            recipe = control["op"]
            if recipe["name"] == "clear_conditional_format":
                # Something to remove first: the rule the same section's other control sets.
                first = {
                    "op": "set_conditional_format",
                    "visual": visual_id,
                    "style": "field_value" if visual_type == "cardVisual" else "gradient",
                    "target": control["options"][0]["value"],
                }
                if visual_type == "cardVisual":
                    first["measure"] = "CF Table"
                cf.apply_conditional(document, cf.parse_conditional([first]), _CF_MODEL)
            operation = {
                "op": recipe["name"],
                "visual": visual_id,
                **recipe["fixed"],
                recipe["field"]: _sample_for(control),
            }
            before = _values(copy.deepcopy(document.visual(visual_id)[1]))
            cf.apply_conditional(document, cf.parse_conditional([operation]), _CF_MODEL)
            after = _values(document.visual(visual_id)[1])
            key = (control["where"], control["object"], control["property"])
            if control["where"] == "query":
                key = ("query", "query", "format")
            assert after.get(key) != before.get(key), (visual_type, control["id"])
            landed.add((visual_type, control["id"]))
    assert landed == offered
    assert len(offered) >= 20
