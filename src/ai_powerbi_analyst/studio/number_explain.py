"""Why a number on an imported report is what it is: one card value, or one point of a chart
or table, investigated the way a Power BI developer would, with every figure from the engine.

Asked "why does this card show 46.76%?", a developer opens the measure, evaluates what it
uses in the same filter context (DAX query view), checks which slicers and filters reach the
visual and which relationships carry them, and looks at why a bar has its colour. The local 4B
model cannot: MAN-08 had it call the same card "employees whose headcount increased", with no
number. Here nothing is estimated:

- the value and every step come from one query of the report's engine (``report_engine``
  locally, or Power BI Desktop) built exactly like the visual's own query (``live_report``'s
  filter compilation and grouping), so each step is evaluated in the point's filter context;
- a measure the formula works out for every row of an iterator, or inside CALCULATE with its
  own filters, has no single value in that context: it is described, never given a number;
- the meaning of each part comes from the formula (``dax_describe``), the colour from the
  render rules and the visual's formatting tokens;
- a part the engine cannot compute is named with the engine's reason, and the rest is kept.

The measure's DAX is returned for the UI to show locally; ``text`` (what chat stores) carries
plain descriptions, values and at most short condition snippets, never a whole formula.
"""

from __future__ import annotations

import colorsys
import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from . import live_report as lr
from . import render_navigation as nav
from .card_answers import card_label, card_period, period_probes
from .dax_describe import Describer, describe
from .dax_describe import describer as new_describer
from .dax_engine import DaxUnsupported, Model
from .dax_format import format_value
from .dax_syntax import (
    Binary,
    Call,
    ColRef,
    Name,
    Node,
    Num,
    Str,
    TableCtor,
    Unary,
    VarBlock,
    parse_expression,
    to_dax,
)
from .native_report import NativeReportPreview
from .render_rules import Rule, holds
from .report_engine import ENGINE_LABEL, LocalReportEngine
from .report_filters import describe_filter
from .report_import import ImportedReport, ImportedVisual, report_relationships
from .report_props import expand_hex

Json = dict[str, Any]
Value = str | int | float | bool | datetime | None
Engine = LocalReportEngine | NativeReportPreview
StepKind = Literal["variable", "measure", "condition", "part", "rows"]
Usage = Literal["direct", "per_row", "shifted"]
FilterSource = Literal[
    "report", "page", "visual", "slicer", "cross_filter", "drill", "bookmark", "point", "other"
]
ColourRule = Literal["fixed", "series", "default", "theme", "fieldValue", "gradient", "rules"]
Focus = Literal["value", "colour", "blank"]

# How far the investigation follows the measures a measure calls with its own context: the
# measure (0), what it calls (1), what those call (2). Deeper parts are described, not valued.
MAX_DEPTH = 2
# Evaluated parts per explanation: one query computes them all, and the text stays readable.
MAX_STEPS = 24
# IF/SWITCH levels followed down one formula (IF(a, IF(b, ...)) chains).
MAX_BRANCH_LEVELS = 4
# Seconds spent retrying parts one at a time after the combined query failed.
RETRY_BUDGET = 40.0
# A condition with no plain wording is shown as DAX up to this many characters.
SNIPPET = 120

_ENGINE_ERRORS = (ValueError, ArithmeticError, LookupError, TypeError)
_USAGE_RANK = {"direct": 0, "shifted": 1, "per_row": 2}
# Functions whose arguments from this index on are evaluated once per row of the first.
_ROW_ITERATORS = {
    "FILTER": 1,
    "SUMX": 1,
    "AVERAGEX": 1,
    "MINX": 1,
    "MAXX": 1,
    "COUNTX": 1,
    "COUNTAX": 1,
    "PRODUCTX": 1,
    "CONCATENATEX": 1,
    "RANKX": 1,
    "MEDIANX": 1,
    "ADDCOLUMNS": 1,
    "SELECTCOLUMNS": 1,
    "GENERATE": 1,
    "GENERATEALL": 1,
    "TOPN": 2,
}
# Functions whose first argument is evaluated with filters of their own.
_SHIFTERS = frozenset({"CALCULATE", "CALCULATETABLE", "TOTALMTD", "TOTALQTD", "TOTALYTD"})
# Functions that return a table: a variable holding one has no single value to show.
_TABLE_FUNCTIONS = frozenset(
    {
        "FILTER", "ALL", "ALLSELECTED", "ALLEXCEPT", "ALLNOBLANKROW", "VALUES", "DISTINCT",
        "TOPN", "CALCULATETABLE", "SUMMARIZE", "SUMMARIZECOLUMNS", "ADDCOLUMNS",
        "SELECTCOLUMNS", "GENERATESERIES", "CALENDAR", "CALENDARAUTO", "DATESMTD", "DATESQTD",
        "DATESYTD", "DATEADD", "DATESINPERIOD", "DATESBETWEEN", "PARALLELPERIOD",
        "SAMEPERIODLASTYEAR", "UNION", "INTERSECT", "EXCEPT", "CROSSJOIN", "TREATAS",
        "KEEPFILTERS", "REMOVEFILTERS", "RELATEDTABLE", "DATATABLE", "ROW", "GROUPBY",
        "GENERATE", "GENERATEALL", "NATURALINNERJOIN", "NATURALLEFTOUTERJOIN", "FIRSTDATE",
        "LASTDATE", "PREVIOUSMONTH", "PREVIOUSYEAR", "PREVIOUSQUARTER", "NEXTMONTH",
        "NEXTYEAR", "NEXTQUARTER", "SUBSTITUTEWITHINDEX",
    }
)  # fmt: skip
# Measures that read these can take filters no relationship carries: whether a filter reaches
# them is then unknown, never "does not reach".
_VIRTUAL_JOINS = frozenset({"TREATAS", "USERELATIONSHIP", "CROSSFILTER", "INTERSECT"})
_AGGREGATES = frozenset(
    {"SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "DISTINCTCOUNT", "COUNTBLANK"}
)
_TABLE_AGGREGATES = frozenset({"COUNTROWS", "SUMX", "AVERAGEX", "MINX", "MAXX", "COUNTX"})
_INVISIBLE = re.compile(r"[​-‏⁠﻿ ]+")
_MARKS = {
    "column": "bar",
    "bar": "bar",
    "line": "point",
    "area": "point",
    "pie": "slice",
    "donut": "slice",
    "table": "row",
}


# --- results -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Point:
    """One data point of a chart or one row of a table: the values of its category columns by
    the column's ``query_ref`` (``Table.Column``, as the render payload's columns name them),
    and the value column asked about (a measure's ``query_ref`` or name; None: the visual's
    first shown measure). A card has no point."""

    values: Mapping[str, Value] = field(default_factory=dict)
    measure: str | None = None

    @classmethod
    def of(cls, raw: Point | Mapping[str, Any] | None) -> Point | None:
        """A point from the API's ``{"values": {...}, "measure": ...}`` (None stays None)."""
        if raw is None or isinstance(raw, Point):
            return raw
        values = raw.get("values")
        measure = raw.get("measure")
        if values is not None and not isinstance(values, Mapping):
            raise ValueError("A point's values must map a column to its value")
        cleaned: dict[str, Value] = {}
        for key, value in (values or {}).items():
            if not isinstance(key, str) or not isinstance(value, str | int | float | bool | None):
                raise ValueError("A point's values must be text, numbers, true/false or null")
            cleaned[key] = value
        if measure is not None and not isinstance(measure, str):
            raise ValueError("A point's measure must be the name of a value column")
        return cls(cleaned, measure)


@dataclass(frozen=True)
class Step:
    """One part of the formula: a variable, a measure it calls, a condition it tests, a part of
    a ratio or a count of the rows it iterates. ``value`` is the engine's, in the point's
    filter context; a measure used per row or with CALCULATE's own filters has none
    (``usage``), and a part the engine refused has ``computed`` False and ``reason``."""

    name: str
    kind: StepKind
    owner: str
    formula: str
    meaning: str | None
    value: Value
    formatted: str
    computed: bool
    usage: Usage
    depth: int
    reason: str | None = None


@dataclass(frozen=True)
class FilterUse:
    """A filter in effect on the point: where it comes from, the field and condition in plain
    words, and whether it can change this number (``reaches``; None when Studio cannot tell)
    and how. A filter on a measure (``on_measure``) decides which rows a visual shows."""

    source: FilterSource
    field: str
    condition: str
    origin: str | None
    on_measure: bool
    reaches: bool | None
    how: str


@dataclass(frozen=True)
class JoinPath:
    """Relationships a filter or grouping travels along to a table the measure reads."""

    tables: tuple[str, ...]
    joins: tuple[str, ...]
    why: str


@dataclass(frozen=True)
class ColourReason:
    """Why the point has its colour: the rule, the colour, its plain name and a sentence."""

    target: str
    color: str | None
    name: str | None
    rule: ColourRule
    why: str
    measure: str | None = None


@dataclass(frozen=True)
class Explanation:
    page_id: str
    visual_id: str
    visual: str
    kind: str
    point: Mapping[str, Value]
    measure: str | None
    value: Value
    formatted: str | None
    computed: bool
    formula: str | None
    meaning: str | None
    period: str | None
    steps: tuple[Step, ...]
    filters: tuple[FilterUse, ...]
    relationships: tuple[JoinPath, ...]
    formatting: ColourReason | None
    blank: str | None
    notes: tuple[str, ...]
    engine: str
    text: str

    def to_dict(self) -> Json:
        """JSON-safe payload for the API (dates as ISO text)."""
        found: Json = _jsonable(asdict(self))
        return found


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


# --- values --------------------------------------------------------------------------------


def _cell(value: object) -> Value:
    if value is None or isinstance(value, bool | str | datetime):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    item = getattr(value, "item", None)
    if callable(item):
        return _cell(item())
    return str(value)


def _clean(text: str) -> str:
    return " ".join(_INVISIBLE.sub(" ", text).split())


def _general(value: float) -> str:
    if float(value).is_integer() and abs(value) < 1e15:
        return f"{int(value):,}"
    if abs(value) >= 100:
        return f"{value:,.2f}"
    return f"{value:.4g}"


def _show(value: Value, fmt: str = "") -> str:
    """A value as the text an explanation shows: the format string when there is one (Studio's
    own FORMAT, the engine's), plain otherwise."""
    if value is None:
        return "blank"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return f"{value.day} {value:%b %Y}"
    if isinstance(value, int | float):
        if fmt:
            try:
                return _clean(format_value(value, fmt))
            except (ValueError, ArithmeticError, IndexError, KeyError):
                pass
        return _general(float(value))
    text = _clean(value)
    if text.startswith("data:image/svg"):
        return "an SVG image"
    return f'"{text[:80]}"'


def _truthy(value: Value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    return bool(value)


def _same(a: object, b: object) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)
    return a == b


def _reason(error: BaseException) -> str:
    text = " ".join(str(error).split()) or type(error).__name__
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    first = first if len(first) <= 200 else first[:197] + "..."
    if isinstance(error, DaxUnsupported | lr.Unsupported):
        return first
    return f"Studio could not calculate it ({first})"


def _snippet(node: Node) -> str:
    text = to_dax(node)
    return "`" + (text if len(text) <= SNIPPET else text[: SNIPPET - 3] + "...") + "`"


def _names(node: Node) -> set[str]:
    """Every identifier (variable or table name) in a tree, casefolded."""
    found: set[str] = set()
    stack: list[Node] = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, Name):
            found.add(n.name.casefold())
        elif isinstance(n, Call):
            stack.extend(n.args)
        elif isinstance(n, Binary):
            stack.extend((n.left, n.right))
        elif isinstance(n, Unary):
            stack.append(n.operand)
        elif isinstance(n, TableCtor):
            stack.extend(v for row in n.rows for v in row)
        elif isinstance(n, VarBlock):
            stack.extend(v for _, v in n.variables)
            stack.append(n.body)
    return found


def _nodes(node: Node) -> list[Node]:
    found: list[Node] = []
    stack: list[Node] = [node]
    while stack:
        n = stack.pop()
        found.append(n)
        if isinstance(n, Call):
            stack.extend(n.args)
        elif isinstance(n, Binary):
            stack.extend((n.left, n.right))
        elif isinstance(n, Unary):
            stack.append(n.operand)
        elif isinstance(n, TableCtor):
            stack.extend(v for row in n.rows for v in row)
        elif isinstance(n, VarBlock):
            stack.extend(v for _, v in n.variables)
            stack.append(n.body)
    return found


Variables = tuple[tuple[str, Node], ...]


def _wrap(variables: Variables, expression: Node) -> str:
    """DAX for ``expression`` with the variables it needs (and only those) defined before it:
    variables of a measure are evaluated where the measure is, so the result is the value the
    formula works with."""
    by_name = {name.casefold(): value for name, value in variables}
    needed: set[str] = set()
    stack = list(_names(expression))
    while stack:
        key = stack.pop()
        if key in by_name and key not in needed:
            needed.add(key)
            stack.extend(_names(by_name[key]))
    kept = tuple((name, value) for name, value in variables if name.casefold() in needed)
    return to_dax(VarBlock(kept, expression)) if kept else to_dax(expression)


def _resolve(node: Node, variables: Variables) -> Node:
    by_name = {name.casefold(): value for name, value in variables}
    seen: set[str] = set()
    while isinstance(node, Name) and node.name.casefold() in by_name:
        if node.name.casefold() in seen:
            break
        seen.add(node.name.casefold())
        node = by_name[node.name.casefold()]
    return node


# --- the formula ---------------------------------------------------------------------------


@dataclass
class _Spec:
    key: str
    name: str
    kind: StepKind
    owner: str
    dax: str
    formula: str
    meaning: str | None
    fmt: str
    usage: Usage
    depth: int
    note: str | None = None
    hidden: bool = False
    # A variable the measure can return, and the measures its value comes from: it takes the
    # measure's format only when it is the same kind of value as the one returned here.
    result: bool = False
    leaves: frozenset[str] = frozenset()


@dataclass
class _Described:
    """A measure the formula calls in another context: described, not valued."""

    name: str
    owner: str
    usage: Usage
    where: str
    depth: int


class _Planner:
    """The parts of a measure (and of the measures it calls in the same context) to evaluate,
    each as DAX for the point's context."""

    def __init__(
        self,
        measures: Mapping[str, str],
        columns: set[tuple[str, str]],
        formats: Mapping[str, str],
    ) -> None:
        self.measures = dict(measures)
        self.by_key = {name.casefold(): name for name in measures}
        self.columns = columns
        self.formats = formats
        self.specs: list[_Spec] = []
        self.described: list[_Described] = []
        self.conditions: dict[tuple[str, int], _Spec] = {}
        self.variables: dict[tuple[str, str], _Spec] = {}
        self.parts: dict[tuple[str, int], _Spec] = {}
        self.by_measure: dict[str, _Spec] = {}
        self.probes: dict[tuple[str, str], _Spec] = {}
        self.trees: dict[str, Node | None] = {}
        self.planned: set[str] = set()

    # -- reading --------------------------------------------------------------------------

    def tree(self, name: str) -> Node | None:
        if name not in self.trees:
            try:
                self.trees[name] = parse_expression(self.measures.get(name, ""))
            except ValueError:
                self.trees[name] = None
        return self.trees[name]

    def parts_of(self, name: str) -> tuple[Variables, Node] | None:
        node = self.tree(name)
        if node is None:
            return None
        if isinstance(node, VarBlock):
            return node.variables, node.body
        return (), node

    def measure_of(self, ref: ColRef) -> str | None:
        if ref.table is not None and (ref.table.casefold(), ref.column.casefold()) in self.columns:
            return None
        if ref.table == "":
            return None
        return self.by_key.get(ref.column.casefold())

    def describer(self, variables: Variables) -> Describer:
        return new_describer(self.measures, (name for name, _ in variables))

    def words(self, node: Node, variables: Variables) -> str | None:
        try:
            return self.describer(variables).scalar(node)
        except (RecursionError, AttributeError, TypeError, ValueError, IndexError):
            return None

    def fmt(self, name: str) -> str:
        return self.formats.get(name, "")

    # -- planning -------------------------------------------------------------------------

    def add(self, spec: _Spec) -> _Spec | None:
        if len(self.specs) >= MAX_STEPS:
            return None
        spec.key = f"X{len(self.specs)}"
        self.specs.append(spec)
        return spec

    def plan(self, name: str, depth: int = 0) -> None:
        if name in self.planned or depth > MAX_DEPTH:
            return
        self.planned.add(name)
        parts = self.parts_of(name)
        if parts is None:
            return
        variables, body = parts
        results = self.results(body, variables)
        for index, (var, expression) in enumerate(variables):
            if self.is_table(expression, variables[:index]):
                continue
            if isinstance(body, Name) and body.name.casefold() == var.casefold():
                continue  # the variable the measure returns: its value is the measure's
            fmt = self.fmt(name) if var.casefold() in results else ""
            if isinstance(expression, ColRef) and self.measure_of(expression):
                fmt = self.fmt(self.measure_of(expression) or "")
            spec = self.add(
                _Spec(
                    "",
                    var,
                    "variable",
                    name,
                    _wrap(variables, Name(var)),
                    to_dax(expression),
                    self.words(expression, variables[:index]),
                    fmt,
                    "direct",
                    depth,
                )
            )
            if spec is not None:
                spec.result = var.casefold() in results
                spec.leaves = self.leaves(expression, variables[:index])
                self.variables[(name, var.casefold())] = spec
        self.branches(name, body, variables, depth, 0)
        self.probe(name, body, variables, depth)
        references = self.references(name)
        for ref, usage, where in references:
            if usage != "direct":
                self.described.append(_Described(ref, name, usage, where, depth))
                continue
            if ref in self.by_measure:
                continue
            spec = self.add(
                _Spec(
                    "",
                    f"[{ref}]",
                    "measure",
                    name,
                    "[" + ref.replace("]", "]]") + "]",
                    f"[{ref}]",
                    describe(self.measures.get(ref, ""), set(self.measures)),
                    self.fmt(ref),
                    "direct",
                    depth,
                )
            )
            if spec is not None:
                self.by_measure[ref] = spec
        for ref, usage, _ in references:
            if usage == "direct":
                self.plan(ref, depth + 1)

    def is_table(self, expression: Node, earlier: Variables) -> bool:
        if isinstance(expression, TableCtor):
            return True
        if isinstance(expression, Call):
            return expression.name in _TABLE_FUNCTIONS
        if isinstance(expression, Name):
            target = _resolve(expression, earlier)
            return target is not expression and self.is_table(target, earlier)
        return False

    def leaves(self, node: Node, variables: Variables, level: int = 0) -> frozenset[str]:
        """The measures an expression's value comes from, through IF/SWITCH branches and
        CALCULATE: _Avg_MTD_Asc and _Avg_MTD_Desc share theirs, _Result_1 ([MOMTD %]) and
        _Result_2 ([MTD Total Sales]) do not."""
        if level > MAX_BRANCH_LEVELS:
            return frozenset()
        node = _resolve(node, variables)
        if isinstance(node, ColRef):
            ref = self.measure_of(node)
            return frozenset({ref}) if ref else frozenset()
        if isinstance(node, Call) and node.name == "IF":
            return frozenset().union(*(self.leaves(a, variables, level + 1) for a in node.args[1:]))
        if isinstance(node, Call) and node.name == "SWITCH":
            rest = node.args[1:]
            picked = [a for i, a in enumerate(rest) if i % 2 == 1 or i == len(rest) - 1]
            return frozenset().union(*(self.leaves(a, variables, level + 1) for a in picked))
        if isinstance(node, Call) and node.name in _SHIFTERS and node.args:
            return self.leaves(node.args[0], variables, level + 1)
        return frozenset()

    def results(self, body: Node, variables: Variables, level: int = 0) -> set[str]:
        """The variables an IF/SWITCH chain can return (they take the measure's format)."""
        found: set[str] = set()
        if level > MAX_BRANCH_LEVELS:
            return found
        if isinstance(body, Name):
            found.add(body.name.casefold())
            target = _resolve(body, variables)
            if target is not body:
                found |= self.results(target, variables, level + 1)
        elif isinstance(body, Call) and body.name == "IF":
            for branch in body.args[1:]:
                found |= self.results(branch, variables, level + 1)
        elif isinstance(body, Call) and body.name == "SWITCH":
            rest = body.args[1:]
            for index, arg in enumerate(rest):
                if index % 2 == 1 or index == len(rest) - 1:
                    found |= self.results(arg, variables, level + 1)
        return found

    def condition(self, owner: str, node: Node, variables: Variables, depth: int) -> None:
        if (owner, id(node)) in self.conditions:
            return
        spec = self.add(
            _Spec(
                "",
                "condition",
                "condition",
                owner,
                _wrap(variables, node),
                to_dax(node),
                self.words(node, variables),
                "",
                "direct",
                depth,
            )
        )
        if spec is not None:
            self.conditions[(owner, id(node))] = spec

    def part(self, owner: str, node: Node, label: str, variables: Variables, depth: int) -> None:
        if isinstance(node, Num | Str | Name) or (
            isinstance(node, ColRef) and self.measure_of(node)
        ):
            return  # a constant, a variable or a measure: shown as itself
        spec = self.add(
            _Spec(
                "",
                label,
                "part",
                owner,
                _wrap(variables, node),
                to_dax(node),
                self.words(node, variables),
                "",
                "direct",
                depth,
            )
        )
        if spec is not None:
            self.parts[(owner, id(node))] = spec

    def branches(
        self, owner: str, node: Node, variables: Variables, depth: int, level: int
    ) -> None:
        if level > MAX_BRANCH_LEVELS:
            return
        node = _resolve(node, variables)
        if isinstance(node, Call) and node.name == "IF" and len(node.args) in {2, 3}:
            self.condition(owner, node.args[0], variables, depth)
            for branch in node.args[1:]:
                self.branches(owner, branch, variables, depth, level + 1)
        elif isinstance(node, Call) and node.name == "SWITCH" and len(node.args) >= 3:
            subject, rest = node.args[0], node.args[1:]
            cases = [(rest[i], rest[i + 1]) for i in range(0, len(rest) - 1, 2)][:8]
            fallback = rest[-1] if len(rest) % 2 else None
            switch_true = isinstance(subject, Call) and subject.name == "TRUE"
            if not switch_true:
                self.part(owner, subject, "the value SWITCH compares", variables, depth)
            for test, result in cases:
                if switch_true:
                    self.condition(owner, test, variables, depth)
                self.branches(owner, result, variables, depth, level + 1)
            if fallback is not None:
                self.branches(owner, fallback, variables, depth, level + 1)
        elif isinstance(node, Call) and node.name == "DIVIDE" and len(node.args) >= 2:
            self.part(owner, node.args[0], "numerator", variables, depth)
            self.part(owner, node.args[1], "denominator", variables, depth)
        elif isinstance(node, Binary) and node.op in {"/", "-", "+", "*"}:
            self.part(owner, node.left, "left side", variables, depth)
            self.part(owner, node.right, "right side", variables, depth)

    def probe(self, owner: str, body: Node, variables: Variables, depth: int) -> None:
        """Counts of what an iterator goes over, for "137 of the 293 checked" and averages."""
        if depth > 1:
            return
        node = _resolve(body, variables)
        if not isinstance(node, Call) or not node.args:
            return
        table = node.args[0]
        if node.name in {"COUNTX", "COUNTAX", "COUNTROWS"}:
            if isinstance(table, Call) and table.name == "FILTER" and table.args:
                self.add_probe(owner, "checked", Call("COUNTROWS", (table.args[0],)), variables)
        elif node.name == "AVERAGEX" and len(node.args) == 2:
            expression = node.args[1]
            self.add_probe(owner, "rows", Call("COUNTROWS", (table,)), variables)
            kept = Call("FILTER", (table, Unary("NOT", Call("ISBLANK", (expression,)))))
            self.add_probe(owner, "with_value", Call("COUNTROWS", (kept,)), variables)
            self.add_probe(owner, "sum", Call("SUMX", (table, expression)), variables)

    def add_probe(self, owner: str, label: str, node: Node, variables: Variables) -> None:
        fmt = self.fmt(owner) if label == "sum" else ""
        spec = self.add(
            _Spec(
                "",
                label,
                "rows",
                owner,
                _wrap(variables, node),
                to_dax(node),
                None,
                fmt,
                "direct",
                0,
                hidden=True,
            )
        )
        if spec is not None:
            self.probes[(owner, label)] = spec

    def references(self, name: str) -> list[tuple[str, Usage, str]]:
        """The measures a formula calls: (name, how it uses them, where in words)."""
        parts = self.parts_of(name)
        if parts is None:
            return []
        variables, body = parts
        describer = self.describer(variables)
        found: dict[str, tuple[Usage, str]] = {}

        def worse(a: Usage, b: Usage) -> Usage:
            return a if _USAGE_RANK[a] >= _USAGE_RANK[b] else b

        def visit(node: Node, usage: Usage, where: str) -> None:
            if isinstance(node, ColRef):
                ref = self.measure_of(node)
                if ref is not None:
                    old = found.get(ref)
                    if old is None or _USAGE_RANK[usage] < _USAGE_RANK[old[0]]:
                        found[ref] = (usage, where)
                return
            if isinstance(node, Call):
                args = node.args
                if node.name in _ROW_ITERATORS and args:
                    start = _ROW_ITERATORS[node.name]
                    try:
                        rows = describer.rows(args[0])
                    except (RecursionError, AttributeError, TypeError, ValueError):
                        rows = None
                    each = rows[0] if rows else "each row of the table it goes over"
                    inner = where if usage == "per_row" else f"for {each} (inside {node.name})"
                    for index, arg in enumerate(args):
                        if index >= start:
                            visit(arg, worse(usage, "per_row"), inner)
                        else:
                            visit(arg, usage, where)
                    return
                if node.name in _SHIFTERS and args:
                    try:
                        phrases = [describer.filter(a) for a in args[1:]]
                    except (RecursionError, AttributeError, TypeError, ValueError):
                        phrases = [None]
                    if node.name.startswith("TOTAL"):
                        phrases = [node.name[5:].lower() + " to date"]
                    said = ", ".join(p for p in phrases if p) if all(phrases) else ""
                    inner = (
                        where
                        if usage != "direct"
                        else (
                            f"{said} (inside {node.name})"
                            if said
                            else f"with the filters {node.name} adds"
                        )
                    )
                    visit(args[0], worse(usage, "shifted"), inner)
                    for arg in args[1:]:
                        visit(arg, usage, where)
                    return
                for arg in args:
                    visit(arg, usage, where)
            elif isinstance(node, Binary):
                visit(node.left, usage, where)
                visit(node.right, usage, where)
            elif isinstance(node, Unary):
                visit(node.operand, usage, where)
            elif isinstance(node, TableCtor):
                for row in node.rows:
                    for value in row:
                        visit(value, usage, where)

        for _, value in variables:
            visit(value, "direct", "")
        visit(body, "direct", "")
        return [(ref, usage, where) for ref, (usage, where) in found.items()]

    def closure(self, name: str) -> list[str]:
        """The measure and every measure it calls, at any depth."""
        seen = [name]
        stack = [name]
        while stack:
            node = self.tree(stack.pop())
            if node is None:
                continue
            for n in _nodes(node):
                if isinstance(n, ColRef):
                    ref = self.measure_of(n)
                    if ref is not None and ref not in seen:
                        seen.append(ref)
                        stack.append(ref)
        return seen


# --- which tables the measure reads, and what reaches them ---------------------------------


@dataclass(frozen=True)
class _Edge:
    many: str
    many_column: str
    one: str
    one_column: str
    both: bool


class _Graph:
    """The model's active relationships as filter paths (from the one side to the many side,
    and back for a two-way one), from the relationships the engine itself uses."""

    def __init__(self, raw: Sequence[Mapping[str, Any]]) -> None:
        self.up: dict[str, list[_Edge]] = {}
        self.names: dict[str, str] = {}
        for rel in raw:
            if not rel.get("IsActive", 1):
                continue
            ends = [
                (str(rel.get(f"{side}TableName", "")), str(rel.get(f"{side}ColumnName", "")))
                for side in ("From", "To")
            ]
            if str(rel.get("Cardinality", "")).upper() == "1:M":
                ends.reverse()
            (many, many_column), (one, one_column) = ends
            both = str(rel.get("CrossFilteringBehavior", "")).casefold() in {"both", "2"}
            edge = _Edge(many, many_column, one, one_column, both)
            self.names[many.casefold()], self.names[one.casefold()] = many, one
            self.up.setdefault(many.casefold(), []).append(edge)
            if both:
                back = _Edge(one, one_column, many, many_column, True)
                self.up.setdefault(one.casefold(), []).append(back)

    def path(self, start: str, target: str) -> list[_Edge] | None:
        """The relationships a filter on ``target`` crosses to reach ``start``."""
        frontier: list[tuple[str, list[_Edge]]] = [(start.casefold(), [])]
        seen = {start.casefold()}
        while frontier:
            table, path = frontier.pop(0)
            if table == target.casefold():
                return path
            for edge in self.up.get(table, []):
                if edge.one.casefold() not in seen:
                    seen.add(edge.one.casefold())
                    frontier.append((edge.one.casefold(), [*path, edge]))
        return None


def _reads(planner: _Planner, measure: str, tables: set[str]) -> tuple[set[str], set[str], bool]:
    """(tables the measure reads, tables it aggregates, whether it can take filters no
    relationship carries), over every measure it calls. ``tables`` are the model's names."""
    by_key = {t.casefold(): t for t in tables}
    read: set[str] = set()
    aggregated: set[str] = set()
    virtual = False
    for name in planner.closure(measure):
        node = planner.tree(name)
        if node is None:
            continue
        variables = {n.casefold() for n in _names_defined(node)}
        for n in _nodes(node):
            if isinstance(n, ColRef) and n.table and planner.measure_of(n) is None:
                found = by_key.get(n.table.casefold())
                if found:
                    read.add(found)
            elif isinstance(n, Name) and n.name.casefold() not in variables:
                found = by_key.get(n.name.casefold())
                if found:
                    read.add(found)
            elif isinstance(n, Call):
                if n.name in _VIRTUAL_JOINS:
                    virtual = True
                first = n.args[0] if n.args else None
                if n.name in _AGGREGATES and isinstance(first, ColRef) and first.table:
                    found = by_key.get(first.table.casefold())
                    if found:
                        aggregated.add(found)
                elif n.name in _TABLE_AGGREGATES and isinstance(first, Name):
                    found = by_key.get(first.name.casefold())
                    if found:
                        aggregated.add(found)
    return read, aggregated, virtual


def _names_defined(node: Node) -> list[str]:
    return [name for n in _nodes(node) if isinstance(n, VarBlock) for name, _ in n.variables]


# --- filters -------------------------------------------------------------------------------

_PRED_REF = r"'(?P<table>(?:[^']|'')+)'\[(?P<column>(?:[^\]]|\]\])+)\]"
_PRED_IN = re.compile(rf"^{_PRED_REF}\s+IN\s+\{{(?P<values>.*)\}}$", re.S)
_PRED_CMP = re.compile(rf"^{_PRED_REF}\s*(?P<op>>=|<=|=|>|<)\s*(?P<value>.+)$", re.S)
_PRED_VALUE = re.compile(
    r'"(?:[^"]|"")*"|DATE\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)|BLANK\(\)|TRUE\(\)|FALSE\(\)|'
    r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
)
_OP_WORDS = {
    "=": "is",
    ">": "is greater than",
    ">=": "is greater than or equal to",
    "<": "is less than",
    "<=": "is less than or equal to",
}


def _value_words(literal: str) -> str:
    if literal.startswith('"'):
        return literal[1:-1].replace('""', '"')
    if literal in {"BLANK()", "TRUE()", "FALSE()"}:
        return "(Blank)" if literal == "BLANK()" else literal[:-2].lower()
    token = literal
    date_match = re.fullmatch(r"DATE\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", token)
    if date_match:
        year, month, day = (int(g) for g in date_match.groups())
        return f"{year:04d}-{month:02d}-{day:02d}"
    return token[:-2] if token.endswith(".0") else token


def _listed(values: list[str]) -> str:
    if len(values) == 1:
        return f"is {values[0]}"
    shown = ", ".join(values[:6]) + (f" and {len(values) - 6} more" if len(values) > 6 else "")
    return f"is one of {shown}"


def predicate_words(predicate: str) -> tuple[str, str, str | None]:
    """(field, condition, table) of a filter the renderer compiled (``'T'[C] IN {..}``,
    ``'T'[C] = 6.0``); a compound one keeps its field unnamed."""
    text = predicate.strip()
    for pattern in (_PRED_IN, _PRED_CMP):
        match = pattern.match(text)
        if match is None:
            continue
        table = match.group("table").replace("''", "'")
        column = match.group("column").replace("]]", "]")
        if pattern is _PRED_IN:
            values = [_value_words(t) for t in _PRED_VALUE.findall(match.group("values"))]
            condition = _listed(values) if values else "is in a list Studio cannot read"
        else:
            tokens = _PRED_VALUE.findall(match.group("value"))
            value = _value_words(tokens[0]) if tokens else match.group("value").strip()
            condition = f"{_OP_WORDS[match.group('op')]} {value}"
        return f"{table}[{column}]", condition, table
    return "a combination of fields", "matches a combined condition", None


def _field_of(node: Any, aliases: Mapping[str, str]) -> tuple[str, str | None, bool]:
    """(label, table, whether it is a measure) of a filter's ``field``."""
    if not isinstance(node, Mapping):
        return "a field", None, False
    if "Aggregation" in node and isinstance(node["Aggregation"], Mapping):
        inner = node["Aggregation"].get("Expression")
        label, table, _ = _field_of(inner, aliases)
        return f"the total of {label}", table, False
    for kind in ("Column", "Measure"):
        item = node.get(kind)
        if isinstance(item, Mapping) and isinstance(item.get("Property"), str):
            source = item.get("Expression", {}).get("SourceRef", {})
            table = source.get("Entity") or aliases.get(str(source.get("Source")))
            name = item["Property"]
            if kind == "Measure":
                return f"[{name}]", table if isinstance(table, str) else None, True
            if isinstance(table, str):
                return f"{table}[{name}]", table, False
            return f"[{name}]", None, False
    level = node.get("HierarchyLevel")
    if isinstance(level, Mapping) and isinstance(level.get("Level"), str):
        return str(level["Level"]), None, False
    return "a field", None, False


def _object_uses(
    compiler: lr.Compiler, holder: Mapping[str, Any], source: FilterSource, origin: str | None
) -> list[tuple[str, FilterUse, str | None]]:
    """(predicate text, the filter in words, its table) for each filter a report, page or
    visual definition holds, as the renderer compiles them."""
    config = holder.get("filterConfig")
    filters = config.get("filters") if isinstance(config, Mapping) else None
    found: list[tuple[str, FilterUse, str | None]] = []
    for item in filters if isinstance(filters, list) else []:
        if not isinstance(item, Mapping) or not isinstance(item.get("filter"), Mapping):
            continue
        body = item["filter"]
        aliases = {
            str(a.get("Name")): str(a.get("Entity"))
            for a in body.get("From", [])
            if isinstance(a, Mapping)
        }
        label, table, measure = _field_of(item.get("field"), aliases)
        text = describe_filter(dict(item))
        prop = label.rsplit("[", 1)[-1].rstrip("]") if "[" in label else label
        condition = text[len(prop) + 1 :] if text.startswith(prop + " ") else text
        use = FilterUse(source, label, condition, origin, measure, None, "")
        try:
            predicates = compiler.predicates(dict(body))
        except (lr.Unsupported, KeyError, TypeError, AttributeError, ValueError):
            predicates = []
        for predicate in predicates:
            found.append((predicate[0], use, table))
    return found


def _caption(visual: ImportedVisual) -> str:
    return visual.caption or visual.title or visual.id


def _filter_uses(
    compiled: lr.Compiled,
    visual: ImportedVisual,
    plan: lr.QueryPlan,
    request: lr.LiveReportRequest,
) -> tuple[list[tuple[FilterUse, str | None]], list[str]]:
    """The filters of the visual's query plan, each traced to where it comes from."""
    ctx = compiled.ctx
    compiler = ctx.compiler
    notes: list[str] = []
    original = next((p for p in ctx.pages if p.id == ctx.page.id), ctx.page)
    bookmark = ctx.bookmark
    saved: Json = (
        nav.bookmark_filters(bookmark, original)
        if bookmark is not None
        else {"report": None, "page": None, "visuals": {}}
    )
    saved_slicers = nav.bookmark_slicers(bookmark, original) if bookmark is not None else {}
    marked = f"bookmark '{bookmark.display_name}'" if bookmark is not None else None
    candidates: list[tuple[str, FilterUse, str | None]] = []
    candidates += _object_uses(
        compiler,
        ctx.report_config,
        "bookmark" if saved["report"] is not None else "report",
        marked if saved["report"] is not None else None,
    )
    drilled = {d.field for d in request.drill if d.values}
    page_config = lr.unbound_page(ctx.page_config, drilled) if drilled else ctx.page_config
    candidates += _object_uses(
        compiler,
        page_config,
        "bookmark" if saved["page"] is not None else "page",
        marked if saved["page"] is not None else None,
    )
    in_bookmark = visual.id in saved["visuals"]
    candidates += _object_uses(
        compiler,
        visual.definition,
        "bookmark" if in_bookmark else "visual",
        marked if in_bookmark else None,
    )
    try:
        drilled_predicates = lr.drill_predicates(ctx, request, notes)
    except (lr.Unsupported, ValueError, KeyError, TypeError):
        drilled_predicates = []
    for predicate in drilled_predicates:
        label, condition, table = predicate_words(predicate[0])
        use = FilterUse(
            "drill", label, condition, "(the point this page was opened for)", False, None, ""
        )
        candidates.append((predicate[0], use, table))
    scope = lr.page_scope(ctx, request)
    for origin, predicates in scope.contexts.items():
        source_visual = scope.visuals.get(origin)
        if source_visual is None:
            continue
        kind = lr.kind_of(source_visual.visual_type)
        source: FilterSource = "slicer" if kind == "slicer" else "cross_filter"
        if origin in saved_slicers:
            source = "bookmark"
        where = (
            "in this visual itself"
            if origin == visual.id
            else f"'{_caption(source_visual)}'"
            if kind == "slicer"
            else f"in '{_caption(source_visual)}'"
        )
        if source == "bookmark":
            where = f"{marked}, {'slicer ' if kind == 'slicer' else 'selection '}{where}"
        for predicate in predicates:
            label, condition, table = predicate_words(predicate[0])
            candidates.append(
                (
                    predicate[0],
                    FilterUse(source, label, condition, where, predicate[2], None, ""),
                    table,
                )
            )
    uses: list[tuple[FilterUse, str | None]] = []
    used: set[int] = set()
    for predicate in plan.filters:
        index = next(
            (i for i, c in enumerate(candidates) if i not in used and c[0] == predicate[0]),
            None,
        )
        if index is None:
            label, condition, table = predicate_words(predicate[0])
            use = FilterUse("other", label, condition, None, predicate[2], None, "")
            uses.append((use, table if not predicate[2] else None))
            continue
        used.add(index)
        _, use, table = candidates[index]
        if all(u is not use for u, _ in uses):
            uses.append((use, table))
    for predicate in plan.highlights:
        label, _, _ = predicate_words(predicate[0])
        notes.append(
            f"A selection on {label} highlights this visual; the number explained is its full "
            "value, not the highlighted part"
        )
    return uses, notes


# --- colour --------------------------------------------------------------------------------

_HUES = (
    (15, "red"), (45, "orange"), (70, "yellow"), (170, "green"), (200, "teal"),
    (260, "blue"), (290, "purple"), (345, "pink"), (360, "red"),
)  # fmt: skip
COLOUR_WORDS = frozenset(
    {"red", "orange", "yellow", "green", "teal", "blue", "purple", "pink", "grey", "gray",
     "black", "white"}
)  # fmt: skip


def colour_name(color: str | None) -> str | None:
    """A plain name for a colour ("#DC143C" -> "red", "#cccccc" -> "grey")."""
    if not color:
        return None
    hexed = expand_hex(color[:7] if re.fullmatch(r"#[0-9a-fA-F]{8}", color) else color)
    if hexed is None:
        return None
    r, g, b = (int(hexed[i : i + 2], 16) / 255 for i in (1, 3, 5))
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)
    if light >= 0.93:
        return "white"
    if light <= 0.1:
        return "black"
    if sat < 0.15:
        return "grey"
    degrees = hue * 360
    return next(name for bound, name in _HUES if degrees < bound)


@dataclass
class _Colour:
    """Where a point's colour comes from, found before the query (a field-value colour needs
    its measure's parts in the same query)."""

    target: str
    rule: ColourRule
    color: str | None
    index: int | None = None
    measure: str | None = None
    rule_object: Rule | None = None
    detail: str = ""


def _mark_colour(
    row_item: Mapping[str, Any] | None,
    fields: Sequence[Mapping[str, Any]],
    values: Sequence[Any] | None,
    column: int,
    theme: Mapping[str, Any] | None,
    row_index: int | None,
) -> _Colour | None:
    """The colour of one point as the render contract draws it (sections 3 and 12)."""
    if row_item is None:
        return None
    formatting = row_item.get("formatting")
    formatting = formatting if isinstance(formatting, Mapping) else {}
    kind = str(row_item.get("kind") or "")
    columns = _list(row_item.get("columns"))

    def from_index(index: int, target: str) -> _Colour | None:
        cell = values[index] if values is not None and index < len(values) else None
        helper = fields[index] if index < len(fields) else {}
        rule = helper.get("_rule")
        if isinstance(rule, Rule):
            return _Colour(
                target,
                rule.style if rule.style != "fieldValue" else "fieldValue",  # type: ignore[arg-type]
                cell if isinstance(cell, str) else None,
                index,
                None,
                rule,
            )
        if helper.get("kind") == "measure":
            return _Colour(
                target,
                "fieldValue",
                cell if isinstance(cell, str) else None,
                index,
                str(helper.get("name") or ""),
            )
        return None

    if kind in {"column", "bar", "line", "area", "pie", "donut"}:
        series = formatting.get("series") if isinstance(formatting.get("series"), Mapping) else {}
        assert isinstance(series, Mapping)
        query_ref = columns[column].get("query_ref") if column < len(columns) else None
        for rule_color in series.get("ruleColors") or []:
            index = rule_color.get("index") if isinstance(rule_color, Mapping) else None
            metadata = rule_color.get("metadata") if isinstance(rule_color, Mapping) else None
            if isinstance(index, int) and metadata in (None, query_ref):
                found = from_index(index, "bar" if kind in {"column", "bar"} else "point")
                if found is not None and found.color:
                    return found
        categories = {
            c.get("query_ref"): (values[i] if values is not None and i < len(values) else None)
            for i, c in enumerate(columns)
            if isinstance(c, Mapping) and c.get("kind") == "column"
        }
        for entry in series.get("categoryColors") or []:
            if not isinstance(entry, Mapping):
                continue
            if entry.get("metadata") not in (None, query_ref):
                continue
            wanted = entry.get("column")
            if wanted in categories and _same(categories[wanted], entry.get("value")):
                return _Colour(
                    "bar",
                    "fixed",
                    str(entry.get("color") or "") or None,
                    detail=f"{entry.get('value')}",
                )
        colors = series.get("seriesColors")
        if isinstance(colors, Mapping) and query_ref in colors:
            return _Colour("bar", "series", str(colors[query_ref]))
        if series.get("defaultColor"):
            return _Colour("bar", "default", str(series["defaultColor"]))
        palette = theme.get("dataColors") if isinstance(theme, Mapping) else None
        if isinstance(palette, list) and palette:
            position = (row_index or 0) if series.get("varyByCategory") else 0
            return _Colour(
                "bar", "theme", str(palette[position % len(palette)]), detail=str(position + 1)
            )
        return _Colour("bar", "theme", None)
    if kind == "table" and column < len(columns):
        spec = columns[column]
        for key, target in (("backgroundIndex", "background"), ("colorIndex", "font")):
            index = spec.get(key)
            if isinstance(index, int):
                found = from_index(index, target)
                if found is not None:
                    return found
        return None
    if kind == "card":
        value_token = formatting.get("cardValue")
        index = value_token.get("colorIndex") if isinstance(value_token, Mapping) else None
        if isinstance(index, int):
            return from_index(index, "value")
    return None


def _condition_text(condition: tuple[Any, ...], label: str) -> str:
    head = condition[0]
    if head in {"and", "or"}:
        return (
            f"{_condition_text(condition[1], label)} {head} {_condition_text(condition[2], label)}"
        )
    if head == "not":
        return f"not ({_condition_text(condition[1], label)})"
    if head == "const":
        return "any value" if condition[1] else "no value"
    if head == "blank":
        return f"{label} is blank"
    words = {0: "is", 1: "is more than", 2: "is at least", 3: "is less than", 4: "is at most"}
    word = words.get(int(condition[1]), "compares with")
    if head == "pct":
        return f"{label} {word} {float(condition[2]):.0%} of the way from its lowest to its highest"
    return f"{label} {word} {_show(condition[2])}"


def _rule_text(
    colour: _Colour,
    row_item: Mapping[str, Any],
    values: Sequence[Any],
    fields: Sequence[Mapping[str, Any]],
) -> str:
    rule = colour.rule_object
    assert rule is not None
    columns = _list(row_item.get("columns"))
    label = (
        str(fields[rule.input].get("label") or fields[rule.input].get("name"))
        if (rule.input < len(fields))
        else "its input"
    )
    value = values[rule.input] if rule.input < len(values) else None
    rows = _list(row_item.get("rows"))
    numbers = [
        float(r[rule.input])
        for r in rows
        if rule.input < len(r)
        and isinstance(r[rule.input], int | float)
        and not isinstance(r[rule.input], bool)
    ]
    shown = _show(
        _cell(value),
        str(columns[rule.input].get("format") or "") if rule.input < len(columns) else "",
    )
    if rule.kind == "field":
        return f"the colour is the value of {label}, which is {shown} here"
    if rule.kind == "rules":
        span = (min(numbers), max(numbers)) if numbers else None
        typed = (
            float(value)
            if isinstance(value, int | float) and not isinstance(value, bool)
            else value
        )
        for index, (condition, outcome) in enumerate(rule.cases, start=1):
            if holds(condition, typed, span):
                before = f" (the {index - 1} before it do not)" if index > 1 else ""
                return (
                    f"rules on {label}: {label} is {shown} here, so the first rule that holds is "
                    f"rule {index}, '{_condition_text(condition, label)}'{before}, which gives "
                    f"{outcome}"
                )
        default = f"its default {rule.default}" if rule.default else "no colour"
        return f"rules on {label}: {label} is {shown} here and no rule holds, so it takes {default}"
    stops = ", ".join(
        f"{_show(stop.value) if stop.value is not None else ('lowest' if i == 0 else 'highest' if i == len(rule.stops) - 1 else 'middle')} -> {stop.color}"  # noqa: E501
        for i, stop in enumerate(rule.stops)
    )
    extent = (
        f"; the lowest and highest {label} shown are {_general(min(numbers))} and "
        f"{_general(max(numbers))}"
        if numbers and rule.range_dependent
        else ""
    )
    return f"a colour scale on {label} ({stops}){extent}; {label} is {shown} here"


# --- the query -----------------------------------------------------------------------------


@dataclass
class _Query:
    group_refs: list[str]
    target: str | None
    filters: list[lr.Predicate]
    point: list[str]

    def text(self, extras: Sequence[str], *, with_target: bool = True) -> str:
        measures = [(0, self.target)] if with_target and self.target else []
        base = lr.grouped_query(self.group_refs, measures, list(extras), self.filters)
        if self.point:
            base = f"FILTER({base}, {' && '.join(self.point)})"
        return "EVALUATE " + base


def _first_row(result: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, int]:
    rows = result.get("rows")
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], Mapping):
        return None, 0
    return rows[0], len(rows)


def _read(row: Mapping[str, Any] | None, name: str) -> Value:
    if row is None:
        return None
    if f"[{name}]" in row:
        return _cell(row[f"[{name}]"])
    return _cell(row.get(name))


def _evaluate(
    engine: Engine, report: ImportedReport, query: _Query, specs: Sequence[_Spec]
) -> tuple[dict[str, Value], dict[str, str], bool]:
    """Values by key ("main" and each spec's), why a key could not be computed, and whether a
    row came back at all. One query first; if the engine refuses it, the target and each part
    alone, so one part it cannot compute costs only that part."""
    values: dict[str, Value] = {}
    failed: dict[str, str] = {}
    extras = [s.dax for s in specs]
    try:
        row, count = _first_row(engine.query(report, query.text(extras), 5))
        values["main"] = _read(row, "M0") if query.target else None
        for index, spec in enumerate(specs):
            values[spec.key] = _read(row, f"X{index}")
        return values, failed, count > 0
    except _ENGINE_ERRORS:
        pass
    found = False
    if query.target:
        try:
            row, count = _first_row(engine.query(report, query.text([]), 5))
            values["main"], found = _read(row, "M0"), count > 0
        except _ENGINE_ERRORS as error:
            failed["main"] = _reason(error)
    started = time.monotonic()
    for spec in specs:
        if time.monotonic() - started > RETRY_BUDGET:
            failed[spec.key] = "Studio ran out of time before it could calculate this part"
            continue
        try:
            row, count = _first_row(
                engine.query(report, query.text([spec.dax], with_target=False), 5)
            )
            values[spec.key] = _read(row, "X0")
            found = found or count > 0
        except _ENGINE_ERRORS as error:
            failed[spec.key] = _reason(error)
    return values, failed, found


# --- explain -------------------------------------------------------------------------------


@dataclass
class _Build:
    """Everything an explanation's text is composed from."""

    report: ImportedReport
    visual: ImportedVisual
    kind: str
    caption: str
    point: dict[str, Value]
    measure: str | None
    measure_label: str
    value: Value = None
    formatted: str | None = None
    computed: bool = False
    meaning: str | None = None
    period: str | None = None
    planner: _Planner | None = None
    values: dict[str, Value] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)
    filters: list[FilterUse] = field(default_factory=list)
    paths: list[JoinPath] = field(default_factory=list)
    colour: ColourReason | None = None
    colour_measure: str | None = None
    blank: str | None = None
    notes: list[str] = field(default_factory=list)
    engine: str = ENGINE_LABEL
    fact_rows: dict[str, Value] = field(default_factory=dict)
    total: bool = False
    # Tables (casefolded) with a (Blank) member: a many side has keys they lack.
    blank_rows: set[str] = field(default_factory=set)


def _visual_row(payload: Mapping[str, Any] | None, visual_id: str) -> Json | None:
    if payload is None:
        return None
    if payload.get("id") == visual_id and "columns" in payload:
        return dict(payload)
    visuals = payload.get("visuals")
    if isinstance(visuals, list):
        for item in visuals:
            if isinstance(item, dict) and item.get("id") == visual_id:
                return item
    if isinstance(visuals, Mapping) and isinstance(visuals.get(visual_id), dict):
        return dict(visuals[visual_id])
    return None


def _find_row(
    row_item: Mapping[str, Any] | None, fields: Sequence[Mapping[str, Any]], point: Point
) -> tuple[int | None, list[Any] | None]:
    """The payload row whose category values are the point's."""
    rows = row_item.get("rows") if row_item is not None else None
    if not isinstance(rows, list) or not point.values:
        return None, None
    wanted: list[tuple[int, Value]] = []
    for query_ref, value in point.values.items():
        index = next(
            (
                i
                for i, f in enumerate(fields)
                if f.get("kind") == "column" and query_ref.casefold() in _refs_of(f)
            ),
            None,
        )
        if index is None:
            return None, None
        wanted.append((index, value))
    for position, row in enumerate(rows):
        if isinstance(row, list) and all(i < len(row) and _same(row[i], v) for i, v in wanted):
            return position, row
    return None, None


def _refs_of(spec: Mapping[str, Any]) -> set[str]:
    """Names a column answers to: its queryRef (Desktop keeps it across a rename, so it can
    differ from the field: the Agents table's [vs Previous Month] is "# Measures.vs PM"),
    Table.Name, its name and its display label."""
    found = {str(spec.get(k) or "").casefold() for k in ("query_ref", "name", "label")}
    found.add(f"{spec.get('table')}.{spec.get('name')}".casefold())
    return found - {""}


def _pick_measure(fields: Sequence[Mapping[str, Any]], asked: str | None) -> int | None:
    if asked:
        folded = asked.casefold()
        for index, f in enumerate(fields):
            names = _refs_of(f)
            if folded in names:
                return index
        raise LookupError(f"This visual has no value column named {asked[:80]!r}")
    return next(
        (i for i, f in enumerate(fields) if f.get("kind") == "measure" and not f.get("hidden")),
        next((i for i, f in enumerate(fields) if f.get("kind") == "measure"), None),
    )


def _point_condition(ctx: lr.PageContext, spec: Mapping[str, Any], value: Value) -> str:
    ref = str(spec["ref"])
    if value is None:
        return f"ISBLANK({ref})"
    types = {
        (str(t.get("TableName")), str(t.get("ColumnName"))): str(t.get("PandasDataType") or "")
        for t in ctx.report.tables
    }
    if "datetime" in types.get((str(spec.get("table")), str(spec.get("name"))), ""):
        return f"{ref} = {lr.date_literal(value if not isinstance(value, datetime) else value.isoformat())}"  # noqa: E501
    if isinstance(value, datetime):
        raise lr.Unsupported("Drilling on a date with a time of day needs Power BI Desktop")
    return f"{ref} = {lr.literal(value)}"


def _formats(ctx: lr.PageContext, report: ImportedReport) -> dict[str, str]:
    by_name: dict[str, str] = {}
    facts = report.model.get("measures") if isinstance(report.model, Mapping) else None
    for item in facts if isinstance(facts, list) else []:
        if isinstance(item, Mapping) and isinstance(item.get("formatString"), str):
            by_name[str(item.get("name"))] = str(item["formatString"])
    for measure in report.measures:
        name, table = str(measure.get("Name", "")), str(measure.get("TableName", ""))
        found = ctx.formats.get((table, name))
        if isinstance(found, str):
            by_name[name] = found
    return by_name


def explain_point(
    report: ImportedReport,
    page_id: str,
    visual_id: str,
    point: Point | Mapping[str, Any] | None = None,
    *,
    engine: Engine,
    payload: Mapping[str, Any] | None = None,
    request: lr.LiveReportRequest | None = None,
    focus: Focus = "value",
) -> Explanation:
    """Why a card shows its value, or a chart or table point its value (and colour).

    ``payload`` is the page (or visual) render the user sees, when the caller has it: its rows
    identify the point and its formatting the colour; without it the visual is rendered here.
    ``request`` is the canvas state (slicer selections, bookmark, drill). Raises LookupError
    for a visual or column that is not there, ValueError for a malformed point; everything the
    engine cannot compute is reported in the explanation, never raised.
    """
    wanted = Point.of(point)
    request = request or lr.LiveReportRequest()
    compiled = lr.compile_visual(report, page_id, request, only={visual_id})
    visual = next((v for v in compiled.ctx.page.visuals if v.id == visual_id), None)
    framed = compiled.framed.get(visual_id)
    if visual is None or framed is None:
        raise LookupError("This visual is not on this page")
    item = framed.item
    kind = str(item.get("kind") or lr.kind_of(visual.visual_type))
    caption = _caption(visual)
    if kind == "card":
        caption = card_label(visual) or caption
    elif isinstance(item.get("title"), str) and item["title"] and not visual.caption:
        caption = str(item["title"])
    fields = framed.fields or []
    build = _Build(
        report,
        visual,
        kind,
        caption,
        {},
        None,
        "",
        engine=getattr(engine, "label", "Power BI Desktop"),
    )
    theme: Json | None = None
    row_item = _visual_row(payload, visual_id)
    if isinstance(payload, Mapping) and isinstance(payload.get("theme"), Mapping):
        theme = dict(payload["theme"])
    if isinstance(payload, Mapping) and isinstance(payload.get("notes"), list):
        build.notes += [str(n) for n in payload["notes"]]
    if row_item is None and kind in lr.DATA_KINDS and framed.plan is not None:
        try:
            rendered = lr.render_page(engine, report, page_id, request, only={visual_id})
            row_item = _visual_row(rendered, visual_id)
            theme = rendered.get("theme") if isinstance(rendered.get("theme"), dict) else None
            build.notes += [str(n) for n in rendered.get("notes") or []]
        except _ENGINE_ERRORS as error:
            build.notes.append(f"Studio could not draw this visual here: {_reason(error)}")
    build.notes += list(compiled.ctx.bookmark_notes)
    if row_item is not None:
        build.notes += [str(n) for n in row_item.get("dataNotes") or []]
    if kind not in lr.DATA_KINDS or kind == "slicer":
        build.notes.append("This visual shows no calculated number to explain")
        return _finish(build, compiled, page_id, None, focus)
    if framed.plan is None or item.get("error"):
        reason = str(item.get("error") or "Studio cannot reproduce this visual's filters")
        build.notes.append(
            f"Studio cannot compute this visual: {reason}. Power BI Desktop shows it"
        )
    column = _pick_measure(fields, wanted.measure if wanted else None)
    if column is None:
        build.notes.append("This visual shows no measure to explain")
        return _finish(build, compiled, page_id, None, focus)
    spec = fields[column]
    if spec.get("kind") != "measure":
        build.point = dict(wanted.values) if wanted else {}
        build.measure_label = str(spec.get("label") or spec.get("name"))
        build.notes.append(
            f"{build.measure_label} is a column of {spec.get('table')}: its values are data, not "
            "a calculation"
        )
        return _finish(build, compiled, page_id, None, focus)
    build.measure = str(spec["name"])
    # A column header can be a blank display name (the Agents table's [vs Previous Month]).
    build.measure_label = str(spec.get("label") or "").strip() or build.measure
    groups = [i for i, f in enumerate(fields) if f.get("kind") == "column"]
    row_index, row_values = (None, None)
    if wanted is not None and groups:
        row_index, row_values = _find_row(row_item, fields, wanted)
    elif not groups and row_item is not None and isinstance(row_item.get("rows"), list):
        rows = row_item["rows"]
        row_index, row_values = (0, rows[0]) if rows and isinstance(rows[0], list) else (None, None)
    point_values: dict[int, Value] = {}
    if groups and wanted is not None:
        if row_values is not None:
            point_values = {i: _cell(row_values[i]) for i in groups}
        else:
            for query_ref, value in wanted.values.items():
                index = next(
                    (i for i in groups if query_ref.casefold() in _refs_of(fields[i])), None
                )
                if index is None:
                    raise LookupError(f"This visual has no category column {query_ref[:80]!r}")
                point_values[index] = value
    build.total = bool(groups) and wanted is None
    build.point = {
        str(fields[i].get("label") or fields[i].get("name")): v
        for i, v in point_values.items()
        if not fields[i].get("hidden")
    }
    measures = {str(m["Name"]): str(m.get("Expression", "")) for m in report.measures}
    formats = _formats(compiled.ctx, report)
    columns = {
        (str(t.get("TableName", "")).casefold(), str(t.get("ColumnName", "")).casefold())
        for t in report.tables
    }
    planner = _Planner(measures, columns, formats)
    build.planner = planner
    build.meaning = describe(measures.get(build.measure, ""), set(measures))
    planner.plan(build.measure)
    colour = _mark_colour(row_item, fields, row_values, column, theme, row_index)
    if colour is None and focus == "colour" and kind == "table":
        # "Why is this row white?": the colour is set on another column of the row.
        colour = next(
            (
                elsewhere
                for other in range(len(fields))
                if other != column
                and (
                    elsewhere := _mark_colour(row_item, fields, row_values, other, theme, row_index)
                )
            ),
            None,
        )
    if colour is not None and colour.rule == "fieldValue" and colour.measure:
        build.colour_measure = colour.measure
        planner.plan(colour.measure, 1)
    # Probes read only for the text: the period a time-intelligence measure covers, and the
    # rows of the tables it adds up.
    model = engine.model if isinstance(engine, LocalReportEngine) else None
    if model is not None:
        build.blank_rows = {t.name.casefold() for t in model.tables.values() if t.blank_row}
    hidden: dict[str, _Spec] = {}
    text = " ".join(measures.get(m, "") for m in planner.closure(build.measure)).upper()
    if model is not None and re.search(r"DATES[MQY]TD|TOTAL[MQY]TD", text):
        for label, dax in zip(("end", "last"), period_probes(model), strict=False):
            found = planner.add(
                _Spec(
                    "", label, "rows", build.measure, dax, dax, None, "", "direct", 0, hidden=True
                )
            )
            if found is not None:
                hidden[label] = found
    tables = {str(t.get("TableName", "")) for t in report.tables}
    read, aggregated, virtual = _reads(planner, build.measure, tables)
    graph = _Graph(report_relationships(report)[0])
    ones = {e.one.casefold() for edges in graph.up.values() for e in edges if not e.both}
    facts = sorted(t for t in aggregated if t.casefold() not in ones) or sorted(aggregated)
    for table in facts[:2]:
        dax = "COUNTROWS('" + table.replace("'", "''") + "')"
        found = planner.add(
            _Spec("", table, "rows", build.measure, dax, dax, None, "", "direct", 0, hidden=True)
        )
        if found is not None:
            hidden["rows:" + table] = found
    plan = framed.plan
    uses: list[tuple[FilterUse, str | None]] = []
    if plan is not None and not item.get("error"):
        try:
            conditions = [
                _point_condition(compiled.ctx, fields[i], v) for i, v in point_values.items()
            ]
            query = _Query(
                [str(fields[i]["ref"]) for i in groups] if point_values else [],
                str(spec["ref"]),
                list(plan.filters),
                conditions,
            )
            values, failed, found_row = _evaluate(engine, report, query, planner.specs)
            build.values, build.failed = values, failed
            if "main" in failed:
                build.notes.append(
                    f"Studio cannot compute {build.measure_label} here: {failed['main']}"
                )
            else:
                build.value, build.computed = values.get("main"), True
            if point_values and not found_row and row_values is None:
                build.notes.append("No row of this visual has these values now")
        except (lr.Unsupported, ValueError) as error:
            build.notes.append(f"Studio cannot compute this point: {_reason(error)}")
        more_uses, notes = _filter_uses(compiled, visual, plan, request)
        uses += more_uses
        build.notes += notes
    shown_format = str(spec.get("format") or build.measure and formats.get(build.measure) or "")
    if build.computed:
        payload_value = (
            row_values[column] if row_values is not None and column < len(row_values) else None
        )
        payload_text = None
        if row_item is not None and row_index is not None:
            formatted_rows = row_item.get("formatted_rows")
            if isinstance(formatted_rows, list) and row_index < len(formatted_rows):
                cells = formatted_rows[row_index]
                if isinstance(cells, list) and column < len(cells) and cells[column]:
                    payload_text = _clean(str(cells[column]))
        if row_values is not None and not _same(_cell(payload_value), build.value):
            build.notes.append(
                f"The canvas showed {_show(_cell(payload_value), shown_format)}; calculated now it "
                f"is {_show(build.value, shown_format)} (the data or the filters changed since)"
            )
        elif payload_text:
            build.formatted = payload_text
        if build.formatted is None:
            build.formatted = _show(build.value, shown_format)
    end, last = (build.values.get(hidden[k].key) if k in hidden else None for k in ("end", "last"))
    period = card_period(measures, build.measure, end, last).strip()
    build.period = period or None
    for key, spec_row in hidden.items():
        if key.startswith("rows:"):
            build.fact_rows[key[5:]] = build.values.get(spec_row.key)
    build.steps = _steps(planner, build)
    point_uses: list[tuple[FilterUse, str | None]] = [
        (
            FilterUse(
                "point",
                f"{fields[i].get('table')}[{fields[i].get('name')}]",
                "is (Blank)" if v is None else f"is {_show(v).strip(chr(34))}",
                f"this {_MARKS.get(kind, 'point')}",
                False,
                None,
                "",
            ),
            str(fields[i].get("table")),
        )
        for i, v in point_values.items()
        if not fields[i].get("hidden")
    ]
    build.filters, build.paths = _reach(point_uses + uses, graph, read, aggregated, virtual, build)
    if colour is not None and row_item is not None:
        build.colour = _colour_reason(colour, row_item, row_values or [], fields, build)
    if build.computed and build.value is None:
        build.blank = _blank_reason(build, model, point_values, fields)
    if isinstance(report.model, Mapping) and report.model.get("roles"):
        build.notes.append(
            "Row-level security roles are not applied: these are the numbers the report's "
            "author sees"
        )
    return _finish(build, compiled, page_id, spec, focus)


def _taken(planner: _Planner, build: _Build, owner: str) -> Node | None:
    """The branch an IF/SWITCH chain returns here (None: not known)."""
    parts = planner.parts_of(owner)
    if parts is None:
        return None
    variables, node = parts
    for _ in range(MAX_BRANCH_LEVELS + 1):
        resolved = _resolve(node, variables)
        if isinstance(resolved, Call) and resolved.name == "IF" and len(resolved.args) in {2, 3}:
            spec = planner.conditions.get((owner, id(resolved.args[0])))
            if spec is None or spec.key not in build.values:
                return None
            if _truthy(build.values[spec.key]):
                node = resolved.args[1]
            elif len(resolved.args) == 3:
                node = resolved.args[2]
            else:
                return Call("BLANK", ())
        elif isinstance(resolved, Call) and resolved.name == "SWITCH" and len(resolved.args) >= 3:
            rest = resolved.args[1:]
            cases = [(rest[i], rest[i + 1]) for i in range(0, len(rest) - 1, 2)][:8]
            chosen: Node = rest[-1] if len(rest) % 2 else Call("BLANK", ())
            for test, result in cases:
                spec = planner.conditions.get((owner, id(test)))
                if spec is None or spec.key not in build.values:
                    return None
                if _truthy(build.values[spec.key]):
                    chosen = result
                    break
            node = chosen
        else:
            return resolved
    return None


def _step_format(planner: _Planner, build: _Build, spec: _Spec) -> str:
    if spec.kind != "variable" or not spec.result:
        return spec.fmt
    taken = _taken(planner, build, spec.owner)
    parts = planner.parts_of(spec.owner)
    if taken is None or parts is None:
        return spec.fmt
    if planner.leaves(taken, parts[0]) == spec.leaves:
        return planner.fmt(spec.owner)
    return planner.fmt(next(iter(spec.leaves))) if len(spec.leaves) == 1 else ""


def _steps(planner: _Planner, build: _Build) -> list[Step]:
    steps: list[Step] = []
    for spec in planner.specs:
        if spec.hidden:
            continue
        value = build.values.get(spec.key)
        reason = build.failed.get(spec.key)
        spec.fmt = _step_format(planner, build, spec)
        steps.append(
            Step(
                spec.name,
                spec.kind,
                spec.owner,
                spec.formula,
                spec.meaning,
                value,
                "not calculated"
                if reason or spec.key not in build.values
                else _show(value, spec.fmt),
                reason is None and spec.key in build.values,
                spec.usage,
                spec.depth,
                reason,
            )
        )
    for item in planner.described:
        if any(s.name == f"[{item.name}]" and s.kind == "measure" for s in steps):
            continue
        steps.append(
            Step(
                f"[{item.name}]",
                "measure",
                item.owner,
                f"[{item.name}]",
                describe(planner.measures.get(item.name, ""), set(planner.measures)),
                None,
                "",
                False,
                item.usage,
                item.depth,
                None,
            )
        )
    return steps


def _reach(
    uses: list[tuple[FilterUse, str | None]],
    graph: _Graph,
    read: set[str],
    aggregated: set[str],
    virtual: bool,
    build: _Build,
) -> tuple[list[FilterUse], list[JoinPath]]:
    """Each filter with whether it can change the number, and the relationship paths it takes."""
    measure = f"[{build.measure}]" if build.measure else "the measure"
    found: list[FilterUse] = []
    paths: list[JoinPath] = []
    seen: set[tuple[str, ...]] = set()
    folded = {t.casefold(): t for t in read}
    for use, table in uses:
        if use.on_measure:
            how = (
                "it decides which rows the visual shows; it does not change this row's value"
                if build.kind == "table" or (build.point and build.kind != "card")
                else "the visual shows its value only while this holds"
            )
            found.append(
                FilterUse(use.source, use.field, use.condition, use.origin, True, None, how)
            )
            continue
        if table is None:
            found.append(
                FilterUse(
                    use.source,
                    use.field,
                    use.condition,
                    use.origin,
                    False,
                    None,
                    "Studio cannot tell which table it filters",
                )
            )
            continue
        if table.casefold() in folded:
            found.append(
                FilterUse(
                    use.source,
                    use.field,
                    use.condition,
                    use.origin,
                    False,
                    True,
                    f"{measure} reads {table} itself",
                )
            )
            continue
        routes = [(t, graph.path(t, table)) for t in sorted(read)]
        reached = [(t, p) for t, p in routes if p]
        if reached:
            target, path = min(reached, key=lambda pair: len(pair[1] or []))
            assert path is not None
            chain = (target, *(e.one for e in path))
            found.append(
                FilterUse(
                    use.source,
                    use.field,
                    use.condition,
                    use.origin,
                    False,
                    True,
                    f"through {' → '.join(reversed(chain))}",
                )
            )
            if chain not in seen:
                seen.add(chain)
                paths.append(
                    JoinPath(
                        tuple(chain),
                        tuple(
                            f"{e.many}[{e.many_column}] → {e.one}[{e.one_column}]"
                            + (" (both ways)" if e.both else "")
                            for e in path
                        ),
                        f"a filter on {table} ({use.field} {use.condition}) reaches {target}, "
                        f"which {measure} reads",
                    )
                )
            continue
        if virtual:
            found.append(
                FilterUse(
                    use.source,
                    use.field,
                    use.condition,
                    use.origin,
                    False,
                    None,
                    f"no relationship carries it, but {measure} builds joins of "
                    "its own (TREATAS or USERELATIONSHIP), so Studio cannot tell",
                )
            )
            continue
        found.append(
            FilterUse(
                use.source,
                use.field,
                use.condition,
                use.origin,
                False,
                False,
                f"it does not change this number: no relationship takes a filter "
                f"on {table} to the tables {measure} reads, and it does not read "
                f"{table}",
            )
        )
    # The joins inside the measure: how the table it adds up reaches the others it reads
    # ("FactSales → DimStore → DimEmployee" for a count of agents by their sales).
    for start in sorted(aggregated):
        for other in sorted(read - {start}):
            inner = graph.path(start, other)
            if not inner:
                continue
            chain = (start, *(e.one for e in inner))
            if chain in seen:
                continue
            seen.add(chain)
            paths.append(
                JoinPath(
                    chain,
                    tuple(
                        f"{e.many}[{e.many_column}] → {e.one}[{e.one_column}]"
                        + (" (both ways)" if e.both else "")
                        for e in inner
                    ),
                    f"{measure} adds up {start} rows and reads {other}: filters on {other} "
                    f"reach {start} through these relationships",
                )
            )
    kept = [
        p
        for p in paths
        if not any(
            q is not p and len(q.tables) > len(p.tables) and q.tables[: len(p.tables)] == p.tables
            for q in paths
        )
    ]
    return found, kept[:6]


def _colour_reason(
    colour: _Colour,
    row_item: Mapping[str, Any],
    values: Sequence[Any],
    fields: Sequence[Mapping[str, Any]],
    build: _Build,
) -> ColourReason | None:
    name = colour_name(colour.color)
    mark = _MARKS.get(build.kind, "point")
    shown = colour.color or ""
    if name and colour.color and name != colour.color.casefold():
        shown = f"{colour.color} ({name})"
    if colour.rule == "fixed":
        why = (
            f"The {colour.detail} {mark} has a colour of its own, {shown}, set in the visual's "
            "data colours for that category"
        )
    elif colour.rule == "series":
        why = f"The series has its own colour, {shown}, set in the visual's data colours"
    elif colour.rule == "default":
        why = f"The visual's default data colour is {shown}"
    elif colour.rule == "theme":
        why = (
            f"No colour is set in the visual, so it takes the theme's data colour "
            f"{colour.detail or 1}{', ' + shown if shown else ''}"
        )
    elif colour.rule == "fieldValue":
        measure = colour.measure or "a measure"
        why = (
            f"The {colour.target} colour comes from the measure [{measure}] (conditional "
            f"formatting by field value); for this {mark} it returns {shown or 'no colour'}"
        )
    else:
        why = (
            f"The {colour.target} colour comes from conditional formatting, "
            f"{_rule_text(colour, row_item, values, fields)}; so it is {shown or 'not set'}"
        )
        if (
            row_item.get("truncated")
            and colour.rule_object is not None
            and (colour.rule_object.range_dependent)
        ):
            why += " (worked out from the first 100 rows Studio reads, so it can differ)"
    return ColourReason(colour.target, colour.color, name, colour.rule, why, colour.measure)


def _blank_reason(
    build: _Build,
    model: Model | None,
    point_values: Mapping[int, Value],
    fields: Sequence[Mapping[str, Any]],
) -> str:
    """Why the value is blank: its own IF/SWITCH, a blank part, no rows, or a blank key."""
    reasons: list[str] = []
    planner = build.planner
    if planner is not None and build.measure:
        lines, ended = _path_lines(planner, build, build.measure, 0)
        if ended == "blank" and lines:
            reasons.append(f"[{build.measure}] returns BLANK by its own logic here: {lines[-1]}")
        elif ended == "part" and lines:
            reasons.append(lines[-1])
        else:
            parts = planner.parts_of(build.measure)
            node = _resolve(parts[1], parts[0]) if parts else None
            if isinstance(node, Call) and node.name == "DIVIDE":
                bottom = _value_of(
                    planner, build, build.measure, node.args[1], parts[0] if parts else ()
                )
                if bottom is not None and not _truthy(bottom[0]):
                    reasons.append(
                        f"the denominator ({bottom[1]}) is {_show(bottom[0])} here, and DIVIDE "
                        "returns BLANK then"
                    )
    empty = [t for t, n in build.fact_rows.items() if n in (0, None)]
    if empty and len(empty) == len(build.fact_rows):
        reasons.append(
            f"no {' or '.join(empty)} rows match this {_MARKS.get(build.kind, 'point')}'s "
            "filters, so there is nothing to add up"
        )
    unreached = [f for f in build.filters if f.source == "point" and f.reaches is False]
    for use in unreached:
        reasons.append(f"the grouping by {use.field} does not reach the tables the measure reads")
    if model is not None:
        for index, value in point_values.items():
            if value is not None:
                continue
            table = str(fields[index].get("table", "")).casefold()
            for rel in model.relationships:
                if rel.one != table or not rel.active:
                    continue
                unmatched = model.unmatched(rel)
                if unmatched.any():
                    many = model.tables[rel.many]
                    column = many.columns[rel.many_column]
                    keys = sorted(
                        {column.value(int(i)) for i in unmatched.nonzero()[0]} - {None}, key=str
                    )[:3]
                    count = int(unmatched.sum())
                    reasons.append(
                        f"the (Blank) {model.tables[rel.one].name} row collects {count:,} "
                        f"{many.name} row{'s' if count != 1 else ''} whose {column.name} "
                        f"({', '.join(str(k) for k in keys) or 'blank'}) is missing from "
                        f"{model.tables[rel.one].name}"
                    )
    if not reasons:
        reasons.append(f"[{build.measure}] gives BLANK for these filters")
    return "; ".join(reasons)


def _value_of(
    planner: _Planner, build: _Build, owner: str, node: Node, variables: Variables
) -> tuple[Value, str] | None:
    """(value, label) of a formula node whose step the engine computed: a variable, measure or
    part. None when it has no step or the engine could not compute it (never a guess)."""

    def computed(spec: _Spec, label: str) -> tuple[Value, str] | None:
        if spec.key not in build.values or spec.key in build.failed:
            return None
        return build.values[spec.key], label

    node_resolved = node
    if isinstance(node, Name):
        spec = planner.variables.get((owner, node.name.casefold()))
        if spec is not None:
            return computed(spec, node.name)
        node_resolved = _resolve(node, variables)
    if isinstance(node_resolved, ColRef):
        ref = planner.measure_of(node_resolved)
        if ref is not None and ref in planner.by_measure:
            return computed(planner.by_measure[ref], f"[{ref}]")
    spec = planner.parts.get((owner, id(node_resolved)))
    if spec is not None:
        return computed(spec, spec.meaning or _snippet(node_resolved))
    if isinstance(node_resolved, Num):
        return node_resolved.value, _general(float(node_resolved.value))
    return None


def _result_words(
    planner: _Planner, build: _Build, owner: str, node: Node | None, variables: Variables
) -> str:
    if node is None:
        return "BLANK (the IF has no else)"
    if isinstance(node, Call) and node.name == "BLANK":
        return "BLANK"
    if isinstance(node, Str):
        return f'"{node.value}"'
    found = _value_of(planner, build, owner, node, variables)
    if found is not None:
        fmt = planner.fmt(owner) if isinstance(node, Name) else ""
        spec = (
            planner.variables.get((owner, node.name.casefold())) if isinstance(node, Name) else None
        )
        fmt = spec.fmt if spec is not None else fmt
        return f"{found[1]} = {_show(found[0], fmt)}"
    words = planner.words(node, variables)
    return words or _snippet(node)


def _path_lines(
    planner: _Planner, build: _Build, owner: str, level: int, node: Node | None = None
) -> tuple[list[str], str]:
    """The IF/SWITCH branches the formula takes here, in words, and how the chain ends:
    "blank" (a BLANK branch), "part" (a blank variable or measure), "value" or "unknown"."""
    parts = planner.parts_of(owner)
    if parts is None or level > MAX_BRANCH_LEVELS:
        return [], "unknown"
    variables, body = parts
    node = _resolve(body if node is None else node, variables)
    if isinstance(node, Call) and node.name == "IF" and len(node.args) in {2, 3}:
        spec = planner.conditions.get((owner, id(node.args[0])))
        words = (spec.meaning if spec else None) or _snippet(node.args[0])
        if spec is None or spec.key in build.failed or spec.key not in build.values:
            why = build.failed.get(spec.key, "") if spec else ""
            return [f"Studio could not check whether {words} holds{': ' + why if why else ''}"], (
                "unknown"
            )
        holds = _truthy(build.values.get(spec.key))
        taken = node.args[1] if holds else (node.args[2] if len(node.args) == 3 else None)
        line = (
            f"{words} {'holds' if holds else 'does not hold'} here, so it returns "
            f"{_result_words(planner, build, owner, taken, variables)}"
        )
        return _follow(planner, build, owner, level, line, taken, variables)
    if isinstance(node, Call) and node.name == "SWITCH" and len(node.args) >= 3:
        subject, rest = node.args[0], node.args[1:]
        cases = [(rest[i], rest[i + 1]) for i in range(0, len(rest) - 1, 2)][:8]
        fallback = rest[-1] if len(rest) % 2 else None
        if not (isinstance(subject, Call) and subject.name == "TRUE"):
            return [], "unknown"
        for index, (test, result) in enumerate(cases):
            spec = planner.conditions.get((owner, id(test)))
            words = (spec.meaning if spec else None) or _snippet(test)
            if spec is None or spec.key not in build.values:
                return [f"Studio could not check the SWITCH condition {words}"], "unknown"
            if _truthy(build.values.get(spec.key)):
                before = f" (the {index} before it do not)" if index else ""
                line = (
                    f"the first SWITCH condition that holds is {words}{before}, so it "
                    f"returns {_result_words(planner, build, owner, result, variables)}"
                )
                return _follow(planner, build, owner, level, line, result, variables)
        line = "none of the SWITCH conditions holds here, so it returns " + (
            _result_words(planner, build, owner, fallback, variables)
            if fallback is not None
            else "BLANK (the SWITCH has no default)"
        )
        return _follow(planner, build, owner, level, line, fallback, variables)
    return [], "value"


def _follow(
    planner: _Planner,
    build: _Build,
    owner: str,
    level: int,
    line: str,
    taken: Node | None,
    variables: Variables,
) -> tuple[list[str], str]:
    if taken is None or (isinstance(taken, Call) and taken.name == "BLANK"):
        return [line], "blank"
    deeper, ended = _path_lines(planner, build, owner, level + 1, taken)
    if deeper:
        return [line, *deeper], ended
    found = _value_of(planner, build, owner, taken, variables)
    if found is not None and found[0] is None:
        return [line], "part"
    return [line], "value"


# --- text ----------------------------------------------------------------------------------


def _point_words(point: Mapping[str, Value]) -> str:
    return ", ".join(
        f"{label} {'(Blank)' if v is None else _show(v).strip(chr(34))}"
        for label, v in point.items()
    )


def _headline(build: _Build) -> str:
    shown = f"**{build.formatted}**" if build.computed else "a value Studio cannot compute here"
    period = f" {build.period}" if build.period else ""
    if build.kind == "card":
        return f"**{build.caption}** shows {shown}{period}."
    if build.total:
        mark = _MARKS.get(build.kind, "point")
        return (
            f"**{build.caption}** as a whole (no {mark} picked): **{build.measure_label}** is "
            f"{shown}{period}."
        )
    if build.kind == "table":
        return (
            f"In **{build.caption}**, the row {_point_words(build.point)}: "
            f"**{build.measure_label}** is {shown}{period}."
        )
    mark = _MARKS.get(build.kind, "point")
    values = " / ".join(
        "(Blank)" if v is None else _show(v).strip('"') for v in build.point.values()
    )
    return f"The **{values}** {mark} of **{build.caption}** shows {shown}{period}."


def _measure_lines(
    planner: _Planner,
    build: _Build,
    owner: str,
    depth: int,
    steps: Sequence[Step],
    *,
    ratio: bool = True,
) -> list[str]:
    indent = "  " * depth
    lines: list[str] = []
    for step in steps:
        if step.owner != owner or step.depth != depth or step.kind not in {"variable", "part"}:
            continue
        value = step.formatted if step.computed else f"not calculated: {step.reason}"
        meaning = f": {step.meaning}" if step.meaning else ""
        lines.append(f"{indent}- {step.name} = {value}{meaning}")
    path, _ = _path_lines(planner, build, owner, 0)
    lines += [f"{indent}- {line[:1].upper()}{line[1:]}." for line in path]
    probes = {
        spec.name: spec
        for spec in planner.specs
        if spec.owner == owner
        and spec.hidden
        and spec.kind == "rows"
        and (owner, spec.name) in planner.probes
    }
    own_value = (
        build.value
        if owner == build.measure
        else build.values.get(planner.by_measure[owner].key)
        if owner in planner.by_measure
        else None
    )
    parts = planner.parts_of(owner)
    node = _resolve(parts[1], parts[0]) if parts else None
    if "checked" in probes and isinstance(node, Call):
        describer_rows = None
        inner = node.args[0] if node.args else None
        if isinstance(inner, Call) and inner.name == "FILTER" and parts is not None:
            try:
                describer_rows = planner.describer(parts[0]).rows(inner)
            except (RecursionError, AttributeError, TypeError, ValueError):
                describer_rows = None
        checked = build.values.get(probes["checked"].key)
        what = (
            describer_rows[0].removeprefix("every ").removeprefix("each ")
            if (describer_rows)
            else "rows"
        )
        condition = (
            f": those where {describer_rows[1]}" if (describer_rows and describer_rows[1]) else ""
        )
        iterated = inner.args[0] if isinstance(inner, Call) and inner.args else None
        table = (
            next(
                (
                    n.table if isinstance(n, ColRef) else n.name
                    for n in _nodes(iterated)
                    if (isinstance(n, ColRef) and n.table) or isinstance(n, Name)
                ),
                None,
            )
            if iterated is not None
            else None
        )
        blank = (
            f" (one is the (Blank) {table} member that unmatched keys add)"
            if table and table.casefold() in build.blank_rows
            else ""
        )
        lines.append(
            f"{indent}- It counts {_show(own_value)} of the {_show(checked)} {what} "
            f"values it checks{blank}{condition}."
        )
    if {"rows", "with_value", "sum"} <= set(probes):
        rows = build.values.get(probes["rows"].key)
        kept = build.values.get(probes["with_value"].key)
        total = build.values.get(probes["sum"].key)
        lines.append(
            f"{indent}- It averages over {_show(rows)} rows; {_show(kept)} of them have a value "
            f"(AVERAGEX leaves blanks out), adding up to {_show(total, probes['sum'].fmt)}. So "
            f"{_show(own_value, planner.fmt(owner))} = {_show(total, probes['sum'].fmt)} ÷ "
            f"{_show(kept)}."
        )
    if ratio:
        lines += _ratio_lines(planner, build, owner, depth)
    return lines


def _ratio_lines(planner: _Planner, build: _Build, owner: str, depth: int) -> list[str]:
    """ "So 46.76% = 137 ÷ 293." for a measure that is a DIVIDE of parts with values."""
    parts = planner.parts_of(owner)
    node = _resolve(parts[1], parts[0]) if parts else None
    own_value = (
        build.value
        if owner == build.measure
        else build.values.get(planner.by_measure[owner].key)
        if owner in planner.by_measure
        else None
    )
    if not (isinstance(node, Call) and node.name == "DIVIDE" and len(node.args) >= 2 and parts):
        return []
    top = _value_of(planner, build, owner, node.args[0], parts[0])
    bottom = _value_of(planner, build, owner, node.args[1], parts[0])
    if top is None or bottom is None or own_value is None:
        return []
    return [
        f"{'  ' * depth}- So {_show(own_value, planner.fmt(owner))} = {_show(top[0])} ÷ "
        f"{_show(bottom[0])}."
    ]


def _compose(build: _Build, steps: Sequence[Step], focus: Focus) -> str:
    lines = [_headline(build), ""]
    planner = build.planner
    colour_lines: list[str] = []
    if build.colour is not None and (
        focus == "colour" or build.colour.rule in {"fieldValue", "gradient", "rules"}
    ):
        colour_lines.append(f"**Why this colour**: {build.colour.why}.")
        if planner is not None and build.colour_measure:
            colour_lines += _measure_lines(planner, build, build.colour_measure, 1, steps)
        colour_lines.append("")
    if focus == "colour":
        lines += colour_lines
    if build.blank and focus != "colour":
        lines += [f"**Why it is blank**: {build.blank}.", ""]
    if planner is not None and build.measure:
        meaning = build.meaning or "a formula with no plain-English wording (see the Explain panel)"
        if len(meaning) > 220 and any(
            s.owner == build.measure and s.kind == "variable" for s in steps
        ):
            # One sentence for a whole chain of variables is unreadable; the steps say it.
            meaning = "worked out in the steps below"
        lines.append(f"**How it is calculated** ([{build.measure}]): {meaning}.")
        lines += _measure_lines(planner, build, build.measure, 0, steps, ratio=False)
        for step in steps:
            if step.kind != "measure" or step.depth != 0 or step.owner != build.measure:
                continue
            meaning = f": {step.meaning}" if step.meaning else ""
            if step.usage == "direct":
                value = step.formatted if step.computed else f"not calculated: {step.reason}"
                lines.append(f"- {step.name} = {value}{meaning}")
                if step.name.strip("[]") in planner.planned:
                    lines += _measure_lines(planner, build, step.name[1:-1], 1, steps)
            else:
                where = next(
                    (
                        d.where
                        for d in planner.described
                        if f"[{d.name}]" == step.name and d.owner == step.owner
                    ),
                    "",
                )
                where = where if len(where) <= 160 else where[:157] + "..."
                lines.append(
                    f"- {step.name}{meaning}; worked out {where}, so it has no single value here"
                )
        lines += _ratio_lines(planner, build, build.measure, 0)
        fact = [f"{_show(n)} {t} rows" for t, n in build.fact_rows.items() if n is not None]
        parts = planner.parts_of(build.measure)
        body = _resolve(parts[1], parts[0]) if parts else None
        plain = isinstance(body, Call) and body.name in _AGGREGATES | {"COUNTROWS", "SUMX"}
        if fact and plain:
            # A plain sum or count: these are the rows it adds up.
            what = "card" if build.kind == "card" else _MARKS.get(build.kind, "point")
            lines.append(f"- This {what}'s filters leave {', '.join(fact)} to add up.")
        lines.append("")
    if focus != "colour":
        lines += colour_lines
    if build.filters:
        lines.append("**Filters in effect**:")
        mark = _MARKS.get(build.kind, "point")
        labels = {
            "report": "Report filter", "page": "Page filter", "visual": "Visual filter",
            "slicer": "Slicer", "cross_filter": "Selection", "drill": "Drill",
            "bookmark": "Bookmark", "point": f"This {mark}", "other": "Filter",
        }  # fmt: skip
        for use in build.filters:
            origin = f" {use.origin}" if use.origin and use.source != "point" else ""
            lines.append(f"- {labels[use.source]}{origin}: {use.field} {use.condition}; {use.how}.")
    elif build.computed:
        lines.append("**Filters in effect**: none reach this visual.")
    if build.paths:
        lines.append("**Relationships**:")
        for path in build.paths:
            lines.append(
                f"- {' → '.join(path.tables[::-1])} ({'; '.join(path.joins)}): {path.why}."
            )
    lines.append("")
    for note in dict.fromkeys(build.notes):
        lines.append(f"_{note}._")
    lines.append(
        f"_Calculated by {build.engine} with this visual's filters and slicers._"
        if build.computed
        else f"_Nothing here was estimated; {build.engine} was asked._"
    )
    return "\n".join(line for line in _squeeze(lines))


def _squeeze(lines: list[str]) -> list[str]:
    kept: list[str] = []
    for line in lines:
        if line == "" and (not kept or kept[-1] == ""):
            continue
        kept.append(line)
    return kept


def _finish(
    build: _Build, compiled: lr.Compiled, page_id: str, spec: Mapping[str, Any] | None, focus: Focus
) -> Explanation:
    steps = tuple(build.steps)
    text = _compose(build, steps, focus)
    formula = None
    if build.measure:
        formula = next(
            (
                str(m.get("Expression", ""))
                for m in build.report.measures
                if m.get("Name") == build.measure
            ),
            None,
        )
    return Explanation(
        page_id=page_id,
        visual_id=build.visual.id,
        visual=build.caption,
        kind=build.kind,
        point=dict(build.point),
        measure=build.measure,
        value=build.value,
        formatted=build.formatted,
        computed=build.computed,
        formula=formula,
        meaning=build.meaning,
        period=build.period,
        steps=steps,
        filters=tuple(build.filters),
        relationships=tuple(build.paths),
        formatting=build.colour,
        blank=build.blank,
        notes=tuple(dict.fromkeys(build.notes)),
        engine=build.engine,
        text=text,
    )


# --- chat ----------------------------------------------------------------------------------

_COLOURS = r"red|green|blue|orange|yellow|purple|pink|grey|gray|black|white|teal"
# Phrases that route a chat message here before the model (see is_explain_question).
CHAT_TRIGGERS: tuple[str, ...] = (
    # "Why does this card show 46.76%?", "why is this bar red?", "why is Store blank?"
    r"\bwhy\s+(?:does|do|did|is|are|was|were)\b[^?\n]{0,120}?(?:\d|%|\b(?:blank|empty|missing|"
    r"zero|negative)\b|\b(?:" + _COLOURS + r")\b|\bthis\s+(?:number|value|figure|bar|column|"
    r"point|row|cell|card|total|percentage|kpi)\b|\bso\s+(?:high|low|big|small)\b|\b(?:higher|"
    r"lower|bigger|smaller)\b)",
    # "How is Agents with Positive Change % calculated?"
    r"\bhow\s+(?:is|was|are|were|do\s+you|does\s+it|did\s+it|is\s+it)\b[^?\n]{0,100}?\b"
    r"(?:calculated|computed|worked\s+out|derived|arrived\s+at|made\s+up)\b",
    # "Explain this number", "explain the BottomN bar"
    r"\bexplain\s+(?:this|that|the|these|how)\b[^?\n]{0,60}?\b(?:number|value|figure|result|"
    r"bar|column|point|row|cell|card|total|percentage|kpi|colou?r|calculation)\b",
    r"\bwhere\s+does\b[^?\n]{0,100}?\bcome\s+from\b",
    r"\bwhat\s+makes\b[^?\n]{0,80}?\b(?:" + _COLOURS + r"|blank|so\s+high|so\s+low)\b",
)
_TRIGGER = re.compile("|".join(f"(?:{t})" for t in CHAT_TRIGGERS), re.I)
_COLOUR_ASKED = re.compile(rf"\b(?:{_COLOURS}|colou?r(?:ed)?)\b", re.I)
_BLANK_ASKED = re.compile(r"\b(?:blank|empty|missing)\b", re.I)
_NUMBER = re.compile(r"(?<![\w.])[-−]?\$?(\d[\d,]*(?:\.\d+)?)\s*(%|bn\b|[kKmMbB]\b)?")
_SCALE = {"%": 0.01, "k": 1e3, "m": 1e6, "b": 1e9, "bn": 1e9}


def is_explain_question(message: str) -> bool:
    """Whether a chat message asks why a number (or a colour, or a blank) is what it is."""
    return _TRIGGER.search(message) is not None


@dataclass(frozen=True)
class AskContext:
    """What chat knows when the question comes: the report and its engine, the page and the
    visual (and point) selected on the canvas, the canvas state and, when the caller has it,
    the page's render payload (without it the page is rendered here when needed)."""

    report: ImportedReport
    engine: Engine
    page_id: str | None = None
    visual_id: str | None = None
    point: Point | None = None
    request: lr.LiveReportRequest | None = None
    payload: Mapping[str, Any] | None = None


@dataclass
class _Candidate:
    visual_id: str
    caption: str
    kind: str
    row: int | None
    column: int
    point: Point | None
    value: Any
    formatted: str | None
    colour: str | None
    measure_label: str = ""

    def label(self) -> str:
        shown = self.formatted or _show(_cell(self.value))
        if self.point is None:
            return f"the value of **{self.caption}** ({shown})"
        what = " / ".join(_show(_cell(v)).strip('"') for v in self.point.values.values())
        mark = _MARKS.get(self.kind, "point")
        if self.kind == "table":
            return f"{self.measure_label} in the {what} row of **{self.caption}** ({shown})"
        return f"the {what} {mark} of **{self.caption}** ({shown})"


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9%$.]+", _clean(text).casefold()))


def _numbers(message: str) -> list[tuple[str, float, float]]:
    """(text, value, tolerance) for each number the message names ("46.76%", "73.5K")."""
    found = []
    for match in _NUMBER.finditer(message):
        digits, unit = match.group(1), (match.group(2) or "").casefold()
        try:
            number = float(digits.replace(",", ""))
        except ValueError:
            continue
        scale = _SCALE.get(unit, 1.0)
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        negative = match.group(0).lstrip().startswith(("-", "−"))
        value = (-number if negative else number) * scale
        found.append((match.group(0).strip(), value, 0.5 * 10 ** (-decimals) * scale))
    return found


def _candidates(
    report: ImportedReport, page_id: str, payload: Mapping[str, Any]
) -> list[_Candidate]:
    page = next((p for p in report.pages if p.id == page_id), None)
    by_id = {v.id: v for v in page.visuals} if page is not None else {}
    theme = payload.get("theme") if isinstance(payload.get("theme"), Mapping) else None
    found: list[_Candidate] = []
    visuals = payload.get("visuals")
    for item in visuals if isinstance(visuals, list) else []:
        if not isinstance(item, Mapping) or item.get("hidden") or item.get("error"):
            continue
        kind = str(item.get("kind") or "")
        if kind not in lr.DATA_KINDS or kind == "slicer":
            continue
        visual = by_id.get(str(item.get("id")))
        caption = _caption(visual) if visual is not None else str(item.get("title") or "")
        columns = _list(item.get("columns"))
        rows = _list(item.get("rows"))
        formatted_rows = _list(item.get("formatted_rows"))
        formatting = item.get("formatting") if isinstance(item.get("formatting"), Mapping) else {}
        assert isinstance(formatting, Mapping)
        if kind == "card":
            value_token = formatting.get("cardValue")
            if isinstance(value_token, Mapping) and value_token.get("show") is False:
                continue
        groups = [
            i
            for i, c in enumerate(columns)
            if isinstance(c, Mapping) and c.get("kind") == "column" and not c.get("hidden")
        ]
        shown = [
            i
            for i, c in enumerate(columns)
            if isinstance(c, Mapping) and c.get("kind") == "measure" and not c.get("hidden")
        ]
        for position, row in enumerate(rows[:100]):
            if not isinstance(row, list):
                continue
            cells = (
                formatted_rows[position]
                if position < len(formatted_rows) and isinstance(formatted_rows[position], list)
                else []
            )
            for column in shown:
                point = None
                if groups:
                    point = Point(
                        {str(columns[i].get("query_ref")): _cell(row[i]) for i in groups},
                        str(columns[column].get("query_ref") or ""),
                    )
                colour = _mark_colour(
                    item, [dict(c) for c in columns], row, column, theme, position
                )
                found.append(
                    _Candidate(
                        str(item.get("id")),
                        caption,
                        kind,
                        position,
                        column,
                        point,
                        row[column] if column < len(row) else None,
                        _clean(str(cells[column]))
                        if column < len(cells) and cells[column]
                        else None,
                        colour.color if colour is not None else None,
                        str(columns[column].get("label") or "").strip()
                        or str(columns[column].get("name") or ""),
                    )
                )
            if kind == "card":
                break
    return found


def _named(message: str, report: ImportedReport, page_id: str) -> list[str]:
    """Visuals the message names by caption, title or measure, longest name first."""
    page = next((p for p in report.pages if p.id == page_id), None)
    asked = f" {_norm(message)} "
    best: list[tuple[int, str]] = []
    for visual in page.visuals if page is not None else []:
        names = {visual.caption or "", visual.title}
        names |= {str(b.get("name")) for b in visual.bindings if b.get("kind") == "measure"}
        lengths = [len(n) for n in (_norm(x) for x in names) if len(n) >= 4 and f" {n} " in asked]
        if lengths:
            best.append((max(lengths), visual.id))
    if not best:
        return []
    top = max(length for length, _ in best)
    return [visual_id for length, visual_id in best if length == top]


def _matches_number(candidate: _Candidate, numbers: list[tuple[str, float, float]]) -> bool:
    for text, value, tolerance in numbers:
        if candidate.formatted and _norm(candidate.formatted).replace("$", "") == _norm(
            text
        ).replace("$", ""):
            return True
        raw = candidate.value
        numeric = isinstance(raw, int | float) and not isinstance(raw, bool)
        if numeric and abs(float(raw) - value) <= tolerance * 1.0000001:
            return True
    return False


def _category_hit(
    candidate: _Candidate, message: str, numbers: list[tuple[str, float, float]]
) -> bool:
    """Whether the message names the point's category ("the BottomN bar", "agent 201")."""
    if candidate.point is None:
        return False
    folded = f" {_norm(message)} "
    for value in candidate.point.values.values():
        if isinstance(value, str) and len(value) >= 2 and f" {_norm(value)} " in folded:
            return True
        if (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            and any(tol < 1 and v == float(value) for _, v, tol in numbers)
        ):
            return True
    return False


def _without_names(message: str, report: ImportedReport, page_id: str) -> str:
    """The message without the page's visual and measure names: "Number of Agents Sold above
    100K" names a card, not the number 100,000."""
    page = next((p for p in report.pages if p.id == page_id), None)
    names = {
        n
        for v in (page.visuals if page is not None else [])
        for n in (v.caption or "", v.title, *(str(b.get("name")) for b in v.bindings))
    }
    for name in sorted((n for n in names if len(n) >= 4), key=len, reverse=True):
        message = re.sub(re.escape(name), " ", message, flags=re.I)
    return message


def find_target(message: str, context: AskContext) -> tuple[str, Point | None] | str | None:
    """(visual id, point) the question is about, a question back when several fit (or none
    does), or None when the message names nothing on the page (another handler answers)."""
    report = context.report
    page_id = context.page_id or (report.pages[0].id if len(report.pages) == 1 else None)
    if page_id is None:
        return None
    numbers = _numbers(_without_names(message, report, page_id))
    colour_words = {
        w.casefold().replace("gray", "grey")
        for w in re.findall(rf"\b(?:{_COLOURS})\b", message, re.I)
    }
    blank = _BLANK_ASKED.search(message) is not None
    named = [context.visual_id] if context.visual_id else _named(message, report, page_id)
    page = next((p for p in report.pages if p.id == page_id), None)
    by_id = {v.id: v for v in page.visuals} if page is not None else {}
    if len(named) == 1 and context.point is not None:
        return named[0], context.point
    card_only = (
        len(named) == 1 and lr.kind_of(by_id[named[0]].visual_type) == "card"
        if (named and named[0] in by_id)
        else False
    )
    if card_only and not numbers:
        return named[0], None
    if not named and not numbers and not colour_words and not blank:
        return None
    payload = context.payload
    if payload is None:
        try:
            payload = lr.render_page(
                context.engine,
                report,
                page_id,
                context.request or lr.LiveReportRequest(),
                only=set(named) if named else None,
            )
        except _ENGINE_ERRORS:
            return None
    pool = _candidates(report, page_id, payload)
    if named:
        pool = [c for c in pool if c.visual_id in named]
    hits = [c for c in pool if _category_hit(c, message, [])]
    if hits:
        pool = hits
    elif numbers:
        # A number the visual shows wins over a category that happens to be the same number.
        shown = [c for c in pool if _matches_number(c, numbers)]
        pool = shown or [c for c in pool if _category_hit(c, message, numbers)]
        if not pool:
            said = ", ".join(t for t, _, _ in numbers)
            return (
                f"No visual on this page shows {said} right now (with its current filters "
                "and slicers). Select the number on the canvas and ask again."
            )
    if colour_words:
        coloured = [c for c in pool if colour_name(c.colour) in colour_words]
        if coloured:
            pool = coloured
        elif not named:
            return None
    if blank:
        blanks = [c for c in pool if c.value is None]
        if blanks:
            pool = blanks
    if not pool:
        return None
    places = {(c.visual_id, c.row) for c in pool}
    if len(places) == 1:
        # One card, or one row or bar (several of its values fit: the first shown is asked).
        return pool[0].visual_id, pool[0].point
    listed = "\n".join(f"- {c.label()}" for c in pool[:6])
    more = f"\n- and {len(pool) - 6} more" if len(pool) > 6 else ""
    return (
        f"Several numbers on this page fit that question:\n{listed}{more}\n\nSelect one on "
        "the canvas (or name the visual and the bar or row) and ask again."
    )


def answer_explanation(message: str, context: AskContext) -> Explanation | str | None:
    """The explanation a chat question asks for, a question back, or None (not an explain
    question, or nothing on the page it could be about: the model answers)."""
    if not is_explain_question(message):
        return None
    target = find_target(message, context)
    if target is None or isinstance(target, str):
        return target
    page_id = context.page_id or context.report.pages[0].id
    focus: Focus = (
        "colour"
        if _COLOUR_ASKED.search(message)
        else ("blank" if _BLANK_ASKED.search(message) else "value")
    )
    try:
        return explain_point(
            context.report,
            page_id,
            target[0],
            target[1],
            engine=context.engine,
            payload=context.payload,
            request=context.request,
            focus=focus,
        )
    except (LookupError, ValueError) as error:
        return f"Studio could not explain that number: {_reason(error)}"


def answer(message: str, context: AskContext) -> str | None:
    """A computed chat answer to "why is this number what it is", or None for the model."""
    found = answer_explanation(message, context)
    if isinstance(found, Explanation):
        return found.text
    return found
