"""Where fields go in a visual, what survives a type change, and where a new visual sits.

``report_edits`` first placed fields by walking ``pbir_catalog.json`` roles in catalog order.
That snapshot was written with ``sort_keys=True``, so the roles are alphabetical, and the
placement came out wrong in ways Desktop shows immediately: a kpi put its measure in
``Goal`` (value and target swapped), a gauge put it in ``MaxValue`` and failed with "needs
Value", scatter ``X``/``Y`` (GroupingOrMeasure) were never filled, and a category dropped on
a card went into ``Rows``, which on the new card is small multiples. This module replaces
that with a hand-written table (``ROLE_TABLE``) that is checked against the catalog by tests,
and adds the pieces around it:

- ``place_fields`` / ``validate_query_state`` / ``role_by_name``: the fill order and limits;
- ``field_identity`` and friends: fields compared by what they *are* (table, column,
  aggregation, hierarchy level), not by a caption that a rename or a blank
  ``nativeQueryRef`` (' ' on the table's ``vs Previous Month``) makes unreliable;
- ``plan_type_change``: a new query state plus every formatting setting the new type has;
- ``automatic_title``, ``default_size`` and ``free_position``: what Desktop shows for a
  visual with no title literal, and a spot that does not cover another visual.

It is pure: no I/O, no document objects. ``report_edits`` calls it.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from . import pbir_expr as expr


class PlacementError(ValueError):
    """The fields cannot be put in this visual as asked (plain English, safe to show)."""


class NeedsAggregation(PlacementError):
    """A column has no default summary, so the user must say how to summarize it."""

    def __init__(self, message: str, options: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.options = tuple(options)


@dataclass(frozen=True)
class FieldSlot:
    kind: Literal["measure", "column"]
    projection: dict[str, Any]


@dataclass(frozen=True)
class TypeRoles:
    visual_type: str
    measure_roles: tuple[str, ...]  # fill order for measures
    grouping_roles: tuple[str, ...]  # fill order for columns
    # GroupingOrMeasure roles: they take what the two lists above could not place, in the
    # order the fields were given. A role may be in both lists (scatter X and Y take
    # measures first).
    either_roles: tuple[str, ...] = ()
    small_multiples: str | None = None  # filled only from the explicit small_multiples argument
    table_like: bool = False  # every field goes to one role, in the order given
    # How many fields a role takes when *placing* (not when validating): a second column on
    # a column chart is a legend, not a second axis level, so Category is capped at 1.
    fill_caps: Mapping[str, int] = field(default_factory=dict[str, int])
    # Limits Desktop enforces that the catalog's maxPerRole does not list.
    max_per_role: Mapping[str, int] = field(default_factory=dict[str, int])
    # None means the catalog's requiredRoles; required_any needs at least one of its roles.
    required_roles: tuple[str, ...] | None = None
    required_any: tuple[str, ...] = ()


_MANY = 10**6

# --- the role table -----------------------------------------------------------------


def _cartesian(name: str, *, category_cap: bool = True) -> TypeRoles:
    # Column, bar, area and ribbon charts share the roles Category (axis), Series (legend),
    # Rows (small multiples) and Y (values). Bars swap the axes on screen, not the role ids.
    return TypeRoles(
        name,
        measure_roles=("Y",),
        grouping_roles=("Category", "Series"),
        small_multiples="Rows",
        fill_caps={"Category": 1} if category_cap else {},
        max_per_role={"Rows": 1},
    )


def _combo(name: str) -> TypeRoles:
    # Y is the columns' axis, Y2 the line's. The first measure is the column, the rest are
    # lines: "Sales and Margin %" as a combo means columns for sales and a line for margin.
    return TypeRoles(
        name,
        measure_roles=("Y", "Y2"),
        grouping_roles=("Category", "Series"),
        small_multiples="Rows",
        fill_caps={"Category": 1, "Y": 1},
        max_per_role={"Rows": 1},
        required_roles=("Category",),
        required_any=("Y", "Y2"),
    )


ROLE_TABLE: dict[str, TypeRoles] = {
    # Legacy card: one field. A column is refused rather than put anywhere.
    "card": TypeRoles("card", measure_roles=("Values",), grouping_roles=()),
    # Multi-row card and tables take any field, in the order given.
    "multiRowCard": TypeRoles("multiRowCard", (), (), either_roles=("Values",), table_like=True),
    "columnChart": _cartesian("columnChart"),
    "clusteredColumnChart": _cartesian("clusteredColumnChart"),
    "hundredPercentStackedColumnChart": _cartesian("hundredPercentStackedColumnChart"),
    "barChart": _cartesian("barChart"),
    "clusteredBarChart": _cartesian("clusteredBarChart"),
    "hundredPercentStackedBarChart": _cartesian("hundredPercentStackedBarChart"),
    # Line and area charts also have Y2 (secondary axis). It is never filled by default:
    # a second measure on a separate scale is a decision, not a placement.
    "lineChart": _cartesian("lineChart"),
    "areaChart": _cartesian("areaChart"),
    "stackedAreaChart": _cartesian("stackedAreaChart"),
    "hundredPercentStackedAreaChart": _cartesian("hundredPercentStackedAreaChart"),
    "ribbonChart": _cartesian("ribbonChart"),
    # Pie and donut: Category is the Legend (slices), Series is Details (a second split).
    "pieChart": TypeRoles("pieChart", ("Y",), ("Category", "Series")),
    "donutChart": TypeRoles("donutChart", ("Y",), ("Category", "Series")),
    "tableEx": TypeRoles("tableEx", (), (), either_roles=("Values",), table_like=True),
    # A matrix: columns go to Rows (a second one nests, as Desktop does when it is dropped
    # on Rows), measures to Values. A matrix with only Rows is valid in Desktop although
    # the catalog lists Values as required.
    "pivotTable": TypeRoles(
        "pivotTable",
        measure_roles=("Values",),
        grouping_roles=("Rows", "Columns"),
        required_roles=(),
        required_any=("Rows", "Columns", "Values"),
    ),
    "slicer": TypeRoles("slicer", (), ("Values",)),
    "treemap": TypeRoles("treemap", ("Values",), ("Group", "Details")),
    "funnel": TypeRoles("funnel", ("Y",), ("Category",)),
    # Gauge: the second measure is the target, then maximum and minimum. Desktop's own well
    # order is Value, Minimum, Maximum, Target, but a bare "gauge of Sales vs Goal" means
    # value and target.
    "gauge": TypeRoles("gauge", ("Y", "TargetValue", "MaxValue", "MinValue"), ()),
    "lineClusteredColumnComboChart": _combo("lineClusteredColumnComboChart"),
    "lineStackedColumnComboChart": _combo("lineStackedColumnComboChart"),
    # Scatter: X and Y are GroupingOrMeasure but almost always aggregated measures, so
    # measures fill X, Y, then Size. Columns become the point Category ("Values" in the
    # UI) and the legend. Columns left over may take a free X or Y.
    "scatterChart": TypeRoles(
        "scatterChart",
        measure_roles=("X", "Y", "Size"),
        grouping_roles=("Category", "Series"),
        either_roles=("X", "Y"),
        max_per_role={"X": 1},  # the catalog lists Y but not X
    ),
    "waterfallChart": TypeRoles("waterfallChart", ("Y",), ("Category", "Breakdown")),
    # The catalog lists kpi roles as Goal, Indicator, TrendLine (alphabetical). The value
    # is Indicator and the target is Goal.
    "kpi": TypeRoles("kpi", ("Indicator", "Goal"), ("TrendLine",)),
    # New card: measures in Data. Rows is small multiples (one card per value), so a
    # category is only ever placed there on request.
    "cardVisual": TypeRoles("cardVisual", ("Data",), (), small_multiples="Rows"),
    # Not addable, but existing visuals of these types are validated and converted.
    "advancedSlicerVisual": TypeRoles("advancedSlicerVisual", (), ("Values",)),
    # Fieldless visuals: content comes from their own objects, not the field wells.
    "textbox": TypeRoles("textbox", (), ()),
    "shape": TypeRoles("shape", (), ()),
    "image": TypeRoles("image", (), ()),
}

# Types Studio can create (the AddVisual/ChangeVisualType gate). Maps, R/Python, Q&A and
# custom visuals are left to Desktop. textbox, shape and image are in ROLE_TABLE but not
# here: adding one needs content (paragraphs, geometry, a registered image) that field
# placement does not provide.
ADDABLE = frozenset(
    {
        "card",
        "multiRowCard",
        "columnChart",
        "clusteredColumnChart",
        "hundredPercentStackedColumnChart",
        "barChart",
        "clusteredBarChart",
        "hundredPercentStackedBarChart",
        "lineChart",
        "areaChart",
        "stackedAreaChart",
        "pieChart",
        "donutChart",
        "tableEx",
        "pivotTable",
        "slicer",
        "treemap",
        "funnel",
        "gauge",
        "lineClusteredColumnComboChart",
        "lineStackedColumnComboChart",
        "scatterChart",
        "waterfallChart",
        "ribbonChart",
        "kpi",
        "cardVisual",
    }
)

FIELDLESS = frozenset({"textbox", "shape", "image"})

_TYPE_NAMES = {
    "card": "card",
    "cardVisual": "card",
    "multiRowCard": "multi-row card",
    "columnChart": "stacked column chart",
    "clusteredColumnChart": "clustered column chart",
    "hundredPercentStackedColumnChart": "100% stacked column chart",
    "barChart": "stacked bar chart",
    "clusteredBarChart": "clustered bar chart",
    "hundredPercentStackedBarChart": "100% stacked bar chart",
    "lineChart": "line chart",
    "areaChart": "area chart",
    "stackedAreaChart": "stacked area chart",
    "hundredPercentStackedAreaChart": "100% stacked area chart",
    "pieChart": "pie chart",
    "donutChart": "donut chart",
    "tableEx": "table",
    "pivotTable": "matrix",
    "slicer": "slicer",
    "advancedSlicerVisual": "button slicer",
    "treemap": "treemap",
    "funnel": "funnel chart",
    "gauge": "gauge",
    "lineClusteredColumnComboChart": "line and clustered column chart",
    "lineStackedColumnComboChart": "line and stacked column chart",
    "scatterChart": "scatter chart",
    "waterfallChart": "waterfall chart",
    "ribbonChart": "ribbon chart",
    "kpi": "KPI",
    "textbox": "text box",
    "shape": "shape",
    "image": "image",
}


def visual_type_name(visual_type: str) -> str:
    """The name a Power BI user knows the type by ("clustered column chart")."""
    return _TYPE_NAMES.get(visual_type, visual_type)


def _a(visual_type: str) -> str:
    name = visual_type_name(visual_type)
    return f"{'an' if name[:1] in 'aeiou' else 'a'} {name}"


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


# --- catalog access -----------------------------------------------------------------


def _catalog_roles(visual_type: str) -> dict[str, dict[str, Any]]:
    try:
        entry = expr.roles(visual_type)
    except expr.ExpressionError:
        return {}
    roles: dict[str, dict[str, Any]] = entry.get("roles", {})
    return roles


def _catalog_max(visual_type: str) -> dict[str, int]:
    try:
        entry = expr.roles(visual_type)
    except expr.ExpressionError:
        return {}
    return {role: int(limit) for role, limit in entry.get("maxPerRole", {}).items()}


def _catalog_required(visual_type: str) -> tuple[str, ...]:
    try:
        entry = expr.roles(visual_type)
    except expr.ExpressionError:
        return ()
    return tuple(entry.get("requiredRoles", []))


def _limit(roles: TypeRoles, role: str, *, filling: bool) -> int:
    limits = [
        _catalog_max(roles.visual_type).get(role, _MANY),
        roles.max_per_role.get(role, _MANY),
    ]
    if filling:
        limits.append(roles.fill_caps.get(role, _MANY))
    return min(limits)


def _required(roles: TypeRoles) -> tuple[str, ...]:
    if roles.required_roles is not None:
        return roles.required_roles
    return _catalog_required(roles.visual_type)


# Names the field wells go by that differ from the catalog's display names.
_ALIASES = {
    "smallmultiple": "@small",
    "smallmultiples": "@small",
    "axis": "Category",
    "legend": "Series",
    "value": "@value",
    "values": "@value",
}


def _normal(text: str) -> str:
    return re.sub(r"[\s_-]+", "", text).casefold()


def role_display_name(visual_type: str, role: str) -> str:
    """The name of a field well as Power BI shows it ("Legend", "X-axis")."""
    roles = ROLE_TABLE.get(visual_type)
    if roles is not None and roles.small_multiples == role:
        return "Small multiples"
    name = _catalog_roles(visual_type).get(role, {}).get("displayName")
    return str(name) if name else role


def role_by_name(visual_type: str, name: str) -> str | None:
    """A role id from its id or display name, ignoring case, spaces and hyphens."""
    catalog = _catalog_roles(visual_type)
    wanted = _normal(name)
    if not wanted:
        return None
    for role in catalog:
        if _normal(role) == wanted:
            return role
    for role in catalog:
        if _normal(role_display_name(visual_type, role)) == wanted:
            return role
    for role, spec in catalog.items():
        if _normal(str(spec.get("displayName", ""))) == wanted:
            return role
    alias = _ALIASES.get(wanted)
    table = ROLE_TABLE.get(visual_type)
    if alias == "@small":
        return table.small_multiples if table else None
    if alias == "@value":
        if table and table.measure_roles:
            return table.measure_roles[0]
        return None
    if alias in catalog:
        return alias
    return None


# --- field identity -----------------------------------------------------------------

# Aggregation function codes as report_edits writes them. Desktop's own menu calls 2
# "Count (Distinct)" and 5 "Count".
AGGREGATION_CODES = {
    "Sum": 0,
    "Average": 1,
    "Count": 2,
    "Min": 3,
    "Max": 4,
    "CountNonNull": 5,
    "Median": 6,
}
_AGGREGATION_NAMES = {code: name for name, code in AGGREGATION_CODES.items()}
_AGGREGATION_PREFIX = {
    "Sum": "Sum of",
    "Average": "Average of",
    "Count": "Count of",
    "Min": "Min of",
    "Max": "Max of",
    "CountNonNull": "Count of",
    "Median": "Median of",
}


def _field_of(node: Any) -> dict[str, Any]:
    if isinstance(node, dict):
        inner = node.get("field")
        return inner if isinstance(inner, dict) else node
    return {}


def _entity(expression: Any, aliases: Mapping[str, str] | None) -> str | None:
    """The table a SourceRef names. Filters and some sorts use a ``Source`` alias declared
    in a From clause; ``aliases`` resolves it, otherwise the alias is kept as ``source:x``."""
    ref = expression.get("SourceRef") if isinstance(expression, dict) else None
    if not isinstance(ref, dict):
        return None
    entity = ref.get("Entity")
    if isinstance(entity, str):
        return entity
    source = ref.get("Source")
    if isinstance(source, str):
        return (aliases or {}).get(source, f"source:{source}")
    return None


def _column_identity(node: Any, aliases: Mapping[str, str] | None) -> tuple[str, str] | None:
    if not isinstance(node, dict):
        return None
    entity = _entity(node.get("Expression"), aliases)
    prop = node.get("Property")
    return (entity, prop) if entity is not None and isinstance(prop, str) else None


def _canonical(node: Any) -> str:
    return json.dumps(node, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def field_identity(
    projection: dict[str, Any], aliases: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """What a projection (or a sort item's field) refers to, independent of its captions.

    ("Column", table, column), ("Measure", table, measure), ("Aggregation", function, table,
    column), ("HierarchyLevel", table, hierarchy, level) or ("Unknown", canonical json).
    A date hierarchy over a column ("Date Hierarchy" on Date) is written as ``Date.Date
    Hierarchy``, since one table can have several date columns.
    """
    node = _field_of(projection)
    column = _column_identity(node.get("Column"), aliases)
    if column:
        return ("Column", *column)
    measure = _column_identity(node.get("Measure"), aliases)
    if measure:
        return ("Measure", *measure)
    aggregation = node.get("Aggregation")
    if isinstance(aggregation, dict):
        inner = aggregation.get("Expression")
        inner_column = (
            _column_identity(inner.get("Column"), aliases) if isinstance(inner, dict) else None
        )
        function = aggregation.get("Function")
        if inner_column and isinstance(function, int) and not isinstance(function, bool):
            return (
                "Aggregation",
                _AGGREGATION_NAMES.get(function, f"Function{function}"),
                *inner_column,
            )
    level = node.get("HierarchyLevel")
    if isinstance(level, dict) and isinstance(level.get("Level"), str):
        hierarchy = (level.get("Expression") or {}).get("Hierarchy")
        if isinstance(hierarchy, dict) and isinstance(hierarchy.get("Hierarchy"), str):
            base = hierarchy.get("Expression")
            name = hierarchy["Hierarchy"]
            entity = _entity(base, aliases)
            if entity is None and isinstance(base, dict):
                variation = base.get("PropertyVariationSource")
                if isinstance(variation, dict):
                    entity = _entity(variation.get("Expression"), aliases)
                    if entity is not None and isinstance(variation.get("Property"), str):
                        name = f"{variation['Property']}.{name}"
            if entity is not None:
                return ("HierarchyLevel", entity, name, level["Level"])
    return ("Unknown", _canonical(node))


def sort_item_identity(
    sort_item: dict[str, Any], aliases: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """The field a sortDefinition item sorts by (its Property can differ from the
    projection's queryRef after a measure was renamed)."""
    return field_identity(sort_item.get("field", sort_item), aliases)


def _contains_key(node: Any, key: str) -> bool:
    if isinstance(node, dict):
        return key in node or any(_contains_key(value, key) for value in node.values())
    if isinstance(node, list):
        return any(_contains_key(value, key) for value in node)
    return False


def is_editable_projection(
    projection: dict[str, Any], *, field_parameter_tables: Collection[str] = ()
) -> bool:
    """False for fields Studio passes through untouched: automatic date hierarchies
    (PropertyVariationSource), visual calculations, and columns of a field-parameter table
    (the caller knows those from the model; ``field_parameter_tables`` names them)."""
    node = _field_of(projection)
    if not node:
        return False
    if _contains_key(node, "PropertyVariationSource") or _contains_key(
        node, "NativeVisualCalculation"
    ):
        return False
    identity = field_identity(node)
    if identity[0] == "Unknown":
        return False
    return not (identity[0] in {"Column", "Measure"} and identity[1] in field_parameter_tables)


def projection_label(projection: dict[str, Any]) -> str:
    """The caption Desktop shows for a field in this visual: its display name, else the
    native query ref (Desktop rewrites that on a rename, unlike queryRef), else the name."""
    for key in ("displayName", "nativeQueryRef"):
        value = projection.get(key)
        if isinstance(value, str) and value.strip():
            return value
    identity = field_identity(projection)
    if identity[0] == "Aggregation":
        return f"{_AGGREGATION_PREFIX.get(identity[1], identity[1] + ' of')} {identity[3]}"
    if identity[0] in {"Column", "Measure"}:
        return identity[2]
    if identity[0] == "HierarchyLevel":
        return identity[3]
    query_ref = projection.get("queryRef")
    return query_ref if isinstance(query_ref, str) else "a field"


def _names(projection: dict[str, Any]) -> set[str]:
    found = set()
    for key in ("displayName", "nativeQueryRef", "queryRef"):
        value = projection.get(key)
        if isinstance(value, str) and value.strip():
            found.add(value.strip().casefold())
    identity = field_identity(projection)
    if identity[0] in {"Column", "Measure"}:
        found.add(identity[2].strip().casefold())
        found.add(f"{identity[1]}.{identity[2]}".casefold())
    elif identity[0] == "Aggregation":
        found.add(identity[3].strip().casefold())
        found.add(f"{identity[2]}.{identity[3]}".casefold())
    return {name for name in found if name}


_IDENTITY_KINDS = {"Column", "Measure", "Aggregation", "HierarchyLevel", "Unknown"}
_FIELD_KEYS = {"Column", "Measure", "Aggregation", "HierarchyLevel"}


def match_projection(projection: dict[str, Any], ref: Any) -> bool:
    """Whether ``ref`` names this projection.

    ``ref`` is a field identity tuple, another projection or field expression, an object
    with ``name`` (and optionally ``table`` and ``aggregation``, like report_edits'
    FieldRef), or a caption. Identity comes first; captions are the fallback and include
    the field's own name, so a measure whose nativeQueryRef is a blank ' ' is still found.
    """
    if isinstance(ref, tuple):
        return bool(ref) and ref[0] in _IDENTITY_KINDS and field_identity(projection) == ref
    if isinstance(ref, dict):
        if isinstance(ref.get("field"), dict) or _FIELD_KEYS & set(ref):
            return field_identity(projection) == field_identity(ref)
        return False
    if isinstance(ref, str):
        wanted = ref.strip().casefold()
        return bool(wanted) and wanted in _names(projection)
    name = getattr(ref, "name", None)
    if not isinstance(name, str) or not name.strip():
        return False
    table = getattr(ref, "table", None)
    aggregation = getattr(ref, "aggregation", None)
    identity = field_identity(projection)
    if aggregation and (
        identity[0] != "Aggregation" or identity[1].casefold() != str(aggregation).casefold()
    ):
        return False
    if identity[0] in {"Column", "Measure", "Aggregation"}:
        if table and identity[-2].casefold() != str(table).casefold():
            return False
    elif table:
        return False
    return name.strip().casefold() in _names(projection)


_SUMMARIZE_BY = {
    # TOM's AggregateFunction as the model stores it (TMSL strings) ...
    "default": "Sum",
    "sum": "Sum",
    "min": "Min",
    "max": "Max",
    "count": "CountNonNull",
    "average": "Average",
    "distinctcount": "Count",
    # ... and its numeric values in TMSCHEMA_COLUMNS: 1 default, 2 none, 3 sum, 4 min,
    # 5 max, 6 count, 7 average, 8 distinct count (unverified on a live model).
    "1": "Sum",
    "3": "Sum",
    "4": "Min",
    "5": "Max",
    "6": "CountNonNull",
    "7": "Average",
    "8": "Count",
}
_SUMMARIZE_OPTIONS = ("Sum", "Average", "Count", "Min", "Max")


def default_aggregation(summarize_by: str | int | None) -> str:
    """The function a column gets when it is dropped on a value role, from its SummarizeBy.

    A column whose SummarizeBy is None (dates, keys, most FactSales columns) has no
    default: writing Sum silently would be a guess, so this raises NeedsAggregation and chat
    asks. An unknown SummarizeBy (the importer did not extract it) asks as well.
    """
    key = str(summarize_by).strip().casefold() if summarize_by is not None else ""
    function = _SUMMARIZE_BY.get(key)
    if function is None:
        raise NeedsAggregation(
            "This column has no default summary. Choose how to summarize it: "
            + ", ".join(_SUMMARIZE_OPTIONS)
            + ".",
            _SUMMARIZE_OPTIONS,
        )
    return function


# --- dropping references to removed fields -----------------------------------------


def _scope_columns(selector: dict[str, Any]) -> list[tuple[str, ...]]:
    """Columns a data selector scopes to (a per-category color names its column)."""
    found: list[tuple[str, ...]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            column = _column_identity(node.get("Column"), None)
            if column:
                found.append(("Column", *column))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(selector.get("data"))
    return found


def _entry_size(entry: dict[str, Any]) -> int:
    properties = entry.get("properties")
    return len(properties) if isinstance(properties, dict) and properties else 1


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def drop_field_references(visual: dict[str, Any], removed: Sequence[dict[str, Any]]) -> list[str]:
    """Remove the sorts and formatting entries that belonged to removed projections.

    Sorts are matched by field identity: after a measure rename the sort's Property
    (``Employees with Positive Change %``) differs from the projection's queryRef
    (``# Measures.Number of Employees with Positive Change %``), so comparing
    ``Entity.Property`` with the queryRef left the sort behind. Formatting entries are keyed
    by ``selector.metadata == queryRef``, or by the column of a per-category color. A field
    that is still in another role of the visual keeps its references.

    ``visual`` is a visual.json document (or its inner ``visual`` object). Returns plain
    English notes of what was dropped.
    """
    inner = visual.get("visual")
    body: dict[str, Any] = inner if isinstance(inner, dict) else visual
    state: dict[str, Any] = body.get("query", {}).get("queryState", {})
    pool: list[dict[str, Any]] = [
        projection
        for bucket in state.values()
        for projection in bucket.get("projections", [])
        if isinstance(projection, dict)
    ]
    # `removed` may or may not have left the query state yet: take one live projection out
    # of the pool per removed one, so the field stays "live" only if it is there twice.
    for gone in removed:
        index = next((i for i, p in enumerate(pool) if p is gone), None)
        if index is None:
            index = next((i for i, p in enumerate(pool) if p == gone), None)
        if index is not None:
            pool.pop(index)
    live_identities = {field_identity(p) for p in pool}
    live_refs = {p.get("queryRef") for p in pool}
    labels: dict[tuple[str, ...], str] = {}
    ref_labels: dict[str, str] = {}
    for gone in removed:
        identity = field_identity(gone)
        if identity in live_identities:
            continue
        labels[identity] = projection_label(gone)
        query_ref = gone.get("queryRef")
        if isinstance(query_ref, str) and query_ref not in live_refs:
            ref_labels[query_ref] = projection_label(gone)
    notes: list[str] = []
    query = body.get("query", {})
    sort = query.get("sortDefinition")
    if isinstance(sort, dict) and labels:
        items = sort.get("sort", [])
        kept = []
        for item in items:
            identity = sort_item_identity(item)
            if identity in labels:
                notes.append(f"Removed the sort by {labels[identity]}.")
            else:
                kept.append(item)
        if len(kept) != len(items):
            if kept:
                sort["sort"] = kept
            else:
                # A sortDefinition with no sort items is not a default sort, it is invalid.
                query.pop("sortDefinition", None)
    if not labels and not ref_labels:
        return notes
    dropped: dict[str, int] = {}
    for where in ("objects", "visualContainerObjects"):
        objects = body.get(where)
        if not isinstance(objects, dict):
            continue
        for name in list(objects):
            kept_entries = []
            for entry in objects[name]:
                selector = entry.get("selector")
                label = None
                if isinstance(selector, dict):
                    metadata = selector.get("metadata")
                    if isinstance(metadata, str) and metadata in ref_labels:
                        label = ref_labels[metadata]
                    else:
                        for column in _scope_columns(selector):
                            if column in labels:
                                label = labels[column]
                                break
                if label is None:
                    kept_entries.append(entry)
                else:
                    dropped[label] = dropped.get(label, 0) + _entry_size(entry)
            if kept_entries:
                objects[name] = kept_entries
            else:
                objects.pop(name)
    for label, count in dropped.items():
        notes.append(f"Removed {_plural(count, 'formatting setting')} for {label}.")
    return notes


# --- placing fields -----------------------------------------------------------------


def _roles_for(visual_type: str) -> TypeRoles:
    """The role table entry, or one read from the catalog for a type the table does not
    list (only used to read an existing visual, never to place fields)."""
    known = ROLE_TABLE.get(visual_type)
    if known is not None:
        return known
    catalog = _catalog_roles(visual_type)
    if not catalog and visual_type not in expr.visual_types():
        raise PlacementError(f"Studio does not know the visual type {visual_type!r}.")
    small = next((r for r, s in catalog.items() if s.get("displayName") == "Small multiples"), None)
    return TypeRoles(
        visual_type,
        measure_roles=tuple(
            r for r, s in catalog.items() if s.get("kind") == "Measure" and r != "Tooltips"
        ),
        grouping_roles=tuple(
            r for r, s in catalog.items() if s.get("kind") == "Grouping" and r != small
        ),
        either_roles=tuple(r for r, s in catalog.items() if s.get("kind") == "GroupingOrMeasure"),
        small_multiples=small,
    )


def _projection_kind(projection: dict[str, Any], fallback: str = "column") -> str:
    node = _field_of(projection)
    if _FIELD_KEYS & set(node):
        return "measure" if {"Measure", "Aggregation"} & set(node) else "column"
    if "NativeVisualCalculation" in node:
        return "measure"
    return fallback


def _order(roles: TypeRoles, kind: str) -> tuple[str, ...]:
    primary = roles.measure_roles if kind == "measure" else roles.grouping_roles
    return tuple(dict.fromkeys(primary))


@dataclass(frozen=True)
class _Refusal:
    slot: FieldSlot
    code: Literal["room", "kind", "no-small-multiples", "small-multiples-kind"]

    def clause(self, subject: str) -> str:
        """What is wrong, without the article: "has no room for Sales"."""
        if self.code == "room":
            return f"has no room for {subject}"
        if self.code == "no-small-multiples":
            return "cannot be split into small multiples"
        if self.code == "small-multiples-kind":
            return f"needs a column for small multiples, and {subject} is a measure"
        return "holds measures" if self.slot.kind == "column" else "holds columns"

    def message(self, roles: TypeRoles) -> str:
        label = projection_label(self.slot.projection)
        head = f"{_capital(_a(roles.visual_type))} {self.clause(label)}"
        if self.code != "kind":
            return f"{head}."
        if self.slot.kind == "column" and roles.small_multiples:
            return (
                f"{head}, so {label} cannot go in it as a category. Use small multiples "
                "to get one card per value."
            )
        return f"{head}, so {label} cannot go in it."


def _place(
    roles: TypeRoles, fields: Sequence[FieldSlot], small: Sequence[FieldSlot]
) -> tuple[dict[str, list[dict[str, Any]]], list[_Refusal]]:
    placed: dict[str, list[dict[str, Any]]] = {}
    left: list[FieldSlot] = []
    refused: list[_Refusal] = []

    def put(role: str, slot: FieldSlot, *, filling: bool) -> bool:
        if len(placed.get(role, [])) >= _limit(roles, role, filling=filling):
            return False
        placed.setdefault(role, []).append(copy.deepcopy(slot.projection))
        return True

    for slot in fields:
        candidates = _order(roles, slot.kind)
        if not candidates and not roles.either_roles:
            refused.append(_Refusal(slot, "kind"))
        elif not any(put(role, slot, filling=True) for role in candidates):
            left.append(slot)
    # Roles that take either kind get what the kind-specific roles could not hold.
    for slot in left:
        if not any(put(role, slot, filling=True) for role in roles.either_roles):
            refused.append(_Refusal(slot, "room"))
    for slot in small:
        if roles.small_multiples is None:
            refused.append(_Refusal(slot, "no-small-multiples"))
        elif slot.kind != "column":
            refused.append(_Refusal(slot, "small-multiples-kind"))
        elif not put(roles.small_multiples, slot, filling=False):
            refused.append(_Refusal(slot, "room"))
    return placed, refused


def _join(names: Sequence[str], word: str = "and") -> str:
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} {word} {names[-1]}"


def _missing_required(roles: TypeRoles, placed: Mapping[str, Any]) -> str | None:
    """A plain-English complaint when a required role is empty, else None."""
    kind = roles.visual_type
    missing = [r for r in _required(roles) if not placed.get(r)]
    if missing:
        names = [role_display_name(kind, r) for r in missing]
        return f"{_capital(_a(kind))} needs a field for {_join(names)}."
    if roles.required_any and not any(placed.get(r) for r in roles.required_any):
        names = [role_display_name(kind, r) for r in roles.required_any]
        return f"{_capital(_a(kind))} needs a field for {_join(names, 'or')}."
    return None


def place_fields(
    visual_type: str,
    fields: Sequence[FieldSlot],
    *,
    small_multiples: Sequence[FieldSlot] = (),
) -> dict[str, list[dict[str, Any]]]:
    """Put fields into a new visual's roles: measures in ``measure_roles`` order, columns
    in ``grouping_roles`` order, the rest wherever an ``either_roles`` role is free.
    Small multiples only from ``small_multiples``. Raises PlacementError naming the field
    or the missing role in plain English."""
    roles = ROLE_TABLE.get(visual_type)
    if roles is None:
        raise PlacementError(f"Studio cannot add fields to {_a(visual_type)} yet.")
    if visual_type in FIELDLESS and (fields or small_multiples):
        raise PlacementError(f"{_capital(_a(visual_type))} holds content, not fields.")
    placed, refused = _place(roles, fields, small_multiples)
    if refused:
        raise PlacementError(refused[0].message(roles))
    complaint = _missing_required(roles, placed)
    if complaint:
        raise PlacementError(complaint)
    return placed


def validate_query_state(visual_type: str, query_state: dict[str, Any]) -> list[str]:
    """Problems with a visual's field wells, in plain English (empty when it is fine).

    Checks that the roles exist, required roles are filled, no role holds more than it
    allows and a field's kind fits its role (a measure cannot be an axis, a bare column
    cannot be a value). A visual type the catalog does not know (a custom visual) is not
    checked.
    """
    catalog = _catalog_roles(visual_type)
    if not catalog and visual_type not in expr.visual_types():
        return []
    roles = _roles_for(visual_type)
    name = _capital(_a(visual_type))
    problems: list[str] = []
    for role, bucket in query_state.items():
        projections = [p for p in (bucket or {}).get("projections", []) if isinstance(p, dict)]
        spec = catalog.get(role)
        if spec is None:
            if projections:
                problems.append(f"{name} has no {role} field well.")
            continue
        display = role_display_name(visual_type, role)
        limit = _limit(roles, role, filling=False)
        if len(projections) > limit:
            problems.append(f"{display} takes at most {limit} field{'' if limit == 1 else 's'}.")
        role_kind = spec.get("kind")
        for projection in projections:
            kind = _projection_kind(projection, "")
            label = projection_label(projection)
            if role_kind == "Grouping" and kind == "measure":
                problems.append(f"{label} is a measure, but {display} needs a column.")
            elif (
                role_kind == "Measure"
                and kind == "column"
                and visual_type not in {"card", "cardVisual"}  # text fields on a card
            ):
                problems.append(f"{label} is a column, but {display} needs a measure or a sum.")
    placed = {r: (b or {}).get("projections") for r, b in query_state.items()}
    complaint = _missing_required(roles, placed)
    if complaint:
        problems.append(complaint)
    return problems


# --- changing a visual's type --------------------------------------------------------

# Chart families whose roles mean the same thing under different ids. A slot is what a
# role is *for*: cat the main grouping, sub the second grouping, small small multiples,
# val the values, val2 values on the second (line) axis, tip tooltips.
_SLOTS: dict[str, dict[str, str]] = {
    "cartesian": {
        "Category": "cat",
        "Series": "sub",
        "Rows": "small",
        "Y": "val",
        "Tooltips": "tip",
    },
    "line": {
        "Category": "cat",
        "Series": "sub",
        "Rows": "small",
        "Y": "val",
        "Y2": "val2",
        "Tooltips": "tip",
    },
    "combo": {
        "Category": "cat",
        "Series": "sub",
        "Rows": "small",
        "Y": "val",
        "Y2": "val2",
        "Tooltips": "tip",
    },
    "pie": {"Category": "cat", "Series": "sub", "Y": "val", "Tooltips": "tip"},
    "treemap": {"Group": "cat", "Details": "sub", "Values": "val", "Tooltips": "tip"},
    "funnel": {"Category": "cat", "Y": "val", "Tooltips": "tip"},
    "waterfall": {"Category": "cat", "Breakdown": "sub", "Y": "val", "Tooltips": "tip"},
    "matrix": {"Rows": "cat", "Columns": "sub", "Values": "val"},
}
_FAMILY = {
    **dict.fromkeys(
        (
            "columnChart",
            "clusteredColumnChart",
            "hundredPercentStackedColumnChart",
            "barChart",
            "clusteredBarChart",
            "hundredPercentStackedBarChart",
            "stackedAreaChart",
            "hundredPercentStackedAreaChart",
            "ribbonChart",
        ),
        "cartesian",
    ),
    "lineChart": "line",
    "areaChart": "line",
    "lineClusteredColumnComboChart": "combo",
    "lineStackedColumnComboChart": "combo",
    "pieChart": "pie",
    "donutChart": "pie",
    "treemap": "treemap",
    "funnel": "funnel",
    "waterfallChart": "waterfall",
    "pivotTable": "matrix",
}
# Where a pair differs from the slot rule. A line's measures are lines: on a combo they go
# to the line axis, and the combo's legend only splits columns, so a line legend is left out.
_PAIR_OVERRIDES: dict[tuple[str, str], dict[str, tuple[str | None, str]]] = {
    ("line", "combo"): {
        "Y": ("Y2", ""),
        "Series": (None, "a combo chart's legend only splits its columns"),
    },
}


@dataclass(frozen=True)
class _Lost:
    """A projection the new type cannot hold: what is wrong, and whether to warn that a
    category would have become small multiples on a card."""

    projection: dict[str, Any]
    reason: str
    small_multiples_hint: bool = False


def _display_order(roles: TypeRoles, present: Collection[str]) -> list[str]:
    ordered = [
        *roles.grouping_roles,
        *roles.either_roles,
        *roles.measure_roles,
        *([roles.small_multiples] if roles.small_multiples else []),
    ]
    ordered = list(dict.fromkeys(ordered))
    return [r for r in ordered if r in present] + [r for r in present if r not in ordered]


def _bucket_projections(state: Mapping[str, Any], role: str) -> list[dict[str, Any]]:
    bucket = state.get(role)
    if not isinstance(bucket, dict):
        return []
    return [p for p in bucket.get("projections", []) if isinstance(p, dict)]


def _convert_by_slots(
    old_type: str,
    new_type: str,
    state: Mapping[str, Any],
    warnings: list[str],
) -> tuple[dict[str, list[dict[str, Any]]], list[_Lost]] | None:
    old_family, new_family = _FAMILY.get(old_type), _FAMILY.get(new_type)
    if old_family is None or new_family is None:
        return None
    old_slots, new_slots = _SLOTS[old_family], _SLOTS[new_family]
    by_slot = {slot: role for role, slot in new_slots.items()}
    overrides = _PAIR_OVERRIDES.get((old_family, new_family), {})
    new_roles = _roles_for(new_type)
    mapped: dict[str, list[dict[str, Any]]] = {}
    lost: list[_Lost] = []
    merged: list[str] = []
    for role in _display_order(_roles_for(old_type), state):
        for projection in _bucket_projections(state, role):
            target: str | None
            reason = ""
            if role in overrides:
                target, reason = overrides[role]
            else:
                slot = old_slots.get(role)
                target = by_slot.get(slot) if slot else None
                if target is None and slot == "val2":
                    target = by_slot.get("val")
                    merged.append(projection_label(projection))
                elif target is None and slot == "small":
                    reason = f"{_a(new_type)} cannot be split into small multiples"
                elif target is None:
                    reason = f"{_a(new_type)} has no {role_display_name(old_type, role)} field well"
            if target is not None and len(mapped.get(target, [])) >= _limit(
                new_roles, target, filling=False
            ):
                reason = (
                    f"{_a(new_type)} has no room for it in {role_display_name(new_type, target)}"
                )
                target = None
            if target is None:
                lost.append(_Lost(projection, reason))
            else:
                mapped.setdefault(target, []).append(copy.deepcopy(projection))
    if merged:
        warnings.append(
            f"{_join(merged)} used a separate line axis; {_a(new_type)} has one value axis, "
            "so they now share it."
        )
    return mapped, lost


def _convert_generic(
    old_type: str,
    new_type: str,
    state: Mapping[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], list[_Lost]]:
    old_roles, new_roles = _roles_for(old_type), ROLE_TABLE[new_type]
    old_catalog = _catalog_roles(old_type)
    new_catalog = _catalog_roles(new_type)
    name = _a(new_type)
    slots: list[FieldSlot] = []
    small: list[FieldSlot] = []
    tooltips: list[dict[str, Any]] = []
    lost: list[_Lost] = []
    for role in _display_order(old_roles, state):
        for projection in _bucket_projections(state, role):
            if role == old_roles.small_multiples:
                if new_roles.small_multiples:
                    small.append(FieldSlot("column", projection))
                else:
                    lost.append(_Lost(projection, f"{name} cannot be split into small multiples"))
            elif role == "Tooltips":
                if "Tooltips" in new_catalog:
                    tooltips.append(copy.deepcopy(projection))
                else:
                    lost.append(_Lost(projection, f"{name} has no tooltip field well"))
            else:
                measure_role = old_catalog.get(role, {}).get("kind") == "Measure"
                kind = _projection_kind(projection, "measure" if measure_role else "column")
                slots.append(FieldSlot("measure" if kind == "measure" else "column", projection))
    placed, refused = _place(new_roles, slots, small)
    for refusal in refused:
        hint = (
            refusal.code == "kind"
            and refusal.slot.kind == "column"
            and bool(new_roles.small_multiples)
        )
        reason = (
            f"{name} shows totals, and a category there would split it into small "
            "multiples, one card per value"
            if hint
            else f"{name} {refusal.clause('it')}"
        )
        lost.append(_Lost(refusal.slot.projection, reason, hint))
    if tooltips:
        placed["Tooltips"] = tooltips
    return placed, lost


_OBJECT_NAMES = {
    "categoryAxis": "Category axis",
    "valueAxis": "Value axis",
    "y2Axis": "Y2 axis",
    "dataPoint": "Data colors",
    "labels": "Data labels",
    "legend": "Legend",
    "plotArea": "Plot area",
    "zoom": "Zoom slider",
    "referenceLine": "Constant line",
    "y1AxisReferenceLine": "Y-axis constant line",
    "xAxisReferenceLine": "X-axis constant line",
    "lineStyles": "Line styles",
    "seriesLabels": "Series labels",
    "smallMultiplesLayout": "Small multiples layout",
    "referenceLabel": "Reference labels",
    "columnHeaders": "Column headers",
    "rowHeaders": "Row headers",
    "subTotals": "Subtotals",
    "total": "Totals",
    "columnTotal": "Column totals",
    "rowTotal": "Row totals",
    "grid": "Grid",
}


def _object_name(name: str) -> str:
    if name in _OBJECT_NAMES:
        return _OBJECT_NAMES[name]
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    return _capital(spaced.lower())


@dataclass
class TypePlan:
    query_state: dict[str, Any]
    objects: dict[str, Any]
    dropped: list[str]
    warnings: list[str]
    # Projections that did not fit the new type. The caller passes them to
    # drop_field_references so sorts on them go too.
    removed: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


# Addable types whose formatting Desktop only reads from a state entry: a cardVisual honors
# nothing written without {"id": "default"}. pivotTable is on Desktop's list too but the
# real matrices keep grid, header and width settings without one, so only cards get it.
_STATE_SELECTOR_TYPES = frozenset({"cardVisual"})


def _fits(old_spec: dict[str, Any] | None, new_spec: dict[str, Any], value: Any) -> bool:
    """Whether a value written for the old type's property is valid for the new type's.

    The same property name can mean something else: a donut's label position is Top or
    Bottom, a card's is belowValue or aboveValue. Desktop does not define the wrong one.
    """
    if old_spec is not None and (
        old_spec.get("type") != new_spec.get("type")
        or old_spec.get("subtype") != new_spec.get("subtype")
    ):
        return False
    values = new_spec.get("values")
    if new_spec.get("type") == "enum" and isinstance(values, list):
        decoded = expr.decode(value)
        if decoded is None:
            return True  # not a plain literal (a measure-driven value): keep it as written
        text = str(decoded).lower() if isinstance(decoded, bool) else str(decoded)
        return text in {str(v) for v in values}
    return True


def _carry_objects(
    old_type: str,
    new_type: str,
    objects: Mapping[str, Any],
    state: Mapping[str, dict[str, Any]],
    old_state: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    refs = {p.get("queryRef") for bucket in state.values() for p in bucket.get("projections", [])}
    columns = {
        field_identity(p)
        for bucket in state.values()
        for p in bucket.get("projections", [])
        if field_identity(p)[0] == "Column"
    }
    labels = {
        p.get("queryRef"): projection_label(p)
        for bucket in old_state.values()
        for p in (bucket or {}).get("projections", [])
        if isinstance(p, dict)
    }
    carried: dict[str, Any] = {}
    lost_props: dict[str, int] = {}
    kept_props: set[str] = set()
    stale: dict[str, int] = {}
    for name, entries in objects.items():
        kept_entries: list[dict[str, Any]] = []
        for entry in entries:
            selector = entry.get("selector")
            if isinstance(selector, dict):
                metadata = selector.get("metadata")
                stale_label: str | None = None
                if isinstance(metadata, str) and metadata not in refs:
                    stale_label = labels.get(metadata, metadata)
                else:
                    for column in _scope_columns(selector):
                        if column not in columns:
                            stale_label = column[-1]
                            break
                if stale_label is not None:
                    stale[stale_label] = stale.get(stale_label, 0) + _entry_size(entry)
                    continue
            properties = entry.get("properties", {})
            supported: dict[str, Any] = {}
            for prop, value in properties.items():
                spec = expr.property_spec(new_type, name, prop)
                if spec is None or spec[0] != "objects":
                    continue
                old_spec = expr.property_spec(old_type, name, prop)
                if _fits(old_spec[1] if old_spec else None, spec[1], value):
                    supported[prop] = value
            lost = len(properties) - len(supported)
            if lost:
                lost_props[name] = lost_props.get(name, 0) + lost
            if supported:
                kept_props.add(name)
                carried_entry = {**copy.deepcopy(entry), "properties": copy.deepcopy(supported)}
                if new_type in _STATE_SELECTOR_TYPES and not carried_entry.get("selector"):
                    carried_entry["selector"] = {"id": expr.STATE_DEFAULT}
                twin = next(
                    (
                        e
                        for e in kept_entries
                        if expr.same_selector(e.get("selector"), carried_entry.get("selector"))
                    ),
                    None,
                )
                if twin is not None and carried_entry.get("selector"):
                    # Two entries for one state would be a duplicate; the later wins.
                    twin["properties"].update(carried_entry["properties"])
                else:
                    kept_entries.append(carried_entry)
        if kept_entries:
            carried[name] = kept_entries
    dropped: list[str] = []
    if lost_props:
        names = [
            _object_name(name) + (" (some options)" if name in kept_props else "")
            for name in lost_props
        ]
        dropped.append(
            f"Dropped {_plural(len(names), 'setting')} the new type does not have: "
            f"{_join(names, 'and')}."
        )
    for label, count in stale.items():
        dropped.append(f"Dropped {_plural(count, 'formatting setting')} for {label}.")
    return carried, dropped


def plan_type_change(old_type: str, new_type: str, visual: dict[str, Any]) -> TypePlan:
    """The query state and formatting objects a visual has as another type.

    Roles are remapped by what they are for (a column chart's Category is a pie's Legend,
    a line chart's measures are a combo's lines), with a fallback by field kind for pairs
    that have no explicit map. Every formatting object and property the new type's catalog
    has is kept, and so are selectors whose field survives (per-category colors, data
    labels, a table's column widths); the rest is listed in ``dropped``. A category that
    would land in a card is left out with a warning rather than becoming small multiples.
    Raises PlacementError when the new type cannot be filled at all.
    """
    if new_type not in ADDABLE:
        raise PlacementError(f"Studio cannot turn a visual into {_a(new_type)} yet.")
    body = visual.get("visual")
    if not isinstance(body, dict):
        raise PlacementError("A group of visuals cannot change type; change the visuals in it.")
    _roles_for(old_type)  # unknown types are refused before anything is planned
    old_state: dict[str, Any] = body.get("query", {}).get("queryState", {}) or {}
    old_objects: dict[str, Any] = body.get("objects", {}) or {}
    if old_type == new_type:
        return TypePlan(copy.deepcopy(old_state), copy.deepcopy(old_objects), [], [])
    warnings: list[str] = []
    converted = _convert_by_slots(old_type, new_type, old_state, warnings)
    if converted is None:
        converted = _convert_generic(old_type, new_type, old_state)
    mapped, lost = converted
    new_roles = ROLE_TABLE[new_type]
    complaint = _missing_required(new_roles, mapped)
    if complaint:
        raise PlacementError(complaint)
    new_state: dict[str, Any] = {}
    for role, projections in mapped.items():
        extras = (
            {k: copy.deepcopy(v) for k, v in old_state[role].items() if k != "projections"}
            if isinstance(old_state.get(role), dict)
            else {}
        )
        new_state[role] = {**extras, "projections": projections}
    dropped: list[str] = []
    removed: list[dict[str, Any]] = []
    for item in lost:
        removed.append(item.projection)
        label = projection_label(item.projection)
        if item.small_multiples_hint:
            warnings.append(
                f"{label} was left out. On a card a category becomes small multiples, one "
                "card per value; ask for small multiples if that is what you want."
            )
        dropped.append(f"Left out {label}: {item.reason}.")
    objects, lost_settings = _carry_objects(old_type, new_type, old_objects, new_state, old_state)
    return TypePlan(new_state, objects, dropped + lost_settings, warnings, removed)


# --- titles, size and placement ------------------------------------------------------

_NO_TITLE = frozenset({"tableEx", "pivotTable", "textbox", "shape", "image"})
_FIELD_NAME_TITLE = frozenset({"card", "cardVisual", "multiRowCard", "slicer"})
_TITLE_OFF = frozenset(
    {
        "tableEx",
        "pivotTable",
        "table",
        "matrix",
        "card",
        "cardVisual",
        "multiRowCard",
        "slicer",
        "advancedSlicerVisual",
        "listSlicer",
        "textSlicer",
        "filterSlicer",
        "textbox",
        "shape",
        "basicShape",
        "image",
        "actionButton",
    }
)


def default_title_visible(visual_type: str) -> bool:
    """Whether Desktop shows a title on a new visual: on for charts, gauge and KPI, off for
    tables, cards, slicers and shapes."""
    return visual_type in expr.visual_types() and visual_type not in _TITLE_OFF


def _labels(state: Mapping[str, Any], role: str) -> list[str]:
    return [projection_label(p) for p in _bucket_projections(state, role)]


def _title_join(names: Sequence[str]) -> str:
    return _join(list(names), "and")


def automatic_title(visual_type: str, query_state: dict[str, Any]) -> str:
    """The title Desktop shows when there is no title literal: the measures joined with
    "and", then " by " and the groupings, e.g. ``Total Sales by ChannelName`` (measured on
    the exp4 chart). Tables, matrices and shapes have none (empty string). The scatter,
    gauge and KPI shapes are Desktop's as far as known and unverified beyond the plain
    "measure by grouping" rule."""
    roles = ROLE_TABLE.get(visual_type)
    if roles is None or visual_type in _NO_TITLE:
        return ""
    if visual_type in _FIELD_NAME_TITLE:
        names = [
            label
            for role in (*roles.measure_roles, *roles.grouping_roles, *roles.either_roles)
            for label in _labels(query_state, role)
        ]
        return _title_join(names)
    if visual_type == "scatterChart":
        by = [
            *_labels(query_state, "X"),
            *_labels(query_state, "Category"),
            *_labels(query_state, "Series"),
        ]
        return _by(_labels(query_state, "Y"), by)
    if visual_type == "gauge":
        return _title_join(_labels(query_state, "Y"))
    if visual_type == "kpi":
        return _by(_labels(query_state, "Indicator"), _labels(query_state, "TrendLine"))
    catalog = _catalog_roles(visual_type)
    measure_roles = [
        *roles.measure_roles,
        *(
            r
            for r, s in catalog.items()
            if s.get("kind") == "Measure" and r != "Tooltips" and r not in roles.measure_roles
        ),
    ]
    measures = [label for role in measure_roles for label in _labels(query_state, role)]
    groups = [label for role in roles.grouping_roles for label in _labels(query_state, role)]
    return _by(measures, groups)


def _by(measures: Sequence[str], groups: Sequence[str]) -> str:
    if measures and groups:
        return f"{_title_join(measures)} by {_title_join(groups)}"
    return _title_join(measures or groups)


_SIZES: dict[str, tuple[float, float]] = {
    "card": (200, 120),
    "cardVisual": (240, 130),
    "multiRowCard": (300, 200),
    "tableEx": (600, 350),
    "pivotTable": (600, 350),
    "slicer": (200, 280),
    "advancedSlicerVisual": (300, 120),
    "pieChart": (320, 300),
    "donutChart": (320, 300),
    "gauge": (300, 240),
    "kpi": (240, 160),
    "scatterChart": (500, 350),
    "waterfallChart": (500, 320),
    "textbox": (300, 60),
    "shape": (200, 100),
    "image": (200, 200),
}


def default_size(visual_type: str) -> tuple[float, float]:
    """A sensible (width, height) in page pixels for a new visual: 400 x 300 for charts."""
    return _SIZES.get(visual_type, (400, 300))


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height


_EPSILON = 1e-6
_GRID = 10.0


def _overlap_area(a: Box, b: Box) -> float:
    width = min(a.right, b.right) - max(a.x, b.x)
    height = min(a.bottom, b.bottom) - max(a.y, b.y)
    return width * height if width > _EPSILON and height > _EPSILON else 0.0


# The public name the page review measures overlaps with (same rule as placement's).
overlap_area = _overlap_area


def overlaps(box: Box, others: Sequence[Box]) -> list[Box]:
    """The boxes that share area with ``box`` (touching edges do not count)."""
    return [other for other in others if _overlap_area(box, other) > 0]


def _axis(low: float, high: float, step: float = _GRID) -> list[float]:
    """Grid positions from ``low`` to ``high`` inclusive; ``high`` itself is always a
    candidate so a box that only fits flush against the edge is found."""
    if high < low - _EPSILON:
        return [low]
    count = int(math.floor((high - low) / step + _EPSILON))
    positions = [low + i * step for i in range(count + 1)]
    if high - positions[-1] > _EPSILON:
        positions.append(high)
    return positions


def _with_edges(grid: list[float], edges: Sequence[float], low: float, high: float) -> list[float]:
    """``grid`` plus the ``edges`` that fall inside [low, high], sorted, without near-duplicates."""
    merged = sorted([*grid, *(e for e in edges if low - _EPSILON <= e <= high + _EPSILON)])
    unique: list[float] = []
    for value in merged:
        if not unique or value - unique[-1] > _EPSILON:
            unique.append(value)
    return unique


def _first_free(
    region: Box,
    width: float,
    height: float,
    occupied: Sequence[Box],
    gap: float,
    *,
    edges: bool = False,
) -> tuple[float, float] | None:
    if region.right - width < region.x - _EPSILON or region.bottom - height < region.y - _EPSILON:
        return None
    xs = _axis(region.x, region.right - width)
    ys = _axis(region.y, region.bottom - height)
    inflated = [Box(o.x - gap, o.y - gap, o.width + 2 * gap, o.height + 2 * gap) for o in occupied]
    if edges:
        # A hole left by a deleted visual is as wide as that visual, so its left edge sits
        # right of a neighbour's edge, which is rarely on the 10-pixel grid (fractional
        # coordinates are normal in real reports). Sliding a free box left and up until it
        # touches something shows such a spot always starts at a neighbour's edge.
        xs = _with_edges(xs, [o.right for o in inflated], region.x, region.right - width)
        ys = _with_edges(ys, [o.bottom for o in inflated], region.y, region.bottom - height)
    for y in ys:
        # Only boxes that reach into this row can block it.
        row = [o for o in inflated if o.y < y + height - _EPSILON and o.bottom > y + _EPSILON]
        index = 0
        while index < len(xs):
            x = xs[index]
            blocker = next(
                (o for o in row if o.x < x + width - _EPSILON and o.right > x + _EPSILON), None
            )
            if blocker is None:
                return (x, y)
            # Skip to the first column past the blocker instead of stepping through it.
            index += 1
            while index < len(xs) and xs[index] < blocker.right - _EPSILON:
                index += 1
    return None


def free_position(
    page_width: float,
    page_height: float,
    occupied: Sequence[Box],
    width: float,
    height: float,
    *,
    prefer: Box | None = None,
    gap: float = _GRID,
) -> tuple[float, float]:
    """The first spot, scanning left to right and top to bottom on a 10-pixel grid, where a
    ``width`` x ``height`` visual overlaps nothing in ``occupied``.

    ``prefer`` (for example the area right of a selected visual) is tried first. A
    ``gap`` of clear space around other visuals is kept when possible and given up
    before overlapping. When the page is full, the spot that covers the least area is
    returned; the caller checks ``overlaps`` and adds an advisory.
    """
    regions = [Box(0.0, 0.0, page_width, page_height)]
    if prefer is not None:
        left, top = max(prefer.x, 0.0), max(prefer.y, 0.0)
        regions.insert(
            0,
            Box(
                left,
                top,
                min(prefer.right, page_width) - left,
                min(prefer.bottom, page_height) - top,
            ),
        )
    spacings = (gap, 0.0) if gap > 0 else (0.0,)
    for region in regions:
        for spacing in spacings:
            # The grid scan is the common, cheap case; edge candidates are the exact fallback.
            for edges in (False, True):
                found = _first_free(region, width, height, occupied, spacing, edges=edges)
                if found is not None:
                    return found
    return _least_overlap(page_width, page_height, occupied, width, height)


def _least_overlap(
    page_width: float, page_height: float, occupied: Sequence[Box], width: float, height: float
) -> tuple[float, float]:
    # A coarser grid keeps a huge page with many visuals quick.
    xs, ys = _axis(0.0, page_width - width), _axis(0.0, page_height - height)
    step = _GRID * max(1, math.ceil(math.sqrt(len(xs) * len(ys) / 10000)))
    edges = [(o.x, o.y, o.right, o.bottom) for o in occupied]
    best = (0.0, 0.0)
    best_area = math.inf
    for y in _axis(0.0, page_height - height, step):
        for x in _axis(0.0, page_width - width, step):
            area = 0.0
            for left, top, right, bottom in edges:
                w = min(x + width, right) - max(x, left)
                h = min(y + height, bottom) - max(y, top)
                if w > _EPSILON and h > _EPSILON:
                    area += w * h
                    if area >= best_area:
                        break
            if area < best_area - _EPSILON:
                best, best_area = (x, y), area
    return best
