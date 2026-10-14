"""What the Format pane offers for one visual type of an imported report.

Studio does not change styling (colors, fonts, titles, data labels, legends, axes, a visual's
box, backgrounds, themes): Power BI Desktop's own Format pane does that, and the pane here has
no controls for it. What it offers is what Studio edits:

- Size & position: the visual's box, its place in the stack and whether it is hidden
  (``set_position``, ``arrange``, ``set_visual_hidden``).
- Number format: display units, decimals, a preset and a format code for the visual's own
  numbers (``set_number_format``).
- Conditional formatting: color scales, measure-driven colors, data bars and icons
  (``set_conditional_format`` / ``clear_conditional_format``).
- Columns (tables and matrices): a column's header text in this visual
  (``set_field_display_name``) and its place (``move_column``).
- Table layout: totals, subtotals, a matrix's layout and values on rows (``set_table_layout``).

Every control names a property that exists for the type in the formatting catalog
(pbir_catalog.json, Microsoft's per-type schema) or the visual's query, and says:

- ``op``: the edit operation that writes it, as {name, field, fixed}.
- ``studio_render``: whether Studio's canvas draws the property (report_tokens). A property
  that is not drawn is still saved; the pane says "visible in Power BI only".
- ``theme_default``: the value the theme gives the property when the visual sets nothing, for
  the "Auto (theme)" placeholder.
- ``scope``: "visual" when the recipe is complete, or "field" when the pane must name one of the
  visual's columns in the operation's ``field_arg`` (``field``). A field-scoped control is
  ``writable: false`` until the pane can pick the column, so a pane that cannot yet never sends
  an operation that lacks it.

Nothing here needs Power BI Desktop or the report itself, only the catalog and the theme.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from . import pbir_expr, report_props, report_theme
from .report_edit_conditional import ICON_SETS, QUERY_DRAWN, conditional_family
from .report_tokens import family_of, property_is_rendered

Json = dict[str, Any]

CONTAINER = "visualContainerObjects"
OBJECTS = "objects"

# Visual types whose formatting only takes effect under a state selector (measured on Desktop
# 2.157, see "Measured decisions" in docs/imported-report-editing.md): a shape fill written
# without {"id": "default"} is ignored. The edit operations add the selector themselves; the
# pane only needs to know so it can say which state a value applies to.
STATE_SELECTOR_TYPES = frozenset(
    {
        "shape",
        "cardVisual",
        "actionButton",
        "pageNavigator",
        "bookmarkNavigator",
        "filterCard",
        "pivotTable",
    }
)
_BUTTON_STATES = ["default", "hover", "selected", "disabled"]
_STATES: dict[str, list[str]] = {
    "actionButton": _BUTTON_STATES,
    "pageNavigator": _BUTTON_STATES,
    "bookmarkNavigator": _BUTTON_STATES,
}

_SECTION_TITLES: dict[str, str] = {
    "size_position": "Size & position",
    "number_format": "Number format",
    "conditional_formatting": "Conditional formatting",
    "columns": "Columns",
    "table_layout": "Table layout",
}

# Which of the theme's text classes stands behind an object's font when the theme's
# visualStyles say nothing about it (report_theme.text_class).
_TEXT_CLASS: dict[str, str] = {
    "title": "title",
    "subTitle": "title",
    "labels": "label",
    "legend": "label",
    "categoryAxis": "label",
    "valueAxis": "label",
    "categoryLabels": "label",
    "items": "label",
    "header": "header",
    "columnHeaders": "header",
    "values": "label",
    "value": "callout",
    "label": "label",
}
_COLOR_PROPERTIES = frozenset({"fontColor", "color", "labelColor"})
_SIZE_PROPERTIES = frozenset({"fontSize", "textSize"})

# Display units in the words set_number_format takes, and the catalog's numbers for them (the
# theme spells its default as a number).
_UNIT_WORDS: dict[str, str] = {
    "0": "auto",
    "1": "none",
    "1000": "thousands",
    "1000000": "millions",
    "1000000000": "billions",
    "1000000000000": "trillions",
}
_DISPLAY_UNITS: list[Json] = [
    {"value": word, "label": word.capitalize()} for word in _UNIT_WORDS.values()
]
_Z_ORDER = ["front", "forward", "backward", "back"]

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


class UnknownVisualType(KeyError):
    """The catalog has no visual type of that name. Reads as its plain message (the API
    turns a KeyError into a 404 with ``str(error)``)."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else "Unknown visual type"


def _humanize(name: str) -> str:
    words = _CAMEL.sub(" ", name).strip().casefold()
    return words[:1].upper() + words[1:] if words else name


def _face(theme: Mapping[str, Any], name: str) -> str | None:
    for source in (theme, report_theme.DEFAULT_THEME):
        classes = source.get("textClasses")
        found = classes.get(name) if isinstance(classes, dict) else None
        face = found.get("fontFace") if isinstance(found, dict) else None
        if isinstance(face, str) and face:
            return face
    return None


def _theme_default(
    theme: Mapping[str, Any], visual_type: str, obj: str, prop: str, kind: str
) -> Any:
    """What the theme gives ``object.property`` when the visual sets nothing: its
    visualStyles entry for the type (or for all visuals), else the text class its font comes
    from. None when the theme says nothing (the pane then shows just "Auto")."""
    raw = report_props.theme_defaults(theme, visual_type, obj).get(prop)
    if raw is not None:
        if kind == "color":
            return report_props.color(raw, theme)
        return report_props.prop(raw)
    name = _TEXT_CLASS.get(obj)
    if name is None:
        return None
    if prop in _SIZE_PROPERTIES:
        return report_theme.text_class(theme, name)["fontSize"]
    if prop == "fontFamily":
        return _face(theme, name)
    if prop in _COLOR_PROPERTIES:
        return report_theme.text_class(theme, name)["color"]
    return None


def _position_controls() -> list[Json]:
    """Size and position are the visual's box, not catalog properties, so they have their own
    operations. Studio always draws them."""

    def number(name: str, label: str, field: str, low: float) -> Json:
        return {
            "id": f"position.{field}",
            "section": "size_position",
            "label": label,
            "description": None,
            "kind": "number",
            "object": "position",
            "property": name,
            "where": "position",
            "type": "numeric",
            "writable": True,
            "options": None,
            "studio_render": True,
            "instance_selector": None,
            "theme_default": None,
            "unit": "px",
            "min": low,
            "max": 20000,
            "op": {"name": "set_position", "field": field, "fixed": {}},
        }

    return [
        number("x", "Horizontal position", "x", 0),
        number("y", "Vertical position", "y", 0),
        number("width", "Width", "width", 10),
        number("height", "Height", "height", 10),
        {
            "id": "position.z",
            "section": "size_position",
            "label": "Order",
            "description": "Bring the visual to the front or send it to the back",
            "kind": "select",
            "object": "position",
            "property": "z",
            "where": "position",
            "type": "enum",
            "writable": True,
            "options": [{"value": item, "label": _humanize(item)} for item in _Z_ORDER],
            "studio_render": True,
            "instance_selector": None,
            "theme_default": None,
            "op": {"name": "arrange", "field": "to", "fixed": {}},
        },
        {
            "id": "position.hidden",
            "section": "size_position",
            "label": "Hide this visual",
            "description": None,
            "kind": "toggle",
            "object": "position",
            "property": "hidden",
            "where": "position",
            "type": "bool",
            "writable": True,
            "options": None,
            "studio_render": True,
            "instance_selector": None,
            "theme_default": None,
            "op": {"name": "set_visual_hidden", "field": "hidden", "fixed": {}},
        },
    ]


# --- the conditional family's controls ------------------------------------------------------

QUERY = "query"  # a control that writes the visual's query (a column's header, order or format)

_PRESETS: list[Json] = [
    {"value": "general", "label": "General (the field's own format)"},
    {"value": "whole_number", "label": "Whole number"},
    {"value": "one_decimal", "label": "One decimal (0.0)"},
    {"value": "two_decimals", "label": "Two decimals (0.00)"},
    {"value": "percent", "label": "Percentage"},
    {"value": "currency", "label": "Currency"},
    {"value": "thousands", "label": "Thousands (K)"},
    {"value": "millions", "label": "Millions (M)"},
    {"value": "billions", "label": "Billions (bn)"},
]
_MATRIX_LAYOUTS: list[Json] = [
    {"value": item, "label": item.capitalize()} for item in ("compact", "outline", "tabular")
]
_ICON_SET_OPTIONS: list[Json] = [{"value": name, "label": _humanize(name)} for name in ICON_SETS]
_TABLE_CF_TARGETS: list[Json] = [
    {"value": "font_color", "label": "Remove the font color rule"},
    {"value": "background", "label": "Remove the background rule"},
    {"value": "icon", "label": "Remove the icons"},
    {"value": "data_bars", "label": "Remove the data bars"},
    {"value": "web_url", "label": "Remove the web address"},
]
# Where a card keeps its value's color: the new card's value object, the old card's labels.
_CARD_VALUE = {"cardVisual": ("value", "fontColor"), "card": ("labels", "color")}


def _extra_control(
    visual_type: str,
    theme: Mapping[str, Any],
    *,
    control_id: str,
    section: str,
    label: str,
    description: str | None,
    kind: str,
    where: str,
    obj: str,
    prop: str,
    op: str,
    field: str,
    fixed: Json | None = None,
    scope: str = "visual",
    options: list[Json] | None = None,
    limits: Json | None = None,
) -> Json | None:
    """A control that writes a ``conditional`` family operation (or a field's display name),
    or None when the visual type has no such property."""
    spec: Mapping[str, Any] | None = None
    if where == QUERY:
        drawn = QUERY_DRAWN.get(prop, False)
    else:
        found = pbir_expr.property_spec(visual_type, obj, prop)
        if found is None or found[0] != where:
            return None
        spec = found[1]
        drawn = property_is_rendered(visual_type, obj, prop, container=where == CONTAINER)
    control: Json = {
        "id": control_id,
        "section": section,
        "label": label,
        "description": description,
        "kind": kind,
        "object": obj,
        "property": prop,
        "where": where,
        "type": spec.get("type") if spec is not None else None,
        # A field-scoped control needs the pane to name a column; until it can, it is listed but
        # not offered, so no operation goes out without its field.
        "writable": scope == "visual",
        "options": options,
        "studio_render": drawn,
        "instance_selector": (
            {"id": "default"} if visual_type in STATE_SELECTOR_TYPES and where == OBJECTS else None
        ),
        "theme_default": (
            _theme_default(theme, visual_type, obj, prop, kind) if spec is not None else None
        ),
        "op": {"name": op, "field": field, "fixed": dict(fixed or {})},
        "scope": scope,
        "field_arg": "field" if scope == "field" else None,
    }
    control.update(limits or {})
    return control


def _units_and_decimals(
    visual_type: str, theme: Mapping[str, Any], obj: str, target: str, what: str
) -> list[Json | None]:
    """Display units and decimal places of ``obj`` through set_number_format (``target``)."""
    units = _extra_control(
        visual_type,
        theme,
        control_id=f"{obj}.labelDisplayUnits",
        section="number_format",
        label="Display units",
        description=f"Thousands, millions... for {what}",
        kind="select",
        where=OBJECTS,
        obj=obj,
        prop="labelDisplayUnits",
        op="set_number_format",
        field="display_units",
        fixed={"target": target},
        options=[item for item in _DISPLAY_UNITS if target != "column" or item["value"] != "auto"],
    )
    if units is not None and units["theme_default"] is not None:
        # The theme spells its default as the catalog does (a number); the pane shows words.
        units["theme_default"] = _UNIT_WORDS.get(str(units["theme_default"]))
    decimals = _extra_control(
        visual_type,
        theme,
        control_id=f"{obj}.labelPrecision",
        section="number_format",
        label="Decimal places",
        description=f"Decimal places of {what}",
        kind="number",
        where=OBJECTS,
        obj=obj,
        prop="labelPrecision",
        op="set_number_format",
        field="decimals",
        fixed={"target": target},
        limits={"min": 0, "max": 15},
    )
    return [units, decimals]


def _number_controls(visual_type: str, theme: Mapping[str, Any]) -> list[Json]:
    """Display units, decimals, a preset and a format code for the visual's own numbers: a
    chart's data labels (and value axis), a card's value, every value column of a table or
    matrix."""
    family = conditional_family(visual_type)
    made: list[Json | None] = []
    if family == "table":
        target, units = "column", (OBJECTS, "columnFormatting")
        code: tuple[str, str, str] = (QUERY, QUERY, "format")
        made += _units_and_decimals(visual_type, theme, "columnFormatting", target, "the values")
    elif visual_type == "cardVisual":
        target, units = "card_value", (OBJECTS, "value")
        code = (OBJECTS, "value", "customFormatString")
        made += _units_and_decimals(visual_type, theme, "value", target, "the value")
    elif family == "card":
        target, units = "card_value", (OBJECTS, "labels")
        code = (QUERY, QUERY, "format")
        made += _units_and_decimals(visual_type, theme, "labels", target, "the value")
    elif pbir_expr.property_spec(visual_type, "labels", "labelDisplayUnits") is not None:
        target, units = "data_labels", (OBJECTS, "labels")
        custom = pbir_expr.property_spec(visual_type, "labels", "valueCustomFormatString")
        code = (
            (OBJECTS, "labels", "valueCustomFormatString") if custom else (QUERY, QUERY, "format")
        )
        made += _units_and_decimals(visual_type, theme, "labels", target, "the data labels")
        made += _units_and_decimals(visual_type, theme, "valueAxis", "value_axis", "the axis")
    else:
        return []
    made += [
        _extra_control(
            visual_type,
            theme,
            control_id="numberFormat.preset",
            section="number_format",
            label="Format",
            description="A common number format for this visual only",
            kind="select",
            where=units[0],
            obj=units[1],
            prop="labelDisplayUnits",
            op="set_number_format",
            field="preset",
            fixed={"target": target},
            options=list(_PRESETS),
        ),
        _extra_control(
            visual_type,
            theme,
            control_id="numberFormat.code",
            section="number_format",
            label="Format code",
            description="A Power BI format code such as #,0.00 or 0.0%",
            kind="text",
            where=code[0],
            obj=code[1],
            prop=code[2],
            op="set_number_format",
            field="format",
            fixed={"target": target},
        ),
    ]
    return [control for control in made if control is not None]


def _conditional_controls(visual_type: str, theme: Mapping[str, Any]) -> list[Json]:
    family = conditional_family(visual_type)
    section = "conditional_formatting"
    made: list[Json | None] = []

    def add(**settings: Any) -> None:
        made.append(_extra_control(visual_type, theme, section=section, **settings))

    if family == "chart":
        for end, words in (("min", "lowest"), ("max", "highest")):
            add(
                control_id=f"cf.dataColors.{end}Color",
                label=f"Color of the {words} values",
                description="Colors the bars by value, on a color scale",
                kind="color",
                where=OBJECTS,
                obj="dataPoint",
                prop="fill",
                op="set_conditional_format",
                field=f"{end}_color",
                fixed={"style": "gradient", "target": "data_colors"},
            )
        add(
            control_id="cf.dataColors.clear",
            label="Remove the color rule",
            description=None,
            kind="select",
            where=OBJECTS,
            obj="dataPoint",
            prop="fill",
            op="clear_conditional_format",
            field="target",
            options=[{"value": "data_colors", "label": "Remove"}],
        )
    elif family == "card":
        obj, prop = _CARD_VALUE[visual_type]
        add(
            control_id="cf.valueColor.measure",
            label="Value color from a measure",
            description='A measure that returns a color name or hex, like "red" or "#D64550"',
            kind="text",
            where=OBJECTS,
            obj=obj,
            prop=prop,
            op="set_conditional_format",
            field="measure",
            fixed={"style": "field_value", "target": "font_color"},
        )
        add(
            control_id="cf.valueColor.clear",
            label="Remove the value color rule",
            description=None,
            kind="select",
            where=OBJECTS,
            obj=obj,
            prop=prop,
            op="clear_conditional_format",
            field="target",
            options=[{"value": "font_color", "label": "Remove"}],
        )
    elif family == "table":
        for end, words in (("min", "lowest"), ("max", "highest")):
            add(
                control_id=f"cf.background.{end}Color",
                label=f"Background of the {words} values",
                description="Shades the column's cells by value, on a color scale",
                kind="color",
                where=OBJECTS,
                obj="values",
                prop="backColor",
                op="set_conditional_format",
                field=f"{end}_color",
                fixed={"style": "gradient", "target": "background"},
                scope="field",
            )
        add(
            control_id="cf.fontColor.measure",
            label="Font color from a measure",
            description="A measure that returns a color name or hex for each row",
            kind="text",
            where=OBJECTS,
            obj="values",
            prop="fontColor",
            op="set_conditional_format",
            field="measure",
            fixed={"style": "field_value", "target": "font_color"},
            scope="field",
        )
        add(
            control_id="cf.dataBars",
            label="Data bars",
            description="Bars in the cells, as long as the values",
            kind="select",
            where=OBJECTS,
            obj="columnFormatting",
            prop="dataBars",
            op="set_conditional_format",
            field="style",
            options=[{"value": "data_bars", "label": "Show data bars"}],
            scope="field",
        )
        add(
            control_id="cf.icons",
            label="Icons",
            description="An icon set split over the range of the values",
            kind="select",
            where=OBJECTS,
            obj="values",
            prop="icon",
            op="set_conditional_format",
            field="icon_set",
            fixed={"style": "icons"},
            options=list(_ICON_SET_OPTIONS),
            scope="field",
        )
        add(
            control_id="cf.clear",
            label="Remove conditional formatting",
            description=None,
            kind="select",
            where=OBJECTS,
            obj="values",
            prop="fontColor",
            op="clear_conditional_format",
            field="target",
            options=list(_TABLE_CF_TARGETS),
            scope="field",
        )
    for control_id, label, obj, prop, target in (
        (
            "cf.titleColor.measure",
            "Title color from a measure",
            "title",
            "fontColor",
            "title_color",
        ),
        (
            "cf.background.measure",
            "Background color from a measure",
            "background",
            "color",
            "visual_background",
        ),
    ):
        add(
            control_id=control_id,
            label=label,
            description="A measure that returns a color name or hex",
            kind="text",
            where=CONTAINER,
            obj=obj,
            prop=prop,
            op="set_conditional_format",
            field="measure",
            fixed={"style": "field_value", "target": target},
        )
    return [control for control in made if control is not None]


def _column_controls(visual_type: str, theme: Mapping[str, Any]) -> list[Json]:
    """One column's header text in this visual and its place: every control names the column
    (scope field)."""
    if conditional_family(visual_type) != "table":
        return []
    made: list[Json | None] = []

    def add(**settings: Any) -> None:
        made.append(
            _extra_control(visual_type, theme, section="columns", scope="field", **settings)
        )

    # A header's text is the field's display name in this visual, which the extras family's
    # set_field_display_name writes the way Desktop does (display name and native name of every
    # projection of the field, visual calculations that use the old name).
    add(
        control_id="columns.headerText",
        label="Header text",
        description="The column's name in this visual only",
        kind="text",
        where=QUERY,
        obj=QUERY,
        prop="displayName",
        op="set_field_display_name",
        field="name",
    )
    add(
        control_id="columns.position",
        label="Position",
        description="0 is the first column",
        kind="number",
        where=QUERY,
        obj=QUERY,
        prop="order",
        op="move_column",
        field="to_index",
        limits={"min": 0, "max": 100},
    )
    return [control for control in made if control is not None]


def _table_layout_controls(visual_type: str, theme: Mapping[str, Any]) -> list[Json]:
    """Totals, subtotals, a matrix's layout and values on rows (the table's structure)."""
    if conditional_family(visual_type) != "table":
        return []
    matrix = visual_type == "pivotTable"
    made: list[Json | None] = []

    def toggle(control_id: str, label: str, obj: str, prop: str, field: str) -> None:
        made.append(
            _extra_control(
                visual_type,
                theme,
                control_id=control_id,
                section="table_layout",
                label=label,
                description=None,
                kind="toggle",
                where=OBJECTS,
                obj=obj,
                prop=prop,
                op="set_table_layout",
                field=field,
            )
        )

    if matrix:
        toggle(
            "tableLayout.rowSubtotals",
            "Row subtotals",
            "subTotals",
            "rowSubtotals",
            "row_subtotals",
        )
        toggle(
            "tableLayout.columnSubtotals",
            "Column subtotals",
            "subTotals",
            "columnSubtotals",
            "column_subtotals",
        )
        made.append(
            _extra_control(
                visual_type,
                theme,
                control_id="tableLayout.layout",
                section="table_layout",
                label="Layout",
                description="Compact is the stepped layout",
                kind="select",
                where=OBJECTS,
                obj="general",
                prop="layout",
                op="set_table_layout",
                field="layout",
                options=list(_MATRIX_LAYOUTS),
            )
        )
        toggle(
            "tableLayout.valuesOnRows", "Values on rows", "values", "valuesOnRow", "values_on_rows"
        )
    else:
        toggle("tableLayout.totals", "Totals", "total", "totals", "totals")
    return [control for control in made if control is not None]


def format_catalog(visual_type: str, theme: Mapping[str, Any] | None = None) -> Json:
    """The Format pane's contents for one visual type.

    ``theme`` is the report's merged theme (report_formatting.theme); None means the Power BI
    default. Raises ``UnknownVisualType`` (a KeyError) for a type the catalog does not have.

    Returns ``{visual_type, family, studio_draws, instance_selector, sections}``. ``sections`` is
    the list in pane order, each ``{id, title, controls}``; a section with no controls for this
    type is left out, except Size & position. ``studio_draws`` is False for a type Studio only
    frames (gauge, treemap, ...): every control still edits the report, and each says
    ``studio_render: false`` where the canvas does not draw it. There are no styling controls:
    Studio does not change colors, fonts, titles, labels, legends, axes or backgrounds.
    """
    entry = pbir_expr.catalog()["visualTypes"].get(visual_type)
    if not isinstance(entry, dict):
        raise UnknownVisualType(f"Studio has no formatting information for {visual_type!r} visuals")
    active: Mapping[str, Any] = theme if theme is not None else report_theme.DEFAULT_THEME
    family = family_of(visual_type)
    buckets: dict[str, list[Json]] = {name: [] for name in _SECTION_TITLES}
    buckets["size_position"] = _position_controls()
    for control in (
        *_number_controls(visual_type, active),
        *_conditional_controls(visual_type, active),
        *_column_controls(visual_type, active),
        *_table_layout_controls(visual_type, active),
    ):
        buckets[control["section"]].append(control)
    sections = [
        {"id": name, "title": _SECTION_TITLES[name], "controls": controls}
        for name, controls in buckets.items()
        if controls
    ]
    return {
        "visual_type": visual_type,
        "family": family,
        "studio_draws": family != "unsupported",
        "instance_selector": (
            {"id": "default", "states": _STATES.get(visual_type, ["default"])}
            if visual_type in STATE_SELECTOR_TYPES
            else None
        ),
        "sections": sections,
    }
