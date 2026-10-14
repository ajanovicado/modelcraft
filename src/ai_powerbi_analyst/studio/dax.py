"""A deliberately small, read-only DAX subset evaluated against the reviewed
normalized snapshot. Anything outside the grammar fails fast naming the
supported patterns — the lab never guesses at semantics and never writes."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import NamedTuple

from .aggregates import AggregatePlanError, AggregateService, RelationshipPath
from .contracts import (
    AdvancedDaxProposal,
    AggregateFilter,
    DatasetSnapshot,
    DaxBreakdownResult,
    DaxDateField,
    DaxDateResult,
    DaxGroupSelection,
    DaxPreviewContext,
    DaxPreviewField,
    DaxResult,
    GovernedMeasureSpec,
    ReadOnlyAggregatePlan,
    RelationshipProposal,
)


class DaxSyntaxError(ValueError):
    """The expression is outside the supported lab subset."""


SUPPORTED_SYNTAX = (
    "Supported patterns: SUM/AVERAGE/MIN/MAX/DISTINCTCOUNT/COUNT('Table'[Column]), "
    "COUNTROWS('Table'), DIVIDE(a, b) or a / b and a - b over two of those aggregates, "
    "[Measure Name], "
    "CALCULATE(expression, 'Table'[Column] = \"value\", ...), "
    "EVALUATE 'Table', EVALUATE TOPN(n, 'Table'), "
    "EVALUATE SUMMARIZECOLUMNS('Table'[Group], \"Label\", expression), "
    "EVALUATE VALUES('Table'[Column]), ORDER BY output columns ASC/DESC; "
    'grand totals via ROLLUPADDISSUBTOTAL(ROLLUPGROUP(group columns), "IsGrandTotal")'
)

_AGGS = {
    "sum": "sum",
    "average": "average",
    "avg": "average",
    "min": "minimum",
    "minimum": "minimum",
    "max": "maximum",
    "maximum": "maximum",
    "distinctcount": "distinct_count",
    "count": "count",
}
_OP_LABELS = {
    "sum": "SUM",
    "average": "AVERAGE",
    "minimum": "MIN",
    "maximum": "MAX",
    "distinct_count": "DISTINCTCOUNT",
    "count": "COUNT",
}
_FILTER_OPS = {"=": "eq", "<>": "neq", "!=": "neq", ">": "gt", ">=": "gte", "<": "lt", "<=": "lte"}

_TOKEN = re.compile(
    r"\s*(?:"
    r"(?P<table>'(?:[^']|'')*')"
    r"|(?P<string>\"(?:[^\"]|\"\")*\")"
    r"|(?P<ref>\[[^\]\r\n]+\])"
    r"|(?P<number>-?\d+(?:\.\d+)?)"
    r"|(?P<ident>[A-Za-z_][A-Za-z0-9_.]*)"
    r"|(?P<op>&&|<>|!=|>=|<=|=|>|<|/|-|\(|\)|,|\{|\})"
    r")"
)


@dataclass
class _Agg:
    op: str
    table: str | None
    column: str | None


@dataclass
class _Ratio:
    numerator: Node
    denominator: Node


@dataclass
class _Difference:
    numerator: Node
    denominator: Node


@dataclass
class _MeasureRef:
    name: str


@dataclass
class _Calc:
    inner: Node
    filters: list[_Filter]


@dataclass
class _Filter:
    table: str
    column: str
    op: str
    value: object


@dataclass
class _Scan:
    table: str
    limit: int | None


@dataclass
class _Values:
    table: str
    column: str


@dataclass
class _Summarize:
    groups: list[tuple[str, str]]
    label: str
    value: Node
    total_label: str | None = None


@dataclass
class _Ordered:
    inner: Node
    columns: list[tuple[str | None, str, str]]


type Node = (
    _Agg | _Ratio | _Difference | _MeasureRef | _Calc | _Scan | _Values | _Summarize | _Ordered
)

_Token = tuple[str, str]  # (kind, text)


def _tokenize(expression: str) -> list[_Token]:
    tokens: list[_Token] = []
    position = 0
    while position < len(expression):
        match = _TOKEN.match(expression, position)
        if match is None:
            raise DaxSyntaxError(
                f"Unexpected text near {expression[position : position + 20]!r}. {SUPPORTED_SYNTAX}"
            )
        position = match.end()
        kind = match.lastgroup or ""
        text = match.group()
        if kind == "table":
            tokens.append(("table", text.strip()[1:-1].replace("''", "'")))
        elif kind == "string":
            tokens.append(("string", text.strip()[1:-1].replace('""', '"')))
        elif kind == "ref":
            tokens.append(("ref", text.strip()[1:-1]))
        elif kind == "number":
            tokens.append(("number", text.strip()))
        elif kind == "ident":
            tokens.append(("ident", text.strip()))
        else:
            tokens.append(("op", text.strip()))
    return tokens


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self, offset: int = 0) -> _Token | None:
        index = self.pos + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def take(self, kind: str | None = None, text: str | None = None) -> _Token:
        token = self.peek()
        if token is None:
            raise DaxSyntaxError(f"Unexpected end of expression. {SUPPORTED_SYNTAX}")
        if kind is not None and token[0] != kind:
            raise DaxSyntaxError(f"Expected {kind}, found {token[1]!r}. {SUPPORTED_SYNTAX}")
        if text is not None and token[1].casefold() != text.casefold():
            raise DaxSyntaxError(f"Expected {text!r}, found {token[1]!r}. {SUPPORTED_SYNTAX}")
        self.pos += 1
        return token

    def at_op(self, text: str) -> bool:
        token = self.peek()
        return token == ("op", text)

    def take_op(self, text: str) -> None:
        self.take("op", text)

    def column_ref(self) -> tuple[str, str]:
        table = self.table_name()
        ref = self.take("ref")[1]
        return table, ref

    def table_name(self) -> str:
        # Simple DAX table identifiers may omit quotes. Resolution still goes
        # through the same reviewed schema and bounded aggregate compiler.
        token = self.peek()
        if token and token[0] == "ident":
            self.pos += 1
            return token[1]
        return self.take("table")[1]

    def parse(self) -> Node:
        token = self.peek()
        if token and token[0] == "ident" and token[1].casefold() == "evaluate":
            self.pos += 1
        token = self.peek()
        if token is None:
            raise DaxSyntaxError(f"Empty expression. {SUPPORTED_SYNTAX}")
        if token == ("op", "{"):
            self.pos += 1
            node = self.scalar()
            self.take_op("}")
        elif token[0] == "ident" and token[1].casefold() == "topn":
            node = self.topn()
        elif token[0] == "table" or (token[0] == "ident" and self.peek(1) is None):
            node = _Scan(table=self.table_name(), limit=None)
        elif token[0] == "ident" and token[1].casefold() == "summarizecolumns":
            node = self.summarize()
        elif token[0] == "ident" and token[1].casefold() == "values":
            node = self.values()
        else:
            node = self.scalar()
        if (following := self.peek()) and following[1].casefold() == "order":
            self.take("ident", "order")
            self.take("ident", "by")
            columns = []
            while True:
                if (following := self.peek()) and following[0] == "ref":
                    table, column = None, self.take("ref")[1]
                else:
                    table, column = self.column_ref()
                direction = "asc"
                if (following := self.peek()) and following[1].casefold() in {"asc", "desc"}:
                    direction = self.take("ident")[1].casefold()
                columns.append((table, column, direction))
                if len(columns) > 5:
                    raise DaxSyntaxError("The lab supports at most 5 sort columns.")
                if not self.at_op(","):
                    break
                self.take_op(",")
            node = _Ordered(node, columns)
        if self.peek() is not None:
            raise DaxSyntaxError(
                f"Unexpected text after expression near {self.peek()}. {SUPPORTED_SYNTAX}"
            )
        return node

    def topn(self) -> Node:
        self.take("ident", "topn")
        self.take_op("(")
        limit = int(self.take("number")[1])
        self.take_op(",")
        node = _Scan(table=self.table_name(), limit=limit)
        self.take_op(")")
        return node

    def values(self) -> Node:
        self.take("ident", "values")
        self.take_op("(")
        table, column = self.column_ref()
        self.take_op(")")
        return _Values(table=table, column=column)

    def summarize(self) -> Node:
        self.take("ident", "summarizecolumns")
        self.take_op("(")
        groups: list[tuple[str, str]] = []
        label: str | None = None
        value: Node | None = None
        total_label: str | None = None
        while True:
            token = self.peek()
            if token is None:
                raise DaxSyntaxError(f"SUMMARIZECOLUMNS is not closed. {SUPPORTED_SYNTAX}")
            if token[0] == "table" or (
                token[0] == "ident" and (following := self.peek(1)) and following[0] == "ref"
            ):
                if total_label:
                    raise DaxSyntaxError("Put all grand-total group columns inside ROLLUPGROUP.")
                groups.append(self.column_ref())
            elif token[0] == "ident" and token[1].casefold() == "rollupaddissubtotal":
                if groups or total_label:
                    raise DaxSyntaxError("The lab supports one grand-total group, not subtotals.")
                self.take("ident", "rollupaddissubtotal")
                self.take_op("(")
                if (following := self.peek()) and following[1].casefold() == "rollupgroup":
                    self.take("ident", "rollupgroup")
                    self.take_op("(")
                    groups.append(self.column_ref())
                    while self.at_op(","):
                        self.take_op(",")
                        groups.append(self.column_ref())
                    self.take_op(")")
                else:
                    groups.append(self.column_ref())
                self.take_op(",")
                total_label = self.take("string")[1]
                if not total_label:
                    raise DaxSyntaxError("The grand-total flag needs a name.")
                self.take_op(")")
            elif token[0] == "string":
                if label is not None:
                    raise DaxSyntaxError("One labelled expression per SUMMARIZECOLUMNS query.")
                label = self.take("string")[1]
                self.take_op(",")
                value = self.scalar()
            elif self.at_op(")"):
                self.pos += 1
                break
            else:
                raise DaxSyntaxError(
                    f"Unexpected {token[1]!r} inside SUMMARIZECOLUMNS. {SUPPORTED_SYNTAX}"
                )
            token = self.peek()
            if self.at_op(")"):
                self.pos += 1
                break
            self.take_op(",")
        if not groups:
            raise DaxSyntaxError("SUMMARIZECOLUMNS needs at least one 'Table'[Group] column.")
        if value is None or label is None:
            raise DaxSyntaxError(
                "SUMMARIZECOLUMNS needs a labelled expression, e.g. \"Total\", SUM('T'[Col])."
            )
        if len(groups) > 3:
            raise DaxSyntaxError("The lab supports at most 3 group columns.")
        return _Summarize(groups=groups, label=label, value=value, total_label=total_label)

    def scalar(self) -> Node:
        node = self.quotient()
        while self.at_op("-"):
            self.pos += 1
            node = _Difference(numerator=node, denominator=self.quotient())
        return node

    def quotient(self) -> Node:
        node = self.term()
        while self.at_op("/"):
            self.pos += 1
            node = _Ratio(numerator=node, denominator=self.term())
        return node

    def term(self) -> Node:
        token = self.peek()
        if token is None:
            raise DaxSyntaxError(f"Unexpected end of expression. {SUPPORTED_SYNTAX}")
        if self.at_op("("):
            self.pos += 1
            node = self.scalar()
            self.take_op(")")
            return node
        if token[0] == "ref":
            self.pos += 1
            return _MeasureRef(name=token[1])
        if token[0] == "table" or (
            token[0] == "ident" and (following := self.peek(1)) and following[0] == "ref"
        ):
            table, column = self.column_ref()
            return _Values(table=table, column=column)
        if token[0] == "ident":
            name = token[1].casefold()
            if name in _AGGS:
                self.pos += 1
                self.take_op("(")
                table, column = self.column_ref()
                self.take_op(")")
                return _Agg(op=_AGGS[name], table=table, column=column)
            if name == "countrows":
                self.pos += 1
                self.take_op("(")
                following = self.peek()
                if following and following[0] == "ident" and following[1].casefold() == "filter":
                    # COUNTROWS(FILTER(T, T[c] = v)) counts the same rows as
                    # CALCULATE(COUNTROWS(T), T[c] = v) for simple column predicates.
                    table, predicates = self.filter_table()
                    self.take_op(")")
                    counted = _Agg(op="count", table=table, column=None)
                    return _Calc(inner=counted, filters=predicates)
                table = self.table_name()
                self.take_op(")")
                return _Agg(op="count", table=table, column=None)
            if name == "calculate":
                self.pos += 1
                self.take_op("(")
                inner = self.scalar()
                filters: list[_Filter] = []
                while self.at_op(","):
                    self.pos += 1
                    token = self.peek()
                    if token and token[0] == "ident" and token[1].casefold() == "filter":
                        filters.extend(self.filter_table()[1])
                    else:
                        filters.append(self.filter_expr())
                self.take_op(")")
                # Filters are optional in DAX. This aggregate-only subset has no
                # row context to transition, so CALCULATE(expr) preserves expr.
                return _Calc(inner=inner, filters=filters)
            if name == "divide":
                self.pos += 1
                self.take_op("(")
                numerator = self.scalar()
                self.take_op(",")
                denominator = self.scalar()
                self.take_op(")")
                return _Ratio(numerator=numerator, denominator=denominator)
            raise DaxSyntaxError(f"Unsupported function {token[1]!r}. {SUPPORTED_SYNTAX}")
        raise DaxSyntaxError(f"Unexpected {token[1]!r}. {SUPPORTED_SYNTAX}")

    def filter_table(self) -> tuple[str, list[_Filter]]:
        """FILTER(T, T[c] = v && T[d] <> w): the table and its simple column predicates."""
        self.take("ident", "filter")
        self.take_op("(")
        table = self.table_name()
        self.take_op(",")
        predicates = [self.filter_expr()]
        while self.at_op("&&"):
            self.pos += 1
            predicates.append(self.filter_expr())
        self.take_op(")")
        if any(item.table.casefold() != table.casefold() for item in predicates):
            raise DaxSyntaxError(
                "FILTER predicates must use the filtered table's own columns in this lab."
            )
        return table, predicates

    def filter_expr(self) -> _Filter:
        table, column = self.column_ref()
        operator = self.take("op")[1]
        if operator not in _FILTER_OPS:
            raise DaxSyntaxError(f"Unsupported filter operator {operator!r}; use = <> > >= < <=.")
        lit = self.literal()
        return _Filter(table=table, column=column, op=_FILTER_OPS[operator], value=lit)

    def literal(self) -> object:
        token = self.peek()
        if token is None:
            raise DaxSyntaxError(f"Expected a literal value. {SUPPORTED_SYNTAX}")
        if token[0] == "string":
            self.pos += 1
            return token[1]
        if token[0] == "number":
            self.pos += 1
            return float(token[1]) if "." in token[1] else int(token[1])
        if token[0] == "ident" and token[1].casefold() in {"true", "false"}:
            self.pos += 1
            # DAX writes the logical constants as functions: TRUE() and FALSE().
            if self.at_op("(") and self.peek(1) == ("op", ")"):
                self.pos += 2
            return token[1].casefold() == "true"
        raise DaxSyntaxError(f"Expected a literal, found {token[1]!r}. {SUPPORTED_SYNTAX}")


# "Profit Margin % = DIVIDE(...)" is how Power BI (and Studio's chat) writes a measure.
# The name may not contain brackets, quotes or parentheses, so comparisons such as
# 'Sales'[Region] = "East" never match.
_MEASURE_HEADER = re.compile(
    r"^\s*(?!(?:VAR|RETURN|EVALUATE|DEFINE)\b)"
    r"[A-Za-z_][^=\n\[\]'\"(){}]{0,120}?\s*:?=(?!=)\s*",
    re.I,
)


def measure_body(expression: str) -> str:
    """The expression without a leading ``Name =`` definition header."""
    return _MEASURE_HEADER.sub("", expression, count=1).strip()


_PERCENT_NAME = re.compile(r"%|\b(?:margin|rate|ratio|share|percent|pct)\b", re.I)


def percent_format_for(name: str, expression: str) -> str | None:
    """Percent display for ratios named as percentages; DIVIDE(Sales, Units) stays a number."""
    try:
        ratio = isinstance(parse_dax(expression), _Ratio)
    except DaxSyntaxError:
        return None
    return "0.0%" if ratio and _PERCENT_NAME.search(name) else None


def governed_preview_equivalent(
    snapshot: DatasetSnapshot, measure_id: str, name: str, expression: str
) -> list[GovernedMeasureSpec] | None:
    """Preview-only governed specs for a plain aggregate or a same-table DIVIDE of two.

    Returns ``[target, *components]`` where ``target.id == measure_id`` so designer
    visuals preview a lab-saved measure through the bounded aggregate path. The saved
    advanced-DAX proposal is untouched and still needs review; anything else, such as
    CALCULATE or measure references, returns None and keeps the honest empty preview.
    """
    try:
        node = parse_dax(expression)
        catalog = _Catalog(snapshot)

        def spec(item: Node, spec_id: str, label: str) -> GovernedMeasureSpec | None:
            if not isinstance(item, _Agg) or item.table is None or item.column is None:
                return None
            table_id = catalog.table_id(item.table)
            return GovernedMeasureSpec(
                id=spec_id,
                name=label,
                operation=item.op,  # type: ignore[arg-type]
                table_id=table_id,
                column_id=catalog.column_id(table_id, item.column),
            )

        if isinstance(node, _Agg):
            single = spec(node, measure_id, name)
            return [single] if single is not None else None
        if isinstance(node, _Ratio):
            numerator = spec(node.numerator, f"{measure_id}__num"[:96], f"{name} numerator")
            denominator = spec(node.denominator, f"{measure_id}__den"[:96], f"{name} denominator")
            if numerator is None or denominator is None:
                return None
            if numerator.table_id != denominator.table_id:
                return None
            target = GovernedMeasureSpec(
                id=measure_id,
                name=name,
                operation="ratio",
                table_id=numerator.table_id,
                numerator_measure_id=numerator.id,
                denominator_measure_id=denominator.id,
            )
            return [target, numerator, denominator]
    except (DaxSyntaxError, AggregatePlanError, ValueError):
        return None
    return None


def parse_dax(expression: str) -> Node:
    cleaned = measure_body(expression).rstrip(";").strip()
    if not cleaned:
        raise DaxSyntaxError(f"Empty expression. {SUPPORTED_SYNTAX}")
    return _Parser(_tokenize(cleaned)).parse()


type _Scope = dict[tuple[str, str], list[_Filter]]


def _in_context(node: Node, scope: _Scope) -> tuple[Node, _Scope]:
    """``node`` under its CALCULATE layers, with the column filters in effect there.

    As in DAX, an inner CALCULATE filter on a column replaces outer ones on it.
    """
    while isinstance(node, _Calc):
        own: _Scope = {}
        for item in node.filters:
            own.setdefault((item.table.casefold(), item.column.casefold()), []).append(item)
        scope = {**scope, **own}
        node = node.inner
    return node, scope


def _ratios(node: Node) -> Iterator[tuple[Node, tuple[Node, _Scope], tuple[Node, _Scope]]]:
    """Every ratio or difference in a formula or query, each side in its filter context."""
    stack: list[tuple[Node, _Scope]] = [(node, {})]
    while stack:
        node, scope = _in_context(*stack.pop())
        if isinstance(node, _Ordered):
            stack.append((node.inner, scope))
        elif isinstance(node, _Summarize):
            stack.append((node.value, scope))
        elif isinstance(node, (_Ratio, _Difference)):
            numerator = _in_context(node.numerator, scope)
            denominator = _in_context(node.denominator, scope)
            yield node, numerator, denominator
            stack.extend((numerator, denominator))


def _signature(node: Node, scope: _Scope) -> str:
    # Structural: quoting, spacing, name case and a repeated filter do not change what
    # DAX computes.
    filters = sorted(
        {re.sub(r"\s", "", repr(item)).casefold() for items in scope.values() for item in items}
    )
    return re.sub(r"\s", "", repr(node)).casefold() + "|" + ";".join(filters)


def _constant_ratio(numerator: tuple[Node, _Scope], denominator: tuple[Node, _Scope]) -> str | None:
    if _signature(*numerator) == _signature(*denominator):
        return "divides a value by itself under the same filters, so it is always 1"
    for core, scope in (numerator, denominator):
        if (
            isinstance(core, _Agg)
            and core.op == "distinct_count"
            and core.column is not None
            and any(
                item.op == "eq"
                for item in scope.get(((core.table or "").casefold(), core.column.casefold()), [])
            )
        ):
            return (
                f"counts distinct {core.column} values under a filter that fixes {core.column} "
                "to one value, so that count is always 1"
            )
    return None


def degenerate_ratio(expression: str) -> str | None:
    """Why a ratio in the formula is constant in DAX filter context, or None.

    Measured: CALCULATE(DIVIDE(SUM(Sales), SUM(Sales)), Segment = "Consumer") ran
    and was accepted as a "share of total sales", which is always 1.
    """
    try:
        node = parse_dax(expression)
    except DaxSyntaxError:
        return None
    return next(
        (
            reason
            for ratio, numerator, denominator in _ratios(node)
            if isinstance(ratio, _Ratio) and (reason := _constant_ratio(numerator, denominator))
        ),
        None,
    )


_FILTER_TEXT = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def _filter_text(item: _Filter) -> str:
    value = item.value
    literal = (
        ("TRUE()" if value else "FALSE()")
        if isinstance(value, bool)
        else '"' + value.replace('"', '""') + '"'
        if isinstance(value, str)
        else str(value)
    )
    table = "'" + item.table.replace("'", "''") + "'"
    return f"{table}[{item.column}] {_FILTER_TEXT[item.op]} {literal}"


class ShareBase(NamedTuple):
    """The value x of DIVIDE(CALCULATE(x, part), x) and the filters it was written under."""

    value: str  # SUM('T'[c]), COUNT('T'[c]), DISTINCTCOUNT('T'[c]), COUNTROWS('T') or [Measure]
    filters: list[tuple[str, str]]  # (column name, DAX filter text)
    table: str | None
    column: str | None


def share_base(expression: str) -> ShareBase | None:
    """x for a formula that cannot be a share yet: a constant top-level ratio or one aggregate.

    None for a real ratio, a [Measure] alone (it may already be the share), a
    query, an average or text outside the lab subset.
    """
    try:
        core, scope = _in_context(parse_dax(expression), {})
    except DaxSyntaxError:
        return None
    ratio = core if isinstance(core, _Ratio) else None
    if ratio is not None:
        numerator = _in_context(ratio.numerator, scope)
        if _constant_ratio(numerator, _in_context(ratio.denominator, scope)) is None:
            return None
        core, scope = numerator
    filters = [(item.column, _filter_text(item)) for items in scope.values() for item in items]
    if isinstance(core, _MeasureRef):
        # Imported models are told to reuse their measures: keep the model's.
        return ShareBase(f"[{core.name}]", filters, None, None) if ratio is not None else None
    if not isinstance(core, _Agg) or core.op not in {"sum", "count", "distinct_count"}:
        return None
    table = "'" + (core.table or "").replace("'", "''") + "'"
    value = (
        f"{_OP_LABELS[core.op]}({table}[{core.column}])"
        if core.column is not None
        else f"COUNTROWS({table})"
    )
    return ShareBase(value, filters, core.table, core.column)


def plain_aggregate(expression: str) -> bool:
    """One aggregate, averages included, under optional CALCULATE filters: no ratio yet."""
    try:
        return isinstance(_in_context(parse_dax(expression), {})[0], _Agg)
    except DaxSyntaxError:
        return False


class _Catalog:
    def __init__(
        self,
        snapshot: DatasetSnapshot,
        relationships: Iterable[RelationshipProposal] = (),
    ) -> None:
        self.relationships = list(relationships)
        # Only an imported report's own joins are followed across several hops: Power BI
        # already propagates them, and nothing evaluated over them is saved or built. A
        # draft's joins stay one hop, the only route the build check accepts for a saved
        # measure (planner.validate_cross_table_bindings).
        self.follows_paths = bool(self.relationships) and all(
            _BUILT_IN_POWER_BI in item.evidence for item in self.relationships
        )
        self.tables = {item.table.id: item.table for item in snapshot.tables}
        self.by_name: dict[str, str] = {}
        for table in self.tables.values():
            for key in (table.name, table.id):
                folded = key.casefold()
                if folded in self.by_name and self.by_name[folded] != table.id:
                    raise AggregatePlanError(f"Ambiguous table name: {key}")
                self.by_name[folded] = table.id

    def table_id(self, name: str) -> str:
        resolved = self.by_name.get(name.casefold())
        if resolved is None:
            known = ", ".join(sorted(table.name for table in self.tables.values()))
            raise DaxSyntaxError(f"Unknown table {name!r}. Tables in this snapshot: {known}")
        return resolved

    def column_id(self, table_id: str, name: str) -> str:
        folded = name.casefold()
        matches = [
            column
            for column in self.tables[table_id].columns
            if column.name.casefold() == folded or column.id.casefold() == folded
        ]
        if len(matches) != 1:
            known = ", ".join(sorted(c.name for c in self.tables[table_id].columns))
            raise DaxSyntaxError(
                f"Unknown column {name!r} on {self.tables[table_id].name}. Columns: {known}"
            )
        return matches[0].id

    def column_name(self, table_id: str, column_id: str) -> str:
        for column in self.tables[table_id].columns:
            if column.id == column_id:
                return column.name
        return column_id

    def column_type(self, table_id: str, column_id: str) -> str:
        for column in self.tables[table_id].columns:
            if column.id == column_id:
                return column.data_type
        return "string"

    def related(self, fact_table_id: str, other_table_id: str) -> RelationshipPath:
        """The approved relationship path through which ``other_table_id`` filters the fact.

        A filter flows one-to-many along each hop, so in an imported report DimEmployee
        reaches FactSales through DimStore. The shortest path wins; two equally short
        ones are ambiguous and refused rather than guessed, since a wrong join gives a
        confident wrong number. A draft's filter must come through one direct
        relationship, as before paths existed: a two-hop lab measure would otherwise
        save, then fail the build check. Power BI builds only approved relationships;
        a formula evaluated through a pending one would return the unfiltered total
        once built.
        """
        fact = self.tables[fact_table_id].name
        other = self.tables[other_table_id].name
        # Keyed by id: a relationship listed twice is one route, not an ambiguity.
        candidates = list(
            {
                item.id: item
                for item in self.relationships
                if item.cardinality in {"many_to_one", "one_to_one"}
                and item.is_active
                and not item.blockers
                and item.from_table_id in self.tables
                and item.to_table_id in self.tables
                and (
                    self.follows_paths
                    or (item.from_table_id == fact_table_id and item.to_table_id == other_table_id)
                )
            }.values()
        )
        approved = [item for item in candidates if item.review_status == "approved"]
        paths = _shortest_paths(approved, fact_table_id, other_table_id)
        if len(paths) == 1:
            return paths[0]
        if paths:
            routes = "; ".join(self._route(path) for path in paths)
            raise DaxSyntaxError(
                f"{other} filters {fact} through more than one relationship path ({routes}). "
                "Power BI cannot tell which one applies; deactivate one of them, or filter "
                "a table on the path you mean."
            )
        pending = _shortest_paths(candidates, fact_table_id, other_table_id)
        if pending and len(pending[0]) == 1:
            raise DaxSyntaxError(
                f"Include the {fact} → {other} relationship in Review & build and approve it; "
                f"filters on {other} reach {fact} only through an approved relationship."
            )
        if pending:
            missing = [item for item in pending[0] if item.review_status != "approved"]
            hops = ", ".join(
                f"{self.tables[item.from_table_id].name} → {self.tables[item.to_table_id].name}"
                for item in missing
            )
            plural = len(missing) > 1
            raise DaxSyntaxError(
                f"Include the {hops} relationship{'s' if plural else ''} in Review & build and "
                f"approve {'them' if plural else 'it'}; filters on {other} reach {fact} only "
                f"through approved relationships ({self._route(pending[0])})."
            )
        raise DaxSyntaxError(
            f"No active many-to-one relationship lets {other} filter {fact}. "
            "Filters and groups must come from the aggregate's table or a table it relates to."
        )

    def _route(self, path: RelationshipPath) -> str:
        """FactSales[StoreKey] → DimStore[StoreKey], DimStore[EmployeeKey] → DimEmployee[...]."""
        return ", ".join(
            f"{self.tables[item.from_table_id].name}"
            f"[{self.column_name(item.from_table_id, item.from_column_id)}] → "
            f"{self.tables[item.to_table_id].name}"
            f"[{self.column_name(item.to_table_id, item.to_column_id)}]"
            for item in path
        )


# report_import marks the report author's own joins with this evidence.
_BUILT_IN_POWER_BI = "Imported from the original Power BI model"


def _shortest_paths(
    relationships: list[RelationshipProposal], start: str, goal: str
) -> list[RelationshipPath]:
    """Up to two shortest many-to-one paths from ``start`` to ``goal``.

    Breadth-first by hop count; each table keeps at most two routes, which is
    enough to tell a unique path from an ambiguous one without enumerating them all.
    """
    frontier: dict[str, list[RelationshipPath]] = {start: [()]}
    seen = {start}
    while frontier:
        reached: dict[str, list[RelationshipPath]] = {}
        for table_id, routes in frontier.items():
            for item in relationships:
                if item.from_table_id != table_id or item.to_table_id in seen:
                    continue
                extended = reached.setdefault(item.to_table_id, [])
                extended.extend((*route, item) for route in routes)
                del extended[2:]
        if goal in reached:
            return reached[goal]
        seen.update(reached)
        frontier = reached
    return []


@dataclass
class _Compiled:
    plan: ReadOnlyAggregatePlan | None = None  # None => raw table scan
    scan_table: str | None = None
    scan_limit: int | None = None
    label: str = "value"
    group_names: list[str] | None = None
    description: str = ""
    dependencies: list[str] | None = None
    order_by: list[tuple[str, str]] = field(default_factory=list)
    total_label: str | None = None
    # Filters on a related one-side table ('Returns'[Returned] = TRUE()), each applied
    # through its approved relationship path; plan.filters stay on the aggregate's table.
    related_filters: list[tuple[RelationshipPath, AggregateFilter]] = field(default_factory=list)
    # Set when SUMMARIZECOLUMNS groups by a related table (People[Person]), possibly
    # several hops away (DimEmployee via DimStore).
    group_path: RelationshipPath | None = None
    # Related-table filters that apply to a ratio's numerator alone (return rate).
    numerator_related_filters: list[tuple[RelationshipPath, AggregateFilter]] = field(
        default_factory=list
    )


def _added_filters[T](numerator: list[T], denominator: list[T]) -> list[T] | None:
    """The numerator's extra filters when it narrows the denominator, else None.

    DIVIDE(CALCULATE(x, f), x) is a share of the whole; the two sides may not
    filter different, unrelated row sets.
    """
    extra = list(numerator)
    for item in denominator:
        if item not in extra:
            return None
        extra.remove(item)
    return extra


def _combined_plan(
    operation: str, left: ReadOnlyAggregatePlan, right: ReadOnlyAggregatePlan, subject: str
) -> ReadOnlyAggregatePlan:
    """DIVIDE or subtraction of two single aggregates over the same rows.

    Each side keeps its own aggregate, so average order value is
    DIVIDE(SUM(Sales), DISTINCTCOUNT(Order ID)) rather than a SUM/SUM ratio.
    """
    if (
        left.operation in {"ratio", "difference"}
        or right.operation in {"ratio", "difference"}
        or left.group_by_column_ids
        or right.group_by_column_ids
    ):
        raise DaxSyntaxError(
            f"{subject} supports two single aggregates (SUM, AVERAGE, MIN, MAX, COUNT, "
            "DISTINCTCOUNT, COUNTROWS or a measure over one) in this lab subset."
        )
    return ReadOnlyAggregatePlan.model_validate(
        {
            **left.model_dump(),
            "operation": operation,
            "denominator_column_id": right.value_column_id,
            # Plain SUM/SUM keeps its original plan shape.
            "numerator_operation": None if left.operation == "sum" else left.operation,
            "denominator_operation": None if right.operation == "sum" else right.operation,
        }
    )


def _operand_label(catalog: _Catalog, plan: ReadOnlyAggregatePlan, label: str) -> str:
    if plan.operation == "sum" and plan.value_column_id:
        return catalog.column_name(plan.table_id, plan.value_column_id)
    return label


def _compile(
    node: Node,
    catalog: _Catalog,
    measures: dict[str, GovernedMeasureSpec | AdvancedDaxProposal],
    top_n: int,
    depth: int,
) -> _Compiled:
    if depth > 3:
        raise DaxSyntaxError("Measure references are nested too deeply.")
    if isinstance(node, _Ordered):
        compiled = _compile(node.inner, catalog, measures, top_n, depth)
        if not isinstance(node.inner, (_Summarize, _Values)) or compiled.plan is None:
            raise DaxSyntaxError("ORDER BY supports grouped results and VALUES in this lab.")
        names = compiled.group_names or []
        aliases = {name.casefold(): f"group_{i + 1}" for i, name in enumerate(names)}
        aliases[compiled.label.casefold()] = "value"
        if compiled.total_label:
            aliases[compiled.total_label.casefold()] = "is_grand_total"
        group_table = (
            compiled.group_path[-1].to_table_id if compiled.group_path else compiled.plan.table_id
        )
        for table, name, direction in node.columns:
            if table is not None and (
                catalog.table_id(table) != group_table
                or name.casefold() not in {n.casefold() for n in names}
            ):
                raise DaxSyntaxError("ORDER BY must reference a group column or output label.")
            alias = aliases.get(name.casefold())
            if alias is None:
                raise DaxSyntaxError(f"Unknown ORDER BY column [{name}]. Use a result column.")
            compiled.order_by.append((alias, direction))
        compiled.description += " ORDER BY " + ", ".join(
            f"{name} {direction.upper()}" for _, name, direction in node.columns
        )
        return compiled
    if isinstance(node, _Scan):
        table_id = catalog.table_id(node.table)
        return _Compiled(
            scan_table=table_id,
            scan_limit=min(node.limit or top_n, 100),
            description=f"EVALUATE '{catalog.tables[table_id].name}'",
            dependencies=[table_id],
        )
    if isinstance(node, _Values):
        table_id = catalog.table_id(node.table)
        column_id = catalog.column_id(table_id, node.column)
        plan = ReadOnlyAggregatePlan(
            table_id=table_id,
            operation="count",
            group_by_column_ids=[column_id],
            top_n=top_n,
        )
        name = catalog.column_name(table_id, column_id)
        return _Compiled(
            plan=plan,
            label="Rows",
            group_names=[name],
            description=f"VALUES('{catalog.tables[table_id].name}'[{name}])",
            dependencies=[table_id, column_id],
        )
    if isinstance(node, _Summarize):
        compiled = _compile(node.value, catalog, measures, top_n, depth)
        if compiled.plan is None:
            raise DaxSyntaxError("SUMMARIZECOLUMNS needs an aggregate expression, not a scan.")
        group_ids: list[str] = []
        names = []
        group_tables: set[str] = set()
        for table_name, column_name in node.groups:
            group_table = catalog.table_id(table_name)
            group_tables.add(group_table)
            column_id = catalog.column_id(group_table, column_name)
            group_ids.append(column_id)
            names.append(catalog.column_name(group_table, column_id))
        if len(group_tables) != 1:
            raise DaxSyntaxError("Group columns must come from one table in this lab subset.")
        (group_table,) = group_tables
        # In DAX a CALCULATE filter on a grouped column replaces that group's filter,
        # so every row would show the filtered value; the lab would instead AND them.
        filtered_here = (
            {item.column_id for item in [*compiled.plan.filters, *compiled.plan.numerator_filters]}
            if group_table == compiled.plan.table_id
            else {
                item.column_id
                for path, item in [
                    *compiled.related_filters,
                    *compiled.numerator_related_filters,
                ]
                if path[-1].to_table_id == group_table
            }
        )
        if filtered_here & set(group_ids):
            raise DaxSyntaxError(
                "A CALCULATE filter on a grouped column replaces the grouping in DAX. "
                "Group by a different column, or filter the whole query instead."
            )
        group_path = None
        if group_table != compiled.plan.table_id:
            # People[Person] groups Orders through the approved Orders -> People join;
            # DimEmployee groups FactSales through DimStore.
            if node.total_label:
                raise DaxSyntaxError(
                    "Grand totals need group columns from the aggregate's table in this lab."
                )
            group_path = catalog.related(compiled.plan.table_id, group_table)
        output_names = [*names, node.label, *([node.total_label] if node.total_label else [])]
        if len({name.casefold() for name in output_names}) != len(output_names):
            raise DaxSyntaxError("Group columns and result labels must be distinct.")
        plan = compiled.plan.model_copy(update={"group_by_column_ids": group_ids})
        return _Compiled(
            plan=plan,
            label=node.label,
            group_names=names,
            description=f"{compiled.description} BY {', '.join(names)}"
            + (" WITH GRAND TOTAL" if node.total_label else ""),
            dependencies=compiled.dependencies
            if group_path is None
            else sorted(
                {
                    *(compiled.dependencies or []),
                    *(item.to_table_id for item in group_path),
                    *group_ids,
                }
            ),
            total_label=node.total_label,
            related_filters=compiled.related_filters,
            group_path=group_path,
            numerator_related_filters=compiled.numerator_related_filters,
        )
    if isinstance(node, _MeasureRef):
        folded = node.name.casefold()
        measure = next(
            (
                item
                for key, item in measures.items()
                if key == folded or item.name.casefold() == folded
            ),
            None,
        )
        if measure is None:
            raise DaxSyntaxError(
                f"Unknown measure [{node.name}]. Save it to the draft first or write an aggregate."
            )
        compiled = _compile_measure(measure, catalog, measures, top_n, depth)
        return _Compiled(
            plan=compiled.plan,
            label=measure.name,
            group_names=compiled.group_names,
            description=f"[{measure.name}]",
            dependencies=compiled.dependencies,
            related_filters=compiled.related_filters,
            numerator_related_filters=compiled.numerator_related_filters,
        )
    if isinstance(node, _Agg):
        table_id = catalog.table_id(node.table or "")
        value_column_id = (
            catalog.column_id(table_id, node.column) if node.column is not None else None
        )
        op = node.op
        op_label = _OP_LABELS.get(op, op.upper())
        label = (
            f"{op_label}({catalog.column_name(table_id, value_column_id)})"
            if value_column_id
            else "COUNTROWS()"
        )
        if value_column_id is not None:
            dtype = catalog.column_type(table_id, value_column_id)
            col_name = catalog.column_name(table_id, value_column_id)
            table_name = catalog.tables[table_id].name
            if op in {"sum", "average"} and dtype not in {"integer", "number"}:
                raise DaxSyntaxError(
                    f"{op_label}('{table_name}'[{col_name}]) needs a numeric column — "
                    f"{col_name} is {dtype}."
                )
            if op == "count" and dtype == "string":
                raise DaxSyntaxError(
                    f"DAX COUNT('{table_name}'[{col_name}]) counts numbers only — "
                    "use COUNTROWS() or DISTINCTCOUNT() instead."
                )
            if op in {"minimum", "maximum"} and dtype == "boolean":
                raise DaxSyntaxError(
                    f"{op_label}('{table_name}'[{col_name}]) is not meaningful on a boolean column."
                )
        plan = ReadOnlyAggregatePlan(
            table_id=table_id,
            operation=op,  # type: ignore[arg-type]
            value_column_id=value_column_id,
            top_n=top_n,
        )
        return _Compiled(
            plan=plan,
            label=label,
            description=label,
            dependencies=[table_id, *([value_column_id] if value_column_id else [])],
        )
    if isinstance(node, (_Ratio, _Difference)):
        operation = "difference" if isinstance(node, _Difference) else "ratio"
        operator = "Subtraction" if operation == "difference" else "DIVIDE"
        numerator = _compile(node.numerator, catalog, measures, top_n, depth)
        denominator = _compile(node.denominator, catalog, measures, top_n, depth)
        if numerator.plan is None or denominator.plan is None:
            raise DaxSyntaxError(f"{operator} needs aggregate expressions, not table scans.")
        left, right = numerator.plan, denominator.plan
        if left.table_id != right.table_id:
            # The 4B model wrote DIVIDE(COUNTROWS('Returns'), COUNTROWS('Orders')); the
            # repair turn needs the pattern that works, not only the rule.
            raise DaxSyntaxError(
                f"{operator} needs both sides on the same table, not "
                f"{catalog.tables[left.table_id].name} and "
                f"{catalog.tables[right.table_id].name}. Aggregate "
                f"{catalog.tables[right.table_id].name} on both sides and filter the first "
                "through the related table: DIVIDE(CALCULATE(x, 'Table'[Column] = value), x)."
            )
        extra_filters = _added_filters(left.filters, right.filters)
        extra_related = _added_filters(numerator.related_filters, denominator.related_filters)
        if (
            left.table_id != right.table_id
            or extra_filters is None
            or extra_related is None
            or numerator.numerator_related_filters
            or denominator.numerator_related_filters
        ):
            raise DaxSyntaxError(
                f"{operator} needs both sides on the same table. The first side may add "
                "CALCULATE filters to the second's, as in DIVIDE(CALCULATE(x, filter), x)."
            )
        plan = _combined_plan(
            operation, left.model_copy(update={"filters": right.filters}), right, operator
        ).model_copy(update={"numerator_filters": extra_filters})
        num_name = _operand_label(catalog, left, numerator.label)
        den_name = _operand_label(catalog, right, denominator.label)
        if extra_filters or extra_related:
            # Without this a return rate read DIVIDE(DISTINCTCOUNT(Order ID), same).
            num_name = f"CALCULATE({num_name}, …)"
        label = (
            f"{num_name} - {den_name}"
            if operation == "difference"
            else f"DIVIDE({num_name}, {den_name})"
        )
        return _Compiled(
            plan=plan,
            label=label,
            description=label,
            dependencies=sorted(
                set(numerator.dependencies or []) | set(denominator.dependencies or [])
            ),
            related_filters=denominator.related_filters,
            numerator_related_filters=extra_related,
        )
    if isinstance(node, _Calc):
        compiled = _compile(node.inner, catalog, measures, top_n, depth)
        if compiled.plan is None:
            raise DaxSyntaxError("CALCULATE needs an aggregate expression, not a table scan.")
        extra: list[AggregateFilter] = []
        related: list[tuple[RelationshipPath, AggregateFilter]] = []
        for item in node.filters:
            table_id = catalog.table_id(item.table)
            condition = AggregateFilter(
                column_id=catalog.column_id(table_id, item.column),
                operator=item.op,  # type: ignore[arg-type]
                value=item.value,  # type: ignore[arg-type]
            )
            if table_id == compiled.plan.table_id:
                extra.append(condition)
                continue
            # A related table filters the aggregate through its relationship path. Only
            # = and <> keep Power BI's (Blank)-row semantics exact for unmatched rows.
            if item.op not in {"eq", "neq"}:
                raise DaxSyntaxError(
                    f"Filters on {catalog.tables[table_id].name} support = and <> in this lab."
                )
            related.append((catalog.related(compiled.plan.table_id, table_id), condition))
        merged = [*compiled.plan.filters, *extra]
        merged_related = [*compiled.related_filters, *related]
        if len(merged) + len(merged_related) > 8:
            raise DaxSyntaxError("The lab supports at most 8 filters.")
        plan = compiled.plan.model_copy(update={"filters": merged})
        summary = ", ".join(f"{f.table}[{f.column}] {f.op} {f.value!r}" for f in node.filters)
        return _Compiled(
            plan=plan,
            label=compiled.label,
            group_names=compiled.group_names,
            description=f"CALCULATE({compiled.description}, {summary})",
            dependencies=sorted(
                set(compiled.dependencies or [])
                | {f.column_id for f in extra}
                | {hop.to_table_id for path, _ in related for hop in path}
                | {f.column_id for _, f in related}
            ),
            related_filters=merged_related,
            numerator_related_filters=compiled.numerator_related_filters,
        )
    raise DaxSyntaxError(f"Unsupported expression. {SUPPORTED_SYNTAX}")


def _compile_measure(
    measure: GovernedMeasureSpec | AdvancedDaxProposal,
    catalog: _Catalog,
    measures: dict[str, GovernedMeasureSpec | AdvancedDaxProposal],
    top_n: int,
    depth: int,
) -> _Compiled:
    if isinstance(measure, AdvancedDaxProposal):
        node = parse_dax(measure.expression)
        return _compile(node, catalog, measures, top_n, depth + 1)
    op = measure.operation
    table_id = measure.table_id
    if op in {"ratio", "margin"}:
        if not measure.numerator_measure_id or not measure.denominator_measure_id:
            raise DaxSyntaxError(f"Measure [{measure.name}] is missing its numerator/denominator.")
        numerator = _compile_measure_ref(
            measure.numerator_measure_id, catalog, measures, top_n, depth
        )
        denominator = _compile_measure_ref(
            measure.denominator_measure_id, catalog, measures, top_n, depth
        )
        if numerator.plan is None or denominator.plan is None:
            raise DaxSyntaxError(f"Measure [{measure.name}] resolves to a table scan.")
        left, right = numerator.plan, denominator.plan
        if left.table_id != right.table_id:
            raise DaxSyntaxError(f"Measure [{measure.name}] spans multiple tables.")
        plan = _combined_plan("ratio", left, right, f"Measure [{measure.name}]").model_copy(
            update={"filters": [], "group_by_column_ids": []}
        )
        return _Compiled(plan=plan, label=measure.name, description=f"[{measure.name}]")
    if op in {"sum", "average", "minimum", "maximum", "distinct_count", "count"}:
        if op != "count" and measure.column_id is None:
            raise DaxSyntaxError(f"Measure [{measure.name}] has no value column.")
        plan = ReadOnlyAggregatePlan(
            table_id=table_id,
            operation=op,  # type: ignore[arg-type]
            value_column_id=measure.column_id,
            top_n=top_n,
        )
        return _Compiled(
            plan=plan,
            label=measure.name,
            description=f"[{measure.name}]",
            dependencies=[table_id, *([measure.column_id] if measure.column_id else [])],
        )
    raise DaxSyntaxError(
        f"Measure [{measure.name}] uses the governed {op!r} operation, which the lab "
        "cannot evaluate yet (sum/average/min/max/count/distinct_count/ratio/margin only)."
    )


def _compile_measure_ref(
    measure_id: str,
    catalog: _Catalog,
    measures: dict[str, GovernedMeasureSpec | AdvancedDaxProposal],
    top_n: int,
    depth: int,
) -> _Compiled:
    measure = measures.get(measure_id.casefold())
    if measure is None:
        raise DaxSyntaxError(f"Unknown measure id {measure_id!r}.")
    return _compile_measure(measure, catalog, measures, top_n, depth + 1)


def evaluate_dax(
    snapshot: DatasetSnapshot,
    measures: Iterable[GovernedMeasureSpec | AdvancedDaxProposal],
    expression: str,
    top_n: int,
    aggregates: AggregateService,
    *,
    preview: DaxPreviewContext | None = None,
    detail_period: date | None = None,
    detail_group: DaxGroupSelection | None = None,
    relationships: Iterable[RelationshipProposal] = (),
) -> DaxResult:
    catalog = _Catalog(snapshot, relationships)
    measure_map = {item.id.casefold(): item for item in measures}
    node = parse_dax(expression)
    compiled = _compile(node, catalog, measure_map, top_n, depth=0)
    through_relationship = bool(
        compiled.related_filters or compiled.group_path or compiled.numerator_related_filters
    )
    if through_relationship and (
        preview is not None or detail_period is not None or detail_group is not None
    ):
        # Breakdowns and source rows read the aggregate's table alone and would
        # silently drop a related-table filter.
        raise DaxSyntaxError(
            "Breakdowns are not available yet for formulas that filter or group through "
            "a relationship. Use SUMMARIZECOLUMNS to group the result instead."
        )
    preview_fields = []
    if (
        compiled.plan is not None
        and not compiled.plan.group_by_column_ids
        and not through_relationship
    ):
        table = catalog.tables[compiled.plan.table_id]
        preview_fields = [
            DaxPreviewField(
                column_id=column.id, name=column.name, table=table.name, data_type=column.data_type
            )
            for column in table.columns
            # CALCULATE can replace a visual filter on the same field.
            # Reject that unsupported interaction instead of intersecting silently.
            if column.id not in {item.column_id for item in compiled.plan.filters}
        ][:40]
    date_fields = [
        DaxDateField(column_id=item.column_id, name=item.name, table=item.table)
        for item in preview_fields
        if item.data_type in {"date", "datetime"}
    ]
    date_result = None
    breakdown = None
    if preview is not None:
        preview_field = next(
            (item for item in preview_fields if item.column_id == preview.column_id), None
        )
        if preview_field is None or compiled.plan is None:
            raise DaxSyntaxError(
                "Choose a field from the measure's own table. Breakdowns require a scalar "
                "measure without an explicit filter on that field."
            )
        is_date = preview_field.data_type in {"date", "datetime"}
        grain = (preview.grain or "month") if is_date else None
        if not is_date and (preview.grain is not None or detail_period is not None):
            raise DaxSyntaxError("Date grouping requires a date or datetime field")
        if is_date and (preview.search or detail_group is not None):
            raise DaxSyntaxError(
                "Inspect dates by period; category search requires a non-date field"
            )
        filters = list(compiled.plan.filters)
        if preview.search.strip():
            filters.append(
                AggregateFilter(
                    column_id=preview.column_id, operator="contains", value=preview.search.strip()
                )
            )
        if len(filters) > 8:
            raise DaxSyntaxError("The lab supports at most 8 filters, including preview search.")
        if detail_period is not None or detail_group is not None:
            null_column_id = None
            if detail_period is not None and grain is not None:
                start, end = _preview_period_bounds(detail_period, grain)
                filters.extend(
                    [
                        AggregateFilter(
                            column_id=preview.column_id, operator="gte", value=start.isoformat()
                        ),
                        AggregateFilter(
                            column_id=preview.column_id, operator="lt", value=end.isoformat()
                        ),
                    ]
                )
                description = f"{start.isoformat()} to {end.isoformat()} (exclusive)"
            else:
                assert detail_group is not None
                value = detail_group.value
                dtype = preview_field.data_type
                if value is None:
                    null_column_id = preview.column_id
                elif (
                    dtype == "string"
                    and not isinstance(value, str)
                    or dtype == "boolean"
                    and type(value) is not bool
                    or dtype in {"number", "integer"}
                    and type(value) not in {int, float}
                ):
                    raise DaxSyntaxError("The selected value must match the grouping field's type")
                else:
                    filters.append(
                        AggregateFilter(column_id=preview.column_id, operator="eq", value=value)
                    )
                description = repr(value)
            receipt = aggregates.scan(
                snapshot,
                compiled.plan.table_id,
                row_limit=top_n,
                filters=filters,
                null_column_id=null_column_id,
            )
            table = catalog.tables[compiled.plan.table_id]
            return DaxResult(
                columns=[column.name for column in table.columns],
                rows=receipt.rows,
                row_count=len(receipt.rows),
                truncated=len(receipt.rows) >= top_n,
                elapsed_ms=receipt.elapsed_ms,
                plan=(
                    f"Source rows for {preview_field.name}: {description}. "
                    "Formula filters retained."
                )[:597],
            )
        group_column = "Period" if is_date else preview_field.name
        if group_column == compiled.label:
            group_column += " (group)"
        compiled.plan = ReadOnlyAggregatePlan.model_validate(
            {
                **compiled.plan.model_dump(),
                "group_by_column_ids": [preview.column_id],
                "time_bucket": grain,
                "filters": filters,
            }
        )
        compiled.group_names = [group_column]
        compiled.description += (
            f" · Preview by {preview_field.name}"
            + (f" ({grain})" if grain else "")
            + (f" matching {preview.search.strip()!r}" if preview.search.strip() else "")
            + "; formula unchanged"
        )
        breakdown = DaxBreakdownResult(
            column=group_column, value_column=compiled.label, field=preview_field, grain=grain
        )
        if grain:
            date_result = DaxDateResult(
                column=group_column,
                value_column=compiled.label,
                field=DaxDateField(
                    column_id=preview_field.column_id,
                    name=preview_field.name,
                    table=preview_field.table,
                ),
                grain=grain,
            )
    elif detail_period is not None or detail_group is not None:
        raise DaxSyntaxError("A group inspection requires a preview")
    if compiled.plan is None:
        assert compiled.scan_table is not None
        receipt = aggregates.scan(
            snapshot, compiled.scan_table, row_limit=compiled.scan_limit or 50
        )
        scan_rows = receipt.rows
        columns = (
            list(scan_rows[0].keys())
            if scan_rows
            else [column.name for column in catalog.tables[compiled.scan_table].columns]
        )
        return DaxResult(
            columns=columns,
            rows=scan_rows,
            row_count=len(scan_rows),
            truncated=len(scan_rows) >= (compiled.scan_limit or 50),
            elapsed_ms=receipt.elapsed_ms,
            plan=compiled.description[:597],
        )
    receipt = (
        aggregates.execute_through(
            snapshot,
            compiled.plan,
            group_relationship=compiled.group_path,
            related_filters=compiled.related_filters,
            numerator_related_filters=compiled.numerator_related_filters,
            order_by=compiled.order_by,
        )
        if through_relationship
        else aggregates.execute(
            snapshot,
            compiled.plan,
            order_by=compiled.order_by,
            grand_total=compiled.total_label is not None,
        )
    )
    group_names = compiled.group_names or []
    rows: list[dict[str, str | int | float | bool | None]] = []
    for row in receipt.rows:
        shaped: dict[str, str | int | float | bool | None] = {}
        for index, name in enumerate(group_names):
            shaped[name] = row.get(f"group_{index + 1}")
        shaped[compiled.label] = row.get("value")
        if compiled.total_label:
            shaped[compiled.total_label] = row.get("is_grand_total")
        rows.append(shaped)
    return DaxResult(
        columns=[
            *group_names,
            compiled.label,
            *([compiled.total_label] if compiled.total_label else []),
        ],
        rows=rows,
        row_count=len(rows),
        truncated=len(rows) >= compiled.plan.top_n,
        elapsed_ms=receipt.elapsed_ms,
        plan=compiled.description[:597],
        grand_total_column=compiled.total_label,
        date_fields=date_fields,
        date_preview=date_result,
        preview_fields=preview_fields,
        breakdown=breakdown,
    )


def _preview_period_bounds(period: date, grain: str) -> tuple[date, date]:
    """Half-open calendar periods preserve midnight boundaries and year identity."""
    try:
        if grain == "day":
            return period, period + timedelta(days=1)
        month = (
            period.month
            if grain == "month"
            else ((period.month - 1) // 3) * 3 + 1
            if grain == "quarter"
            else 1
        )
        start = date(period.year, month, 1)
        next_month = month + {"month": 1, "quarter": 3, "year": 12}[grain]
        end = date(period.year + (next_month - 1) // 12, (next_month - 1) % 12 + 1, 1)
    except (ValueError, OverflowError) as error:
        raise DaxSyntaxError("This date period cannot be previewed") from error
    if start != period:
        raise DaxSyntaxError("Select the start of a displayed date period")
    return start, end


def describe_dax(
    snapshot: DatasetSnapshot,
    measures: Iterable[GovernedMeasureSpec | AdvancedDaxProposal],
    expression: str,
    relationships: Iterable[RelationshipProposal] = (),
) -> tuple[str, list[str]]:
    """Compile-only pass used when saving a lab expression to the draft.

    Returns (primary table_id, dependency ids). Raises DaxSyntaxError when the
    expression is outside the evaluatable subset.
    """
    catalog = _Catalog(snapshot, relationships)
    measure_map = {item.id.casefold(): item for item in measures}
    compiled = _compile(parse_dax(expression), catalog, measure_map, 1, depth=0)
    if compiled.plan is None or compiled.plan.group_by_column_ids:
        raise DaxSyntaxError(
            "Table scans can't be saved as measures — write a scalar expression like "
            "SUM('Table'[Column])."
        )
    # AdvancedDaxProposal.dependencies is the measure graph (measure ids only);
    # compiled.dependencies carries table/column ids used for lab evaluation.
    expression_without_columns = re.sub(
        r"(?:'[^']+'|[A-Za-z_][A-Za-z0-9_ ]*)\[[^\]]+\]", "", expression
    )
    referenced = {ref.casefold() for ref in re.findall(r"\[([^\]]+)\]", expression_without_columns)}
    measure_ids = sorted(
        {
            item.id
            for item in measure_map.values()
            if item.id.casefold() in referenced or item.name.casefold() in referenced
        }
    )
    return compiled.plan.table_id, measure_ids
