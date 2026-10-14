"""Filter operations for an imported report, written the way Power BI Desktop writes them.

A filter lives in ``filterConfig.filters`` of a visual.json, a page.json or report.json.
Desktop's shape has rules that Microsoft's validator enforces and that pbi-cli got wrong:
- ``filter.Version`` is 2 and the ``From`` list holds an alias per table. Inside ``Where`` a
  field is reached through ``SourceRef.Source`` (the alias), never ``SourceRef.Entity``
  (PBIR_FILTER_ENTITY_IN_WHERE, PBIR_FILTER_SOURCE_UNKNOWN). Only the top-level ``field``
  uses ``Entity``.
- ``name`` is ``Filter`` plus 20 hex characters and is unique in the whole report, including
  bookmarks (PBIR_FILTER_NAME_DUPLICATE_GLOBAL). A replaced filter keeps its name, because a
  bookmark may point at it.
- A literal's type comes from the field in the model, not from the Python type of what the
  user typed: 2014 is ``'2014'`` on a text column, ``2014L`` on a whole-number column and
  ``2014M`` on a fixed-decimal one (``pbir_expr.data_literal``).

Every variant was checked in Power BI Desktop (``VERIFIED_VARIANTS``); the edit layer
(``report_edit_filters``) still routes each operation through ``spec_variants`` and refuses a
variant that flag turns off, so a shape found wrong later can be withdrawn in one place.

This module imports nothing from ``report_edits`` so the operation union can import it.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any, Literal, Protocol

from pydantic import Field

from ai_powerbi_analyst.contracts import ClosedModel

from . import pbir_expr as expr
from .pbir_document import ROOT, DocumentError, PbirDocument
from .pbix_package import PackageError

# --- the model the filters are checked against ---------------------------------------------


class FilterModel(Protocol):
    """Implemented by ``report_edits.ModelInfo`` in the integration step."""

    def resolve(self, ref: Any) -> tuple[str, str, str]:
        """(kind "column"|"measure", table, name) in the model's own spelling, or DocumentError.

        ``ref.table`` may be None: the model then finds the one column or measure called
        ``ref.name`` and raises DocumentError when it is missing or ambiguous."""
        ...

    def data_type(self, table: str, name: str) -> str | int | None:
        """The model data type id or name of a column or measure, None when unknown."""
        ...


# --- specifications ------------------------------------------------------------------------

Aggregation = Literal["Sum", "Average", "Min", "Max", "Count", "CountNonNull"]
# Same codes as report_edits._field_projection so a filter and a visual agree on a field.
# UNVERIFIED: Desktop may use 2 for a distinct count and 5 for a plain count.
_AGGREGATION_CODES = {"Sum": 0, "Average": 1, "Count": 2, "Min": 3, "Max": 4, "CountNonNull": 5}
_AGGREGATION_WORDS = {
    0: "Sum of",
    1: "Average of",
    2: "Count of",
    3: "Minimum of",
    4: "Maximum of",
    5: "Count of",
}

FilterValue = Annotated[str, Field(max_length=500)] | int | float | bool


class FieldRef(ClosedModel):
    table: str | None = Field(default=None, min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    # "Sum", "Average"… for a numeric column filtered as a total (Desktop's "Sum of Sales").
    aggregation: Aggregation | None = None


ConditionOperator = Literal[
    "is",
    "is_not",
    "greater_than",
    "greater_or_equal",
    "less_than",
    "less_or_equal",
    "contains",
    "not_contains",
    "starts_with",
    "not_starts_with",
    "is_blank",
    "is_not_blank",
]


class Condition(ClosedModel):
    operator: ConditionOperator
    value: FilterValue | None = None  # not used by is_blank / is_not_blank


class CategoricalFilter(ClosedModel):
    """Keep (or with ``exclude`` drop) the listed values of a column."""

    kind: Literal["categorical"] = "categorical"
    field: FieldRef
    values: list[FilterValue | None] = Field(min_length=1, max_length=200)  # None is (Blank)
    exclude: bool = False


class AdvancedFilter(ClosedModel):
    """One or two conditions on a column or measure, as Desktop's advanced filtering allows."""

    kind: Literal["advanced"] = "advanced"
    field: FieldRef
    conditions: list[Condition] = Field(min_length=1, max_length=2)
    logic: Literal["and", "or"] = "and"


class TopNFilter(ClosedModel):
    """The top or bottom ``n`` values of a column ranked by a measure (or a total)."""

    kind: Literal["top_n"] = "top_n"
    field: FieldRef
    n: int = Field(ge=1, le=1000)
    by: FieldRef
    direction: Literal["top", "bottom"] = "top"


RelativeUnit = Literal[
    "days", "weeks", "months", "years", "calendar_weeks", "calendar_months", "calendar_years"
]


class RelativeDateFilter(ClosedModel):
    """A date column relative to today: last 30 days, this calendar month, next 2 years."""

    kind: Literal["relative_date"] = "relative_date"
    field: FieldRef
    unit: RelativeUnit
    direction: Literal["last", "this", "next"]
    count: int = Field(default=1, ge=1, le=1000)  # ignored for "this"
    include_today: bool = False  # or the current week, month or year for a calendar unit


FilterSpec = Annotated[
    CategoricalFilter | AdvancedFilter | TopNFilter | RelativeDateFilter,
    Field(discriminator="kind"),
]


class SetPageFilter(ClosedModel):
    op: Literal["set_page_filter"] = "set_page_filter"
    page: str = Field(min_length=1, max_length=200)  # page id or display name
    filter: FilterSpec
    locked: bool = False
    hidden: bool = False


class SetVisualFilter(ClosedModel):
    op: Literal["set_visual_filter"] = "set_visual_filter"
    visual: str = Field(min_length=1, max_length=200)
    filter: FilterSpec
    locked: bool = False
    hidden: bool = False


class SetReportFilter(ClosedModel):
    op: Literal["set_report_filter"] = "set_report_filter"
    filter: FilterSpec
    locked: bool = False
    hidden: bool = False


class ClearFilter(ClosedModel):
    """Remove the filter on one field, or every filter, at one scope."""

    op: Literal["clear_filter"] = "clear_filter"
    scope: Literal["visual", "page", "report"]
    target: str | None = Field(default=None, min_length=1, max_length=200)  # visual id / page
    field: FieldRef | None = None  # None clears every filter at that scope


FilterOperation = SetPageFilter | SetVisualFilter | SetReportFilter | ClearFilter
FILTER_OPERATIONS = (SetPageFilter, SetVisualFilter, SetReportFilter, ClearFilter)

# Which shapes were checked in Power BI Desktop 2.157.1354.0 on 2026-09-28: the Agents report's
# own advanced measure filter, and 30 filters written by this module on scratch pages (every
# variant below, visual and page scope, plus a locked and hidden one). Desktop returned the
# expected rows for each (Jan/Feb only, Year > 2007, top 3 months by Total Sales, last 17 years
# rolling vs calendar, ...) and its saved file kept every filter object unchanged. Not probed:
# a fixed-decimal (M) column, a HierarchyLevel field and a filter on a measure of another table
# than the visual's. ``spec_variants`` says which variants a spec uses.
VERIFIED_VARIANTS: dict[str, bool] = {
    "categorical_in": True,
    "categorical_not_in": True,
    "advanced_comparison_measure": True,
    "advanced_comparison_column": True,
    "advanced_date_comparison": True,
    "advanced_not": True,
    "advanced_text": True,
    "advanced_blank": True,
    "advanced_and": True,
    "advanced_or": True,
    "advanced_aggregated_field": True,
    "top_n": True,
    "bottom_n": True,
    "relative_date_days": True,
    "relative_date_rolling": True,
    "relative_date_calendar": True,
    "locked_in_view_mode": True,
    "hidden_in_view_mode": True,
}

# --- values ----------------------------------------------------------------------------------

_TYPE_WORDS = {
    "text": "text",
    "integer": "a whole number",
    "double": "a number",
    "decimal": "a fixed decimal number",
    "datetime": "a date",
    "boolean": "true or false",
}
_NUMERIC = frozenset({"integer", "double", "decimal"})


def _default_name() -> str:
    return "Filter" + secrets.token_hex(10)


def _type_name(data_type: str | int | None) -> str | None:
    """A model data type as one of the names in ``_TYPE_WORDS``, or None when unknown.

    ``ModelInfo`` keeps pandas dtypes, so those are read too."""
    name = expr.data_type_name(data_type)
    if name is not None:
        return "datetime" if name == "date" else name
    if isinstance(data_type, str):
        lowered = data_type.casefold()
        for marker, result in (
            ("datetime", "datetime"),
            ("bool", "boolean"),
            ("int", "integer"),
            ("decimal", "decimal"),
            ("float", "double"),
            ("double", "double"),
            ("object", "text"),
            ("string", "text"),
            ("str", "text"),
        ):
            if marker in lowered:
                return result
    return None


@dataclass(frozen=True)
class _Field:
    kind: str  # "column" | "measure"
    table: str
    name: str
    aggregation: str | None
    type_name: str | None

    @property
    def value_type(self) -> str | None:
        """The type of the values the filter compares (a total of a column is not a column)."""
        if self.kind == "measure" or self.aggregation is None:
            return self.type_name
        if self.aggregation in {"Count", "CountNonNull"}:
            return "integer"
        if self.aggregation == "Average":
            return "double"
        return self.type_name

    @property
    def text(self) -> str:
        if self.aggregation:
            return f"{_AGGREGATION_WORDS[_AGGREGATION_CODES[self.aggregation]]} {self.name}"
        return self.name


def _resolve(ref: FieldRef, model: FilterModel) -> _Field:
    kind, table, name = model.resolve(ref)
    if kind not in {"column", "measure"}:
        raise DocumentError(f"{name} cannot be used in a filter")
    type_name = _type_name(model.data_type(table, name))
    if ref.aggregation is not None:
        if kind == "measure":
            raise DocumentError(f"{name} is a measure, which already totals; leave the total out")
        wide = type_name in {"text", "boolean", "datetime"}
        if ref.aggregation in {"Sum", "Average"} and wide:
            raise DocumentError(
                f"{name} is {_TYPE_WORDS[type_name or '']}, so it cannot be totalled"
            )
    return _Field(kind, table, name, ref.aggregation, type_name)


def _literal(field: _Field, value: Any) -> str:
    """The Literal text of ``value`` for this field, named in the message when it is wrong."""
    type_name = field.value_type
    if type_name is None:
        if field.kind == "measure":
            return _measure_literal(field, value)
        raise DocumentError(
            f"Studio cannot tell what kind of values {field.name} holds, so it cannot filter it"
        )
    try:
        text = expr.data_literal(value, type_name)
    except expr.ExpressionError as error:
        if "too large" in str(error):  # a number of the right kind that the column cannot hold
            raise DocumentError(
                f"{value!r} is too large for {field.name}, {_TYPE_WORDS[type_name]}"
            ) from error
        raise DocumentError(
            f"{field.name} is {_TYPE_WORDS[type_name]}; {value!r} is not"
        ) from error
    return _in_range(field, value, text)


def _in_range(field: _Field, value: Any, text: str) -> str:
    """Desktop cannot load a whole-number literal beyond 64 bits, and data_literal has no check."""
    whole = text[:-1] if text.endswith("L") else ""
    if whole.lstrip("-").isdigit() and not -(2**63) <= int(whole) < 2**63:
        raise DocumentError(f"{value!r} is too large for {field.name}, a whole number")
    if text.endswith("M"):
        return _fixed_decimal(field, value, text[:-1])
    return text


# A fixed decimal is a 64-bit integer scaled by 10,000, so it holds 4 decimals and stops here.
_FIXED_LIMIT = Decimal("922337203685477.5807")


def _fixed_decimal(field: _Field, value: Any, digits: str) -> str:
    """An M literal as plain digits with at most 4 decimals: ``repr`` of a small float has an
    exponent (``1e-07``) and a huge one has hundreds of digits, neither of which the column
    can hold, and M columns were not probed in Desktop for anything but plain digits."""
    number = Decimal(digits)
    if abs(number) > _FIXED_LIMIT:
        raise DocumentError(
            f"{value!r} is too large for {field.name}, a fixed decimal number "
            "(the limit is about 922 trillion)"
        )
    rounded = number.quantize(Decimal("0.0001"))
    return "0M" if rounded == 0 else format(rounded.normalize(), "f") + "M"


def _measure_literal(field: _Field, value: Any) -> str:
    """A measure whose type the model does not report takes a number."""
    for guess in ("integer", "double"):
        try:
            return _in_range(field, value, expr.data_literal(value, guess))
        except (expr.ExpressionError, DocumentError):
            continue  # too big for a whole number: try a decimal one
    raise DocumentError(f"{field.name} is a measure and takes a number; {value!r} is not")


def _literal_node(text: str) -> dict[str, Any]:
    return {"Literal": {"Value": text}}


# --- names and aliases --------------------------------------------------------------------------

_FILTER_NAME = re.compile(r"Filter[0-9a-f]{20}")


def _names_in(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        name = node.get("name")
        if isinstance(name, str) and _FILTER_NAME.fullmatch(name):
            found.add(name)
        for value in node.values():
            _names_in(value, found)
    elif isinstance(node, list):
        for item in node:
            _names_in(item, found)


def _scope_names(holder: dict[str, Any], found: set[str]) -> None:
    config = holder.get("filterConfig")
    filters = config.get("filters") if isinstance(config, dict) else None
    if isinstance(filters, list):
        for item in filters:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                found.add(item["name"])


def collect_filter_names(document: PbirDocument) -> set[str]:
    """Every filter name in the report: report, page and visual filters, and bookmarks."""
    found: set[str] = set()
    _scope_names(document.report, found)
    for page in document.pages.values():
        _scope_names(page.data, found)
        for visual in page.visuals.values():
            _scope_names(visual, found)
    for part in document.package.names():
        if part.startswith(ROOT + "bookmarks/") and part.endswith(".json"):
            try:
                _names_in(document.package.read_json(part), found)
            except (KeyError, PackageError, ValueError):
                continue
    return found


def _new_name(used: set[str], factory: Callable[[], str]) -> str:
    for _ in range(100):
        candidate = factory()
        if candidate not in used:
            return candidate
    raise DocumentError("Studio could not find an unused name for the filter")


class _Aliases:
    """From-list aliases the way Desktop picks them: the table's first letter, lower-cased."""

    def __init__(self) -> None:
        self._by_entity: dict[str, str] = {}

    def __call__(self, entity: str) -> str:
        if entity not in self._by_entity:
            base = entity[:1].lower() or "t"
            alias, index = base, 0
            taken = set(self._by_entity.values())
            while alias in taken:
                index += 1
                alias = f"{base}{index}"
            self._by_entity[entity] = alias
        return self._by_entity[entity]

    def source(self, entity: str) -> dict[str, Any]:
        return {"Source": self(entity)}

    def entries(self) -> list[dict[str, Any]]:
        return [{"Name": a, "Entity": e, "Type": 0} for e, a in self._by_entity.items()]


# --- expression nodes ---------------------------------------------------------------------


def _field_node(field: _Field, source: dict[str, Any]) -> dict[str, Any]:
    prop = {"Expression": {"SourceRef": source}, "Property": field.name}
    if field.kind == "measure":
        return {"Measure": prop}
    column: dict[str, Any] = {"Column": prop}
    if field.aggregation is None:
        return column
    return {
        "Aggregation": {"Expression": column, "Function": _AGGREGATION_CODES[field.aggregation]}
    }


_COMPARISON_KINDS = {
    "is": 0,
    "greater_than": 1,
    "greater_or_equal": 2,
    "less_than": 3,
    "less_or_equal": 4,
}
_TEXT_OPERATORS = {
    "contains": ("Contains", False),
    "not_contains": ("Contains", True),
    "starts_with": ("StartsWith", False),
    "not_starts_with": ("StartsWith", True),
}


def _not(node: dict[str, Any]) -> dict[str, Any]:
    return {"Not": {"Expression": node}}


def _condition_node(condition: Condition, field: _Field, left: dict[str, Any]) -> dict[str, Any]:
    operator = condition.operator
    if operator in {"is_blank", "is_not_blank"}:
        blank = {"Comparison": {"ComparisonKind": 0, "Left": left, "Right": _literal_node("null")}}
        return blank if operator == "is_blank" else _not(blank)
    if condition.value is None:
        raise DocumentError(f"Give a value to compare {field.text} with")
    type_name = field.value_type
    if operator in _TEXT_OPERATORS:
        if type_name != "text":
            raise DocumentError(
                f"{field.text} is not text, so it cannot use 'contains' or 'starts with'"
            )
        name, negate = _TEXT_OPERATORS[operator]
        node: dict[str, Any] = {
            name: {"Left": left, "Right": _literal_node(_literal(field, condition.value))}
        }
        return _not(node) if negate else node
    if operator not in {"is", "is_not"} and type_name not in _NUMERIC | {"datetime", None}:
        raise DocumentError(
            f"{field.text} is {_TYPE_WORDS.get(type_name or '', 'not a number')}, so it cannot be "
            "compared with greater or less than"
        )
    comparison = {
        "Comparison": {
            "ComparisonKind": _COMPARISON_KINDS["is" if operator == "is_not" else operator],
            "Left": left,
            "Right": _literal_node(_literal(field, condition.value)),
        }
    }
    return _not(comparison) if operator == "is_not" else comparison


# --- relative dates -----------------------------------------------------------------------

_DAY = 0
# TimeUnit codes of the semantic query schema: day 0, week 1, month 2, year 3 (4 is a decade).
_YEAR = 3
_UNITS: dict[str, tuple[int, bool]] = {
    "days": (_DAY, True),  # a run of days is a calendar run of one-day units
    "weeks": (1, False),
    "months": (2, False),
    "years": (_YEAR, False),
    "calendar_weeks": (1, True),
    "calendar_months": (2, True),
    "calendar_years": (_YEAR, True),
}
_UNIT_WORDS = {
    "days": "days",
    "weeks": "weeks",
    "months": "months",
    "years": "years",
    "calendar_weeks": "calendar weeks",
    "calendar_months": "calendar months",
    "calendar_years": "calendar years",
}
_THIS_WORDS = {
    "days": "today",
    "calendar_weeks": "this calendar week",
    "calendar_months": "this calendar month",
    "calendar_years": "this calendar year",
}
_CURRENT_WORDS = {
    "days": "today",
    "calendar_weeks": "this week",
    "calendar_months": "this month",
    "calendar_years": "this year",
    "weeks": "today",
    "months": "today",
    "years": "today",
}

# A bound is (DateAdd steps from Now outwards as (amount, unit), DateSpan unit).
_Bound = tuple[tuple[tuple[int, int], ...], int]


def _bounds(direction: str, unit: str, count: int, include_today: bool) -> tuple[_Bound, _Bound]:
    """The Between bounds of Desktop's relative date filter.

    Not measured in Desktop yet: from the published query language. "Last 30 days" runs from
    30 days ago to yesterday, or to today when today is included; a calendar unit shifts
    whole weeks, months or years and DateSpan widens each bound to the unit's edge."""
    code, calendar = _UNITS[unit]
    span = code if calendar else _DAY

    def bound(*steps: tuple[int, int]) -> _Bound:
        return tuple(s for s in steps if s[0] != 0), span

    if direction == "this":
        return bound(), bound()
    if calendar:
        if direction == "last":
            first = -(count - 1) if include_today else -count
            return bound((first, code)), bound((0 if include_today else -1, code))
        last = count - 1 if include_today else count
        return bound((0 if include_today else 1, code)), bound((last, code))
    if direction == "last":
        if include_today:
            return bound((1, _DAY), (-count, code)), bound()
        return bound((-count, code)), bound((-1, _DAY))
    if include_today:
        return bound(), bound((-1, _DAY), (count, code))
    return bound((1, _DAY)), bound((count, code))


def _span_node(bound: _Bound) -> dict[str, Any]:
    steps, span = bound
    inner: dict[str, Any] = {"Now": {}}
    for amount, unit in steps:
        inner = {"DateAdd": {"Expression": inner, "Amount": amount, "TimeUnit": unit}}
    return {"DateSpan": {"Expression": inner, "TimeUnit": span}}


def _read_span(node: Any) -> _Bound | None:
    if not isinstance(node, dict) or not isinstance(node.get("DateSpan"), dict):
        return None
    span = node["DateSpan"]
    inner = span.get("Expression")
    steps: list[tuple[int, int]] = []
    while isinstance(inner, dict) and "DateAdd" in inner:
        add = inner["DateAdd"]
        amount, unit = add.get("Amount"), add.get("TimeUnit")
        if not isinstance(amount, int) or not isinstance(unit, int):
            return None
        steps.append((amount, unit))
        inner = add.get("Expression")
    if not (isinstance(inner, dict) and "Now" in inner) or not isinstance(
        span.get("TimeUnit"), int
    ):
        return None
    return tuple(reversed(steps)), int(span["TimeUnit"])


def _relative_words(direction: str, unit: str, count: int, include_today: bool) -> str:
    if direction == "this":
        return _THIS_WORDS[unit]
    words = (
        f"{direction} {count} {_UNIT_WORDS[unit]}"
        if count != 1
        else f"{direction} {_UNIT_WORDS[unit][:-1]}"
    )
    text = f"the {words}"
    return f"{text}, including {_CURRENT_WORDS[unit]}" if include_today else text


def _read_relative(between: dict[str, Any]) -> str | None:
    """The plain-English range of a Between node Studio wrote, or None if it is not ours."""
    lower, upper = _read_span(between.get("LowerBound")), _read_span(between.get("UpperBound"))
    if lower is None or upper is None:
        return None
    # Including the current unit writes one step fewer, so the count is amount or amount + 1.
    seen = {abs(a) for steps in (lower[0], upper[0]) for a, _ in steps}
    amounts = seen | {a + 1 for a in seen} | {1}
    # "This" first: the last 1 calendar month including this one is the same range.
    for direction in ("this", "last", "next"):
        for unit in _UNITS:
            for count in sorted(amounts):
                for include_today in (False, True):
                    if direction == "this" and (count != 1 or include_today):
                        continue
                    if (lower, upper) == _bounds(direction, unit, count, include_today):
                        return _relative_words(direction, unit, count, include_today)
    return None


# --- building a filter --------------------------------------------------------------------


def _check_column(field: _Field, why: str) -> None:
    if field.kind != "column" or field.aggregation is not None:
        raise DocumentError(f"{field.text} is a measure or a total, so it cannot be {why}")


def _categorical(
    spec: CategoricalFilter, field: _Field, aliases: _Aliases
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if field.kind != "column" or field.aggregation is not None:
        raise DocumentError(
            f"{field.text} is a measure or a total; use a condition such as 'is greater than'"
        )
    if field.value_type is None:
        raise DocumentError(
            f"Studio cannot tell what kind of values {field.name} holds, so it cannot filter it"
        )
    rows: list[list[dict[str, Any]]] = []
    seen: set[str] = set()
    for value in spec.values:
        text = "null" if value is None else _literal(field, value)
        if text not in seen:
            seen.add(text)
            rows.append([_literal_node(text)])
    condition: dict[str, Any] = {
        "In": {
            "Expressions": [_field_node(field, aliases.source(field.table))],
            "Values": rows,
        }
    }
    return ({"Condition": _not(condition) if spec.exclude else condition}, aliases.entries())


def _advanced(
    spec: AdvancedFilter, field: _Field, aliases: _Aliases
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    left = _field_node(field, aliases.source(field.table))
    nodes = [_condition_node(c, field, left) for c in spec.conditions]
    if len(nodes) == 2:
        nodes = [{("And" if spec.logic == "and" else "Or"): {"Left": nodes[0], "Right": nodes[1]}}]
    return {"Condition": nodes[0]}, aliases.entries()


def _top_n(
    spec: TopNFilter, field: _Field, ranked_by: _Field, aliases: _Aliases
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    _check_column(field, "ranked as a top or bottom list")
    if ranked_by.kind == "column" and ranked_by.aggregation is None:
        raise DocumentError(
            f"Choose how to total {ranked_by.name} (Sum, Average…) or rank by a measure"
        )
    column = _field_node(field, aliases.source(field.table))
    order = _field_node(ranked_by, aliases.source(ranked_by.table))
    subquery = {
        "Version": 2,
        "From": aliases.entries(),
        "Select": [{**column, "Name": "field"}],
        # Descending (2) keeps the biggest values, ascending (1) the smallest.
        "OrderBy": [{"Direction": 2 if spec.direction == "top" else 1, "Expression": order}],
        "Top": spec.n,
    }
    from_entries = [
        {"Name": "subquery", "Expression": {"Subquery": {"Query": subquery}}, "Type": 2},
        *aliases.entries(),
    ]
    where = {
        "Condition": {
            "In": {
                "Expressions": [column],
                "Table": {"SourceRef": {"Source": "subquery"}},
            }
        }
    }
    return where, from_entries


def _relative(
    spec: RelativeDateFilter, field: _Field, aliases: _Aliases
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    _check_column(field, "filtered by relative date")
    if field.value_type != "datetime":
        raise DocumentError(
            f"{field.name} is not a date column, so it cannot filter by relative date"
        )
    if spec.direction == "this" and not _UNITS[spec.unit][1]:
        raise DocumentError(
            "'This' needs days or a calendar week, month or year, not a run of "
            f"{_UNIT_WORDS[spec.unit]}"
        )
    lower, upper = _bounds(spec.direction, spec.unit, spec.count, spec.include_today)
    between = {
        "Between": {
            "Expression": _field_node(field, aliases.source(field.table)),
            "LowerBound": _span_node(lower),
            "UpperBound": _span_node(upper),
        }
    }
    return {"Condition": between}, aliases.entries()


_FILTER_TYPES = {
    "categorical": "Categorical",
    "advanced": "Advanced",
    "top_n": "TopN",
    "relative_date": "RelativeDate",
}


def build_filter(
    spec: CategoricalFilter | AdvancedFilter | TopNFilter | RelativeDateFilter,
    model: FilterModel,
    existing_names: set[str],
    *,
    locked: bool = False,
    hidden: bool = False,
    name: str | None = None,
    name_factory: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """The ``filterConfig.filters[i]`` object for ``spec``.

    ``existing_names`` are the filter names already in the report; the new name avoids them
    and is NOT added to the set. ``name`` forces a name (a replaced filter keeps its own).
    ``name_factory`` supplies candidate names, ``Filter`` plus 20 hex by default, so tests can
    seed it. Raises DocumentError, in words a Power BI user follows, for a field that is not in
    the model or a value that does not fit the field's type."""
    field = _resolve(spec.field, model)
    aliases = _Aliases()
    aliases(field.table)
    if isinstance(spec, CategoricalFilter):
        where, from_entries = _categorical(spec, field, aliases)
    elif isinstance(spec, AdvancedFilter):
        where, from_entries = _advanced(spec, field, aliases)
    elif isinstance(spec, TopNFilter):
        ranked_by = _resolve(spec.by, model)
        aliases(ranked_by.table)
        where, from_entries = _top_n(spec, field, ranked_by, aliases)
    else:
        where, from_entries = _relative(spec, field, aliases)
    built: dict[str, Any] = {
        "name": name or _new_name(existing_names, name_factory or _default_name),
        "field": _field_node(field, {"Entity": field.table}),
        "type": _FILTER_TYPES[spec.kind],
        "filter": {"Version": 2, "From": from_entries, "Where": [where]},
        "howCreated": "User",
    }
    if locked:
        built["isLockedInViewMode"] = True
    if hidden:
        built["isHiddenInViewMode"] = True
    return built


def spec_variants(
    spec: CategoricalFilter | AdvancedFilter | TopNFilter | RelativeDateFilter,
    model: FilterModel,
    *,
    locked: bool = False,
    hidden: bool = False,
) -> list[str]:
    """The ``VERIFIED_VARIANTS`` keys this spec relies on."""
    keys: list[str] = []
    if isinstance(spec, CategoricalFilter):
        keys.append("categorical_not_in" if spec.exclude else "categorical_in")
    elif isinstance(spec, AdvancedFilter):
        field = _resolve(spec.field, model)
        if field.aggregation:
            keys.append("advanced_aggregated_field")
        if len(spec.conditions) == 2:
            keys.append(f"advanced_{spec.logic}")
        for condition in spec.conditions:
            operator = condition.operator
            if operator in _TEXT_OPERATORS:
                keys.append("advanced_text")
            elif operator in {"is_blank", "is_not_blank"}:
                keys.append("advanced_blank")
            elif field.value_type == "datetime":
                keys.append("advanced_date_comparison")
            else:
                keys.append(f"advanced_comparison_{field.kind}")
            if operator in {"is_not", "not_contains", "not_starts_with", "is_not_blank"}:
                keys.append("advanced_not")
    elif isinstance(spec, TopNFilter):
        keys.append("top_n" if spec.direction == "top" else "bottom_n")
    else:
        if spec.unit == "days":
            keys.append("relative_date_days")
        else:
            keys.append(
                "relative_date_calendar" if _UNITS[spec.unit][1] else "relative_date_rolling"
            )
    if locked:
        keys.append("locked_in_view_mode")
    if hidden:
        keys.append("hidden_in_view_mode")
    return list(dict.fromkeys(keys))


# --- reading filters back -----------------------------------------------------------------

_COMPARISON_WORDS = {
    0: "is",
    1: "is greater than",
    2: "is greater than or equal to",
    3: "is less than",
    4: "is less than or equal to",
}
_NUMBER = re.compile(r"(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)[LDMd]")
_DATETIME = re.compile(r"datetime'(\d{4}-\d{2}-\d{2})T([\d:]+)(?:\.\d+)?'")
_MAX_SHOWN = 6


def _value_text(value: str) -> str:
    if value == "null":
        return "(Blank)"
    if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    stamp = _DATETIME.fullmatch(value)
    if stamp:
        return (
            stamp.group(1) if stamp.group(2) == "00:00:00" else f"{stamp.group(1)} {stamp.group(2)}"
        )
    number = _NUMBER.fullmatch(value)
    if number:
        text = number.group(1)
        return text[:-2] if text.endswith(".0") else text
    return value


def _literal_text(node: Any) -> str | None:
    value = node.get("Literal", {}).get("Value") if isinstance(node, dict) else None
    return _value_text(value) if isinstance(value, str) else None


def _field_text(field: Any) -> str:
    if not isinstance(field, dict):
        return "a field"
    if "Aggregation" in field:
        inner = field["Aggregation"]
        column = inner.get("Expression", {}).get("Column", {}).get("Property", "a field")
        return f"{_AGGREGATION_WORDS.get(inner.get('Function'), 'Total of')} {column}"
    for kind in ("Column", "Measure"):
        if kind in field:
            return str(field[kind].get("Property", "a field"))
    level = field.get("HierarchyLevel")
    if isinstance(level, dict) and isinstance(level.get("Level"), str):
        return str(level["Level"])
    return "a field"


def _list_text(values: list[str]) -> str:
    if len(values) > _MAX_SHOWN:
        return ", ".join(values[:_MAX_SHOWN]) + f" and {len(values) - _MAX_SHOWN} more"
    return ", ".join(values)


def _is_blank(comparison: dict[str, Any]) -> bool:
    right = comparison.get("Right", {}).get("Literal", {}).get("Value")
    return comparison.get("ComparisonKind") == 0 and right == "null"


def _condition_text(node: Any) -> str | None:
    """What a condition says about its field ("is one of A, B"), or None if unreadable."""
    if not isinstance(node, dict):
        return None
    if "In" in node:
        rows = node["In"].get("Values", [])
        values = [
            text for row in rows if len(row) == 1 and (text := _literal_text(row[0])) is not None
        ]
        if not values or len(values) != len(rows):
            return None
        return f"is {values[0]}" if len(values) == 1 else f"is one of {_list_text(values)}"
    if "Comparison" in node:
        comparison = node["Comparison"]
        if _is_blank(comparison):
            return "is blank"
        word, value = (
            _COMPARISON_WORDS.get(comparison.get("ComparisonKind")),
            _literal_text(comparison.get("Right")),
        )
        return f"{word} {value}" if word and value is not None else None
    for name, positive in (("Contains", "contains"), ("StartsWith", "starts with")):
        if name in node:
            value = _literal_text(node[name].get("Right"))
            return f"{positive} {value}" if value is not None else None
    if "Not" in node:
        return _negated_text(node["Not"].get("Expression"))
    for name, word in (("And", "and"), ("Or", "or")):
        if name in node:
            left, right = (
                _condition_text(node[name].get("Left")),
                _condition_text(node[name].get("Right")),
            )
            return f"{left} {word} {right}" if left and right else None
    return None


def _negated_text(inner: Any) -> str | None:
    if not isinstance(inner, dict):
        return None
    if "Comparison" in inner:
        comparison = inner["Comparison"]
        if _is_blank(comparison):
            return "is not blank"
        word = _COMPARISON_WORDS.get(comparison.get("ComparisonKind"))
        if word:
            value = _literal_text(comparison.get("Right"))
            return f"is not{word[2:]} {value}" if value is not None else None
    if "In" in inner:
        text = _condition_text(inner)
        return text.replace("is ", "is not ", 1) if text else None
    for name, word in (("Contains", "does not contain"), ("StartsWith", "does not start with")):
        if name in inner:
            value = _literal_text(inner[name].get("Right"))
            return f"{word} {value}" if value is not None else None
    return None


def _top_n_text(filter_obj: dict[str, Any], field: str) -> str | None:
    try:
        query = filter_obj["filter"]["From"][0]["Expression"]["Subquery"]["Query"]
        order = query["OrderBy"][0]
        word = "Top" if order["Direction"] == 2 else "Bottom"
        return f"{word} {query['Top']} {field} by {_field_text(order['Expression'])}"
    except (KeyError, IndexError, TypeError):
        return None


def describe_filter(filter_obj: dict[str, Any]) -> str:
    """One filter as plain English ("Country is one of Canada, USA")."""
    field = _field_text(filter_obj.get("field"))
    kind = filter_obj.get("type")
    raw_body = filter_obj.get("filter")
    body: dict[str, Any] = raw_body if isinstance(raw_body, dict) else {}
    conditions = [w.get("Condition") for w in body.get("Where", []) if isinstance(w, dict)]
    text: str | None = None
    if kind == "TopN":
        text = _top_n_text(filter_obj, field)
    elif kind == "RelativeDate" and len(conditions) == 1 and isinstance(conditions[0], dict):
        between = conditions[0].get("Between")
        words = _read_relative(between) if isinstance(between, dict) else None
        text = f"{field} is in a relative date range"
        if words:
            text = f"{field} is {words}" if words == "today" else f"{field} is in {words}"
    elif conditions:
        parts = [_condition_text(c) for c in conditions]
        if all(parts):
            text = f"{field} " + " and ".join(p for p in parts if p)
    if text is None and not isinstance(raw_body, dict):
        # Desktop's filter card for a field nobody has picked values for yet.
        text = f"{field} (no values selected)"
    if text is None:
        label = str(kind or "custom")
        article = "an" if label[:1].lower() in "aeiou" else "a"
        text = f"{field} has {article} {label} filter that Studio cannot describe"
    flags = [
        w
        for key, w in (("isLockedInViewMode", "locked"), ("isHiddenInViewMode", "hidden"))
        if filter_obj.get(key)
    ]
    return f"{text} ({', '.join(flags)})" if flags else text


def summarize_filters(scope_dict: dict[str, Any]) -> list[str]:
    """The filters of a report, page or visual definition, one line each."""
    config = scope_dict.get("filterConfig")
    filters = config.get("filters") if isinstance(config, dict) else None
    if not isinstance(filters, list):
        return []
    return [describe_filter(f) for f in filters if isinstance(f, dict)]


# --- what the local canvas can draw -------------------------------------------------------

_LOCAL_LITERAL = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?[LDd]?")
_MAX_DEPTH = 12  # the local compiler's nesting limit (live_report.Compiler.condition)
_MAX_IN_VALUES = 100  # ...and the most values it compiles into one IN list


def _unsupported_condition(node: Any, depth: int = 0) -> str | None:
    """Mirror of live_report.Compiler.condition: what it cannot compile, in plain words."""
    if depth > _MAX_DEPTH:
        return "This filter is too deeply nested for Studio's canvas"
    if not isinstance(node, dict):
        return "Studio's canvas cannot read this filter"
    for name in ("And", "Or"):
        if name in node:
            pair = node[name]
            return _unsupported_condition(pair.get("Left"), depth + 1) or _unsupported_condition(
                pair.get("Right"), depth + 1
            )
    if "Not" in node:
        return _unsupported_condition(node["Not"].get("Expression"), depth + 1)
    if "Comparison" in node:
        comparison = node["Comparison"]
        return (
            _unsupported_field(comparison.get("Left"))
            or _unsupported_literal(comparison.get("Right"))
            or (
                None
                if comparison.get("ComparisonKind") in _COMPARISON_WORDS
                else "Studio's canvas cannot apply this comparison"
            )
        )
    if "In" in node:
        item = node["In"]
        if "Table" in item or "Values" not in item:
            return "A top or bottom N filter is drawn by Power BI, not by Studio's canvas"
        expressions = item.get("Expressions", [])
        if len(expressions) != 1:
            return "A filter on several fields at once needs Power BI"
        if len(item["Values"]) > _MAX_IN_VALUES:
            return "A filter with more than 100 values is drawn by Power BI, not by Studio's canvas"
        for row in item["Values"]:
            if len(row) != 1:
                return "A filter on several fields at once needs Power BI"
            reason = _unsupported_literal(row[0])
            if reason:
                return reason
        return _unsupported_field(expressions[0])
    if "Between" in node:
        return "A relative date filter is drawn by Power BI, not by Studio's canvas"
    if "Contains" in node or "StartsWith" in node:
        return "A 'contains' or 'starts with' filter is drawn by Power BI, not by Studio's canvas"
    return "Studio's canvas cannot apply this kind of filter"


def _unsupported_field(field: Any) -> str | None:
    if isinstance(field, dict) and ("Column" in field or "Measure" in field):
        return None
    return "A filter on a total or a hierarchy is drawn by Power BI, not by Studio's canvas"


def _unsupported_literal(node: Any) -> str | None:
    value = node.get("Literal", {}).get("Value") if isinstance(node, dict) else None
    if not isinstance(value, str):
        return "Studio's canvas cannot compare with this value"
    if value.startswith("'") and value.endswith("'"):
        return None
    if value.casefold() in {"true", "false", "null"} or _LOCAL_LITERAL.fullmatch(value):
        return None
    return "A filter on dates or fixed decimals is drawn by Power BI, not by Studio's canvas"


def filter_unsupported_reason(filter_obj: dict[str, Any]) -> str | None:
    """None when Studio's canvas (the local DAX engine) can evaluate the filter, else why the
    visuals it covers need Power BI. Kept in step with ``live_report.Compiler``."""
    body = filter_obj.get("filter")
    if not isinstance(body, dict):
        return None
    if filter_obj.get("type") == "TopN":
        return "A top or bottom N filter is drawn by Power BI, not by Studio's canvas"
    if filter_obj.get("type") == "RelativeDate":
        return "A relative date filter is drawn by Power BI, not by Studio's canvas"
    for where in body.get("Where", []):
        reason = _unsupported_condition(where.get("Condition") if isinstance(where, dict) else None)
        if reason:
            return reason
    return None


# --- applying operations ------------------------------------------------------------------

_NOT_FILTERABLE = frozenset({"textbox", "image", "shape", "basicShape", "actionButton"})


_REBUILT_KEYS = frozenset({"name", "field", "type", "filter", "howCreated"})


@dataclass
class _Scope:
    holder: dict[str, Any]
    label: str
    touch: Callable[[], None]


def _visual_label(visual: dict[str, Any], visual_id: str) -> str:
    body = visual.get("visual", {})
    for entry in body.get("visualContainerObjects", {}).get("title", []):
        text = expr.decode(entry.get("properties", {}).get("text"))
        if isinstance(text, str) and text.strip():
            return f"'{text.strip()}'"
    visual_type = body.get("visualType", "visual")
    for role in body.get("query", {}).get("queryState", {}).values():
        for projection in role.get("projections", []):
            first = projection.get("nativeQueryRef") or projection.get("queryRef")
            if first:
                return f"the {visual_type} of {first}"
    return f"the {visual_type} {visual_id}"


def _scope_of(document: PbirDocument, op: Any) -> _Scope:
    if isinstance(op, SetPageFilter) or (isinstance(op, ClearFilter) and op.scope == "page"):
        key = op.page if isinstance(op, SetPageFilter) else op.target
        if not key:
            raise DocumentError("Say which page to clear the filter from")
        page = document.page(key)
        return _Scope(page.data, f"the {page.display_name} page", lambda: document.touch_page(page))
    if isinstance(op, SetVisualFilter) or (isinstance(op, ClearFilter) and op.scope == "visual"):
        key = op.visual if isinstance(op, SetVisualFilter) else op.target
        if not key:
            raise DocumentError("Say which visual to clear the filter from")
        page, visual = document.visual(key)
        if "visual" not in visual:
            raise DocumentError("Visual groups hold other visuals; filter the visuals inside them")
        if visual["visual"].get("visualType") in _NOT_FILTERABLE:
            raise DocumentError("Text boxes, images and shapes cannot be filtered")
        return _Scope(visual, _visual_label(visual, key), lambda: document.touch_visual(page, key))
    return _Scope(document.report, "the whole report", document.touch_report)


def _field_key(field: Any) -> tuple[str, str, str, int | None] | None:
    """Identity of a filter's field: (kind, table, name, aggregation code), case-folded."""
    if not isinstance(field, dict):
        return None
    code: int | None = None
    if "Aggregation" in field:
        code = field["Aggregation"].get("Function")
        field = field["Aggregation"].get("Expression", {})
    for kind, label in (("Column", "column"), ("Measure", "measure")):
        item = field.get(kind) if isinstance(field, dict) else None
        if isinstance(item, dict):
            entity = item.get("Expression", {}).get("SourceRef", {}).get("Entity")
            if isinstance(entity, str) and isinstance(item.get("Property"), str):
                return label, entity.casefold(), item["Property"].casefold(), code
    return None


def _key_of(field: _Field) -> tuple[str, str, str, int | None]:
    code = _AGGREGATION_CODES[field.aggregation] if field.aggregation else None
    return field.kind, field.table.casefold(), field.name.casefold(), code


def _filters(holder: dict[str, Any], create: bool) -> list[dict[str, Any]]:
    # A key that is present but null (or of the wrong type) must not reach setdefault, which
    # would hand that value back.
    config = holder.get("filterConfig")
    if config is None:
        if not create:
            return []
        config = holder["filterConfig"] = {}
    elif not isinstance(config, dict):
        raise DocumentError("The filters saved in this report are not in a form Studio can edit")
    filters = config.get("filters")
    if filters is None:
        if not create:
            return []
        filters = config["filters"] = []
    elif not isinstance(filters, list):
        raise DocumentError("The filters saved in this report are not in a form Studio can edit")
    result: list[dict[str, Any]] = filters
    return result


def _tidy(holder: dict[str, Any]) -> None:
    """Drop an emptied filterConfig, as Desktop does, but keep any other key it holds."""
    config = holder.get("filterConfig")
    if isinstance(config, dict) and not config.get("filters"):
        config.pop("filters", None)
        if not config:
            holder.pop("filterConfig")


@dataclass(frozen=True)
class FilterApplied:
    """What ``apply_filter`` did: the summary line and, for a set operation, the filter object
    written (``None`` for a clear)."""

    summary: str
    filter: dict[str, Any] | None = None


def _set_filter(
    document: PbirDocument,
    op: SetPageFilter | SetVisualFilter | SetReportFilter,
    model: FilterModel,
    names: set[str],
    name_factory: Callable[[], str] | None,
) -> FilterApplied:
    scope = _scope_of(document, op)
    built = build_filter(
        op.filter,
        model,
        set(names),
        locked=op.locked,
        hidden=op.hidden,
        name_factory=name_factory,
    )
    filters = _filters(scope.holder, create=True)
    key = _field_key(built["field"])
    for index, existing in enumerate(filters):
        if isinstance(existing, dict) and _field_key(existing.get("field")) == key:
            if isinstance(existing.get("name"), str) and existing["name"]:
                built["name"] = existing["name"]
            else:
                names.add(built["name"])
            # The card's title, order and formatting (and a lock the op does not ask for) belong
            # to the old filter, not to what it selects; rebuilding must not lose them.
            for extra, value in existing.items():
                if extra not in _REBUILT_KEYS and extra not in built:
                    built[extra] = value
            filters[index] = built
            verb = "Replaced"
            break
    else:
        names.add(built["name"])
        filters.append(built)
        verb = "Added"
    scope.touch()
    return FilterApplied(f"{verb} a filter on {scope.label}: {describe_filter(built)}", built)


def _clear(document: PbirDocument, op: ClearFilter, model: FilterModel) -> str:
    scope = _scope_of(document, op)
    filters = _filters(scope.holder, create=False)
    if op.field is None:
        if not filters:
            raise DocumentError(f"There are no filters on {scope.label}")
        count = len(filters)
        filters.clear()
        summary = (
            f"Removed {'the filter' if count == 1 else f'all {count} filters'} from {scope.label}"
        )
    else:
        field = _resolve(op.field, model)
        key = _key_of(field)
        kept = [
            f for f in filters if _field_key(f.get("field") if isinstance(f, dict) else None) != key
        ]
        if len(kept) == len(filters):
            raise DocumentError(f"There is no filter on {field.text} on {scope.label}")
        filters[:] = kept
        summary = f"Removed the filter on {field.text} from {scope.label}"
    _tidy(scope.holder)
    scope.touch()
    return summary


def apply_filter(
    document: PbirDocument,
    op: Any,
    model: FilterModel,
    all_filter_names: set[str] | None = None,
    *,
    name_factory: Callable[[], str] | None = None,
) -> FilterApplied:
    """``apply_filter_operation`` that also hands back the filter object it wrote, for callers
    that check it (``filter_unsupported_reason``)."""
    if isinstance(op, ClearFilter):
        return FilterApplied(_clear(document, op, model))
    if isinstance(op, SetPageFilter | SetVisualFilter | SetReportFilter):
        names = collect_filter_names(document) if all_filter_names is None else all_filter_names
        return _set_filter(document, op, model, names, name_factory)
    raise DocumentError(f"{type(op).__name__} is not a filter operation")


def apply_filter_operation(
    document: PbirDocument,
    op: Any,
    model: FilterModel,
    all_filter_names: set[str] | None = None,
    *,
    name_factory: Callable[[], str] | None = None,
) -> str:
    """Apply one filter operation; returns a plain-English summary.

    A filter on a field that already has one at the same scope replaces it and keeps its
    name (a bookmark may refer to it). ``all_filter_names`` is the set of names in use across
    the report; it is updated with a new name so a batch stays unique. When omitted it is read
    from the document. Raises DocumentError; the document is unchanged when it does."""
    return apply_filter(document, op, model, all_filter_names, name_factory=name_factory).summary
