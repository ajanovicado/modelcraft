"""Conditional formatting, number formats and table/matrix layout (family ``conditional``).

What a Power BI developer does in the Format pane's fx dialogs, the number-format cards and the
table/matrix layout settings, written the way Power BI Desktop 2.157 writes it:

- ``set_conditional_format`` / ``clear_conditional_format``: color scales (``FillRule``), rules
  (``Conditional`` cases), field values (a measure that returns a color, an icon, a web address
  or text), data bars and icons, on a table or matrix column, a chart's data colors, a card's
  value, and (field value only) a visual's title and background.
- ``set_number_format``: display units, decimals, presets and format codes on data labels, the
  value axis, a card's value, a table or matrix column, or any field of a visual. Only this visual
  changes; a measure's own format string is a model change (``update_measure``). The settings
  Desktop always keeps come first; a field's visual-level format string (a projection's
  ``format``, kept by Desktop only behind its preview switches) is used only where nothing else
  can say the code, and the receipt warns (``FORMAT_WARNING``).
- ``set_table_layout``, ``move_column``: totals, subtotals, a matrix's layout and values on rows;
  column order. A column's header text is the extras family's ``set_field_display_name``.

Styling (a column's width, alignment and colors, banded rows, grid lines, row padding, word wrap)
is not Studio's: the operation and the settings that wrote it were removed, and chat refuses it.

Where the shapes come from (``DESKTOP_AUTHORED_SHAPES`` and ``UNVERIFIED_SHAPES`` list each one):

- Desktop-authored, from the Agents dashboard's table: a ``Measure`` font color under
  ``{"data": [{"dataViewWildcard": {"matchingOption": 1}}], "metadata": queryRef}``; data bars in
  ``columnFormatting.dataBars`` under ``{"metadata": queryRef}`` (fill definitions and literal
  flags); ``total.totals``.
- Read from Power BI Desktop 2.157's own code (its conditional-formatting dialog and query
  serializer, shipped in the app) and its embedded PBIR schemas, not yet from a file Desktop saved:
  the ``FillRule`` gradient (stop colors are bare ``{"Literal"}`` nodes with no ``expr`` wrapper,
  the mistake Microsoft's validator reports as ``PBIR_FILLRULE_STOP_DOUBLE_WRAP``; stop values are
  doubles; ``nullColoringStrategy`` is ``asZero``, ``noColor`` or ``specificColor``); rules as
  ``Conditional`` cases whose numeric thresholds are always doubles and whose percent thresholds are
  ``RangePercent`` over the visual's lowest and highest value; text rules tagged with the
  dialog's ``Annotations`` (ordinal evaluation and the operator chosen); the icon definition
  ``{"kind": "Icon", "layout", "verticalAlignment", "value"}`` and the icon set names; data bar
  minimum and maximum typed by the field (whole numbers ``L`` rounded, fixed decimals ``M``, else
  ``D``); a chart's rule under ``{"data": [{"dataViewWildcard": {"matchingOption": 1}}]}``; the
  visual-level format string as the projection's ``format``; a custom format code as display units
  ``-1`` (Custom) with ``customFormatString``/``valueCustomFormatString``; a matrix's layout as
  ``general.layout`` (Desktop upgrades the old ``rowHeaders.stepped`` into it).

The lead's Desktop probes (``DESKTOP_PROBES``) decide the unverified ones.

Studio never silently destroys conditional formatting: writing over an existing rule, scale,
field value, data bars or icons stops with ``NeedsConfirmation("replace_conditional")`` naming
what would be lost, and the confirmed write lists it in ``replaced``. Changing only part of an
existing color scale, data bars or icon placement edits them in place (nothing is lost).

What Studio's canvas draws (``VisibleIn`` uses ``report_tokens.property_is_rendered``'s answer at
the time of the edit): today table font colors, data bars, column alignment and widths, header
texts and column order, card and chart colors; a renderer change in progress adds gradients, rule
cases, icons and visual-level number formats, after which the same edits report ``studio``.

The family is registered by the integration step (``report_edits``): ``ConditionalOperation``,
``CONDITIONAL_OPERATIONS`` and ``CONDITIONAL_HANDLERS``. ``apply_conditional`` mirrors
``report_edits.apply_operations`` for this family alone.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, ValidationError

from ai_powerbi_analyst.contracts import ClosedModel

from . import pbir_expr as expr
from .edit_tools import ProbeCase, Slot, ToolCard
from .pbir_document import DocumentError, PbirDocument
from .report_edit_context import (
    Choice,
    EditOutcome,
    Handler,
    HandlerResult,
    ModelInfo,
    NeedsChoice,
    NeedsConfirmation,
    VisibleIn,
    _objects,
    _projections,
    _visual_type,
)
from .report_edit_geometry import describe, resolve_leaf, sentence
from .report_edit_ops import ColorValue, FieldRef, ThemeColorRef, operation_classes
from .report_roles import projection_label
from .report_tokens import property_is_rendered

FAMILY = "conditional"

# --- evidence ---------------------------------------------------------------------------------

DESKTOP_AUTHORED_SHAPES: dict[str, str] = {
    "values.fontColor field value": (
        "Agents table: {solid: {color: {expr: {Measure}}}} under {data: [dataViewWildcard "
        "matchingOption 1], metadata: queryRef}"
    ),
    "columnFormatting.dataBars": (
        "Agents table: positiveColor/negativeColor/axisColor fills, reverseDirection and hideText "
        "literals, under {metadata: queryRef}; no minValue/maxValue when automatic"
    ),
    "total.totals": "Agents table: a selector-less boolean",
    "value (new card) per-field entry": (
        "Agents cards: value font and size under {metadata: queryRef} and {id: 'default'}"
    ),
}

UNVERIFIED_SHAPES: dict[str, str] = {
    "FillRule gradient": (
        "Desktop 2.157 dialog code and semanticQuery schema: Input + linearGradient2/3 with bare "
        "Literal stop colors, double stop values, nullColoringStrategy"
    ),
    "Conditional rules": (
        "Desktop 2.157 dialog code: Cases of Comparison/And conditions, double thresholds, a "
        "Literal hex or icon name as the value; a catch-all case is Literal true; the first "
        "matching case wins (Desktop's dialog lists its rules in the reverse of the file's order)"
    ),
    "RangePercent threshold": (
        "Desktop 2.157 serializer: RangePercent{Min, Max: ScopedEval of Aggregation(Min/Max) of "
        "ScopedEval(input, [AllRolesRef]), Percent}"
    ),
    "text rule": (
        "Desktop 2.157 dialog code (TextConditionViewMode): Comparison Equal against a text "
        "literal (or null for 'is blank') with Annotations {'PowerBI.SQExprEvaluationKind': 1 "
        "(ordinal), 'PowerBI.SQExprTextOperatorOption': 2 (is) or 5 (is blank)}"
    ),
    "values.backColor rules/gradient/field value": "same selector as the verified font color",
    "values.icon": (
        "formattingObjectDefinitions IconDefinition {kind: 'Icon', layout, verticalAlignment, "
        "value}; icon set names from Desktop 2.157's theme code"
    ),
    "values.webURL": "field value as {expr: Measure|Aggregation} under the column's selector",
    "dataBars minValue/maxValue/totalMatchingOption": "Desktop 2.157 dialog code (typed literals)",
    "dataPoint.fill conditional": (
        "Desktop 2.157 upgrade code: {data: [dataViewWildcard matchingOption 1]} "
        "(+ metadata for one series)"
    ),
    "cardVisual value.fontColor conditional": (
        "guessed {data: [dataViewWildcard matchingOption 1], metadata: queryRef}"
    ),
    "card labels.color conditional": "guessed {data: [dataViewWildcard matchingOption 1]}",
    "title.text / title.fontColor / background.color field value": (
        "Desktop 2.157 feature registry lists them as expression-bound; selector-less entry"
    ),
    "projection.format": (
        "Desktop 2.157 PBIR serializer: columnProperties[queryRef].formatString <-> projection "
        "format, written and read only behind the visual-level format string preview switches "
        "(off by default: Desktop then drops it on save); used only where nothing else can say "
        "the code, with a warning"
    ),
    "labels.labelDisplayUnits -1 + valueCustomFormatString": "Desktop 2.157 display-unit code",
    "labels per series {metadata: queryRef}": (
        "a chart's data-label number format for one of several values; guessed from the new "
        "card's per-field value entries"
    ),
    "value.labelDisplayUnits -1 + customFormatString": "Desktop 2.157 display-unit code",
    "rowHeaders.stepped removed with general.layout": (
        "Desktop 2.157 upgrade code: stepped true is Compact, false is Outline (row subtotals on "
        "top) or Tabular"
    ),
    "columnFormatting.labelDisplayUnits/labelPrecision": "catalog types, never saved by Desktop",
    "valueAxis.labelDisplayUnits/labelPrecision": "catalog types",
    "subTotals.rowSubtotals/columnSubtotals": "catalog types, selector-less entry",
    "general.layout": "Desktop 2.157 upgrade code: general.layout 'Compact'/'Outline'/'Tabular'",
    "values.valuesOnRow": "catalog type",
    "projection order (move_column)": "the order of a role's projections",
}

# Icon sets as Desktop 2.157 offers them, lowest icon first (its theme code's iconSets).
ICON_SETS: dict[str, tuple[str, ...]] = {
    "ThreeColoredShapes": ("SignLow", "SignMedium", "CircleHigh"),
    "ThreeTrafficLightsUnrimmed": (
        "CircleRedPatternFill",
        "CircleYellowPatternFill",
        "CircleGreenPatternFill",
    ),
    "ThreeTrafficLightsRimmed": (
        "CircleRedBlackBackgroundPatternFill",
        "CircleYellowBlackBackgroundPatternFill",
        "CircleGreenBlackBackgroundPatternFill",
    ),
    "ThreeTrafficLightsLightRimmed": (
        "CircleRedBlackOutlinePatternFill",
        "CircleYellowBlackOutlinePatternFill",
        "CircleGreenBlackOutlinePatternFill",
    ),
    "ThreeColoredArrows": ("ColoredArrowDown", "ColoredArrowRight", "ColoredArrowUp"),
    "ThreeGrayedArrows": ("GreyArrowDown", "GreyArrowRight", "GreyArrowUp"),
    "ThreeColoredFlags": ("FlagRedPatternFill", "FlagYellowPatternFill", "FlagGreenPatternFill"),
    "ThreeColoredSymbols": ("CircleSymbolLow", "CircleSymbolMedium", "CircleSymbolHigh"),
    "ThreeUncircledColorSymbols": ("SymbolLow", "SymbolMedium", "SymbolHigh"),
    "ThreeGrayStars": ("StarLow", "StarMedium", "StarHigh"),
    "ThreeGoldStars": ("StarLow", "StarMediumLight", "StarHighLight"),
    "ThreeTriangles": ("TriangleLow", "TriangleMedium", "TriangleHigh"),
    "FourColoredArrows": (
        "ColoredArrowDown",
        "ColoredArrowDownRight",
        "ColoredArrowUpRight",
        "ColoredArrowUp",
    ),
    "FourGrayArrows": ("GreyArrowDown", "GreyArrowDownRight", "GreyArrowUpRight", "GreyArrowUp"),
    "FourColoredCirclesBlackToRed": (
        "CircleBlackFill",
        "CircleGrayPatternFill",
        "CirclePurplePatternFill",
        "CircleRedPatternFill",
    ),
    "FourGraySignalMeters": (
        "SignalBarLow",
        "SignalBarMedium",
        "SignalBarMedium2",
        "SignalBarFull",
    ),
    "FourColoredSignalMeters": (
        "SignalBarLowColored",
        "SignalBarMediumColored",
        "SignalBarMedium2Colored",
        "SignalBarFullColored",
    ),
    "FourColoredCircles": (
        "CircleBlackFill",
        "CircleRedPatternFill",
        "CircleYellowPatternFill",
        "CircleGreenPatternFill",
    ),
    "FiveColoredArrows": (
        "ColoredArrowDown",
        "ColoredArrowDownRight",
        "ColoredArrowRight",
        "ColoredArrowUpRight",
        "ColoredArrowUp",
    ),
    "FiveArrows": (
        "GreyArrowDown",
        "GreyArrowDownRight",
        "GreyArrowRight",
        "GreyArrowUpRight",
        "GreyArrowUp",
    ),
    "FiveSignalMeters": (
        "SignalBarEmpty",
        "SignalBarLow",
        "SignalBarMedium",
        "SignalBarMedium2",
        "SignalBarFull",
    ),
    "FiveColoredSignalMeters": (
        "SignalBarEmpty",
        "SignalBarLowColored",
        "SignalBarMediumColored",
        "SignalBarMedium2Colored",
        "SignalBarFullColored",
    ),
    "FivePieCharts": ("CircleEmpty", "Circle75", "CircleHalf", "Circle25", "CircleFilled"),
    "FiveBoxes": ("QuadrantEmpty", "Quadrant25", "Quadrant50", "Quadrant75", "QuadrantFull"),
    "FiveColoredBoxes": (
        "QuadrantEmpty",
        "Quadrant25Colored",
        "Quadrant50Colored",
        "Quadrant75Colored",
        "QuadrantFullColored",
    ),
}
# Every icon Studio writes by name: the ones the sets use, plus the three Desktop starts the rules
# dialog with and the plain circles of its icon list.
ICON_NAMES: frozenset[str] = frozenset(
    {name for icons in ICON_SETS.values() for name in icons}
    | {"CircleHigh", "CircleMedium", "CircleLow", "CircleGreenBlackOutlinePatternFill"}
)
_ICON_LOOKUP = {name.casefold(): name for name in ICON_NAMES}
_ICON_SET_WORDS = {
    "ThreeColoredShapes": "3 colored shapes",
    "ThreeTrafficLightsUnrimmed": "3 traffic lights",
    "ThreeTrafficLightsRimmed": "3 rimmed traffic lights",
    "ThreeTrafficLightsLightRimmed": "3 light-rimmed traffic lights",
    "ThreeColoredArrows": "3 colored arrows",
    "ThreeGrayedArrows": "3 grey arrows",
    "ThreeColoredFlags": "3 flags",
    "ThreeColoredSymbols": "3 circled symbols",
    "ThreeUncircledColorSymbols": "3 symbols",
    "ThreeGrayStars": "3 grey stars",
    "ThreeGoldStars": "3 gold stars",
    "ThreeTriangles": "3 triangles",
    "FourColoredArrows": "4 colored arrows",
    "FourGrayArrows": "4 grey arrows",
    "FourColoredCirclesBlackToRed": "4 circles, black to red",
    "FourGraySignalMeters": "4 grey signal bars",
    "FourColoredSignalMeters": "4 colored signal bars",
    "FourColoredCircles": "4 colored circles",
    "FiveColoredArrows": "5 colored arrows",
    "FiveArrows": "5 grey arrows",
    "FiveSignalMeters": "5 grey signal bars",
    "FiveColoredSignalMeters": "5 colored signal bars",
    "FivePieCharts": "5 pie charts",
    "FiveBoxes": "5 boxes",
    "FiveColoredBoxes": "5 colored boxes",
}

# --- the operations ---------------------------------------------------------------------------

Style = Literal["gradient", "rules", "field_value", "data_bars", "icons"]
Target = Literal[
    "font_color",
    "background",
    "data_colors",
    "icon",
    "data_bars",
    "web_url",
    "title_text",
    "title_color",
    "visual_background",
]
Summarization = Literal[
    "sum",
    "average",
    "min",
    "max",
    "count",
    "count_distinct",
    "median",
    "first",
    "last",
]
IconSetName = Literal[
    "ThreeColoredShapes",
    "ThreeTrafficLightsUnrimmed",
    "ThreeTrafficLightsRimmed",
    "ThreeTrafficLightsLightRimmed",
    "ThreeColoredArrows",
    "ThreeGrayedArrows",
    "ThreeColoredFlags",
    "ThreeColoredSymbols",
    "ThreeUncircledColorSymbols",
    "ThreeGrayStars",
    "ThreeGoldStars",
    "ThreeTriangles",
    "FourColoredArrows",
    "FourGrayArrows",
    "FourColoredCirclesBlackToRed",
    "FourGraySignalMeters",
    "FourColoredSignalMeters",
    "FourColoredCircles",
    "FiveColoredArrows",
    "FiveArrows",
    "FiveSignalMeters",
    "FiveColoredSignalMeters",
    "FivePieCharts",
    "FiveBoxes",
    "FiveColoredBoxes",
]
ApplyTo = Literal["values", "values_and_totals", "totals"]
FieldName = Annotated[str, Field(min_length=1, max_length=200)]


class Rule(ClosedModel):
    """One rule of a rules format: ``if value >= min and < max then color`` (or ``icon``).

    A bound left out is open (``min`` alone is "from min up", neither is "every other value").
    ``unit`` "percent" measures the bounds as a percent of the range between the visual's lowest
    and highest value (0 to 100), as Desktop's Percent option does. ``equals`` matches one value
    (a number, or text on a text field) and ``blank`` matches empty values. The first rule that
    matches decides.
    """

    color: ColorValue | None = None
    icon: str | None = Field(default=None, max_length=60)
    min: float | None = None
    max: float | None = None
    min_inclusive: bool = True
    max_inclusive: bool = False
    unit: Literal["number", "percent"] = "number"
    equals: float | Annotated[str, Field(max_length=200)] | None = None
    blank: bool = False


class SetConditionalFormat(ClosedModel):
    """Conditional formatting: a color scale, rules, a field's values, data bars or icons.

    ``target`` is what gets formatted: a table or matrix column's ``font_color``,
    ``background``, ``icon``, ``data_bars`` or ``web_url``; a chart's ``data_colors``; a card's
    value ``font_color``; or (field value only) the visual's ``title_text``, ``title_color`` or
    ``visual_background``. ``field`` names the column (or a chart's series, or a card's field) as
    its header reads. ``based_on`` is the field whose values decide (by default the formatted field
    itself) with ``summarization`` for a column.

    - gradient: ``min_color``/``max_color`` (and ``mid_color`` for three colors); ``min_value``,
      ``mid_value``, ``max_value`` fix a stop at a number (left out: lowest, middle, highest);
      ``blank`` says how empty values are colored (as zero by default).
    - rules: ``rules`` in order, the first that matches decides.
    - field_value: ``measure`` returns the color (a name or hex), icon, web address or text.
    - data_bars: ``positive_color``, ``negative_color``, ``axis_color``, ``direction``,
      ``bar_only``, and ``min_value``/``max_value`` (left out: lowest and highest).
    - icons: ``icon_set`` (split over the range like Desktop does) or ``rules`` with icons;
      ``icon_position`` and ``icon_alignment`` place them.

    Existing conditional formatting on the same spot is only replaced with ``replace_conditional``.
    """

    op: Literal["set_conditional_format"] = "set_conditional_format"
    visual: str
    style: Style
    target: Target | None = None
    field: FieldName | None = None
    based_on: FieldRef | None = None
    summarization: Summarization | None = None
    apply_to: ApplyTo = "values"
    min_color: ColorValue | None = None
    mid_color: ColorValue | None = None
    max_color: ColorValue | None = None
    min_value: float | None = None
    mid_value: float | None = None
    max_value: float | None = None
    blank: Literal["as_zero", "no_color", "specific_color"] | None = None
    blank_color: ColorValue | None = None
    rules: list[Rule] | None = Field(default=None, min_length=1, max_length=20)
    measure: FieldRef | FieldName | None = None
    positive_color: ColorValue | None = None
    negative_color: ColorValue | None = None
    axis_color: ColorValue | None = None
    direction: Literal["left_to_right", "right_to_left"] | None = None
    bar_only: bool | None = None
    icon_set: IconSetName | None = None
    icon_position: Literal["left", "right", "icon_only"] | None = None
    icon_alignment: Literal["top", "middle", "bottom"] | None = None
    replace_conditional: bool = False


class ClearConditionalFormat(ClosedModel):
    """Remove conditional formatting. With a ``target`` and ``field`` only that one; with a
    ``field`` every kind on that column; with a ``target`` that kind on every column; with
    neither, every color rule, scale, field value, data bar and icon of the visual (a title bound
    to a measure only goes when ``target`` is ``title_text``). The plain formatting underneath
    shows again."""

    op: Literal["clear_conditional_format"] = "clear_conditional_format"
    visual: str
    target: Target | None = None
    field: FieldName | None = None


NumberTarget = Literal["data_labels", "value_axis", "card_value", "column", "field"]
NumberPreset = Literal[
    "general",
    "whole_number",
    "one_decimal",
    "two_decimals",
    "percent",
    "currency",
    "thousands",
    "millions",
    "billions",
]
DisplayUnits = Literal["auto", "none", "thousands", "millions", "billions", "trillions"]


class SetNumberFormat(ClosedModel):
    """How the numbers of one visual read: a ``preset``, a Power BI ``format`` code such as
    ``#,0.00`` or ``0.0%``, ``display_units`` and ``decimals``.

    ``target``: a chart's ``data_labels`` (the default for charts) or ``value_axis``, a card's
    ``card_value`` (the default for cards), a table or matrix ``column`` (``field`` names it; left
    out, every value column), or any ``field`` of the visual. Only this visual changes: a
    measure's own format string, used everywhere, is changed with ``update_measure``.
    """

    op: Literal["set_number_format"] = "set_number_format"
    visual: str
    target: NumberTarget | None = None
    field: FieldName | None = None
    preset: NumberPreset | None = None
    format: str | None = Field(default=None, min_length=1, max_length=255)
    display_units: DisplayUnits | None = None
    decimals: int | None = Field(default=None, ge=0, le=15)
    currency_symbol: str | None = Field(default=None, min_length=1, max_length=5)
    replace_conditional: bool = False


class SetTableLayout(ClosedModel):
    """The layout of a table or matrix: totals (table), row and column subtotals, stepped layout
    and ``layout`` (matrix), values shown on rows (matrix). How it looks (banded rows, grid lines,
    row padding, word wrap) is styling, which Studio does not change."""

    op: Literal["set_table_layout"] = "set_table_layout"
    visual: str
    totals: bool | None = None
    row_subtotals: bool | None = None
    column_subtotals: bool | None = None
    stepped: bool | None = None
    layout: Literal["compact", "outline", "tabular"] | None = None
    values_on_rows: bool | None = None
    replace_conditional: bool = False


class MoveColumn(ClosedModel):
    """Move a column of a table (or a field of a matrix well) to another place: ``to_index`` 0 is
    first, among the fields of the same well."""

    op: Literal["move_column"] = "move_column"
    visual: str
    field: FieldName
    to_index: int = Field(ge=0, le=100)


ConditionalOperation = (
    SetConditionalFormat | ClearConditionalFormat | SetNumberFormat | SetTableLayout | MoveColumn
)
CONDITIONAL_OPERATIONS = operation_classes(ConditionalOperation)
_ADAPTER: TypeAdapter[Any] = TypeAdapter(Annotated[ConditionalOperation, Field(discriminator="op")])

# --- constants of the shapes ------------------------------------------------------------------

_CONTAINER = "visualContainerObjects"
_OBJECTS = "objects"
_TABLE_TYPES = frozenset({"tableEx", "pivotTable"})
# The object and property of a card's value color.
_CARD_VALUE_COLOR = {"cardVisual": ("value", "fontColor"), "card": ("labels", "color")}
_MATCHING = {"values": 1, "values_and_totals": 0, "totals": 2}
# Power BI's aggregate function codes (its QueryAggregateFunction): Count is the distinct count
# and CountNonNull the plain one; First and Last of a text field are Min and Max.
_FUNCTIONS: dict[str, int] = {
    "sum": 0,
    "average": 1,
    "count_distinct": 2,
    "min": 3,
    "max": 4,
    "count": 5,
    "median": 6,
    "first": 3,
    "last": 4,
}
_SUMMARIZE_BY = {
    "sum": "sum",
    "average": "average",
    "min": "min",
    "max": "max",
    "count": "count",
    "distinctcount": "count_distinct",
}
_NUMERIC = frozenset({"integer", "double", "decimal"})
_EQUAL, _GREATER, _GREATER_EQUAL, _LESS, _LESS_EQUAL = 0, 1, 2, 3, 4
_LAYOUT_WORDS = {"left": "Before", "right": "After", "icon_only": "IconOnly"}
_ALIGN_WORDS = {"top": "Top", "middle": "Middle", "bottom": "Bottom"}
_BLANK_STRATEGY = {"as_zero": "asZero", "no_color": "noColor", "specific_color": "specificColor"}
_UNITS: dict[str, int] = {
    "auto": 0,
    "none": 1,
    "thousands": 1000,
    "millions": 1000000,
    "billions": 1000000000,
    "trillions": 1000000000000,
}
_CUSTOM_UNITS = -1  # Desktop's DisplayUnit.Custom: the custom format code decides
# Properties that hold conditional formatting (a rule, a scale, a field value, bars, icons):
# the only ones "clear everything" touches (``_clear_spots`` keeps no other), so a card's image
# measure or a reference label's value (bindings, not formatting) are never removed.
_CF_PROPERTIES: frozenset[tuple[str, str, str]] = frozenset(
    {
        (_OBJECTS, "values", "fontColor"),
        (_OBJECTS, "values", "backColor"),
        (_OBJECTS, "values", "icon"),
        (_OBJECTS, "values", "webURL"),
        (_OBJECTS, "columnFormatting", "dataBars"),
        (_OBJECTS, "dataPoint", "fill"),
        (_OBJECTS, "value", "fontColor"),
        (_OBJECTS, "labels", "color"),
        (_CONTAINER, "title", "fontColor"),
        (_CONTAINER, "background", "color"),
    }
)
_TARGET_WORDS = {
    "font_color": "font color",
    "background": "background",
    "data_colors": "data colors",
    "icon": "icons",
    "data_bars": "data bars",
    "web_url": "web address",
    "title_text": "title text",
    "title_color": "title color",
    "visual_background": "background",
}
# Query-level edits have no catalog property; what the canvas does with them: it labels columns
# by the projection's displayName and draws them in projection order (live_report), and does not
# read a visual-level format string yet. The Format pane's catalog reads it too.
QUERY_DRAWN: dict[str, bool] = {"displayName": True, "order": True, "format": False}


def conditional_family(visual_type: str) -> Literal["table", "chart", "card", "other"]:
    """Which conditional formatting a visual type takes: a table or matrix (per column), a
    chart (its data colors), a card (its value), or other (only title and background)."""
    return _family(visual_type)


# --- one operation on one visual --------------------------------------------------------------


class _Work:
    """The changes one operation makes to one visual, kept on a copy until it is complete, so a
    refusal halfway through leaves the visual as it was."""

    def __init__(self, document: PbirDocument, op: Any) -> None:
        self.document = document
        self.op_name = str(op.op)
        self.page, self.original = resolve_leaf(document, op.visual)
        self.name = str(self.original.get("name") or op.visual)
        self.work = copy.deepcopy(self.original)
        self.visual_type = _visual_type(self.work)
        # Named as the person knew it before this change: a moved first column or a title bound
        # to a measure would otherwise rename the visual in its own receipt.
        self._label = describe(self.original, self.page)
        self.written: dict[tuple[str, str, str], None] = {}
        self.replaced: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []

    def label(self) -> str:
        return self._label

    def headline(self) -> str:
        return sentence(self.label())

    def holder(self, where: str) -> dict[str, Any]:
        return _objects(self.work, where)

    def mark(self, where: str, object_name: str, prop: str) -> None:
        self.written[(where, object_name, prop)] = None

    def has(self, object_name: str, prop: str) -> bool:
        return expr.property_spec(self.visual_type, object_name, prop) is not None

    def spec(self, object_name: str, prop: str, words: str) -> tuple[str, dict[str, Any]]:
        found = expr.property_spec(self.visual_type, object_name, prop)
        if found is None:
            raise DocumentError(f"{self.headline()} has no {words} setting in Power BI")
        return found

    def encode(self, object_name: str, prop: str, value: Any, words: str) -> tuple[str, Any]:
        """(where, expression) for a plain value of a catalog property."""
        where, definition = self.spec(object_name, prop, words)
        existing = None
        for entry in self.holder(where).get(object_name, []):
            if prop in entry.get("properties", {}):
                existing = entry["properties"][prop]
                break
        return where, expr.encode_property(definition, prop, value, existing)

    def write_plain(
        self,
        object_name: str,
        prop: str,
        value: Any,
        words: str,
        *,
        replace: bool,
        selector: dict[str, Any] | None = None,
    ) -> None:
        """A plain value, for the whole visual (every entry that would override it too) or
        under ``selector``; conditional formatting in the way is only overwritten with
        ``replace``."""
        where, encoded = self.encode(object_name, prop, value, words)
        objects = self.holder(where)
        if selector is None:
            result = expr.write_everywhere(
                objects,
                object_name,
                prop,
                encoded,
                replace_conditional=replace,
                visual_type=self.visual_type if where == _OBJECTS else None,
            )
            if result.protected:
                kinds = sorted({entry.kind for entry in result.protected})
                raise NeedsConfirmation(
                    "replace_conditional",
                    f"{self.headline()} sets its {words} by a rule ({', '.join(kinds)}); "
                    "this change would overwrite it. Replace it?",
                )
            own = 0
            for entry in result.replaced:
                if isinstance(entry.previous, dict) and not (
                    "hex" in entry.previous or "theme" in entry.previous
                ):
                    self.replaced.append(f"the conditional {words}")
                elif entry.selector:
                    own += 1
            if own:
                # A field or category that set its own value now shows the visual's one.
                self.replaced.append(
                    f"the {words} {_plural(own, 'field or category')} had of "
                    f"{'its' if own == 1 else 'their'} own"
                )
        else:
            current = expr.get_property(objects, object_name, prop, selector)
            if current is not None and expr.protected_kind(current) is not None:
                if not replace:
                    raise NeedsConfirmation(
                        "replace_conditional",
                        f"{self.headline()} sets this {words} by {_cf_words(current)}; this "
                        "change would overwrite it. Replace it?",
                    )
                self.replaced.append(f"{_cf_words(current)} ({words})")
            expr.set_property(objects, object_name, prop, encoded, selector)
        self.mark(where, object_name, prop)

    def finish(self, summary: str) -> HandlerResult:
        body, before = self.work.get("visual", {}), self.original.get("visual", {})
        for where in (_OBJECTS, _CONTAINER):
            holder = body.get(where)
            if isinstance(holder, dict):
                for name in [n for n, entries in holder.items() if entries == []]:
                    if name not in (before.get(where) or {}):
                        del holder[name]
                if not holder and where not in before:
                    del body[where]
        if self.work == self.original:
            raise DocumentError(f"{self.headline()} already looks like that: nothing to change")
        self.original.clear()
        self.original.update(copy.deepcopy(self.work))
        self.document.touch_visual(self.page, self.name)
        text = " ".join([summary, *self.notes])
        if self.replaced:
            text += f" This replaced {'; '.join(dict.fromkeys(self.replaced))}."
        return HandlerResult(
            text,
            warnings=list(dict.fromkeys(self.warnings)),
            replaced=list(dict.fromkeys(self.replaced)),
            visible_in=[self._visible(where, obj, prop) for where, obj, prop in self.written],
        )

    def _visible(self, where: str, object_name: str, prop: str) -> VisibleIn:
        if where == "query":
            drawn = QUERY_DRAWN.get(prop, False)
        else:
            drawn = property_is_rendered(
                self.visual_type, object_name, prop, container=where == _CONTAINER
            )
        return VisibleIn(
            self.op_name, "studio" if drawn else "powerbi_only", f"{object_name}.{prop}"
        )


# --- reading a visual -------------------------------------------------------------------------


def _family(visual_type: str) -> Literal["table", "chart", "card", "other"]:
    if visual_type in _TABLE_TYPES:
        return "table"
    if visual_type in _CARD_VALUE_COLOR:
        return "card"
    if expr.property_spec(visual_type, "dataPoint", "fillRule") is not None:
        # The types whose capabilities have a color-by-value rule on their data colors.
        return "chart"
    return "other"


def _roles(visual: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    state = visual.get("visual", {}).get("query", {}).get("queryState", {})
    found: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(state, dict):
        return found
    for role, holder in state.items():
        projections = holder.get("projections") if isinstance(holder, dict) else None
        if isinstance(projections, list):
            found[str(role)] = [p for p in projections if isinstance(p, dict)]
    return found


def _field_node(projection: Mapping[str, Any]) -> dict[str, Any]:
    node = projection.get("field")
    return node if isinstance(node, dict) else {}


def _is_grouping(projection: Mapping[str, Any]) -> bool:
    node = _field_node(projection)
    return "Column" in node or "HierarchyLevel" in node or "Hierarchy" in node


def _has_grouping(visual: dict[str, Any]) -> bool:
    """Whether the visual splits its values by a field. Without one (a card, a table of
    measures only), Desktop has no lowest or highest value to offer: stops and thresholds are
    numbers."""
    return any(_is_grouping(p) for p in _projections(visual))


def _names(projection: Mapping[str, Any]) -> set[str]:
    """Every way a person may name a column: its header, its field, its query name."""
    names = {projection_label(dict(projection))}
    for key in ("displayName", "nativeQueryRef", "queryRef"):
        value = projection.get(key)
        if isinstance(value, str):
            names.add(value)
            if key == "queryRef" and "." in value:
                names.add(value.split(".", 1)[1].rstrip(")"))
    node = _field_node(projection)
    for kind in ("Measure", "Column"):
        inner = node.get(kind)
        if isinstance(inner, dict) and isinstance(inner.get("Property"), str):
            names.add(inner["Property"])
    aggregation = node.get("Aggregation")
    if isinstance(aggregation, dict):
        column = (aggregation.get("Expression") or {}).get("Column")
        if isinstance(column, dict) and isinstance(column.get("Property"), str):
            names.add(column["Property"])
    return {" ".join(n.split()).casefold() for n in names if isinstance(n, str) and n.strip()}


def _column_list(visual: dict[str, Any]) -> str:
    labels = [f"'{projection_label(p).strip()}'" for p in _projections(visual)]
    return ", ".join(labels) or "none"


def _find_field(
    work: _Work, name: str, roles: Iterable[str] | None = None
) -> tuple[str, int, dict[str, Any]]:
    """(role, index, projection) of the field ``name`` names, looked up by its header, its
    field or its query name. The header wins over a field name when both match different
    columns."""
    wanted = " ".join(name.split()).casefold()
    allowed = set(roles) if roles is not None else None
    matches: list[tuple[str, int, dict[str, Any]]] = []
    for role, projections in _roles(work.work).items():
        if allowed is not None and role not in allowed:
            continue
        for index, projection in enumerate(projections):
            if wanted in _names(projection):
                matches.append((role, index, projection))
    if len(matches) > 1:
        shown = [m for m in matches if projection_label(m[2]).strip().casefold() == wanted]
        matches = shown if len(shown) == 1 else matches
    if len(matches) > 1:
        labels = ", ".join(sorted({f"'{projection_label(m[2]).strip()}'" for m in matches}))
        raise DocumentError(f"More than one field of {work.label()} matches {name!r}: {labels}")
    if not matches:
        elsewhere = [
            (role, p)
            for role, projections in _roles(work.work).items()
            for p in projections
            if allowed is not None and role not in allowed and wanted in _names(p)
        ]
        if elsewhere:
            # A matrix's row or column field: its cells are not formatted, its values are.
            values = [
                f"'{projection_label(p).strip()}'"
                for role, projections in _roles(work.work).items()
                if role in (allowed or set())
                for p in projections
            ]
            raise DocumentError(
                f"{projection_label(elsewhere[0][1]).strip()} is a {elsewhere[0][0].lower()} field "
                f"of {work.label()}; this formats its values: {', '.join(values) or 'none'}"
            )
        raise DocumentError(
            f"{work.headline()} has no column or field called {name!r}. "
            f"Its fields: {_column_list(work.work)}"
        )
    return matches[0]


def _query_ref(projection: Mapping[str, Any]) -> str:
    value = projection.get("queryRef")
    if not isinstance(value, str) or not value:
        raise DocumentError("That field has no name in the report, so it cannot be formatted")
    return value


def _value_projections(visual: dict[str, Any]) -> list[dict[str, Any]]:
    """The fields whose numbers a visual shows: measures and summarized columns, in role
    order (a chart's Y, a card's Data, a table's value columns)."""
    return [p for p in _projections(visual) if not _is_grouping(p)]


def _ask_for_column(work: _Work, question: str, *, numeric_only: bool = False) -> NeedsChoice:
    options = [
        Choice(projection_label(p).strip() or _query_ref(p), {"field": projection_label(p).strip()})
        for p in (_value_projections(work.work) if numeric_only else _projections(work.work))
    ]
    return NeedsChoice(question, options[:8])


# --- the model's types ------------------------------------------------------------------------


def _pandas_type(raw: str) -> str | None:
    lowered = raw.casefold()
    for prefix, name in (
        ("int", "integer"),
        ("uint", "integer"),
        ("float", "double"),
        ("datetime", "datetime"),
        ("bool", "boolean"),
        ("decimal", "decimal"),
        ("object", "text"),
        ("string", "text"),
        ("str", "text"),
        ("category", "text"),
    ):
        if lowered.startswith(prefix):
            return name
    return None


def _type_of(model: ModelInfo, table: str, name: str) -> str | None:
    """The field's data type name (integer, double, decimal, text, datetime, boolean), or None
    when the model does not say (a measure usually)."""
    raw = model.data_type(table, name)
    named = expr.data_type_name(raw)
    if named is None and isinstance(raw, str):
        named = _pandas_type(raw)
    return "datetime" if named == "date" else named


_TYPE_WORDS = {
    "text": "text",
    "datetime": "dates",
    "boolean": "true/false values",
    "integer": "whole numbers",
    "double": "numbers",
    "decimal": "fixed decimal numbers",
}


@dataclass(frozen=True)
class _Basis:
    """The expression a rule or scale reads its values from, with its data type."""

    node: dict[str, Any]
    type_name: str | None  # None: not typed by the model (a measure); treated as a number
    words: str

    @property
    def numeric(self) -> bool:
        return self.type_name is None or self.type_name in _NUMERIC


@dataclass(frozen=True)
class _Ref:
    table: str | None
    name: str


def _parse_ref(value: FieldRef | str) -> tuple[_Ref, str | None]:
    """A field reference from an operation: a FieldRef, 'Table[Name]' or a bare name."""
    if isinstance(value, FieldRef):
        return _Ref(value.table, value.name), value.aggregation
    text = value.strip()
    found = re.fullmatch(r"'?(?P<table>[^'\[\]]+?)'?\s*\[(?P<name>[^\[\]]+)\]", text)
    if found:
        return _Ref(found.group("table").strip(), found.group("name").strip()), None
    bracket = re.fullmatch(r"\[(?P<name>[^\[\]]+)\]", text)
    return _Ref(None, bracket.group("name").strip() if bracket else text), None


def _aggregate_type(code: Any, type_name: str | None) -> str | None:
    """The data type an aggregation of a column gives: counts are whole numbers; an average,
    median, standard deviation or variance is a number; a sum, minimum or maximum keeps the
    column's type."""
    if code in (2, 5):
        return "integer"
    if code in (1, 6, 7, 8):
        return "double"
    return type_name


def _aggregate(table: str, column: str, function: str, type_name: str | None, words: str) -> _Basis:
    code = _FUNCTIONS[function]
    node = {"Aggregation": {"Expression": expr.column_ref(table, column), "Function": code}}
    return _Basis(node, _aggregate_type(code, type_name), words)


def _node_basis(model: ModelInfo, node: dict[str, Any], words: str) -> _Basis:
    """The basis an expression already in the report reads (a measure, or an aggregation of a
    column copied as it is, whatever its function), typed from the model where it can be."""
    measure = node.get("Measure")
    if isinstance(measure, dict):
        table = str((measure.get("Expression") or {}).get("SourceRef", {}).get("Entity", ""))
        return _Basis(
            copy.deepcopy(node), _type_of(model, table, str(measure.get("Property"))), words
        )
    aggregation = node.get("Aggregation")
    if isinstance(aggregation, dict):
        column = (aggregation.get("Expression") or {}).get("Column") or {}
        table = str((column.get("Expression") or {}).get("SourceRef", {}).get("Entity", ""))
        type_name = _type_of(model, table, str(column.get("Property", ""))) if column else None
        return _Basis(
            copy.deepcopy(node), _aggregate_type(aggregation.get("Function"), type_name), words
        )
    return _Basis(copy.deepcopy(node), None, words)


def _default_function(model: ModelInfo, table: str, column: str, type_name: str | None) -> str:
    summarize = (model.summarize_by(table, column) or "").replace("_", "").casefold()
    mapped = _SUMMARIZE_BY.get(summarize)
    if mapped is not None:
        return mapped
    return "sum" if type_name is None or type_name in _NUMERIC else "first"


def _model_basis(model: ModelInfo, value: FieldRef | str, summarization: str | None) -> _Basis:
    ref, aggregation = _parse_ref(value)
    kind, table, name = model.resolve(ref)
    if kind == "measure":
        if summarization is not None:
            raise DocumentError(f"[{name}] is a measure: it has no summarization to choose")
        return _Basis(expr.measure_ref(table, name), _type_of(model, table, name), f"[{name}]")
    type_name = _type_of(model, table, name)
    names = {"Sum": "sum", "Average": "average", "Min": "min", "Max": "max"}
    names |= {"Count": "count_distinct", "CountNonNull": "count"}
    function = summarization or (names[aggregation] if aggregation else None)
    function = function or _default_function(model, table, name, type_name)
    return _aggregate(table, name, function, type_name, f"{table}[{name}]")


def _projection_basis(
    model: ModelInfo, projection: dict[str, Any], summarization: str | None
) -> _Basis:
    node = copy.deepcopy(_field_node(projection))
    words = projection_label(projection).strip()
    # A measure, or a column already summarized in the visual (a standard deviation stays one).
    if isinstance(node.get("Measure"), dict) or isinstance(node.get("Aggregation"), dict):
        return _node_basis(model, node, words)
    column = node.get("Column")
    if isinstance(column, dict):
        table = str((column.get("Expression") or {}).get("SourceRef", {}).get("Entity", ""))
        name = str(column.get("Property", ""))
        type_name = _type_of(model, table, name)
        function = summarization or _default_function(model, table, name, type_name)
        return _aggregate(table, name, function, type_name, words)
    raise DocumentError(
        f"Power BI cannot format by the values of {words!r}; name a measure or column with based_on"
    )


# --- colors and literals ----------------------------------------------------------------------


def _hex(document: PbirDocument, value: ColorValue) -> str:
    """A color as the hex Desktop writes into rules and scales (its dialog stores the picked
    color's hex, never a theme reference)."""
    if isinstance(value, ThemeColorRef):
        try:
            return expr.resolve_theme_color(document.theme(), value.theme, value.percent)
        except expr.ExpressionError as error:
            raise DocumentError(
                f"Theme color {value.theme} does not exist: Power BI numbers them 0 to 9. Use a "
                "hex color like #1E90FF instead."
            ) from error
    return expr.hex_color(value)


def _plain_color(value: ColorValue) -> str | dict[str, Any]:
    if isinstance(value, ThemeColorRef):
        if value.theme not in expr.VERIFIED_THEME_COLOR_IDS:
            raise DocumentError(
                f"Theme color {value.theme} does not exist: Power BI numbers them 0 to 9"
            )
        return {"theme": value.theme, "percent": value.percent}
    return value


def _literal(text: str) -> dict[str, Any]:
    return {"Literal": {"Value": text}}


def _text(value: str) -> dict[str, Any]:
    return _literal(expr.quote(value))


def _double(value: float) -> dict[str, Any]:
    number = float(value)
    if not math.isfinite(number) or abs(number) > 1e15:
        raise DocumentError(f"{value!r} is out of range for Power BI")
    text = str(int(number)) if number.is_integer() else repr(number)
    return _literal(f"{text}D")


def _typed_bar_value(value: float, type_name: str | None) -> dict[str, Any]:
    """A data bar's minimum or maximum, typed like Desktop types it from the field: a whole
    number rounds half up (as JavaScript's Math.round does), a fixed decimal keeps M."""
    number = float(value)
    if not math.isfinite(number) or abs(number) > 1e15:
        raise DocumentError(f"{value!r} is out of range for data bars")
    if type_name == "integer":
        found = expr.integer(math.floor(number + 0.5))["expr"]
        assert isinstance(found, dict)
        return found
    if type_name == "decimal":
        return _literal(expr.data_literal(number, "decimal"))
    return _double(number)


def _theme_color(document: PbirDocument, key: str, fallback: str) -> str:
    """One of the theme's gradient colors (minimum, center, maximum, foreground...), which
    Desktop's dialog starts from."""
    value = document.theme().get(key)
    try:
        return expr.hex_color(value) if isinstance(value, str) else fallback
    except expr.ExpressionError:
        return fallback


# --- what an existing value is ----------------------------------------------------------------


def _expression_of(value: Any) -> dict[str, Any] | None:
    """The query expression inside a property value: a fill's color, an icon's value, a bare
    ``{expr}``."""
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("solid"), dict):
        value = value["solid"].get("color")
    elif value.get("kind") == "Icon":
        value = value.get("value")
    inner = value.get("expr") if isinstance(value, dict) else None
    return inner if isinstance(inner, dict) else None


def _cf_words(value: Any) -> str:
    """What a conditional value is, for a person: "a color scale", "rules", "the measure X"."""
    if isinstance(value, dict) and "positiveColor" in value:
        return "data bars"
    icon = isinstance(value, dict) and value.get("kind") == "Icon"
    inner = _expression_of(value)
    if inner is None:
        return "icons" if icon else "conditional formatting"
    if "FillRule" in inner:
        return "a color scale"
    if "Conditional" in inner:
        return "icon rules" if icon else "rules"
    measure = inner.get("Measure")
    if isinstance(measure, dict):
        return f"the measure [{measure.get('Property')}]"
    if "Aggregation" in inner or "Column" in inner:
        return "a field's values"
    return "icons" if icon else "conditional formatting"


def _removed_words(spot: _Spot, value: Any) -> str:
    """What a clear took away, as a person reads it: "the data bars of the X column", "the
    conditional font color of the Rank column (the measure [CF Table])"."""
    if spot.prop == "dataBars":
        return spot.words
    return f"the conditional {spot.words.removeprefix('the ')} ({_cf_words(value)})"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _n(value: float) -> str:
    """A number for a sentence as it was given: 250000.5, 5, 0.33 (never 2.5e+05)."""
    number = float(value)
    return str(int(number)) if number.is_integer() else format(number, ".15g")


def _style_of(value: Any) -> str | None:
    if isinstance(value, dict) and "positiveColor" in value:
        return "data_bars"
    inner = _expression_of(value)
    if inner is None:
        return None
    if "FillRule" in inner:
        return "gradient"
    if "Conditional" in inner:
        return "rules"
    return "field_value"


# --- where a conditional format goes ----------------------------------------------------------


@dataclass
class _Spot:
    """One place conditional formatting is written: the object and property, the selector of
    the entry, the column it formats (None for the whole visual) and its words."""

    where: str
    object_name: str
    prop: str
    selector: dict[str, Any] | None
    query_ref: str | None
    words: str
    projection: dict[str, Any] | None = None


def _wildcard(option: int, query_ref: str | None) -> dict[str, Any]:
    selector: dict[str, Any] = {"data": [{"dataViewWildcard": {"matchingOption": option}}]}
    if query_ref is not None:
        selector["metadata"] = query_ref
    return selector


_TABLE_TARGETS: dict[str, tuple[str, str]] = {
    "font_color": ("values", "fontColor"),
    "background": ("values", "backColor"),
    "icon": ("values", "icon"),
    "web_url": ("values", "webURL"),
    "data_bars": ("columnFormatting", "dataBars"),
}
_CONTAINER_TARGETS: dict[str, tuple[str, str]] = {
    "title_text": ("title", "text"),
    "title_color": ("title", "fontColor"),
    "visual_background": ("background", "color"),
}


# Which styles each target takes (Desktop's fx dialog for that property offers these).
_TARGET_STYLES: dict[str, frozenset[str]] = {
    "font_color": frozenset({"gradient", "rules", "field_value"}),
    "background": frozenset({"gradient", "rules", "field_value"}),
    "data_colors": frozenset({"gradient", "rules", "field_value"}),
    "icon": frozenset({"icons", "rules", "field_value"}),
    "data_bars": frozenset({"data_bars"}),
    "web_url": frozenset({"field_value"}),
    "title_text": frozenset({"field_value"}),
    "title_color": frozenset({"field_value"}),
    "visual_background": frozenset({"field_value"}),
}


def _target_for(work: _Work, op: SetConditionalFormat) -> str:
    """The target the operation formats, from its style when it leaves it out."""
    family = _family(work.visual_type)
    if op.target is not None:
        allowed = _TARGET_STYLES[op.target]
        if op.style not in allowed:
            styles = " or ".join(sorted(s.replace("_", " ") for s in allowed))
            raise DocumentError(
                f"Power BI sets the {_TARGET_WORDS[op.target]} by {styles}, not by "
                f"{op.style.replace('_', ' ')}"
            )
        return op.target
    if op.style == "data_bars":
        return "data_bars"
    if op.style == "icons":
        return "icon"
    if family == "chart":
        return "data_colors"
    if family == "card":
        return "font_color"
    if family == "table":
        raise NeedsChoice(
            f"Color the text or the cell background of {work.label()}?",
            [
                Choice("The text (font color)", {"target": "font_color"}),
                Choice("The cell background", {"target": "background"}),
            ],
        )
    raise DocumentError(
        f"{work.headline()} has no data colors, columns or value Power BI formats by rules; "
        "only its title and background can follow a field (target title_text, title_color or "
        "visual_background with style field_value)"
    )


def _cf_spot(
    work: _Work, target: str, field: str | None, apply_to: str, *, clearing: bool = False
) -> _Spot:
    family = _family(work.visual_type)
    if target in _CONTAINER_TARGETS:
        if field is not None:
            raise DocumentError(
                f"The {_TARGET_WORDS[target]} belongs to the whole visual: leave out the field"
            )
        object_name, prop = _CONTAINER_TARGETS[target]
        found = expr.property_spec(work.visual_type, object_name, prop)
        if found is None or found[0] != _CONTAINER:
            raise DocumentError(f"{work.headline()} has no {_TARGET_WORDS[target]} to format")
        return _Spot(_CONTAINER, object_name, prop, None, None, f"the {_TARGET_WORDS[target]}")
    if family == "table":
        if target not in _TABLE_TARGETS:
            raise DocumentError(
                f"A table or matrix formats its columns' font color, background, icons, data "
                f"bars or web addresses, not {_TARGET_WORDS.get(target, target)}"
            )
        if field is None:
            raise _ask_for_column(
                work,
                f"Which column of {work.label()} should get the {_TARGET_WORDS[target]}?",
                numeric_only=target == "data_bars",
            )
        # A matrix formats the cells of its values, not its row or column headers.
        roles = ("Values",) if work.visual_type == "pivotTable" else None
        _, _, projection = _find_field(work, field, roles)
        query_ref = _query_ref(projection)
        object_name, prop = _TABLE_TARGETS[target]
        column = projection_label(projection).strip()
        if target == "data_bars":
            if apply_to not in _MATCHING:
                raise DocumentError(f"Unknown apply_to {apply_to!r}")
            selector: dict[str, Any] = {"metadata": query_ref}
        else:
            selector = _wildcard(_MATCHING[apply_to], query_ref)
        words = f"the {_TARGET_WORDS[target]} of the {column} column"
        return _Spot(_OBJECTS, object_name, prop, selector, query_ref, words, projection)
    if apply_to != "values":
        raise DocumentError("Only a table or matrix has totals to format separately")
    if family == "chart":
        if target != "data_colors":
            raise DocumentError(
                f"{work.headline()} takes conditional data colors (target data_colors), and a "
                f"title or visual background from a field, not {_TARGET_WORDS[target]}"
            )
        legend = [p for role in ("Series", "Legend") for p in _roles(work.work).get(role, [])]
        if legend and not clearing:
            raise DocumentError(
                f"{work.headline()} has a legend field, and Power BI colors bars by value only "
                "on a chart without one"
            )
        series: dict[str, Any] | None = None
        series_ref: str | None = None
        words = "the data colors"
        if field is not None:
            _, _, series = _find_field(work, field)
            if _is_grouping(series):
                raise DocumentError(
                    f"{projection_label(series).strip()} is what {work.label()} is split by; "
                    "name one of the values it shows"
                )
            series_ref = _query_ref(series)
            words = f"the color of {projection_label(series).strip()}"
        return _Spot(
            _OBJECTS, "dataPoint", "fill", _wildcard(1, series_ref), series_ref, words, series
        )
    if family == "card":
        if target != "font_color":
            raise DocumentError(
                f"{work.headline()} takes a conditional value color (target font_color), and a "
                f"title or visual background from a field, not {_TARGET_WORDS[target]}"
            )
        object_name, prop = _CARD_VALUE_COLOR[work.visual_type]
        values = _value_projections(work.work)
        if field is not None:
            _, _, projection = _find_field(work, field)
        elif values:
            projection = values[0]
        else:
            raise DocumentError(f"{work.headline()} shows no value to format")
        query_ref = _query_ref(projection)
        # A new card keeps each field's style under its own metadata; the old card has one value.
        scoped = query_ref if work.visual_type == "cardVisual" else None
        return _Spot(
            _OBJECTS,
            object_name,
            prop,
            _wildcard(1, scoped),
            scoped,
            "the value's font color",
            projection,
        )
    raise DocumentError(
        f"{work.headline()} has no data colors, columns or value Power BI formats by rules"
    )


def _existing(work: _Work, spot: _Spot) -> list[dict[str, Any]]:
    """The entries that hold conditional formatting at the spot: the column's (any selector
    naming its query name), or the whole visual's."""
    objects = work.work.get("visual", {}).get(spot.where) or {}
    found: list[dict[str, Any]] = []
    for entry in objects.get(spot.object_name, []) if isinstance(objects, dict) else []:
        if not isinstance(entry, dict):
            continue
        value = (entry.get("properties") or {}).get(spot.prop)
        if value is None or expr.protected_kind(value) is None:
            continue
        selector = entry.get("selector") or {}
        if spot.where == _CONTAINER:
            if selector:
                continue
        elif spot.query_ref is not None:
            if selector.get("metadata") != spot.query_ref:
                continue
        elif selector.get("metadata") or any(
            isinstance(item, dict) and "scopeId" in item for item in selector.get("data") or []
        ):
            continue
        found.append(entry)
    return found


def _remove(work: _Work, spot: _Spot, entries: Sequence[dict[str, Any]]) -> None:
    if not entries:
        return
    objects = work.holder(spot.where)
    kept = []
    for entry in objects.get(spot.object_name, []):
        if any(entry is e for e in entries):
            entry.get("properties", {}).pop(spot.prop, None)
            if not entry.get("properties"):
                continue
        kept.append(entry)
    if kept:
        objects[spot.object_name] = kept
    else:
        objects.pop(spot.object_name, None)  # no empty list left for Desktop to read
    work.mark(spot.where, spot.object_name, spot.prop)


def _place(work: _Work, spot: _Spot, existing: Sequence[dict[str, Any]], value: Any) -> None:
    """Write the value at the spot. An entry that already holds formatting under the same
    selector keeps its place in the list (a diff against Desktop's file stays small); others
    with formatting for the spot lose it, so only one decides."""
    same = [e for e in existing if expr.same_selector(e.get("selector"), spot.selector)]
    _remove(work, spot, [e for e in existing if not any(e is s for s in same)])
    if same:
        same[0].setdefault("properties", {})[spot.prop] = copy.deepcopy(value)
    else:
        expr.set_property(
            work.holder(spot.where), spot.object_name, spot.prop, value, spot.selector
        )
    work.mark(spot.where, spot.object_name, spot.prop)


# --- building the expressions -----------------------------------------------------------------


def _range_percent(source: dict[str, Any], fraction: float) -> dict[str, Any]:
    """Desktop's percent threshold: lowest + fraction * (highest - lowest) of the source over
    everything the visual shows (its ``SQRangePercentExpr.overRangeOf``)."""

    def over_everything(function: int) -> dict[str, Any]:
        per_row = {
            "ScopedEval": {"Expression": copy.deepcopy(source), "Scope": [{"AllRolesRef": {}}]}
        }
        return {
            "ScopedEval": {
                "Expression": {"Aggregation": {"Expression": per_row, "Function": function}},
                "Scope": [],
            }
        }

    return {
        "RangePercent": {"Min": over_everything(3), "Max": over_everything(4), "Percent": fraction}
    }


def _comparison(kind: int, left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {"Comparison": {"ComparisonKind": kind, "Left": copy.deepcopy(left), "Right": right}}


# Desktop's text rules tag each comparison (its conditional-formatting dialog, TextConditionView
# Mode.getDefinition): the evaluation is ordinal (1) and the dialog's operator id says which
# choice made it; "is blank" is only told apart from "is" by that id.
_TEXT_ANNOTATIONS = ("PowerBI.SQExprEvaluationKind", "PowerBI.SQExprTextOperatorOption")
_TEXT_ORDINAL = 1
_TEXT_EQUAL, _TEXT_IS_BLANK = 2, 5


def _text_comparison(
    source: dict[str, Any], right: dict[str, Any], operator: int
) -> dict[str, Any]:
    found = _comparison(_EQUAL, source, right)
    found["Annotations"] = {_TEXT_ANNOTATIONS[0]: _TEXT_ORDINAL, _TEXT_ANNOTATIONS[1]: operator}
    return found


def _is_text(basis: _Basis, rules: Sequence[Rule]) -> bool:
    """Whether Desktop would open these rules in its text dialog: the field holds text, or a
    measure the model does not type is compared with text."""
    if basis.type_name == "text":
        return True
    return basis.type_name is None and any(isinstance(r.equals, str) for r in rules)


def _condition(
    rule: Rule, basis: _Basis, grouped: bool, words: str, text: bool = False
) -> dict[str, Any]:
    source = basis.node
    if basis.type_name is not None and not basis.numeric and not text:
        raise DocumentError(
            f"{words} holds {_TYPE_WORDS.get(basis.type_name, 'values')}: rules follow numbers "
            "or text"
        )
    if rule.blank:
        if rule.min is not None or rule.max is not None or rule.equals is not None:
            raise DocumentError("A rule for blank values takes no numbers")
        if text:
            return _text_comparison(source, _literal("null"), _TEXT_IS_BLANK)
        return _comparison(_EQUAL, source, _literal("null"))
    if text:
        if rule.equals is None and rule.min is None and rule.max is None:
            return _literal("true")  # every other value, as Desktop's text rules write it
        if not isinstance(rule.equals, str) or rule.min is not None or rule.max is not None:
            raise DocumentError(
                f"{words} holds text: a rule on it can only match one value (equals, as text) "
                "or blank values"
            )
        return _text_comparison(source, _text(rule.equals), _TEXT_EQUAL)
    if rule.equals is not None:
        if rule.min is not None or rule.max is not None:
            raise DocumentError("Give a rule either a value to equal or a range, not both")
        if isinstance(rule.equals, str):
            raise DocumentError(f"{rule.equals!r} is text, but {words} holds numbers")
        return _comparison(_EQUAL, source, _double(rule.equals))
    percent = rule.unit == "percent"
    if percent and not grouped:
        raise DocumentError(
            "This visual shows a single value, so there is no range for a percent: give the "
            "rule numbers"
        )
    for bound in (rule.min, rule.max):
        if percent and bound is not None and not 0 <= bound <= 100:
            raise DocumentError(f"A percent runs from 0 to 100, not {_n(bound)}")
    # Desktop's dialog: a percent rule starts below 100% and ends above 0%.
    if percent and rule.min is not None and rule.min >= 100:
        raise DocumentError("A percent rule has to start below 100")
    if percent and rule.max is not None and rule.max <= 0:
        raise DocumentError("A percent rule has to end above 0")
    if rule.min is not None and rule.max is not None and rule.min >= rule.max:
        raise DocumentError(
            f"A rule from {_n(rule.min)} to {_n(rule.max)} matches nothing: the lower bound has to "
            "be below the upper one"
        )

    def right(value: float) -> dict[str, Any]:
        return _range_percent(source, value / 100) if percent else _double(value)

    lower = upper = None
    # 0% and 100% are the ends of the range: Desktop leaves those bounds out.
    if rule.min is not None and not (percent and rule.min == 0):
        lower = _comparison(
            _GREATER_EQUAL if rule.min_inclusive else _GREATER, source, right(rule.min)
        )
    if rule.max is not None and not (percent and rule.max == 100):
        upper = _comparison(_LESS_EQUAL if rule.max_inclusive else _LESS, source, right(rule.max))
    if lower and upper:
        return {"And": {"Left": lower, "Right": upper}}
    return lower or upper or _literal("true")


def _icon_name(name: str) -> str:
    found = _ICON_LOOKUP.get(name.strip().casefold())
    if found is None:
        known = ", ".join(sorted(ICON_NAMES)[:12])
        raise DocumentError(f"Power BI has no icon called {name!r}. Icons include: {known} …")
    return found


def _rules(
    document: PbirDocument,
    rules: Sequence[Rule],
    basis: _Basis,
    grouped: bool,
    *,
    icons: bool,
) -> dict[str, Any]:
    cases = []
    text = _is_text(basis, rules)
    for number, rule in enumerate(rules, start=1):
        try:
            condition = _condition(rule, basis, grouped, basis.words, text)
        except DocumentError as error:
            raise DocumentError(f"Rule {number}: {error}") from error
        if icons:
            if rule.icon is None or rule.color is not None:
                raise DocumentError(f"Rule {number}: an icon rule names an icon, not a color")
            value = _text(_icon_name(rule.icon))
        else:
            if rule.color is None or rule.icon is not None:
                raise DocumentError(f"Rule {number}: a color rule names a color, not an icon")
            value = _text(_hex(document, rule.color))
        cases.append({"Condition": condition, "Value": value})
    return {"Conditional": {"Cases": cases}}


def _icon_set_rules(icon_set: str, basis: _Basis) -> dict[str, Any]:
    """Desktop's own rules for an icon set: the range split evenly by percent, lowest icon for
    the lowest part; written highest first, the way its dialog saves them."""
    icons = ICON_SETS[icon_set]
    count = len(icons)
    cases = []
    for index, icon in enumerate(icons):
        low = math.floor(index / count * 100 + 0.5)
        high = math.floor((index + 1) / count * 100 + 0.5)
        rule = Rule(icon=icon, min=low, max=high, max_inclusive=index + 1 == count, unit="percent")
        cases.append(
            {"Condition": _condition(rule, basis, True, basis.words), "Value": _text(icon)}
        )
    return {"Conditional": {"Cases": list(reversed(cases))}}


def _stop(color: str, value: float | None) -> dict[str, Any]:
    stop: dict[str, Any] = {"color": _text(color)}
    if value is not None:
        stop["value"] = _double(value)
    return stop


def _plain_words(work: _Work, decoded: Any) -> str:
    """A plain value a field value replaced, as it reads: a hex, a theme color by the hex it
    paints (or its number), text in quotes."""
    if isinstance(decoded, dict) and "hex" in decoded:
        return str(decoded["hex"])
    if isinstance(decoded, dict) and "theme" in decoded:
        themed = {"ColorId": decoded["theme"], "Percent": decoded.get("percent", 0)}
        painted = expr.color_hex(
            {"solid": {"color": {"expr": {"ThemeDataColor": themed}}}}, work.document.palette()
        )
        return painted or f"theme color {decoded['theme']}"
    if isinstance(decoded, str):
        return f"'{decoded}'"
    return str(decoded)


def _node_words(node: dict[str, Any], spot: _Spot) -> str:
    """A basis expression in words: the column's own name when it is the column's field."""
    if spot.projection is not None and _field_node(spot.projection) == node:
        return projection_label(spot.projection).strip()
    measure = node.get("Measure")
    if isinstance(measure, dict):
        return f"[{measure.get('Property')}]"
    column = ((node.get("Aggregation") or {}).get("Expression") or {}).get("Column")
    if isinstance(column, dict):
        table = (column.get("Expression") or {}).get("SourceRef", {}).get("Entity", "")
        return f"{table}[{column.get('Property')}]"
    return "its field"


def _old_gradient(existing: Sequence[dict[str, Any]], spot: _Spot) -> dict[str, Any] | None:
    for entry in existing:
        inner = _expression_of(entry["properties"][spot.prop])
        found = inner.get("FillRule") if inner is not None else None
        if isinstance(found, dict):
            return found
    return None


def _stop_parts(stop: Any) -> tuple[str | None, float | None]:
    if not isinstance(stop, dict):
        return None, None
    color = (
        expr.decode({"expr": stop.get("color")}) if isinstance(stop.get("color"), dict) else None
    )
    value = (
        expr.decode({"expr": stop.get("value")}) if isinstance(stop.get("value"), dict) else None
    )
    number = (
        float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None
    )
    return (color if isinstance(color, str) else None), number


def _gradient(
    work: _Work,
    op: SetConditionalFormat,
    basis: _Basis,
    grouped: bool,
    old: dict[str, Any] | None,
) -> tuple[dict[str, Any], str]:
    """The FillRule, and its words. Parts the operation leaves out keep what an existing scale
    on the same spot has, else Desktop's defaults (the theme's minimum, center and maximum
    colors, lowest/middle/highest value, blanks as zero)."""
    if not basis.numeric:
        raise DocumentError(
            f"A color scale follows numbers, and {basis.words} holds "
            f"{_TYPE_WORDS.get(basis.type_name or '', 'text')}"
        )
    document = work.document
    previous = (old or {}).get("FillRule") or {}
    old_stops = previous.get("linearGradient3") or previous.get("linearGradient2") or {}
    three = op.mid_color is not None or op.mid_value is not None or "linearGradient3" in previous

    def stop(
        name: str, color: ColorValue | None, value: float | None, theme_key: str, fallback: str
    ) -> tuple[str, float | None]:
        old_color, old_value = _stop_parts(old_stops.get(name))
        chosen = _hex(document, color) if color is not None else old_color
        return (
            chosen or _theme_color(document, theme_key, fallback),
            value if value is not None else old_value,
        )

    low = stop("min", op.min_color, op.min_value, "minimum", "#DEEFFF")
    high = stop("max", op.max_color, op.max_value, "maximum", "#118DFF")
    mid = stop("mid", op.mid_color, op.mid_value, "center", "#D9B300") if three else None
    if not grouped and (low[1] is None or high[1] is None or (mid is not None and mid[1] is None)):
        raise DocumentError(
            "This visual shows a single value, so there is no lowest or highest value: give "
            "min_value and max_value" + (" and mid_value" if mid is not None else "")
        )
    fixed = [v for v in (low[1], mid[1] if mid else None, high[1]) if v is not None]
    if fixed != sorted(fixed) or len(set(fixed)) != len(fixed):
        raise DocumentError("The color scale's numbers have to rise from minimum to maximum")
    stops: dict[str, Any] = {"min": _stop(*low)}
    if mid is not None:
        stops["mid"] = _stop(*mid)
    stops["max"] = _stop(*high)
    old_null = old_stops.get("nullColoringStrategy") if isinstance(old_stops, dict) else None
    strategy = _BLANK_STRATEGY.get(op.blank or "")
    null: dict[str, Any]
    if strategy is None and isinstance(old_null, dict) and op.blank_color is None:
        null = copy.deepcopy(old_null)
    else:
        if strategy is None:
            strategy = "specificColor" if op.blank_color is not None else "asZero"
        null = {"strategy": _text(strategy)}
        if strategy == "specificColor":
            color = op.blank_color
            if color is None:
                old_blank = old_null.get("color") if isinstance(old_null, dict) else None
                decoded = expr.decode({"expr": old_blank}) if isinstance(old_blank, dict) else None
                null["color"] = _text(
                    decoded
                    if isinstance(decoded, str)
                    else _theme_color(document, "null", "#FF7F48")
                )
            else:
                null["color"] = _text(_hex(document, color))
        elif op.blank_color is not None:
            raise DocumentError("A blank color is only used with blank set to specific_color")
    stops["nullColoringStrategy"] = null
    kind = "linearGradient3" if mid is not None else "linearGradient2"
    rule = {"FillRule": {"Input": copy.deepcopy(basis.node), "FillRule": {kind: stops}}}

    def end(color: str, value: float | None, word: str) -> str:
        return f"{color} ({word if value is None else f'at {_n(value)}'})"

    parts = [end(low[0], low[1], "lowest")]
    if mid is not None:
        parts.append(end(mid[0], mid[1], "middle"))
    parts.append(end(high[0], high[1], "highest"))
    return rule, f"a color scale on {basis.words}, " + " to ".join(parts)


def _data_bars(
    work: _Work, op: SetConditionalFormat, basis: _Basis, old: dict[str, Any] | None
) -> dict[str, Any]:
    if not basis.numeric:
        raise DocumentError(
            f"Data bars show numbers, and {basis.words} holds "
            f"{_TYPE_WORDS.get(basis.type_name or '', 'text')}"
        )
    if op.min_value is not None and op.max_value is not None and op.min_value >= op.max_value:
        raise DocumentError("The data bars' minimum has to be below their maximum")
    document = work.document
    bars: dict[str, Any] = copy.deepcopy(old) if old else {}

    def fill(color: str) -> dict[str, Any]:
        return {"solid": {"color": {"expr": _text(color)}}}

    if op.min_value is not None:
        bars["minValue"] = {"expr": _typed_bar_value(op.min_value, basis.type_name)}
    if op.max_value is not None:
        bars["maxValue"] = {"expr": _typed_bar_value(op.max_value, basis.type_name)}
    # A change to one end is held to the other end the bars already have.
    low, high = (expr.decode(bars.get(key)) for key in ("minValue", "maxValue"))
    if isinstance(low, int | float) and isinstance(high, int | float) and low >= high:
        raise DocumentError(
            f"The data bars would start at {_n(low)} and end at {_n(high)}: the minimum has to "
            "be below the maximum"
        )
    for key, color, theme_key, fallback in (
        ("positiveColor", op.positive_color, "maximum", "#118DFF"),
        ("negativeColor", op.negative_color, "minimum", "#DEEFFF"),
        ("axisColor", op.axis_color, "foreground", "#252423"),
    ):
        if color is not None:
            bars[key] = fill(_hex(document, color))
        elif key not in bars:
            bars[key] = fill(_theme_color(document, theme_key, fallback))
    if op.direction is not None or "reverseDirection" not in bars:
        bars["reverseDirection"] = {
            "expr": _literal("true" if op.direction == "right_to_left" else "false")
        }
    if op.bar_only is not None or "hideText" not in bars:
        bars["hideText"] = {"expr": _literal("true" if op.bar_only else "false")}
    if op.apply_to != "values":
        bars["totalMatchingOption"] = {"expr": _literal(f"{_MATCHING[op.apply_to]}L")}
    else:
        bars.pop("totalMatchingOption", None)
    order = (
        "minValue",
        "maxValue",
        "positiveColor",
        "negativeColor",
        "axisColor",
        "reverseDirection",
        "hideText",
        "totalMatchingOption",
    )
    return {key: bars[key] for key in (*order, *(k for k in bars if k not in order)) if key in bars}


def _field_value(
    work: _Work, model: ModelInfo, op: SetConditionalFormat, target: str
) -> tuple[dict[str, Any], str]:
    if op.measure is None:
        raise DocumentError(
            "Name the measure (or column) whose values give the "
            f'{_TARGET_WORDS[target]}, for example a measure that returns "#FF0000" or "red"'
        )
    ref, aggregation = _parse_ref(op.measure)
    kind, table, name = model.resolve(ref)
    if kind == "measure":
        return expr.measure_ref(table, name), f"the measure [{name}]"
    # A column gives its first or last value per row, the only summaries Desktop offers here.
    function = op.summarization or ("last" if aggregation == "Max" else "first")
    if function not in {"first", "last"}:
        raise DocumentError("A column's values give a color as its first or last value only")
    node = {
        "Aggregation": {
            "Expression": expr.column_ref(table, name),
            "Function": _FUNCTIONS[function],
        }
    }
    return node, f"the {function} value of {table}[{name}]"


def _wrap(spot: _Spot, inner: dict[str, Any], icon_parts: dict[str, Any] | None) -> Any:
    """The property value around a query expression, as the property's type stores it."""
    if spot.prop in {"fontColor", "backColor", "fill", "color"}:
        return {"solid": {"color": {"expr": inner}}}
    if spot.prop == "icon":
        value: dict[str, Any] = {"kind": "Icon"}
        value.update(icon_parts or {})
        value["value"] = {"expr": inner}
        return value
    return {"expr": inner}


def _icon_parts(op: SetConditionalFormat, old: Any) -> dict[str, Any]:
    parts: dict[str, Any] = {}
    previous = old if isinstance(old, dict) and old.get("kind") == "Icon" else {}
    if op.icon_position is not None:
        parts["layout"] = {"expr": _text(_LAYOUT_WORDS[op.icon_position])}
    elif "layout" in previous:
        parts["layout"] = copy.deepcopy(previous["layout"])
    else:
        parts["layout"] = {"expr": _text("Before")}
    if op.icon_alignment is not None:
        parts["verticalAlignment"] = {"expr": _text(_ALIGN_WORDS[op.icon_alignment])}
    elif "verticalAlignment" in previous:
        parts["verticalAlignment"] = copy.deepcopy(previous["verticalAlignment"])
    else:
        parts["verticalAlignment"] = {"expr": _text("Top")}
    return parts


# --- set_conditional_format -------------------------------------------------------------------


# The settings each style reads; anything else given is refused so nothing asked for is
# silently dropped.
_STYLE_SETTINGS: dict[str, frozenset[str]] = {
    "gradient": frozenset(
        {
            "min_color",
            "mid_color",
            "max_color",
            "min_value",
            "mid_value",
            "max_value",
            "blank",
            "blank_color",
            "based_on",
            "summarization",
        }
    ),
    "rules": frozenset({"rules", "based_on", "summarization"}),
    "field_value": frozenset({"measure", "summarization"}),
    "data_bars": frozenset(
        {
            "positive_color",
            "negative_color",
            "axis_color",
            "direction",
            "bar_only",
            "min_value",
            "max_value",
        }
    ),
    "icons": frozenset(
        {"icon_set", "rules", "icon_position", "icon_alignment", "based_on", "summarization"}
    ),
}
_ALL_SETTINGS = frozenset().union(*_STYLE_SETTINGS.values())


def _check_arguments(op: SetConditionalFormat, target: str) -> None:
    allowed = set(_STYLE_SETTINGS[op.style])
    if target == "icon":
        allowed |= {"icon_position", "icon_alignment"}
    given = sorted(n for n in _ALL_SETTINGS if getattr(op, n) is not None and n not in allowed)
    if given:
        raise DocumentError(
            f"A {op.style.replace('_', ' ')} format does not use {', '.join(given)}"
        )
    if op.apply_to != "values" and target not in _TABLE_TARGETS:
        raise DocumentError("Only a table or matrix column has totals to format separately")


def _category_names(entries: Iterable[dict[str, Any]]) -> list[str]:
    """The categories a chart colors one by one (its scopeId entries), as a person reads them."""
    names: list[str] = []
    for entry in entries:
        for item in (entry.get("selector") or {}).get("data") or []:
            comparison = (
                (item.get("scopeId") or {}).get("Comparison") if isinstance(item, dict) else None
            )
            right = comparison.get("Right") if isinstance(comparison, dict) else None
            value = expr.decode({"expr": right}) if isinstance(right, dict) else None
            if value is not None:
                names.append(str(value))
    return list(dict.fromkeys(names))


def _apply_set_conditional_format(
    document: PbirDocument, op: SetConditionalFormat, model: ModelInfo
) -> HandlerResult:
    work = _Work(document, op)
    target = _target_for(work, op)
    _check_arguments(op, target)
    spot = _cf_spot(work, target, op.field, op.apply_to)
    existing = _existing(work, spot)
    old_value = existing[0]["properties"][spot.prop] if existing else None
    old_style = _style_of(old_value)
    grouped = _has_grouping(work.work)
    # Rules on the icon target are icon rules.
    style_now = "icons" if target == "icon" and op.style == "rules" else op.style

    def basis() -> _Basis:
        if op.based_on is not None:
            return _model_basis(model, op.based_on, op.summarization)
        if spot.projection is not None:
            return _projection_basis(model, spot.projection, op.summarization)
        values = _value_projections(work.work)
        if not values:
            raise DocumentError(f"{work.headline()} shows no values to format by")
        return _projection_basis(model, values[0], op.summarization)

    edit = False  # the same kind of format, changed in part: nothing is lost
    moved = False  # icons placed anew, their rules kept
    icon_parts: dict[str, Any] | None = None
    if style_now == "data_bars":
        if op.based_on is not None:
            raise DocumentError("Data bars always show the column's own values")
        bars_basis = basis()
        old_bars = old_value if old_style == "data_bars" else None
        value: Any = _data_bars(work, op, bars_basis, old_bars)
        edit = old_bars is not None
        what = "data bars"
    elif style_now == "gradient":
        old = _old_gradient(existing, spot) if old_style == "gradient" else None
        kept_input = old.get("Input") if old is not None else None
        if isinstance(kept_input, dict) and op.based_on is None and op.summarization is None:
            # Changing a color or a stop keeps the field the scale follows.
            base = _node_basis(model, kept_input, _node_words(kept_input, spot))
        else:
            base = basis()
            if old is not None and old.get("Input") != base.node:
                old = None
        inner, what = _gradient(work, op, base, grouped, old)
        edit = old is not None
        value = _wrap(spot, inner, None)
    elif style_now == "rules":
        if op.rules is None:
            raise DocumentError("Give the rules: each has a color and a range, a value or blank")
        base = basis()
        inner = _rules(document, op.rules, base, grouped, icons=False)
        count = len(op.rules)
        what = f"{count} rule{'s' if count != 1 else ''} on {base.words}"
        value = _wrap(spot, inner, None)
    elif style_now == "icons":
        if op.icon_set is not None and op.rules is not None:
            raise DocumentError("Give an icon set or icon rules, not both")
        icon_parts = _icon_parts(op, old_value)
        moved = op.icon_set is None and op.rules is None and old_style is not None
        if moved:
            inner = copy.deepcopy(_expression_of(old_value) or {})
            what = "the same icons"
            edit = True
        else:
            base = basis()
            if op.rules is not None:
                inner = _rules(document, op.rules, base, grouped, icons=True)
                count = len(op.rules)
                what = f"icons by {count} rule{'s' if count != 1 else ''} on {base.words}"
            else:
                chosen = op.icon_set or "ThreeColoredShapes"
                if not grouped:
                    raise DocumentError(
                        "This visual shows a single value, so an icon set has no range to split: "
                        "give icon rules with numbers"
                    )
                if not base.numeric:
                    raise DocumentError(
                        f"An icon set follows numbers, and {base.words} holds "
                        f"{_TYPE_WORDS.get(base.type_name or '', 'text')}"
                    )
                inner = _icon_set_rules(chosen, base)
                what = f"icons ({_ICON_SET_WORDS[chosen]}) on {base.words}"
        value = _wrap(spot, inner, icon_parts)
    else:  # field_value
        inner, what = _field_value(work, model, op, target)
        if target == "icon":
            icon_parts = _icon_parts(op, old_value)
        value = _wrap(spot, inner, icon_parts)

    # Writing over other conditional formatting loses it: only with the user's yes. The same
    # value moved to another selector (apply_to) or an edit of the same scale loses nothing.
    if existing and not edit and old_value != value:
        if not op.replace_conditional:
            raise NeedsConfirmation(
                "replace_conditional",
                f"{sentence(spot.words)} in {work.label()} already follows "
                f"{_cf_words(old_value)}. Replace it with {what}?",
            )
        work.replaced.append(f"{_cf_words(old_value)} on {spot.words.removeprefix('the ')}")
    replaced_plain = None
    if spot.where == _CONTAINER:
        current = expr.get_property(work.holder(_CONTAINER), spot.object_name, spot.prop, None)
        if current is not None and expr.protected_kind(current) is None:
            replaced_plain = expr.decode(current)
    _place(work, spot, existing, value)
    if replaced_plain is not None:
        work.replaced.append(
            f"the fixed {_TARGET_WORDS[target]} {_plain_words(work, replaced_plain)}"
        )
    if target == "title_text":
        title = work.holder(_CONTAINER).get("title", [])
        if any(expr.decode(e.get("properties", {}).get("show")) is False for e in title):
            work.warnings.append(
                "The title is turned off, so the text only shows once the title is turned on "
                "in Power BI Desktop's Format pane."
            )
    if spot.object_name == "dataPoint":
        categories = _category_names(work.holder(_OBJECTS).get("dataPoint", []))
        if categories:
            work.warnings.append(
                f"{work.headline()} also colors {', '.join(categories[:4])} one by one; those "
                "colors were kept, and Power BI may show them instead on those bars."
            )
    verb = "Updated" if edit else "Set"
    if moved:
        return work.finish(f"Moved {spot.words} in {work.label()}.")
    if style_now == "data_bars":
        column = spot.words.removeprefix("the data bars of ")
        return work.finish(f"{verb} data bars on {column} in {work.label()}.")
    return work.finish(f"{verb} {spot.words} in {work.label()} to follow {what}.")


# --- clear_conditional_format -----------------------------------------------------------------


def _clear_spots(work: _Work, op: ClearConditionalFormat) -> list[_Spot]:
    family = _family(work.visual_type)
    targets: list[str]
    if op.target is not None:
        targets = [op.target]
    elif family == "table":
        targets = list(_TABLE_TARGETS)
    elif family == "chart":
        targets = ["data_colors"]
    elif family == "card":
        targets = ["font_color"]
    else:
        targets = []
    if op.target is None and op.field is None:
        targets += ["title_color", "visual_background"]
    spots: list[_Spot] = []
    for target in targets:
        if target in _CONTAINER_TARGETS:
            object_name, prop = _CONTAINER_TARGETS[target]
            spots.append(
                _Spot(_CONTAINER, object_name, prop, None, None, f"the {_TARGET_WORDS[target]}")
            )
            continue
        if family == "table" and target in _TABLE_TARGETS:
            object_name, prop = _TABLE_TARGETS[target]
            fields = (
                [_find_field(work, op.field)[2]]
                if op.field is not None
                else _projections(work.work)
            )
            for projection in fields:
                column = projection_label(projection).strip()
                spots.append(
                    _Spot(
                        _OBJECTS,
                        object_name,
                        prop,
                        None,
                        _query_ref(projection),
                        f"the {_TARGET_WORDS[target]} of the {column} column",
                    )
                )
            continue
        if family == "card" and work.visual_type == "cardVisual" and op.field is None:
            # A new card keeps each field's rule under its own name: every field's goes.
            object_name, prop = _CARD_VALUE_COLOR[work.visual_type]
            for projection in _value_projections(work.work):
                spots.append(
                    _Spot(
                        _OBJECTS,
                        object_name,
                        prop,
                        None,
                        _query_ref(projection),
                        f"the value's font color of {projection_label(projection).strip()}",
                    )
                )
            continue
        try:
            spot = _cf_spot(work, target, op.field, "values", clearing=True)
        except DocumentError:
            if op.target is not None:
                raise
            continue
        spots.append(spot)
        if family == "chart" and op.field is None:
            # A rule for one series is the chart's too when no series is named.
            for projection in _value_projections(work.work):
                spots.append(
                    _Spot(
                        _OBJECTS,
                        "dataPoint",
                        "fill",
                        None,
                        _query_ref(projection),
                        f"the color of {projection_label(projection).strip()}",
                    )
                )
    if op.target is None:
        # Clearing everything touches formatting only, never a binding (see _CF_PROPERTIES).
        spots = [s for s in spots if (s.where, s.object_name, s.prop) in _CF_PROPERTIES]
    return spots


def _apply_clear_conditional_format(
    document: PbirDocument, op: ClearConditionalFormat, model: ModelInfo
) -> HandlerResult:
    work = _Work(document, op)
    removed: list[str] = []
    for spot in _clear_spots(work, op):
        entries = _existing(work, spot)
        if not entries:
            continue
        removed.append(_removed_words(spot, entries[0]["properties"][spot.prop]))
        _remove(work, spot, entries)
    if not removed:
        where = f" on {op.field}" if op.field else ""
        kind = _TARGET_WORDS[op.target] if op.target else "formatting"
        raise DocumentError(f"{work.headline()} has no conditional {kind}{where} to remove")
    listed = "; ".join(dict.fromkeys(removed))
    return work.finish(f"Removed {listed} in {work.label()}; the plain formatting shows again.")


# --- set_number_format ------------------------------------------------------------------------

_PRESET_UNITS = {"thousands": "thousands", "millions": "millions", "billions": "billions"}
_PRESET_DECIMALS = {"whole_number": 0, "one_decimal": 1, "two_decimals": 2}
# A format code scales by one thousand per trailing comma.
_SCALE_CODES = {
    1000: ',"K"',
    1000000: ',,"M"',
    1000000000: ',,,"B"',
    1000000000000: ',,,,"T"',
}


def _decimals(count: int) -> str:
    return "." + "0" * count if count else ""


def _currency(symbol: str | None, decimals: int | None) -> str:
    sign = symbol or "$"
    # Power BI's own currency codes escape a dollar sign and quote any other symbol.
    shown = "\\$" if sign == "$" else f'"{sign}"' if not re.fullmatch(r"[€£¥₹]", sign) else sign
    body = f"{shown}#,0{_decimals(2 if decimals is None else decimals)}"
    return f"{body};({body});{body}"


def _preset_code(preset: str, decimals: int | None, symbol: str | None) -> str | None:
    """The format code of a preset that only a code can say (percent, currency)."""
    if preset == "percent":
        return "0" + _decimals(2 if decimals is None else decimals) + "%"
    if preset == "currency":
        return _currency(symbol, decimals)
    return None


def _check_code(code: str) -> str:
    text = code.strip()
    try:
        expr.quote(text)
    except expr.ExpressionError as error:
        raise DocumentError(str(error)) from error
    if text.count('"') % 2:
        raise DocumentError(f"The format code {code!r} has an unclosed quote")
    if not text:
        raise DocumentError("Give a format code such as #,0.00 or 0.0%")
    return text


@dataclass
class _NumberPlan:
    units: int | None = None  # a display unit code
    decimals: int | None = None
    code: str | None = None  # a format code
    reset: bool = False
    words: str = ""


def _number_plan(op: SetNumberFormat) -> _NumberPlan:
    given = [op.preset, op.format, op.display_units, op.decimals]
    if all(v is None for v in given):
        raise DocumentError(
            "Say how the numbers should read: a preset, a format code, units or decimals"
        )
    if op.preset is not None and op.format is not None:
        raise DocumentError("Give a preset or a format code, not both")
    if op.currency_symbol is not None and op.preset != "currency":
        raise DocumentError("A currency symbol goes with the currency preset")
    if op.currency_symbol is not None and (
        not op.currency_symbol.strip() or any(c in op.currency_symbol for c in '"\\')
    ):
        # A quote or backslash would end or escape the quoted symbol inside the format code.
        raise DocumentError(
            f"{op.currency_symbol!r} cannot be a currency symbol: give one like $, € or CHF"
        )
    plan = _NumberPlan()
    if op.preset == "general":
        if any(v is not None for v in (op.format, op.display_units, op.decimals)):
            raise DocumentError("The general preset resets the format; give nothing else with it")
        plan.reset = True
        plan.words = "the model's own format"
        return plan
    if op.preset in _PRESET_UNITS:
        plan.units = _UNITS[_PRESET_UNITS[op.preset]]
        plan.decimals = op.decimals
        plan.words = op.preset
    elif op.preset in _PRESET_DECIMALS:
        plan.decimals = _PRESET_DECIMALS[op.preset] if op.decimals is None else op.decimals
        if op.preset == "whole_number":
            plan.units = _UNITS["none"]
        plan.words = op.preset.replace("_", " ")
    elif op.preset in {"percent", "currency"}:
        code = _preset_code(op.preset, op.decimals, op.currency_symbol)
        plan.code = _check_code(code) if code is not None else None
        plan.words = op.preset
    if op.format is not None:
        plan.code = _check_code(op.format)
        plan.words = f"the format {plan.code}"
    if op.display_units is not None:
        if plan.units is not None and plan.units != _UNITS[op.display_units]:
            raise DocumentError("The preset already sets display units; give one or the other")
        plan.units = _UNITS[op.display_units]
        plan.words = ", ".join(w for w in (plan.words, op.display_units) if w)
    if plan.code is not None and plan.units is not None:
        raise DocumentError("A format code sets the units itself; give a code or display units")
    if op.format is not None and op.decimals is not None:
        raise DocumentError("A format code sets the decimals itself; give a code or decimals")
    if op.decimals is not None and plan.decimals is None and plan.code is None:
        plan.decimals = op.decimals
    if op.decimals is not None:
        plan.words = ", ".join(w for w in (plan.words, _plural(op.decimals, "decimal")) if w)
    return plan


def _code_of(plan: _NumberPlan) -> str | None:
    """The plan as one format code, for a field whose visual has no units or decimals of its
    own (a table of text, a legacy card's field): the code says it all. None resets."""
    if plan.reset:
        return None
    if plan.code is not None:
        return plan.code
    if plan.units == _UNITS["auto"]:
        raise DocumentError(
            "Automatic display units exist on data labels and cards only; say thousands, "
            "millions or none"
        )
    body = "#,0" + _decimals(plan.decimals or 0)
    return body + _SCALE_CODES.get(plan.units or 1, "")


def _number_target(work: _Work, op: SetNumberFormat) -> str:
    """Where the numbers of the visual read. A field target goes to the visual's own setting
    for that field when it has one (a table column, a card value, a chart's data labels), which
    Desktop always keeps; only a visual with none takes the field's visual-level format."""
    family = _family(work.visual_type)
    if op.target not in {None, "field"}:
        return str(op.target)
    if work.visual_type in _TABLE_TYPES:
        return "column"
    if family == "card":
        return "card_value"
    if work.has("labels", "labelDisplayUnits"):
        return "data_labels"
    if op.field is not None or op.target == "field":
        return "field"
    raise DocumentError(
        f"{work.headline()} has no data labels or values with a number format; name a field "
        "(target field) to format it in this visual"
    )


# The visual-level format string (a projection's ``format``) is read and written by Desktop only
# behind its preview switches (visualLevelFormatStringAuthoring, visualCalculationsAuthoring,
# visualLevelFormatStringConsumption, all off by default in 2.157); without them Desktop ignores
# it and drops it on its next save. Used only where no other setting can say the code.
FORMAT_WARNING = (
    "Power BI Desktop keeps this only when visual-level format strings are turned on "
    "(Options > Preview features); otherwise it uses the measure's own format."
)


def _set_projection_format(work: _Work, projection: dict[str, Any], code: str | None) -> None:
    if code is None:
        if "format" not in projection:
            return
        projection.pop("format")
    else:
        projection["format"] = code
        work.warnings.append(FORMAT_WARNING)
    work.mark("query", "query", "format")


def _scope(work: _Work, op: SetNumberFormat) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The selector of a write for the named field (None: the whole visual) and the value
    fields it covers. The only value field of a visual is the whole visual."""
    values = _value_projections(work.work)
    if op.field is None:
        return None, values
    _, _, projection = _find_field(work, op.field)
    if _is_grouping(projection):
        raise DocumentError(
            f"{projection_label(projection).strip()} is what {work.label()} is split by; name "
            "one of the values it shows"
        )
    if len(values) == 1 and values[0] is projection:
        return None, [projection]
    return {"metadata": _query_ref(projection)}, [projection]


def _fields_words(projections: Sequence[dict[str, Any]]) -> str:
    names = [projection_label(p).strip() for p in projections]
    if len(names) == 1:
        return names[0]
    shown = ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
    return f"the columns {shown}"


def _format_projections(work: _Work, op: SetNumberFormat) -> list[dict[str, Any]]:
    if op.field is not None:
        # A matrix's numbers are its values; its row and column fields are headers.
        roles = ("Values",) if work.visual_type == "pivotTable" else None
        return [_find_field(work, op.field, roles)[2]]
    values = _value_projections(work.work)
    if not values:
        raise DocumentError(f"{work.headline()} shows no numbers to format")
    return values


def _remove_everywhere(
    work: _Work,
    object_name: str,
    props: Iterable[str],
    selector: dict[str, Any] | None = None,
) -> None:
    """Drop number-format properties so the model's format shows: from the entry of one field
    (``selector``), else from the whole visual's entries and its per-field ones (no selector,
    the default state, or a field's metadata)."""
    objects = work.holder(_OBJECTS)
    if object_name not in objects:
        return
    wanted = list(props)
    for entry in objects[object_name]:
        found = entry.get("selector")
        if selector is not None:
            if not expr.same_selector(found, selector):
                continue
        elif found and found != {"id": expr.STATE_DEFAULT} and set(found) != {"metadata"}:
            continue
        for prop in wanted:
            if entry.get("properties", {}).pop(prop, None) is not None:
                work.mark(_OBJECTS, object_name, prop)
    objects[object_name] = [e for e in objects[object_name] if e.get("properties")]
    if not objects[object_name]:
        del objects[object_name]


def _apply_set_number_format(
    document: PbirDocument, op: SetNumberFormat, model: ModelInfo
) -> HandlerResult:
    work = _Work(document, op)
    plan = _number_plan(op)
    target = _number_target(work, op)
    family = _family(work.visual_type)
    replace = op.replace_conditional
    if target == "field":
        code = _code_of(plan)
        chosen = _format_projections(work, op)
        for projection in chosen:
            _set_projection_format(work, projection, code)
        return work.finish(
            f"Set the number format of {_fields_words(chosen)} in {work.label()} to {plan.words}."
        )
    if target == "column":
        if work.visual_type not in _TABLE_TYPES:
            raise DocumentError(f"{work.headline()} has no columns; use target field")
        chosen = _format_projections(work, op)
        for projection in chosen:
            selector = {"metadata": _query_ref(projection)}
            if plan.reset:
                _remove_everywhere(
                    work, "columnFormatting", ("labelDisplayUnits", "labelPrecision"), selector
                )
                _set_projection_format(work, projection, None)
                continue
            if plan.units is not None:
                if plan.units == 0:
                    raise DocumentError("A table column has no automatic display units; say none")
                work.write_plain(
                    "columnFormatting",
                    "labelDisplayUnits",
                    plan.units,
                    "display units",
                    replace=replace,
                    selector=selector,
                )
            if plan.decimals is not None:
                work.write_plain(
                    "columnFormatting",
                    "labelPrecision",
                    plan.decimals,
                    "decimals",
                    replace=replace,
                    selector=selector,
                )
            if plan.code is not None:
                _set_projection_format(work, projection, plan.code)
        return work.finish(
            f"Set the number format of {_fields_words(chosen)} in {work.label()} to {plan.words}."
        )
    if target == "value_axis":
        if not work.has("valueAxis", "labelDisplayUnits"):
            raise DocumentError(f"{work.headline()} has no value axis")
        scoped, _ = _scope(work, op)
        if scoped is not None:
            raise DocumentError(
                f"The value axis of {work.label()} is shared by all its values; leave out the field"
            )
        if plan.code is not None:
            raise DocumentError(
                "The axis labels follow the field's format: set a format code on the data "
                "labels or the field instead"
            )
        if plan.reset:
            _remove_everywhere(work, "valueAxis", ("labelDisplayUnits", "labelPrecision"))
        if plan.units is not None:
            work.write_plain(
                "valueAxis", "labelDisplayUnits", plan.units, "axis display units", replace=replace
            )
        if plan.decimals is not None:
            work.write_plain(
                "valueAxis", "labelPrecision", plan.decimals, "axis decimals", replace=replace
            )
        return work.finish(f"Set the value axis of {work.label()} to {plan.words}.")
    if target == "data_labels":
        if not work.has("labels", "labelDisplayUnits"):
            raise DocumentError(f"{work.headline()} has no data labels")
        custom = (
            "valueCustomFormatString" if work.has("labels", "valueCustomFormatString") else None
        )
        return _numbers_in(work, op, plan, "labels", custom, "data label")
    if family != "card":
        raise DocumentError(f"{work.headline()} is not a card; use target field")
    value_object = "value" if work.visual_type == "cardVisual" else "labels"
    custom = "customFormatString" if work.has(value_object, "customFormatString") else None
    return _numbers_in(work, op, plan, value_object, custom, "value")


def _numbers_in(
    work: _Work,
    op: SetNumberFormat,
    plan: _NumberPlan,
    object_name: str,
    custom: str | None,
    noun: str,
) -> HandlerResult:
    """Display units, decimals and a format code of a chart's data labels or a card's value:
    for the whole visual, or for the named field under its metadata (a new card keeps each
    field's value style that way). A code goes to the Custom display unit (-1) with the
    object's own format-code property, which Desktop keeps; a visual without one (a pie's
    labels, the old card) takes the field's visual-level format, with the warning."""
    selector, chosen = _scope(work, op)
    replace = op.replace_conditional
    props = ("labelDisplayUnits", "labelPrecision", *(p for p in [custom] if p))
    if plan.reset:
        _remove_everywhere(work, object_name, props, selector)
        for projection in chosen:
            _set_projection_format(work, projection, None)
    if plan.code is not None:
        if custom is None:
            if not chosen:
                raise DocumentError(f"{work.headline()} shows no numbers to format")
            for projection in chosen:
                _set_projection_format(work, projection, plan.code)
        else:
            work.write_plain(
                object_name,
                "labelDisplayUnits",
                _enum_units(work, object_name, _CUSTOM_UNITS),
                f"{noun} display units",
                replace=replace,
                selector=selector,
            )
            work.write_plain(
                object_name,
                custom,
                plan.code,
                f"{noun} format",
                replace=replace,
                selector=selector,
            )
    if plan.units is not None:
        work.write_plain(
            object_name,
            "labelDisplayUnits",
            _enum_units(work, object_name, plan.units),
            f"{noun} display units",
            replace=replace,
            selector=selector,
        )
    if plan.decimals is not None:
        work.write_plain(
            object_name,
            "labelPrecision",
            plan.decimals,
            f"{noun} decimals",
            replace=replace,
            selector=selector,
        )
    what = "the data labels" if noun == "data label" else "the value"
    of = f" of {_fields_words(chosen)}" if selector is not None else ""
    return work.finish(f"Set {what}{of} of {work.label()} to {plan.words}.")


def _enum_units(work: _Work, object_name: str, units: int) -> Any:
    """Display units as the property's catalog type takes them: an enum spells them as
    strings of numbers, a formatting property as the number."""
    spec = expr.property_spec(work.visual_type, object_name, "labelDisplayUnits")
    if spec is not None and spec[1].get("type") == "enum":
        return str(units)
    return units


# --- set_table_layout -------------------------------------------------------------------------


def _unstepped_layout(work: _Work) -> str:
    """The layout Desktop gives a matrix whose stepped layout is off (its upgrade of the old
    ``rowHeaders.stepped``): outline when row subtotals sit on top, else tabular."""
    found = expr.effective_property(
        work.holder(_OBJECTS), "subTotals", "rowSubtotalsPosition", None
    )
    return "outline" if found is not None and found.value == "Top" else "tabular"


def _drop_old_stepped(work: _Work) -> None:
    """Desktop removes the whole-visual ``rowHeaders.stepped`` when it writes
    ``general.layout``; two answers to one question would disagree."""
    objects = work.holder(_OBJECTS)
    entries = objects.get("rowHeaders")
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not entry.get("selector") and "stepped" in entry.get("properties", {}):
            del entry["properties"]["stepped"]
            work.mark(_OBJECTS, "rowHeaders", "stepped")
    objects["rowHeaders"] = [e for e in entries if e.get("properties")]
    if not objects["rowHeaders"]:
        del objects["rowHeaders"]


def _apply_set_table_layout(
    document: PbirDocument, op: SetTableLayout, model: ModelInfo
) -> HandlerResult:
    work = _Work(document, op)
    matrix = work.visual_type == "pivotTable"
    if work.visual_type not in _TABLE_TYPES:
        raise DocumentError(f"{work.headline()} is not a table or matrix")
    replace = op.replace_conditional
    changed: list[str] = []

    def flag(object_name: str, prop: str, value: bool | None, words: str) -> None:
        if value is None:
            return
        work.write_plain(object_name, prop, value, words, replace=replace)
        changed.append(f"{words} {'on' if value else 'off'}")

    if op.totals is not None:
        if matrix:
            raise DocumentError(
                "A matrix shows its totals as row and column subtotals: use row_subtotals and "
                "column_subtotals"
            )
        flag("total", "totals", op.totals, "totals")
    for setting, prop, words in (
        (op.row_subtotals, "rowSubtotals", "row subtotals"),
        (op.column_subtotals, "columnSubtotals", "column subtotals"),
        (op.values_on_rows, "valuesOnRow", "values on rows"),
    ):
        if setting is not None and not matrix:
            raise DocumentError(f"Only a matrix has {words}")
        flag("values" if prop == "valuesOnRow" else "subTotals", prop, setting, words)
    layout: str | None = op.layout
    if op.stepped is not None:
        if not matrix:
            raise DocumentError("Only a matrix has a stepped layout")
        wanted = "compact" if op.stepped else _unstepped_layout(work)
        if layout is not None and layout != wanted:
            raise DocumentError(
                f"A stepped layout {'on' if op.stepped else 'off'} is the {wanted} layout here; "
                "give one of the two"
            )
        layout = wanted
    if layout is not None:
        if not matrix:
            raise DocumentError("Only a matrix has a compact, outline or tabular layout")
        work.write_plain("general", "layout", layout.capitalize(), "layout", replace=replace)
        _drop_old_stepped(work)
        changed.append(f"{layout} layout")
    if not changed:
        raise DocumentError("Say what to change in the table's layout")
    return work.finish(f"Changed the layout of {work.label()} ({', '.join(changed)}).")


# --- move_column ------------------------------------------------------------------------------


def _apply_move_column(document: PbirDocument, op: MoveColumn, model: ModelInfo) -> HandlerResult:
    work = _Work(document, op)
    role, _, projection = _find_field(work, op.field)
    projections = work.work["visual"]["query"]["queryState"][role]["projections"]
    index = next(i for i, p in enumerate(projections) if p is projection)
    column = projection_label(projection).strip()
    if len(projections) == 1:
        raise DocumentError(f"The {column} field is the only one in its well: it has nowhere to go")
    if op.to_index >= len(projections):
        raise DocumentError(
            f"{work.headline()} has {_plural(len(projections), 'field')} there; places run from "
            f"0 (first) to {len(projections) - 1} (last)"
        )
    if op.to_index == index:
        raise DocumentError(f"The {column} column is already there: nothing to change")
    moved = projections.pop(index)
    projections.insert(op.to_index, moved)
    work.mark("query", "query", "order")
    place = (
        "first"
        if op.to_index == 0
        else "last"
        if op.to_index == len(projections) - 1
        else f"place {op.to_index + 1}"
    )
    return work.finish(f"Moved the {column} column of {work.label()} to {place}.")


# --- the family's dispatcher ------------------------------------------------------------------

CONDITIONAL_HANDLERS: dict[str, Handler] = {
    "set_conditional_format": _apply_set_conditional_format,
    "clear_conditional_format": _apply_clear_conditional_format,
    "set_number_format": _apply_set_number_format,
    "set_table_layout": _apply_set_table_layout,
    "move_column": _apply_move_column,
}


def parse_conditional(raw: Sequence[Mapping[str, Any]]) -> list[Any]:
    """Operation dicts of this family into typed operations (DocumentError names the step)."""
    parsed: list[Any] = []
    for index, item in enumerate(raw, start=1):
        try:
            parsed.append(_ADAPTER.validate_python(item))
        except ValidationError as error:
            problem = error.errors(include_url=False)[0]
            where = ".".join(str(p) for p in problem["loc"][1:]) or "the step"
            raise DocumentError(
                f"Step {index} ({item.get('op')}): {where}: {problem['msg']}"
            ) from error
    return parsed


def apply_conditional(
    document: PbirDocument, operations: Sequence[Any], model: ModelInfo
) -> EditOutcome:
    """Apply this family's operations in order, the way ``report_edits.apply_operations`` does:
    all or nothing (the caller discards the document on an error), a question or confirmation
    keeps its type with the step number."""
    outcome = EditOutcome()
    for index, operation in enumerate(operations):
        handler = CONDITIONAL_HANDLERS[operation.op]
        try:
            result = handler(document, operation, model)
        except expr.ExpressionError as error:
            raise DocumentError(str(error)) from error
        except (NeedsConfirmation, NeedsChoice) as error:
            error.step = index + 1
            raise
        except DocumentError as error:
            prefix = f"Step {index + 1}: " if len(operations) > 1 else ""
            raise DocumentError(prefix + str(error)) from error
        if isinstance(result, HandlerResult):
            outcome.summaries.append(result.summary)
            outcome.warnings.extend(result.warnings)
            outcome.protected.extend(result.protected)
            outcome.replaced.extend(result.replaced)
            outcome.visible_in.extend(result.visible_in)
            outcome.partial = outcome.partial or result.partial
        else:
            outcome.summaries.append(result)
        outcome.report_changed = True
    return outcome


# --- tool cards -------------------------------------------------------------------------------

_TABLE = "e82e02f93055d6b0b544"  # the Agents table
_CHART = "b404a461ede7604a2774"  # 'Total MTD Sales'
_CHART_2 = "da32cdbb0090e885649b"  # 'Avg MTD Sales by Agents'
_CARD = "fb4c5aa34e0b084b62b9"  # 'Agents with Positive Change %'
_CARD_2 = "2202e311b1493069e4eb"  # 'Agents Avg MTD Sales'
_CF_MEASURE = {"table": "# Measures", "name": "CF Table"}


def _build_conditional(filled: Mapping[str, Any]) -> dict[str, Any]:
    operation: dict[str, Any] = {"op": "set_conditional_format"}
    simple = (
        "visual",
        "style",
        "target",
        "field",
        "based_on",
        "min_color",
        "mid_color",
        "max_color",
        "min_value",
        "mid_value",
        "max_value",
        "blank",
        "summarization",
        "apply_to",
        "measure",
        "positive_color",
        "negative_color",
        "bar_only",
        "icon_set",
        "icon_position",
        "icon_alignment",
    )
    for name in simple:
        if filled.get(name) is not None:
            operation[name] = filled[name]
    if filled.get("threshold") is not None:
        # Only the rules whose colors the request names: "red when below 0" colors the values
        # below 0 and leaves the rest as they are (no green nobody asked for).
        threshold = filled["threshold"]
        unit = filled.get("threshold_unit") or "number"
        extra = {} if unit == "number" else {"unit": unit}
        rules = []
        if filled.get("below_color"):
            rules.append({"max": threshold, "color": filled["below_color"], **extra})
        if filled.get("above_color"):
            rules.append({"min": threshold, "color": filled["above_color"], **extra})
        if rules:
            operation.setdefault("style", "rules")
            operation["rules"] = rules
    return operation


def _build_move_column(filled: Mapping[str, Any]) -> dict[str, Any]:
    """A place as a person counts it (1 is first) into the operation's 0-based index."""
    operation: dict[str, Any] = {"op": "move_column"}
    for name in ("visual", "field"):
        if filled.get(name) is not None:
            operation[name] = filled[name]
    place = filled.get("place")
    if isinstance(place, int | float) and not isinstance(place, bool):
        operation["to_index"] = int(place) - 1
    return operation


def _visual_slot() -> Slot:
    return Slot("visual", "visual", "the visual to format")


TOOL_CARDS: tuple[ToolCard, ...] = (
    ToolCard(
        op="set_conditional_format",
        family=FAMILY,
        summary=(
            "Color a table column, chart bars or a card value by its values: a color scale, "
            "rules (red below a number), a measure that returns a color, data bars or icons"
        ),
        slots=(
            _visual_slot(),
            Slot(
                "style",
                "enum",
                "how the value decides: gradient (color scale), rules, field_value (a measure "
                "returns the color), data_bars, icons",
                choices=("gradient", "rules", "field_value", "data_bars", "icons"),
            ),
            Slot(
                "target",
                "enum",
                "what gets colored (left out: the chart's bars or the card's value)",
                required=False,
                choices=(
                    "font_color",
                    "background",
                    "data_colors",
                    "icon",
                    "web_url",
                    "title_text",
                    "title_color",
                    "visual_background",
                ),
            ),
            Slot(
                "field",
                "text",
                "the table column (as its header reads) or chart series",
                required=False,
            ),
            Slot("based_on", "field", "another field whose values decide", required=False),
            Slot("min_color", "color", "color of the lowest values", required=False),
            Slot("max_color", "color", "color of the highest values", required=False),
            Slot(
                "mid_color", "color", "color of the middle values (3-color scale)", required=False
            ),
            Slot("min_value", "number", "number where the scale or bars start", required=False),
            Slot("mid_value", "number", "number of the scale's middle color", required=False),
            Slot("max_value", "number", "number where the scale or bars end", required=False),
            Slot("threshold", "number", "one number splitting two colors (rules)", required=False),
            Slot(
                "threshold_unit",
                "enum",
                "the threshold as a number or a percent of the range",
                required=False,
                choices=("number", "percent"),
            ),
            Slot("below_color", "color", "color below the threshold", required=False),
            Slot("above_color", "color", "color from the threshold up", required=False),
            Slot(
                "blank",
                "enum",
                "how empty values are colored by a scale",
                required=False,
                choices=("as_zero", "no_color", "specific_color"),
            ),
            Slot(
                "summarization",
                "enum",
                "how a column's values are summed up",
                required=False,
                choices=(
                    "sum",
                    "average",
                    "min",
                    "max",
                    "count",
                    "count_distinct",
                    "median",
                    "first",
                    "last",
                ),
            ),
            Slot(
                "apply_to",
                "enum",
                "a table column's values, totals or both",
                required=False,
                choices=("values", "values_and_totals", "totals"),
            ),
            Slot(
                "measure", "measure", "the measure returning a color, icon or URL", required=False
            ),
            Slot("positive_color", "color", "data bar color", required=False),
            Slot("negative_color", "color", "data bar color below zero", required=False),
            Slot("bar_only", "bool", "show the bar without the number", required=False),
            Slot(
                "icon_set",
                "enum",
                "the icon set",
                required=False,
                choices=tuple(ICON_SETS),
            ),
            Slot(
                "icon_position",
                "enum",
                "icons left or right of the value",
                required=False,
                choices=("left", "right", "icon_only"),
            ),
            Slot(
                "icon_alignment",
                "enum",
                "icons at the top, middle or bottom of the cell",
                required=False,
                choices=("top", "middle", "bottom"),
            ),
        ),
        cues=(
            "conditional formatting",
            "conditional format",
            "color scale",
            "gradient",
            "heat map",
            "data bars",
            "data bar",
            "icons",
            "traffic lights",
            "arrows",
            "red when",
            "green when",
            "red if",
            "below zero",
            "negative in red",
            "based on",
            "color by value",
            "color the bars by",
            "fx",
            "rules",
            "field value",
        ),
        examples=(
            (
                "shade the Rank column background from light to dark blue",
                {
                    "op": "set_conditional_format",
                    "visual": _TABLE,
                    "style": "gradient",
                    "target": "background",
                    "field": "Rank",
                    "min_color": "#DEEFFF",
                    "max_color": "#118DFF",
                },
            ),
            (
                "shade the vs PM % cells red below 0 and green from 0 up",
                {
                    "op": "set_conditional_format",
                    "visual": _TABLE,
                    "style": "rules",
                    "target": "background",
                    "field": "vs PM %",
                    "rules": [
                        {"max": 0, "color": "#D64550"},
                        {"min": 0, "color": "#1AAB40"},
                    ],
                },
            ),
            (
                "color the Agent Name text with the CF Table measure",
                {
                    "op": "set_conditional_format",
                    "visual": _TABLE,
                    "style": "field_value",
                    "target": "font_color",
                    "field": "Agent Name",
                    "measure": _CF_MEASURE,
                },
            ),
            (
                "add data bars to the Rank column",
                {
                    "op": "set_conditional_format",
                    "visual": _TABLE,
                    "style": "data_bars",
                    "field": "Rank",
                    "positive_color": "#118DFF",
                },
            ),
            (
                "show three colored arrows next to vs PM %, on the right",
                {
                    "op": "set_conditional_format",
                    "visual": _TABLE,
                    "style": "icons",
                    "field": "vs PM %",
                    "icon_set": "ThreeColoredArrows",
                    "icon_position": "right",
                },
            ),
            (
                "color the bars of Total MTD Sales by value from grey to blue",
                {
                    "op": "set_conditional_format",
                    "visual": _CHART,
                    "style": "gradient",
                    "target": "data_colors",
                    "min_color": "#C8C6C4",
                    "max_color": "#118DFF",
                },
            ),
        ),
        build=_build_conditional,
        capability="conditional_formatting",
    ),
    ToolCard(
        op="clear_conditional_format",
        family=FAMILY,
        summary=(
            "Remove conditional formatting (rules, color scales, data bars, icons) from a visual "
            "or one column"
        ),
        slots=(
            _visual_slot(),
            Slot(
                "target",
                "enum",
                "which kind to remove (left out: every kind)",
                required=False,
                choices=(
                    "font_color",
                    "background",
                    "data_colors",
                    "icon",
                    "data_bars",
                    "web_url",
                    "title_text",
                    "title_color",
                    "visual_background",
                ),
            ),
            Slot("field", "text", "the column (left out: every column)", required=False),
        ),
        cues=(
            "remove conditional formatting",
            "clear conditional formatting",
            "turn off conditional formatting",
            "remove the data bars",
            "remove the icons",
            "remove the color rule",
            "plain colors again",
            "no more red and green",
        ),
        examples=(
            (
                "remove the color rule from the Rank column",
                {
                    "op": "clear_conditional_format",
                    "visual": _TABLE,
                    "target": "font_color",
                    "field": "Rank",
                },
            ),
            (
                "remove the data bars from the table",
                {"op": "clear_conditional_format", "visual": _TABLE, "target": "data_bars"},
            ),
            (
                "clear all conditional formatting from the Agent ID column",
                {"op": "clear_conditional_format", "visual": _TABLE, "field": "Agent ID"},
            ),
        ),
        capability="conditional_formatting",
    ),
    ToolCard(
        op="set_number_format",
        family=FAMILY,
        summary=(
            "Number format in one visual: thousands/millions, decimals, percent, currency or a "
            "format code, on data labels, the value axis, a card value or a table column"
        ),
        slots=(
            _visual_slot(),
            Slot(
                "target",
                "enum",
                "where (left out: data labels of a chart, a card's value, table columns)",
                required=False,
                choices=("data_labels", "value_axis", "card_value", "column", "field"),
            ),
            Slot("field", "text", "the column or field (left out: every value)", required=False),
            Slot(
                "preset",
                "enum",
                "a common format",
                required=False,
                choices=(
                    "general",
                    "whole_number",
                    "one_decimal",
                    "two_decimals",
                    "percent",
                    "currency",
                    "thousands",
                    "millions",
                    "billions",
                ),
            ),
            Slot("format", "text", "a Power BI format code such as #,0.00 or 0.0%", required=False),
            Slot(
                "display_units",
                "enum",
                "display units",
                required=False,
                choices=("auto", "none", "thousands", "millions", "billions", "trillions"),
            ),
            Slot("decimals", "integer", "decimal places", required=False, minimum=0, maximum=15),
            Slot("currency_symbol", "text", "currency symbol, e.g. $ or €", required=False),
        ),
        cues=(
            "number format",
            "format the numbers",
            "in thousands",
            "in millions",
            "values in thousands",
            "values in millions",
            "values with decimals",
            "show as k",
            "decimal places",
            "decimals",
            "percentage",
            "as percent",
            "currency",
            "dollar sign",
            "euro",
            "display units",
            "format string",
            "whole number",
        ),
        examples=(
            (
                "show the Total MTD Sales data labels in thousands with one decimal",
                {
                    "op": "set_number_format",
                    "visual": _CHART,
                    "target": "data_labels",
                    "preset": "thousands",
                    "decimals": 1,
                },
            ),
            (
                "show the value axis of Avg MTD Sales by Agents in millions",
                {
                    "op": "set_number_format",
                    "visual": _CHART_2,
                    "target": "value_axis",
                    "display_units": "millions",
                },
            ),
            (
                "show the Agents with Positive Change % card as a percentage with one decimal",
                {"op": "set_number_format", "visual": _CARD, "preset": "percent", "decimals": 1},
            ),
            (
                "show vs Previous Month in the table as currency in euros",
                {
                    "op": "set_number_format",
                    "visual": _TABLE,
                    "field": "vs Previous Month",
                    "preset": "currency",
                    "currency_symbol": "€",
                },
            ),
        ),
        capability="visual_number_formats",
    ),
    ToolCard(
        op="set_table_layout",
        family=FAMILY,
        summary=(
            "Table or matrix layout: the total row, row and column subtotals, stepped or tabular "
            "layout, values on rows"
        ),
        slots=(
            _visual_slot(),
            Slot("totals", "bool", "show the total row (table)", required=False),
            Slot("row_subtotals", "bool", "row subtotals (matrix)", required=False),
            Slot("column_subtotals", "bool", "column subtotals (matrix)", required=False),
            Slot(
                "layout",
                "enum",
                "matrix layout",
                required=False,
                choices=("compact", "outline", "tabular"),
            ),
            Slot("values_on_rows", "bool", "show values on rows (matrix)", required=False),
        ),
        cues=(
            "total row",
            "show totals",
            "hide totals",
            "subtotals",
            "stepped layout",
            "tabular",
            "show on rows",
        ),
        examples=(
            (
                "show the total row in the agents table",
                {"op": "set_table_layout", "visual": _TABLE, "totals": True},
            ),
            (
                "include a grand total below the agents table",
                {"op": "set_table_layout", "visual": _TABLE, "totals": True},
            ),
            (
                "turn on the total row of the table",
                {"op": "set_table_layout", "visual": _TABLE, "totals": True},
            ),
        ),
        # Totals, subtotals and the matrix layout are the table's structure, not its looks.
        capability="table_layout",
    ),
    ToolCard(
        op="move_column",
        family=FAMILY,
        summary="Move a table column (or a matrix field) to another position",
        slots=(
            _visual_slot(),
            Slot("field", "text", "the column as its header reads"),
            Slot(
                "place",
                "integer",
                "its new place, counted from 1 (the first column)",
                minimum=1,
                maximum=100,
            ),
        ),
        cues=(
            "move the column",
            "reorder columns",
            "column first",
            "column last",
            "put the column",
            "swap columns",
        ),
        examples=(
            (
                "move Rank to be the first column",
                {"op": "move_column", "visual": _TABLE, "field": "Rank", "to_index": 0},
            ),
            (
                "put MTD Sales last",
                {"op": "move_column", "visual": _TABLE, "field": "MTD Sales", "to_index": 5},
            ),
            (
                "move Agent Name to the third column",
                {"op": "move_column", "visual": _TABLE, "field": "Agent Name", "to_index": 2},
            ),
        ),
        build=_build_move_column,
        capability="table_layout",
    ),
)


# --- Desktop probes ---------------------------------------------------------------------------


Check = Callable[[Mapping[str, Any]], list[str]]


def _saved_visual(parts: Mapping[str, Any], visual: str) -> dict[str, Any]:
    """The saved visual.json of a visual id, or of the visual titled ``title:<text>`` (one a
    probe adds, whose id Studio chooses)."""
    for name, data in parts.items():
        if not str(name).endswith("/visual.json") or not isinstance(data, dict):
            continue
        if visual.startswith("title:"):
            titles = (data.get("visual", {}).get(_CONTAINER) or {}).get("title", [])
            texts = [expr.decode(t.get("properties", {}).get("text")) for t in titles]
            if visual.removeprefix("title:") in texts:
                return data
        elif str(name).endswith(f"/visuals/{visual}/visual.json"):
            return data
    return {}


def _values_at(
    parts: Mapping[str, Any],
    visual: str,
    where: str,
    object_name: str,
    prop: str,
    selector: Mapping[str, Any] | None,
) -> list[Any]:
    """The property's values in the saved visual under exactly this selector."""
    body = _saved_visual(parts, visual).get("visual", {})
    return [
        entry["properties"][prop]
        for entry in (body.get(where) or {}).get(object_name, [])
        if isinstance(entry, dict)
        and expr.same_selector(entry.get("selector"), dict(selector) if selector else None)
        and prop in entry.get("properties", {})
    ]


def _is(
    visual: str,
    object_name: str,
    prop: str,
    selector: Mapping[str, Any] | None,
    value: Any,
    where: str = _OBJECTS,
) -> Check:
    """Desktop's saved file has exactly ``value`` at the property."""

    def check(parts: Mapping[str, Any]) -> list[str]:
        found = _values_at(parts, visual, where, object_name, prop, selector)
        if not found:
            return [f"{object_name}.{prop} of {visual} is gone after the save"]
        if found[-1] != value:
            return [
                f"{object_name}.{prop} of {visual} changed in the save: "
                f"{json.dumps(found[-1])[:300]}"
            ]
        return []

    return check


def _absent(
    visual: str, object_name: str, prop: str, selector: Mapping[str, Any] | None = None
) -> Check:
    """No entry (under ``selector``, else under any selector) holds the property."""

    def check(parts: Mapping[str, Any]) -> list[str]:
        body = _saved_visual(parts, visual).get("visual", {})
        for entry in (body.get(_OBJECTS) or {}).get(object_name, []):
            same = selector is None or expr.same_selector(entry.get("selector"), dict(selector))
            if same and prop in entry.get("properties", {}):
                return [f"{object_name}.{prop} of {visual} is still set after the save"]
        return []

    return check


def _no_rule(visual: str, object_name: str, prop: str, metadata: str) -> Check:
    def check(parts: Mapping[str, Any]) -> list[str]:
        body = _saved_visual(parts, visual).get("visual", {})
        for entry in (body.get(_OBJECTS) or {}).get(object_name, []):
            value = entry.get("properties", {}).get(prop)
            if (entry.get("selector") or {}).get("metadata") != metadata or value is None:
                continue
            if expr.protected_kind(value) is not None:
                return [f"{object_name}.{prop} of {metadata} still holds conditional formatting"]
        return []

    return check


def _projection_is(visual: str, role: str, index: int, key: str, value: Any) -> Check:
    """The saved visual's ``role`` well has ``value`` at ``key`` of its ``index``-th field
    (``queryRef`` for the order, ``format`` for a visual-level format string)."""

    def check(parts: Mapping[str, Any]) -> list[str]:
        query = _saved_visual(parts, visual).get("visual", {}).get("query", {})
        projections = (query.get("queryState", {}).get(role) or {}).get("projections", [])
        found = projections[index].get(key) if index < len(projections) else None
        if found != value:
            return [f"field {index + 1} of {role} in {visual} has {key} {found!r}, not {value!r}"]
        return []

    return check


def _all(*checks: Check) -> Check:
    def check(parts: Mapping[str, Any]) -> list[str]:
        return [problem for one in checks for problem in one(parts)]

    return check


def _lit(text: str) -> dict[str, Any]:
    return {"expr": _literal(text)}


def _col(query_ref: str, option: int = 1) -> dict[str, Any]:
    return _wildcard(option, query_ref)


def _paint(inner: dict[str, Any]) -> dict[str, Any]:
    return {"solid": {"color": {"expr": inner}}}


def _m(name: str) -> dict[str, Any]:
    return expr.measure_ref("# Measures", name)


def _pct(source: dict[str, Any], kind: int, fraction: float) -> dict[str, Any]:
    return _comparison(kind, source, _range_percent(source, fraction))


# The agents table's columns. MTD Sales ('Table MTD Sales') is a text measure (it returns
# formatted text, "⋮" for the middle rows), so scales, rules on numbers and data bars go on
# Rank (a whole number) and the unnamed vs Previous Month column (a decimal number) instead.
_SALES = "# Measures.Table MTD Sales"
_RANK = "# Measures.Rank"
_VS_PREVIOUS = "# Measures.vs PM"  # 'vs Previous Month', shown with a blank header
_VS_PM = "# Measures.vs PM MOM%"
_NAME = "# Measures.Employee Name"
_WILD = {"data": [{"dataViewWildcard": {"matchingOption": 1}}]}
_FIRST_CLUSTER = {
    "Aggregation": {
        "Expression": expr.column_ref("Clustered Employees", "Clustered Employees"),
        "Function": 3,
    }
}
DESKTOP_PROBES: tuple[ProbeCase, ...] = (
    ProbeCase(
        "cf_gradient_table_background",
        FAMILY,
        (
            {
                "op": "set_conditional_format",
                "visual": _TABLE,
                "style": "gradient",
                "target": "background",
                "field": "Rank",
                "min_color": "#FFFFFF",
                "max_color": "#118DFF",
            },
        ),
        "The Rank cells shade from white (lowest rank number) to blue (highest); the other "
        "columns are unchanged.",
        check=_is(
            _TABLE,
            "values",
            "backColor",
            _col(_RANK),
            _paint(
                {
                    "FillRule": {
                        "Input": _m("Rank"),
                        "FillRule": {
                            "linearGradient2": {
                                "min": {"color": _literal("'#FFFFFF'")},
                                "max": {"color": _literal("'#118DFF'")},
                                "nullColoringStrategy": {"strategy": _literal("'asZero'")},
                            }
                        },
                    }
                }
            ),
        ),
    ),
    ProbeCase(
        "cf_gradient_three_colors_with_numbers_and_blank_color",
        FAMILY,
        (
            {
                "op": "set_conditional_format",
                "visual": _TABLE,
                "style": "gradient",
                "target": "background",
                "field": "vs PM %",
                "min_color": "#D64550",
                "mid_color": "#FFFFFF",
                "max_color": "#1AAB40",
                "min_value": -0.5,
                "mid_value": 0,
                "max_value": 0.5,
                "blank": "specific_color",
                "blank_color": "#C8C6C4",
            },
        ),
        "vs PM % cells are red below -50%, white at 0 and green from +50%; blank cells are grey.",
        check=_is(
            _TABLE,
            "values",
            "backColor",
            _col(_VS_PM),
            _paint(
                {
                    "FillRule": {
                        "Input": _m("vs PM MOM%"),
                        "FillRule": {
                            "linearGradient3": {
                                "min": {"color": _literal("'#D64550'"), "value": _literal("-0.5D")},
                                "mid": {"color": _literal("'#FFFFFF'"), "value": _literal("0D")},
                                "max": {"color": _literal("'#1AAB40'"), "value": _literal("0.5D")},
                                "nullColoringStrategy": {
                                    "strategy": _literal("'specificColor'"),
                                    "color": _literal("'#C8C6C4'"),
                                },
                            }
                        },
                    }
                }
            ),
        ),
    ),
    ProbeCase(
        "cf_rules_number_font_color",
        FAMILY,
        (
            {
                "op": "set_conditional_format",
                "visual": _TABLE,
                "style": "rules",
                "target": "font_color",
                "field": "vs PM %",
                "rules": [{"max": 0, "color": "#D64550"}, {"min": 0, "color": "#1AAB40"}],
                "replace_conditional": True,
            },
        ),
        "vs PM % text is red for negative changes and green for zero and up.",
        check=_is(
            _TABLE,
            "values",
            "fontColor",
            _col(_VS_PM),
            _paint(
                {
                    "Conditional": {
                        "Cases": [
                            {
                                "Condition": _comparison(_LESS, _m("vs PM MOM%"), _literal("0D")),
                                "Value": _literal("'#D64550'"),
                            },
                            {
                                "Condition": _comparison(
                                    _GREATER_EQUAL, _m("vs PM MOM%"), _literal("0D")
                                ),
                                "Value": _literal("'#1AAB40'"),
                            },
                        ]
                    }
                }
            ),
        ),
    ),
    ProbeCase(
        "cf_rules_percent_background",
        FAMILY,
        (
            {
                "op": "set_conditional_format",
                "visual": _TABLE,
                "style": "rules",
                "target": "background",
                "field": "Rank",
                "rules": [
                    {"min": 0, "max": 33, "unit": "percent", "color": "#F8696B"},
                    {"min": 33, "max": 67, "unit": "percent", "color": "#FFEB84"},
                    {
                        "min": 67,
                        "max": 100,
                        "max_inclusive": True,
                        "unit": "percent",
                        "color": "#63BE7B",
                    },
                ],
            },
        ),
        "Rank cells: the lowest third of the range red, the middle third yellow, the top third "
        "green.",
        check=_is(
            _TABLE,
            "values",
            "backColor",
            _col(_RANK),
            _paint(
                {
                    "Conditional": {
                        "Cases": [
                            {
                                "Condition": _pct(_m("Rank"), _LESS, 0.33),
                                "Value": _literal("'#F8696B'"),
                            },
                            {
                                "Condition": {
                                    "And": {
                                        "Left": _pct(_m("Rank"), _GREATER_EQUAL, 0.33),
                                        "Right": _pct(_m("Rank"), _LESS, 0.67),
                                    }
                                },
                                "Value": _literal("'#FFEB84'"),
                            },
                            {
                                "Condition": _pct(_m("Rank"), _GREATER_EQUAL, 0.67),
                                "Value": _literal("'#63BE7B'"),
                            },
                        ]
                    }
                }
            ),
        ),
    ),
    ProbeCase(
        "cf_field_value_font_color",
        FAMILY,
        (
            {
                "op": "set_conditional_format",
                "visual": _TABLE,
                "style": "field_value",
                "target": "font_color",
                "field": "Agent Name",
                "measure": _CF_MEASURE,
            },
        ),
        "Agent Name text takes the colors the CF Table measure returns (the same as Agent ID's).",
        check=_is(_TABLE, "values", "fontColor", _col(_NAME), _paint(_m("CF Table"))),
    ),
    ProbeCase(
        "cf_data_bars_numbers_and_direction",
        FAMILY,
        (
            {
                "op": "set_conditional_format",
                "visual": _TABLE,
                "style": "data_bars",
                "field": "Rank",
                "positive_color": "#118DFF",
                "negative_color": "#D64550",
                "axis_color": "#252423",
                "direction": "right_to_left",
                "min_value": 0,
                "bar_only": False,
            },
        ),
        "Rank shows blue bars growing from the right, starting at 0, with the numbers still "
        "shown. Rank is a whole number, so the minimum is written as one (0L): the check finds "
        "it kept, or reports how Desktop rewrote it.",
        check=_is(
            _TABLE,
            "columnFormatting",
            "dataBars",
            {"metadata": _RANK},
            {
                "minValue": _lit("0L"),
                "positiveColor": _paint(_literal("'#118DFF'")),
                "negativeColor": _paint(_literal("'#D64550'")),
                "axisColor": _paint(_literal("'#252423'")),
                "reverseDirection": _lit("true"),
                "hideText": _lit("false"),
            },
        ),
    ),
    ProbeCase(
        "cf_icons_set_right_middle",
        FAMILY,
        (
            {
                "op": "set_conditional_format",
                "visual": _TABLE,
                "style": "icons",
                "field": "vs PM %",
                "icon_set": "ThreeColoredArrows",
                "icon_position": "right",
                "icon_alignment": "middle",
            },
        ),
        "vs PM % shows a red down, yellow right or green up arrow right of each value, by thirds "
        "of the range.",
        check=_is(
            _TABLE,
            "values",
            "icon",
            _col(_VS_PM),
            {
                "kind": "Icon",
                "layout": _lit("'After'"),
                "verticalAlignment": _lit("'Middle'"),
                "value": {
                    "expr": {
                        "Conditional": {
                            "Cases": [
                                {
                                    "Condition": _pct(_m("vs PM MOM%"), _GREATER_EQUAL, 0.67),
                                    "Value": _literal("'ColoredArrowUp'"),
                                },
                                {
                                    "Condition": {
                                        "And": {
                                            "Left": _pct(_m("vs PM MOM%"), _GREATER_EQUAL, 0.33),
                                            "Right": _pct(_m("vs PM MOM%"), _LESS, 0.67),
                                        }
                                    },
                                    "Value": _literal("'ColoredArrowRight'"),
                                },
                                {
                                    "Condition": _pct(_m("vs PM MOM%"), _LESS, 0.33),
                                    "Value": _literal("'ColoredArrowDown'"),
                                },
                            ]
                        }
                    }
                },
            },
        ),
    ),
    ProbeCase(
        "cf_icons_rules_numbers",
        FAMILY,
        (
            {
                "op": "set_conditional_format",
                "visual": _TABLE,
                "style": "icons",
                "field": "Rank",
                "rules": [{"max": 4, "icon": "StarHigh"}, {"min": 4, "icon": "StarLow"}],
            },
        ),
        "The top 3 ranks show a full star left of the number, the others an empty one.",
        check=_is(
            _TABLE,
            "values",
            "icon",
            _col("# Measures.Rank"),
            {
                "kind": "Icon",
                "layout": _lit("'Before'"),
                "verticalAlignment": _lit("'Top'"),
                "value": {
                    "expr": {
                        "Conditional": {
                            "Cases": [
                                {
                                    "Condition": _comparison(_LESS, _m("Rank"), _literal("4D")),
                                    "Value": _literal("'StarHigh'"),
                                },
                                {
                                    "Condition": _comparison(
                                        _GREATER_EQUAL, _m("Rank"), _literal("4D")
                                    ),
                                    "Value": _literal("'StarLow'"),
                                },
                            ]
                        }
                    }
                },
            },
        ),
    ),
    ProbeCase(
        "cf_web_url_field_value",
        FAMILY,
        (
            {
                "op": "set_conditional_format",
                "visual": _TABLE,
                "style": "field_value",
                "target": "web_url",
                "field": "Agent Name",
                "measure": {
                    "table": "# Measures",
                    "name": "SVG 100% Stacked Bar Chart Employees Positive Change",
                },
            },
        ),
        "Agent Name cells become links (Power BI underlines them; the address is the measure's "
        "text).",
        check=_is(
            _TABLE,
            "values",
            "webURL",
            _col(_NAME),
            {"expr": _m("SVG 100% Stacked Bar Chart Employees Positive Change")},
        ),
    ),
    ProbeCase(
        "cf_chart_gradient",
        FAMILY,
        (
            {
                "op": "set_conditional_format",
                "visual": _CHART,
                "style": "gradient",
                "target": "data_colors",
                "min_color": "#C8C6C4",
                "max_color": "#12239E",
            },
        ),
        "The Total MTD Sales bars are colored by value from grey to dark blue; note whether the "
        "TopN/BottomN/Others colors still win.",
        check=_is(
            _CHART,
            "dataPoint",
            "fill",
            _WILD,
            _paint(
                {
                    "FillRule": {
                        "Input": _m("TopN/BottomN/ MTD Sales"),
                        "FillRule": {
                            "linearGradient2": {
                                "min": {"color": _literal("'#C8C6C4'")},
                                "max": {"color": _literal("'#12239E'")},
                                "nullColoringStrategy": {"strategy": _literal("'asZero'")},
                            }
                        },
                    }
                }
            ),
        ),
    ),
    ProbeCase(
        "cf_chart_rules",
        FAMILY,
        (
            {
                "op": "set_conditional_format",
                "visual": _CHART_2,
                "style": "rules",
                "target": "data_colors",
                "rules": [
                    {"min": 0, "max": 50000, "color": "#D64550"},
                    {"min": 50000, "color": "#1AAB40"},
                ],
            },
        ),
        "Avg MTD Sales by Agents: bars under 50,000 red, from 50,000 green.",
        check=_is(
            _CHART_2,
            "dataPoint",
            "fill",
            _WILD,
            _paint(
                {
                    "Conditional": {
                        "Cases": [
                            {
                                "Condition": {
                                    "And": {
                                        "Left": _comparison(
                                            _GREATER_EQUAL,
                                            _m("TopN/BottomN/ Avg MTD Sales"),
                                            _literal("0D"),
                                        ),
                                        "Right": _comparison(
                                            _LESS,
                                            _m("TopN/BottomN/ Avg MTD Sales"),
                                            _literal("50000D"),
                                        ),
                                    }
                                },
                                "Value": _literal("'#D64550'"),
                            },
                            {
                                "Condition": _comparison(
                                    _GREATER_EQUAL,
                                    _m("TopN/BottomN/ Avg MTD Sales"),
                                    _literal("50000D"),
                                ),
                                "Value": _literal("'#1AAB40'"),
                            },
                        ]
                    }
                }
            ),
        ),
    ),
    ProbeCase(
        "cf_card_value_field_value_and_rules",
        FAMILY,
        (
            {
                "op": "set_conditional_format",
                "visual": _CARD,
                "style": "field_value",
                "measure": _CF_MEASURE,
            },
            {
                "op": "set_conditional_format",
                "visual": _CARD_2,
                "style": "rules",
                "rules": [{"max": 100000, "color": "#D64550"}, {"min": 100000, "color": "#1AAB40"}],
            },
        ),
        "The Agents with Positive Change % value takes CF Table's color; Agents Avg MTD Sales is "
        "red under 100K, green from 100K.",
        check=_all(
            _is(
                _CARD,
                "value",
                "fontColor",
                _col("# Measures.Number of Employees with Positive Change %"),
                _paint(_m("CF Table")),
            ),
            _is(
                _CARD_2,
                "value",
                "fontColor",
                _col("# Measures.Employees Avg MTD Sales"),
                _paint(
                    {
                        "Conditional": {
                            "Cases": [
                                {
                                    "Condition": _comparison(
                                        _LESS, _m("Employees Avg MTD Sales"), _literal("100000D")
                                    ),
                                    "Value": _literal("'#D64550'"),
                                },
                                {
                                    "Condition": _comparison(
                                        _GREATER_EQUAL,
                                        _m("Employees Avg MTD Sales"),
                                        _literal("100000D"),
                                    ),
                                    "Value": _literal("'#1AAB40'"),
                                },
                            ]
                        }
                    }
                ),
            ),
        ),
    ),
    ProbeCase(
        "cf_title_and_background_field_value",
        FAMILY,
        (
            {
                "op": "set_conditional_format",
                "visual": _CHART_2,
                "style": "field_value",
                "target": "title_color",
                "measure": _CF_MEASURE,
            },
            {
                "op": "set_conditional_format",
                "visual": _CHART_2,
                "style": "field_value",
                "target": "visual_background",
                "measure": _CF_MEASURE,
            },
            {
                "op": "set_conditional_format",
                "visual": _CHART,
                "style": "field_value",
                "target": "title_text",
                "measure": {"table": "# Measures", "name": "Employee Name"},
            },
        ),
        "Avg MTD Sales by Agents: title and background take CF Table's color; Total MTD Sales' "
        "title shows the Employee Name measure's text.",
        check=_all(
            _is(_CHART_2, "title", "fontColor", None, _paint(_m("CF Table")), _CONTAINER),
            _is(_CHART_2, "background", "color", None, _paint(_m("CF Table")), _CONTAINER),
            _is(_CHART, "title", "text", None, {"expr": _m("Employee Name")}, _CONTAINER),
        ),
    ),
    ProbeCase(
        "cf_clear_desktop_authored_rule",
        FAMILY,
        (
            {
                "op": "clear_conditional_format",
                "visual": _TABLE,
                "target": "font_color",
                "field": "Rank",
            },
        ),
        "The Rank numbers turn plain black; the other columns keep their colors.",
        check=_no_rule(_TABLE, "values", "fontColor", "# Measures.Rank"),
    ),
    ProbeCase(
        "nf_data_labels_and_axis",
        FAMILY,
        (
            {
                "op": "set_number_format",
                "visual": _CHART,
                "target": "data_labels",
                "preset": "thousands",
                "decimals": 1,
            },
            {
                "op": "set_number_format",
                "visual": _CHART_2,
                "target": "value_axis",
                "display_units": "millions",
                "decimals": 2,
            },
            {
                "op": "set_number_format",
                "visual": _CHART_2,
                "target": "data_labels",
                "format": "#,0.0",
            },
        ),
        "Total MTD Sales labels read like 12.3K; Avg MTD Sales by Agents' axis reads 0.05M and "
        "its labels 12,345.6.",
        check=_all(
            _is(_CHART, "labels", "labelDisplayUnits", None, _lit("1000D")),
            _is(_CHART, "labels", "labelPrecision", None, _lit("1L")),
            _is(_CHART_2, "valueAxis", "labelDisplayUnits", None, _lit("1000000D")),
            _is(_CHART_2, "valueAxis", "labelPrecision", None, _lit("2L")),
            _is(_CHART_2, "labels", "labelDisplayUnits", None, _lit("-1D")),
            _is(_CHART_2, "labels", "valueCustomFormatString", None, _lit("'#,0.0'")),
        ),
    ),
    ProbeCase(
        "nf_card_and_table_columns",
        FAMILY,
        (
            {"op": "set_number_format", "visual": _CARD, "preset": "percent", "decimals": 1},
            {
                "op": "set_number_format",
                "visual": _TABLE,
                "field": "vs Previous Month",
                "display_units": "thousands",
                "decimals": 1,
            },
            {"op": "set_number_format", "visual": _TABLE, "field": "Rank", "format": "#,0"},
        ),
        "The card reads like 45.6%; the unnamed vs Previous Month column (the one with data "
        "bars) reads like 12.3K; Rank shows whole numbers only when visual-level format strings "
        "are on in Desktop's preview features (the check then finds Rank's format kept; without "
        "them Desktop drops it, which the check reports).",
        check=_all(
            _is(_CARD, "value", "labelDisplayUnits", {"id": "default"}, _lit("-1D")),
            _is(_CARD, "value", "customFormatString", {"id": "default"}, _lit("'0.0%'")),
            _is(
                _TABLE,
                "columnFormatting",
                "labelDisplayUnits",
                {"metadata": _VS_PREVIOUS},
                _lit("1000D"),
            ),
            _is(
                _TABLE,
                "columnFormatting",
                "labelPrecision",
                {"metadata": _VS_PREVIOUS},
                _lit("1L"),
            ),
            _projection_is(_TABLE, "Values", 2, "format", "#,0"),
        ),
    ),
    ProbeCase(
        "move_column",
        FAMILY,
        ({"op": "move_column", "visual": _TABLE, "field": "Rank", "to_index": 0},),
        "Rank is the first column of the agents table.",
        check=_projection_is(_TABLE, "Values", 0, "queryRef", "# Measures.Rank"),
    ),
    ProbeCase(
        "table_layout",
        FAMILY,
        ({"op": "set_table_layout", "visual": _TABLE, "totals": True},),
        "The agents table has a total row.",
        check=_is(_TABLE, "total", "totals", None, _lit("true")),
    ),
    ProbeCase(
        "matrix_layout",
        FAMILY,
        (
            {
                "op": "add_visual",
                "page": "1",
                "visual_type": "pivotTable",
                "fields": [
                    {"table": "Clustered Employees", "name": "Clustered Employees"},
                    {"table": "# Measures", "name": "TopN/BottomN/ MTD Sales"},
                ],
                "title": "Probe matrix",
            },
            {
                "op": "set_table_layout",
                "visual": "Probe matrix",
                "row_subtotals": False,
                "layout": "tabular",
                "values_on_rows": True,
            },
        ),
        "A new matrix 'Probe matrix' shows TopN/BottomN/Others rows in tabular layout, values on "
        "rows, with no row subtotals.",
        check=_all(
            _is("title:Probe matrix", "general", "layout", None, _lit("'Tabular'")),
            _is("title:Probe matrix", "subTotals", "rowSubtotals", None, _lit("false")),
            _is("title:Probe matrix", "values", "valuesOnRow", None, _lit("true")),
        ),
    ),
    ProbeCase(
        "cf_apply_to_totals",
        FAMILY,
        (
            {
                "op": "set_conditional_format",
                "visual": _TABLE,
                "style": "rules",
                "target": "background",
                "field": "vs Previous Month",
                "apply_to": "values_and_totals",
                "rules": [{"min": 0, "color": "#1AAB40"}],
            },
            {
                "op": "set_conditional_format",
                "visual": _TABLE,
                "style": "data_bars",
                "field": "vs PM %",
                "apply_to": "totals",
            },
            {"op": "set_table_layout", "visual": _TABLE, "totals": True},
        ),
        "The unnamed vs Previous Month column (the one with data bars): cells from 0 up are "
        "green, the total too; vs PM % shows a data bar in its total only.",
        check=_all(
            _is(
                _TABLE,
                "values",
                "backColor",
                _col(_VS_PREVIOUS, 0),
                _paint(
                    {
                        "Conditional": {
                            "Cases": [
                                {
                                    "Condition": _comparison(
                                        _GREATER_EQUAL, _m("vs Previous Month"), _literal("0D")
                                    ),
                                    "Value": _literal("'#1AAB40'"),
                                }
                            ]
                        }
                    }
                ),
            ),
            _is(
                _TABLE,
                "columnFormatting",
                "dataBars",
                {"metadata": _VS_PM},
                {
                    "positiveColor": _paint(_literal("'#118DFF'")),
                    "negativeColor": _paint(_literal("'#DEEFFF'")),
                    "axisColor": _paint(_literal("'#252423'")),
                    "reverseDirection": _lit("false"),
                    "hideText": _lit("false"),
                    "totalMatchingOption": _lit("2L"),
                },
            ),
        ),
    ),
    ProbeCase(
        "cf_text_and_blank_rules_based_on_a_column",
        FAMILY,
        (
            {
                "op": "set_conditional_format",
                "visual": _CHART_2,
                "style": "rules",
                "based_on": {"table": "Clustered Employees", "name": "Clustered Employees"},
                "rules": [
                    {"equals": "TopN", "color": "#1AAB40"},
                    {"blank": True, "color": "#C8C6C4"},
                    {"color": "#D64550"},
                ],
            },
        ),
        "Avg MTD Sales by Agents: the TopN bar green, the others red (the per-category colors "
        "may still win: note which); Desktop's rules dialog shows 'is TopN' and 'is blank'.",
        check=_is(
            _CHART_2,
            "dataPoint",
            "fill",
            _WILD,
            _paint(
                {
                    "Conditional": {
                        "Cases": [
                            {
                                "Condition": _text_comparison(
                                    _FIRST_CLUSTER, _literal("'TopN'"), _TEXT_EQUAL
                                ),
                                "Value": _literal("'#1AAB40'"),
                            },
                            {
                                "Condition": _text_comparison(
                                    _FIRST_CLUSTER, _literal("null"), _TEXT_IS_BLANK
                                ),
                                "Value": _literal("'#C8C6C4'"),
                            },
                            {"Condition": _literal("true"), "Value": _literal("'#D64550'")},
                        ]
                    }
                }
            ),
        ),
    ),
    ProbeCase(
        "cf_icons_from_a_field_value_only_the_icon",
        FAMILY,
        (
            {
                "op": "set_conditional_format",
                "visual": _TABLE,
                "style": "field_value",
                "target": "icon",
                "field": "Agent Name",
                "measure": {
                    "table": "# Measures",
                    "name": "SVG 100% Stacked Bar Chart Employees Positive Change",
                },
                "icon_position": "icon_only",
            },
        ),
        "Agent Name cells show only the picture the SVG measure returns, without the name.",
        check=_is(
            _TABLE,
            "values",
            "icon",
            _col(_NAME),
            {
                "kind": "Icon",
                "layout": _lit("'IconOnly'"),
                "verticalAlignment": _lit("'Top'"),
                "value": {"expr": _m("SVG 100% Stacked Bar Chart Employees Positive Change")},
            },
        ),
    ),
    ProbeCase(
        "cf_scale_based_on_a_summarized_column",
        FAMILY,
        (
            {
                "op": "set_conditional_format",
                "visual": _TABLE,
                "style": "gradient",
                "target": "background",
                "field": "Agent ID",
                "based_on": {"table": "DimEmployee", "name": "EmployeeKey"},
                "summarization": "max",
            },
        ),
        "Agent ID cells shade by the agent's key, lightest for the lowest.",
        check=_is(
            _TABLE,
            "values",
            "backColor",
            _col("DimEmployee.EmployeeKey"),
            _paint(
                {
                    "FillRule": {
                        "Input": {
                            "Aggregation": {
                                "Expression": expr.column_ref("DimEmployee", "EmployeeKey"),
                                "Function": 4,
                            }
                        },
                        "FillRule": {
                            "linearGradient2": {
                                "min": {"color": _literal("'#DEEFFF'")},
                                "max": {"color": _literal("'#118DFF'")},
                                "nullColoringStrategy": {"strategy": _literal("'asZero'")},
                            }
                        },
                    }
                }
            ),
        ),
    ),
    ProbeCase(
        "nf_general_reset",
        FAMILY,
        (
            {"op": "set_number_format", "visual": _CHART, "preset": "thousands", "decimals": 1},
            {"op": "set_number_format", "visual": _CHART, "preset": "general"},
            {"op": "set_number_format", "visual": _CARD, "format": "0.0%"},
            {"op": "set_number_format", "visual": _CARD, "preset": "general"},
        ),
        "Total MTD Sales labels and the Agents with Positive Change % card read as the measures' "
        "own formats again.",
        check=_all(
            _absent(_CHART, "labels", "labelDisplayUnits"),
            _absent(_CHART, "labels", "labelPrecision"),
            _absent(_CARD, "value", "customFormatString"),
            _absent(_CARD, "value", "labelDisplayUnits"),
        ),
    ),
)


__all__ = [
    "CONDITIONAL_HANDLERS",
    "CONDITIONAL_OPERATIONS",
    "DESKTOP_AUTHORED_SHAPES",
    "DESKTOP_PROBES",
    "FAMILY",
    "ICON_NAMES",
    "ICON_SETS",
    "QUERY_DRAWN",
    "TOOL_CARDS",
    "UNVERIFIED_SHAPES",
    "ClearConditionalFormat",
    "ConditionalOperation",
    "MoveColumn",
    "Rule",
    "SetConditionalFormat",
    "SetNumberFormat",
    "SetTableLayout",
    "apply_conditional",
    "conditional_family",
    "parse_conditional",
]
