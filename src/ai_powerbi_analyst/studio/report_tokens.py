"""Typed style tokens the canvas draws from, and the fidelity of that drawing.

A visual.json holds formatting as PBIR objects; the canvas must show what the edit
operations write, so every property Studio draws is turned into a token here and every
property it does not draw is listed in ``unrendered``. Values resolve in this order: the
visual's own entries (by selector specificity), the theme's ``visualStyles`` for the visual
type and then for all visuals, the theme's ``textClasses``, and last a built-in default.
Every color and font leaves this module validated; the payload is never markup.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import pbir_expr
from .report_props import (
    color,
    enum_text,
    flag,
    is_dynamic,
    number,
    prop,
    properties,
    px,
    safe_color,
    text,
    theme_defaults,
    themed_color_problem,
    with_alpha,
)
from .report_theme import css_font_family, text_class

Json = dict[str, Any]

# --- families ---------------------------------------------------------------------------

TYPE_FAMILY: dict[str, str] = {
    "columnChart": "chart",
    "clusteredColumnChart": "chart",
    "stackedColumnChart": "chart",
    "barChart": "chart",
    "clusteredBarChart": "chart",
    "stackedBarChart": "chart",
    "lineChart": "chart",
    "areaChart": "chart",
    "stackedAreaChart": "chart",
    "pieChart": "chart",
    "donutChart": "chart",
    "card": "card",
    "multiRowCard": "card",
    "cardVisual": "cardVisual",
    "tableEx": "table",
    "slicer": "slicer",
    "advancedSlicerVisual": "advancedSlicer",
    "textbox": "textbox",
    "shape": "shape",
    "basicShape": "shape",
    "image": "image",
    "visualGroup": "group",
    "group": "group",
    "actionButton": "button",
    "pageNavigator": "navigator",
    "bookmarkNavigator": "navigator",
}

# Shapes the tokens read that no Desktop-authored file in hand shows (the lead's Desktop
# probes decide them), and defaults that are design decisions rather than measurements.
UNVERIFIED_SHAPES: dict[str, str] = {
    "actionButton": (
        "fill/outline/text/icon under state selectors default, hover, selected, disabled "
        "(instance-selectors.json), their show switches also without a selector (Desktop's "
        "button defaults, per the navigation family); defaults are design decisions: no fill, a "
        "1 px outline in foregroundNeutralSecondary, text off at 12 pt, icon on"
    ),
    "pageNavigator/bookmarkNavigator": (
        "As actionButton without icon, states default, hover, press, selected, plus layout "
        "{orientation 2 horizontal | 1 vertical | 0 grid, rowCount, columnCount, cellPadding}"
    ),
    "total": "tableEx total row on by default (totals true), bold by default, label 'Total'",
    "general.formatString": "A visual-level format string under a metadata selector",
    "value.customFormatString": "A new card's own format string for its value",
    "projection.format": "A projection's own format string (queryState.<role>.projections[])",
    "labels.valueCustomFormatString": (
        "A chart's data-label format string; labelDisplayUnits 0 is Auto and -1 Custom (the "
        "custom format string gives the units), so only a positive unit is a token"
    ),
    "columnFormatting": (
        "labelDisplayUnits, labelPrecision, fontColor and backColor under a metadata "
        "selector naming the column (a plain color, not a rule)"
    ),
    "navigator states": (
        "default, hover, press, selected ids on the fill/outline/text selectors; "
        "layout.orientation 2 horizontal (the default), 1 vertical, 0 grid"
    ),
}

# Objects that only change what a click or hover does, or are read by Desktop's editing
# surface: a static preview looks the same with or without them.
IGNORED_OBJECTS = frozenset(
    {"visualTooltip", "visualHeader", "visualHeaderTooltip", "visualLink", "keepLayerOrder"}
)
IGNORED_PROPERTIES = frozenset(
    {"general.altText", "general.keepLayerOrder", "general.filter", "general.responsive"}
)

_CONTAINER_RENDERED: dict[str, frozenset[str]] = {
    "title": frozenset(
        {
            "show",
            "text",
            "fontColor",
            "fontSize",
            "fontFamily",
            "bold",
            "italic",
            "underline",
            "alignment",
            "background",
            "transparency",
            "titleWrap",
        }
    ),
    "background": frozenset({"show", "color", "transparency"}),
    "border": frozenset({"show", "color", "width", "radius"}),
    "dropShadow": frozenset({"show"}),
    "padding": frozenset({"top", "right", "bottom", "left"}),
}

_AXIS = frozenset(
    {
        "show",
        "showAxisTitle",
        "titleText",
        "titleColor",
        "titleFontSize",
        "fontSize",
        "labelColor",
        "fontFamily",
        "gridlineShow",
        "gridlineColor",
        "labelDisplayUnits",
        "labelPrecision",
    }
)
# Analytics lines (render_analytics.LINE_OBJECTS) and the properties they draw.
_LINE = frozenset(
    {
        "show",
        "displayName",
        "value",
        "lineColor",
        "transparency",
        "style",
        "width",
        "position",
        "dataLabelShow",
        "dataLabelText",
        "dataLabelColor",
        "dataLabelDecimalPoints",
        "dataLabelHorizontalPosition",
        "dataLabelVerticalPosition",
        "dataLabelDisplayUnits",
        "percentile",
        "combineSeries",
    }
)
LINE_OBJECT_NAMES = (
    "y1AxisReferenceLine",
    "referenceLine",
    "xAxisReferenceLine",
    "minLine",
    "maxLine",
    "averageLine",
    "medianLine",
    "percentileLine",
    "trend",
)
_FORMAT_STRING = frozenset({"formatString"})
_BUTTON_OBJECTS: dict[str, frozenset[str]] = {
    "fill": frozenset({"show", "fillColor", "transparency"}),
    "outline": frozenset({"show", "lineColor", "weight", "transparency"}),
    "text": frozenset(
        {
            "show",
            "text",
            "fontFamily",
            "fontSize",
            "bold",
            "italic",
            "underline",
            "fontColor",
            "horizontalAlignment",
            "verticalAlignment",
        }
    ),
    "shape": frozenset({"tileShape", "roundEdge", "rectangleRoundedCurve"}),
}

# "object.property" pairs each family draws; the token that carries them is named in
# docs/imported-report-render-contract.md. A pair is drawn whatever selector scopes it.
RENDERED: dict[str, dict[str, frozenset[str]]] = {
    "container": _CONTAINER_RENDERED,
    "chart": {
        "dataPoint": frozenset({"fill", "showAllDataPoints"}),
        "labels": frozenset(
            {
                "show",
                "fontSize",
                "color",
                "fontFamily",
                "labelDisplayUnits",
                "labelPrecision",
                "labelPosition",
                "labelStyle",
                "bold",
                "italic",
                "valueCustomFormatString",
            }
        ),
        "legend": frozenset({"show", "position", "fontSize", "labelColor", "fontFamily"}),
        "categoryAxis": _AXIS,
        "valueAxis": _AXIS,
        "lineStyles": frozenset({"strokeWidth", "showMarker"}),
        "smallMultiplesLayout": frozenset({"rowCount", "columnCount", "layoutType"}),
        "general": _FORMAT_STRING,
        **{name: _LINE for name in LINE_OBJECT_NAMES},
    },
    "card": {
        "labels": frozenset(
            {"color", "fontSize", "fontFamily", "labelDisplayUnits", "labelPrecision"}
        ),
        "categoryLabels": frozenset({"show", "color", "fontSize", "fontFamily"}),
        "general": _FORMAT_STRING,
    },
    "cardVisual": {
        "general": _FORMAT_STRING,
        "value": frozenset(
            {
                "show",
                "fontSize",
                "fontFamily",
                "fontColor",
                "color",
                "bold",
                "italic",
                "horizontalAlignment",
                "labelDisplayUnits",
                "labelPrecision",
                "customFormatString",
            }
        ),
        "label": frozenset(
            {
                "show",
                "text",
                "fontSize",
                "fontFamily",
                "fontColor",
                "color",
                "bold",
                "italic",
                "horizontalAlignment",
            }
        ),
        "fillCustom": frozenset({"show", "fillColor", "transparency"}),
        "outline": frozenset({"show", "lineColor", "weight", "transparency"}),
        "padding": frozenset(
            {"paddingSelection", "leftMargin", "topMargin", "rightMargin", "bottomMargin"}
        ),
        "accentBar": frozenset({"show", "color", "width", "position"}),
        "image": frozenset({"show", "imageUrl"}),
        "referenceLabel": frozenset({"value"}),
        "referenceLabelTitle": frozenset({"titleText"}),
        "referenceLabelDetail": frozenset({"show", "detailValue"}),
    },
    "table": {
        "values": frozenset(
            {
                "fontSize",
                "fontFamily",
                "fontColor",
                "fontColorPrimary",
                "fontColorSecondary",
                "backColor",
                "backColorPrimary",
                "backColorSecondary",
                "wordWrap",
                "icon",
            }
        ),
        "columnHeaders": frozenset(
            {"fontSize", "fontFamily", "fontColor", "backColor", "alignment", "wordWrap"}
        ),
        "grid": frozenset(
            {
                "gridHorizontal",
                "gridVertical",
                "gridHorizontalColor",
                "gridVerticalColor",
                "gridHorizontalWeight",
                "gridVerticalWeight",
                "rowPadding",
            }
        ),
        "columnFormatting": frozenset(
            {
                "alignment",
                "dataBars",
                "styleHeader",
                "fontColor",
                "backColor",
                "labelDisplayUnits",
                "labelPrecision",
            }
        ),
        "columnWidth": frozenset({"value"}),
        "total": frozenset(
            {"totals", "label", "fontSize", "fontFamily", "fontColor", "backColor", "bold"}
        ),
        "general": _FORMAT_STRING,
    },
    "slicer": {
        "header": frozenset(
            {"show", "text", "fontColor", "fontSize", "textSize", "fontFamily", "bold"}
        ),
        "items": frozenset({"fontColor", "fontSize", "textSize", "fontFamily", "background"}),
        "data": frozenset({"mode"}),
    },
    "advancedSlicer": {
        "value": frozenset({"fontSize", "fontFamily", "fontColor", "color", "bold"}),
        "fillCustom": frozenset({"show", "fillColor", "transparency"}),
        "outline": frozenset({"show", "lineColor", "weight", "transparency"}),
        "padding": frozenset(
            {"paddingSelection", "leftMargin", "topMargin", "rightMargin", "bottomMargin"}
        ),
        "accentBar": frozenset({"show", "color", "width", "position"}),
        "layout": frozenset({"rowCount", "columnCount", "cellPadding"}),
        "shapeCustomRectangle": frozenset({"tileShape", "rectangleRoundedCurve"}),
        "header": frozenset({"show", "text", "fontColor", "fontSize", "fontFamily", "bold"}),
    },
    "textbox": {"general": frozenset({"paragraphs"})},
    "shape": {
        "shape": frozenset({"tileShape", "roundEdge", "rectangleRoundedCurve"}),
        "fill": frozenset({"show", "fillColor", "transparency"}),
        "outline": frozenset({"show", "lineColor", "weight", "transparency"}),
        "rotation": frozenset({"shapeAngle"}),
    },
    "image": {
        "general": frozenset({"imageUrl"}),
        "imageScaling": frozenset({"imageScalingType"}),
    },
    "button": {
        **_BUTTON_OBJECTS,
        "icon": frozenset(
            {
                "show",
                "shapeType",
                "lineColor",
                "lineWeight",
                "lineTransparency",
                "placement",
                "iconSize",
                "horizontalAlignment",
                "verticalAlignment",
            }
        ),
    },
    "navigator": {
        **_BUTTON_OBJECTS,
        "layout": frozenset({"orientation", "rowCount", "columnCount", "cellPadding"}),
        "pages": frozenset({"showHiddenPages", "showTooltipPages", "showByDefault", "showPage"}),
        "bookmarks": frozenset({"bookmarkGroup", "selectedBookmark"}),
    },
    "group": {},
    "unsupported": {},
}


def family_of(visual_type: str) -> str:
    """The rendering family of a visual type; ``unsupported`` for types Studio only frames."""
    return TYPE_FAMILY.get(visual_type, "unsupported")


def property_is_rendered(
    visual_type: str, object_name: str, property_name: str, *, container: bool | None = None
) -> bool:
    """Whether the canvas draws ``object.property`` of a visual type.

    ``container`` is True for visualContainerObjects (title, background, border, shadow,
    padding, common to every type), False for a visual's own objects, None to accept either.
    """
    in_container = property_name in _CONTAINER_RENDERED.get(object_name, frozenset())
    if container is not False and in_container:
        return True
    if container is True:
        return False
    own = RENDERED.get(family_of(visual_type), {}).get(object_name)
    return own is not None and property_name in own


_WHOLE = frozenset({"none", "default"})

# Beyond "the whole visual" (no selector, or {"id": "default"}), the selector kinds a token
# resolves, per family and object. An entry that scopes a property to one series, column or
# state anywhere else is set in the report but not drawn: the token reads the unscoped value.
SCOPES_DRAWN: dict[str, dict[str, frozenset[str]]] = {
    "chart": {
        "dataPoint": frozenset({"metadata", "data", "data+metadata"}),
        "general": frozenset({"metadata"}),
        # One entry per line ({"id": "0"}), optionally for one measure.
        **{name: frozenset({"state", "metadata"}) for name in LINE_OBJECT_NAMES},
    },
    "card": {"general": frozenset({"metadata"})},
    "cardVisual": {
        "value": frozenset({"metadata"}),
        "label": frozenset({"metadata"}),
        "image": frozenset({"metadata"}),
        "referenceLabel": frozenset({"metadata", "field"}),
        "referenceLabelTitle": frozenset({"metadata", "field"}),
        "referenceLabelDetail": frozenset({"metadata", "field"}),
        "general": frozenset({"metadata"}),
    },
    "advancedSlicer": {
        name: frozenset({"state"})
        for name in ("value", "fillCustom", "outline", "padding", "accentBar")
    },
    "table": {
        "columnFormatting": frozenset({"metadata"}),
        "columnWidth": frozenset({"metadata"}),
        "general": frozenset({"metadata"}),
    },
    # Button and navigator fill, outline, text and icon only take effect under a state
    # selector (instance-selectors.json); their shape and layout are whole-visual.
    "button": {name: frozenset({"state"}) for name in ("fill", "outline", "text", "icon")},
    "navigator": {name: frozenset({"state"}) for name in ("fill", "outline", "text", "pages")},
}
# A table's conditional formats: a rule or a color scoped to every value cell of one
# column ({data: [dataViewWildcard], metadata}).
_CELL_RULES = frozenset({"fontColor", "backColor", "icon"})


def selector_kind(selector: Any) -> str:
    """``pbir_expr.selector_kind`` that also answers for a selector of the wrong type."""
    if not selector:
        return "none"
    return pbir_expr.selector_kind(selector) if isinstance(selector, dict) else "invalid"


def _scope_drawn(
    family: str, object_name: str, key: str, selector: Any, *, container: bool
) -> bool:
    kind = selector_kind(selector)
    if kind in _WHOLE:
        return True
    if container:
        return False
    if (
        family == "table"
        and object_name == "values"
        and key in _CELL_RULES
        and kind == "data+metadata"
    ):
        # A conditional font color, background or icon: scoped to every value cell of one
        # column (matchingOption 1). Applied to the totals too (0) or to the totals only (2),
        # it is not fully drawn: Studio formats no totals row.
        data = selector.get("data") if isinstance(selector, dict) else None
        return bool(data) and all(
            isinstance(d, dict)
            and isinstance(d.get("dataViewWildcard"), dict)
            and d["dataViewWildcard"].get("matchingOption", 1) == 1
            for d in data or []
        )
    if (
        family == "cardVisual"
        and object_name in {"value", "label", "fillCustom"}
        and kind == "data+metadata"
    ):
        # A card's conditional color, scoped to its value's every instance.
        return rule_scope(selector)
    return kind in SCOPES_DRAWN.get(family, {}).get(object_name, frozenset())


def unrendered_properties(visual_type: str, body: Mapping[str, Any]) -> list[str]:
    """Properties a visual sets that the canvas does not draw, as sorted "object.property".

    A property that is drawn only when it applies to the whole visual counts as not drawn
    when its entry is scoped to a series, a column or a state the tokens do not resolve.
    """
    family = family_of(visual_type)
    found: set[str] = set()
    for holder_name, container in (
        ("objects", False),
        ("visualContainerObjects", True),
        ("vcObjects", True),
    ):
        holder = body.get(holder_name)
        if not isinstance(holder, dict):
            continue
        for object_name, entries in holder.items():
            if object_name in IGNORED_OBJECTS or not isinstance(entries, list):
                continue
            for entry in entries:
                props = entry.get("properties") if isinstance(entry, dict) else None
                for key in props if isinstance(props, dict) else ():
                    path = f"{object_name}.{key}"
                    if path in IGNORED_PROPERTIES:
                        continue
                    selector = entry.get("selector") if isinstance(entry, dict) else None
                    if (
                        not property_is_rendered(visual_type, object_name, key, container=container)
                        or not _scope_drawn(family, object_name, key, selector, container=container)
                        or (
                            path == "columnFormatting.dataBars"
                            and totals_bars((props or {}).get(key)) is not None
                        )
                    ):
                        found.add(path)
    return sorted(found)


def totals_bars(bars: Any) -> int | None:
    """The totals a table column's data bars apply to: None for the values only (Power BI's
    default), 0 for the values and the totals, 2 for the totals only. Studio draws no bars
    on a totals row."""
    node = bars.get("totalMatchingOption") if isinstance(bars, dict) else None
    found = pbir_expr.decode(node) if node is not None else None
    if isinstance(found, bool) or not isinstance(found, int | float) or found == 1:
        return None
    return int(found)


def fidelity_of(family: str, unrendered: list[str], approximate: list[str]) -> str:
    """exact, approximate (something set is not drawn or only close), or none (framed only)."""
    if family == "unsupported":
        return "none"
    return "approximate" if unrendered or approximate else "exact"


# --- reader -----------------------------------------------------------------------------


@dataclass
class FormatContext:
    """What the tokens need from outside one visual: the theme and report resources."""

    theme: Json
    palette: list[str]
    image: Callable[[Any, Any], str | None] | None = None
    literal_image: Callable[[str], str | None] | None = None


@dataclass
class Reader:
    """One visual's formatting: own entries by specificity, then the theme's defaults."""

    visual_type: str
    body: Json
    context: FormatContext
    approximate: list[str] = field(default_factory=list)

    @property
    def objects(self) -> Json:
        found = self.body.get("objects")
        return found if isinstance(found, dict) else {}

    @property
    def container_objects(self) -> Json:
        for key in ("visualContainerObjects", "vcObjects"):
            found = self.body.get(key)
            if isinstance(found, dict):
                return found
        return {}

    def own(
        self,
        name: str,
        *,
        container: bool = False,
        metadata: str | None = None,
        state: str | None = None,
        data: list[Any] | None = None,
        selector_required: bool = False,
    ) -> Json:
        holder = self.container_objects if container else self.objects
        return properties(holder, name, metadata, state, data, selector_required=selector_required)

    def note(self, path: str) -> None:
        if path not in self.approximate:
            self.approximate.append(path)

    def resolved(self, path: str) -> None:
        """Withdraw a note: the conditional format the token could not read was evaluated
        per row after all (``render_rules``)."""
        if path in self.approximate:
            self.approximate.remove(path)

    def get(self, name: str, key: str, default: Any = None, **scope: Any) -> Any:
        """A decoded property: the visual's own value, the theme's, else ``default``."""
        own = self.own(name, **scope)
        if key in own:
            node = own[key]
            if is_dynamic(node):
                self.note(f"{name}.{key}")
            else:
                found = prop(node)
                if found is not None:
                    return found
        base = theme_defaults(self.context.theme, self.visual_type, name)
        if key in base:
            found = prop(base[key])
            if found is not None:
                return found
        return default

    def color(self, name: str, key: str, default: str | None = None, **scope: Any) -> str | None:
        own = self.own(name, **scope)
        if key in own:
            node = own[key]
            if is_dynamic(node):
                self.note(f"{name}.{key}")
            else:
                found = color(node, self.context.palette)
                if found:
                    return found
                if themed_color_problem(node, self.context.palette):
                    # Desktop paints its default fill for an id it cannot resolve.
                    self.note(f"{name}.{key}")
        base = theme_defaults(self.context.theme, self.visual_type, name)
        if key in base:
            found = color(base[key], self.context.palette)
            if found:
                return found
        return default

    def number(
        self, name: str, key: str, default: float | None = None, **scope: Any
    ) -> float | None:
        found = self.get(name, key, None, **scope)
        result = number(found)
        return default if result is None else result

    def flag(self, name: str, key: str, default: bool | None = None, **scope: Any) -> bool | None:
        return flag(self.get(name, key, None, **scope), default)

    def text(self, name: str, key: str, default: str | None = None, **scope: Any) -> str | None:
        return text(self.get(name, key, None, **scope), default)

    def enum(self, name: str, key: str, default: str | None = None, **scope: Any) -> str | None:
        """A short label-like value (a position or mode name), validated."""
        return enum_text(self.get(name, key, None, **scope), default)


def _class_size(reader: Reader, name: str) -> float:
    return float(text_class(reader.context.theme, name)["fontSize"])


def _font(
    reader: Reader,
    name: str,
    text_class_name: str,
    *,
    size_key: str = "fontSize",
    color_key: str = "fontColor",
    family_key: str = "fontFamily",
    **scope: Any,
) -> Json:
    """fontSize (px), color and family of one object, with a text class as the default.

    A slicer names its font size ``textSize`` where every other object says ``fontSize``.
    """
    cls = text_class(reader.context.theme, text_class_name)
    size = reader.number(name, size_key, None, **scope)
    if size is None and size_key == "fontSize" and name in {"header", "items"}:
        size = reader.number(name, "textSize", None, **scope)
    return {
        "fontSize": px(size if size is not None else float(cls["fontSize"])),
        "color": reader.color(name, color_key, cls["color"], **scope),
        "family": css_font_family(reader.get(name, family_key, None, **scope)) or cls["family"],
    }


# --- container --------------------------------------------------------------------------


def container_token(reader: Reader) -> Json:
    """The visual box: background, border, shadow and padding, from visualContainerObjects."""
    theme = reader.context.theme
    # A new textbox, shape or image has its background off; every other visual has it on
    # (white, the theme's background), unless the theme's visualStyles say otherwise.
    framed_by_default = family_of(reader.visual_type) not in {"textbox", "shape", "image"}
    show = reader.flag("background", "show", framed_by_default, container=True)
    fill = reader.color("background", "color", None, container=True)
    transparency = reader.number("background", "transparency", 0.0, container=True)
    if show is False:
        background = None
    else:
        base = fill or safe_color(theme.get("background")) or "#ffffff"
        background = with_alpha(base, transparency)
    border_on = bool(reader.flag("border", "show", False, container=True))
    border_color = reader.color("border", "color", None, container=True)
    width = reader.number("border", "width", None, container=True)
    radius = reader.number("border", "radius", 0.0, container=True) or 0.0
    padding = [
        reader.number("padding", side, None, container=True)
        for side in ("top", "right", "bottom", "left")
    ]
    fallback_border = safe_color(theme.get("foregroundNeutralTertiary")) or "#b3b0ad"
    return {
        "background": background,
        "border": {
            "show": border_on,
            "color": (border_color or fallback_border) if border_on else None,
            "width": (1.0 if width is None else width) if border_on else None,
            "radius": radius if border_on else 0.0,
        },
        "shadow": bool(reader.flag("dropShadow", "show", False, container=True)),
        "padding": padding if any(p is not None for p in padding) else None,
    }


# --- title ------------------------------------------------------------------------------

_ALIGN = {"left", "center", "right"}
_ROLE_RANK = {"Category": 0, "X": 0, "Rows": 0, "Series": 1, "Columns": 1, "Legend": 1}


def automatic_title(fields: Iterable[Mapping[str, Any]]) -> str | None:
    """Power BI's automatic title: ``<measures joined by " and "> by <category> and <series>``.

    Charts show it when the title is on and no text is set (Desktop shows
    "Total Sales by ChannelName" for a chart of that field pair). Formatting-only helper
    measures are left out.
    """
    shown = [f for f in fields if not f.get("hidden") and f.get("role") != "Formatting"]
    measures = list(dict.fromkeys(str(f["label"]) for f in shown if f.get("kind") == "measure"))
    groups = sorted(
        (f for f in shown if f.get("kind") == "column"),
        key=lambda f: _ROLE_RANK.get(str(f.get("role")), 2),
    )
    names = list(dict.fromkeys(str(f["label"]) for f in groups))
    names = [n for n in names if n.strip()]
    measures = [m for m in measures if m.strip()]
    if measures and names:
        return f"{' and '.join(measures)} by {' and '.join(names)}"
    return " and ".join(measures or names) or None


def title_token(reader: Reader, family: str, fields: Iterable[Mapping[str, Any]]) -> Json:
    """The container title: visibility, text (literal or automatic) and style.

    Charts show a title by default and tables, cards and slicers do not; text typed into a
    title that has no ``show`` counts as shown (the old importer already relied on this).
    """
    own = reader.own("title", container=True)
    text_node = own.get("text")
    literal = text(text_node)
    dynamic = is_dynamic(text_node)
    show = reader.flag("title", "show", None, container=True)
    if show is None:
        show = True if (literal or dynamic) else family == "chart"
    automatic = None if literal or dynamic else automatic_title(fields)
    if literal is not None and literal.strip():
        source, shown_text = "literal", literal
    elif dynamic:
        source, shown_text = "dynamic", ""
        reader.note("title.text")
    elif automatic and show:
        source, shown_text = "automatic", automatic
    else:
        source, shown_text = "none", ""
    cls = text_class(reader.context.theme, "title")
    size = reader.number("title", "fontSize", None, container=True)
    alignment = str(reader.get("title", "alignment", "left", container=True)).lower()
    background = reader.color("title", "background", None, container=True)
    return {
        "show": bool(show),
        "text": shown_text,
        "textSource": source,
        "automaticText": automatic,
        "fontSize": px(size if size is not None else float(cls["fontSize"])),
        "fontColor": reader.color("title", "fontColor", cls["color"], container=True),
        "fontFamily": css_font_family(reader.get("title", "fontFamily", None, container=True))
        or cls["family"],
        "bold": bool(reader.flag("title", "bold", False, container=True)),
        "italic": bool(reader.flag("title", "italic", False, container=True)),
        "underline": bool(reader.flag("title", "underline", False, container=True)),
        "alignment": alignment if alignment in _ALIGN else "left",
        "wrap": bool(reader.flag("title", "titleWrap", True, container=True)),
        "background": with_alpha(
            background, reader.number("title", "transparency", 0.0, container=True)
        ),
    }


# --- charts -----------------------------------------------------------------------------

_LEGEND_POSITIONS = {
    "top": ("top", "start"),
    "topcenter": ("top", "center"),
    "bottom": ("bottom", "start"),
    "bottomcenter": ("bottom", "center"),
    "left": ("left", "start"),
    "leftcenter": ("left", "center"),
    "right": ("right", "start"),
    "rightcenter": ("right", "center"),
}
_LABEL_POSITIONS = {"Auto", "OutsideEnd", "InsideEnd", "InsideCenter", "InsideBase"}
CHART_SHAPE: dict[str, tuple[str, str]] = {
    # visualType -> (orientation, stacking). PBIR names the stacked chart "columnChart" and
    # "barChart"; clustered ones carry the word.
    "columnChart": ("vertical", "stacked"),
    "stackedColumnChart": ("vertical", "stacked"),
    "clusteredColumnChart": ("vertical", "clustered"),
    "barChart": ("horizontal", "stacked"),
    "stackedBarChart": ("horizontal", "stacked"),
    "clusteredBarChart": ("horizontal", "clustered"),
    "lineChart": ("vertical", "none"),
    "areaChart": ("vertical", "none"),
    "stackedAreaChart": ("vertical", "stacked"),
    "pieChart": ("vertical", "none"),
    "donutChart": ("vertical", "none"),
}


def _axis_token(reader: Reader, name: str, gridlines: bool) -> Json:
    fonts = _font(reader, name, "label", color_key="labelColor")
    title_size = reader.number(name, "titleFontSize", None)
    units = reader.number(name, "labelDisplayUnits", None)
    precision = reader.number(name, "labelPrecision", None)
    return {
        # 0 is Power BI's "Auto" display unit and -1 its "Custom".
        "displayUnits": units if units and units > 0 else None,
        "decimals": int(precision) if precision is not None and precision >= 0 else None,
        "show": bool(reader.flag(name, "show", True)),
        "title": bool(reader.flag(name, "showAxisTitle", True)),
        "titleText": reader.text(name, "titleText"),
        "titleColor": reader.color(name, "titleColor", fonts["color"]),
        "titleFontSize": px(title_size) if title_size is not None else fonts["fontSize"],
        "fontSize": fonts["fontSize"],
        "color": fonts["color"],
        "family": fonts["family"],
        "gridlines": bool(reader.flag(name, "gridlineShow", gridlines)),
        "gridlineColor": reader.color(name, "gridlineColor", None),
    }


def _labels_token(reader: Reader) -> Json:
    fonts = _font(reader, "labels", "label", color_key="color")
    units = reader.number("labels", "labelDisplayUnits", None)
    precision = reader.number("labels", "labelPrecision", None)
    position = reader.text("labels", "labelPosition")
    token = {
        "show": bool(reader.flag("labels", "show", False)),
        "fontSize": fonts["fontSize"],
        "color": fonts["color"],
        "family": fonts["family"],
        "bold": bool(reader.flag("labels", "bold", False)),
        "italic": bool(reader.flag("labels", "italic", False)),
        # 0 is Power BI's "Auto" display unit and -1 its "Custom" (a format string).
        "displayUnits": units if units and units > 0 else None,
        "decimals": int(precision) if precision is not None and precision >= 0 else None,
        "position": position if position in _LABEL_POSITIONS else None,
        "style": reader.enum("labels", "labelStyle"),
    }
    custom = reader.text("labels", "valueCustomFormatString", None)
    if custom:
        # The labels' own format string; the canvas formats label values with it.
        token["format"] = custom[:500]
    return token


def _legend_token(reader: Reader) -> Json:
    fonts = _font(reader, "legend", "label", color_key="labelColor")
    raw = str(reader.get("legend", "position", "Top")).lower()
    position, align = _LEGEND_POSITIONS.get(raw, ("top", "start"))
    return {
        "show": bool(reader.flag("legend", "show", True)),
        "position": position,
        "alignment": align,
        "fontSize": fonts["fontSize"],
        "color": fonts["color"],
        "family": fonts["family"],
    }


def walk(node: Any, *path: str) -> Any:
    """``node[path[0]][path[1]]...`` through objects only; None when a step is not one."""
    for step in path:
        if not isinstance(node, dict):
            return None
        node = node.get(step)
    return node


def _scope_column(item: Mapping[str, Any]) -> str | None:
    left = walk(item, "scopeId", "Comparison", "Left")
    for kind in ("Column", "Measure", "HierarchyLevel"):
        node = left.get(kind) if isinstance(left, dict) else None
        if isinstance(node, dict):
            source = walk(node, "Expression", "SourceRef")
            if not isinstance(source, dict):
                continue
            entity = source.get("Entity") or source.get("Source")
            name = node.get("Property") or node.get("Level")
            if isinstance(entity, str) and isinstance(name, str) and entity and name:
                return f"{entity}.{name}"
    return None


def _category_value(item: Mapping[str, Any]) -> tuple[Any, bool] | None:
    comparison = walk(item, "scopeId", "Comparison")
    if not isinstance(comparison, dict) or comparison.get("ComparisonKind") != 0:
        return None
    decoded = pbir_expr.decode(comparison.get("Right"))
    if decoded is None:
        return None
    if hasattr(decoded, "isoformat"):
        return decoded.strftime("%Y-%m-%dT%H:%M:%S"), True
    return decoded, False


def series_token(reader: Reader, visual_type: str) -> Json:
    """Mark colors: the fill of the whole visual, of one series and of single categories.

    ``defaultColor`` is the selector-less dataPoint fill, ``seriesColors`` maps a field's
    queryRef (a metadata selector) to its color, and ``categoryColors`` lists one entry per
    category (a scopeId selector), with ``L``/``M`` numbers and ``datetime'..'`` decoded.
    """
    objects = reader.objects
    default_node = properties(objects, "dataPoint").get("fill")
    default: str | None = None
    # A rule-driven fill of the whole visual or of one series is a conditional format:
    # report_formatting evaluates it per point (series.ruleColors) and notes it when it
    # cannot. An id Desktop cannot resolve is drawn as the default fill.
    if default_node is not None and not is_dynamic(default_node):
        default = color(default_node, reader.context.palette)
        if default is None:
            reader.note("dataPoint.fill")
    series_colors: dict[str, str] = {}
    categories: list[Json] = []
    for entry in _entries(objects):
        fill_node = (entry.get("properties") or {}).get("fill")
        if fill_node is None:
            continue
        selector = entry.get("selector") or {}
        kind = selector_kind(selector)
        if kind in {"none", "default"}:
            continue
        if is_dynamic(fill_node):
            if not rule_scope(selector):
                reader.note("dataPoint.fill")
            continue
        fill = color(fill_node, reader.context.palette)
        if fill is None:
            if themed_color_problem(fill_node, reader.context.palette):
                reader.note("dataPoint.fill")
            continue
        data = selector.get("data")
        data = data if isinstance(data, list) else None
        metadata = selector.get("metadata") if isinstance(selector.get("metadata"), str) else None
        scopes = [d for d in data if isinstance(d, dict) and "scopeId" in d] if data else []
        if scopes:
            if len(scopes) > 1:
                # A point scoped by two fields (category and series) has no single value.
                reader.note("dataPoint.fill")
                continue
            found = _category_value(scopes[0])
            if found is None:
                reader.note("dataPoint.fill")
                continue
            item: Json = {"value": found[0], "color": fill}
            if found[1]:
                item["type"] = "datetime"
            column = _scope_column(scopes[0])
            if column:
                item["column"] = column
            if metadata:
                item["metadata"] = metadata
            categories.append(item)
        elif metadata:
            series_colors[metadata] = fill
        elif data:
            # A wildcard with no field paints every point.
            default = fill
    vary = bool(reader.flag("dataPoint", "showAllDataPoints", False)) or visual_type in {
        "pieChart",
        "donutChart",
    }
    return {
        "defaultColor": default,
        "seriesColors": series_colors,
        "categoryColors": categories,
        "varyByCategory": vary,
    }


def _entries(objects: Mapping[str, Any]) -> list[Json]:
    return entries_of(objects, "dataPoint")


def rule_scope(selector: Any) -> bool:
    """Whether a selector scopes a conditional format to every point (of one series): no
    selector, ``{"id": "default"}``, or ``dataViewWildcard`` data with or without metadata.
    Those are the scopes the conditional formats are evaluated for."""
    kind = selector_kind(selector)
    if kind in _WHOLE or kind == "metadata":
        return True
    if kind not in {"data", "data+metadata"}:
        return False
    data = selector.get("data") if isinstance(selector, dict) else None
    if not isinstance(data, list):
        return False
    # matchingOption 2 is the totals only: it colors no ordinary point or cell.
    return bool(data) and all(
        isinstance(d, dict)
        and isinstance(d.get("dataViewWildcard"), dict)
        and d["dataViewWildcard"].get("matchingOption") != 2
        for d in data
    )


def empty_column() -> Json:
    """``table.columns[query_ref]`` for a column the file styles in no other way."""
    return {
        "alignment": None,
        "styleHeader": False,
        "width": None,
        "fontColor": None,
        "background": None,
        "displayUnits": None,
        "decimals": None,
    }


def chart_tokens(reader: Reader, visual_type: str) -> Json:
    orientation, stacking = CHART_SHAPE.get(visual_type, ("vertical", "none"))
    tokens: Json = {
        "chart": {"orientation": orientation, "stacking": stacking},
        "labels": _labels_token(reader),
        "legend": _legend_token(reader),
        "axes": {
            "category": _axis_token(reader, "categoryAxis", False),
            "value": _axis_token(reader, "valueAxis", True),
        },
        "series": series_token(reader, visual_type),
    }
    if visual_type in {"lineChart", "areaChart", "stackedAreaChart"}:
        width = reader.number("lineStyles", "strokeWidth", 3.0)
        tokens["line"] = {
            "strokeWidth": width,
            "markers": bool(reader.flag("lineStyles", "showMarker", False)),
        }
    return tokens


# --- cards ------------------------------------------------------------------------------


def _tile(reader: Reader, state: str | None) -> Json:
    """Tile fill, outline, padding and accent bar of a new card or a tile slicer, in one state."""
    scope = {"state": state} if state else {}
    fill_on = reader.flag("fillCustom", "show", True, **scope)
    fill = reader.color("fillCustom", "fillColor", None, **scope)
    fill_alpha = reader.number("fillCustom", "transparency", 0.0, **scope)
    outline_on = bool(reader.flag("outline", "show", False, **scope))
    outline_color = reader.color("outline", "lineColor", None, **scope)
    weight = reader.number("outline", "weight", None, **scope)
    padding: list[float | None] = [None, None, None, None]
    if reader.text("padding", "paddingSelection", None, **scope) == "Custom":
        padding = [
            reader.number("padding", key, None, **scope)
            for key in ("topMargin", "rightMargin", "bottomMargin", "leftMargin")
        ]
    accent_on = bool(reader.flag("accentBar", "show", False, **scope))
    return {
        "fill": with_alpha(fill, fill_alpha) if fill_on is not False and fill else None,
        "fillShown": fill_on is not False,
        "outline": {
            "show": outline_on,
            "color": outline_color if outline_on else None,
            "width": (weight if weight is not None else 1.0) if outline_on else None,
            "transparency": reader.number("outline", "transparency", 0.0, **scope),
        },
        "padding": padding if any(p is not None for p in padding) else None,
        "accentBar": {
            "show": accent_on,
            "color": reader.color("accentBar", "color", None, **scope) if accent_on else None,
            "width": reader.number("accentBar", "width", None, **scope) if accent_on else None,
            "position": reader.enum("accentBar", "position", None, **scope) if accent_on else None,
        },
    }


def _has_state(reader: Reader, state: str) -> bool:
    return any(
        (entry.get("selector") or {}).get("id") == state
        for name in ("fillCustom", "outline", "padding", "accentBar", "value")
        for entry in entries_of(reader.objects, name)
    )


def entries_of(objects: Mapping[str, Any], name: str) -> list[Json]:
    """Well-formed entries of one object: a dict with an object (or no) selector and
    properties. A malformed entry is dropped rather than failing the whole visual."""
    found = objects.get(name)
    return (
        [
            e
            for e in found
            if isinstance(e, dict)
            and isinstance(e.get("selector") or {}, dict)
            and isinstance(e.get("properties") or {}, dict)
        ]
        if isinstance(found, list)
        else []
    )


def _value_token(reader: Reader, first: str | None, state: str | None = None) -> Json:
    scope: Json = {"metadata": first}
    if state:
        scope["state"] = state
    callout = text_class(reader.context.theme, "callout")
    size = reader.number("value", "fontSize", None, **scope)
    return {
        "show": bool(reader.flag("value", "show", True, **scope)),
        "fontSize": px(max(10.0, min(72.0, size if size is not None else callout["fontSize"]))),
        "color": reader.color("value", "fontColor", None, **scope)
        or reader.color("value", "color", callout["color"], **scope),
        "family": css_font_family(reader.get("value", "fontFamily", None, **scope))
        or callout["family"],
        "bold": bool(reader.flag("value", "bold", False, **scope)),
        "italic": bool(reader.flag("value", "italic", False, **scope)),
        "alignment": _alignment(reader.text("value", "horizontalAlignment", None, **scope)),
    }


def _alignment(raw: str | None) -> str | None:
    lowered = raw.lower() if raw else None
    return lowered if lowered in _ALIGN else None


def card_visual_tokens(reader: Reader, first: str | None, label_text: str | None) -> Json:
    """The new card: its value, caption label and tile (fill, outline, padding, accent bar)."""
    label_class = text_class(reader.context.theme, "label")
    scope: Json = {"metadata": first}
    size = reader.number("label", "fontSize", None, **scope)
    return {
        "value": _value_token(reader, first),
        "label": {
            "show": bool(reader.flag("label", "show", True, **scope)),
            "text": reader.text("label", "text", label_text, **scope),
            "fontSize": px(size if size is not None else label_class["fontSize"]),
            "color": reader.color("label", "fontColor", None, **scope)
            or reader.color("label", "color", label_class["color"], **scope),
            "family": css_font_family(reader.get("label", "fontFamily", None, **scope))
            or label_class["family"],
            "bold": bool(reader.flag("label", "bold", False, **scope)),
            "italic": bool(reader.flag("label", "italic", False, **scope)),
            "alignment": _alignment(reader.text("label", "horizontalAlignment", None, **scope)),
        },
        "tile": {"default": _tile(reader, None), "selected": None},
    }


def legacy_card_tokens(reader: Reader) -> Json:
    """A classic card: value from ``labels``, caption from ``categoryLabels``."""
    callout = text_class(reader.context.theme, "callout")
    label_class = text_class(reader.context.theme, "label")
    size = reader.number("labels", "fontSize", None)
    caption_size = reader.number("categoryLabels", "fontSize", None)
    units = reader.number("labels", "labelDisplayUnits", None)
    precision = reader.number("labels", "labelPrecision", None)
    return {
        "value": {
            "show": True,
            "fontSize": px(max(10.0, min(72.0, size if size is not None else callout["fontSize"]))),
            "color": reader.color("labels", "color", callout["color"]),
            "family": css_font_family(reader.get("labels", "fontFamily", None))
            or callout["family"],
            "bold": False,
            "italic": False,
            "alignment": None,
            "displayUnits": units if units and units > 0 else None,
            "decimals": int(precision) if precision is not None and precision >= 0 else None,
        },
        "label": {
            "show": bool(reader.flag("categoryLabels", "show", True)),
            "text": None,
            "fontSize": px(caption_size if caption_size is not None else label_class["fontSize"]),
            "color": reader.color("categoryLabels", "color", label_class["color"]),
            "family": css_font_family(reader.get("categoryLabels", "fontFamily", None))
            or label_class["family"],
            "bold": False,
            "italic": False,
            "alignment": None,
        },
        "tile": None,
    }


# --- tables -----------------------------------------------------------------------------


def _column_scope_names(reader: Reader, name: str) -> list[str]:
    return sorted(
        {
            str(entry["selector"]["metadata"])
            for entry in entries_of(reader.objects, name)
            if isinstance(entry.get("selector"), dict)
            and isinstance(entry["selector"].get("metadata"), str)
        }
    )


def table_tokens(reader: Reader) -> Json:
    header = _font(reader, "columnHeaders", "header")
    values = _font(reader, "values", "label", color_key="fontColorPrimary")
    if not reader.own("values").get("fontColorPrimary"):
        # A selector-less fontColor is the same as the primary row color.
        values["color"] = reader.color("values", "fontColor", values["color"])
    secondary_font = reader.color("values", "fontColorSecondary", values["color"])
    primary_fill = reader.color("values", "backColorPrimary", None) or reader.color(
        "values", "backColor", None
    )
    secondary_fill = reader.color("values", "backColorSecondary", None)
    columns: Json = {}
    for query_ref in sorted(
        set(_column_scope_names(reader, "columnFormatting"))
        | set(_column_scope_names(reader, "columnWidth"))
    ):
        scope = {"metadata": query_ref}
        alignment = _alignment(reader.text("columnFormatting", "alignment", None, **scope))
        width = reader.number("columnWidth", "value", None, **scope)
        units = reader.number("columnFormatting", "labelDisplayUnits", None, **scope)
        precision = reader.number("columnFormatting", "labelPrecision", None, **scope)
        columns[query_ref] = {
            "alignment": alignment,
            "styleHeader": bool(reader.flag("columnFormatting", "styleHeader", False, **scope)),
            "width": round(width, 2) if width is not None else None,
            # A column's own font and background colors ("Specific column" in Desktop).
            "fontColor": reader.color("columnFormatting", "fontColor", None, **scope),
            "background": reader.color("columnFormatting", "backColor", None, **scope),
            "displayUnits": units if units and units > 0 else None,
            "decimals": int(precision) if precision is not None and precision >= 0 else None,
        }
    total_size = reader.number("total", "fontSize", None)
    return {
        "table": {
            "header": {
                **header,
                "background": reader.color("columnHeaders", "backColor", None),
                "alignment": _alignment(reader.text("columnHeaders", "alignment", None)),
                "wrap": bool(reader.flag("columnHeaders", "wordWrap", True)),
            },
            "values": {
                **values,
                "secondaryColor": secondary_font,
                "banding": {"primary": primary_fill, "secondary": secondary_fill},
                "wrap": bool(reader.flag("values", "wordWrap", True)),
            },
            "grid": {
                "horizontal": bool(reader.flag("grid", "gridHorizontal", True)),
                "vertical": bool(reader.flag("grid", "gridVertical", False)),
                "horizontalColor": reader.color("grid", "gridHorizontalColor", None),
                "verticalColor": reader.color("grid", "gridVerticalColor", None),
                "horizontalWeight": reader.number("grid", "gridHorizontalWeight", None),
                "verticalWeight": reader.number("grid", "gridVerticalWeight", None),
                "rowPadding": reader.number("grid", "rowPadding", None),
            },
            "columns": columns,
            # A table shows a totals row unless it is switched off (the Agents table is).
            "total": {
                "show": bool(reader.flag("total", "totals", True)),
                "label": reader.text("total", "label", "Total"),
                "fontSize": px(total_size) if total_size is not None else values["fontSize"],
                "color": reader.color("total", "fontColor", values["color"]),
                "family": css_font_family(reader.get("total", "fontFamily", None))
                or values["family"],
                "background": reader.color("total", "backColor", None),
                "bold": bool(reader.flag("total", "bold", True)),
            },
        }
    }


# --- slicers ----------------------------------------------------------------------------


def slicer_tokens(reader: Reader, visual_type: str, caption: str | None) -> Json:
    header = _font(reader, "header", "header")
    items = _font(reader, "items", "label")
    tokens: Json = {
        "slicer": {
            # Desktop's "Dropdown", "Basic" (a list), "Between", "Before"...: the canvas
            # draws dropdown and list; the rest is approximate.
            "mode": (reader.enum("data", "mode", "Dropdown") or "Dropdown").lower(),
            "header": {
                "show": bool(reader.flag("header", "show", True)),
                "text": reader.text("header", "text", caption),
                "bold": bool(reader.flag("header", "bold", False)),
                **header,
            },
            "items": items,
        }
    }
    if visual_type == "slicer" and tokens["slicer"]["mode"] not in {"dropdown", "basic"}:
        reader.note("data.mode")
    if visual_type == "advancedSlicerVisual":
        state = "selection:selected"
        selected = _tile(reader, state) if _has_state(reader, state) else None
        tokens["slicer"]["tile"] = {"default": _tile(reader, None), "selected": selected}
        tokens["slicer"]["values"] = {
            "default": _value_token(reader, None),
            "selected": _value_token(reader, None, state) if selected else None,
        }
        shape_scope = {"state": "default"}
        tokens["slicer"]["layout"] = {
            "rows": _int(reader.number("layout", "rowCount", None)),
            "columns": _int(reader.number("layout", "columnCount", None)),
            "cellPadding": reader.number("layout", "cellPadding", None),
            "shape": reader.enum("shapeCustomRectangle", "tileShape", None, **shape_scope),
            "radius": reader.number(
                "shapeCustomRectangle", "rectangleRoundedCurve", None, **shape_scope
            ),
        }
    return tokens


def _int(number_value: float | None) -> int | None:
    return None if number_value is None else int(number_value)


# --- textbox, shape, image --------------------------------------------------------------

_TAG = re.compile(r"<[^>]{0,300}>")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_PARAGRAPHS = 60
_MAX_RUNS = 120
_MAX_RUN_TEXT = 4000


def _clean_text(raw: Any) -> str:
    """Plain text of a run: control characters and anything tag-shaped removed."""
    if not isinstance(raw, str):
        return ""
    return _CONTROL.sub("", _TAG.sub("", raw))[:_MAX_RUN_TEXT]


def clean_text(raw: Any) -> str | None:
    """Text a measure returned (a dynamic title), made plain like a textbox run; None for a
    value that is not text."""
    return _clean_text(raw) if isinstance(raw, str) else None


def _size_px(raw: Any) -> float | None:
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return px(float(raw))
    if isinstance(raw, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(pt|px)?\s*", raw)
        if match:
            size = float(match.group(1))
            return round(min(size, 400.0), 2) if match.group(2) == "px" else px(size)
    return None


def textbox_content(reader: Reader) -> list[Json]:
    """The textbox's paragraphs as plain-text runs: text, font, size (px), color, emphasis.

    Text is stripped of markup-shaped content and control characters, colors and fonts are
    validated, and hyperlinks are dropped (``hasLink`` says one was there).
    """
    paragraphs: Any = None
    for entry in entries_of(reader.objects, "general"):
        found = (entry.get("properties") or {}).get("paragraphs")
        if isinstance(found, list):
            paragraphs = found
    content: list[Json] = []
    for paragraph in (paragraphs or [])[:_MAX_PARAGRAPHS]:
        if not isinstance(paragraph, dict):
            continue
        runs: list[Json] = []
        for run in (paragraph.get("textRuns") or [])[:_MAX_RUNS]:
            if not isinstance(run, dict):
                continue
            style = run.get("textStyle")
            style = style if isinstance(style, dict) else {}
            weight = style.get("fontWeight")
            runs.append(
                {
                    "text": _clean_text(run.get("value")),
                    "fontFamily": css_font_family(style.get("fontFamily")),
                    "fontSize": _size_px(style.get("fontSize")),
                    "color": safe_color(style.get("color")),
                    "bold": weight == "bold" or (isinstance(weight, int) and weight >= 600),
                    "italic": style.get("fontStyle") == "italic",
                    "underline": style.get("textDecoration") == "underline",
                    "hasLink": bool(run.get("url") or run.get("link")),
                }
            )
        align = str(paragraph.get("horizontalTextAlignment", "left")).lower()
        content.append({"align": align if align in _ALIGN | {"justify"} else "left", "runs": runs})
    return content


# Desktop's own tileShape values (pbir_catalog.json, visualTypes.shape.objects.shape.tileShape),
# not invented names: a rounded rectangle or a triangle written by Desktop must be recognised.
SHAPE_NAMES: tuple[str, ...] = (
    "arrow",
    "arrowChevron",
    "arrowPentagon",
    "heart",
    "hexagon",
    "line",
    "octagon",
    "oval",
    "parallelogram",
    "pentagon",
    "pill",
    "rectangle",
    "rectangleRounded",
    "rectangleRoundedByPixel",
    "tabCutCorner",
    "tabCutTopCorners",
    "tabCutTopCornersByPixel",
    "tabRoundCorner",
    "tabRoundTopCorners",
    "speechbubbleRectangle",
    "trapezoid",
    "triangleIsoc",
    "triangleRight",
)
# The canvas draws these with a box, a corner radius or a line; every other shape arrives as
# a rectangle and the visual is approximate.
DRAWN_SHAPES = frozenset(
    {"rectangle", "rectangleRounded", "rectangleRoundedByPixel", "oval", "pill", "line"}
)
_SHAPE_BY_LOWER = {name.lower(): name for name in SHAPE_NAMES}


def shape_token(reader: Reader) -> Json:
    """A shape's outline and fill, read only from state-selector entries: Desktop ignores
    the shape objects written without ``{"id": "default"}``.

    ``shape`` is what the canvas draws; ``tileShape`` is what the report says (null when it
    names none Desktop has).
    """
    scope: Json = {"state": "default", "selector_required": True}
    raw = reader.get("shape", "tileShape", None, **scope)
    declared = "rectangle" if raw is None else _SHAPE_BY_LOWER.get(str(raw).lower())
    if declared not in DRAWN_SHAPES:
        # A shape Desktop has but the canvas cannot draw, or a name Desktop does not have.
        reader.note("shape.tileShape")
    fill_on = reader.flag("fill", "show", True, **scope)
    fill = reader.color("fill", "fillColor", None, **scope)
    palette = reader.context.palette
    default_fill = palette[2] if len(palette) > 2 and palette[2] else None
    outline_on = bool(reader.flag("outline", "show", False, **scope))
    weight = reader.number("outline", "weight", None, **scope)
    radius = reader.number("shape", "roundEdge", None, **scope)
    if radius is None:
        radius = reader.number("shape", "rectangleRoundedCurve", None, **scope)
    fill_alpha = reader.number("fill", "transparency", 0.0, **scope)
    return {
        "shape": declared if declared in DRAWN_SHAPES else "rectangle",
        "tileShape": declared,
        "fill": with_alpha(fill or default_fill, fill_alpha) if fill_on is not False else None,
        "outline": {
            "show": outline_on,
            "color": reader.color("outline", "lineColor", None, **scope) if outline_on else None,
            "width": (weight if weight is not None else 1.0) if outline_on else None,
        },
        "radius": radius,
        "rotation": reader.number("rotation", "shapeAngle", 0.0, **scope),
    }


# A button's states (instance-selectors.json; "selected" is the Format pane's "On press") and a
# navigator's (its selected button is the current page or bookmark).
BUTTON_STATES = ("default", "hover", "selected", "disabled")
NAVIGATOR_STATES = ("default", "hover", "press", "selected")
_VALIGN = {"top", "middle", "bottom"}
# The navigator's orientation enumeration, as Desktop 2.157's code reads it (the navigation
# family found it there): 2 horizontal, 1 vertical, 0 grid. Unset is horizontal.
_ORIENTATION = {2: "horizontal", 1: "vertical", 0: "grid"}


def _valign(raw: str | None) -> str | None:
    lowered = raw.lower() if raw else None
    return lowered if lowered in _VALIGN else None


def _shown(reader: Reader, name: str, value_key: str, scope: Json) -> bool:
    """A switch that is on when set, or when unset while its value is set (as a title is).

    Desktop's own button defaults write ``show`` without a state selector (the navigation
    family read this in its code), so the switch is read from those entries too."""
    switch = reader.flag(name, "show", None, state=scope.get("state"))
    if switch is not None:
        return switch
    return value_key in reader.own(name, **scope)


def _button_state(reader: Reader, state: str, with_icon: bool) -> Json:
    """Fill, outline, text (and icon) of a button or navigator in one state; a state takes
    what it does not set from the default state."""
    theme = reader.context.theme
    scope: Json = {"state": state, "selector_required": True}
    label = text_class(theme, "label")
    fill = reader.color("fill", "fillColor", None, **scope)
    fill_on = _shown(reader, "fill", "fillColor", scope)
    outline_on = bool(reader.flag("outline", "show", True, state=state))
    weight = reader.number("outline", "weight", None, **scope)
    fallback = safe_color(theme.get("foregroundNeutralSecondary")) or "#605e5c"
    size = reader.number("text", "fontSize", None, **scope)
    token: Json = {
        "fill": with_alpha(
            fill or safe_color(theme.get("background")) or "#ffffff",
            reader.number("fill", "transparency", 0.0, **scope),
        )
        if fill_on
        else None,
        "outline": {
            "show": outline_on,
            "color": (reader.color("outline", "lineColor", None, **scope) or fallback)
            if outline_on
            else None,
            "width": (weight if weight is not None else 1.0) if outline_on else None,
        },
        "text": {
            "show": _shown(reader, "text", "text", scope),
            "text": reader.text("text", "text", None, **scope),
            "fontSize": px(size if size is not None else 12.0),
            "color": reader.color("text", "fontColor", label["color"], **scope),
            "family": css_font_family(reader.get("text", "fontFamily", None, **scope))
            or label["family"],
            "bold": bool(reader.flag("text", "bold", False, **scope)),
            "italic": bool(reader.flag("text", "italic", False, **scope)),
            "underline": bool(reader.flag("text", "underline", False, **scope)),
            "alignment": _alignment(reader.text("text", "horizontalAlignment", None, **scope))
            or "center",
            "verticalAlignment": _valign(reader.text("text", "verticalAlignment", None, **scope))
            or "middle",
        },
    }
    if with_icon:
        icon_size = reader.number("icon", "iconSize", None, **scope)
        token["icon"] = {
            "show": bool(reader.flag("icon", "show", True, state=state)),
            # Desktop's own icon names (blank, leftArrow, rightArrow, back, reset, help,
            # information, qna, bookmarks, ...): the canvas maps them to glyphs.
            "name": reader.enum("icon", "shapeType", None, **scope),
            "color": reader.color("icon", "lineColor", label["color"], **scope),
            "width": reader.number("icon", "lineWeight", None, **scope),
            "size": px(icon_size) if icon_size is not None else None,
            "placement": reader.enum("icon", "placement", None, **scope),
            "alignment": _alignment(reader.text("icon", "horizontalAlignment", None, **scope)),
            "verticalAlignment": _valign(reader.text("icon", "verticalAlignment", None, **scope)),
        }
    return token


def _states_used(reader: Reader, names: Iterable[str]) -> set[str]:
    return {
        str((entry.get("selector") or {}).get("id"))
        for name in names
        for entry in entries_of(reader.objects, name)
        if isinstance((entry.get("selector") or {}).get("id"), str)
    }


def _button_shape(reader: Reader) -> Json:
    raw = reader.get("shape", "tileShape", None)
    declared = "rectangle" if raw is None else _SHAPE_BY_LOWER.get(str(raw).lower())
    if declared not in DRAWN_SHAPES:
        reader.note("shape.tileShape")
    radius = reader.number("shape", "roundEdge", None)
    if radius is None:
        radius = reader.number("shape", "rectangleRoundedCurve", None)
    return {
        "shape": declared if declared in DRAWN_SHAPES else "rectangle",
        "tileShape": declared,
        "radius": radius,
    }


def button_token(reader: Reader, *, navigator: bool = False) -> Json:
    """A button's (or a navigator's buttons') look per state: ``default`` always, ``hover``,
    ``selected`` and ``disabled`` when the visual styles them (else null). The action a
    button runs is on the visual row (``action``), a navigator's buttons in ``items``."""
    names = ("fill", "outline", "text") if navigator else ("fill", "outline", "text", "icon")
    used = _states_used(reader, names)
    token: Json = {
        **_button_shape(reader),
        "states": {
            state: _button_state(reader, state, not navigator)
            if state == "default" or state in used
            else None
            for state in (NAVIGATOR_STATES if navigator else BUTTON_STATES)
        },
    }
    if navigator:
        orientation = reader.number("layout", "orientation", None)
        token["layout"] = {
            "orientation": _ORIENTATION.get(int(orientation) if orientation is not None else 2)
            or "horizontal",
            "rows": _int(reader.number("layout", "rowCount", None)),
            "columns": _int(reader.number("layout", "columnCount", None)),
            "cellPadding": reader.number("layout", "cellPadding", None),
        }
    return token


def image_token(reader: Reader) -> Json:
    """The image of an image visual, as an inert data URI (None when it cannot be read)."""
    source: str | None = None
    node = reader.own("general").get("imageUrl")
    if node is not None:
        resource = walk(node, "expr", "ResourcePackageItem")
        literal = text(node)
        if isinstance(resource, dict) and reader.context.image is not None:
            source = reader.context.image(resource.get("PackageName"), resource.get("ItemName"))
        elif literal is not None and reader.context.literal_image is not None:
            # Only an embedded picture can be drawn; a web address is never fetched.
            source = reader.context.literal_image(literal)
        if source is None:
            # A missing resource, a web address, a rejected SVG or a measure-driven picture.
            reader.note("general.imageUrl")
    scaling = str(reader.get("imageScaling", "imageScalingType", "Normal")).lower()
    return {
        "src": source,
        "scaling": scaling if scaling in {"normal", "fit", "fill"} else "normal",
    }


# --- assembly ---------------------------------------------------------------------------


def group_background(body: Mapping[str, Any], palette: list[str]) -> str | None:
    """A visual group's own background (``visualGroup.objects.background``), if it paints one."""
    group = body.get("visualGroup")
    objects = group.get("objects") if isinstance(group, dict) else None
    entries = objects.get("background") if isinstance(objects, dict) else None
    merged: Json = {}
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("properties"), dict):
            merged.update(entry["properties"])
    if flag(merged.get("show"), None) is not True:
        return None
    fill = color(merged.get("color"), palette)
    return with_alpha(fill, number(merged.get("transparency"), 0.0)) if fill else None
