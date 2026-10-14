"""Plain-English descriptions of DAX measures, read from the formula's structure.

The 4B model paraphrased [Number of Employees with Positive Change] (agents whose
month-to-date sales grew on the previous month) as "employees whose headcount
increased" (MAN-08). A description built from the parsed formula cannot drift like
that: each known pattern has one fixed wording, and anything else returns None so the
caller shows the formula itself instead of a guess.
"""

from __future__ import annotations

from collections.abc import Iterable

from .dax_syntax import (
    Binary,
    Call,
    ColRef,
    Keyword,
    Name,
    Node,
    Num,
    Str,
    TableCtor,
    Unary,
    VarBlock,
    parse_expression,
)

_COMPARE = {
    ">=": "is at least",
    ">": "is more than",
    "<=": "is at most",
    "<": "is less than",
    "=": "is",
    "==": "is exactly",
    "<>": "is not",
}
_PERIODS = {"DATESMTD": "month to date", "DATESQTD": "quarter to date", "DATESYTD": "year to date"}
_TOTALS = {"TOTALMTD": "month to date", "TOTALQTD": "quarter to date", "TOTALYTD": "year to date"}


def _number(value: float | int) -> str:
    if isinstance(value, int) or float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:g}"


class _Describer:
    def __init__(self, measures: set[str]) -> None:
        self.measures = {m.casefold() for m in measures}
        self.variables: dict[str, str] = {}

    def column(self, node: ColRef) -> str:
        return f"{node.table}[{node.column}]" if node.table else f"[{node.column}]"

    def scalar(self, node: Node) -> str | None:
        if isinstance(node, Num):
            return _number(node.value)
        if isinstance(node, Str):
            return f'"{node.value}"'
        if isinstance(node, ColRef):
            if node.table is None or node.column.casefold() in self.measures:
                return f"[{node.column}]"
            return self.column(node)
        if isinstance(node, Name):
            return self.variables.get(node.name.casefold())
        if isinstance(node, VarBlock):
            saved = dict(self.variables)
            try:
                for name, value in node.variables:
                    text = self.scalar(value) or self.table(value)
                    if text is None:
                        return None
                    self.variables[name.casefold()] = text
                return self.scalar(node.body)
            finally:
                self.variables = saved
        if isinstance(node, Unary):
            inner = self.scalar(node.operand)
            if inner is None:
                return None
            if node.op == "NOT":
                if isinstance(node.operand, Call) and node.operand.name == "ISBLANK":
                    shown = self.scalar(node.operand.args[0])
                    return f"{shown} is not blank" if shown else None
                return f"not ({inner})"
            return f"minus {inner}" if node.op == "-" else inner
        if isinstance(node, Binary):
            return self.binary(node)
        if isinstance(node, Call):
            return self.call(node)
        return None

    def binary(self, node: Binary) -> str | None:
        if node.op == "IN" and isinstance(node.right, TableCtor):
            # _Rank IN { _N + 1, _N + 2 }: DEV-01's "⋮" rows.
            left = self.scalar(node.left)
            values = [self.scalar(row[0]) for row in node.right.rows if len(row) == 1]
            if left is None or not values or None in values:
                return None
            shown = [v for v in values if v is not None]
            listed = " or ".join([", ".join(shown[:-1]), shown[-1]]) if len(shown) > 1 else shown[0]
            return f"{left} is {listed}"
        if node.op == "&":
            parts = self.joined(node)
            if parts is None:
                return None
            return "the text " + " followed by ".join(parts)
        left, right = self.scalar(node.left), self.scalar(node.right)
        if left is None or right is None:
            return None
        if node.op in _COMPARE:
            return f"{left} {_COMPARE[node.op]} {right}"
        words = {
            "+": "plus",
            "-": "minus",
            "*": "times",
            "/": "divided by",
            "&&": "and",
            "||": "or",
        }
        if node.op in words:
            return f"{left} {words[node.op]} {right}"
        return None

    def joined(self, node: Node) -> list[str] | None:
        """The parts of a & b & c, a space shown as such."""
        if isinstance(node, Binary) and node.op == "&":
            left, right = self.joined(node.left), self.joined(node.right)
            return None if left is None or right is None else left + right
        if isinstance(node, Str) and node.value == " ":
            return ["a space"]
        part = self.scalar(node)
        return None if part is None else [part]

    def rows(self, node: Node) -> tuple[str, str | None] | None:
        """(what each row is, the condition) for an iterated table."""
        if isinstance(node, Call) and node.name == "FILTER" and len(node.args) == 2:
            inner = self.rows(node.args[0])
            condition = self.scalar(node.args[1])
            if inner is None or condition is None:
                return None
            return inner[0], condition if inner[1] is None else f"{inner[1]} and {condition}"
        if isinstance(node, Call) and node.name in {"ALL", "VALUES", "DISTINCT", "ALLSELECTED"}:
            if len(node.args) == 1 and isinstance(node.args[0], ColRef) and node.args[0].table:
                column = self.column(node.args[0])
                scope = {
                    "ALL": "every",
                    "VALUES": "each",
                    "DISTINCT": "each",
                    "ALLSELECTED": "each selected",
                }[node.name]
                return f"{scope} {column}", None
            if len(node.args) == 1 and isinstance(node.args[0], Name):
                return f"each row of {node.args[0].name}", None
            return None
        if isinstance(node, Name) and node.name.casefold() not in self.variables:
            return f"each row of {node.name}", None
        return None

    def table(self, node: Node) -> str | None:
        rows = self.rows(node)
        if rows is None:
            return None
        return rows[0] + (f" where {rows[1]}" if rows[1] else "")

    def filter(self, node: Node) -> str | None:
        """A CALCULATE filter argument as a phrase."""
        if isinstance(node, Call):
            if node.name in _PERIODS:
                return _PERIODS[node.name]
            if node.name == "CALCULATETABLE" and node.args:
                return self.filter(node.args[0])
            if node.name == "KEEPFILTERS" and len(node.args) == 1:
                return self.filter(node.args[0])
            if node.name in {"DATEADD", "PARALLELPERIOD"} and len(node.args) == 3:
                amount = node.args[1]
                interval = node.args[2]
                if (
                    isinstance(amount, Unary)
                    and amount.op == "-"
                    and isinstance(amount.operand, Num)
                ):
                    steps = -int(amount.operand.value)
                elif isinstance(amount, Num):
                    steps = int(amount.value)
                else:
                    return None
                unit = interval.word.lower() if isinstance(interval, Keyword) else None
                if unit is None:
                    return None
                if steps == -1:
                    if node.name == "PARALLELPERIOD":
                        return f"for the whole previous {unit}"
                    return f"for the same days of the previous {unit}"
                return f"shifted {steps} {unit}{'s' if abs(steps) != 1 else ''}"
            if node.name == "SAMEPERIODLASTYEAR":
                return "for the same days of the previous year"
            if node.name == "DATESINPERIOD" and len(node.args) == 4:
                amount, interval = node.args[2], node.args[3]
                if (
                    isinstance(amount, Unary)
                    and isinstance(amount.operand, Num)
                    and isinstance(interval, Keyword)
                ):
                    n = int(amount.operand.value)
                    unit = interval.word.lower()
                    span = f"the {unit}" if n == 1 else f"the {n} {unit}s"
                    return f"over {span} up to the latest date"
                return None
            if node.name in {"REMOVEFILTERS", "ALL"} and not node.args:
                return "ignoring all filters"
            if node.name in {"REMOVEFILTERS", "ALL"} and node.args:
                names = [
                    self.column(a) if isinstance(a, ColRef) else getattr(a, "name", "")
                    for a in node.args
                ]
                return "ignoring filters on " + ", ".join(names)
            if node.name == "FILTER":
                table = self.table(node)
                return f"for {table}" if table else None
            return None
        condition = self.scalar(node)
        return f"where {condition}" if condition else None

    def call(self, node: Call) -> str | None:
        name, args = node.name, node.args
        simple = {
            "SUM": "the sum of {}",
            "AVERAGE": "the average of {}",
            "MIN": "the smallest {}",
            "MAX": "the largest {}",
            "COUNT": "the number of non-blank {} values",
            "COUNTA": "the number of non-blank {} values",
            "DISTINCTCOUNT": "the number of distinct {} values",
        }
        if name in simple and len(args) == 1 and isinstance(args[0], ColRef) and args[0].table:
            return simple[name].format(self.column(args[0]))
        if name == "COUNTROWS" and len(args) == 1:
            if isinstance(args[0], Name):
                return f"the number of rows in {args[0].name}"
            table = self.table(args[0])
            return f"the number of {table}" if table else None
        if name in {"SUMX", "AVERAGEX", "MINX", "MAXX", "COUNTX"} and len(args) == 2:
            rows = self.rows(args[0])
            value = self.scalar(args[1])
            if rows is None or value is None:
                return None
            each, condition = rows
            where = f" where {condition}" if condition else ""
            if name == "COUNTX":
                counted = each.removeprefix("every ").removeprefix("each ")
                return f"the number of {counted} values{where}"
            verb = {
                "SUMX": "the sum",
                "AVERAGEX": "the average",
                "MINX": "the smallest",
                "MAXX": "the largest",
            }[name]
            return f"{verb} of {value} over {each}{where}"
        if name == "DIVIDE" and len(args) in {2, 3}:
            top, bottom = self.scalar(args[0]), self.scalar(args[1])
            return f"{top} divided by {bottom}" if top and bottom else None
        if name == "CALCULATE" and args:
            base = self.scalar(args[0])
            filters = [self.filter(a) for a in args[1:]]
            if base is None or any(f is None for f in filters):
                return None
            return " ".join([base, *filters])  # type: ignore[list-item]
        if name in _TOTALS and len(args) >= 2:
            base = self.scalar(args[0])
            return f"{base} {_TOTALS[name]}" if base else None
        if name == "IF" and len(args) == 2:
            condition, value = self.scalar(args[0]), self.scalar(args[1])
            if condition is None or value is None:
                return None
            return f"{value} (blank unless {condition})"
        if name == "IF" and len(args) == 3:
            condition = self.scalar(args[0])
            yes, no = self.scalar(args[1]), self.scalar(args[2])
            if None in (condition, yes, no):
                return None
            return f"{yes} if {condition}, otherwise {no}"
        if name == "ISBLANK" and len(args) == 1:
            inner = self.scalar(args[0])
            return f"{inner} is blank" if inner else None
        if name == "SELECTEDVALUE" and args and isinstance(args[0], ColRef):
            fallback = self.scalar(args[1]) if len(args) > 1 else "blank"
            return (
                f"the selected {self.column(args[0])} ({fallback} when not exactly one is selected)"
            )
        if name == "RANKX" and len(args) >= 2:
            # RANKX(FILTER(ALL(DimEmployee[EmployeeKey]), ...), [MOMTD % Total Sales], , DESC)
            rows, value = self.rows(args[0]), self.scalar(args[1])
            if rows is None or value is None:
                return None
            if len(args) > 2 and not (isinstance(args[2], Name) and args[2].name == ""):
                return None  # ranking a given value, not the current row's
            order = args[3] if len(args) > 3 else None
            word = order.word if isinstance(order, Keyword) else "DESC"
            if isinstance(order, Num):
                word = "ASC" if order.value == 1 else "DESC"
            elif order is not None and not isinstance(order, Keyword | Name):
                return None
            first = "lowest" if word == "ASC" else "highest"
            each, condition = rows
            where = f" where {condition}" if condition else ""
            return f"the rank of {value} among {each}{where} (1 = the {first}; blank counts as 0)"
        if name in {"TRUE", "FALSE"} and not args:
            return name.lower()
        if name == "BLANK" and not args:
            return "blank"
        return None


def describe(expression: str, measures: set[str]) -> str | None:
    """A sentence for the formula, or None when a part has no fixed wording."""
    try:
        node = parse_expression(expression)
    except ValueError:
        return None
    return _Describer(measures).scalar(node)


# The describer's public name (number_explain words each step of a formula with it).
Describer = _Describer


def describer(measures: Iterable[str], variables: Iterable[str] = ()) -> _Describer:
    """A describer that knows ``measures`` and the formula's ``variables`` by name, so a
    reference to either is worded as that name."""
    found = _Describer(set(measures))
    found.variables = {name.casefold(): name for name in variables}
    return found


def describe_parts(expression: str, measures: set[str]) -> list[tuple[str, str]]:
    """(variable, sentence) for each VAR and the RETURN a fixed wording covers, other
    variables named as themselves: DEV-01's [Employee Name] had no sentence as a whole,
    so the model guessed that its "⋮" rows marked employees with no name."""
    try:
        node = parse_expression(expression)
    except ValueError:
        return []
    if not isinstance(node, VarBlock):
        return []
    describer = _Describer(measures)
    parts: list[tuple[str, str]] = []
    for name, value in node.variables:
        text = describer.scalar(value)
        if text is not None and text != name:
            parts.append((name, text))
        describer.variables[name.casefold()] = name
    body = describer.scalar(node.body)
    if body is not None and not isinstance(node.body, Name):
        parts.append(("RETURN", body))
    return parts


def referenced_measures(expression: str, measures: dict[str, str]) -> list[str]:
    """Measures the formula names, in order of first mention."""
    try:
        node = parse_expression(expression)
    except ValueError:
        return []
    by_key = {m.casefold(): m for m in measures}
    found: list[str] = []

    def walk(n: Node) -> None:
        if isinstance(n, ColRef) and (n.table is None or n.column.casefold() in by_key):
            name = by_key.get(n.column.casefold())
            if name and name not in found:
                found.append(name)
        elif isinstance(n, Call):
            for a in n.args:
                walk(a)
        elif isinstance(n, Binary):
            walk(n.left)
            walk(n.right)
        elif isinstance(n, Unary):
            walk(n.operand)
        elif isinstance(n, VarBlock):
            for _, v in n.variables:
                walk(v)
            walk(n.body)

    walk(node)
    return found
