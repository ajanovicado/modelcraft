"""Analytics lines, visual calculations and small multiples, computed from a visual's rows.

Everything here runs in Python over the rows the query returned (at most 101): Power BI
draws an average line at the average of the plotted values, a running sum over the visual's
own axis, a small multiple per value of the small-multiples field. The definitions are read
from the file alone, so the formatting-only render reports the same fidelity as the data
render; the values exist only in the data render.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from . import pbir_expr
from .report_props import alpha_of, color, enum_text

Json = dict[str, Any]

# Chart objects that draw a line, and what the line's value is.
LINE_OBJECTS: dict[str, tuple[str, Literal["value", "category"]]] = {
    "y1AxisReferenceLine": ("constant", "value"),
    "referenceLine": ("constant", "value"),
    "xAxisReferenceLine": ("constant", "category"),
    "minLine": ("min", "value"),
    "maxLine": ("max", "value"),
    "averageLine": ("average", "value"),
    "medianLine": ("median", "value"),
    "percentileLine": ("percentile", "value"),
    "trend": ("trend", "value"),
}
LINE_PROPERTIES = frozenset(
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

UNVERIFIED_SHAPES: dict[str, str] = {
    "y1AxisReferenceLine/xAxisReferenceLine/referenceLine": (
        "One entry per line under a selector {id}; value a numeric literal (constant), a "
        "Measure (constant from a measure), {Aggregation: {Expression: {SelectRef: "
        "{ExpressionName: <queryRef>}}, Function: 1 average|3 min|4 max|6 median}} or "
        "{Percentile: {Expression: <SelectRef>, K: 0..1}} (the extras family writes these). "
        "Property names from the visual capabilities, no Desktop-authored file in hand"
    ),
    "minLine/maxLine/averageLine/medianLine/percentileLine": (
        "Object names of dynamic analytics lines in older reports, not in the capabilities "
        "catalog; read as one entry per line with an optional metadata selector naming the "
        "measure and percentile (0-100) on percentileLine"
    ),
    "trend": (
        "One entry; show defaults to off; combineSeries on (the default) draws one line over "
        "every measure's points, off one line per measure (x = the category's position); off "
        "with a legend well (one line per legend series) is listed unrendered"
    ),
    "Percentile.Exclusive": (
        "{Percentile: {Expression, K, Exclusive: true}} for PERCENTILE.EXC (rank K x (n + 1), "
        "blank outside 1..n); without it PERCENTILE.INC"
    ),
    "NativeVisualCalculation": (
        "Projection field {NativeVisualCalculation: {Language: 'dax', Expression, Name}}; "
        "evaluated over the visual's axis in ascending order of its category fields"
    ),
    "smallMultiplesLayout": "rowCount/columnCount/layoutType read as the grid of panels",
}

_STYLES = {"solid", "dashed", "dotted", "custom"}
_LABEL_TEXT = {"value": "Value", "name": "Name", "valueandname": "ValueAndName"}


@dataclass(frozen=True)
class LineSpec:
    """One analytics line as the file defines it.

    ``kind`` is ``constant``, ``measure`` (a constant a measure gives for the whole visual:
    ``measure`` holds its expression and the caller puts its field in ``value_index``),
    ``min``, ``max``, ``average``, ``median``, ``percentile`` or ``trend``.
    """

    object: str
    kind: str
    axis: Literal["value", "category"]
    id: str | None
    series: str | None
    constant: Any
    percentile: float | None
    style: Json
    measure: Json | None = None
    value_index: int | None = None
    # PERCENTILE.EXC instead of PERCENTILE.INC.
    exclusive: bool = False
    # A trend line over all series together (Power BI's default) or one per series.
    combine: bool = True


# QueryAggregateFunction codes a dynamic line aggregates its series with.
_LINE_AGGREGATES = {1: "average", 3: "min", 4: "max", 6: "median"}


@dataclass(frozen=True)
class _Dynamic:
    kind: str
    series: str | None = None
    percentile: float | None = None
    measure: Json | None = None
    exclusive: bool = False


def _dynamic_kind(node: Any) -> _Dynamic | None:
    """What a line whose value is an expression computes; None for a literal value."""
    inner = node.get("expr") if isinstance(node, dict) else None
    if not isinstance(inner, dict) or "Literal" in inner:
        return None
    aggregation = inner.get("Aggregation")
    if isinstance(aggregation, dict):
        kind = _LINE_AGGREGATES.get(aggregation.get("Function", -1))
        return _Dynamic(kind or "unsupported", _select_ref(aggregation.get("Expression")))
    percentile = inner.get("Percentile")
    if isinstance(percentile, dict):
        k = percentile.get("K")
        series = _select_ref(percentile.get("Expression"))
        exclusive = percentile.get("Exclusive") is True
        if isinstance(k, int | float) and not isinstance(k, bool) and 0 <= k <= 1:
            return _Dynamic("percentile", series, float(k) * 100, exclusive=exclusive)
        return _Dynamic("unsupported", series)
    if isinstance(inner.get("Measure"), dict):
        return _Dynamic("measure", measure=inner)
    return _Dynamic("unsupported")


def _select_ref(node: Any) -> str | None:
    ref = node.get("SelectRef") if isinstance(node, dict) else None
    name = ref.get("ExpressionName") if isinstance(ref, dict) else None
    return name if isinstance(name, str) else None


def _decoded(props: Mapping[str, Any], key: str) -> Any:
    return pbir_expr.decode(props.get(key)) if key in props else None


def _num(props: Mapping[str, Any], key: str) -> float | None:
    found = _decoded(props, key)
    if isinstance(found, bool) or not isinstance(found, int | float) or not math.isfinite(found):
        return None
    return float(found)


def line_specs(
    objects: Mapping[str, Any], palette: Sequence[str]
) -> tuple[list[LineSpec], list[str]]:
    """The chart's analytics lines and the ``object.property`` paths it cannot draw.

    A line whose value is not a literal (a measure-driven constant line) or whose
    percentile is missing is left out and listed.
    """
    specs: list[LineSpec] = []
    approximate: list[str] = []
    for name, (kind, axis) in LINE_OBJECTS.items():
        entries = objects.get(name)
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or not isinstance(entry.get("properties"), dict):
                continue
            props: Json = entry["properties"]
            raw_selector = entry.get("selector")
            selector: Json = raw_selector if isinstance(raw_selector, dict) else {}
            shown = _decoded(props, "show")
            if (shown is False) or (kind == "trend" and shown is not True):
                continue
            constant: Any = None
            percentile = None
            measure: Json | None = None
            exclusive = False
            metadata = selector.get("metadata")
            series = metadata if isinstance(metadata, str) else None
            line_kind = kind
            if kind == "constant":
                dynamic = _dynamic_kind(props.get("value"))
                if dynamic is not None:
                    line_kind, percentile = dynamic.kind, dynamic.percentile
                    measure, exclusive = dynamic.measure, dynamic.exclusive
                    series = dynamic.series or series
                    if line_kind == "unsupported":
                        approximate.append(f"{name}.value")
                        continue
                else:
                    constant = pbir_expr.decode(props.get("value"))
                    if hasattr(constant, "isoformat"):
                        constant = constant.strftime("%Y-%m-%dT%H:%M:%S")
                    if constant is None or isinstance(constant, bool | dict):
                        approximate.append(f"{name}.value")
                        continue
            if kind == "percentile":
                percentile = _num(props, "percentile")
                if percentile is None or not 0 <= percentile <= 100:
                    approximate.append(f"{name}.percentile")
                    continue
            raw_style = str(_decoded(props, "style") or "dashed").lower()
            label_text = str(_decoded(props, "dataLabelText") or "Value").lower()
            decimals = _num(props, "dataLabelDecimalPoints")
            units = _num(props, "dataLabelDisplayUnits")
            style = {
                "name": _text(props, "displayName"),
                "color": color(props.get("lineColor"), list(palette)),
                "alpha": alpha_of(_num(props, "transparency")),
                "style": raw_style if raw_style in _STYLES else "dashed",
                "width": _num(props, "width"),
                "position": "back"
                if str(_decoded(props, "position") or "").lower() == "back"
                else "front",
                "label": {
                    "show": _decoded(props, "dataLabelShow") is True,
                    "text": _LABEL_TEXT.get(label_text, "Value"),
                    "color": color(props.get("dataLabelColor"), list(palette)),
                    "decimals": int(decimals) if decimals is not None and decimals >= 0 else None,
                    "displayUnits": units if units else None,
                    "horizontal": "right"
                    if str(_decoded(props, "dataLabelHorizontalPosition") or "").lower() == "right"
                    else "left",
                    "vertical": "under"
                    if str(_decoded(props, "dataLabelVerticalPosition") or "").lower() == "under"
                    else "above",
                },
            }
            ident = selector.get("id")
            specs.append(
                LineSpec(
                    name,
                    line_kind,
                    axis,
                    ident if isinstance(ident, str) else None,
                    series,
                    constant,
                    percentile,
                    style,
                    measure,
                    exclusive=exclusive,
                    combine=_decoded(props, "combineSeries") is not False,
                )
            )
    return specs, approximate


def _text(props: Mapping[str, Any], key: str) -> str | None:
    found = _decoded(props, key)
    return found[:200] if isinstance(found, str) else None


def _series_indexes(fields: Sequence[Mapping[str, Any]]) -> list[int]:
    return [
        i
        for i, f in enumerate(fields)
        if f.get("kind") in {"measure", "computed"} and not f.get("hidden")
    ]


def _series_index(spec: LineSpec, fields: Sequence[Mapping[str, Any]]) -> int | None:
    shown = _series_indexes(fields)
    if spec.series is not None:
        return next((i for i in shown if fields[i].get("query_ref") == spec.series), None)
    return shown[0] if shown else None


def percentile_inclusive(values: Sequence[float], percent: float) -> float:
    """PERCENTILE.INC: linear interpolation between the closest ranks."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    lower = math.floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def percentile_exclusive(values: Sequence[float], percent: float) -> float | None:
    """PERCENTILE.EXC: the rank is p x (n + 1); outside 1..n there is no value (blank)."""
    ordered = sorted(values)
    position = percent / 100 * (len(ordered) + 1) - 1
    if position < 0 or position > len(ordered) - 1:
        return None
    lower = math.floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def trend_splits(specs: Sequence[LineSpec], fields: Sequence[Mapping[str, Any]]) -> list[str]:
    """The trend lines Studio cannot split: one line per legend series (combine off with a
    Series well) is drawn by Power BI only (``trend.combineSeries`` not drawn)."""
    legend = any(f.get("role") == "Series" and f.get("kind") == "column" for f in fields)
    return [
        f"{spec.object}.combineSeries"
        for spec in specs
        if spec.kind == "trend" and not spec.combine and legend
    ]


def line_values(
    specs: Sequence[LineSpec],
    fields: Sequence[Mapping[str, Any]],
    rows: Sequence[Sequence[Any]],
    visual_values: Sequence[Any] | None = None,
) -> list[Json]:
    """The lines to draw: each spec with its value computed from the rows (in display
    order). ``visual_values`` is the row of values for the whole visual (the totals query)
    a measure-driven constant line reads. A line with no value to show has ``value``
    null."""
    found: list[Json] = []
    for spec in specs:
        if spec.kind == "trend":
            found.extend(_trend_lines(spec, fields, rows))
            continue
        index = _series_index(spec, fields)
        numbers = (
            [
                float(row[index])
                for row in rows
                if index < len(row)
                and isinstance(row[index], int | float)
                and not isinstance(row[index], bool)
                and math.isfinite(row[index])
            ]
            if index is not None
            else []
        )
        line: Json = {
            "id": spec.id,
            "object": spec.object,
            "kind": spec.kind,
            "axis": spec.axis,
            "series": fields[index].get("query_ref") if index is not None else spec.series,
            "value": None,
            **spec.style,
        }
        if spec.kind == "constant":
            line["value"] = spec.constant
        elif spec.kind == "measure":
            line["series"] = spec.series
            if visual_values is not None and spec.value_index is not None:
                line["value"] = _num_cell(visual_values, spec.value_index)
        elif numbers and spec.kind == "min":
            line["value"] = min(numbers)
        elif numbers and spec.kind == "max":
            line["value"] = max(numbers)
        elif numbers and spec.kind == "average":
            line["value"] = sum(numbers) / len(numbers)
        elif numbers and spec.kind == "median":
            line["value"] = percentile_inclusive(numbers, 50)
        elif numbers and spec.kind == "percentile" and spec.percentile is not None:
            line["value"] = (
                percentile_exclusive(numbers, spec.percentile)
                if spec.exclusive
                else percentile_inclusive(numbers, spec.percentile)
            )
        found.append(line)
    return found


def _category_positions(
    fields: Sequence[Mapping[str, Any]], rows: Sequence[Sequence[Any]]
) -> list[float]:
    """Each row's x on the category axis: the position of its category in display order
    (rows of one category, one per legend series, share it)."""
    category = next(
        (
            i
            for i, f in enumerate(fields)
            if f.get("kind") == "column" and f.get("role") not in {"Series", "SortBy", "Rows"}
        ),
        None,
    )
    if category is None:
        return [float(x) for x in range(len(rows))]
    seen: dict[str, int] = {}
    positions: list[float] = []
    for row in rows:
        key = repr(row[category] if category < len(row) else None)
        positions.append(float(seen.setdefault(key, len(seen))))
    return positions


def _trend_lines(
    spec: LineSpec, fields: Sequence[Mapping[str, Any]], rows: Sequence[Sequence[Any]]
) -> list[Json]:
    """Least squares over the points (x = the category's position in display order): the
    line's value at the first and the last category. Combined (Power BI's default), one line
    over every series' points; otherwise one line per measure."""
    positions = _category_positions(fields, rows)
    last = max(positions) if positions else 0.0
    shown = _series_indexes(fields)
    if spec.series is not None:
        shown = [i for i in shown if fields[i].get("query_ref") == spec.series]
    groups = [shown] if spec.combine else [[i] for i in shown]
    found: list[Json] = []
    for group in groups or [[]]:
        points = [
            (positions[r], value)
            for r, row in enumerate(rows)
            for i in group
            if (value := _num_cell(row, i)) is not None
        ]
        series = fields[group[0]].get("query_ref") if len(group) == 1 else None
        found.append(
            {
                "id": spec.id,
                "object": spec.object,
                "kind": "trend",
                "axis": spec.axis,
                "series": series,
                "value": None,
                **spec.style,
                **_least_squares(points, last),
            }
        )
    return found


def _least_squares(points: Sequence[tuple[float, float]], last: float) -> Json:
    if len(points) < 2:
        return {"start": None, "end": None}
    n = len(points)
    mean_x = sum(p[0] for p in points) / n
    mean_y = sum(p[1] for p in points) / n
    spread = sum((p[0] - mean_x) ** 2 for p in points)
    slope = sum((p[0] - mean_x) * (p[1] - mean_y) for p in points) / spread if spread else 0.0
    intercept = mean_y - slope * mean_x
    return {"start": intercept, "end": intercept + slope * last}


# --- visual calculations -----------------------------------------------------------------


@dataclass(frozen=True)
class Calculation:
    """A visual calculation Studio evaluates: ``kind`` over the field at ``source``."""

    kind: str
    source: int
    window: int = 1

    @property
    def range_dependent(self) -> bool:
        """Whether a row's value depends on the other rows (a running sum, a previous
        value, a share of a parent computed from the rows), not only on its own row and the
        engine's grand total."""
        return self.kind not in GRAND_TOTAL_KINDS


# Calculations relative to the grand total, which the engine's totals piece gives (Power
# BI's COLLAPSEALL is the measure for the whole visual, not a sum of the rows).
GRAND_TOTAL_KINDS = frozenset({"percentOfTotal", "versusTotal"})


_REF = r"\[([^\[\]]{1,120})\]"
_AXIS = r"(?:\s*,\s*(?:ROWS|COLUMNS|ROWS\s+COLUMNS|COLUMNS\s+ROWS))?"
_PATTERNS: tuple[tuple[str, str], ...] = (
    ("runningSum", rf"RUNNINGSUM\(\s*{_REF}{_AXIS}\s*\)"),
    ("movingAverage", rf"MOVINGAVERAGE\(\s*{_REF}\s*,\s*(\d{{1,3}}){_AXIS}\s*\)"),
    ("previous", rf"PREVIOUS\(\s*{_REF}{_AXIS}\s*\)"),
    ("next", rf"NEXT\(\s*{_REF}{_AXIS}\s*\)"),
    ("first", rf"FIRST\(\s*{_REF}{_AXIS}\s*\)"),
    ("last", rf"LAST\(\s*{_REF}{_AXIS}\s*\)"),
    ("difference", rf"{_REF}\s*-\s*PREVIOUS\(\s*\[\1\]{_AXIS}\s*\)"),
    ("differenceNext", rf"{_REF}\s*-\s*NEXT\(\s*\[\1\]{_AXIS}\s*\)"),
    ("versusParent", rf"{_REF}\s*-\s*COLLAPSE\(\s*\[\1\]{_AXIS}\s*\)"),
    ("versusTotal", rf"{_REF}\s*-\s*COLLAPSEALL\(\s*\[\1\]{_AXIS}\s*\)"),
    (
        "percentChange",
        rf"DIVIDE\(\s*{_REF}\s*-\s*PREVIOUS\(\s*\[\1\]{_AXIS}\s*\)\s*,"
        rf"\s*PREVIOUS\(\s*\[\1\]{_AXIS}\s*\)\s*\)",
    ),
    ("percentOfParent", rf"DIVIDE\(\s*{_REF}\s*,\s*COLLAPSE\(\s*\[\1\]{_AXIS}\s*\)\s*\)"),
    ("percentOfParent", rf"{_REF}\s*/\s*COLLAPSE\(\s*\[\1\]{_AXIS}\s*\)"),
    ("percentOfTotal", rf"DIVIDE\(\s*{_REF}\s*,\s*COLLAPSEALL\(\s*\[\1\]{_AXIS}\s*\)\s*\)"),
    ("percentOfTotal", rf"{_REF}\s*/\s*COLLAPSEALL\(\s*\[\1\]{_AXIS}\s*\)"),
)


def parse_calculation(
    expression: str, fields: Sequence[Mapping[str, Any]], own: int
) -> Calculation | None:
    """The calculation an expression is, when it is one of the common ones (running sum,
    moving average, previous/next/first/last, versus previous, percent of parent or of the
    grand total) over another field of the visual; None for anything else."""
    text = " ".join(expression.split())
    for kind, pattern in _PATTERNS:
        match = re.fullmatch(pattern, text, re.IGNORECASE)
        if match is None:
            continue
        name = match.group(1).strip().casefold()
        source = next(
            (
                i
                for i, f in enumerate(fields)
                if i != own
                and name in {str(f.get(k, "")).casefold() for k in ("label", "name", "query_ref")}
            ),
            None,
        )
        if source is None:
            return None
        window = int(match.group(2)) if kind == "movingAverage" else 1
        return Calculation(kind, source, max(1, window))
    return None


def _axis_order(
    fields: Sequence[Mapping[str, Any]], rows: Sequence[Sequence[Any]]
) -> tuple[list[int], list[int]]:
    """Row indexes in the visual's axis order (ascending by its category fields, blanks
    first; a field the model sorts by another column follows that column) and the group
    field indexes."""
    groups = [
        i for i, f in enumerate(fields) if f.get("kind") == "column" and f.get("role") != "SortBy"
    ]
    order_by: list[int] = []
    for g in groups:
        helper = fields[g].get("_sortBy")
        order_by.append(helper if isinstance(helper, int) else g)

    def key(row_index: int) -> tuple[Any, ...]:
        row = rows[row_index]
        return tuple(_ascending(row[g] if g < len(row) else None) for g in order_by)

    return sorted(range(len(rows)), key=key), groups


def _ascending(cell: Any) -> tuple[bool, int, Any]:
    """Sort key of a category value: blanks first, then numbers, then text by case."""
    if isinstance(cell, int | float) and not isinstance(cell, bool):
        return (True, 0, cell)
    return (cell is not None, 1, str(cell).casefold())


def _num_cell(row: Sequence[Any], index: int) -> float | None:
    cell = row[index] if index < len(row) else None
    if isinstance(cell, bool) or not isinstance(cell, int | float) or not math.isfinite(cell):
        return None
    return float(cell)


def evaluate_calculation(
    calc: Calculation,
    fields: Sequence[Mapping[str, Any]],
    rows: Sequence[Sequence[Any]],
    grand: Any = None,
) -> list[float | None]:
    """The calculation's value in every row (in the order ``rows`` are given). ``grand`` is
    the source measure for the whole visual (the engine's totals piece), which a percent of
    or a difference from the grand total divides by or subtracts; without it those are
    blank."""
    order, groups = _axis_order(fields, rows)
    values = [_num_cell(row, calc.source) for row in rows]
    result: list[float | None] = [None] * len(rows)
    if calc.kind in GRAND_TOTAL_KINDS:
        if isinstance(grand, bool) or not isinstance(grand, int | float):
            return result
        whole = float(grand)
        for i, share in enumerate(values):
            if calc.kind == "versusTotal":
                result[i] = (share or 0.0) - whole
            elif share is not None and whole != 0:
                result[i] = share / whole
        return result
    if calc.kind in {"percentOfParent", "versusParent"}:
        # The parent is the rows sharing every level but the last, summed from the visible
        # rows: exact for a measure that adds up, which is why a visual with more than one
        # level is approximate (report_formatting notes it).
        parent_levels = groups[:-1]
        totals: dict[tuple[Any, ...], float] = {}
        for i, row in enumerate(rows):
            key = tuple(row[g] for g in parent_levels)
            totals[key] = totals.get(key, 0.0) + (values[i] or 0.0)
        for i, row in enumerate(rows):
            total = totals[tuple(row[g] for g in parent_levels)]
            share = values[i]
            if calc.kind.startswith("versus"):
                result[i] = (share or 0.0) - total
            else:
                result[i] = None if share is None or total == 0 else share / total
        return result
    running: float | None = None
    ordered_values = [values[i] for i in order]
    for position, row_index in enumerate(order):
        current = ordered_values[position]
        previous = ordered_values[position - 1] if position > 0 else None
        following = ordered_values[position + 1] if position + 1 < len(ordered_values) else None
        if calc.kind == "differenceNext" and (current is not None or following is not None):
            result[row_index] = (current or 0.0) - (following or 0.0)
        elif calc.kind == "runningSum":
            if current is not None:
                running = (running or 0.0) + current
            result[row_index] = running
        elif calc.kind == "movingAverage":
            window = [
                v
                for v in ordered_values[max(0, position - calc.window + 1) : position + 1]
                if v is not None
            ]
            result[row_index] = sum(window) / len(window) if window else None
        elif calc.kind == "previous":
            result[row_index] = previous
        elif calc.kind == "next":
            result[row_index] = following
        elif calc.kind == "first":
            result[row_index] = ordered_values[0] if ordered_values else None
        elif calc.kind == "last":
            result[row_index] = ordered_values[-1] if ordered_values else None
        elif calc.kind == "difference" and (current is not None or previous is not None):
            # A blank is 0 in the subtraction, as in DAX; blank minus blank stays blank.
            result[row_index] = (current or 0.0) - (previous or 0.0)
        elif (
            calc.kind == "percentChange"
            and current is not None
            and previous is not None
            and previous != 0
        ):
            result[row_index] = (current - previous) / previous
    return result


# --- small multiples ---------------------------------------------------------------------


def panels(rows: Sequence[Sequence[Any]], index: int, order_by: int | None = None) -> Json:
    """The small multiples of a chart: the panel values in ascending order (of the column
    at ``order_by`` when the model sorts the field by another one) and, for each, the
    indexes of its rows (in ``rows`` order)."""
    by_value: dict[str, tuple[Any, Any, list[int]]] = {}
    for i, row in enumerate(rows):
        cell = row[index] if index < len(row) else None
        rank = row[order_by] if order_by is not None and order_by < len(row) else cell
        by_value.setdefault(repr(cell), (cell, rank, []))[2].append(i)

    ordered = sorted(by_value.values(), key=lambda item: _ascending(item[1]))
    return {
        "values": [cell for cell, _, _ in ordered],
        "panels": [{"value": cell, "rows": found} for cell, _, found in ordered],
    }


def grid_layout(props: Mapping[str, Any]) -> Json:
    """The small-multiples grid: rows and columns per screen (null: Power BI's automatic)."""
    rows = _num(props, "rowCount")
    columns = _num(props, "columnCount")
    kind = enum_text(props.get("layoutType"), None)
    return {
        "type": "custom" if (kind or "").lower() == "custom" else "auto",
        "rows": int(rows) if rows is not None and rows >= 1 else None,
        "columns": int(columns) if columns is not None and columns >= 1 else None,
    }
