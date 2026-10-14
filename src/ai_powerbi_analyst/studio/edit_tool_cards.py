"""Tool cards for the operations that existed before the planner (edit_tools.ToolCard).

One card per operation of ``report_edits.REPORT_OPERATIONS`` at the time the planner was added
that Studio still has: layout, alignment and added objects, pages, filters and measures. The
styling operations (titles, fonts, colors, data labels, legends, axes, a visual's box, page
backgrounds, the theme) were removed with their cards: Studio does not change styling, and the
capability matrix refuses such a request (edit_capabilities).
The hand-built parser (edit_parser) reads requests for these operations without a model; the
cards are what the local model chooses from when it could not, what the capability matrix lists
as supported, and what the planner eval is built on.

Every example is a real operation on the Agents Performance dashboard (its structure twin in
fixtures/imported/structure-twin.pbix: one page, the visual ids below), and a test applies each
one to the twin, so an example that stops validating or applying is found at once.

"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .edit_tools import Slot, ToolCard

# --- the Agents dashboard (structure twin) -----------------------------------------------------

PAGE = "ReportSection8454c812b60b8bb83b99"  # its only page, named "1"
TABLE = "e82e02f93055d6b0b544"  # table: Employee Name, Rank, Table MTD Sales, ...
SLICER_TOPN = "953a59a3cad98b0b5a39"  # slicer 'Top-Bottom-N'
CARD_POSITIVE = "fb4c5aa34e0b084b62b9"  # card 'Agents with Positive Change %'
CARD_AVG = "2202e311b1493069e4eb"  # card 'Agents Avg MTD Sales'
CARD_CHANGE = "9479353528b0eb22b958"  # card 'Agents Avg MTD % Change'
CARD_100K = "71a164765d9c63200302"  # card 'Number of Agents Sold above 100K'
CHART_TOTAL = "b404a461ede7604a2774"  # column chart 'Total MTD Sales'
CHART_AVG = "da32cdbb0090e885649b"  # column chart 'Avg MTD Sales by Agents'
CHART_CHANGE = "ab13db00c71dcec86e99"  # column chart 'Avg MTD % change by Agents'
CHART_DISCOUNT = "97d1504080b699eae7d7"  # column chart 'Avg Discount per Transaction'
SLICER_STORE = "5b0dbccd61c6c4adb415"  # slicer 'StoreName'
SLICER_COUNTRY = "54be5cf0783e66471540"  # slicer 'Country' (on ChannelName)
SLICER_CHANNEL = "b85aa81a8b5c60030d13"  # slicer 'ChannelName'
CARD_TOPN = "8a8ba163374a72762030"  # card 'TopN/BottomN'

MEASURES_TABLE = "# Measures"

# Visual types with a legend, axes and data labels (edit_targets.CHART_TYPES, kept here so this
# module imports nothing but the card contract).
CHART_TYPES: tuple[str, ...] = (
    "columnChart",
    "clusteredColumnChart",
    "hundredPercentStackedColumnChart",
    "barChart",
    "clusteredBarChart",
    "hundredPercentStackedBarChart",
    "lineChart",
    "areaChart",
    "stackedAreaChart",
    "lineClusteredColumnComboChart",
    "lineStackedColumnComboChart",
    "pieChart",
    "donutChart",
    "treemap",
    "funnel",
    "waterfallChart",
    "ribbonChart",
    "scatterChart",
)
# The words a person uses for a visual type, and the type Power BI stores.
VISUAL_TYPES: dict[str, str] = {
    "column": "clusteredColumnChart",
    "stacked column": "columnChart",
    "bar": "clusteredBarChart",
    "stacked bar": "barChart",
    "line": "lineChart",
    "area": "areaChart",
    "combo": "lineClusteredColumnComboChart",
    "pie": "pieChart",
    "donut": "donutChart",
    "treemap": "treemap",
    "funnel": "funnel",
    "waterfall": "waterfallChart",
    "ribbon": "ribbonChart",
    "scatter": "scatterChart",
    "gauge": "gauge",
    "kpi": "kpi",
    "card": "cardVisual",
    "multi-row card": "multiRowCard",
    "table": "tableEx",
    "matrix": "pivotTable",
    "slicer": "slicer",
}
SHAPES: dict[str, str] = {
    "rectangle": "rectangle",
    "rounded rectangle": "rectangleRounded",
    "oval": "oval",
    "line": "line",
    "arrow": "arrow",
    "triangle": "triangleIsoc",
    "pentagon": "pentagon",
    "hexagon": "hexagon",
    "chevron": "arrowChevron",
    "speech bubble": "speechbubbleRectangle",
}
ROLES = ("Category", "Y", "Series", "Values", "Rows", "Columns", "Tooltips")
# The conditions the planner offers (report_filters has more; "is" is a list of values): kept
# short because every filter card lists them in the model's prompt.
CONDITIONS = (
    "greater_than",
    "greater_or_equal",
    "less_than",
    "less_or_equal",
    "is_not",
    "contains",
    "is_blank",
)


# --- builders: slots that do not map one to one onto an operation field -------------------------


def _drop_none(values: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


def _typed(value: str) -> str | int | float:
    """A value a person typed: a number when it reads as one (the filter layer types it by the
    field, so 2019 on a text column is still the text '2019')."""
    text = value.strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


def _values(text: Any) -> list[str | int | float]:
    if isinstance(text, list | tuple):
        return [_typed(str(v)) for v in text if str(v).strip()]
    parts = re.split(r"\s*(?:,|;|\bor\b|\band\b)\s*", str(text or ""))
    return [_typed(p.strip(" '\"")) for p in parts if p.strip(" '\"")]


def filter_spec(filled: Mapping[str, Any]) -> dict[str, Any]:
    """The ``filter`` of a filter operation from the filter slots: the top or bottom N when a
    count is given, a condition when one is named, else the listed values."""
    field = filled.get("field")
    top, bottom = filled.get("top"), filled.get("bottom")
    count = top if top is not None else bottom
    if count is not None:
        return _drop_none(
            {
                "kind": "top_n",
                "field": field,
                "n": count,
                "by": filled.get("by"),
                "direction": "top" if top is not None else "bottom",
            }
        )
    condition = filled.get("condition")
    if condition:
        blank = condition in {"is_blank", "is_not_blank"}
        value = filled.get("value")
        return {
            "kind": "advanced",
            "field": field,
            "conditions": [
                _drop_none(
                    {
                        "operator": condition,
                        "value": None if blank or value is None else _typed(str(value)),
                    }
                )
            ],
        }
    return _drop_none(
        {
            "kind": "categorical",
            "field": field,
            "values": _values(filled.get("values")),
            "exclude": True if filled.get("exclude") else None,
        }
    )


def _visual_filter(filled: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "op": "set_visual_filter",
        "visual": filled.get("visual"),
        "filter": filter_spec(filled),
    }


def _page_filter(filled: Mapping[str, Any]) -> dict[str, Any]:
    return {"op": "set_page_filter", "page": filled.get("page"), "filter": filter_spec(filled)}


def _report_filter(filled: Mapping[str, Any]) -> dict[str, Any]:
    return {"op": "set_report_filter", "filter": filter_spec(filled)}


def _clear_filter(filled: Mapping[str, Any]) -> dict[str, Any]:
    scope = filled.get("scope") or (
        "visual" if filled.get("visual") else "page" if filled.get("page") else "report"
    )
    target = filled.get("visual") if scope == "visual" else filled.get("page")
    return _drop_none(
        {
            "op": "clear_filter",
            "scope": scope,
            "target": target if scope != "report" else None,
            "field": filled.get("field"),
        }
    )


def _add_visual(filled: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(filled.get("visual_type") or "column")
    return _drop_none(
        {
            "op": "add_visual",
            "page": filled.get("page"),
            "visual_type": VISUAL_TYPES.get(kind, kind),
            "fields": filled.get("fields"),
            "title": filled.get("title"),
        }
    )


def _change_type(filled: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(filled.get("visual_type") or "")
    return {
        "op": "change_visual_type",
        "visual": filled.get("visual"),
        "visual_type": VISUAL_TYPES.get(kind, kind),
    }


def _add_shape(filled: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(filled.get("shape") or "rectangle")
    return _drop_none(
        {
            "op": "add_shape",
            "page": filled.get("page"),
            "shape": SHAPES.get(kind, kind),
            "fill": filled.get("fill"),
            "outline": filled.get("outline"),
        }
    )


def _move_page(filled: Mapping[str, Any]) -> dict[str, Any]:
    position = filled.get("position")
    return _drop_none(
        {
            "op": "move_page",
            "page": filled.get("page"),
            "index": int(position) - 1 if isinstance(position, int | float) else None,
        }
    )


# --- slots used by several cards ------------------------------------------------------------

_VISUAL = Slot("visual", "visual", "the visual to change")
_PAGE = Slot("page", "page", "the page")
_PAGE_OPTIONAL = Slot("page", "page", "the page (the page on screen when not said)", required=False)
_FILTER_SLOTS = (
    Slot("field", "field", "the column or measure to filter on"),
    Slot("values", "text", "values to keep, comma separated", required=False),
    Slot(
        "exclude",
        "bool",
        "true to drop the listed values instead",
        required=False,
        words=("except", "excluding", "leave out", "out", "all but", "other than", "without"),
    ),
    Slot("condition", "enum", "a condition instead of values", required=False, choices=CONDITIONS),
    Slot("value", "text", "the number or text the condition compares with", required=False),
    Slot("top", "integer", "keep the top N", required=False, minimum=1, maximum=1000),
    Slot("bottom", "integer", "keep the bottom N", required=False, minimum=1, maximum=1000),
    Slot("by", "field", "the measure the top or bottom N is ranked by", required=False),
)


def _size(name: str, description: str, field: str | None = None) -> Slot:
    return Slot(name, "number", description, False, field, (), 6, 72)


# --- layout -------------------------------------------------------------------------------------

_LAYOUT: tuple[ToolCard, ...] = (
    ToolCard(
        op="set_position",
        title="Move or resize a visual",
        family="layout",
        capability="layout",
        summary="Move or resize a visual to exact pixel numbers (x, y, width, height).",
        slots=(
            _VISUAL,
            Slot("x", "number", "left edge in pixels", False, None, (), 0, 20000),
            Slot("y", "number", "top edge in pixels", False, None, (), 0, 20000),
            Slot("width", "number", "width in pixels", False, None, (), 10, 20000),
            Slot("height", "number", "height in pixels", False, None, (), 10, 20000),
        ),
        cues=(
            "move",
            "resize",
            "width",
            "height",
            "wide",
            "tall",
            "position",
            "pixels",
            "px",
            "size of the visual",
        ),
        examples=(
            (
                "move the StoreName slicer to x 1600 and y 40",
                {"op": "set_position", "visual": SLICER_STORE, "x": 1600, "y": 40},
            ),
            ("make the table 800 px wide", {"op": "set_position", "visual": TABLE, "width": 800}),
            (
                "resize Total MTD Sales to 400 by 250",
                {"op": "set_position", "visual": CHART_TOTAL, "width": 400, "height": 250},
            ),
        ),
    ),
    ToolCard(
        op="arrange",
        title="Change the layer order",
        family="layout",
        capability="z_order",
        summary="Bring a visual to the front or send it to the back (layer order).",
        slots=(
            _VISUAL,
            Slot(
                "to",
                "enum",
                "front, back, one step forward or backward",
                choices=("front", "back", "forward", "backward"),
            ),
        ),
        cues=(
            "bring to front",
            "send to back",
            "front",
            "back",
            "layer",
            "on top",
            "behind",
            "forward",
            "backward",
        ),
        examples=(
            ("bring the table to the front", {"op": "arrange", "visual": TABLE, "to": "front"}),
            (
                "send the TopN/BottomN card to the back",
                {"op": "arrange", "visual": CARD_TOPN, "to": "back"},
            ),
            (
                "move the Total MTD Sales chart one layer forward",
                {"op": "arrange", "visual": CHART_TOTAL, "to": "forward"},
            ),
        ),
    ),
    ToolCard(
        op="set_visual_hidden",
        title="Hide or show a visual",
        family="layout",
        capability="visibility",
        summary="Hide a visual or show a hidden one again.",
        slots=(_VISUAL, Slot("hidden", "bool", "true hides the visual, false shows it")),
        cues=("hide", "unhide", "show again", "invisible", "visible"),
        examples=(
            (
                "hide the TopN/BottomN card",
                {"op": "set_visual_hidden", "visual": CARD_TOPN, "hidden": True},
            ),
            (
                "hide the Country slicer",
                {"op": "set_visual_hidden", "visual": SLICER_COUNTRY, "hidden": True},
            ),
            (
                "make the Avg Discount per Transaction chart invisible",
                {"op": "set_visual_hidden", "visual": CHART_DISCOUNT, "hidden": True},
            ),
        ),
    ),
    ToolCard(
        op="delete_visual",
        title="Delete a visual",
        family="layout",
        capability="visual_lifecycle",
        summary="Delete a visual from its page.",
        slots=(_VISUAL,),
        cues=("delete", "remove the visual", "remove the chart", "get rid of", "drop"),
        examples=(
            ("delete the TopN/BottomN card", {"op": "delete_visual", "visual": CARD_TOPN}),
            ("remove the Country slicer", {"op": "delete_visual", "visual": SLICER_COUNTRY}),
            (
                "get rid of the Avg Discount per Transaction chart",
                {"op": "delete_visual", "visual": CHART_DISCOUNT},
            ),
        ),
    ),
    ToolCard(
        op="duplicate_visual",
        title="Copy a visual",
        family="layout",
        capability="visual_lifecycle",
        summary="Copy a visual, on the same page or onto another page.",
        slots=(
            _VISUAL,
            Slot("page", "page", "the page the copy goes to", required=False),
            Slot("x", "number", "left edge of the copy in pixels", False, None, (), 0, 20000),
            Slot("y", "number", "top edge of the copy in pixels", False, None, (), 0, 20000),
        ),
        cues=("duplicate", "copy", "clone", "another one like"),
        examples=(
            (
                "duplicate the Total MTD Sales chart",
                {"op": "duplicate_visual", "visual": CHART_TOTAL},
            ),
            ("make a copy of the table", {"op": "duplicate_visual", "visual": TABLE}),
            (
                "copy the Agents Avg MTD Sales card to x 20 y 20",
                {"op": "duplicate_visual", "visual": CARD_AVG, "x": 20, "y": 20},
            ),
        ),
    ),
    ToolCard(
        op="add_visual",
        title="Add a visual",
        family="layout",
        capability="visual_lifecycle",
        summary="Add a new visual (chart, card, table, matrix or slicer) with the fields it shows.",
        slots=(
            _PAGE_OPTIONAL,
            Slot("visual_type", "enum", "the kind of visual", choices=tuple(VISUAL_TYPES)),
            Slot("fields", "fields", "the measures and columns it shows"),
            Slot("title", "text", "its title", required=False),
        ),
        build=_add_visual,
        cues=(
            "add a chart",
            "new chart",
            "add a visual",
            "add a card",
            "add a table",
            "add a slicer",
            "add a matrix",
            "create a chart",
            "insert a",
            "new visual",
            "new card",
            "new table",
            "new slicer",
            "put a card",
            "pie chart",
            "line chart",
            "bar chart",
        ),
        examples=(
            (
                "add a column chart of Total Sales by StoreName",
                {
                    "op": "add_visual",
                    "page": PAGE,
                    "visual_type": "clusteredColumnChart",
                    "fields": [
                        {"table": MEASURES_TABLE, "name": "Total Sales"},
                        {"table": "DimStore", "name": "StoreName"},
                    ],
                },
            ),
            (
                "add a card showing Profit",
                {
                    "op": "add_visual",
                    "page": PAGE,
                    "visual_type": "cardVisual",
                    "fields": [{"table": MEASURES_TABLE, "name": "Profit"}],
                },
            ),
            (
                "add a slicer for Year",
                {
                    "op": "add_visual",
                    "page": PAGE,
                    "visual_type": "slicer",
                    "fields": [{"table": "Date", "name": "Year"}],
                },
            ),
        ),
    ),
    ToolCard(
        op="change_visual_type",
        title="Change the visual type",
        family="layout",
        capability="visual_lifecycle",
        summary="Turn a visual into another kind (a column chart into a bar or line chart...).",
        slots=(
            _VISUAL,
            Slot("visual_type", "enum", "the new kind of visual", choices=tuple(VISUAL_TYPES)),
        ),
        build=_change_type,
        cues=(
            "change to",
            "turn into",
            "make it a",
            "convert",
            "switch to",
            "chart type",
            "instead of a",
        ),
        examples=(
            (
                "turn Total MTD Sales into a line chart",
                {"op": "change_visual_type", "visual": CHART_TOTAL, "visual_type": "lineChart"},
            ),
            (
                "make Avg MTD Sales by Agents a bar chart",
                {
                    "op": "change_visual_type",
                    "visual": CHART_AVG,
                    "visual_type": "clusteredBarChart",
                },
            ),
            (
                "change the Avg Discount per Transaction chart to an area chart",
                {"op": "change_visual_type", "visual": CHART_DISCOUNT, "visual_type": "areaChart"},
            ),
        ),
    ),
    ToolCard(
        op="set_fields",
        title="Change the fields of a visual",
        family="layout",
        capability="fields",
        summary="Add, remove or replace the fields in one well of a visual (Values, Category, "
        "Y, Series, Rows, Columns, Tooltips).",
        slots=(
            _VISUAL,
            Slot("role", "enum", "the field well", choices=ROLES),
            Slot("fields", "fields", "the fields"),
            Slot(
                "mode",
                "enum",
                "add them, remove them or replace the well",
                required=False,
                choices=("replace", "add", "remove"),
            ),
        ),
        cues=(
            "add to the table",
            "remove from the table",
            "field",
            "fields",
            "column to the",
            "well",
            "tooltips",
            "replace with",
            "put on the axis",
            "include",
            "take out of",
            "add to",
            "remove from",
        ),
        examples=(
            (
                "add Profit to the table",
                {
                    "op": "set_fields",
                    "visual": TABLE,
                    "role": "Values",
                    "mode": "add",
                    "fields": [{"table": MEASURES_TABLE, "name": "Profit"}],
                },
            ),
            (
                "remove Rank from the table",
                {
                    "op": "set_fields",
                    "visual": TABLE,
                    "role": "Values",
                    "mode": "remove",
                    "fields": [{"table": MEASURES_TABLE, "name": "Rank"}],
                },
            ),
            (
                "put Total Sales in the tooltips of Total MTD Sales",
                {
                    "op": "set_fields",
                    "visual": CHART_TOTAL,
                    "role": "Tooltips",
                    "mode": "add",
                    "fields": [{"table": MEASURES_TABLE, "name": "Total Sales"}],
                },
            ),
        ),
    ),
    ToolCard(
        op="set_sort",
        title="Sort a visual",
        family="layout",
        capability="fields",
        summary="Sort a visual by one of its fields, ascending or descending.",
        slots=(
            _VISUAL,
            Slot("field", "field", "the field to sort by"),
            Slot(
                "direction",
                "enum",
                "ascending or descending",
                required=False,
                choices=("ascending", "descending"),
            ),
        ),
        cues=(
            "sort",
            "order by",
            "ascending",
            "descending",
            "highest first",
            "lowest first",
            "alphabetical",
        ),
        examples=(
            (
                "sort the table by Rank",
                {
                    "op": "set_sort",
                    "visual": TABLE,
                    "field": {"table": MEASURES_TABLE, "name": "Rank"},
                    "direction": "descending",
                },
            ),
            (
                "sort Total MTD Sales by TopN/BottomN/ MTD Sales ascending",
                {
                    "op": "set_sort",
                    "visual": CHART_TOTAL,
                    "field": {"table": MEASURES_TABLE, "name": "TopN/BottomN/ MTD Sales"},
                    "direction": "ascending",
                },
            ),
            (
                "order the table by Table MTD Sales, highest first",
                {
                    "op": "set_sort",
                    "visual": TABLE,
                    "field": {"table": MEASURES_TABLE, "name": "Table MTD Sales"},
                    "direction": "descending",
                },
            ),
        ),
    ),
)

# --- align, text boxes and shapes -------------------------------------------------------------

_ALIGN: tuple[ToolCard, ...] = (
    ToolCard(
        op="align_visuals",
        title="Align visuals",
        family="align",
        capability="align_distribute",
        summary="Line visuals up on one edge or center, with each other or with the page.",
        slots=(
            Slot("visuals", "visuals", "the visuals to line up"),
            Slot(
                "edge",
                "enum",
                "the edge or center line",
                choices=("left", "right", "top", "bottom", "center_h", "center_v"),
            ),
            Slot(
                "to",
                "enum",
                "with each other, or with the page",
                required=False,
                choices=("selection", "page"),
            ),
        ),
        cues=("align", "line up", "center", "centre", "same left edge", "flush"),
        examples=(
            (
                "align the cards to the top",
                {
                    "op": "align_visuals",
                    "visuals": [CARD_POSITIVE, CARD_AVG, CARD_CHANGE, CARD_100K],
                    "edge": "top",
                    "to": "selection",
                },
            ),
            (
                "align the two top charts to the left of the page",
                {
                    "op": "align_visuals",
                    "visuals": [CHART_TOTAL, CHART_AVG],
                    "edge": "left",
                    "to": "page",
                },
            ),
            (
                "center the table horizontally on the page",
                {"op": "align_visuals", "visuals": [TABLE], "edge": "center_h", "to": "page"},
            ),
        ),
    ),
    ToolCard(
        op="distribute_visuals",
        title="Space visuals evenly",
        family="align",
        capability="align_distribute",
        summary="Space three or more visuals evenly, across or down.",
        slots=(
            Slot("visuals", "visuals", "the visuals to space out"),
            Slot("axis", "enum", "across or down", choices=("horizontal", "vertical")),
        ),
        cues=("distribute", "space evenly", "even spacing", "equal gaps", "spread out"),
        examples=(
            (
                "space the cards evenly",
                {
                    "op": "distribute_visuals",
                    "visuals": [CARD_POSITIVE, CARD_AVG, CARD_CHANGE, CARD_100K],
                    "axis": "horizontal",
                },
            ),
            (
                "distribute the slicers vertically",
                {
                    "op": "distribute_visuals",
                    "visuals": [SLICER_TOPN, SLICER_STORE, SLICER_COUNTRY, SLICER_CHANNEL],
                    "axis": "vertical",
                },
            ),
            (
                "spread the three slicers at the top evenly across",
                {
                    "op": "distribute_visuals",
                    "visuals": [SLICER_CHANNEL, SLICER_COUNTRY, SLICER_STORE],
                    "axis": "horizontal",
                },
            ),
        ),
    ),
    ToolCard(
        op="swap_visuals",
        title="Swap two visuals",
        family="align",
        capability="align_distribute",
        summary="Swap the places of two visuals.",
        slots=(
            Slot("first", "visual", "one visual", field="a"),
            Slot("second", "visual", "the other visual", field="b"),
        ),
        cues=("swap", "switch places", "trade places", "exchange"),
        examples=(
            (
                "swap the two top charts",
                {"op": "swap_visuals", "a": CHART_TOTAL, "b": CHART_AVG},
            ),
            (
                "swap the StoreName slicer and the ChannelName slicer",
                {"op": "swap_visuals", "a": SLICER_STORE, "b": SLICER_CHANNEL},
            ),
            (
                "switch places of Avg MTD % change by Agents and Avg Discount per Transaction",
                {"op": "swap_visuals", "a": CHART_CHANGE, "b": CHART_DISCOUNT},
            ),
        ),
    ),
    ToolCard(
        op="place_visual",
        title="Place a visual by another one",
        family="align",
        capability="align_distribute",
        summary="Put a visual next to, above or below another one, or line it up with it.",
        slots=(
            _VISUAL,
            Slot("next_to", "visual", "the visual it goes next to", field="relative_to"),
            Slot(
                "side",
                "enum",
                "where it goes",
                choices=("left_of", "right_of", "above", "below", "align_with"),
            ),
            Slot("gap", "number", "gap in pixels", False, None, (), 0, 2000),
        ),
        cues=(
            "next to",
            "below",
            "above",
            "beside",
            "under",
            "left of",
            "right of",
            "put it after",
        ),
        examples=(
            (
                "put the StoreName slicer below the ChannelName slicer",
                {
                    "op": "place_visual",
                    "visual": SLICER_STORE,
                    "relative_to": SLICER_CHANNEL,
                    "side": "below",
                },
            ),
            (
                "put the table 30 px below the cards",
                {
                    "op": "place_visual",
                    "visual": TABLE,
                    "relative_to": CARD_TOPN,
                    "side": "below",
                    "gap": 30,
                },
            ),
            (
                "place Avg MTD Sales by Agents right of Total MTD Sales",
                {
                    "op": "place_visual",
                    "visual": CHART_AVG,
                    "relative_to": CHART_TOTAL,
                    "side": "right_of",
                },
            ),
        ),
    ),
    ToolCard(
        op="add_textbox",
        title="Add a text box",
        family="align",
        capability="text_boxes",
        summary="Add a text box with the given words to a page.",
        slots=(
            _PAGE_OPTIONAL,
            Slot("text", "text", "the words in the text box"),
            _size("font_size", "text size in points"),
            Slot("color", "color", "text color", required=False),
            Slot("bold", "bool", "bold text", required=False),
        ),
        cues=("text box", "textbox", "note", "label saying", "add text", "heading text"),
        examples=(
            (
                "add a text box saying Agent performance overview",
                {"op": "add_textbox", "page": PAGE, "text": "Agent performance overview"},
            ),
            (
                "add a bold text box that says Q3 review",
                {"op": "add_textbox", "page": PAGE, "text": "Q3 review", "bold": True},
            ),
            (
                "put a note Data refreshed daily in 10 pt",
                {
                    "op": "add_textbox",
                    "page": PAGE,
                    "text": "Data refreshed daily",
                    "font_size": 10,
                },
            ),
        ),
    ),
    ToolCard(
        op="add_shape",
        title="Add a shape",
        family="align",
        capability="shapes",
        summary="Add a shape (rectangle, oval, line, arrow...) with a fill and outline color.",
        slots=(
            _PAGE_OPTIONAL,
            Slot("shape", "enum", "the shape", required=False, choices=tuple(SHAPES)),
            Slot("fill", "color", "fill color", required=False),
            Slot("outline", "color", "outline color", required=False),
        ),
        build=_add_shape,
        cues=("shape", "rectangle", "oval", "circle", "arrow", "line", "triangle", "divider"),
        examples=(
            (
                "add a blue rectangle",
                {"op": "add_shape", "page": PAGE, "shape": "rectangle", "fill": "#0078D4"},
            ),
            ("add an oval", {"op": "add_shape", "page": PAGE, "shape": "oval"}),
            (
                "add a red arrow with a black outline",
                {
                    "op": "add_shape",
                    "page": PAGE,
                    "shape": "arrow",
                    "fill": "#E81123",
                    "outline": "#000000",
                },
            ),
        ),
    ),
)

# --- pages ------------------------------------------------------------------------------------

_PAGES: tuple[ToolCard, ...] = (
    ToolCard(
        op="rename_page",
        title="Rename a page",
        family="pages",
        capability="pages",
        summary="Rename a page.",
        slots=(_PAGE, Slot("name", "text", "the new page name")),
        cues=("rename the page", "page name", "call the page", "rename page", "tab name"),
        examples=(
            (
                "rename page 1 to Overview",
                {"op": "rename_page", "page": PAGE, "name": "Overview"},
            ),
            (
                "call the page Agent Performance",
                {"op": "rename_page", "page": PAGE, "name": "Agent Performance"},
            ),
            (
                "change the name of the first page to Summary",
                {"op": "rename_page", "page": PAGE, "name": "Summary"},
            ),
        ),
    ),
    ToolCard(
        op="add_page",
        title="Add a page",
        family="pages",
        capability="pages",
        summary="Add a new empty page.",
        slots=(
            Slot("name", "text", "the new page's name"),
            Slot("width", "number", "width in pixels", False, None, (), 100, 20000),
            Slot("height", "number", "height in pixels", False, None, (), 100, 20000),
        ),
        cues=("new page", "add a page", "add page", "another page", "blank page", "new tab"),
        examples=(
            ("add a page called Details", {"op": "add_page", "name": "Details"}),
            ("add a new page named Forecast", {"op": "add_page", "name": "Forecast"}),
            (
                "add a 1920 by 1080 page called Wide",
                {"op": "add_page", "name": "Wide", "width": 1920, "height": 1080},
            ),
        ),
    ),
    ToolCard(
        op="duplicate_page",
        title="Copy a page",
        family="pages",
        capability="pages",
        summary="Copy a page with everything on it.",
        slots=(_PAGE, Slot("name", "text", "the copy's name", required=False)),
        cues=("duplicate the page", "copy the page", "duplicate page", "copy of the page"),
        examples=(
            ("duplicate page 1", {"op": "duplicate_page", "page": PAGE}),
            (
                "copy the page and call it Agents copy",
                {"op": "duplicate_page", "page": PAGE, "name": "Agents copy"},
            ),
            ("make a copy of this page", {"op": "duplicate_page", "page": PAGE}),
        ),
    ),
    ToolCard(
        op="delete_page",
        title="Delete a page",
        family="pages",
        capability="pages",
        summary="Delete a page.",
        slots=(_PAGE,),
        cues=("delete the page", "remove the page", "delete page", "get rid of the page"),
        examples=(
            ("delete the Details page", {"op": "delete_page", "page": "Details"}),
            ("remove page Details", {"op": "delete_page", "page": "Details"}),
            ("get rid of the Details page", {"op": "delete_page", "page": "Details"}),
        ),
    ),
    ToolCard(
        op="move_page",
        title="Move a page",
        family="pages",
        capability="pages",
        summary="Move a page to another position in the page tabs.",
        slots=(
            _PAGE,
            Slot("position", "integer", "its new position, 1 is first", True, None, (), 1, 500),
        ),
        build=_move_page,
        cues=("move the page", "reorder", "page order", "first page", "last page", "tab order"),
        examples=(
            (
                "move the Details page to the front",
                {"op": "move_page", "page": "Details", "index": 0},
            ),
            ("make page 1 the second page", {"op": "move_page", "page": PAGE, "index": 1}),
            ("put Details first", {"op": "move_page", "page": "Details", "index": 0}),
        ),
    ),
    ToolCard(
        op="set_page_size",
        title="Resize a page",
        family="pages",
        capability="page_format",
        summary="Change a page's size in pixels (the visuals scale with it).",
        slots=(
            _PAGE_OPTIONAL,
            Slot("width", "number", "width in pixels", True, None, (), 100, 20000),
            Slot("height", "number", "height in pixels", True, None, (), 100, 20000),
        ),
        cues=(
            "page size",
            "canvas size",
            "canvas",
            "16:9",
            "4:3",
            "resolution",
            "1280x720",
            "1920x1080",
        ),
        examples=(
            (
                "make the page 1280 by 720",
                {"op": "set_page_size", "page": PAGE, "width": 1280, "height": 720},
            ),
            (
                "set page 1 to 1920x1080",
                {"op": "set_page_size", "page": PAGE, "width": 1920, "height": 1080},
            ),
            (
                "change the canvas size to 1600 x 900",
                {"op": "set_page_size", "page": PAGE, "width": 1600, "height": 900},
            ),
        ),
    ),
    ToolCard(
        op="set_page_hidden",
        title="Hide or show a page",
        family="pages",
        capability="pages",
        summary="Hide a page from the page tabs or show it again.",
        slots=(_PAGE, Slot("hidden", "bool", "true hides the page, false shows it")),
        cues=("hide the page", "hidden page", "unhide the page", "show the page"),
        examples=(
            ("hide the Details page", {"op": "set_page_hidden", "page": "Details", "hidden": True}),
            (
                "hide page Details from the tabs",
                {"op": "set_page_hidden", "page": "Details", "hidden": True},
            ),
            (
                "make the Details page hidden",
                {"op": "set_page_hidden", "page": "Details", "hidden": True},
            ),
        ),
    ),
)

# --- measures (the model layer: applied through Power BI Desktop, always confirmed) -------------

_MEASURES: tuple[ToolCard, ...] = (
    ToolCard(
        op="create_measure",
        title="Create a measure",
        family="measures",
        capability="measures",
        needs_desktop=True,
        confirm=True,
        summary="Create a new measure with a DAX formula.",
        slots=(
            Slot("name", "text", "the new measure's name"),
            Slot("expression", "dax", "the DAX formula"),
            Slot("table", "table", "the table it lives in", required=False),
            Slot("format_string", "text", "a format such as 0.0% or #,0", required=False),
        ),
        cues=(
            "new measure",
            "create a measure",
            "add a measure",
            "measure called",
            "calculate",
            "dax",
            "formula",
        ),
        examples=(
            (
                "create a measure Profit Margin = DIVIDE([Profit], [Total Sales])",
                {
                    "op": "create_measure",
                    "table": MEASURES_TABLE,
                    "name": "Profit Margin",
                    "expression": "DIVIDE([Profit], [Total Sales])",
                },
            ),
            (
                "add a measure Total Cost = SUM(FactSales[TotalCost])",
                {
                    "op": "create_measure",
                    "table": MEASURES_TABLE,
                    "name": "Total Cost",
                    "expression": "SUM(FactSales[TotalCost])",
                },
            ),
            (
                "new measure Discount % as DIVIDE(SUM(FactSales[DiscountAmount]), [Total Sales]) "
                "formatted as 0.0%",
                {
                    "op": "create_measure",
                    "table": MEASURES_TABLE,
                    "name": "Discount %",
                    "expression": "DIVIDE(SUM(FactSales[DiscountAmount]), [Total Sales])",
                    "format_string": "0.0%",
                },
            ),
        ),
    ),
    ToolCard(
        op="update_measure",
        title="Change a measure",
        family="measures",
        capability="measures",
        needs_desktop=True,
        confirm=True,
        # (UpdateMeasure also has ``hidden``, which the Desktop step refuses for now: no slot.)
        summary="Change a measure's DAX formula or its format string.",
        slots=(
            Slot("name", "measure", "the measure to change"),
            Slot("expression", "dax", "its new DAX formula", required=False),
            Slot("format_string", "text", "a format such as 0.0% or #,0", required=False),
        ),
        cues=(
            "change the measure",
            "update the measure",
            "change the formula",
            "edit the measure",
            "format of the measure",
            "measure format",
            "formatted as",
        ),
        examples=(
            (
                "change Profit to SUMX(FactSales, FactSales[SalesAmount] - FactSales[TotalCost] - "
                "FactSales[DiscountAmount])",
                {
                    "op": "update_measure",
                    "name": "Profit",
                    "expression": "SUMX(FactSales, FactSales[SalesAmount] - FactSales[TotalCost] "
                    "- FactSales[DiscountAmount])",
                },
            ),
            (
                "format Total Sales as #,0",
                {"op": "update_measure", "name": "Total Sales", "format_string": "#,0"},
            ),
            (
                "show Employees Avg MTD % Change as a percentage with one decimal",
                {
                    "op": "update_measure",
                    "name": "Employees Avg MTD % Change",
                    "format_string": "0.0%",
                },
            ),
        ),
    ),
    ToolCard(
        op="rename_measure",
        title="Rename a measure",
        family="measures",
        capability="measures",
        needs_desktop=True,
        confirm=True,
        summary="Rename a measure (the visuals and formulas that use it follow).",
        slots=(
            Slot("name", "measure", "the measure to rename"),
            Slot("new_name", "text", "its new name"),
        ),
        cues=("rename the measure", "measure name", "call the measure"),
        examples=(
            (
                "rename the measure Rank to Agent Rank",
                {"op": "rename_measure", "name": "Rank", "new_name": "Agent Rank"},
            ),
            (
                "call the Profit measure Gross Profit",
                {"op": "rename_measure", "name": "Profit", "new_name": "Gross Profit"},
            ),
            (
                "rename Total Sales to Sales Amount",
                {"op": "rename_measure", "name": "Total Sales", "new_name": "Sales Amount"},
            ),
        ),
    ),
    ToolCard(
        op="delete_measure",
        title="Delete a measure",
        family="measures",
        capability="measures",
        needs_desktop=True,
        confirm=True,
        summary="Delete a measure from the model.",
        slots=(Slot("name", "measure", "the measure to delete"),),
        cues=("delete the measure", "remove the measure", "drop the measure"),
        examples=(
            ("delete the measure Profit", {"op": "delete_measure", "name": "Profit"}),
            ("remove the Profit measure", {"op": "delete_measure", "name": "Profit"}),
            ("drop Profit from the model", {"op": "delete_measure", "name": "Profit"}),
        ),
    ),
)

# --- filters ------------------------------------------------------------------------------------

_FILTER_CUES = (
    "filter",
    "only show",
    "exclude",
    "keep only",
    "top",
    "bottom",
    "greater than",
    "less than",
    "where",
    "just",
    "restrict",
    "limit",
    "leave out",
)

_FILTERS: tuple[ToolCard, ...] = (
    ToolCard(
        op="set_visual_filter",
        title="Filter a visual",
        family="filters",
        capability="filters",
        summary="Filter one visual: keep listed values, a condition, or the top or bottom N.",
        slots=(_VISUAL, *_FILTER_SLOTS),
        build=_visual_filter,
        cues=(*_FILTER_CUES, "on this chart", "in the table", "visual filter"),
        examples=(
            (
                "filter Total MTD Sales to the Online channel",
                {
                    "op": "set_visual_filter",
                    "visual": CHART_TOTAL,
                    "filter": {
                        "kind": "categorical",
                        "field": {"table": "DimChannel", "name": "ChannelName"},
                        "values": ["Online"],
                    },
                },
            ),
            (
                "only show agents with Total Sales above 100000 in the table",
                {
                    "op": "set_visual_filter",
                    "visual": TABLE,
                    "filter": {
                        "kind": "advanced",
                        "field": {"table": MEASURES_TABLE, "name": "Total Sales"},
                        "conditions": [{"operator": "greater_than", "value": 100000}],
                    },
                },
            ),
            (
                "show the top 5 LastName by Total Sales in the table",
                {
                    "op": "set_visual_filter",
                    "visual": TABLE,
                    "filter": {
                        "kind": "top_n",
                        "field": {"table": "DimEmployee", "name": "LastName"},
                        "n": 5,
                        "by": {"table": MEASURES_TABLE, "name": "Total Sales"},
                        "direction": "top",
                    },
                },
            ),
        ),
    ),
    ToolCard(
        op="set_page_filter",
        title="Filter a page",
        family="filters",
        capability="filters",
        summary="Filter every visual on a page: keep listed values, a condition, or the top or "
        "bottom N.",
        slots=(_PAGE_OPTIONAL, *_FILTER_SLOTS),
        build=_page_filter,
        cues=(*_FILTER_CUES, "on this page", "page filter", "on the page"),
        examples=(
            (
                "only show 2019 on this page",
                {
                    "op": "set_page_filter",
                    "page": PAGE,
                    "filter": {
                        "kind": "categorical",
                        "field": {"table": "Date", "name": "Year"},
                        "values": [2019],
                    },
                },
            ),
            (
                "exclude the Online channel from page 1",
                {
                    "op": "set_page_filter",
                    "page": PAGE,
                    "filter": {
                        "kind": "categorical",
                        "field": {"table": "DimChannel", "name": "ChannelName"},
                        "values": ["Online"],
                        "exclude": True,
                    },
                },
            ),
            (
                "filter the page to sales amounts greater than 1000",
                {
                    "op": "set_page_filter",
                    "page": PAGE,
                    "filter": {
                        "kind": "advanced",
                        "field": {"table": "FactSales", "name": "SalesAmount"},
                        "conditions": [{"operator": "greater_than", "value": 1000}],
                    },
                },
            ),
        ),
    ),
    ToolCard(
        op="set_report_filter",
        title="Filter the report",
        family="filters",
        capability="filters",
        summary="Filter every page of the report: keep listed values, a condition, or the top "
        "or bottom N.",
        slots=_FILTER_SLOTS,
        build=_report_filter,
        cues=(*_FILTER_CUES, "whole report", "all pages", "every page", "report filter"),
        examples=(
            (
                "filter the whole report to 2019 and 2020",
                {
                    "op": "set_report_filter",
                    "filter": {
                        "kind": "categorical",
                        "field": {"table": "Date", "name": "Year"},
                        "values": [2019, 2020],
                    },
                },
            ),
            (
                "on every page leave out the Online channel",
                {
                    "op": "set_report_filter",
                    "filter": {
                        "kind": "categorical",
                        "field": {"table": "DimChannel", "name": "ChannelName"},
                        "values": ["Online"],
                        "exclude": True,
                    },
                },
            ),
            (
                "only keep stores whose name contains Contoso in the whole report",
                {
                    "op": "set_report_filter",
                    "filter": {
                        "kind": "advanced",
                        "field": {"table": "DimStore", "name": "StoreName"},
                        "conditions": [{"operator": "contains", "value": "Contoso"}],
                    },
                },
            ),
        ),
    ),
    ToolCard(
        op="clear_filter",
        title="Remove filters",
        family="filters",
        capability="filters",
        summary="Remove the filters of a visual, a page or the report (one field's, or all).",
        slots=(
            Slot(
                "scope",
                "enum",
                "a visual, a page or the report",
                choices=("visual", "page", "report"),
            ),
            Slot("visual", "visual", "the visual, for a visual filter", required=False),
            Slot("page", "page", "the page, for a page filter", required=False),
            Slot("field", "field", "only the filter on this field", required=False),
        ),
        build=_clear_filter,
        cues=(
            "clear the filter",
            "remove the filter",
            "remove filters",
            "reset the filter",
            "no filter",
            "unfilter",
            "show everything",
            "filter off",
            "take off",
        ),
        examples=(
            (
                "remove the Year filter from this page",
                {
                    "op": "clear_filter",
                    "scope": "page",
                    "target": PAGE,
                    "field": {"table": "Date", "name": "Year"},
                },
            ),
            (
                "clear the filters of the Total MTD Sales chart",
                {"op": "clear_filter", "scope": "visual", "target": CHART_TOTAL},
            ),
            ("remove all report filters", {"op": "clear_filter", "scope": "report"}),
        ),
    ),
)

TOOL_CARDS: tuple[ToolCard, ...] = (
    *_LAYOUT,
    *_ALIGN,
    *_PAGES,
    *_MEASURES,
    *_FILTERS,
)

__all__ = ["CHART_TYPES", "TOOL_CARDS", "VISUAL_TYPES", "filter_spec"]
