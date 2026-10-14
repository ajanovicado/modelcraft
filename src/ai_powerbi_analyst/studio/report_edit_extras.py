"""The rest of what a Power BI developer edits on a report page: the Analytics pane, the phone
layout, groups, visual calculations, small multiples and accessibility settings (family
``extras``). The format painter and the visual header's icons are styling, which Studio does not
change: their operations were removed.

Every operation resolves its targets by caption or id (``report_edit_geometry``), works in page
pixels (a group member's stored position is relative to its group: measured in Desktop 2.157,
see "Measured decisions" in docs/imported-report-editing.md) and returns a plain-English
summary. What the canvas cannot draw is reported through ``VisibleIn``; the answer comes from
``report_tokens.property_is_rendered`` where a property decides it, so a renderer that learns to
draw analytics lines flips the receipts without a change here.

Evidence behind the shapes (``VERIFIED_SHAPES`` and ``UNVERIFIED_SHAPES`` list each one):

- Desktop-authored: the group container (exp6: Desktop opened a hand-built ``visualGroup`` with
  ``ScaleMode``, drew its members relative to its origin and saved it unchanged), a field renamed
  for one visual (``displayName`` and ``nativeQueryRef`` both hold the new name, ``queryRef``
  stays: the Agents table), ``tabOrder`` numbers, ``displayOption: FitToPage``,
  ``general.keepLayerOrder`` (an Agents card).
- Desktop 2.157's own shipped code (its Analytics pane writers and object serializer, read from
  the installed app): new line ids and names, the dynamic lines' ``Aggregation``/``Percentile``
  over a ``SelectRef``, the forecast's ``transform`` and the error bars' ``errorRange``; the
  bundled schemas: ``mobile.json`` (visualContainerMobileState 2.7.0).
- Microsoft's published capabilities and schemas only: the other Analytics pane properties,
  ``general.altText``, the ``NativeVisualCalculation`` field,
  ``smallMultiplesLayout`` and ``groupMode: ScrollMode``. None of these was seen in a file Desktop
  saved, so they are the first things the Desktop probes (``DESKTOP_PROBES``) check.

Lock objects is not here: no Desktop-authored file or published schema shows where Desktop stores
it, and a guessed key in report.json's settings could make Desktop refuse the whole report.
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from ai_powerbi_analyst.contracts import ClosedModel

from . import dax_syntax
from . import pbir_expr as expr
from .edit_tools import ProbeCase, Slot, ToolCard
from .pbir_document import DocumentError, PageDoc, PbirDocument
from .report_edit_context import (
    _DISPLAY_UNITS,
    Choice,
    EditOutcome,
    Handler,
    HandlerResult,
    ModelInfo,
    NeedsChoice,
    NeedsConfirmation,
    VisibleIn,
    _field_projection,
    _objects,
    _page_bounds,
    _set,
    _visual_type,
)
from .report_edit_geometry import (
    ancestors,
    backdrops,
    describe,
    is_group,
    join_names,
    leaf_boxes,
    page_box,
    resolve_leaf,
    resolve_page,
    resolve_visual,
    sentence,
    show,
    tidy,
    write_box,
)
from .report_edit_ops import ColorValue, FieldRef, _color_arg, operation_classes
from .report_refs import remove_visual_references
from .report_roles import (
    ROLE_TABLE,
    drop_field_references,
    field_identity,
    match_projection,
    projection_label,
    role_by_name,
    visual_type_name,
)
from .report_tokens import property_is_rendered

FAMILY = "extras"

# --- shared values ----------------------------------------------------------------------

LineStyle = Literal["solid", "dashed", "dotted"]
DisplayUnits = Literal["auto", "none", "thousands", "millions", "billions"]
AnalyticsKind = Literal[
    "constant",
    "x_constant",
    "min",
    "max",
    "average",
    "median",
    "percentile",
    "trend",
    "forecast",
    "error_bars",
    "ratio",
    "symmetry_shading",
]
GroupMode = Literal["scale", "scroll"]
CalcTemplate = Literal[
    "running_sum",
    "moving_average",
    "percent_of_parent",
    "percent_of_grand_total",
    "average_of_children",
    "versus_previous",
    "versus_next",
    "versus_parent",
    "versus_grand_total",
]
PageView = Literal["fit_to_page", "fit_to_width", "actual_size"]

# The phone canvas Studio lays visuals out on. The multipage fixture's hand-built mobile.json and
# Power BI's documented phone grid use 320 px; no Desktop-saved file confirms it yet.
MOBILE_WIDTH = 320
# The phone-layout part's schema: Desktop 2.157 ships visualContainerMobileState 1.5.0 and 2.7.0
# (its bundled schemas; the 2.x pattern accepts any 2.n.n), so a new mobile.json names 2.7.0.
MOBILE_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
    "visualContainerMobileState/2.7.0/schema.json"
)


# --- operations -------------------------------------------------------------------------


class SetAnalyticsLine(ClosedModel):
    """Add or change one line of the Analytics pane.

    ``kind`` picks the line. Without ``line`` (a name or the 1-based number among lines of that
    kind) and without ``new``, the one existing line of the kind (for ``measure``, when given) is
    changed, or a new one is added when there is none. ``measure`` is the value the line is
    based on: for min, max, average, median, percentile, forecast and error bars a field of the
    visual; for a constant line a measure of the model (the Analytics pane's fx), in which case
    ``value`` is left out. ``value`` is the constant (a date ``YYYY-MM-DD`` on a date X axis).
    ``upper`` and ``lower`` are the model measures error bars run between. A forecast takes
    ``forecast_length`` and ``ignore_last`` (points), ``seasonality`` (points; Power BI finds it
    when left out) and ``confidence`` (percent), the settings of Power BI's Forecast card.
    """

    op: Literal["set_analytics_line"] = "set_analytics_line"
    visual: str
    kind: AnalyticsKind
    line: str | int | None = None
    new: bool = False
    measure: str | None = Field(default=None, min_length=1, max_length=200)
    value: float | str | None = None
    percentile: float | None = Field(default=None, ge=0, le=100)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    show: bool | None = None
    color: ColorValue | None = None
    style: LineStyle | None = None
    width: float | None = Field(default=None, ge=0.5, le=20)
    transparency: float | None = Field(default=None, ge=0, le=100)
    position: Literal["front", "back"] | None = None
    data_label: bool | None = None
    label_text: Literal["value", "name", "value_and_name"] | None = None
    label_color: ColorValue | None = None
    label_horizontal: Literal["left", "right"] | None = None
    label_vertical: Literal["above", "under"] | None = None
    label_decimals: int | None = Field(default=None, ge=0, le=9)
    label_units: DisplayUnits | None = None
    shade: Literal["none", "before", "after"] | None = None
    shade_color: ColorValue | None = None
    shade_transparency: float | None = Field(default=None, ge=0, le=100)
    band: bool | None = None  # error bars: the shaded band between the bounds
    upper: str | None = Field(default=None, min_length=1, max_length=200)
    lower: str | None = Field(default=None, min_length=1, max_length=200)
    upper_color: ColorValue | None = None  # symmetry shading
    lower_color: ColorValue | None = None
    combine_series: bool | None = None  # trend line: one line for all series
    # The bounds are the Forecast card's own (Desktop 2.157: length 1..2e9, ignore 0..500).
    forecast_length: int | None = Field(default=None, ge=1, le=10000)
    ignore_last: int | None = Field(default=None, ge=0, le=500)
    seasonality: int | None = Field(default=None, ge=1, le=10000)
    confidence: Literal[75, 80, 85, 90, 95, 99] | None = None


class RemoveAnalyticsLine(ClosedModel):
    """Remove analytics lines: one by ``line`` (name or number), every line of ``kind``, or
    with ``all`` every analytics line on the visual."""

    op: Literal["remove_analytics_line"] = "remove_analytics_line"
    visual: str
    kind: AnalyticsKind | None = None
    line: str | int | None = None
    all: bool = False


class PlaceOnMobile(ClosedModel):
    """Put a visual on its page's phone layout, or move or resize it there. Positions are phone
    canvas pixels (320 wide); a value left out keeps what the visual has, and a visual that is
    not on the phone layout yet goes below the others at full width."""

    op: Literal["place_on_mobile"] = "place_on_mobile"
    visual: str
    x: float | None = Field(default=None, ge=0, le=MOBILE_WIDTH)
    y: float | None = Field(default=None, ge=0, le=20000)
    width: float | None = Field(default=None, ge=20, le=MOBILE_WIDTH)
    height: float | None = Field(default=None, ge=20, le=5000)


class RemoveFromMobile(ClosedModel):
    """Take visuals off the phone layout, or with ``all`` every visual of ``page`` (a page and no
    visuals asks first)."""

    op: Literal["remove_from_mobile"] = "remove_from_mobile"
    visuals: list[str] = Field(default_factory=list, max_length=200)
    page: str | None = None
    all: bool = False


class ArrangeMobileLayout(ClosedModel):
    """Lay a page's phone layout out again from scratch: the visuals in reading order of the
    page (or in the order of ``visuals``, the only ones then placed) on a grid of ``columns``;
    a visual wider than 40% of the page takes a whole row. Visuals not placed leave it."""

    op: Literal["arrange_mobile_layout"] = "arrange_mobile_layout"
    page: str
    columns: int = Field(default=2, ge=1, le=3)
    visuals: list[str] | None = Field(default=None, min_length=1, max_length=200)
    gap: float = Field(default=10, ge=0, le=100)


class GroupVisuals(ClosedModel):
    """Group visuals (or groups) that share a parent into a new group around them; nothing
    moves on the page. ``mode`` "scale" resizes the members with the group, "scroll" keeps
    their size and scrolls."""

    op: Literal["group_visuals"] = "group_visuals"
    visuals: list[str] = Field(min_length=2, max_length=100)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    mode: GroupMode = "scale"


class UngroupVisuals(ClosedModel):
    """Dissolve a group: its members stay where they are on the page. Ungrouping a group that
    bookmarks remember, or a hidden group (its members would show), asks first
    (``NeedsConfirmation("cleanup_references")``); ``cleanup_references`` is the yes."""

    op: Literal["ungroup_visuals"] = "ungroup_visuals"
    group: str
    cleanup_references: bool = False


class SetGroup(ClosedModel):
    """Rename a group or change how it resizes its members."""

    op: Literal["set_group"] = "set_group"
    group: str
    name: str | None = Field(default=None, min_length=1, max_length=100)
    mode: GroupMode | None = None


class SetVisualCalculation(ClosedModel):
    """Add a visual calculation (DAX that runs on the visual's own data), or change one.

    Give ``expression`` (fields in brackets by their name in the visual, like
    ``RUNNINGSUM([MTD Sales])``) or ``template`` with ``field`` (and ``window`` for a moving
    average). Without ``name`` a template's calculation is named the way Power BI names it
    ("Running sum"). ``new_name`` renames an existing one and updates the calculations that use
    it."""

    op: Literal["set_visual_calculation"] = "set_visual_calculation"
    visual: str
    name: str | None = Field(default=None, min_length=1, max_length=100)
    expression: str | None = Field(default=None, min_length=1, max_length=4000)
    template: CalcTemplate | None = None
    field: str | None = Field(default=None, min_length=1, max_length=200)
    window: int | None = Field(default=None, ge=2, le=100)
    new_name: str | None = Field(default=None, min_length=1, max_length=100)
    role: str | None = Field(default=None, min_length=1, max_length=40)

    @model_validator(mode="after")
    def says_what_to_do(self) -> SetVisualCalculation:
        if self.expression is None and self.template is None and self.new_name is None:
            raise ValueError(
                "give the calculation an expression or a template (with its field), or a new name"
            )
        return self


class RemoveVisualCalculation(ClosedModel):
    op: Literal["remove_visual_calculation"] = "remove_visual_calculation"
    visual: str
    name: str = Field(min_length=1, max_length=100)


class SetSmallMultiples(ClosedModel):
    """Split a chart into one small chart per value of ``field`` and set the grid; ``remove``
    turns small multiples off. Without ``field`` the grid of the existing split changes."""

    op: Literal["set_small_multiples"] = "set_small_multiples"
    visual: str
    field: FieldRef | None = None
    remove: bool = False
    rows: int | None = Field(default=None, ge=1, le=6)
    columns: int | None = Field(default=None, ge=1, le=6)
    shared_y_axis: bool | None = None
    titles: bool | None = None

    @model_validator(mode="after")
    def says_what_to_do(self) -> SetSmallMultiples:
        settings = (self.field, self.rows, self.columns, self.shared_y_axis, self.titles)
        if not self.remove and all(value is None for value in settings):
            raise ValueError(
                "say the column to split by, the grid, the shared axis or the titles, or remove"
            )
        return self


class SetAltText(ClosedModel):
    """The description a screen reader reads for a visual; empty or null removes it. ``text``
    is always given, so a request whose text was not read never removes the alt text."""

    op: Literal["set_alt_text"] = "set_alt_text"
    visual: str
    text: str | None = Field(max_length=1000)


class SetTabOrder(ClosedModel):
    """The order keyboard users reach the visuals of a page: ``visuals`` first, in that order,
    then the rest in their current order; ``by_position`` orders the page top to bottom, left to
    right (the listed visuals still go first)."""

    op: Literal["set_tab_order"] = "set_tab_order"
    visuals: list[str] = Field(default_factory=list, max_length=200)
    page: str | None = None
    by_position: bool = False


class SetKeepLayerOrder(ClosedModel):
    """ "Maintain layer order": the visual keeps its place in the stack when clicked in reading
    view instead of coming to the front."""

    op: Literal["set_keep_layer_order"] = "set_keep_layer_order"
    visuals: list[str] = Field(min_length=1, max_length=100)
    keep: bool = True


class SetPageView(ClosedModel):
    """How Power BI scales the page in the window (View > Page view)."""

    op: Literal["set_page_view"] = "set_page_view"
    page: str
    view: PageView


class SetFieldDisplayName(ClosedModel):
    """The name a field goes by in one visual: an axis, a legend entry, a table's column header
    (the Format pane's "Header text" sends this too). Null or empty resets it to the field's own
    name; a single space is a blank header, as Desktop stores one. ``name`` is always given, so
    a request whose new name was not read never resets one."""

    op: Literal["set_field_display_name"] = "set_field_display_name"
    visual: str
    field: str = Field(min_length=1, max_length=200)
    name: str | None = Field(max_length=200)


ExtrasOperation = (
    SetAnalyticsLine
    | RemoveAnalyticsLine
    | PlaceOnMobile
    | RemoveFromMobile
    | ArrangeMobileLayout
    | GroupVisuals
    | UngroupVisuals
    | SetGroup
    | SetVisualCalculation
    | RemoveVisualCalculation
    | SetSmallMultiples
    | SetAltText
    | SetTabOrder
    | SetKeepLayerOrder
    | SetPageView
    | SetFieldDisplayName
)


# --- small helpers ----------------------------------------------------------------------


def _name(visual: dict[str, Any]) -> str:
    return str(visual["name"])


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _number_text(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.4g}" if abs(value) < 1000 else f"{value:,.2f}"


def _type_word(visual_type: str) -> str:
    known = visual_type_name(visual_type)
    return known if known != visual_type else re.sub(r"(?<!^)(?=[A-Z])", " ", visual_type).lower()


def _catalog_objects(visual_type: str) -> Mapping[str, Any]:
    entry = expr.catalog()["visualTypes"].get(visual_type)
    objects = entry.get("objects") if isinstance(entry, dict) else None
    return objects if isinstance(objects, dict) else {}


def _encode(visual_type: str, object_name: str, prop: str, value: Any, existing: Any) -> Any:
    """The expression for one catalog property of a visual's own object."""
    spec = expr.property_spec(visual_type, object_name, prop)
    if spec is None:
        word = _type_word(visual_type)
        raise DocumentError(
            f"{_article(word).capitalize()} {word} has no {object_name}.{prop} setting"
        )
    return expr.encode_property(spec[1], prop, value, existing)


def _powerbi_only(op: str, detail: str) -> VisibleIn:
    return VisibleIn(op, "powerbi_only", detail)


def _state(visual: dict[str, Any]) -> dict[str, Any]:
    query = visual.get("visual", {}).get("query")
    state = query.get("queryState") if isinstance(query, dict) else None
    return state if isinstance(state, dict) else {}


def _role_projections(visual: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for role, bucket in _state(visual).items():
        for projection in bucket.get("projections", []) if isinstance(bucket, dict) else []:
            if isinstance(projection, dict):
                yield role, projection


def _selector(entry: dict[str, Any]) -> dict[str, Any]:
    selector = entry.get("selector")
    return selector if isinstance(selector, dict) else {}


def _query_refs(visual: dict[str, Any]) -> set[str]:
    return {
        str(p["queryRef"])
        for _, p in _role_projections(visual)
        if isinstance(p.get("queryRef"), str)
    }


def _calc_of(projection: dict[str, Any]) -> dict[str, Any] | None:
    field = projection.get("field")
    calc = field.get("NativeVisualCalculation") if isinstance(field, dict) else None
    return calc if isinstance(calc, dict) else None


def _role_kind(visual_type: str, role: str) -> str:
    entry = expr.catalog()["visualTypes"].get(visual_type)
    roles = entry.get("roles") if isinstance(entry, dict) else None
    spec = roles.get(role) if isinstance(roles, dict) else None
    return str(spec.get("kind", "")) if isinstance(spec, dict) else ""


def _is_value(visual_type: str, role: str, projection: dict[str, Any]) -> bool:
    """A projection that plots numbers: a measure, an aggregated column or a visual calculation
    in a role that takes values."""
    if _role_kind(visual_type, role) not in {"Measure", "GroupingOrMeasure"}:
        return False
    if _calc_of(projection) is not None:
        return True
    return field_identity(projection)[0] in {"Measure", "Aggregation"}


def _find_fields(visual: dict[str, Any], name: str) -> list[tuple[str, dict[str, Any]]]:
    wanted = name.strip().casefold()
    found: list[tuple[str, dict[str, Any]]] = []
    for role, projection in _role_projections(visual):
        calc = _calc_of(projection)
        if match_projection(projection, name) or (
            calc is not None and str(calc.get("Name", "")).casefold() == wanted
        ):
            found.append((role, projection))
    return found


def _field_list(visual: dict[str, Any]) -> str:
    labels = [projection_label(p) for _, p in _role_projections(visual)]
    return join_names([f"'{label}'" for label in labels if label.strip()], 8) or "none"


@dataclass(frozen=True)
class _Ref:
    """A field named by a user: ``Table.Name``, ``Table[Name]`` or a bare name."""

    table: str | None
    name: str


def _parse_ref(text: str) -> _Ref:
    cleaned = text.strip()
    bracket = re.fullmatch(r"'?([^'\[\]]+?)'?\s*\[([^\]]+)\]", cleaned)
    if bracket:
        return _Ref(bracket.group(1).strip(), bracket.group(2).strip())
    if cleaned.startswith("[") and cleaned.endswith("]"):
        return _Ref(None, cleaned[1:-1].strip())
    return _Ref(None, cleaned)


def _model_measure(model: ModelInfo, text: str) -> tuple[str, str]:
    """(table, measure) of a model measure named by the user."""
    ref = _parse_ref(text)
    try:
        kind, table, name = model.resolve(ref)
    except DocumentError as first:
        if ref.table is None and "." in ref.name:
            # "# Measures.Target", the way a queryRef spells it.
            table_part, _, name_part = ref.name.partition(".")
            try:
                kind, table, name = model.resolve(_Ref(table_part, name_part))
            except DocumentError:
                raise first from None
        else:
            raise
    if kind != "measure":
        raise DocumentError(f"{table}[{name}] is a column; this needs a measure")
    return table, name


# --- analytics lines ----------------------------------------------------------------------

_KIND_WORDS: dict[str, str] = {
    "constant": "constant line",
    "x_constant": "X-axis constant line",
    "min": "min line",
    "max": "max line",
    "average": "average line",
    "median": "median line",
    "percentile": "percentile line",
    "trend": "trend line",
    "forecast": "forecast",
    "error_bars": "error bars",
    "ratio": "ratio line",
    "symmetry_shading": "symmetry shading",
}
# How a receipt says a new one was added ("Added an average line ...").
_KIND_PHRASES: dict[str, str] = {
    "x_constant": "an X-axis constant line",
    "average": "an average line",
    "error_bars": "error bars",
    "symmetry_shading": "symmetry shading",
}
# The names the Analytics pane gives a new line: "{name} {n}" with the first n no line of the
# object has yet (Desktop 2.157's AnalyticsPane_New_Instance_Default_Format and its strings; a bar
# chart's axes are swapped, so its Y constant line is named for the X axis and the other way).
_DEFAULT_NAMES: dict[str, str] = {
    "constant": "Y-Axis Constant Line",
    "x_constant": "X-Axis Constant Line",
    "min": "Min line",
    "max": "Max line",
    "average": "Average line",
    "median": "Median line",
    "percentile": "Percentile line",
    "forecast": "Forecast",
    "trend": "Trend line",
    "ratio": "Ratio line",
    "symmetry_shading": "Symmetry shading",
}
_BAR_TYPES = frozenset({"barChart", "clusteredBarChart", "hundredPercentStackedBarChart"})
# A new forecast as Desktop 2.157 adds one (AnalyticsAuthoringService.addAnalyticsObject): the
# selector {id: "1", metadata: <the chart's value>}, then the Forecast transform with these
# parameters (integers L, the confidence a double D; MaxSeasonality only when set), show and
# displayName "Forecast 1". A transform parameter is saved as its expression plus a Name.
_FORECAST_DEFAULTS: dict[str, tuple[str, float]] = {
    "Unit": ("L", 7),
    "ForecastLength": ("L", 10),
    "IgnoreLast": ("L", 0),
    "ConfidenceLevel": ("D", 0.95),
}
# Desktop's default percentile for a new percentile line.
_DEFAULT_PERCENTILE = 90.0
# Aggregate function codes of semanticQuery 1.4.0 (QueryAggregateFunction).
_AGGREGATES: dict[str, int] = {"average": 1, "min": 3, "max": 4, "median": 6}
_AGGREGATE_KINDS = {code: kind for kind, code in _AGGREGATES.items()}
_DYNAMIC = frozenset({"min", "max", "average", "median", "percentile"})
_REFERENCE_OBJECTS = ("y1AxisReferenceLine", "xAxisReferenceLine", "referenceLine")
_SINGLE_OBJECTS: dict[str, str] = {
    "trend": "trend",
    "forecast": "forecast",
    "ratio": "ratioLine",
    "symmetry_shading": "plotAreaShading",
}
ANALYTICS_OBJECTS = frozenset(
    {
        *_REFERENCE_OBJECTS,
        "y2AxisReferenceLine",
        "error",
        "anomalyDetection",
        *_SINGLE_OBJECTS.values(),
    }
)

_LINE_PROPS: dict[str, str] = {
    "name": "displayName",
    "show": "show",
    "color": "lineColor",
    "style": "style",
    "width": "width",
    "transparency": "transparency",
    "position": "position",
}
_LABEL_PROPS: dict[str, str] = {
    "data_label": "dataLabelShow",
    "label_text": "dataLabelText",
    "label_color": "dataLabelColor",
    "label_horizontal": "dataLabelHorizontalPosition",
    "label_vertical": "dataLabelVerticalPosition",
    "label_decimals": "dataLabelDecimalPoints",
    "label_units": "dataLabelDisplayUnits",
}
_SHADE_PROPS: dict[str, str] = {
    "shade_color": "shadeColor",
    "shade_transparency": "shadeTransparency",
}
# Which operation field lands in which property, per object. A field the object has no
# property for is refused by name ("a trend line has no data label").
_OBJECT_PROPS: dict[str, dict[str, str]] = {
    "y1AxisReferenceLine": {**_LINE_PROPS, **_LABEL_PROPS, **_SHADE_PROPS},
    "xAxisReferenceLine": {**_LINE_PROPS, **_LABEL_PROPS, **_SHADE_PROPS},
    "referenceLine": {**_LINE_PROPS, **_LABEL_PROPS, **_SHADE_PROPS},
    "trend": {
        **{k: v for k, v in _LINE_PROPS.items() if k != "position"},
        "combine_series": "combineSeries",
    },
    "forecast": {
        **{k: v for k, v in _LINE_PROPS.items() if k not in {"position", "transparency"}},
        "transparency": "strokeTransparency",
    },
    "ratioLine": {k: v for k, v in _LINE_PROPS.items() if k != "position"},
    "plotAreaShading": {
        "name": "displayName",
        "show": "show",
        "transparency": "transparency",
        "upper_color": "upperShadingColor",
        "lower_color": "lowerShadingColor",
    },
    "error": {
        "show": "enabled",
        "color": "barColor",
        "width": "barWidth",
        "data_label": "labelShow",
        "band": "shadeShow",
        "shade_color": "shadeColor",
        "shade_transparency": "shadeTransparency",
    },
}
_FIELD_WORDS: dict[str, str] = {
    "name": "name",
    "show": "on/off switch",
    "color": "color",
    "style": "line style",
    "width": "width",
    "transparency": "transparency",
    "position": "layer position",
    "data_label": "data label",
    "label_text": "data label text",
    "label_color": "data label color",
    "label_horizontal": "data label position",
    "label_vertical": "data label position",
    "label_decimals": "data label decimals",
    "label_units": "data label units",
    "shade": "shading",
    "shade_color": "shading color",
    "shade_transparency": "shading transparency",
    "band": "band",
    "upper_color": "upper shading color",
    "lower_color": "lower shading color",
    "combine_series": "combine series switch",
}
_LABEL_TEXT = {"value": "Value", "name": "Name", "value_and_name": "ValueAndName"}
_COLOR_FIELDS = frozenset({"color", "label_color", "shade_color", "upper_color", "lower_color"})


@dataclass(frozen=True)
class AnalyticsLine:
    """One line of a visual's Analytics pane, as Studio reads it back."""

    kind: str
    object: str
    index: int  # the entry's position in the object's list
    number: int  # 1-based among the lines of its kind
    name: str | None
    measure: str | None  # the queryRef a dynamic line, forecast or error bars is based on
    value: Any
    shown: bool

    @property
    def label(self) -> str:
        if self.name:
            return f"'{self.name}'"
        return f"{_KIND_WORDS[self.kind]} {self.number}"


def _select_ref(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    ref = node.get("SelectRef")
    name = ref.get("ExpressionName") if isinstance(ref, dict) else None
    return name if isinstance(name, str) else None


def _line_kind(object_name: str, value: Any) -> tuple[str, str | None]:
    """(kind, measure queryRef) of a reference line entry from its value expression."""
    if object_name == "y1AxisReferenceLine":
        return "constant", None
    if object_name == "xAxisReferenceLine":
        return "x_constant", None
    inner = value.get("expr") if isinstance(value, dict) else None
    if isinstance(inner, dict):
        aggregation = inner.get("Aggregation")
        if isinstance(aggregation, dict):
            kind = _AGGREGATE_KINDS.get(aggregation.get("Function", -1))
            if kind is not None:
                return kind, _select_ref(aggregation.get("Expression"))
        percentile = inner.get("Percentile")
        if isinstance(percentile, dict):
            return "percentile", _select_ref(percentile.get("Expression"))
    return "constant", None


def analytics_lines(visual: dict[str, Any]) -> list[AnalyticsLine]:
    """Every analytics line of a visual, in the order the Analytics pane lists them."""
    objects = visual.get("visual", {}).get("objects", {})
    if not isinstance(objects, dict):
        return []
    found: list[AnalyticsLine] = []
    counts: dict[str, int] = {}
    order = [*_REFERENCE_OBJECTS, "error", *_SINGLE_OBJECTS.values()]
    single_kinds = {obj: kind for kind, obj in _SINGLE_OBJECTS.items()}
    for object_name in order:
        entries = objects.get(object_name)
        for index, entry in enumerate(entries if isinstance(entries, list) else []):
            if not isinstance(entry, dict):
                continue
            props = entry.get("properties", {})
            props = props if isinstance(props, dict) else {}
            selector = _selector(entry)
            measure = (
                selector.get("metadata") if isinstance(selector.get("metadata"), str) else None
            )
            if object_name in _REFERENCE_OBJECTS:
                kind, from_value = _line_kind(object_name, props.get("value"))
                measure = from_value or (measure if kind in _DYNAMIC else None)
            elif object_name == "error":
                kind = "error_bars"
            else:
                kind = single_kinds[object_name]
            counts[kind] = counts.get(kind, 0) + 1
            name = expr.decode(props.get("displayName"))
            shown_key = "enabled" if object_name == "error" else "show"
            found.append(
                AnalyticsLine(
                    kind,
                    object_name,
                    index,
                    counts[kind],
                    name if isinstance(name, str) and name.strip() else None,
                    measure,
                    expr.decode(props.get("value")),
                    expr.decode(props.get(shown_key)) is not False,
                )
            )
    return found


def analytics_kinds(visual_type: str) -> list[str]:
    """The analytics lines Power BI's Analytics pane offers for a visual type (the Format pane
    and the planner show only these)."""
    objects = _catalog_objects(visual_type)
    kinds = []
    for kind in _KIND_WORDS:
        try:
            _object_for(visual_type, kind, objects)
        except DocumentError:
            continue
        kinds.append(kind)
    return kinds


def _object_for(visual_type: str, kind: str, objects: Mapping[str, Any] | None = None) -> str:
    """The object a new line of ``kind`` goes in for this visual type."""
    objects = _catalog_objects(visual_type) if objects is None else objects
    if kind == "constant":
        candidates = ["y1AxisReferenceLine", "referenceLine"]
    elif kind == "x_constant":
        candidates = ["xAxisReferenceLine"]
    elif kind in _DYNAMIC:
        candidates = ["referenceLine"]
    elif kind == "error_bars":
        candidates = ["error"]
    else:
        candidates = [_SINGLE_OBJECTS[kind]]
    for candidate in candidates:
        if candidate in objects:
            return candidate
    word = _type_word(visual_type)
    offered = [
        _type_word(t)
        for t in (
            "clusteredColumnChart",
            "clusteredBarChart",
            "lineChart",
            "areaChart",
            "scatterChart",
        )
        if t != visual_type and any(c in _catalog_objects(t) for c in candidates)
    ]
    hint = f" Visuals that have one: {join_names(offered, 5)}." if offered else ""
    raise DocumentError(
        f"{_article(word).capitalize()} {word} has no {_KIND_WORDS[kind]} in Power BI's Analytics "
        f"pane.{hint}"
    )


def _value_measures(visual: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    visual_type = _visual_type(visual)
    return [(r, p) for r, p in _role_projections(visual) if _is_value(visual_type, r, p)]


def _series(visual: dict[str, Any], op: SetAnalyticsLine, word: str) -> str:
    """The queryRef of the visual field a dynamic line, forecast or error bars is based on."""
    measures = _value_measures(visual)
    if op.measure is not None:
        found = [p for _, p in _find_fields(visual, op.measure) if any(p is q for _, q in measures)]
        if not found:
            names = join_names([f"'{projection_label(p)}'" for _, p in measures], 6) or "none"
            raise DocumentError(
                f"The {word} needs a value the visual shows, and {op.measure!r} is not one. "
                f"Its values: {names}."
            )
        return str(found[0]["queryRef"])
    unique = {str(p["queryRef"]): p for _, p in measures if isinstance(p.get("queryRef"), str)}
    if len(unique) == 1:
        return next(iter(unique))
    if not unique:
        raise DocumentError(f"The {word} needs a value in the visual, and it has none")
    raise NeedsChoice(
        f"Which value should the {word} follow?",
        [Choice(projection_label(p), {"measure": projection_label(p)}) for p in unique.values()],
    )


def _next_id(entries: list[dict[str, Any]]) -> str:
    """Desktop numbers a new instance one above the highest numeric id of the object, from 1."""
    taken = [
        int(str(selector["id"]))
        for entry in entries
        if isinstance(selector := entry.get("selector"), dict)
        and str(selector.get("id", "")).isdigit()
    ]
    return str(max(taken, default=0) + 1)


def _default_name(kind: str, visual_type: str, entries: list[dict[str, Any]]) -> str:
    """The name Desktop gives a new line: its kind's name and the first number no line of the
    object already has ("Average line 2" after "Average line 1", "Average line 1" again once
    it was removed)."""
    base = _DEFAULT_NAMES[kind]
    if visual_type in _BAR_TYPES and kind in {"constant", "x_constant"}:
        base = _DEFAULT_NAMES["x_constant" if kind == "constant" else "constant"]
    names = {expr.decode((e.get("properties") or {}).get("displayName")) for e in entries}
    number = 1
    while f"{base} {number}" in names:
        number += 1
    return f"{base} {number}"


def _line_choices(lines: list[AnalyticsLine], with_kind: bool = False) -> list[Choice]:
    """One answer per line; a line whose name another shares is picked by its number."""
    names = [line.name for line in lines]
    choices = []
    for line in lines:
        pick: dict[str, Any] = {
            "line": line.name if line.name and names.count(line.name) == 1 else line.number
        }
        if with_kind:
            pick["kind"] = line.kind
        label = line.name if line.name else line.label
        if line.name and names.count(line.name) > 1:
            label = f"{line.name} ({_KIND_WORDS[line.kind]} {line.number})"
        choices.append(Choice(label, pick))
    return choices


def _pick_line(lines: list[AnalyticsLine], ref: str | int, word: str, label: str) -> AnalyticsLine:
    if isinstance(ref, int):
        for line in lines:
            if line.number == ref:
                return line
    else:
        wanted = ref.strip().casefold()
        named = [line for line in lines if (line.name or "").casefold() == wanted]
        if len(named) == 1:
            return named[0]
        if len(named) > 1:
            raise NeedsChoice(
                f"{sentence(label)} has {len(named)} lines called {ref!r}. Which one?",
                _line_choices(named, with_kind=len({line.kind for line in named}) > 1),
            )
        if wanted.isdecimal():
            return _pick_line(lines, int(wanted), word, label)
    listed = join_names([line.label for line in lines], 6)
    where = f" It has {listed}." if lines else ""
    raise DocumentError(f"{sentence(label)} has no {word} {ref!r}.{where}")


def _matching(lines: list[AnalyticsLine], kind: str) -> list[AnalyticsLine]:
    return [line for line in lines if line.kind == kind]


def _line_value(op: SetAnalyticsLine, model: ModelInfo, object_name: str) -> Any:
    """The ``value`` expression of a constant line: a number, a date, or a model measure."""
    if op.measure is not None and op.value is None:
        table, name = _model_measure(model, op.measure)
        return {"expr": expr.measure_ref(table, name)}
    if op.value is None:
        raise DocumentError(f"Say where the {_KIND_WORDS[op.kind]} goes (a value)")
    if isinstance(op.value, str):
        text = op.value.strip()
        dates = object_name == "xAxisReferenceLine"
        if dates and re.fullmatch(r"\d{4}-\d{2}-\d{2}(T[\d:.]+)?", text):
            return {"expr": {"Literal": {"Value": expr.data_literal(text, "datetime")}}}
        if not re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?|-?\d+(\.\d+)?", text):
            raise DocumentError(
                f"{op.value!r} is not a number" + (" or a date (YYYY-MM-DD)" if dates else "")
            )
        return expr.double(float(text.replace(",", "")))
    return expr.double(op.value)


def _check_x_axis(visual: dict[str, Any], model: ModelInfo, value: Any) -> None:
    """An X-axis constant line needs a continuous axis of the value's kind: Power BI offers it
    only on a number or date axis (a scatter's X is always a number)."""
    axis = next(
        (p for role, p in _role_projections(visual) if role == "Category"),
        None,
    )
    if axis is None or _visual_type(visual) == "scatterChart":
        return
    identity = field_identity(axis)
    if identity[0] != "Column":
        return
    kind = expr.data_type_name(model.data_type(identity[1], identity[2]))
    literal = _dig(value, "expr", "Literal", "Value")
    is_date = isinstance(literal, str) and literal.startswith("datetime'")
    label = projection_label(axis)
    if kind == "text":
        raise DocumentError(
            f"An X-axis constant line needs a number or date axis, and this chart's axis "
            f"'{label}' is text"
        )
    if kind in {"datetime", "date"} and not is_date:
        raise DocumentError(f"The axis '{label}' holds dates: give the line a date (YYYY-MM-DD)")
    if kind in {"integer", "double", "decimal"} and is_date:
        raise DocumentError(f"The axis '{label}' holds numbers: give the line a number")


def _dynamic_value(kind: str, query_ref: str, percentile: float | None) -> dict[str, Any]:
    """A dynamic line's value as Desktop's Analytics pane writes it: an aggregate (or a
    percentile, K from 0 to 1, Exclusive left out as Desktop does when it is false) of the
    chart's value, named by its queryRef."""
    select = {"SelectRef": {"ExpressionName": query_ref}}
    if kind == "percentile":
        wanted = _DEFAULT_PERCENTILE if percentile is None else percentile
        # Rounded so 33.3 is written 0.333, not the float quotient 0.33299999999999996.
        k = round(wanted / 100, 12)
        return {"expr": {"Percentile": {"Expression": select, "K": k}}}
    return {"expr": {"Aggregation": {"Expression": select, "Function": _AGGREGATES[kind]}}}


def _error_range(model: ModelInfo, upper: str, lower: str) -> dict[str, Any]:
    """Absolute bounds as Desktop 2.157 saves an error range (its StructuralObjectValue
    serializer): the kind, then an explicit range whose parts are each ``{expr}``: isRelative
    false, the lower bound, the upper bound."""
    upper_table, upper_name = _model_measure(model, upper)
    lower_table, lower_name = _model_measure(model, lower)
    return {
        "kind": "ErrorRange",
        "explicit": {
            "isRelative": {"expr": {"Literal": {"Value": "false"}}},
            "lowerBound": {"expr": expr.measure_ref(lower_table, lower_name)},
            "upperBound": {"expr": expr.measure_ref(upper_table, upper_name)},
        },
    }


def _forecast_series(visual: dict[str, Any], model: ModelInfo, op: SetAnalyticsLine) -> str:
    """The one value a forecast extends. Desktop offers a forecast only on a line chart with
    one value, no legend and a date or number axis (AnalyticsAuthoringService), so anything
    else is refused rather than asked about."""
    measures = _value_measures(visual)
    unique = {str(p["queryRef"]): p for _, p in measures if isinstance(p.get("queryRef"), str)}
    if len(unique) != 1:
        shown = join_names([f"'{projection_label(p)}'" for p in unique.values()], 4) or "none"
        raise DocumentError(
            f"Power BI forecasts a line chart with exactly one value; this one has {len(unique)} "
            f"({shown}). Keep one value in the chart to add a forecast"
        )
    if any(role == "Series" for role, _ in _role_projections(visual)):
        raise DocumentError("Power BI cannot forecast a line chart that has a legend")
    axis = next((p for role, p in _role_projections(visual) if role == "Category"), None)
    identity = field_identity(axis) if axis is not None else ("Unknown",)
    if identity[0] == "Column":
        kind = expr.data_type_name(model.data_type(identity[1], identity[2]))
        if kind == "text":
            raise DocumentError(
                f"Power BI forecasts along a date or number axis, and this chart's axis "
                f"'{projection_label(axis or {})}' is text"
            )
    query_ref = next(iter(unique))
    if op.measure is not None and query_ref != _series(visual, op, "forecast"):
        raise DocumentError("A forecast extends the chart's one value")
    return query_ref


def _forecast_transform(op: SetAnalyticsLine, current: Any) -> dict[str, Any]:
    """The Forecast transform: Desktop's defaults, the settings the file already has, then the
    ones asked for. Each parameter is saved as its literal plus a Name, in Desktop's order."""
    values: dict[str, str] = {
        name: f"{_plain_number(default)}{suffix}"
        for name, (suffix, default) in _FORECAST_DEFAULTS.items()
    }
    parameters = current.get("parameters") if isinstance(current, dict) else None
    for parameter in parameters if isinstance(parameters, list) else []:
        literal = _dig(parameter, "Literal", "Value")
        if isinstance(parameter, dict) and isinstance(parameter.get("Name"), str) and literal:
            values[parameter["Name"]] = str(literal)
    asked = {
        "ForecastLength": (op.forecast_length, "L"),
        "IgnoreLast": (op.ignore_last, "L"),
        "MaxSeasonality": (op.seasonality, "L"),
        "ConfidenceLevel": (None if op.confidence is None else op.confidence / 100, "D"),
    }
    for name, (value, suffix) in asked.items():
        if value is not None:
            values[name] = f"{_plain_number(value)}{suffix}"
    order = [*_FORECAST_DEFAULTS, "MaxSeasonality"]
    names = [n for n in order if n in values] + [n for n in values if n not in order]
    return {
        "algorithm": "Forecast",
        "parameters": [{"Literal": {"Value": values[n]}, "Name": n} for n in names],
    }


def _plain_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _style_values(op: SetAnalyticsLine) -> dict[str, Any]:
    """The operation's styling fields that were given, as catalog values."""
    given: dict[str, Any] = {}
    for key in _FIELD_WORDS:
        value = getattr(op, key, None)
        if value is None or key == "shade":
            continue
        if key in _COLOR_FIELDS:
            value = _color_arg(value)
        elif key == "label_text":
            value = _LABEL_TEXT[str(value)]
        elif key == "label_units":
            value = _DISPLAY_UNITS[str(value)]
        given[key] = value
    return given


def _has(word: str) -> str:
    """ "has", or "have" for the plural "error bars"."""
    return "have" if word.endswith("bars") else "has"


def _write_line_props(
    visual_type: str, object_name: str, entry: dict[str, Any], op: SetAnalyticsLine
) -> list[str]:
    """Write the styling fields into the entry; returns the words of what was set."""
    props = entry.setdefault("properties", {})
    mapping = _OBJECT_PROPS[object_name]
    word = _KIND_WORDS[op.kind]
    writes: list[tuple[str, str, Any]] = []  # (field word, property, value)
    for key, value in _style_values(op).items():
        prop = mapping.get(key)
        if prop is None:
            raise DocumentError(
                f"The {word} {_has(word)} no {_FIELD_WORDS[key]} setting in Power BI"
            )
        writes.append((_FIELD_WORDS[key], prop, value))
    if op.shade is not None:
        if object_name not in _REFERENCE_OBJECTS:
            band = "; error bars have a band instead" if object_name == "error" else ""
            raise DocumentError(f"The {word} {_has(word)} no shading setting in Power BI{band}")
        writes.append(("shading", "shadeShow", op.shade != "none"))
        if op.shade != "none":
            writes.append(("shading", "shadeRegion", op.shade))
    if object_name == "error":
        # Error bars take the series' color while "match series color" is on, so a color given
        # for them turns it off, or the color would never show.
        if op.color is not None:
            writes.append(("color", "barMatchSeriesColor", False))
        if op.shade_color is not None:
            writes.append(("shading color", "shadeMatchSeriesColor", False))
    for field_word, prop, value in writes:
        if expr.property_spec(visual_type, object_name, prop) is None:
            raise DocumentError(
                f"The {word} of this visual {_has(word)} no {field_word} setting in Power BI"
            )
        props[prop] = _encode(visual_type, object_name, prop, value, props.get(prop))
    return list(dict.fromkeys(field_word for field_word, _, _ in writes))


def _property_word(object_name: str, prop: str) -> str:
    """What a user calls a line's property ("line style"), never its name in the file."""
    for field_name, candidate in _OBJECT_PROPS.get(object_name, {}).items():
        if candidate == prop:
            return _FIELD_WORDS[field_name]
    return {"shadeShow": "shading", "shadeRegion": "shading", "value": "position"}.get(
        prop, re.sub(r"(?<!^)(?=[A-Z])", " ", prop).lower()
    )


def _visible(
    op: str, visual_type: str, object_name: str, word: str, entry: dict[str, Any]
) -> list[VisibleIn]:
    """Where the line shows: nowhere in Studio when its object is not drawn, else only the
    properties the canvas does not draw (a reference line's shading) are Power BI only."""
    shown = "enabled" if object_name == "error" else "show"
    if not property_is_rendered(visual_type, object_name, shown, container=False):
        return [_powerbi_only(op, f"Studio's canvas does not draw {word} yet; Power BI shows it.")]
    unseen = sorted(
        {
            _property_word(object_name, prop)
            for prop in entry.get("properties", {})
            if prop != "errorRange"
            and not property_is_rendered(visual_type, object_name, prop, container=False)
        }
    )
    if not unseen:
        return []
    return [
        _powerbi_only(
            op, f"Studio draws the {word}; its {join_names(unseen, 4)} show only in Power BI."
        )
    ]


def _apply_set_analytics_line(
    document: PbirDocument, op: SetAnalyticsLine, model: ModelInfo
) -> HandlerResult:
    page, visual = resolve_leaf(document, op.visual)
    label = describe(visual, page)
    visual_type = _visual_type(visual)
    word = _KIND_WORDS[op.kind]
    object_name = _object_for(visual_type, op.kind)
    existing = _matching(analytics_lines(visual), op.kind)
    target: AnalyticsLine | None = None
    if op.line is not None:
        target = _pick_line(existing, op.line, word, label)
    elif op.kind in _SINGLE_OBJECTS:
        # A visual has one of these: "add" on a visual that has it changes it.
        target = existing[0] if existing else None
    elif not op.new:
        pool = existing
        if op.kind in _DYNAMIC | {"error_bars"} and op.measure is not None:
            query_ref = _series(visual, op, word)
            pool = [line for line in existing if line.measure == query_ref]
        if len(pool) == 1:
            target = pool[0]
        elif len(pool) > 1:
            raise NeedsChoice(
                f"{sentence(label)} has {len(pool)} {word}s. Which one?",
                _line_choices(pool) + [Choice(f"Add another {word}", {"new": True})],
            )
    objects = _objects(visual, "objects")
    before: dict[str, Any] | None = None
    moved: list[str] = []
    if target is None:
        entry = _new_line(visual, op, model, object_name, existing)
    else:
        entry = objects[target.object][target.index]
        before = copy.deepcopy(entry)
        object_name = target.object
        moved = _update_line(visual, op, model, object_name, entry, target)
    styled = list(dict.fromkeys(_write_line_props(visual_type, object_name, entry, op)))
    if target is None:
        objects.setdefault(object_name, []).append(entry)
    name = expr.decode(entry.get("properties", {}).get("displayName"))
    called = f" '{name}'" if isinstance(name, str) and name.strip() else ""
    if before is not None and before == entry:
        return HandlerResult(f"The {word}{called} on {label} was already set that way.")
    document.touch_visual(page, _name(visual))
    if before is None:
        detail = _line_detail(op, entry)
        summary = f"Added {_KIND_PHRASES.get(op.kind, 'a ' + word)}{called}{detail} to {label}."
    else:
        # "…: color and style." / "…: moved it to 7 and changed its color."
        said = list(dict.fromkeys(moved))
        if said and styled:
            said.append(f"changed its {join_names(styled, 6)}")
        what = f": {join_names(said or styled, 6)}" if said or styled else ""
        summary = f"Changed the {word}{called} on {label}{what}."
    warnings = []
    if object_name == "error":
        warnings.append(
            "Error bars are not confirmed yet: Studio writes them the way Power BI Desktop's own "
            "code saves them, but has not seen Desktop keep them in a file. Check them in Power "
            "BI Desktop."
        )
    return HandlerResult(
        summary,
        warnings=warnings,
        visible_in=_visible(op.op, visual_type, object_name, word, entry),
    )


def _line_detail(op: SetAnalyticsLine, entry: dict[str, Any]) -> str:
    if op.kind in {"constant", "x_constant"}:
        return _position_words(entry)
    if op.kind == "percentile":
        k = _dig(entry, "properties", "value", "expr", "Percentile", "K")
        if isinstance(k, int | float) and not isinstance(k, bool):
            return f" at the {_number_text(round(float(k) * 100, 10))}th percentile"
    return ""


def _position_words(entry: dict[str, Any], preposition: str = "at") -> str:
    """ " at 50,000", " at 2019-06-30" or " from the measure 'Target'": where a constant line
    is (``preposition`` "to" for a line that moved)."""
    decoded = expr.decode(entry["properties"].get("value"))
    if isinstance(decoded, int | float) and not isinstance(decoded, bool):
        return f" {preposition} {_number_text(float(decoded))}"
    if hasattr(decoded, "strftime"):
        return f" {preposition} {decoded:%Y-%m-%d}"
    measure = _dig(entry, "properties", "value", "expr", "Measure", "Property")
    return f" from the measure '{measure}'" if isinstance(measure, str) else ""


def _new_line(
    visual: dict[str, Any],
    op: SetAnalyticsLine,
    model: ModelInfo,
    object_name: str,
    existing: list[AnalyticsLine],
) -> dict[str, Any]:
    word = _KIND_WORDS[op.kind]
    entries = _objects(visual, "objects").get(object_name, [])
    visual_type = _visual_type(visual)
    props: dict[str, Any] = {}
    selector: dict[str, Any] | None = None
    if op.kind in {"constant", "x_constant"}:
        props["value"] = _line_value(op, model, object_name)
        if object_name == "xAxisReferenceLine":
            _check_x_axis(visual, model, props["value"])
        selector = {"id": _next_id(entries)}
    elif op.kind in _DYNAMIC:
        query_ref = _series(visual, op, word)
        props["value"] = _dynamic_value(op.kind, query_ref, op.percentile)
        selector = {"metadata": query_ref, "id": _next_id(entries)}
    elif op.kind == "error_bars":
        query_ref = _series(visual, op, word)
        if op.upper is None or op.lower is None:
            raise DocumentError(
                "Error bars need the two measures they run between: say the upper and the lower "
                "bound"
            )
        props["errorRange"] = _error_range(model, op.upper, op.lower)
        selector = {"metadata": query_ref}
        if any(line.measure == query_ref for line in existing):
            raise DocumentError("That value already has error bars; change them instead")
    elif op.kind == "forecast":
        props["transform"] = _forecast_transform(op, None)
        selector = {"metadata": _forecast_series(visual, model, op), "id": "1"}
    show_prop = "enabled" if object_name == "error" else "show"
    props[show_prop] = expr.boolean(op.show if op.show is not None else True)
    if (
        op.kind in _DEFAULT_NAMES
        and op.name is None
        and "displayName" in _OBJECT_PROPS.get(object_name, {}).values()
    ):
        props["displayName"] = expr.string(_default_name(op.kind, visual_type, entries))
    entry: dict[str, Any] = {"properties": props}
    if selector is not None:
        entry["selector"] = selector
    for prop in props:
        if (
            prop not in {"errorRange", "value", "transform"}
            and expr.property_spec(visual_type, object_name, prop) is None
        ):
            raise DocumentError(
                f"The {word} of this visual {_has(word)} no {_property_word(object_name, prop)} "
                "setting in Power BI"
            )
    return entry


def _update_line(
    visual: dict[str, Any],
    op: SetAnalyticsLine,
    model: ModelInfo,
    object_name: str,
    entry: dict[str, Any],
    line: AnalyticsLine,
) -> list[str]:
    """Change what the line marks; returns the words of what changed."""
    props = entry.setdefault("properties", {})
    word = _KIND_WORDS[op.kind]
    before = copy.deepcopy(props)
    said: list[str] = []
    if op.kind in {"constant", "x_constant"} and (op.value is not None or op.measure is not None):
        props["value"] = _line_value(op, model, object_name)
        if object_name == "xAxisReferenceLine":
            _check_x_axis(visual, model, props["value"])
        if props["value"] != before.get("value"):
            where = _position_words(entry, "to")
            said.append(
                f"it now takes its value{where}"
                if where.startswith(" from")
                else f"moved it{where}"
            )
    elif op.kind == "forecast":
        if any(
            v is not None
            for v in (op.forecast_length, op.ignore_last, op.seasonality, op.confidence)
        ):
            props["transform"] = _forecast_transform(op, props.get("transform"))
            if props["transform"] != before.get("transform"):
                said.append("changed its forecast settings")
    elif op.kind in _DYNAMIC and (op.measure is not None or op.percentile is not None):
        query_ref = line.measure if op.measure is None else None
        if query_ref is None:
            query_ref = _series(visual, op, word)
        old = props.get("value")
        old_k = _dig(old, "expr", "Percentile", "K")
        value = _dynamic_value(
            op.kind, query_ref, op.percentile if op.percentile is not None else 50.0
        )
        if op.kind == "percentile" and op.percentile is None:
            if not isinstance(old_k, int | float) or isinstance(old_k, bool):
                raise DocumentError("Say which percentile the line marks (0 to 100)")
            value["expr"]["Percentile"]["K"] = old_k  # kept exactly as written
        props["value"] = value
        if query_ref != line.measure:
            said.append(f"it now follows '{query_ref.rpartition('.')[2]}'")
        if op.kind == "percentile" and value != before.get("value") and op.percentile is not None:
            said.append(f"it now marks the {_number_text(op.percentile)}th percentile")
        entry.setdefault("selector", {})["metadata"] = query_ref
    elif op.kind == "error_bars" and (op.upper is not None or op.lower is not None):
        upper, lower = op.upper, op.lower
        explicit = _dig(props.get("errorRange"), "explicit")
        for key, given in (("upperBound", upper), ("lowerBound", lower)):
            if given is None:
                current = _dig(explicit, key, "expr", "Measure", "Property")
                if key == "upperBound":
                    upper = current if isinstance(current, str) else None
                else:
                    lower = current if isinstance(current, str) else None
        if not upper or not lower:
            raise DocumentError("Say both the upper and the lower bound of the error bars")
        props["errorRange"] = _error_range(model, upper, lower)
        if props["errorRange"] != before.get("errorRange"):
            said.append("changed the bounds")
    return said


def _dig(node: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _apply_remove_analytics_line(
    document: PbirDocument, op: RemoveAnalyticsLine, model: ModelInfo
) -> HandlerResult:
    page, visual = resolve_leaf(document, op.visual)
    label = describe(visual, page)
    lines = analytics_lines(visual)
    pool = _matching(lines, op.kind) if op.kind is not None else lines
    word = _KIND_WORDS[op.kind] if op.kind is not None else "analytics line"
    if not pool:
        plural = word if word.endswith("s") else f"{word}s"
        raise DocumentError(f"{sentence(label)} has no {plural}")
    if op.line is not None and op.kind is None:
        # Lines are numbered per kind: "line 1" without a kind can be the first of several.
        named = [
            line
            for line in pool
            if isinstance(op.line, str)
            and (line.name or "").casefold() == op.line.strip().casefold()
        ]
        number = (
            op.line
            if isinstance(op.line, int)
            else int(op.line)
            if not named and op.line.strip().isdecimal()
            else None
        )
        numbered = [line for line in pool if number is not None and line.number == number]
        if len(named) > 1 or len(numbered) > 1:
            same = named if len(named) > 1 else numbered
            raise NeedsChoice(
                f"{sentence(label)} has {len(same)} analytics lines called {op.line!r}. Which "
                "should go?",
                _line_choices(same, with_kind=True),
            )
    if op.line is not None:
        chosen = [_pick_line(pool, op.line, word, label)]
    elif op.all or len(pool) == 1:
        chosen = pool
    else:
        raise NeedsChoice(
            f"{sentence(label)} has {len(pool)} {word}s. Which should go?",
            _line_choices(pool, with_kind=True) + [Choice("All of them", {"all": True})],
        )
    objects = _objects(visual, "objects")
    gone: dict[str, set[int]] = {}
    for line in chosen:
        gone.setdefault(line.object, set()).add(line.index)
    for object_name, indexes in gone.items():
        kept = [e for i, e in enumerate(objects.get(object_name, [])) if i not in indexes]
        if kept:
            objects[object_name] = kept
        else:
            objects.pop(object_name, None)
    document.touch_visual(page, _name(visual))
    names = [
        f"the {_KIND_WORDS[line.kind]}" + (f" '{line.name}'" if line.name else "")
        for line in chosen
    ]
    return HandlerResult(f"Removed {join_names(names, 4)} from {label}.")


# --- the phone layout ----------------------------------------------------------------------


def _mobile_part(page: PageDoc, name: str) -> str:
    return f"{page.folder}visuals/{name}/mobile.json"


def _mobile_state(document: PbirDocument, page: PageDoc, name: str) -> dict[str, Any] | None:
    part = _mobile_part(page, name)
    if not document.package.has(part):
        return None
    loaded = document.package.read_json(part)
    return loaded if isinstance(loaded, dict) else None


@dataclass(frozen=True)
class MobilePlacement:
    """A visual on a page's phone layout, in phone canvas pixels."""

    visual: str
    label: str
    x: float
    y: float
    width: float
    height: float


def _number(value: Any) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def mobile_layout(document: PbirDocument, page: PageDoc | str) -> list[MobilePlacement]:
    """The visuals a page's phone layout holds, top to bottom."""
    owner = page if isinstance(page, PageDoc) else resolve_page(document, page)
    found: list[MobilePlacement] = []
    for name, visual in owner.visuals.items():
        state = _mobile_state(document, owner, name)
        position = state.get("position") if state is not None else None
        if not isinstance(position, dict):
            continue
        found.append(
            MobilePlacement(
                name,
                describe(visual, owner),
                _number(position.get("x")),
                _number(position.get("y")),
                _number(position.get("width")),
                _number(position.get("height")),
            )
        )
    return sorted(found, key=lambda p: (p.y, p.x))


def describe_mobile_layout(document: PbirDocument, page: str) -> str:
    """The phone layout of a page in words, for a chat answer."""
    owner = resolve_page(document, page)
    placed = mobile_layout(document, owner)
    if not placed:
        return f"Page '{owner.display_name}' has no phone layout yet."
    lines = [
        f"{i}. {sentence(p.label)}: {show(p.width)} x {show(p.height)} "
        f"at ({show(p.x)}, {show(p.y)})"
        for i, p in enumerate(placed, 1)
    ]
    return (
        f"The phone layout of page '{owner.display_name}' has {len(placed)} "
        f"visual{'' if len(placed) == 1 else 's'}:\n" + "\n".join(lines)
    )


def _write_mobile(
    document: PbirDocument,
    page: PageDoc,
    name: str,
    x: float,
    y: float,
    width: float,
    height: float,
    order: int,
) -> None:
    state = _mobile_state(document, page, name) or {"$schema": MOBILE_SCHEMA}
    state["position"] = {
        "x": tidy(x),
        "y": tidy(y),
        "z": order,
        "height": tidy(height),
        "width": tidy(width),
        "tabOrder": order,
    }
    document.package.write_json(_mobile_part(page, name), state)


def _remove_mobile(document: PbirDocument, page: PageDoc, name: str) -> bool:
    part = _mobile_part(page, name)
    if not document.package.has(part):
        return False
    document.package.remove_part(part)
    return True


def _mobile_leaf(page: PageDoc, visual: dict[str, Any]) -> None:
    if is_group(visual) or ancestors(page, _name(visual)):
        raise DocumentError(
            f"{sentence(describe(visual, page))} is "
            + ("a group" if is_group(visual) else "inside a group")
            + ". Studio places single visuals on the phone layout; place groups in Power BI "
            "Desktop's Mobile layout view."
        )


def _mobile_size(page: PageDoc, name: str, width: float) -> float:
    box = page_box(page, name)
    if box.width <= 0:
        return 120.0
    return float(round(min(max(box.height * width / box.width, 40.0), 480.0)))


_MOBILE_NOTE = (
    "The phone layout shows in Power BI Desktop's Mobile layout view and the Power BI mobile "
    "app; Studio's canvas shows the desktop page."
)


def _apply_place_on_mobile(
    document: PbirDocument, op: PlaceOnMobile, model: ModelInfo
) -> HandlerResult:
    page, visual = resolve_visual(document, op.visual)
    _mobile_leaf(page, visual)
    name = _name(visual)
    label = describe(visual, page)
    current = _mobile_state(document, page, name)
    position = current.get("position") if current is not None else None
    placed = mobile_layout(document, page)
    if isinstance(position, dict):
        x = op.x if op.x is not None else _number(position.get("x"))
        y = op.y if op.y is not None else _number(position.get("y"))
        width = op.width if op.width is not None else _number(position.get("width"))
        height = op.height if op.height is not None else _number(position.get("height"))
        order = int(_number(position.get("z")))
        verb = "Moved" if op.x is not None or op.y is not None else "Resized"
        if (op.x is not None or op.y is not None) and (
            op.width is not None or op.height is not None
        ):
            verb = "Moved and resized"
    else:
        width = op.width if op.width is not None else float(MOBILE_WIDTH)
        height = op.height if op.height is not None else _mobile_size(page, name, width)
        x = op.x if op.x is not None else 0.0
        y = op.y if op.y is not None else max((p.y + p.height for p in placed), default=0.0)
        orders = [
            int(
                _number(
                    (_mobile_state(document, page, p.visual) or {}).get("position", {}).get("z")
                )
            )
            for p in placed
        ]
        order = max(orders) + 1000 if orders else 0
        verb = "Added"
    warnings: list[str] = []
    if x + width > MOBILE_WIDTH + 0.5:
        x = max(0.0, MOBILE_WIDTH - width)
        warnings.append(f"It was kept inside the {MOBILE_WIDTH}-pixel-wide phone canvas.")
    if visual.get("isHidden") is True:
        warnings.append(
            f"{sentence(label)} is hidden on the page; check in Power BI Desktop's Mobile layout "
            "view whether the phone shows it."
        )
    _write_mobile(document, page, name, x, y, width, height, order)
    where = f"{show(width)} x {show(height)} at ({show(x)}, {show(y)})"
    summary = (
        f"Added {label} to the phone layout of page '{page.display_name}', {where}."
        if verb == "Added"
        else f"{verb} {label} on the phone layout to {where}."
    )
    return HandlerResult(
        summary, warnings=warnings, visible_in=[_powerbi_only(op.op, _MOBILE_NOTE)]
    )


def _apply_remove_from_mobile(
    document: PbirDocument, op: RemoveFromMobile, model: ModelInfo
) -> HandlerResult:
    if op.all or (op.page is not None and not op.visuals):
        if op.page is None:
            raise DocumentError("Say which page's phone layout to clear")
        page = resolve_page(document, op.page)
        names = [p.visual for p in mobile_layout(document, page)]
        if not names:
            return HandlerResult(f"Page '{page.display_name}' has no phone layout to clear.")
        if not op.all:
            # A page and no visuals: the visuals the request named may not have been found, so
            # the whole page only goes when the person says so.
            raise NeedsChoice(
                f"Take every visual ({len(names)}) off the phone layout of page "
                f"'{page.display_name}'?",
                [Choice(f"Clear the phone layout of page '{page.display_name}'", {"all": True})],
            )
        for name in names:
            _remove_mobile(document, page, name)
        return HandlerResult(
            f"Cleared the phone layout of page '{page.display_name}' ({len(names)} "
            f"visual{'' if len(names) == 1 else 's'}).",
            visible_in=[_powerbi_only(op.op, _MOBILE_NOTE)],
        )
    if not op.visuals:
        raise DocumentError("Say which visuals to take off the phone layout")
    removed: list[str] = []
    absent: list[str] = []
    for ref in op.visuals:
        page, visual = resolve_visual(document, ref)
        (removed if _remove_mobile(document, page, _name(visual)) else absent).append(
            describe(visual, page)
        )
    if not removed:
        raise DocumentError(f"{sentence(join_names(absent))} is not on the phone layout")
    warnings = [f"{sentence(join_names(absent))} was not on the phone layout."] if absent else []
    return HandlerResult(
        f"Took {join_names(removed, 4)} off the phone layout.",
        warnings=warnings,
        visible_in=[_powerbi_only(op.op, _MOBILE_NOTE)],
    )


def _reading_order(page: PageDoc, names: Sequence[str]) -> list[str]:
    """Top to bottom, left to right: a visual whose top is above the middle of the row's first
    visual belongs to that row."""
    boxes = {n: page_box(page, n) for n in names}
    ordered = sorted(names, key=lambda n: (boxes[n].y, boxes[n].x))
    rows: list[list[str]] = []
    limit = -math.inf
    for name in ordered:
        box = boxes[name]
        if rows and box.y < limit:
            rows[-1].append(name)
        else:
            rows.append([name])
            limit = box.y + box.height / 2
    return [n for row in rows for n in sorted(row, key=lambda n: boxes[n].x)]


def _apply_arrange_mobile_layout(
    document: PbirDocument, op: ArrangeMobileLayout, model: ModelInfo
) -> HandlerResult:
    page = resolve_page(document, op.page)
    boxes = leaf_boxes(page)
    panels = backdrops(page, boxes)

    def arranged(name: str) -> bool:
        """What the automatic layout places: a visual of its own, shown, not a panel."""
        visual = page.visuals[name]
        return (
            name in boxes
            and not ancestors(page, name)
            and visual.get("isHidden") is not True
            and name not in panels
        )

    skipped: list[str] = []
    if op.visuals is not None:
        names: list[str] = []
        for ref in op.visuals:
            found_page, visual = resolve_visual(document, ref)
            if found_page is not page:
                raise DocumentError(
                    f"{sentence(describe(visual, found_page))} is on page "
                    f"'{found_page.display_name}', not '{page.display_name}'"
                )
            _mobile_leaf(page, visual)
            if _name(visual) not in names:
                names.append(_name(visual))
    else:
        names = []
        for name, visual in page.visuals.items():
            if arranged(name):
                names.append(name)
            elif not ancestors(page, name) and (is_group(visual) or name in boxes):
                skipped.append(describe(visual, page))
        names = _reading_order(page, names)
    if not names:
        raise DocumentError(f"Page '{page.display_name}' has no visuals to put on the phone layout")
    page_width, _ = _page_bounds(page)
    columns = op.columns
    cell = (MOBILE_WIDTH - op.gap * (columns - 1)) / columns
    y = 0.0
    column = 0
    row_height = 0.0
    kept: list[str] = []
    for placed in mobile_layout(document, page):
        if placed.visual in names:
            continue
        # Only what this layout could have placed is taken off; a group's, a grouped, hidden or
        # panel visual's own placement is Desktop's work and stays.
        if arranged(placed.visual):
            _remove_mobile(document, page, placed.visual)
        else:
            kept.append(placed.label)
    for index, name in enumerate(names):
        wide = columns == 1 or page_box(page, name).width > page_width * 0.4
        if wide:
            if column:
                y += row_height + op.gap
                column, row_height = 0, 0.0
            height = _mobile_size(page, name, MOBILE_WIDTH)
            _write_mobile(document, page, name, 0, y, MOBILE_WIDTH, height, index * 1000)
            y += height + op.gap
            continue
        height = _mobile_size(page, name, cell)
        x = column * (cell + op.gap)
        _write_mobile(document, page, name, round(x, 2), y, round(cell, 2), height, index * 1000)
        row_height = max(row_height, height)
        column += 1
        if column == columns:
            y += row_height + op.gap
            column, row_height = 0, 0.0
    grid = "one column" if columns == 1 else f"{columns} columns"
    order = "in the order given" if op.visuals is not None else "in the page's reading order"
    summary = (
        f"Laid out the phone layout of page '{page.display_name}': {len(names)} "
        f"visual{'' if len(names) == 1 else 's'} in {grid}, {order}."
    )
    warnings = []
    if skipped:
        warnings.append(
            f"Left out {join_names(skipped, 4)}: groups, hidden visuals and background panels "
            "are placed in Power BI Desktop's Mobile layout view."
        )
    if kept:
        warnings.append(
            f"Kept the phone placement of {join_names(kept, 4)}: Studio does not arrange "
            "groups, grouped, hidden or background visuals."
        )
    return HandlerResult(
        summary, warnings=warnings, visible_in=[_powerbi_only(op.op, _MOBILE_NOTE)]
    )


# --- groups ------------------------------------------------------------------------------

_GROUP_MODES = {"scale": "ScaleMode", "scroll": "ScrollMode"}
_MODE_WORDS = {
    "ScaleMode": "resizes its visuals with it",
    "ScrollMode": "keeps its visuals' size and scrolls",
}


def _resolve_group(document: PbirDocument, ref: str) -> tuple[PageDoc, dict[str, Any]]:
    page, visual = resolve_visual(document, ref)
    if not is_group(visual):
        raise DocumentError(f"{sentence(describe(visual, page))} is not a group")
    return page, visual


def _group_name(page: PageDoc, wanted: str | None) -> str:
    if wanted is not None:
        if any(c in wanted for c in "\r\n\t"):
            raise DocumentError("A group name is one line of text")
        return " ".join(wanted.split())
    taken = {
        str(v["visualGroup"].get("displayName", "")).casefold()
        for v in page.visuals.values()
        if is_group(v)
    }
    number = 1
    while f"group {number}" in taken:
        number += 1
    return f"Group {number}"


def _apply_group_visuals(
    document: PbirDocument, op: GroupVisuals, model: ModelInfo
) -> HandlerResult:
    page: PageDoc | None = None
    names: list[str] = []
    for ref in op.visuals:
        found_page, visual = resolve_visual(document, ref)
        if page is not None and found_page is not page:
            raise DocumentError(
                "A group holds visuals of one page, but "
                f"{describe(visual, found_page)} is on page '{found_page.display_name}'"
            )
        page = found_page
        if _name(visual) not in names:
            names.append(_name(visual))
    assert page is not None
    if len(names) < 2:
        raise DocumentError("A group needs at least two different visuals")
    for name in names:
        for above in ancestors(page, name):
            if above in names:
                raise DocumentError(
                    f"{sentence(describe(page.visuals[name], page))} is already inside "
                    f"{describe(page.visuals[above], page)}; group the group or its members, "
                    "not both"
                )
    parents = {page.visuals[n].get("parentGroupName") for n in names}
    parents = {p if isinstance(p, str) and p in page.visuals else None for p in parents}
    if len(parents) > 1:
        raise DocumentError(
            "Those visuals sit in different groups. Group visuals that share a group (or none)"
        )
    parent = parents.pop()
    boxes = {n: page_box(page, n) for n in names}
    # An integral origin keeps a member's page position exact through group and ungroup:
    # x - n and n + (x - n) are both exact in floating point when n is a whole number <= x.
    left = math.floor(min(b.x for b in boxes.values()))
    top = math.floor(min(b.y for b in boxes.values()))
    right = math.ceil(max(b.right for b in boxes.values()))
    bottom = math.ceil(max(b.bottom for b in boxes.values()))
    positions = [page.visuals[n].get("position") or {} for n in names]
    z_values = [_number(p.get("z")) for p in positions if "z" in p]
    tab_orders = [_number(p.get("tabOrder")) for p in positions if "tabOrder" in p]
    interleaved = _interleaved(page, names, parent, z_values)
    display_name = _group_name(page, op.name)
    group_id = document.new_name(document.visual_names())
    group: dict[str, Any] = {
        "$schema": document.schema("visualContainer"),
        "name": group_id,
        "position": {
            "x": 0,
            "y": 0,
            "z": tidy(min(z_values)) if z_values else 0,
            "height": bottom - top,
            "width": right - left,
            "tabOrder": _free_tab_order(page, min(tab_orders) if tab_orders else None),
        },
        "visualGroup": {"displayName": display_name, "groupMode": _GROUP_MODES[op.mode]},
    }
    if parent is not None:
        group["parentGroupName"] = parent
    page.visuals[group_id] = group
    write_box(page, group_id, x=left, y=top)
    document.touch_visual(page, group_id)
    for name in names:
        page.visuals[name]["parentGroupName"] = group_id
        write_box(page, name, x=boxes[name].x, y=boxes[name].y)
        document.touch_visual(page, name)
    labels = [describe(page.visuals[n], page) for n in names]
    inside = f" inside {describe(page.visuals[parent], page)}" if parent is not None else ""
    warnings = []
    if interleaved:
        between = join_names([describe(page.visuals[n], page) for n in interleaved], 3)
        one = len(interleaved) == 1
        warnings.append(
            f"Their layers were interleaved with {between}: in Power BI a group stacks as one "
            f"block, so {'it' if one else 'those visuals'} may now draw above or below all of "
            f"them. Bring {'it' if one else 'them'} forward or back if "
            f"{'it does' if one else 'they do'}."
        )
    return HandlerResult(
        f"Grouped {join_names(labels, 4)} into the group '{display_name}'{inside}; they keep "
        f"their places on the page. The group {_MODE_WORDS[_GROUP_MODES[op.mode]]}.",
        warnings=warnings,
    )


def _interleaved(
    page: PageDoc, names: Sequence[str], parent: str | None, z_values: Sequence[float]
) -> list[str]:
    """Visuals beside the new group whose layer falls between its members' layers."""
    if len(z_values) < 2:
        return []
    low, high = min(z_values), max(z_values)
    found = []
    for other, visual in page.visuals.items():
        if other in names:
            continue
        other_parent = visual.get("parentGroupName")
        if (other_parent if isinstance(other_parent, str) else None) != parent:
            continue
        position = visual.get("position") or {}
        if "z" in position and low < _number(position.get("z")) < high:
            found.append(other)
    return found


def _free_tab_order(page: PageDoc, before: float | None) -> int:
    """A tab stop no visual of the page has, just before the group's first member (so the
    keyboard reaches the group where it reached its first member), else after every other."""
    taken = {
        _number(v["position"].get("tabOrder"))
        for v in page.visuals.values()
        if isinstance(v.get("position"), dict) and "tabOrder" in v["position"]
    }
    if before is not None:
        floor = max((t for t in taken if t < before), default=-1.0)
        candidate = math.ceil(before) - 1
        while candidate > floor:
            if candidate not in taken and candidate >= 0:
                return candidate
            candidate -= 1
    return int(max(taken, default=-1000.0)) + 1000


def _bookmarks_with(document: PbirDocument, page: PageDoc, name: str) -> list[str]:
    """The bookmarks that remember a visual's (or group's) state or target it."""
    found = []
    for key, bookmark in document.bookmarks().items():
        state = bookmark.get("explorationState") if isinstance(bookmark, dict) else None
        section = _dig(state, "sections", page.name)
        mentioned = any(
            isinstance(section, dict)
            and isinstance(section.get(kind), dict)
            and name in section[kind]
            for kind in ("visualContainers", "visualContainerGroups")
        )
        targets = _dig(bookmark, "options", "targetVisualNames")
        if mentioned or (isinstance(targets, list) and name in targets):
            found.append(str(bookmark.get("displayName") or key))
    return found


def _apply_ungroup_visuals(
    document: PbirDocument, op: UngroupVisuals, model: ModelInfo
) -> HandlerResult:
    page, group = _resolve_group(document, op.group)
    group_id = _name(group)
    label = describe(group, page)
    members = [n for n, v in page.visuals.items() if v.get("parentGroupName") == group_id]
    remembered = _bookmarks_with(document, page, group_id)
    hidden = group.get("isHidden") is True and bool(members)
    if (remembered or hidden) and not op.cleanup_references:
        losses = []
        if remembered:
            losses.append(
                f"{join_names([f"'{b}'" for b in remembered], 4)} "
                f"remember{'s' if len(remembered) == 1 else ''} the group's state, which would be "
                "dropped"
            )
        if hidden:
            losses.append(
                f"the group is hidden, so its {len(members)} visual"
                f"{'' if len(members) == 1 else 's'} would show on the page"
            )
        raise NeedsConfirmation(
            "cleanup_references", f"Ungrouping {label}: {'; and '.join(losses)}."
        )
    boxes = {n: page_box(page, n) for n in members}
    result = remove_visual_references(document, page, group_id, group_children=False)
    for name in members:
        write_box(page, name, x=boxes[name].x, y=boxes[name].y)
        document.touch_visual(page, name)
    summary = (
        f"Ungrouped {label}: its {len(members)} visual{'' if len(members) == 1 else 's'} "
        "stay where they are."
        if members
        else f"Removed {label}, which held no visuals."
    )
    if result.counts.get("bookmark"):
        count = result.counts["bookmark"]
        summary += f" The group was also removed from {count} bookmark{'' if count == 1 else 's'}."
    return HandlerResult(summary)


def _apply_set_group(document: PbirDocument, op: SetGroup, model: ModelInfo) -> HandlerResult:
    if op.name is None and op.mode is None:
        raise DocumentError("Say the group's new name or how it resizes (scale or scroll)")
    page, group = _resolve_group(document, op.group)
    config = group["visualGroup"]
    label = describe(group, page)
    said: list[str] = []
    if op.name is not None:
        name = _group_name(page, op.name)
        if config.get("displayName") != name:
            config["displayName"] = name
            said.append(f"renamed it '{name}'")
    if op.mode is not None:
        mode = _GROUP_MODES[op.mode]
        if config.get("groupMode") != mode:
            config["groupMode"] = mode
            said.append(f"it now {_MODE_WORDS[mode]}")
    if not said:
        return HandlerResult(f"{sentence(label)} was already set that way.")
    document.touch_visual(page, _name(group))
    return HandlerResult(f"Changed {label}: {join_names(said)}.")


# --- visual calculations ---------------------------------------------------------------

_TEMPLATES: dict[str, tuple[str, str]] = {
    "running_sum": ("Running sum", "RUNNINGSUM({f})"),
    "moving_average": ("Moving average", "MOVINGAVERAGE({f}, {window})"),
    "percent_of_parent": ("Percent of parent", "DIVIDE({f}, COLLAPSE({f}, ROWS))"),
    "percent_of_grand_total": ("Percent of grand total", "DIVIDE({f}, COLLAPSEALL({f}, ROWS))"),
    "average_of_children": ("Average of children", "EXPAND(AVERAGE({f}), ROWS)"),
    "versus_previous": ("Versus previous", "{f} - PREVIOUS({f})"),
    "versus_next": ("Versus next", "{f} - NEXT({f})"),
    "versus_parent": ("Versus parent", "{f} - COLLAPSE({f}, ROWS)"),
    "versus_grand_total": ("Versus grand total", "{f} - COLLAPSEALL({f}, ROWS)"),
}
_BRACKETED = re.compile(r'"(?:[^"]|"")*"|\[((?:[^\]]|\]\])*)\]')


def _bracket(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _calc_names(visual: dict[str, Any]) -> dict[str, str]:
    """casefolded name -> name, for every name a visual calculation can use in brackets: each
    field's name in the visual (nativeQueryRef, display name, own name) and other calculations."""
    names: dict[str, str] = {}
    for _, projection in _role_projections(visual):
        calc = _calc_of(projection)
        candidates = [projection.get("nativeQueryRef"), projection.get("displayName")]
        if calc is not None:
            candidates.append(calc.get("Name"))
        else:
            identity = field_identity(projection)
            if identity[0] in {"Column", "Measure"}:
                candidates.append(identity[2])
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                names.setdefault(candidate.strip().casefold(), candidate)
    return names


def _dax_nodes(node: Any) -> Iterator[Any]:
    yield node
    if is_dataclass(node) and not isinstance(node, type):
        for item in fields(node):
            yield from _dax_nodes(getattr(node, item.name))
    elif isinstance(node, tuple | list):
        for item in node:
            yield from _dax_nodes(item)


def _check_calculation(visual: dict[str, Any], name: str, expression: str) -> list[str]:
    """Parse the expression and check every bracketed name is a field of the visual; returns
    the names it uses."""
    try:
        tree = dax_syntax.parse_expression(expression)
    except dax_syntax.DaxParseError as error:
        raise DocumentError(f"The calculation is not valid DAX: {error}") from error
    known = _calc_names(visual)
    used: list[str] = []
    for node in _dax_nodes(tree):
        if not isinstance(node, dax_syntax.ColRef):
            continue
        if node.table is not None:
            raise DocumentError(
                "A visual calculation can only use the fields in the visual: write "
                f"{_bracket(node.column)}, not '{node.table}'{_bracket(node.column)}"
            )
        key = node.column.strip().casefold()
        if key == name.strip().casefold():
            raise DocumentError(f"The calculation {name!r} cannot use itself")
        if key not in known:
            raise DocumentError(
                f"{_bracket(node.column)} is not a field of this visual. Its fields: "
                f"{_field_list(visual)}."
            )
        used.append(known[key])
    return used


def _rename_in_expression(expression: str, old: str, new: str) -> str:
    wanted = old.casefold()

    def swap(match: re.Match[str]) -> str:
        inner = match.group(1)
        if inner is not None and inner.replace("]]", "]").casefold() == wanted:
            return _bracket(new)
        return match.group(0)

    return _BRACKETED.sub(swap, expression)


def _uses_name(expression: str, name: str) -> bool:
    wanted = name.casefold()
    return any(
        m.group(1) is not None and m.group(1).replace("]]", "]").casefold() == wanted
        for m in _BRACKETED.finditer(expression)
    )


@dataclass(frozen=True)
class VisualCalculation:
    name: str
    expression: str
    role: str


def visual_calculations(visual: dict[str, Any]) -> list[VisualCalculation]:
    found: list[VisualCalculation] = []
    for role, projection in _role_projections(visual):
        calc = _calc_of(projection)
        if calc is not None:
            found.append(
                VisualCalculation(str(calc.get("Name", "")), str(calc.get("Expression", "")), role)
            )
    return found


def _small_multiples_role(visual_type: str) -> str | None:
    table = ROLE_TABLE.get(visual_type)
    if table is not None and table.small_multiples:
        return table.small_multiples
    return role_by_name(visual_type, "small multiples")


def _calc_role(visual: dict[str, Any], wanted: str | None) -> str:
    visual_type = _visual_type(visual)
    catalog_roles = expr.catalog()["visualTypes"].get(visual_type, {}).get("roles", {})
    takes_values: list[str] = [
        str(role)
        for role, spec in catalog_roles.items()
        if isinstance(spec, dict) and spec.get("kind") in {"Measure", "GroupingOrMeasure"}
    ]
    if wanted is not None:
        role = role_by_name(visual_type, wanted)
        if role is None or role not in takes_values:
            raise DocumentError(f"A {_type_word(visual_type)} has no value well called {wanted!r}")
        return role
    table = ROLE_TABLE.get(visual_type)
    preferred = [*(table.measure_roles if table else ()), *(table.either_roles if table else ())]
    for role in [*preferred, *takes_values]:
        if role in takes_values and role in _state(visual):
            return role
    for role in [*preferred, *takes_values]:
        if role in takes_values:
            return role
    raise DocumentError(f"A {_type_word(visual_type)} has no place for a calculated value")


def _find_calc(visual: dict[str, Any], name: str) -> tuple[str, dict[str, Any]] | None:
    wanted = name.strip().casefold()
    for role, projection in _role_projections(visual):
        calc = _calc_of(projection)
        if calc is not None and str(calc.get("Name", "")).strip().casefold() == wanted:
            return role, projection
    return None


def _calc_visible(op: str, visual: dict[str, Any], name: str) -> list[VisibleIn]:
    """Studio's canvas computes the common calculations itself (``render_analytics``: running
    sum, moving average, versus previous, percent of parent or total...); anything else shows
    only in Power BI. The fields are described the way the page payload labels them."""
    try:
        from . import render_analytics
    except ImportError:
        parse = None
    else:
        parse = getattr(render_analytics, "parse_calculation", None)
    found = _find_calc(visual, name)
    if parse is not None and found is not None:
        fields: list[dict[str, Any]] = []
        own = -1
        for index, (_, projection) in enumerate(_role_projections(visual)):
            calc = _calc_of(projection)
            identity = field_identity(projection)
            if calc is not None:
                own_name = str(calc.get("Name", ""))
                if projection is found[1]:
                    own = index
            elif identity[0] in {"Column", "Measure"}:
                own_name = identity[2]
            else:
                own_name = projection_label(projection)
            fields.append(
                {
                    "label": projection.get("displayName")
                    or projection.get("nativeQueryRef")
                    or own_name,
                    "name": own_name,
                    "query_ref": projection.get("queryRef"),
                }
            )
        calc = _calc_of(found[1]) or {}
        if parse(str(calc.get("Expression", "")), fields, own) is not None:
            return []
    return [
        _powerbi_only(
            op,
            "Studio's canvas computes running sums, moving averages, previous and next values "
            "and percents of parent or total; Power BI shows this calculation.",
        )
    ]


def _calc_name(visual: dict[str, Any], op: SetVisualCalculation) -> str:
    """The calculation's name: the one given, else the template's name as Power BI gives it
    ("Running sum", then "Running sum 2")."""
    if op.name is not None:
        name = " ".join(op.name.split())
        if not name or any(c in op.name for c in "[]\r\n\t"):
            raise DocumentError("A calculation name is one line of text without brackets")
        return name
    if op.template is None:
        raise DocumentError("Say the calculation's name")
    base = _TEMPLATES[op.template][0]
    taken = _calc_names(visual)
    name, number = base, 2
    while name.casefold() in taken:
        name, number = f"{base} {number}", number + 1
    return name


def _calc_expression(visual: dict[str, Any], op: SetVisualCalculation) -> str | None:
    if op.expression is not None and op.template is not None:
        raise DocumentError("Give either an expression or a template, not both")
    if op.template is None:
        return op.expression
    if op.field is None:
        raise DocumentError("Say which field of the visual the calculation is based on")
    found = [p for _, p in _find_fields(visual, op.field)]
    if not found:
        raise DocumentError(
            f"{op.field!r} is not a field of this visual. Its fields: {_field_list(visual)}."
        )
    projection = found[0]
    calc = _calc_of(projection)
    reference = (
        str(calc.get("Name"))
        if calc is not None
        else str(projection.get("nativeQueryRef") or "").strip() or projection_label(projection)
    )
    if op.template != "moving_average" and op.window is not None:
        raise DocumentError("Only a moving average takes a window")
    return _TEMPLATES[op.template][1].format(f=_bracket(reference), window=op.window or 3)


def _apply_set_visual_calculation(
    document: PbirDocument, op: SetVisualCalculation, model: ModelInfo
) -> HandlerResult:
    page, visual = resolve_leaf(document, op.visual)
    label = describe(visual, page)
    name = _calc_name(visual, op)
    found = _find_calc(visual, name)
    expression = _calc_expression(visual, op)
    if found is None:
        if expression is None:
            raise DocumentError(
                f"{sentence(label)} has no calculation {name!r}. To add one, give its "
                "expression or a template"
            )
        if op.new_name is not None:
            raise DocumentError(f"{sentence(label)} has no calculation {name!r} to rename")
        visual_type = _visual_type(visual)
        split = _small_multiples_role(visual_type)
        if split and _state(visual).get(split, {}).get("projections"):
            raise DocumentError(
                "Power BI does not allow visual calculations on a visual split into small "
                "multiples; remove the small multiples first"
            )
        taken = _calc_names(visual).get(name.casefold())
        if taken is not None:
            raise DocumentError(f"{sentence(label)} already has a field called {taken!r}")
        _check_calculation(visual, name, expression)
        role = _calc_role(visual, op.role)
        refs = _query_refs(visual)
        query_ref = name if name not in refs else f"{name} (calculation)"
        projection = {
            "field": {
                "NativeVisualCalculation": {
                    "Language": "dax",
                    "Expression": expression,
                    "Name": name,
                }
            },
            "queryRef": query_ref,
            "nativeQueryRef": name,
        }
        state = visual["visual"].setdefault("query", {}).setdefault("queryState", {})
        state.setdefault(role, {}).setdefault("projections", []).append(projection)
        document.touch_visual(page, _name(visual))
        return HandlerResult(
            f"Added the visual calculation '{name}' = {expression} to {label}.",
            visible_in=_calc_visible(op.op, visual, name),
        )
    role, projection = found
    calc = _calc_of(projection)
    assert calc is not None
    said: list[str] = []
    old_name = str(calc.get("Name"))
    if expression is not None and expression != calc.get("Expression"):
        _check_calculation(visual, old_name, expression)
        calc["Expression"] = expression
        _sync_calc_sorts(visual, old_name, calc)
        said.append(f"its formula is now {expression}")
    if op.new_name is not None and op.new_name != old_name:
        new_name = " ".join(op.new_name.split())
        if not new_name or any(c in op.new_name for c in "[]\r\n\t"):
            raise DocumentError("A calculation name is one line of text without brackets")
        clash = _calc_names(visual).get(new_name.casefold())
        if clash is not None and clash.casefold() != old_name.casefold():
            raise DocumentError(f"{sentence(label)} already has a field called {clash!r}")
        calc["Name"] = new_name
        projection["nativeQueryRef"] = new_name
        _sync_calc_sorts(visual, old_name, calc)
        updated = 0
        for other in visual_calculations(visual):
            if other.name == new_name:
                continue
            match = _find_calc(visual, other.name)
            if match is None:
                continue
            other_calc = _calc_of(match[1])
            assert other_calc is not None
            text = str(other_calc.get("Expression", ""))
            if _uses_name(text, old_name):
                other_calc["Expression"] = _rename_in_expression(text, old_name, new_name)
                _sync_calc_sorts(visual, other.name, other_calc)
                updated += 1
        said.append(f"renamed it '{new_name}'")
        if updated:
            said.append(
                "updated the calculation that uses it"
                if updated == 1
                else f"updated the {updated} calculations that use it"
            )
    if not said:
        return HandlerResult(f"The calculation '{old_name}' on {label} was already like that.")
    document.touch_visual(page, _name(visual))
    return HandlerResult(
        f"Changed the visual calculation '{old_name}' on {label}: {join_names(said)}.",
        visible_in=_calc_visible(op.op, visual, str(calc.get("Name"))),
    )


def _apply_remove_visual_calculation(
    document: PbirDocument, op: RemoveVisualCalculation, model: ModelInfo
) -> HandlerResult:
    page, visual = resolve_leaf(document, op.visual)
    label = describe(visual, page)
    found = _find_calc(visual, op.name)
    if found is None:
        names = [f"'{c.name}'" for c in visual_calculations(visual)]
        listed = f" It has {join_names(names, 6)}." if names else " It has none."
        raise DocumentError(f"{sentence(label)} has no visual calculation {op.name!r}.{listed}")
    role, projection = found
    calc = _calc_of(projection)
    assert calc is not None
    name = str(calc.get("Name"))
    users = [
        c.name
        for c in visual_calculations(visual)
        if c.name != name and _uses_name(c.expression, name)
    ]
    if users:
        raise DocumentError(
            f"The calculation '{name}' is used by {join_names([f"'{u}'" for u in users])}; change "
            "or remove those first"
        )
    state = _state(visual)
    bucket = state[role]
    bucket["projections"] = [p for p in bucket.get("projections", []) if p is not projection]
    if not bucket["projections"]:
        state.pop(role)
    notes = drop_field_references(visual, [projection])
    if _drop_calc_sorts(visual, name):
        notes.append(f"Removed the sort by {name}.")
    document.touch_visual(page, _name(visual))
    return HandlerResult(
        f"Removed the visual calculation '{name}' from {label}.",
        warnings=list(dict.fromkeys(notes)),
    )


def _calc_sorts(visual: dict[str, Any]) -> list[dict[str, Any]]:
    query = visual.get("visual", {}).get("query")
    sort = query.get("sortDefinition") if isinstance(query, dict) else None
    items = sort.get("sort") if isinstance(sort, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _sync_calc_sorts(visual: dict[str, Any], old_name: str, calc: dict[str, Any]) -> None:
    """A sort by a visual calculation holds its own copy of the calculation (name and DAX), so
    a renamed or rewritten calculation rewrites the sort's copy too, matched by name."""
    for item in _calc_sorts(visual):
        own = _dig(item, "field", "NativeVisualCalculation")
        if isinstance(own, dict) and str(own.get("Name", "")).casefold() == old_name.casefold():
            item["field"] = {"NativeVisualCalculation": copy.deepcopy(calc)}


def _drop_calc_sorts(visual: dict[str, Any], name: str) -> bool:
    """Remove the sorts by a visual calculation, matched by its name (the sort's copy of the
    DAX may be older than the calculation's). Returns whether any was removed."""
    query = visual.get("visual", {}).get("query")
    sort = query.get("sortDefinition") if isinstance(query, dict) else None
    items = _calc_sorts(visual)
    kept = [
        item
        for item in items
        if str(_dig(item, "field", "NativeVisualCalculation", "Name") or "").casefold()
        != name.casefold()
        or _dig(item, "field", "NativeVisualCalculation") is None
    ]
    if len(kept) == len(items) or not isinstance(query, dict) or not isinstance(sort, dict):
        return False
    if kept:
        sort["sort"] = kept
    else:
        query.pop("sortDefinition", None)
    return True


# --- small multiples ------------------------------------------------------------------------


def _apply_set_small_multiples(
    document: PbirDocument, op: SetSmallMultiples, model: ModelInfo
) -> HandlerResult:
    page, visual = resolve_leaf(document, op.visual)
    label = describe(visual, page)
    visual_type = _visual_type(visual)
    role = _small_multiples_role(visual_type)
    if role is None:
        raise DocumentError(f"{sentence(label)} cannot be split into small multiples in Power BI")
    state = visual["visual"].setdefault("query", {}).setdefault("queryState", {})
    current = (
        state.get(role, {}).get("projections", []) if isinstance(state.get(role), dict) else []
    )
    written: list[tuple[str, str, str]] = []  # (object, property, words)
    if op.remove:
        if not current:
            return HandlerResult(f"{sentence(label)} was not split into small multiples.")
        removed = list(current)
        state.pop(role)
        notes = drop_field_references(visual, removed)
        objects = _objects(visual, "objects")
        for object_name in ("smallMultiplesLayout", "subheader"):
            objects.pop(object_name, None)
        document.touch_visual(page, _name(visual))
        return HandlerResult(f"Turned off the small multiples of {label}.", warnings=notes)
    said: list[str] = []
    if op.field is not None:
        if visual_calculations(visual):
            raise DocumentError(
                "Power BI does not allow small multiples on a visual with visual calculations; "
                "remove the calculations first"
            )
        kind, table, name = model.resolve(op.field)
        if kind != "column":
            raise DocumentError(
                f"Small multiples split the visual by a column's values, and [{name}] is a measure"
            )
        projection = _field_projection(model, op.field, measure_role=False)
        replaced = [p for p in current if field_identity(p) != field_identity(projection)]
        state[role] = {"projections": [projection]}
        drop_field_references(visual, replaced)
        said.append(f"split by {name}")
    elif not current:
        raise DocumentError(f"Say which field to split {label} by")
    if op.rows is not None or op.columns is not None:
        objects = _catalog_objects(visual_type).get("smallMultiplesLayout", {})
        if "layoutType" in objects:
            _set(document, visual, "smallMultiplesLayout", "layoutType", "custom")
        if op.rows is not None:
            _set(document, visual, "smallMultiplesLayout", "rowCount", op.rows)
            written.append(("smallMultiplesLayout", "rowCount", "the grid"))
        if op.columns is not None:
            _set(document, visual, "smallMultiplesLayout", "columnCount", op.columns)
            written.append(("smallMultiplesLayout", "columnCount", "the grid"))
        grid = " x ".join(str(v) for v in (op.rows, op.columns) if v is not None)
        said.append(
            f"a grid of {grid}"
            if op.rows and op.columns
            else f"{op.rows} row{'s' if op.rows != 1 else ''}"
            if op.rows
            else f"{op.columns} column{'s' if op.columns != 1 else ''}"
        )
    if op.shared_y_axis is not None:
        _set(document, visual, "valueAxis", "sharedAxis", op.shared_y_axis)
        said.append("a shared y-axis" if op.shared_y_axis else "a y-axis per chart")
        written.append(("valueAxis", "sharedAxis", "the shared y-axis"))
    if op.titles is not None:
        _set(document, visual, "subheader", "show", op.titles)
        said.append("titles on each chart" if op.titles else "no titles on the charts")
        written.append(("subheader", "show", "the titles of the charts"))
    if not said:
        raise DocumentError("Say the field to split by, or the grid (rows, columns)")
    document.touch_visual(page, _name(visual))
    # The canvas draws small multiples when it draws their grid; then only the settings it
    # does not draw are Power BI only.
    shows: list[VisibleIn] = []
    if not property_is_rendered(visual_type, "smallMultiplesLayout", "rowCount", container=False):
        shows.append(
            _powerbi_only(
                op.op,
                "Studio draws the visual as one chart; Power BI shows the grid of small multiples.",
            )
        )
    else:
        unseen = list(
            dict.fromkeys(
                words
                for obj, prop, words in written
                if not property_is_rendered(visual_type, obj, prop, container=False)
            )
        )
        if unseen:
            shows.append(
                _powerbi_only(op.op, f"Power BI shows {join_names(unseen)}; Studio does not yet.")
            )
    return HandlerResult(
        f"Set the small multiples of {label}: {join_names(said, 5)}.", visible_in=shows
    )


# --- accessibility and other visual settings ------------------------------------------------

_PAGE_VIEWS = {
    "fit_to_page": "FitToPage",
    "fit_to_width": "FitToWidth",
    "actual_size": "ActualSize",
}
_PAGE_VIEW_WORDS = {
    "FitToPage": "fit to page",
    "FitToWidth": "fit to width",
    "ActualSize": "actual size",
}


def _leaves(document: PbirDocument, refs: Sequence[str]) -> list[tuple[PageDoc, dict[str, Any]]]:
    found: list[tuple[PageDoc, dict[str, Any]]] = []
    for ref in refs:
        page, visual = resolve_leaf(document, ref)
        if all(v is not visual for _, v in found):
            found.append((page, visual))
    return found


def _container_value(visual: dict[str, Any], object_name: str, prop: str) -> Any:
    entries = visual.get("visual", {}).get("visualContainerObjects", {}).get(object_name, [])
    for entry in entries if isinstance(entries, list) else []:
        props = entry.get("properties") if isinstance(entry, dict) else None
        if isinstance(props, dict) and prop in props:
            return expr.decode(props[prop])
    return None


def _apply_set_alt_text(document: PbirDocument, op: SetAltText, model: ModelInfo) -> HandlerResult:
    page, visual = resolve_leaf(document, op.visual)
    label = describe(visual, page)
    text = " ".join((op.text or "").split())
    note = _powerbi_only(
        op.op, "Alt text is read by screen readers in Power BI; nothing changes on the canvas."
    )
    if not text:
        if _container_value(visual, "general", "altText") is None:
            return HandlerResult(f"{sentence(label)} had no alt text.")
        container = _objects(visual, "visualContainerObjects")
        for entry in container.get("general", []):
            entry.get("properties", {}).pop("altText", None)
        container["general"] = [e for e in container.get("general", []) if e.get("properties")]
        if not container["general"]:
            container.pop("general")
        if not container:
            visual["visual"].pop("visualContainerObjects")
        document.touch_visual(page, _name(visual))
        return HandlerResult(f"Removed the alt text of {label}.", visible_in=[note])
    if _container_value(visual, "general", "altText") == text:
        return HandlerResult(f"{sentence(label)} already has that alt text.")
    _set(document, visual, "general", "altText", text)
    document.touch_visual(page, _name(visual))
    return HandlerResult(f"Set the alt text of {label} to '{text}'.", visible_in=[note])


def _apply_set_tab_order(
    document: PbirDocument, op: SetTabOrder, model: ModelInfo
) -> HandlerResult:
    page: PageDoc | None = resolve_page(document, op.page) if op.page is not None else None
    first: list[str] = []
    for ref in op.visuals:
        found_page, visual = resolve_leaf(document, ref)
        if page is not None and found_page is not page:
            raise DocumentError(
                f"Tab order is per page, and {describe(visual, found_page)} is on page "
                f"'{found_page.display_name}', not '{page.display_name}'"
            )
        page = found_page
        if _name(visual) not in first:
            first.append(_name(visual))
    if page is None:
        raise DocumentError("Say which visuals come first, or which page to order by position")
    if not first and not op.by_position:
        raise DocumentError("Say which visuals come first in the tab order, or order by position")
    leaves = [n for n, v in page.visuals.items() if not is_group(v)]

    def current(name: str) -> tuple[float, int]:
        position = page.visuals[name].get("position") or {}
        order = position.get("tabOrder")
        number = _number(order) if order is not None else math.inf
        return number, leaves.index(name)

    rest = [n for n in leaves if n not in first]
    rest = _reading_order(page, rest) if op.by_position else sorted(rest, key=current)
    ordered = [*first, *rest]
    # A group's tab stop comes just before its first member's, outer groups first, and like
    # every other it is unique on the page (Desktop lists a group where its members are).
    firsts: dict[int, list[tuple[int, str]]] = {}
    for name, visual in page.visuals.items():
        if not is_group(visual):
            continue
        members = [ordered.index(m) for m in ordered if name in ancestors(page, m)]
        if members:
            firsts.setdefault(min(members), []).append((len(ancestors(page, name)), name))
    # Whole thousands from 0 like Desktop, from 1000 when a group needs a stop before the first.
    base = 1000 if 0 in firsts else 0
    changed = False
    for index, name in enumerate(ordered):
        position = page.visuals[name].setdefault("position", {})
        if position.get("tabOrder") != base + index * 1000:
            position["tabOrder"] = base + index * 1000
            document.touch_visual(page, name)
            changed = True
    for start, groups in firsts.items():
        groups.sort()
        for rank, (_, name) in enumerate(groups):
            wanted = base + start * 1000 - len(groups) + rank
            position = page.visuals[name].setdefault("position", {})
            if position.get("tabOrder") != wanted:
                position["tabOrder"] = wanted
                document.touch_visual(page, name)
                changed = True
    shown = [describe(page.visuals[n], page) for n in ordered[:4]]
    listing = ", ".join(f"{i}. {text}" for i, text in enumerate(shown, 1))
    more = f" and {len(ordered) - 4} more" if len(ordered) > 4 else ""
    how = " by position" if op.by_position and not first else ""
    summary = (
        f"Set the keyboard tab order of page '{page.display_name}'{how}: {listing}{more}."
        if changed
        else f"The tab order of page '{page.display_name}' was already {listing}{more}."
    )
    return HandlerResult(
        summary,
        visible_in=[
            _powerbi_only(
                op.op,
                "Tab order is the order keyboard and screen-reader users move through the page "
                "in Power BI; nothing changes on the canvas.",
            )
        ],
    )


def _apply_set_keep_layer_order(
    document: PbirDocument, op: SetKeepLayerOrder, model: ModelInfo
) -> HandlerResult:
    targets = _leaves(document, op.visuals)
    for page, visual in targets:
        _set(document, visual, "general", "keepLayerOrder", op.keep)
        document.touch_visual(page, _name(visual))
    labels = join_names([describe(v, p) for p, v in targets], 4)
    summary = (
        f"{sentence(labels)} now keep{'s' if len(targets) == 1 else ''} its place in the stack "
        "when clicked."
        if op.keep
        else f"{sentence(labels)} now come{'s' if len(targets) == 1 else ''} to the front when "
        "clicked, as Power BI does by default."
    )
    if op.keep and len(targets) > 1:
        summary = summary.replace(" its place", " their place")
    return HandlerResult(
        summary,
        visible_in=[
            _powerbi_only(op.op, "Layer order on click matters in Power BI's reading view only.")
        ],
    )


def _apply_set_page_view(
    document: PbirDocument, op: SetPageView, model: ModelInfo
) -> HandlerResult:
    page = resolve_page(document, op.page)
    value = _PAGE_VIEWS[op.view]
    words = _PAGE_VIEW_WORDS[value]
    if page.data.get("displayOption") == value:
        return HandlerResult(f"Page '{page.display_name}' was already set to {words}.")
    page.data["displayOption"] = value
    document.touch_page(page)
    return HandlerResult(
        f"Set the page view of page '{page.display_name}' to {words}.",
        visible_in=[
            _powerbi_only(op.op, "Page view changes how Power BI scales the page in its window.")
        ],
    )


def _own_name(projection: dict[str, Any]) -> str:
    """The name a field has in a visual before anyone renames it, as ``_field_projection``
    writes it: a column's or measure's name, an aggregated column's name, a level's name."""
    identity = field_identity(projection)
    if identity[0] in {"Column", "Measure"}:
        return identity[2]
    if identity[0] in {"Aggregation", "HierarchyLevel"}:
        return identity[3]
    query_ref = str(projection.get("queryRef") or "")
    return query_ref.rpartition(".")[2] or projection_label(projection)


def _apply_set_field_display_name(
    document: PbirDocument, op: SetFieldDisplayName, model: ModelInfo
) -> HandlerResult:
    page, visual = resolve_leaf(document, op.visual)
    label = describe(visual, page)
    found = [(r, p) for r, p in _find_fields(visual, op.field) if _calc_of(p) is None]
    if not found:
        raise DocumentError(
            f"{op.field!r} is not a field of {label}. Its fields: {_field_list(visual)}."
        )
    query_refs = {p.get("queryRef") for _, p in found}
    if len(query_refs) > 1:
        raise NeedsChoice(
            f"{op.field!r} matches {len(query_refs)} fields of {label}. Which one?",
            [Choice(projection_label(p), {"field": str(p.get("queryRef"))}) for _, p in found],
        )
    projections = [p for _, p in found]
    old = str(projections[0].get("nativeQueryRef") or projection_label(projections[0]))
    shown_old = old if old.strip() else projection_label(projections[0])
    if op.name is None or op.name == "":
        new_name = ""
    elif not op.name.strip():
        # A blank header: Desktop stores a single space (the Agents table's 'vs Previous Month').
        new_name = " "
    else:
        new_name = " ".join(op.name.split())
    if new_name == " " and any(_uses_name(c.expression, old) for c in visual_calculations(visual)):
        raise DocumentError(
            f"A visual calculation uses [{old}], and a blank header cannot be named in DAX; "
            "change the calculation first"
        )
    if not new_name:
        own = _own_name(projections[0])
        if all("displayName" not in p for p in projections) and old == own:
            return HandlerResult(
                f"The field {shown_old!r} in {label} already goes by its own name."
            )
        for projection in projections:
            projection.pop("displayName", None)
            projection["nativeQueryRef"] = own
        new_name = own
        summary = f"The field {shown_old!r} in {label} goes by its own name, {new_name!r}, again."
    else:
        others = {
            str(p.get("nativeQueryRef", "")).casefold(): str(p.get("nativeQueryRef"))
            for _, p in _role_projections(visual)
            if all(p is not q for q in projections)
        }
        native = new_name
        if new_name == " ":
            # Another blank header: the header text stays one space, the native name (which
            # Desktop keeps unique within a visual) takes one more space, as people do by hand.
            while native in others:
                native += " "
        elif new_name.casefold() in others:
            raise DocumentError(
                f"{sentence(label)} already has a field called {others[new_name.casefold()]!r}"
            )
        # Desktop writes the new name into displayName and nativeQueryRef and keeps queryRef
        # (the Agents table: 'MTD Sales' for '# Measures.Table MTD Sales').
        for projection in projections:
            projection["displayName"] = new_name
            projection["nativeQueryRef"] = native
        summary = (
            f"The column {shown_old!r} in {label} now has a blank header."
            if not new_name.strip()
            else f"The field {shown_old!r} in {label} is now called {new_name!r}."
        )
    updated = 0
    if old.strip() and new_name.strip() and old.casefold() != new_name.casefold():
        for _, projection in _role_projections(visual):
            calc = _calc_of(projection)
            if calc is None:
                continue
            text = str(calc.get("Expression", ""))
            if _uses_name(text, old):
                calc["Expression"] = _rename_in_expression(text, old, new_name)
                _sync_calc_sorts(visual, str(calc.get("Name")), calc)
                updated += 1
    if updated:
        summary += (
            " The visual calculation that uses it was updated."
            if updated == 1
            else f" The {updated} visual calculations that use it were updated."
        )
    document.touch_visual(page, _name(visual))
    return HandlerResult(summary)


# --- the family -----------------------------------------------------------------------------

EXTRAS_OPERATIONS = operation_classes(ExtrasOperation)
EXTRAS_HANDLERS: dict[str, Handler] = {
    "set_analytics_line": _apply_set_analytics_line,
    "remove_analytics_line": _apply_remove_analytics_line,
    "place_on_mobile": _apply_place_on_mobile,
    "remove_from_mobile": _apply_remove_from_mobile,
    "arrange_mobile_layout": _apply_arrange_mobile_layout,
    "group_visuals": _apply_group_visuals,
    "ungroup_visuals": _apply_ungroup_visuals,
    "set_group": _apply_set_group,
    "set_visual_calculation": _apply_set_visual_calculation,
    "remove_visual_calculation": _apply_remove_visual_calculation,
    "set_small_multiples": _apply_set_small_multiples,
    "set_alt_text": _apply_set_alt_text,
    "set_tab_order": _apply_set_tab_order,
    "set_keep_layer_order": _apply_set_keep_layer_order,
    "set_page_view": _apply_set_page_view,
    "set_field_display_name": _apply_set_field_display_name,
}
EXTRAS_ADAPTER: TypeAdapter[Any] = TypeAdapter(
    Annotated[ExtrasOperation, Field(discriminator="op")]
)


def parse_extras(raw: Sequence[Mapping[str, Any]]) -> list[Any]:
    """Operation dicts of this family into typed operations (``report_edits.parse_operations``
    does the same once the family is registered)."""
    parsed: list[Any] = []
    for index, item in enumerate(raw, start=1):
        try:
            parsed.append(EXTRAS_ADAPTER.validate_python(item))
        except ValidationError as error:
            first = error.errors(include_url=False)[0]
            where = ".".join(str(p) for p in first["loc"][1:]) or "the step"
            raise DocumentError(
                f"Step {index} ({item.get('op')}): {where}: {first['msg']}"
            ) from error
    return parsed


def apply_extras(
    document: PbirDocument, operations: Sequence[Any], model: ModelInfo
) -> EditOutcome:
    """Apply this family's operations in order, as ``report_edits.apply_operations`` does: the
    first failure raises and the caller discards the document."""
    outcome = EditOutcome()
    for index, operation in enumerate(operations):
        handler = EXTRAS_HANDLERS[operation.op]
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
            outcome.lineage.update(result.lineage)
            outcome.partial = outcome.partial or result.partial
        else:
            outcome.summaries.append(result)
        outcome.report_changed = True
    return outcome


# --- shapes: what a Desktop-authored file confirms, and what the Desktop probes must -----------

VERIFIED_SHAPES: dict[str, str] = {
    "group.container": (
        "visual.json with visualGroup {displayName, groupMode 'ScaleMode'} and a position, members "
        "with parentGroupName and positions relative to the group (exp6: Desktop drew and saved it "
        "unchanged)"
    ),
    "field.display_name": (
        "projection displayName and nativeQueryRef both set to the new name, queryRef unchanged "
        "(the Agents table: 'MTD Sales' on '# Measures.Table MTD Sales')"
    ),
    "position.tabOrder": "whole numbers 1000 apart, as the Agents page has them",
    "page.displayOption.FitToPage": "the Agents page",
    "container.general.keepLayerOrder": (
        "visualContainerObjects.general keepLayerOrder (the Agents card 2202e311b1493069e4eb; the "
        "base theme CY24SU02 sets it for shapes)"
    ),
}
# Not yet seen in a file Desktop saved. "Desktop's code" means Power BI Desktop 2.157's own
# shipped JavaScript (desktopDataExplore: the Analytics pane's writers and the object serializer),
# read to take the shape; a Desktop probe still has to show it kept.
UNVERIFIED_SHAPES: dict[str, str] = {
    "analytics.constant_line": (
        "y1AxisReferenceLine entry {selector {id: '<n>'}, value 'nD', show, displayName "
        "'Y-Axis Constant Line <n>', lineColor, style, dataLabel*, shade*}: ids count from 1 and "
        "names take the next free number as Desktop's code does"
    ),
    "analytics.constant_from_measure": "a constant line's value as {expr: {Measure}} (the fx)",
    "analytics.x_constant_line": "xAxisReferenceLine entry, value 'nD' or datetime'...'",
    "analytics.dynamic_lines": (
        "referenceLine entries: value {Aggregation {Expression {SelectRef {ExpressionName: "
        "queryRef}}, Function 1 average / 3 min / 4 max / 6 median}}, selector {metadata: "
        "queryRef, id: '<n>'} (Desktop's code builds exactly these)"
    ),
    "analytics.percentile_line": (
        "value {Percentile {Expression {SelectRef}, K: p / 100}}, Exclusive left out when false "
        "and 90 by default (Desktop's code)"
    ),
    "analytics.trend": "one selector-less trend entry named 'Trend line 1' (Desktop's code)",
    "analytics.forecast": (
        "forecast entry {selector {metadata: <the value>, id: '1'}, transform {algorithm "
        "'Forecast', parameters [{Literal, Name} for Unit 7L, ForecastLength 10L, IgnoreLast 0L, "
        "ConfidenceLevel 0.95D, MaxSeasonality nL when set]}, show, displayName 'Forecast 1'} "
        "(Desktop's AnalyticsAuthoringService and serializer)"
    ),
    "analytics.error_bars": (
        "error entry {selector {metadata: <the value>}} with errorRange {kind 'ErrorRange', "
        "explicit {isRelative {expr false}, lowerBound {expr}, upperBound {expr}}} (the range is "
        "Desktop's serializer; the per-value selector is not confirmed) and "
        "barMatchSeriesColor/shadeMatchSeriesColor false when a color is given"
    ),
    "analytics.ratio_line": "one selector-less ratioLine entry (scatter)",
    "analytics.symmetry_shading": "one selector-less plotAreaShading entry (scatter)",
    "mobile.json": (
        "{$schema visualContainerMobileState/2.7.0 (the version Desktop 2.157 ships), position "
        "{x, y, z, height, width, tabOrder}} on a 320-pixel canvas"
    ),
    "group.scroll_mode": "groupMode 'ScrollMode' (schema only; exp6 measured 'ScaleMode')",
    "group.nested": "a group inside a group (the multipage fixture's are hand-built)",
    "group.z_and_tab_order": (
        "a new group takes its lowest member's z and a tab stop of its own just before its first "
        "member's"
    ),
    "visual_calculation": (
        "projection {field {NativeVisualCalculation {Language 'dax', Expression, Name}}, queryRef "
        "and nativeQueryRef = the name} (semanticQuery 1.4.0 schema; pbi-cli writes queryRef "
        "'select')"
    ),
    "small_multiples": (
        "a column projection in the small-multiples role (Rows), smallMultiplesLayout layoutType "
        "'custom' / rowCount nL / columnCount nL, valueAxis.sharedAxis, subheader.show"
    ),
    "field.blank_header_again": (
        "a second blank header in one visual: displayName ' ' (as the Agents table's 'vs PM'), "
        "nativeQueryRef padded to two or more spaces so it stays unique (Desktop's code keeps "
        "native names unique when it adds a field; its rename was not traced)"
    ),
    "container.general.altText": "visualContainerObjects.general altText string",
    "page.displayOption.FitToWidth_ActualSize": "schema values, not seen in a Desktop file",
}


# --- tool cards --------------------------------------------------------------------------------

# Visuals of the Agents dashboard (and of fixtures/imported/structure-twin.pbix, which keeps them).
_TABLE = "e82e02f93055d6b0b544"
_CHART_SALES = "b404a461ede7604a2774"  # 'Total MTD Sales', stacked column chart
_CHART_AVG = "da32cdbb0090e885649b"  # 'Avg MTD Sales by Agents'
_CHART_CHANGE = "ab13db00c71dcec86e99"  # 'Avg MTD % change by Agents'
_CHART_DISCOUNT = "97d1504080b699eae7d7"  # 'Avg Discount per Transaction'
_CARDS = (
    "fb4c5aa34e0b084b62b9",  # 'Agents with Positive Change %'
    "2202e311b1493069e4eb",  # 'Agents Avg MTD Sales'
    "9479353528b0eb22b958",  # 'Agents Avg MTD % Change'
    "71a164765d9c63200302",  # 'Number of Agents Sold above 100K'
)
_CARD_TOPN = "8a8ba163374a72762030"  # the small 'TopN/BottomN' card
_SLICERS = (
    "b85aa81a8b5c60030d13",  # Channel
    "54be5cf0783e66471540",  # 'Country'
    "5b0dbccd61c6c4adb415",  # Store
)


# The capability-matrix entry (edit_capabilities) each operation provides.
_CAPABILITIES: dict[str, str] = {
    "set_analytics_line": "analytics_lines",
    "remove_analytics_line": "analytics_lines",
    "place_on_mobile": "mobile_layout",
    "remove_from_mobile": "mobile_layout",
    "arrange_mobile_layout": "mobile_layout",
    "group_visuals": "group_align_distribute",
    "ungroup_visuals": "group_align_distribute",
    "set_group": "group_align_distribute",
    "set_visual_calculation": "visual_calculations",
    "remove_visual_calculation": "visual_calculations",
    "set_small_multiples": "small_multiples",
    "set_alt_text": "alt_text",
    "set_tab_order": "alt_text",
    "set_keep_layer_order": "layer_order",
    "set_page_view": "page_view",
    "set_field_display_name": "field_display_names",
}
_ANALYTICS_TYPES = tuple(sorted(t for t in expr.visual_types() if analytics_kinds(t)))
_VISUAL_TYPES: dict[str, tuple[str, ...]] = {
    "set_analytics_line": _ANALYTICS_TYPES,
    "remove_analytics_line": _ANALYTICS_TYPES,
    "set_small_multiples": tuple(
        sorted(t for t in expr.visual_types() if _small_multiples_role(t) is not None)
    ),
}


def _card(
    op: str,
    summary: str,
    slots: tuple[Slot, ...],
    cues: tuple[str, ...],
    examples: tuple[tuple[str, Mapping[str, Any]], ...],
    **extra: Any,
) -> ToolCard:
    return ToolCard(
        op,
        FAMILY,
        summary,
        slots,
        cues,
        examples,
        capability=_CAPABILITIES[op],
        visual_types=_VISUAL_TYPES.get(op, ()),
        **extra,
    )


def _remove_line_operation(filled: Mapping[str, Any]) -> dict[str, Any]:
    """ "Clear all analytics lines": the words' "all" is a choice, not an on/off (a bool slot
    would read "clear" as off)."""
    operation: dict[str, Any] = {"op": "remove_analytics_line", "visual": filled["visual"]}
    operation.update({name: filled[name] for name in ("kind", "line") if name in filled})
    if filled.get("scope"):
        operation["all"] = True
    return operation


def _remove_mobile_operation(filled: Mapping[str, Any]) -> dict[str, Any]:
    """ "All" clears the page only when no visual is named ("take all the slicers off the phone
    layout" takes the slicers)."""
    operation: dict[str, Any] = {"op": "remove_from_mobile"}
    operation.update({name: filled[name] for name in ("visuals", "page") if name in filled})
    if filled.get("scope") and not filled.get("visuals"):
        operation["all"] = True
    return operation


def _small_multiples_operation(filled: Mapping[str, Any]) -> dict[str, Any]:
    """ "Turn off the small multiples" / "remove the small multiples" switch them off: the on/off
    of the words is small multiples' own (the ``remove`` field would take "remove" as off)."""
    if filled.get("small_multiples") is False:
        return {"op": "set_small_multiples", "visual": filled["visual"], "remove": True}
    names = ("visual", "field", "rows", "columns", "shared_y_axis", "titles")
    return {"op": "set_small_multiples", **{n: filled[n] for n in names if n in filled}}


_KIND_CHOICES: tuple[str, ...] = tuple(_KIND_WORDS)
TOOL_CARDS: tuple[ToolCard, ...] = (
    _card(
        "set_analytics_line",
        "Add or change a line from the Analytics pane of a chart: a constant (target) line, min, "
        "max, average, median or percentile line, trend line, forecast, error bars, or a "
        "scatter's ratio line and symmetry shading.",
        (
            Slot("visual", "visual", "The chart"),
            Slot("kind", "enum", "Which analytics line", choices=_KIND_CHOICES),
            Slot("value", "number", "Where a constant line goes", required=False),
            Slot(
                "date",
                "text",
                "The date an X-axis constant line marks, YYYY-MM-DD",
                required=False,
                field="value",
            ),
            Slot(
                "measure",
                "measure",
                "The value the line follows (a field of the chart), or the measure a constant "
                "line takes its value from",
                required=False,
            ),
            Slot("upper", "measure", "The measure error bars run up to", required=False),
            Slot("lower", "measure", "The measure error bars run down to", required=False),
            Slot("percentile", "number", "A percentile line's percentile", False, None, (), 0, 100),
            Slot("name", "text", "The line's name", required=False),
            Slot("color", "color", "The line's color", required=False),
            Slot("style", "enum", "Line style", False, None, ("solid", "dashed", "dotted")),
            Slot("width", "number", "Line width", False, None, (), 0.5, 20),
            Slot("transparency", "number", "Line transparency (%)", False, None, (), 0, 100),
            Slot("data_label", "bool", "Show the line's value on the chart", required=False),
            Slot(
                "label_text",
                "enum",
                "What the line's label says",
                False,
                None,
                ("value", "name", "value_and_name"),
            ),
            Slot(
                "shade",
                "enum",
                "Shade the area before or after the line",
                False,
                None,
                ("none", "before", "after"),
            ),
            Slot("forecast_length", "integer", "Points to forecast", False, None, (), 1, 10000),
            Slot("line", "text", "The name of an existing line to change", required=False),
        ),
        (
            "target line",
            "constant line",
            "reference line",
            "average line",
            "min line",
            "max line",
            "median line",
            "percentile line",
            "trend line",
            "forecast",
            "error bars",
            "ratio line",
            "symmetry shading",
            "analytics pane",
        ),
        (
            (
                "add a dashed red target line at 50,000 to the Total MTD Sales chart",
                {
                    "op": "set_analytics_line",
                    "visual": _CHART_SALES,
                    "kind": "constant",
                    "value": 50000,
                    "name": "Target",
                    "color": "#D64550",
                    "style": "dashed",
                },
            ),
            (
                "add a trend line to the Avg MTD Sales by Agents chart",
                {"op": "set_analytics_line", "visual": _CHART_AVG, "kind": "trend"},
            ),
            (
                "draw a constant line at 10,000 on the discount chart and show its value",
                {
                    "op": "set_analytics_line",
                    "visual": _CHART_DISCOUNT,
                    "kind": "constant",
                    "value": 10000,
                    "data_label": True,
                },
            ),
            (
                "make the trend line on the Avg MTD % change chart grey and dotted",
                {
                    "op": "set_analytics_line",
                    "visual": _CHART_CHANGE,
                    "kind": "trend",
                    "color": "#808080",
                    "style": "dotted",
                },
            ),
            (
                "add error bars to the Total MTD Sales chart from Profit up to PY Total Sales",
                {
                    "op": "set_analytics_line",
                    "visual": _CHART_SALES,
                    "kind": "error_bars",
                    "upper": "PY Total Sales",
                    "lower": "Profit",
                },
            ),
        ),
    ),
    _card(
        "remove_analytics_line",
        "Remove analytics lines from a chart: one by name, every line of a kind, or all of them.",
        (
            Slot("visual", "visual", "The chart"),
            Slot("kind", "enum", "Which kind of line", False, None, _KIND_CHOICES),
            Slot("line", "text", "The name of the line", required=False),
            Slot(
                "scope",
                "enum",
                "'all' removes every analytics line (every line of the kind, when one is named)",
                False,
                None,
                ("all", "every"),
            ),
        ),
        ("remove the line", "delete the target line", "remove the trend line", "clear analytics"),
        (
            (
                "remove the target line from the Total MTD Sales chart",
                {"op": "remove_analytics_line", "visual": _CHART_SALES, "line": "Target"},
            ),
            (
                "take the trend line off the Avg MTD Sales by Agents chart",
                {"op": "remove_analytics_line", "visual": _CHART_AVG, "kind": "trend"},
            ),
            (
                "clear all analytics lines from the Total MTD Sales chart",
                {"op": "remove_analytics_line", "visual": _CHART_SALES, "all": True},
            ),
        ),
        build=_remove_line_operation,
    ),
    _card(
        "place_on_mobile",
        "Put a visual on its page's phone layout, or move or resize it there (phone canvas "
        "pixels, 320 wide).",
        (
            Slot("visual", "visual", "The visual"),
            Slot("x", "number", "Left edge on the phone canvas", False, None, (), 0, MOBILE_WIDTH),
            Slot("y", "number", "Top edge on the phone canvas", False, None, (), 0, 20000),
            Slot("width", "number", "Width on the phone", False, None, (), 20, MOBILE_WIDTH),
            Slot("height", "number", "Height on the phone", False, None, (), 20, 5000),
        ),
        ("mobile layout", "phone layout", "on mobile", "on the phone", "mobile view"),
        (
            (
                "put the Store slicer at the top of the phone layout",
                {
                    "op": "place_on_mobile",
                    "visual": _SLICERS[2],
                    "x": 0,
                    "y": 0,
                    "width": 320,
                    "height": 60,
                },
            ),
            (
                "add the Total MTD Sales chart to the mobile layout",
                {"op": "place_on_mobile", "visual": _CHART_SALES},
            ),
            (
                "make the agents table 400 pixels tall on the phone",
                {"op": "place_on_mobile", "visual": _TABLE, "height": 400},
            ),
        ),
    ),
    _card(
        "remove_from_mobile",
        "Take visuals off the phone layout, or clear a page's phone layout.",
        (
            Slot("visuals", "visuals", "The visuals to take off", required=False),
            Slot("page", "page", "The page whose whole phone layout to clear", required=False),
            Slot(
                "scope",
                "enum",
                "'all' clears the page's whole phone layout (no visuals named)",
                False,
                None,
                ("all", "every", "whole"),
            ),
        ),
        ("remove from mobile", "off the phone layout", "clear the mobile layout"),
        (
            (
                "remove the table from the phone layout",
                {"op": "remove_from_mobile", "visuals": [_TABLE]},
            ),
            (
                "take the slicers off the mobile layout",
                {"op": "remove_from_mobile", "visuals": list(_SLICERS)},
            ),
            (
                "clear the whole phone layout of page 1",
                {"op": "remove_from_mobile", "page": "1", "all": True},
            ),
        ),
        build=_remove_mobile_operation,
    ),
    _card(
        "arrange_mobile_layout",
        "Build a page's phone layout automatically: its visuals in reading order on a grid of "
        "one to three columns (wide visuals take a whole row).",
        (
            Slot("page", "page", "The page"),
            Slot("columns", "integer", "Columns on the phone", False, None, (), 1, 3),
            Slot("visuals", "visuals", "Only these visuals, in this order", required=False),
        ),
        ("mobile layout", "phone layout", "arrange for mobile", "make it work on phones"),
        (
            ("build a phone layout for page 1", {"op": "arrange_mobile_layout", "page": "1"}),
            (
                "arrange the mobile layout of page 1 in one column",
                {"op": "arrange_mobile_layout", "page": "1", "columns": 1},
            ),
            (
                "phone layout with only the four cards and the table",
                {
                    "op": "arrange_mobile_layout",
                    "page": "1",
                    "visuals": [*_CARDS, _TABLE],
                },
            ),
        ),
    ),
    _card(
        "group_visuals",
        "Group visuals (or groups) into a new group; nothing moves on the page.",
        (
            Slot("visuals", "visuals", "The visuals to group (two or more)"),
            Slot("name", "text", "The group's name", required=False),
            Slot("mode", "enum", "How the group resizes", False, None, ("scale", "scroll")),
        ),
        (
            "group these",
            "group them",
            "group the",
            "group together",
            "make a group",
            "into a group",
        ),
        (
            (
                "group the four cards and call the group KPI cards",
                {"op": "group_visuals", "visuals": list(_CARDS), "name": "KPI cards"},
            ),
            (
                "group the two sales charts",
                {
                    "op": "group_visuals",
                    "visuals": [_CHART_SALES, _CHART_AVG],
                    "name": "Sales charts",
                },
            ),
            (
                "group the slicers so they keep their size and scroll",
                {
                    "op": "group_visuals",
                    "visuals": list(_SLICERS),
                    "name": "Filters",
                    "mode": "scroll",
                },
            ),
        ),
    ),
    _card(
        "ungroup_visuals",
        "Dissolve a group; its visuals stay where they are.",
        (Slot("group", "visual", "The group"),),
        ("ungroup", "break up the group", "remove the group"),
        (
            ("ungroup the KPI cards", {"op": "ungroup_visuals", "group": "KPI cards"}),
            ("break up the Sales charts group", {"op": "ungroup_visuals", "group": "Sales charts"}),
            ("ungroup the Filters", {"op": "ungroup_visuals", "group": "Filters"}),
        ),
    ),
    _card(
        "set_group",
        "Rename a group, or set whether it scales its visuals or keeps their size and scrolls.",
        (
            Slot("group", "visual", "The group"),
            Slot("name", "text", "The new name", required=False),
            Slot("mode", "enum", "How the group resizes", False, None, ("scale", "scroll")),
        ),
        ("rename the group", "group mode", "scale mode", "scroll mode"),
        (
            (
                "rename the KPI cards group to Headline numbers",
                {"op": "set_group", "group": "KPI cards", "name": "Headline numbers"},
            ),
            (
                "make the Filters group scroll instead of scaling",
                {"op": "set_group", "group": "Filters", "mode": "scroll"},
            ),
            (
                "set the Sales charts group to scale its visuals",
                {"op": "set_group", "group": "Sales charts", "mode": "scale"},
            ),
        ),
    ),
    _card(
        "set_visual_calculation",
        "Add or change a visual calculation (DAX on the visual's own data): running sum, moving "
        "average, percent of parent or grand total, versus previous, or any expression over the "
        "visual's fields.",
        (
            Slot("visual", "visual", "The visual"),
            Slot("name", "text", "The calculation's name", required=False),
            Slot(
                "template",
                "enum",
                "A ready-made calculation",
                False,
                None,
                tuple(_TEMPLATES),
            ),
            Slot(
                "field",
                "text",
                "The field the template uses, named as the visual shows it",
                required=False,
            ),
            Slot("window", "integer", "Points in a moving average", False, None, (), 2, 100),
            Slot("expression", "dax", "The DAX, fields in brackets", required=False),
            Slot("new_name", "text", "A new name for an existing calculation", required=False),
        ),
        (
            "visual calculation",
            "running total",
            "running sum",
            "moving average",
            "percent of total",
            "percent of parent",
            "versus previous",
        ),
        (
            (
                "add a running total of MTD Sales to the agents table",
                {
                    "op": "set_visual_calculation",
                    "visual": _TABLE,
                    "name": "Running MTD Sales",
                    "template": "running_sum",
                    "field": "MTD Sales",
                },
            ),
            (
                "add each agent's share of total MTD Sales to the table",
                {
                    "op": "set_visual_calculation",
                    "visual": _TABLE,
                    "name": "Share of MTD Sales",
                    "template": "percent_of_grand_total",
                    "field": "MTD Sales",
                },
            ),
            (
                "add a calculation Double rank = [Rank] * 2 to the table",
                {
                    "op": "set_visual_calculation",
                    "visual": _TABLE,
                    "name": "Double rank",
                    "expression": "[Rank] * 2",
                },
            ),
            (
                "add a 3-point moving average to the Total MTD Sales chart",
                {
                    "op": "set_visual_calculation",
                    "visual": _CHART_SALES,
                    "template": "moving_average",
                    "field": "TopN/BottomN/ MTD Sales",
                    "window": 3,
                },
            ),
        ),
    ),
    _card(
        "remove_visual_calculation",
        "Remove a visual calculation from a visual.",
        (Slot("visual", "visual", "The visual"), Slot("name", "text", "The calculation's name")),
        ("remove the calculation", "delete the running total", "remove the moving average"),
        (
            (
                "remove the running total from the table",
                {"op": "remove_visual_calculation", "visual": _TABLE, "name": "Running MTD Sales"},
            ),
            (
                "delete the Share of MTD Sales calculation",
                {
                    "op": "remove_visual_calculation",
                    "visual": _TABLE,
                    "name": "Share of MTD Sales",
                },
            ),
            (
                "remove the moving average from the Total MTD Sales chart",
                {
                    "op": "remove_visual_calculation",
                    "visual": _CHART_SALES,
                    "name": "Moving average",
                },
            ),
        ),
    ),
    _card(
        "set_small_multiples",
        "Split a chart into small multiples by a column, set the grid (rows, columns), a shared "
        "y-axis and titles, or turn small multiples off.",
        (
            Slot("visual", "visual", "The chart"),
            Slot("field", "column", "The column to split by", required=False),
            Slot("rows", "integer", "Rows in the grid", False, None, (), 1, 6),
            Slot("columns", "integer", "Columns in the grid", False, None, (), 1, 6),
            Slot("shared_y_axis", "bool", "One y-axis for all charts", required=False),
            Slot("titles", "bool", "A title above each small chart", required=False),
            Slot(
                "small_multiples",
                "bool",
                "Small multiples on; false turns them off",
                required=False,
            ),
        ),
        ("small multiples", "split the chart by", "one chart per", "trellis"),
        (
            (
                "split the Total MTD Sales chart by channel",
                {
                    "op": "set_small_multiples",
                    "visual": _CHART_SALES,
                    "field": {"table": "DimChannel", "name": "ChannelName"},
                },
            ),
            (
                "small multiples by store in a 2 by 3 grid on the Avg MTD Sales chart",
                {
                    "op": "set_small_multiples",
                    "visual": _CHART_AVG,
                    "field": {"table": "DimStore", "name": "StoreName"},
                    "rows": 2,
                    "columns": 3,
                },
            ),
            (
                "split the discount chart by channel with one shared y-axis",
                {
                    "op": "set_small_multiples",
                    "visual": _CHART_DISCOUNT,
                    "field": {"table": "DimChannel", "name": "ChannelName"},
                    "shared_y_axis": True,
                },
            ),
            (
                "turn off the small multiples on the Total MTD Sales chart",
                {"op": "set_small_multiples", "visual": _CHART_SALES, "remove": True},
            ),
        ),
        build=_small_multiples_operation,
    ),
    _card(
        "set_alt_text",
        "Set or remove the alt text a screen reader reads for a visual.",
        (
            Slot("visual", "visual", "The visual"),
            Slot("text", "text", "The description; empty removes it"),
        ),
        ("alt text", "screen reader", "accessibility description"),
        (
            (
                "set the alt text of the agents table to Agents ranked by MTD sales",
                {"op": "set_alt_text", "visual": _TABLE, "text": "Agents ranked by MTD sales"},
            ),
            (
                "describe the Total MTD Sales chart for screen readers as MTD sales of the top, "
                "bottom and other agents",
                {
                    "op": "set_alt_text",
                    "visual": _CHART_SALES,
                    "text": "MTD sales of the top, bottom and other agents",
                },
            ),
            (
                "remove the alt text of the table",
                {"op": "set_alt_text", "visual": _TABLE, "text": ""},
            ),
        ),
    ),
    _card(
        "set_tab_order",
        "Set the order keyboard users move through a page: some visuals first, or by position.",
        (
            Slot("visuals", "visuals", "The visuals that come first, in order", required=False),
            Slot("page", "page", "The page", required=False),
            Slot("by_position", "bool", "Top to bottom, left to right", required=False),
        ),
        ("tab order", "keyboard order", "focus order", "reading order"),
        (
            (
                "make the slicers the first tab stops",
                {"op": "set_tab_order", "visuals": list(_SLICERS)},
            ),
            (
                "set the tab order of page 1 by position",
                {"op": "set_tab_order", "page": "1", "by_position": True},
            ),
            (
                "put the table first in the keyboard order",
                {"op": "set_tab_order", "visuals": [_TABLE]},
            ),
        ),
    ),
    _card(
        "set_keep_layer_order",
        "Maintain layer order: a visual keeps its place in the stack when clicked in reading view.",
        (
            Slot("visuals", "visuals", "The visuals"),
            Slot(
                "layer_order",
                "bool",
                "Keep the layer order when clicked (false lets it come to the front)",
                required=False,
                field="keep",
            ),
        ),
        ("maintain layer order", "keep its layer", "layer order when clicked"),
        (
            (
                "keep the TopN/BottomN card's layer order when people click it",
                {"op": "set_keep_layer_order", "visuals": [_CARD_TOPN], "keep": True},
            ),
            (
                "maintain layer order on the slicers",
                {"op": "set_keep_layer_order", "visuals": list(_SLICERS)},
            ),
            (
                "stop maintaining layer order on the TopN/BottomN card",
                {"op": "set_keep_layer_order", "visuals": [_CARD_TOPN], "keep": False},
            ),
        ),
    ),
    _card(
        "set_page_view",
        "Set how Power BI scales a page in the window: fit to page, fit to width or actual size.",
        (
            Slot("page", "page", "The page"),
            Slot("view", "enum", "The page view", choices=tuple(_PAGE_VIEWS)),
        ),
        ("page view", "fit to width", "fit to page", "actual size"),
        (
            ("fit page 1 to width", {"op": "set_page_view", "page": "1", "view": "fit_to_width"}),
            (
                "show page 1 at actual size",
                {"op": "set_page_view", "page": "1", "view": "actual_size"},
            ),
            (
                "make page 1 fit the window",
                {"op": "set_page_view", "page": "1", "view": "fit_to_page"},
            ),
        ),
    ),
    _card(
        "set_field_display_name",
        "Rename a field for one visual only: a table's column header, an axis or a legend entry "
        "(a single space is a blank header), or reset it to the field's own name.",
        (
            Slot("visual", "visual", "The visual"),
            Slot("field", "text", "The field as the visual shows it (its current header)"),
            Slot("name", "text", "The new name; empty resets it, a single space leaves it blank"),
        ),
        (
            "rename the column header",
            "rename the column",
            "column header",
            "header text",
            "change the header",
            "call the column",
            "rename the field",
            "rename for this visual",
            "display name",
        ),
        (
            (
                "rename MTD Sales to Sales this month in the agents table",
                {
                    "op": "set_field_display_name",
                    "visual": _TABLE,
                    "field": "MTD Sales",
                    "name": "Sales this month",
                },
            ),
            (
                "call the 'MTD Sales' column 'Sales this month'",
                {
                    "op": "set_field_display_name",
                    "visual": _TABLE,
                    "field": "MTD Sales",
                    "name": "Sales this month",
                },
            ),
            (
                "change the header text of 'vs PM %' to 'Change vs last month'",
                {
                    "op": "set_field_display_name",
                    "visual": _TABLE,
                    "field": "vs PM %",
                    "name": "Change vs last month",
                },
            ),
            (
                "make the Rank column header blank",
                {"op": "set_field_display_name", "visual": _TABLE, "field": "Rank", "name": " "},
            ),
            (
                "call the Clustered Employees axis Agent group on the Total MTD Sales chart",
                {
                    "op": "set_field_display_name",
                    "visual": _CHART_SALES,
                    "field": "Clustered Employees",
                    "name": "Agent group",
                },
            ),
            (
                "reset the name of Agent ID in the table",
                {"op": "set_field_display_name", "visual": _TABLE, "field": "Agent ID", "name": ""},
            ),
        ),
    ),
)


# --- Desktop probes ------------------------------------------------------------------------------

_PROBE_PAGE = "Probe extras"
_AGENTS_PAGE = "ReportSection8454c812b60b8bb83b99"  # the Agents dashboard's page "1"
_AGENTS_MEASURE = "# Measures.TopN/BottomN/ MTD Sales"  # the value of 'Total MTD Sales'
# Page positions of the Agents visuals the group probes move in and out of groups.
_CARD_ORIGINS = {
    _CARDS[0]: (173.0, 154.0),
    _CARDS[1]: (612.0, 154.0),
    _CARDS[2]: (1051.0, 154.0),
    _CARDS[3]: (1490.0, 154.0),
}
_SLICER_ORIGINS = {
    _SLICERS[0]: (1120.8955223880596, 40.0),
    _SLICERS[1]: (1394.4477611940297, 40.0),
    _SLICERS[2]: (1668.0, 40.0),
}


def _saved_visuals(parts: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """name -> visual.json of every visual in the saved file."""
    found: dict[str, dict[str, Any]] = {}
    for part, data in parts.items():
        match = re.search(r"/visuals/([^/]+)/visual\.json$", part)
        if match and isinstance(data, dict):
            found[match.group(1)] = data
    return found


def _saved_caption(visual: Mapping[str, Any]) -> str | None:
    titles = visual.get("visual", {}).get("visualContainerObjects", {}).get("title", [])
    for entry in titles if isinstance(titles, list) else []:
        text = expr.decode((entry.get("properties") or {}).get("text"))
        if isinstance(text, str):
            return text
    return None


def _find_saved(parts: Mapping[str, Any], ref: str) -> dict[str, Any] | None:
    visuals = _saved_visuals(parts)
    if ref in visuals:
        return visuals[ref]
    return next((v for v in visuals.values() if _saved_caption(v) == ref), None)


Check = Callable[[Mapping[str, Any]], list[str]]


def _lit(value: str) -> dict[str, Any]:
    return {"expr": {"Literal": {"Value": value}}}


def _fill(hex_value: str) -> dict[str, Any]:
    return {"solid": {"color": _lit(f"'{hex_value}'")}}


def _aggregate_of(query_ref: str, function: int) -> dict[str, Any]:
    select = {"SelectRef": {"ExpressionName": query_ref}}
    return {"expr": {"Aggregation": {"Expression": select, "Function": function}}}


def _percentile_of(query_ref: str, k: float) -> dict[str, Any]:
    select = {"SelectRef": {"ExpressionName": query_ref}}
    return {"expr": {"Percentile": {"Expression": select, "K": k}}}


def _all_of(*checks: Check) -> Check:
    def check(parts: Mapping[str, Any]) -> list[str]:
        return [problem for one in checks for problem in one(parts)]

    return check


def _saved_objects(parts: Mapping[str, Any], ref: str, where: str) -> dict[str, Any] | None:
    visual = _find_saved(parts, ref)
    if visual is None:
        return None
    found = visual.get("visual", {}).get(where)
    return found if isinstance(found, dict) else {}


def _entries_are(
    ref: str,
    object_name: str,
    expected: Sequence[tuple[dict[str, Any] | None, Mapping[str, Any]]],
    where: str = "objects",
) -> Check:
    """Desktop kept exactly these entries of the object, in order: each one's selector, and at
    least these properties with these very expressions (content, not a count)."""

    def check(parts: Mapping[str, Any]) -> list[str]:
        objects = _saved_objects(parts, ref, where)
        if objects is None:
            return [f"The saved file has no visual {ref!r}"]
        entries = objects.get(object_name)
        entries = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
        problems = []
        if len(entries) != len(expected):
            problems.append(
                f"{ref}: {object_name} has {len(entries)} entries, Studio wrote {len(expected)}"
            )
        for index, (entry, (selector, props)) in enumerate(zip(entries, expected, strict=False)):
            if (entry.get("selector") or None) != selector:
                problems.append(
                    f"{ref}: {object_name}[{index}] selector is {entry.get('selector')!r}, "
                    f"Studio wrote {selector!r}"
                )
            saved = entry.get("properties") or {}
            for prop, value in props.items():
                if saved.get(prop) != value:
                    problems.append(
                        f"{ref}: {object_name}[{index}].{prop} is {saved.get(prop)!r}, Studio "
                        f"wrote {value!r}"
                    )
        return problems

    return check


def _has_properties(ref: str, object_name: str, props: Mapping[str, Any], where: str) -> Check:
    """Some entry of the object holds these properties with these expressions."""

    def check(parts: Mapping[str, Any]) -> list[str]:
        objects = _saved_objects(parts, ref, where)
        if objects is None:
            return [f"The saved file has no visual {ref!r}"]
        merged: dict[str, Any] = {}
        for entry in objects.get(object_name) or []:
            merged.update((entry.get("properties") or {}) if isinstance(entry, dict) else {})
        return [
            f"{ref}: {object_name}.{prop} is {merged.get(prop)!r}, Studio wrote {value!r}"
            for prop, value in props.items()
            if merged.get(prop) != value
        ]

    return check


def _saved_page_position(visuals: Mapping[str, Any], name: str) -> tuple[float, float]:
    x = y = 0.0
    seen: set[str] = set()
    current: str | None = name
    while current is not None and current in visuals and current not in seen:
        seen.add(current)
        position = visuals[current].get("position") or {}
        x += _number(position.get("x"))
        y += _number(position.get("y"))
        parent = visuals[current].get("parentGroupName")
        current = parent if isinstance(parent, str) else None
    return x, y


def _positions_kept(origins: Mapping[str, tuple[float, float]], group: str | None) -> Any:
    """A check that the visuals sit at their original page positions, inside the group with
    that display name (or in no group when ``group`` is None)."""

    def check(parts: Mapping[str, Any]) -> list[str]:
        visuals = _saved_visuals(parts)
        problems: list[str] = []
        groups = {
            n
            for n, v in visuals.items()
            if isinstance(v.get("visualGroup"), dict)
            and v["visualGroup"].get("displayName") == group
        }
        for name, (x, y) in origins.items():
            if name not in visuals:
                problems.append(f"The saved file lost visual {name}")
                continue
            parent = visuals[name].get("parentGroupName")
            if group is None and parent is not None:
                problems.append(f"{name} is still in a group")
            if group is not None and parent not in groups:
                problems.append(f"{name} is not in the group {group!r}")
            px, py = _saved_page_position(visuals, name)
            if abs(px - x) > 0.5 or abs(py - y) > 0.5:
                problems.append(f"{name} moved from ({x}, {y}) to ({px}, {py})")
        return problems

    return check


def _group_is(display_name: str, mode: str, inside: str | None, members: int) -> Check:
    """Desktop kept a group by that name, its mode, its place in another group and its size."""

    def check(parts: Mapping[str, Any]) -> list[str]:
        visuals = _saved_visuals(parts)
        names = {
            n: v["visualGroup"].get("displayName")
            for n, v in visuals.items()
            if isinstance(v.get("visualGroup"), dict)
        }
        found = [n for n, shown in names.items() if shown == display_name]
        if len(found) != 1:
            return [f"The saved file has {len(found)} groups called {display_name!r}"]
        group = visuals[found[0]]
        problems = []
        if group["visualGroup"].get("groupMode") != mode:
            problems.append(
                f"{display_name!r} is {group['visualGroup'].get('groupMode')}, not {mode}"
            )
        parent = group.get("parentGroupName")
        parent_name = names.get(parent) if isinstance(parent, str) else None
        if parent_name != inside:
            problems.append(f"{display_name!r} is inside {parent_name!r}, not {inside!r}")
        count = sum(1 for v in visuals.values() if v.get("parentGroupName") == found[0])
        if count != members:
            problems.append(f"{display_name!r} holds {count} visuals, Studio grouped {members}")
        return problems

    return check


def _mobile_parts(count: int, heights: Mapping[str, float], absent: Collection[str]) -> Check:
    """Desktop kept the phone layout: the parts, the schema they name, sizes Studio set."""

    def check(parts: Mapping[str, Any]) -> list[str]:
        found = {
            part.split("/visuals/")[1].split("/")[0]: data
            for part, data in parts.items()
            if part.endswith("/mobile.json") and isinstance(data, dict)
        }
        problems = []
        if len(found) != count:
            problems.append(f"Desktop kept {len(found)} phone layout parts of {count}")
        schemas = {str(data.get("$schema")) for data in found.values()}
        if schemas - {MOBILE_SCHEMA}:
            problems.append(f"Desktop saved the phone layout as {sorted(schemas)}")
        for name, height in heights.items():
            saved = _dig(found.get(name), "position", "height")
            if saved != height:
                problems.append(f"{name} is {saved} tall on the phone, Studio set {height}")
        problems.extend(f"{name} is on the phone layout" for name in absent if name in found)
        return problems

    return check


def _calculations_are(ref: str, expected: Sequence[tuple[str, str, str]]) -> Check:
    """Desktop kept each visual calculation: its name, its DAX and its queryRef."""

    def check(parts: Mapping[str, Any]) -> list[str]:
        visual = _find_saved(parts, ref)
        if visual is None:
            return [f"The saved file has no visual {ref!r}"]
        saved = [
            (str(calc.get("Name")), str(calc.get("Expression")), str(p.get("queryRef")))
            for _, p in _role_projections(dict(visual))
            if (calc := _calc_of(p)) is not None
        ]
        return [] if saved == list(expected) else [f"{ref}: calculations are {saved!r}"]

    return check


def _projection_is(ref: str, role: str, query_ref: str, expected: Mapping[str, Any]) -> Check:
    """Desktop kept a field of a visual with these keys (a name, a field expression)."""

    def check(parts: Mapping[str, Any]) -> list[str]:
        visual = _find_saved(parts, ref)
        if visual is None:
            return [f"The saved file has no visual {ref!r}"]
        found = [
            p
            for r, p in _role_projections(dict(visual))
            if r == role and p.get("queryRef") == query_ref
        ]
        if len(found) != 1:
            return [f"{ref}: {role} has no field {query_ref!r}"]
        return [
            f"{ref}: {query_ref} {key} is {found[0].get(key)!r}, Studio wrote {value!r}"
            for key, value in expected.items()
            if found[0].get(key) != value
        ]

    return check


def _page_view_is(value: str) -> Check:
    def check(parts: Mapping[str, Any]) -> list[str]:
        pages = [d for p, d in parts.items() if p.endswith(f"/{_AGENTS_PAGE}/page.json")]
        saved = pages[0].get("displayOption") if pages and isinstance(pages[0], dict) else None
        return [] if saved == value else [f"Page 1's view is {saved!r}, Studio set {value!r}"]

    return check


def _tab_orders_unique(parts: Mapping[str, Any]) -> list[str]:
    visuals = [
        v
        for part, v in parts.items()
        if f"/{_AGENTS_PAGE}/visuals/" in part and isinstance(v, dict)
    ]
    orders = [_dig(v, "position", "tabOrder") for v in visuals]
    return [] if len(orders) == len(set(orders)) else ["Two visuals of page 1 share a tab order"]


_P = FAMILY
DESKTOP_PROBES: tuple[ProbeCase, ...] = (
    ProbeCase(
        "extras_constant_line",
        _P,
        (
            {
                "op": "set_analytics_line",
                "visual": _CHART_SALES,
                "kind": "constant",
                "value": 50000,
                "name": "Target",
                "color": "#D64550",
                "style": "dashed",
                "width": 2,
                "data_label": True,
                "label_text": "value_and_name",
                "label_horizontal": "right",
                "label_vertical": "above",
            },
        ),
        "The 'Total MTD Sales' chart has a dashed red horizontal line at 50K labelled "
        "'Target 50K' at the right, above the line.",
        check=_entries_are(
            _CHART_SALES,
            "y1AxisReferenceLine",
            [
                (
                    {"id": "1"},
                    {
                        "value": _lit("50000D"),
                        "show": _lit("true"),
                        "displayName": _lit("'Target'"),
                        "lineColor": _fill("#D64550"),
                        "style": _lit("'dashed'"),
                        "width": _lit("2D"),
                        "dataLabelShow": _lit("true"),
                        "dataLabelText": _lit("'ValueAndName'"),
                        "dataLabelHorizontalPosition": _lit("'right'"),
                        "dataLabelVerticalPosition": _lit("'above'"),
                    },
                )
            ],
        ),
    ),
    ProbeCase(
        "extras_constant_line_from_measure",
        _P,
        (
            {
                "op": "set_analytics_line",
                "visual": _CHART_AVG,
                "kind": "constant",
                "measure": "Employees Avg MTD Sales",
                "name": "Average agent",
                "color": "#118DFF",
                "data_label": True,
            },
        ),
        "The 'Avg MTD Sales by Agents' chart has a blue line at the level of the 'Agents Avg MTD "
        "Sales' card's value, with its value shown.",
        check=_entries_are(
            _CHART_AVG,
            "y1AxisReferenceLine",
            [
                (
                    {"id": "1"},
                    {
                        "value": {
                            "expr": expr.measure_ref("# Measures", "Employees Avg MTD Sales")
                        },
                        "displayName": _lit("'Average agent'"),
                        "lineColor": _fill("#118DFF"),
                        "dataLabelShow": _lit("true"),
                    },
                )
            ],
        ),
    ),
    ProbeCase(
        "extras_trend_line",
        _P,
        (
            {
                "op": "set_analytics_line",
                "visual": _CHART_CHANGE,
                "kind": "trend",
                "color": "#404040",
                "style": "dotted",
            },
        ),
        "The 'Avg MTD % change by Agents' chart shows a dotted dark grey trend line across its "
        "three bars.",
        check=_entries_are(
            _CHART_CHANGE,
            "trend",
            [
                (
                    None,
                    {
                        "show": _lit("true"),
                        "displayName": _lit("'Trend line 1'"),
                        "lineColor": _fill("#404040"),
                        "style": _lit("'dotted'"),
                    },
                )
            ],
        ),
    ),
    ProbeCase(
        "extras_error_bars",
        _P,
        (
            {
                "op": "set_analytics_line",
                "visual": _CHART_SALES,
                "kind": "error_bars",
                "upper": "TopN/BottomN/ PMTD Sales",
                "lower": "Employees Avg MTD Sales",
                "color": "#252423",
                "width": 2,
            },
        ),
        "Each bar of 'Total MTD Sales' has a black error bar running between the previous month's "
        "MTD sales and the agents' average.",
        check=_entries_are(
            _CHART_SALES,
            "error",
            [
                (
                    {"metadata": _AGENTS_MEASURE},
                    {
                        "errorRange": {
                            "kind": "ErrorRange",
                            "explicit": {
                                "isRelative": _lit("false"),
                                "lowerBound": {
                                    "expr": expr.measure_ref(
                                        "# Measures", "Employees Avg MTD Sales"
                                    )
                                },
                                "upperBound": {
                                    "expr": expr.measure_ref(
                                        "# Measures", "TopN/BottomN/ PMTD Sales"
                                    )
                                },
                            },
                        },
                        "enabled": _lit("true"),
                        "barColor": _fill("#252423"),
                        "barMatchSeriesColor": _lit("false"),
                        "barWidth": _lit("2D"),
                    },
                )
            ],
        ),
    ),
    ProbeCase(
        "extras_dynamic_lines",
        _P,
        (
            {
                "op": "change_visual_type",
                "visual": _CHART_SALES,
                "visual_type": "clusteredColumnChart",
            },
            {
                "op": "set_analytics_line",
                "visual": _CHART_SALES,
                "kind": "average",
                "color": "#118DFF",
            },
            {"op": "set_analytics_line", "visual": _CHART_SALES, "kind": "min", "color": "#1AAB40"},
            {
                "op": "set_analytics_line",
                "visual": _CHART_SALES,
                "kind": "max",
                "color": "#D64550",
                "data_label": True,
            },
            {
                "op": "set_analytics_line",
                "visual": _CHART_SALES,
                "kind": "median",
                "style": "dotted",
                "shade": "after",
                "shade_color": "#EEEEEE",
            },
            {
                "op": "set_analytics_line",
                "visual": _CHART_SALES,
                "kind": "percentile",
                "percentile": 75,
                "style": "dashed",
            },
        ),
        "The 'Total MTD Sales' chart, now clustered, shows five horizontal lines: blue at the "
        "average, green at the lowest bar, red at the highest bar with its value, a dotted median "
        "line with the area above it shaded light grey, and a dashed line at the 75th percentile.",
        check=_entries_are(
            _CHART_SALES,
            "referenceLine",
            [
                (
                    {"metadata": _AGENTS_MEASURE, "id": str(number)},
                    {"value": value, "displayName": _lit(f"'{name}'"), "show": _lit("true")},
                )
                for number, (name, value) in enumerate(
                    [
                        ("Average line 1", _aggregate_of(_AGENTS_MEASURE, 1)),
                        ("Min line 1", _aggregate_of(_AGENTS_MEASURE, 3)),
                        ("Max line 1", _aggregate_of(_AGENTS_MEASURE, 4)),
                        ("Median line 1", _aggregate_of(_AGENTS_MEASURE, 6)),
                        ("Percentile line 1", _percentile_of(_AGENTS_MEASURE, 0.75)),
                    ],
                    start=1,
                )
            ],
        ),
    ),
    ProbeCase(
        "extras_forecast_and_x_line",
        _P,
        (
            {"op": "add_page", "name": _PROBE_PAGE},
            {
                "op": "add_visual",
                "page": _PROBE_PAGE,
                "visual_type": "lineChart",
                "fields": [
                    {"table": "Date", "name": "Date"},
                    {"table": "# Measures", "name": "Total Sales"},
                ],
                "title": "Probe sales trend",
                "x": 20,
                "y": 20,
                "width": 800,
                "height": 400,
            },
            {
                "op": "set_analytics_line",
                "visual": "Probe sales trend",
                "kind": "forecast",
                "color": "#744EC2",
                "forecast_length": 6,
                "confidence": 90,
            },
            {
                "op": "set_analytics_line",
                "visual": "Probe sales trend",
                "kind": "x_constant",
                "value": "2019-01-01",
                "name": "2019",
                "color": "#D64550",
                "data_label": True,
            },
        ),
        "On page 'Probe extras', the 'Probe sales trend' line chart continues six points past its "
        "last date with a purple forecast and a shaded 90% confidence band, and has a red vertical "
        "line at 1 January 2019 labelled with its date.",
        page=_PROBE_PAGE,
        check=_all_of(
            _entries_are(
                "Probe sales trend",
                "forecast",
                [
                    (
                        {"metadata": "# Measures.Total Sales", "id": "1"},
                        {
                            "transform": {
                                "algorithm": "Forecast",
                                "parameters": [
                                    {"Literal": {"Value": value}, "Name": name}
                                    for name, value in (
                                        ("Unit", "7L"),
                                        ("ForecastLength", "6L"),
                                        ("IgnoreLast", "0L"),
                                        ("ConfidenceLevel", "0.9D"),
                                    )
                                ],
                            },
                            "show": _lit("true"),
                            "displayName": _lit("'Forecast 1'"),
                            "lineColor": _fill("#744EC2"),
                        },
                    )
                ],
            ),
            _entries_are(
                "Probe sales trend",
                "xAxisReferenceLine",
                [
                    (
                        {"id": "1"},
                        {
                            "value": _lit("datetime'2019-01-01T00:00:00'"),
                            "displayName": _lit("'2019'"),
                            "dataLabelShow": _lit("true"),
                        },
                    )
                ],
            ),
        ),
    ),
    ProbeCase(
        "extras_scatter_lines",
        _P,
        (
            {"op": "add_page", "name": _PROBE_PAGE},
            {
                "op": "add_visual",
                "page": _PROBE_PAGE,
                "visual_type": "scatterChart",
                "fields": [
                    {"table": "DimStore", "name": "StoreName"},
                    {"table": "# Measures", "name": "Total Sales"},
                    {"table": "# Measures", "name": "Profit"},
                ],
                "title": "Probe scatter",
                "x": 20,
                "y": 20,
                "width": 800,
                "height": 500,
            },
            {
                "op": "set_analytics_line",
                "visual": "Probe scatter",
                "kind": "ratio",
                "color": "#E66C37",
            },
            {
                "op": "set_analytics_line",
                "visual": "Probe scatter",
                "kind": "symmetry_shading",
                "upper_color": "#C6E0B4",
                "lower_color": "#F8CBAD",
                "transparency": 60,
            },
            {
                "op": "set_analytics_line",
                "visual": "Probe scatter",
                "kind": "x_constant",
                "value": 1000000,
                "color": "#252423",
            },
        ),
        "On page 'Probe extras', the 'Probe scatter' chart of stores has an orange ratio line "
        "through the origin, the area above the diagonal shaded green and below it orange, and a "
        "black vertical line at 1M sales.",
        page=_PROBE_PAGE,
        check=_all_of(
            _entries_are(
                "Probe scatter",
                "ratioLine",
                [(None, {"show": _lit("true"), "lineColor": _fill("#E66C37")})],
            ),
            _entries_are(
                "Probe scatter",
                "plotAreaShading",
                [
                    (
                        None,
                        {
                            "show": _lit("true"),
                            "upperShadingColor": _fill("#C6E0B4"),
                            "lowerShadingColor": _fill("#F8CBAD"),
                            "transparency": _lit("60D"),
                        },
                    )
                ],
            ),
            _entries_are(
                "Probe scatter",
                "xAxisReferenceLine",
                [({"id": "1"}, {"value": _lit("1000000D"), "lineColor": _fill("#252423")})],
            ),
        ),
    ),
    ProbeCase(
        "extras_mobile_layout",
        _P,
        (
            {"op": "arrange_mobile_layout", "page": "1"},
            {"op": "place_on_mobile", "visual": _TABLE, "height": 400},
            {"op": "remove_from_mobile", "visuals": [_CARD_TOPN]},
        ),
        "View > Mobile layout shows page 1 in two columns: the three slicers, the four cards, the "
        "button slicer and the Top-Bottom-N slicer in pairs, the agents table 400 pixels tall "
        "across the width, then the four charts in pairs; the small 'TopN/BottomN' card is not "
        "on the phone canvas.",
        check=_mobile_parts(14, {_TABLE: 400}, [_CARD_TOPN]),
    ),
    ProbeCase(
        "extras_group_cards",
        _P,
        ({"op": "group_visuals", "visuals": list(_CARDS), "name": "KPI cards"},),
        "Nothing moves on page 1; the Selection pane lists a group 'KPI cards' holding the four "
        "cards, and dragging the group moves them together.",
        check=_all_of(
            _positions_kept(_CARD_ORIGINS, "KPI cards"),
            _group_is("KPI cards", "ScaleMode", None, 4),
            _tab_orders_unique,
        ),
    ),
    ProbeCase(
        "extras_nested_groups",
        _P,
        (
            {
                "op": "group_visuals",
                "visuals": [_CHART_SALES, _CHART_AVG],
                "name": "Sales charts",
            },
            {
                "op": "group_visuals",
                "visuals": [_CHART_CHANGE, _CHART_DISCOUNT],
                "name": "Change charts",
                "mode": "scroll",
            },
            {"op": "group_visuals", "visuals": ["Sales charts", "Change charts"], "name": "Charts"},
            {"op": "set_group", "group": "Charts", "name": "All charts"},
        ),
        "Nothing moves on page 1; the Selection pane shows 'All charts' holding the groups "
        "'Sales charts' and 'Change charts' (the second set to scroll, not scale), each holding "
        "two charts.",
        check=_all_of(
            _positions_kept(
                {
                    _CHART_SALES: (174.32432432432432, 645.9459459459459),
                    _CHART_AVG: (550.0, 645.9459459459459),
                },
                "Sales charts",
            ),
            _group_is("All charts", "ScaleMode", None, 2),
            _group_is("Sales charts", "ScaleMode", "All charts", 2),
            _group_is("Change charts", "ScrollMode", "All charts", 2),
            _tab_orders_unique,
        ),
    ),
    ProbeCase(
        "extras_group_ungroup",
        _P,
        (
            {"op": "group_visuals", "visuals": list(_SLICERS), "name": "Filters"},
            {"op": "ungroup_visuals", "group": "Filters"},
        ),
        "Page 1 looks exactly as before: the three slicers are where they were and in no group.",
        check=_positions_kept(_SLICER_ORIGINS, None),
    ),
    ProbeCase(
        "extras_visual_calculations",
        _P,
        (
            {
                "op": "set_visual_calculation",
                "visual": _TABLE,
                "name": "Running MTD Sales",
                "template": "running_sum",
                "field": "MTD Sales",
            },
            {
                "op": "set_visual_calculation",
                "visual": _TABLE,
                "name": "Share of MTD Sales",
                "template": "percent_of_grand_total",
                "field": "MTD Sales",
            },
        ),
        "The agents table has two more columns: 'Running MTD Sales' adding up MTD Sales down the "
        "rows, and 'Share of MTD Sales' with each agent's fraction of the total.",
        check=_calculations_are(
            _TABLE,
            [
                ("Running MTD Sales", "RUNNINGSUM([MTD Sales])", "Running MTD Sales"),
                (
                    "Share of MTD Sales",
                    "DIVIDE([MTD Sales], COLLAPSEALL([MTD Sales], ROWS))",
                    "Share of MTD Sales",
                ),
            ],
        ),
    ),
    ProbeCase(
        "extras_small_multiples",
        _P,
        (
            {
                "op": "set_small_multiples",
                "visual": _CHART_SALES,
                "field": {"table": "DimChannel", "name": "ChannelName"},
                "rows": 1,
                "columns": 2,
                "shared_y_axis": True,
                "titles": True,
            },
        ),
        "The 'Total MTD Sales' chart is split into small charts, one per channel, two side by "
        "side per page of the grid, each titled with its channel and sharing one y-axis scale.",
        check=_all_of(
            _projection_is(
                _CHART_SALES,
                "Rows",
                "DimChannel.ChannelName",
                {"field": expr.column_ref("DimChannel", "ChannelName")},
            ),
            _has_properties(
                _CHART_SALES,
                "smallMultiplesLayout",
                {
                    "layoutType": _lit("'custom'"),
                    "rowCount": _lit("1L"),
                    "columnCount": _lit("2L"),
                },
                "objects",
            ),
            _has_properties(_CHART_SALES, "valueAxis", {"sharedAxis": _lit("true")}, "objects"),
            _has_properties(_CHART_SALES, "subheader", {"show": _lit("true")}, "objects"),
        ),
    ),
    ProbeCase(
        "extras_page_view_actual_size",
        _P,
        ({"op": "set_page_view", "page": "1", "view": "actual_size"},),
        "Page 1 opens at its actual size (1920 x 1200) with scroll bars instead of being fitted "
        "to the window.",
        check=_page_view_is("ActualSize"),
    ),
    ProbeCase(
        "extras_accessibility",
        _P,
        (
            {"op": "set_alt_text", "visual": _TABLE, "text": "Agents ranked by MTD sales"},
            {"op": "set_tab_order", "page": "1", "by_position": True},
            {"op": "set_keep_layer_order", "visuals": [_CARD_TOPN]},
            {"op": "set_page_view", "page": "1", "view": "fit_to_width"},
            {
                "op": "set_field_display_name",
                "visual": _CHART_SALES,
                "field": "Clustered Employees",
                "name": "Agent group",
            },
        ),
        "Tab moves from the slicers at the top down the page; the page fills the "
        "window's width; the 'Total MTD Sales' tooltip names the axis 'Agent group'; the table's "
        "alt text shows under Format > General > Alt text.",
        check=_all_of(
            _has_properties(
                _TABLE,
                "general",
                {"altText": _lit("'Agents ranked by MTD sales'")},
                "visualContainerObjects",
            ),
            _has_properties(
                _CARD_TOPN, "general", {"keepLayerOrder": _lit("true")}, "visualContainerObjects"
            ),
            _page_view_is("FitToWidth"),
            _projection_is(
                _CHART_SALES,
                "Category",
                "Clustered Employees.Clustered Employees",
                {"displayName": "Agent group", "nativeQueryRef": "Agent group"},
            ),
            _tab_orders_unique,
        ),
    ),
)
