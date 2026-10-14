"""A local DAX engine for imported reports: filter context, relationships, iterators,
time intelligence and queries, evaluated over the imported snapshot.

Why it exists: an imported dashboard's cards are measures like
COUNTX(FILTER(ALL(DimEmployee[EmployeeKey]), [MTD Total Sales] >= 100000), ...), and the
lab's governed evaluator (``dax.py``) deliberately supports only plain aggregates. Before
this engine, only Power BI Desktop could say what such a card shows, so chat guessed and
the live report needed Desktop open. The engine follows Power BI's semantics: filters
propagate from the one side of a relationship to the many side, CALCULATE replaces
filters on the columns it names and performs context transition, a relationship whose
many side has unmatched keys adds a blank row to the one side, filtering a date table's
key column removes the table's other filters, and BLANK behaves as in DAX.

It is checked against Power BI Desktop (evals/dax_engine_differential). Anything it does
not implement raises ``DaxUnsupported`` naming the function, never a guess.
"""

from __future__ import annotations

import bisect
import calendar
import math
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any, NoReturn, cast

import numpy as np
import pandas as pd

from .dax_format import format_value
from .dax_syntax import (
    Binary,
    Call,
    ColRef,
    Keyword,
    Name,
    Node,
    Num,
    Query,
    Str,
    TableCtor,
    Unary,
    VarBlock,
    parse_expression,
    parse_query,
)


class DaxEngineError(ValueError):
    """The formula fails as it would in Power BI (wrong reference, type, shape)."""


class DaxUnsupported(DaxEngineError):
    """The formula uses something this engine does not implement."""


class DaxEngineLimit(DaxEngineError):
    """The evaluation exceeded the time or size budget."""


class DaxModelIncomplete(DaxUnsupported):
    """The imported copy may lack some of the report's relationships."""


Scalar = None | bool | int | float | str | datetime
RowValues = tuple[Scalar, ...]
FilterValue = Scalar | RowValues
MemoKey = tuple[object, ...]

# ------------------------------------------------------------------------ model


def _key(name: str) -> str:
    return name.strip().casefold()


@dataclass
class EngineColumn:
    name: str
    kind: str  # "integer", "number", "string", "boolean", "date"
    values: np.ndarray
    _folded: np.ndarray | None = field(default=None, repr=False)
    _index: dict[Any, np.ndarray] | None = field(default=None, repr=False)

    def value(self, i: int) -> Scalar:
        if i >= len(self.values):
            return None  # the blank row
        return _python(self.values[i], self.kind)

    def folded(self) -> np.ndarray:
        """Casefolded text for case-insensitive comparisons (DAX collation)."""
        if self._folded is None:
            self._folded = np.array(
                [v.casefold() if isinstance(v, str) else None for v in self.values], dtype=object
            )
        return self._folded

    def rows_for(self, keys: np.ndarray) -> np.ndarray | None:
        """Mask of rows whose value is one of a few keys, from a value -> rows index.

        Filtering 200,000 sales rows by one agent's stores with pandas isin cost ~1 ms a
        time, over a thousand times per card; the index makes it a few slices.
        """
        if self.kind not in {"number", "integer", "date"}:
            return None
        if self._index is None:
            raw = (
                self.values.view("int64") if self.kind == "date" else self.values.astype("float64")
            )
            order = np.argsort(raw, kind="stable")
            ordered = raw[order]
            starts = np.flatnonzero(np.r_[True, ordered[1:] != ordered[:-1]])
            ends = np.r_[starts[1:], len(ordered)]
            self._index = {ordered[a].item(): order[a:b] for a, b in zip(starts, ends, strict=True)}
        mask = np.zeros(len(self.values), dtype=bool)
        raw_keys = keys.view("int64") if self.kind == "date" and keys.dtype.kind == "M" else keys
        for key in raw_keys:
            rows = self._index.get(key.item() if hasattr(key, "item") else key)
            if rows is None and self.kind != "date":
                rows = self._index.get(float(key))
            if rows is not None:
                mask[rows] = True
        return mask

    def nulls(self) -> np.ndarray:
        if self.kind in {"number", "integer"}:
            return (
                np.isnan(self.values)
                if self.values.dtype.kind == "f"
                else np.zeros(len(self.values), dtype=bool)
            )
        if self.kind == "date":
            return np.isnat(self.values)
        return pd.isna(pd.Series(self.values, dtype=object)).to_numpy()


def _python(value: Any, kind: str) -> Scalar:
    if value is None:
        return None
    if kind == "date":
        if pd.isna(value):
            return None
        if isinstance(value, np.datetime64):
            if np.isnat(value):
                return None
            return pd.Timestamp(value).to_pydatetime()
        if isinstance(value, pd.Timestamp):
            return None if pd.isna(value) else value.to_pydatetime()
        return cast(Scalar, value)
    if isinstance(value, np.bool_ | bool):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating | float):
        if math.isnan(value):
            return None
        return int(value) if kind == "integer" and float(value).is_integer() else float(value)
    if isinstance(value, float) and math.isnan(value):
        return None
    return cast(Scalar, value)


@dataclass
class EngineTable:
    name: str
    columns: dict[str, EngineColumn]  # by casefolded name
    rows: int
    blank_row: bool = False

    def column(self, name: str) -> EngineColumn:
        found = self.columns.get(_key(name))
        if found is None:
            raise DaxEngineError(f"Column '{name}' in table '{self.name}' cannot be found.")
        return found


@dataclass(frozen=True)
class EngineRelationship:
    many: str  # table key
    many_column: str  # column key
    one: str
    one_column: str
    active: bool = True
    both: bool = False


@dataclass
class EngineMeasure:
    name: str
    table: str
    text: str
    format_string: str = ""
    _node: Node | None = None

    def node(self) -> Node:
        if self._node is None:
            try:
                self._node = parse_expression(self.text)
            except ValueError as error:
                raise DaxEngineError(
                    f"Measure [{self.name}] has a syntax error: {error}"
                ) from error
        return self._node


def _column_from_series(name: str, series: pd.Series) -> EngineColumn:
    dtype = series.dtype
    if pd.api.types.is_bool_dtype(dtype) and not series.isna().any():
        return EngineColumn(name, "boolean", series.to_numpy(dtype=bool))
    if pd.api.types.is_integer_dtype(dtype):
        if series.isna().any():
            return EngineColumn(name, "integer", series.to_numpy(dtype="float64", na_value=np.nan))
        return EngineColumn(name, "integer", series.to_numpy(dtype="int64"))
    if pd.api.types.is_float_dtype(dtype):
        return EngineColumn(name, "number", series.to_numpy(dtype="float64", na_value=np.nan))
    if pd.api.types.is_datetime64_any_dtype(dtype):
        values = series.dt.tz_localize(None) if getattr(series.dt, "tz", None) else series
        return EngineColumn(name, "date", values.to_numpy(dtype="datetime64[ns]"))
    if pd.api.types.is_bool_dtype(dtype):
        return EngineColumn(
            name, "boolean", np.array([None if pd.isna(v) else bool(v) for v in series], object)
        )
    # Import dictionaries share one display value for case-insensitive text keys.
    # Trailing spaces are trimmed; leading spaces remain significant.
    representatives: dict[str, str] = {}
    string_values: list[Any] = []
    for value in series.astype(object):
        if pd.isna(value):
            string_values.append(None)
        elif isinstance(value, str):
            text = value.rstrip(" ")
            string_values.append(representatives.setdefault(text.casefold(), text))
        else:
            string_values.append(value)
    return EngineColumn(name, "string", np.array(string_values, dtype=object))


class Model:
    """Tables, relationships and measures, keyed case-insensitively as in DAX."""

    def __init__(
        self,
        tables: Iterable[EngineTable],
        relationships: Iterable[EngineRelationship],
        measures: Iterable[EngineMeasure],
    ) -> None:
        self.tables = {_key(t.name): t for t in tables}
        self.relationships = [
            r for r in relationships if r.many in self.tables and r.one in self.tables
        ]
        checked_keys: set[tuple[str, str]] = set()
        for rel in self.relationships:
            endpoint = (rel.one, rel.one_column)
            if endpoint in checked_keys:
                continue
            checked_keys.add(endpoint)
            column = self.tables[rel.one].column(rel.one_column)
            keys = column.folded() if column.kind == "string" else column.values
            if pd.Series(keys).duplicated().any():
                raise DaxEngineError(
                    f"Relationship requires a unique one-side key: "
                    f"'{self.tables[rel.one].name}'[{column.name}]. "
                    "Snapshot results cannot be calculated from duplicate keys."
                )
        self.measures = {_key(m.name): m for m in measures}
        self.upstream: dict[str, list[EngineRelationship]] = {}
        for rel in self.relationships:
            if rel.active:
                self.upstream.setdefault(rel.many, []).append(rel)
        # A date column on the one side of a relationship: filtering it in CALCULATE
        # removes the date table's other filters, as for a marked date table.
        self.date_keys: set[tuple[str, str]] = {
            (r.one, r.one_column)
            for r in self.relationships
            if r.active and self.tables[r.one].column(r.one_column).kind == "date"
        }
        self._closure: dict[str, frozenset[str]] = {}
        self._unmatched: dict[EngineRelationship, np.ndarray] = {}
        # A blank row passes up a chain: a sale of an unknown store meets DimStore's blank
        # row, whose blank EmployeeKey meets DimEmployee's, so sales by agent show (Blank).
        changed = True
        while changed:
            changed = False
            for rel in self.relationships:
                if not rel.active or self.tables[rel.one].blank_row:
                    continue
                if self.tables[rel.many].blank_row or self.unmatched(rel).any():
                    self.tables[rel.one].blank_row = True
                    changed = True

    @classmethod
    def from_frames(
        cls,
        frames: dict[str, pd.DataFrame],
        relationships: Iterable[tuple[str, str, str, str]] = (),
        measures: dict[str, str] | None = None,
        *,
        measure_table: str = "",
    ) -> Model:
        """Build from DataFrames: relationships as (many, column, one, column) names."""
        tables = [
            EngineTable(
                name,
                {_key(c): _column_from_series(c, frame[c]) for c in frame.columns},
                len(frame),
            )
            for name, frame in frames.items()
        ]
        rels = [
            EngineRelationship(_key(a), _key(b), _key(c), _key(d)) for a, b, c, d in relationships
        ]
        table = measure_table or (next(iter(frames)) if frames else "")
        return cls(tables, rels, [EngineMeasure(n, table, t) for n, t in (measures or {}).items()])

    def table(self, name: str) -> EngineTable:
        found = self.tables.get(_key(name))
        if found is None:
            raise DaxEngineError(f"Cannot find table '{name}'.")
        return found

    def has_column(self, table: str, column: str) -> bool:
        found = self.tables.get(_key(table))
        return found is not None and _key(column) in found.columns

    def closure(self, table: str) -> frozenset[str]:
        """The table and every table whose filters reach it (its expanded table)."""
        if table not in self._closure:
            seen = {table}
            stack = [table]
            while stack:
                for rel in self.upstream.get(stack.pop(), []):
                    if rel.one not in seen:
                        seen.add(rel.one)
                        stack.append(rel.one)
            self._closure[table] = frozenset(seen)
        return self._closure[table]

    def unmatched(self, rel: EngineRelationship) -> np.ndarray:
        """Many-side rows whose key has no row on the one side (they meet the blank row)."""
        if rel not in self._unmatched:
            many = self.tables[rel.many].column(rel.many_column)
            one = self.tables[rel.one].column(rel.one_column)
            one_values = one.folded() if one.kind == "string" else one.values
            many_values = many.folded() if many.kind == "string" else many.values
            keys = pd.Series(one_values).dropna()
            found = pd.Series(many_values).isin(keys).to_numpy()
            if one.nulls().any():
                found |= many.nulls()
            self._unmatched[rel] = ~found
        return self._unmatched[rel]


# --------------------------------------------------------------- filter context


@dataclass(frozen=True)
class Filter:
    table: str
    columns: tuple[str, ...]
    values: frozenset[FilterValue]  # scalars for one column, tuples for several


Ctx = frozenset[Filter]
EMPTY: Ctx = frozenset()
# The column name of a filter holding row positions: context transition over whole rows
# of a model table (SUMX(FactSales, [Measure])) filters those rows, not 14 values each.
_ROW = "\x00row"


@dataclass(frozen=True, eq=False)
class Col:
    name: str
    lineage: tuple[str, str] | None = None  # (table key, column key)
    renamed: bool = False  # SELECTCOLUMNS "Store", DimStore[StoreName]: shown as [Store]


@dataclass(eq=False)
class Row:
    columns: tuple[Col, ...]
    values: RowValues
    base: str | None = None
    index: int | None = None


class TableValue:
    """A table result. Whole rows of a model table stay as row indices until needed."""

    def __init__(
        self,
        columns: Sequence[Col],
        rows: list[RowValues] | None = None,
        *,
        base: str | None = None,
        indices: np.ndarray | None = None,
        model: Model | None = None,
    ) -> None:
        self.columns = list(columns)
        self._rows = rows
        self.base, self.indices, self.model = base, indices, model
        self.token = 0  # set when the engine caches this table: a stable identity

    def __len__(self) -> int:
        if self._rows is not None:
            return len(self._rows)
        return 0 if self.indices is None else len(self.indices)

    @property
    def rows(self) -> list[RowValues]:
        if self._rows is None:
            assert self.model is not None and self.base is not None and self.indices is not None
            if len(self.indices) > 300_000:
                raise DaxEngineLimit(
                    f"Iterating {len(self.indices):,} rows of "
                    f"'{self.model.tables[self.base].name}' row by row is too slow here. "
                    "Aggregate a column instead."
                )
            table = self.model.tables[self.base]
            cols = [table.columns[c.lineage[1]] for c in self.columns if c.lineage]
            self._rows = [tuple(col.value(int(i)) for col in cols) for i in self.indices]
        return self._rows

    def row(self, position: int) -> Row:
        index = None if self.indices is None else int(self.indices[position])
        return Row(tuple(self.columns), self.rows[position], self.base, index)

    def iter_rows(self) -> Iterable[Row]:
        cols = tuple(self.columns)
        for position, values in enumerate(self.rows):
            index = None if self.indices is None else int(self.indices[position])
            yield Row(cols, values, self.base, index)


@dataclass(frozen=True)
class Env:
    variables: dict[str, Scalar | TableValue] = field(default_factory=dict)
    rows: tuple[Row, ...] = ()
    scope: frozenset[tuple[str, str]] = frozenset()  # groupings in scope (ISINSCOPE)
    shadow: Ctx | None = None  # the context ALLSELECTED restores


# ---------------------------------------------------------------- DAX semantics

_TABLE_FUNCTIONS = {
    "ALL",
    "ALLNOBLANKROW",
    "ALLSELECTED",
    "ALLEXCEPT",
    "VALUES",
    "DISTINCT",
    "FILTER",
    "CALCULATETABLE",
    "ADDCOLUMNS",
    "SELECTCOLUMNS",
    "SUMMARIZE",
    "SUMMARIZECOLUMNS",
    "ROW",
    "TOPN",
    "UNION",
    "CROSSJOIN",
    "GENERATESERIES",
    "TREATAS",
    "EXCEPT",
    "INTERSECT",
    "RELATEDTABLE",
    "DATESMTD",
    "DATESQTD",
    "DATESYTD",
    "DATEADD",
    "SAMEPERIODLASTYEAR",
    "PARALLELPERIOD",
    "DATESINPERIOD",
    "DATESBETWEEN",
    "PREVIOUSDAY",
    "PREVIOUSMONTH",
    "PREVIOUSQUARTER",
    "PREVIOUSYEAR",
    "NEXTDAY",
    "NEXTMONTH",
    "NEXTQUARTER",
    "NEXTYEAR",
    "FIRSTDATE",
    "LASTDATE",
    "STARTOFMONTH",
    "STARTOFQUARTER",
    "STARTOFYEAR",
    "ENDOFMONTH",
    "ENDOFQUARTER",
    "ENDOFYEAR",
    "CALENDAR",
    "KEEPFILTERS",
    "REMOVEFILTERS",
    "GENERATE",
    "DATATABLE",
    "TOPNSKIP",
    "NATURALINNERJOIN",
    "GROUPBY",
}
_MODIFIERS = {"ALL", "ALLNOBLANKROW", "REMOVEFILTERS", "ALLEXCEPT", "ALLSELECTED"}
_INTERVAL_MONTHS = {"MONTH": 1, "QUARTER": 3, "YEAR": 12}


def is_blank(value: Scalar) -> bool:
    return value is None


def _number(value: Scalar, context: str = "") -> float | int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return value
    if isinstance(value, datetime):
        return _serial(value)
    if isinstance(value, str):
        try:
            return float(value) if any(c in value for c in ".eE") else int(value)
        except ValueError:
            raise DaxEngineError(
                f"Cannot convert value '{value}' of type Text to type Number{context}."
            ) from None
    raise DaxEngineError(f"Cannot convert {value!r} to a number")


_EPOCH = datetime(1899, 12, 30)


def _serial(value: datetime) -> float:
    delta = value - _EPOCH
    return delta.days + delta.seconds / 86400


def _from_serial(value: float) -> datetime:
    return _EPOCH + timedelta(days=value)


def _text(value: Scalar) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, float):
        return format_value(value, "General Number")
    if isinstance(value, datetime):
        return format_value(value, "General Date")
    return str(value)


def _truthy(value: Scalar) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        folded = value.strip().casefold()
        if folded in {"true", "false"}:
            return folded == "true"
        raise DaxEngineError(f"Cannot convert value '{value}' of type Text to type True/False.")
    return True


def _kind(value: Scalar) -> str:
    if value is None:
        return "blank"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, datetime):
        return "date"
    return "text"


def compare(op: str, left: Scalar, right: Scalar) -> bool:
    """DAX comparison: BLANK compares as 0, "" or FALSE; text ignores case."""
    if op == "==":
        if left is None or right is None:
            return left is None and right is None
        op = "="
    lk, rk = _kind(left), _kind(right)
    if lk == "blank" and rk == "blank":
        a: Any = 0
        b: Any = 0
    elif lk == "blank" or rk == "blank":
        other = rk if lk == "blank" else lk
        filler: Any = {"text": "", "boolean": False, "number": 0, "date": _EPOCH}[other]
        a = filler if lk == "blank" else left
        b = filler if rk == "blank" else right
        if other == "text":
            a, b = str(a).casefold(), str(b).casefold()
    elif lk == rk:
        a, b = left, right
        if lk == "text":
            a, b = str(a).casefold(), str(b).casefold()
    elif {lk, rk} == {"number", "date"}:
        a = _serial(left) if isinstance(left, datetime) else left
        b = _serial(right) if isinstance(right, datetime) else right
    elif {lk, rk} == {"number", "boolean"}:
        a, b = _number(left), _number(right)
    else:
        names = {"text": "Text", "number": "Integer", "boolean": "True/False", "date": "Date"}
        raise DaxEngineError(
            "DAX comparison operations do not support comparing values of type "
            f"{names[lk]} with values of type {names[rk]}. Consider using the VALUE or FORMAT "
            "function to convert one of the values."
        )
    if op == "=":
        return bool(a == b)
    if op == "<>":
        return bool(a != b)
    if op == "<":
        return bool(a < b)
    if op == ">":
        return bool(a > b)
    if op == "<=":
        return bool(a <= b)
    if op == ">=":
        return bool(a >= b)
    raise DaxEngineError(f"Unknown comparison {op}")


def arithmetic(op: str, left: Scalar, right: Scalar) -> Scalar:
    if op in {"+", "-"}:
        if left is None and right is None:
            return None
        if isinstance(left, datetime) or isinstance(right, datetime):
            if op == "+" and isinstance(left, datetime) and not isinstance(right, datetime):
                return left + timedelta(days=_number(right))
            if op == "+" and isinstance(right, datetime) and not isinstance(left, datetime):
                return right + timedelta(days=_number(left))
            if op == "-" and isinstance(left, datetime) and not isinstance(right, datetime):
                return left - timedelta(days=_number(right))
            return _number(left) - _number(right)
        a, b = _number(left), _number(right)
        return a + b if op == "+" else a - b
    if op == "*":
        if left is None or right is None:
            return None
        return _number(left) * _number(right)
    if op == "/":
        if left is None and right is None:
            return None
        a, b = _number(left), _number(right)
        if b == 0:
            if a == 0:
                return math.nan
            return math.inf if a > 0 else -math.inf
        return a / b
    if op == "^":
        if left is None and right is None:
            return None
        return float(_number(left)) ** _number(right)
    raise DaxEngineError(f"Unknown operator {op}")


def _sort_key(value: Scalar) -> tuple[int, int | float | str]:
    """Order values as DAX does: BLANK first, then numbers/dates, text by folded case."""
    if value is None:
        return (0, 0)
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, int | float):
        return (1, value)
    if isinstance(value, datetime):
        return (1, _serial(value))
    return (2, str(value).casefold())


# ------------------------------------------------------------------- dates


def _day(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return datetime(value.year, value.month, value.day)
    return datetime(value.year, value.month, value.day)


def _add_months(value: datetime, months: int) -> datetime:
    total = value.year * 12 + value.month - 1 + months
    year, month = divmod(total, 12)
    last = calendar.monthrange(year, month + 1)[1]
    return datetime(year, month + 1, min(value.day, last))


def _month_end(value: datetime) -> datetime:
    return datetime(value.year, value.month, calendar.monthrange(value.year, value.month)[1])


def _is_month_end(value: datetime) -> bool:
    return value.day == calendar.monthrange(value.year, value.month)[1]


def _period_start(value: datetime, months: int) -> datetime:
    if months == 12:
        return datetime(value.year, 1, 1)
    if months == 3:
        return datetime(value.year, (value.month - 1) // 3 * 3 + 1, 1)
    return datetime(value.year, value.month, 1)


def _period_end(value: datetime, months: int) -> datetime:
    start = _period_start(value, months)
    return _add_months(start, months) - timedelta(days=1)


# --------------------------------------------------------------------- engine


class Engine:
    """Evaluates DAX over a model. One instance per request: it memoizes measures."""

    def __init__(
        self,
        model: Model,
        *,
        time_budget: float = 30.0,
        query_measures: dict[str, EngineMeasure] | None = None,
        now: datetime | None = None,
    ) -> None:
        self.model = model
        self.deadline = time.monotonic() + time_budget
        self.measures = dict(model.measures)
        self.measures.update(query_measures or {})
        self.now = now or datetime.now()
        self._memo: dict[MemoKey, Scalar] = {}
        self._value_memo: dict[MemoKey, list[Scalar]] = {}
        self._table_memo: dict[MemoKey, TableValue] = {}
        self._condition_memo: dict[MemoKey, tuple[Node, list[Filter]]] = {}
        self._row_memo: dict[MemoKey, dict[RowValues, Scalar]] = {}
        self._date_memo: dict[MemoKey, list[datetime]] = {}
        self._date_sets: dict[MemoKey, frozenset[datetime]] = {}
        self._rank_memo: dict[MemoKey, tuple[list[Scalar], list[int | float | str]]] = {}
        self._masks: dict[tuple[str, Ctx], np.ndarray] = {}
        self._own: dict[tuple[str, Filter], np.ndarray] = {}
        self._depth = 0
        self._ticks = 0
        self._tokens = 0

    # ----------------------------------------------------------- guards
    def _tick(self) -> None:
        self._ticks += 1
        if self._ticks & 255 == 0 and time.monotonic() > self.deadline:
            raise DaxEngineLimit(
                "The calculation took too long for the local engine. "
                "Simplify it or run it in Power BI Desktop."
            )

    # ------------------------------------------------------- filter context
    def apply(
        self, ctx: Ctx, filters: Iterable[Filter], *, keep: bool = False, together: bool = False
    ) -> Ctx:
        """Add filters. Each replaces the context's filters on its columns unless ``keep``
        (KEEPFILTERS); ``together`` filters replace the context's but not each other's
        (one CALCULATE's filter arguments)."""
        current = set(ctx)
        filters = list(filters)
        for new in filters:
            if not keep:
                if new.columns == (_ROW,) or any(
                    (new.table, c) in self.model.date_keys for c in new.columns
                ):
                    current = {f for f in current if f.table != new.table}
                else:
                    current = self._without_columns(current, new.table, set(new.columns))
            if not together:
                current.add(new)
        if together:
            current.update(filters)
        return frozenset(current)

    def _without_columns(self, filters: set[Filter], table: str, columns: set[str]) -> set[Filter]:
        out: set[Filter] = set()
        for f in filters:
            if f.table != table or not columns.intersection(f.columns):
                out.add(f)
                continue
            keep = [i for i, c in enumerate(f.columns) if c not in columns]
            if keep:
                # A remaining column implies this was a multi-column filter, whose
                # members are rows rather than scalar values.
                values = frozenset(
                    (v[keep[0]] if len(keep) == 1 else tuple(v[i] for i in keep))
                    for v in cast(frozenset[RowValues], f.values)
                )
                out.add(Filter(table, tuple(f.columns[i] for i in keep), values))
        return out

    def remove(self, ctx: Ctx, table: str | None = None, columns: Iterable[str] = ()) -> Ctx:
        """ALL/REMOVEFILTERS: a whole table removes its expanded table's filters."""
        if table is None:
            return EMPTY
        columns = set(columns)
        if not columns:
            expanded = self.model.closure(table)
            return frozenset(f for f in ctx if f.table not in expanded)
        return frozenset(self._without_columns(set(ctx), table, columns))

    def transition(self, ctx: Ctx, rows: Sequence[Row]) -> Ctx:
        """Context transition: each row context becomes filters on its columns."""
        for row in rows:
            if row.base is not None and row.index is not None:
                table = self.model.tables[row.base]
                if len(row.columns) >= len(table.columns) and all(
                    c.lineage and c.lineage[0] == row.base for c in row.columns
                ):
                    # A whole model-table row: filter that row (all its columns).
                    ctx = self.apply(ctx, [Filter(row.base, (_ROW,), frozenset([row.index]))])
                    continue
            by_table: dict[str, list[tuple[str, Scalar]]] = {}
            for col, value in zip(row.columns, row.values, strict=False):
                if col.lineage:
                    by_table.setdefault(col.lineage[0], []).append((col.lineage[1], value))
            filters = []
            for table_key, pairs in by_table.items():
                if len(pairs) == 1:
                    filters.append(Filter(table_key, (pairs[0][0],), frozenset([pairs[0][1]])))
                else:
                    filters.append(
                        Filter(
                            table_key,
                            tuple(c for c, _ in pairs),
                            frozenset([tuple(v for _, v in pairs)]),
                        )
                    )
            ctx = self.apply(ctx, filters)
        return ctx

    # ------------------------------------------------------------ visibility
    def _member(self, table: EngineTable, f: Filter) -> np.ndarray:
        cached = self._own.get((table.name, f))
        if cached is not None:
            return cached
        n = table.rows
        if f.columns == (_ROW,):
            full = np.zeros(n + 1, dtype=bool)
            for index in cast(frozenset[int], f.values):
                full[index] = True
            self._own[(table.name, f)] = full
            return full
        if len(f.columns) == 1:
            column = table.columns[f.columns[0]]
            mask = _isin(column, f.values)
            blank = None in f.values
        else:
            cols = [table.columns[c] for c in f.columns]
            wanted = {tuple(_fold(v) for v in t) for t in cast(frozenset[RowValues], f.values)}
            arrays = [c.folded() if c.kind == "string" else c.values for c in cols]
            mask = np.fromiter(
                (
                    tuple(_fold(_python(a[i], c.kind)) for a, c in zip(arrays, cols, strict=True))
                    in wanted
                    for i in range(n)
                ),
                dtype=bool,
                count=n,
            )
            blank = tuple(None for _ in f.columns) in wanted
        full = np.empty(n + 1, dtype=bool)
        full[:n] = mask
        full[n] = blank
        self._own[(table.name, f)] = full
        return full

    def mask(self, table_key: str, ctx: Ctx) -> np.ndarray:
        """Visible rows of a table (plus its blank row at the end) under a filter context."""
        closure = self.model.closure(table_key)
        relevant = frozenset(f for f in ctx if f.table in closure)
        cached = self._masks.get((table_key, relevant))
        if cached is not None:
            return cached
        table = self.model.tables[table_key]
        mask = np.ones(table.rows + 1, dtype=bool)
        mask[table.rows] = table.blank_row
        for f in relevant:
            if f.table == table_key:
                mask &= self._member(table, f)
        for rel in self.model.upstream.get(table_key, []):
            upstream = frozenset(f for f in relevant if f.table in self.model.closure(rel.one))
            if not upstream:
                continue
            one = self.model.tables[rel.one]
            one_mask = self.mask(rel.one, upstream)
            if one_mask[: one.rows].all() and (one_mask[one.rows] or not one.blank_row):
                continue
            one_column = one.columns[rel.one_column]
            keys = one_column.values[one_mask[: one.rows]]
            many = table.columns[rel.many_column]
            allowed = None
            if len(keys) <= 512 and keys.dtype.kind in "iufM":
                present = (
                    keys[~np.isnan(keys)]
                    if keys.dtype.kind == "f"
                    else (keys[~np.isnat(keys)] if keys.dtype.kind == "M" else keys)
                )
                if many.kind == "date" and present.dtype.kind == "M":
                    allowed = many.rows_for(present.astype("datetime64[ns]"))
                elif many.kind in {"number", "integer"} and present.dtype.kind in "iuf":
                    allowed = many.rows_for(present.astype("float64"))
            if allowed is None:
                if many.kind == one_column.kind == "string":
                    keys = one_column.folded()[one_mask[: one.rows]]
                    many_values = many.folded()
                else:
                    many_values = many.values
                allowed = pd.Series(many_values).isin(pd.Series(keys).dropna()).to_numpy()
            if one_column.nulls()[one_mask[: one.rows]].any():
                allowed |= many.nulls()
            if one.blank_row and one_mask[one.rows]:
                allowed |= self.model.unmatched(rel)
            mask[: table.rows] &= allowed
            # This table's own blank row relates to the other table's blank row.
            mask[table.rows] &= bool(one_mask[one.rows]) if one.blank_row else False
        self._masks[(table_key, relevant)] = mask
        return mask

    def visible_values(
        self, table_key: str, column_key: str, ctx: Ctx, *, blank_row: bool = True
    ) -> list[Scalar]:
        closure = self.model.closure(table_key)
        relevant = frozenset(f for f in ctx if f.table in closure)
        memo_key = ("__values__", table_key, column_key, relevant, blank_row)
        cached = self._value_memo.get(memo_key)
        if cached is not None:
            return cached
        table = self.model.tables[table_key]
        column = table.columns[column_key]
        mask = self.mask(table_key, relevant)
        values = _distinct(column, mask[: table.rows], blank_row and bool(mask[table.rows]))
        self._value_memo[memo_key] = values
        return values

    # ------------------------------------------------------------- entry points
    def evaluate_scalar(self, text: str, ctx: Ctx = EMPTY) -> Scalar:
        node = parse_expression(text)
        if self.is_table(node, Env()):
            return self.as_scalar(self.table(node, ctx, Env()))
        return self.scalar(node, ctx, Env())

    def run_query(self, text: str) -> TableValue:
        return self.run_parsed(parse_query(text))

    def run_parsed(self, query: Query) -> TableValue:
        for measure in query.measures:
            self.measures[_key(measure.name)] = EngineMeasure(
                measure.name, measure.table, "", _node=measure.expression
            )
        env = Env()
        variables: dict[str, Scalar | TableValue] = {}
        for name, node in query.variables:
            env = replace(env, variables=variables)
            variables = {**variables, _key(name): self.value(node, EMPTY, env)}
        env = replace(env, variables=variables)
        evaluate = query.evaluates[0]
        result = self.table(evaluate.table, EMPTY, env)
        if evaluate.order_by:
            keyed = []
            for row in result.iter_rows():
                inner = replace(env, rows=(row,))
                keyed.append(
                    (
                        [self.scalar(expr, EMPTY, inner) for expr, _ in evaluate.order_by],
                        row.values,
                    )
                )
            for position in reversed(range(len(evaluate.order_by))):
                ascending = evaluate.order_by[position][1]
                keyed.sort(key=lambda item: _sort_key(item[0][position]), reverse=not ascending)
            return TableValue(result.columns, [values for _, values in keyed])
        return result

    # -------------------------------------------------------------- dispatch
    def value(self, node: Node, ctx: Ctx, env: Env) -> Scalar | TableValue:
        """A scalar or a table, as the node produces."""
        if self.is_table(node, env):
            return self.table(node, ctx, env)
        return self.scalar(node, ctx, env)

    def is_table(self, node: Node, env: Env) -> bool:
        if isinstance(node, TableCtor):
            return True
        if isinstance(node, Call):
            if node.name in {"FIRSTDATE", "LASTDATE"}:
                return True
            return node.name in _TABLE_FUNCTIONS
        if isinstance(node, Name):
            bound = env.variables.get(_key(node.name))
            if bound is not None or _key(node.name) in env.variables:
                return isinstance(bound, TableValue)
            return _key(node.name) in self.model.tables
        if isinstance(node, VarBlock):
            inner = dict(env.variables)
            for name, value in node.variables:
                inner[_key(name)] = (
                    TableValue([]) if self.is_table(value, replace(env, variables=inner)) else 0
                )
            return self.is_table(node.body, replace(env, variables=inner))
        return False

    def scalar(self, node: Node, ctx: Ctx, env: Env) -> Scalar:
        self._tick()
        if isinstance(node, Num):
            return node.value
        if isinstance(node, Str):
            return node.value
        if isinstance(node, ColRef):
            return self.column_value(node, ctx, env)
        if isinstance(node, Name):
            if node.name == "":
                return None
            key = _key(node.name)
            if key in env.variables:
                bound = env.variables[key]
                return self.as_scalar(bound) if isinstance(bound, TableValue) else bound
            if key in self.model.tables:
                return self.as_scalar(self.table(node, ctx, env))
            raise DaxEngineError(
                f"Failed to resolve name '{node.name}'. It is not a valid table, variable, "
                "or function name."
            )
        if isinstance(node, VarBlock):
            variables = dict(env.variables)
            for name, value in node.variables:
                variables[_key(name)] = self.value(value, ctx, replace(env, variables=variables))
            return self.scalar(node.body, ctx, replace(env, variables=variables))
        if isinstance(node, Unary):
            if node.op == "NOT":
                return not _truthy(self.scalar(node.operand, ctx, env))
            operand_value = self.scalar(node.operand, ctx, env)
            if node.op == "-":
                return None if operand_value is None else -_number(operand_value)
            return operand_value
        if isinstance(node, Binary):
            return self.binary(node, ctx, env)
        if isinstance(node, Call):
            return self.call(node, ctx, env)
        if isinstance(node, TableCtor):
            return self.as_scalar(self.table(node, ctx, env))
        if isinstance(node, Keyword):
            return {"ASC": 1, "DESC": 0}.get(node.word, node.word)
        raise DaxEngineError(f"Cannot evaluate {node!r}")

    def binary(self, node: Binary, ctx: Ctx, env: Env) -> Scalar:
        op = node.op
        if op == "&&":
            return _truthy(self.scalar(node.left, ctx, env)) and _truthy(
                self.scalar(node.right, ctx, env)
            )
        if op == "||":
            return _truthy(self.scalar(node.left, ctx, env)) or _truthy(
                self.scalar(node.right, ctx, env)
            )
        if op == "IN":
            left = self.scalar(node.left, ctx, env)
            table = self.table(node.right, ctx, env)
            if len(table.columns) != 1:
                raise DaxEngineError("The IN operator needs a one-column table here.")
            return any(compare("==", left, row[0]) for row in table.rows)
        left = self.scalar(node.left, ctx, env)
        right = self.scalar(node.right, ctx, env)
        if op in {"=", "==", "<>", "<", ">", "<=", ">="}:
            return compare(op, left, right)
        if op == "&":
            return _text(left) + _text(right)
        return arithmetic(op, left, right)

    # ------------------------------------------------------------ references
    def _row_value(self, env: Env, test: Callable[[Col], bool]) -> tuple[bool, Scalar]:
        for row in reversed(env.rows):
            for col, value in zip(row.columns, row.values, strict=False):
                if test(col):
                    return True, value
        return False, None

    def column_value(self, ref: ColRef, ctx: Ctx, env: Env) -> Scalar:
        """A column of the current row, or a measure ([Name], 'Table'[Measure])."""
        column_key = _key(ref.column)
        if ref.table is None:
            # Added columns (ADDCOLUMNS "Value", SUMMARIZECOLUMNS "M1") come first, then
            # measures, then a model column of the current row named alone.
            found, value = self._row_value(
                env, lambda c: c.lineage is None and _key(c.name) == column_key
            )
            if found:
                return value
            measure = self.measures.get(column_key)
            if measure is not None:
                return self.measure(measure, ctx, env)
            found, value = self._row_value(env, lambda c: _key(c.name) == column_key)
            if found:
                return value
            raise DaxEngineError(
                f"The value for '{ref.column}' cannot be determined. Either the column doesn't "
                "exist, or there is no current row for this column."
            )
        table_key = _key(ref.table)
        if table_key == "":
            found, value = self._row_value(
                env, lambda c: c.lineage is None and _key(c.name) == column_key
            )
            if found:
                return value
            raise DaxEngineError(f"The column '[{ref.column}]' cannot be found in this context.")
        found, value = self._row_value(env, lambda c: c.lineage == (table_key, column_key))
        if found:
            return value
        if self.model.has_column(ref.table, ref.column):
            raise DaxEngineError(
                f"A single value for column '{ref.column}' in table '{ref.table}' cannot be "
                "determined. This can happen when a measure formula refers to a column that "
                "contains many values without specifying an aggregation such as min, max, "
                "count, or sum to get a single result."
            )
        measure = self.measures.get(column_key)
        if measure is not None:
            return self.measure(measure, ctx, env)
        raise DaxEngineError(
            f"Column '{ref.column}' in table '{ref.table}' cannot be found or may not be "
            "used in this expression."
        )

    def measure(self, measure: EngineMeasure, ctx: Ctx, env: Env) -> Scalar:
        if env.rows:
            ctx = self.transition(ctx, env.rows)
        key = (_key(measure.name), ctx, env.scope, env.shadow)
        if key in self._memo:
            return self._memo[key]
        self._depth += 1
        if self._depth > 64:
            raise DaxEngineError(f"Measure [{measure.name}] refers to itself.")
        try:
            inner = Env(scope=env.scope, shadow=env.shadow)
            node = measure.node()
            result = (
                self.as_scalar(self.table(node, ctx, inner))
                if self.is_table(node, inner)
                else self.scalar(node, ctx, inner)
            )
        finally:
            self._depth -= 1
        self._memo[key] = result
        return result

    def as_scalar(self, table: TableValue) -> Scalar:
        if len(table.columns) != 1:
            raise DaxEngineError(
                "The expression refers to multiple columns. Multiple columns cannot be "
                "converted to a scalar value."
            )
        if len(table) == 0:
            return None
        if len(table) > 1:
            raise DaxEngineError(
                "A table of multiple values was supplied where a single value was expected."
            )
        return table.rows[0][0]

    def _column_ref(self, node: Node, what: str = "column") -> tuple[str, str]:
        if not isinstance(node, ColRef) or not node.table:
            raise DaxEngineError(f"The function expects a {what} reference like 'Table'[Column].")
        table = _key(node.table)
        if table not in self.model.tables:
            raise DaxEngineError(f"Cannot find table '{node.table}'.")
        column = _key(node.column)
        if column not in self.model.tables[table].columns:
            raise DaxEngineError(f"Column '{node.column}' in table '{node.table}' cannot be found.")
        return table, column

    def _is_column(self, node: Node) -> bool:
        return (
            isinstance(node, ColRef)
            and bool(node.table)
            and self.model.has_column(node.table or "", node.column)
        )

    # --------------------------------------------------------------- tables
    def table(self, node: Node, ctx: Ctx, env: Env) -> TableValue:
        self._tick()
        if isinstance(node, Name):
            key = _key(node.name)
            if key in env.variables:
                bound = env.variables[key]
                if not isinstance(bound, TableValue):
                    raise DaxEngineError(f"Variable '{node.name}' holds a value, not a table.")
                return bound
            if key in self.model.tables:
                # A table reference never includes the relationship's blank row:
                # Desktop counts COUNTROWS(DimEmployee) 293, VALUES(DimEmployee) 294.
                return self._base_rows(key, self.mask(key, ctx), blank_row=False)
            raise DaxEngineError(f"Cannot find table '{node.name}'.")
        if isinstance(node, TableCtor):
            return self._constructor(node, ctx, env)
        if isinstance(node, VarBlock):
            variables = dict(env.variables)
            for name, value in node.variables:
                variables[_key(name)] = self.value(value, ctx, replace(env, variables=variables))
            return self.table(node.body, ctx, replace(env, variables=variables))
        if isinstance(node, Call):
            handler = _TABLE_HANDLERS.get(node.name)
            if handler is None:
                raise DaxUnsupported(
                    f"The function {node.name} is not supported in a table expression here."
                )
            memo_key = self._table_memo_key(node, ctx, env)
            if memo_key is not None:
                cached = self._table_memo.get(memo_key)
                if cached is not None:
                    return cached
            result = handler(self, node.args, ctx, env)
            if memo_key is not None:
                self._tokens += 1
                result.token = self._tokens
                self._table_memo[memo_key] = result
            return result
        if isinstance(node, ColRef):
            raise DaxEngineError("A column reference cannot be used as a table here. Use VALUES().")
        raise DaxEngineError("The expression does not return a table.")

    def _table_memo_key(self, node: Call, ctx: Ctx, env: Env) -> MemoKey | None:
        """A cache key for ALL(...) and FILTER(ALL(...), row-only condition) tables.

        Their rows depend on the context only through the filters a row would replace,
        so every agent's RANKX(FILTER(ALL(DimEmployee[EmployeeKey]), ...)) shares one.
        """
        if node.name in {"ALL", "ALLNOBLANKROW"}:
            return ("__table__", id(node))
        if node.name != "FILTER" or len(node.args) != 2:
            return None
        inner = node.args[0]
        if not (isinstance(inner, Call) and inner.name in {"ALL", "ALLNOBLANKROW"} and inner.args):
            return None
        if all(self._is_column(a) for a in inner.args):
            lineage = tuple(self._column_ref(a) for a in inner.args)
        elif (
            len(inner.args) == 1
            and isinstance(inner.args[0], Name)
            and _key(inner.args[0].name) in self.model.tables
        ):
            table = _key(inner.args[0].name)
            lineage = tuple((table, c) for c in self.model.tables[table].columns)
        else:
            return None
        if not self._row_pure(node.args[1], env, frozenset(t for t, _ in lineage)):
            return None
        variables = self._referenced_variables(node.args[1], env)
        if variables is None:
            return None
        return (
            "__table__",
            id(node),
            self._reduced(ctx, lineage),
            variables,
            tuple(id(r) for r in env.rows),
            env.scope,
            env.shadow,
        )

    def _base_rows(self, table_key: str, mask: np.ndarray, *, blank_row: bool = True) -> TableValue:
        table = self.model.tables[table_key]
        if not blank_row and mask[table.rows]:
            mask = mask.copy()
            mask[table.rows] = False
        cols = [Col(c.name, (table_key, k)) for k, c in table.columns.items()]
        indices = np.flatnonzero(mask)
        return TableValue(cols, base=table_key, indices=indices, model=self.model)

    def _constructor(self, node: TableCtor, ctx: Ctx, env: Env) -> TableValue:
        rows = [tuple(self.scalar(v, ctx, env) for v in row) for row in node.rows]
        width = max((len(r) for r in node.rows), default=1)
        if any(len(r) != width for r in rows):
            raise DaxEngineError("Each row of a table constructor needs the same number of values.")
        names = ["Value"] if width == 1 else [f"Value{i + 1}" for i in range(width)]
        return TableValue([Col(n) for n in names], rows)

    def column_table(
        self,
        refs: Sequence[tuple[str, str]],
        ctx: Ctx | None,
        *,
        blank_row: bool = True,
        distinct_only: bool = False,
    ) -> TableValue:
        """Distinct combinations of columns of one table: ALL (ctx None) or VALUES."""
        tables = {t for t, _ in refs}
        if len(tables) != 1:
            raise DaxEngineError("All column arguments must be from the same table.")
        table_key = next(iter(tables))
        table = self.model.tables[table_key]
        mask = (
            np.concatenate([np.ones(table.rows, dtype=bool), [table.blank_row]])
            if ctx is None
            else self.mask(table_key, ctx)
        )
        cols = [Col(table.columns[c].name, (table_key, c)) for _, c in refs]
        include_blank = blank_row and bool(mask[table.rows]) and not distinct_only
        if len(refs) == 1:
            values = _distinct(table.columns[refs[0][1]], mask[: table.rows], include_blank)
            return TableValue(cols, [(v,) for v in values])
        frame = pd.DataFrame(
            {c: table.columns[c].values[mask[: table.rows]] for _, c in refs}
        ).drop_duplicates()
        rows = [
            tuple(_python(v, table.columns[c].kind) for v, (_, c) in zip(r, refs, strict=True))
            for r in frame.itertuples(index=False, name=None)
        ]
        if include_blank:
            rows.append(tuple(None for _ in refs))
        return TableValue(cols, rows)

    # --------------------------------------------------------- calls (scalar)
    def call(self, node: Call, ctx: Ctx, env: Env) -> Scalar:
        handler = _SCALAR_HANDLERS.get(node.name)
        if handler is not None:
            return handler(self, node.args, ctx, env)
        if node.name in _TABLE_FUNCTIONS or node.name in _TABLE_HANDLERS:
            return self.as_scalar(self.table(node, ctx, env))
        raise DaxUnsupported(f"The function {node.name} is not supported by the local engine.")

    # ------------------------------------------------------ CALCULATE family
    def calculate_context(self, args: Sequence[Node], ctx: Ctx, env: Env) -> Ctx:
        """The context CALCULATE's expression sees, from its filter arguments."""
        removals: list[tuple[str | None, list[str], bool]] = []  # (table, cols, selected)
        filters: list[tuple[list[Filter], bool]] = []
        for arg in args:
            self._filter_argument(arg, ctx, env, removals, filters, keep=False)
        new = self.transition(ctx, env.rows) if env.rows else ctx
        for table, cols, selected in removals:
            if selected:
                new = self._allselected(new, table, cols, env)
            else:
                new = self.remove(new, table, cols)
        # The filter arguments replace the outer filters on their columns and intersect
        # with each other: 'FactSales'[DateKey] >= DATE(2009, 12, 1) and < DATE(2010, 1, 1)
        # is December, where applying them one by one kept only the second.
        replacing = [f for group, keep in filters if not keep for f in group]
        new = self.apply(new, replacing, together=True)
        kept = [f for group, keep in filters if keep for f in group]
        return self.apply(new, kept, keep=True)

    def _allselected(self, ctx: Ctx, table: str | None, cols: list[str], env: Env) -> Ctx:
        shadow = env.shadow if env.shadow is not None else EMPTY
        if table is None:
            return shadow
        targets = set(cols) if cols else None
        kept = {
            f
            for f in ctx
            if f.table != table or (targets is not None and not targets.intersection(f.columns))
        }
        restored = {
            f
            for f in shadow
            if f.table == table and (targets is None or targets.intersection(f.columns))
        }
        return frozenset(kept | restored)

    def _filter_argument(
        self,
        arg: Node,
        ctx: Ctx,
        env: Env,
        removals: list[tuple[str | None, list[str], bool]],
        filters: list[tuple[list[Filter], bool]],
        *,
        keep: bool,
    ) -> None:
        if isinstance(arg, Call) and arg.name in _MODIFIERS and not keep:
            if arg.name == "ALLEXCEPT":
                table_node, *keep_cols = arg.args
                table = self._table_name(table_node)
                kept = {self._column_ref(c)[1] for c in keep_cols}
                expanded = self.model.closure(table)
                for other in expanded:
                    columns = [
                        c
                        for c in self.model.tables[other].columns
                        if other != table or c not in kept
                    ]
                    removals.append((other, columns, False))
                return
            selected = arg.name == "ALLSELECTED"
            if not arg.args:
                removals.append((None, [], selected))
                return
            first = arg.args[0]
            if isinstance(first, Name):
                removals.append((self._table_name(first), [], selected))
                return
            refs = [self._column_ref(a) for a in arg.args]
            for table in {t for t, _ in refs}:
                removals.append((table, [c for t, c in refs if t == table], selected))
            return
        if isinstance(arg, Call) and arg.name == "KEEPFILTERS":
            if len(arg.args) != 1:
                raise DaxEngineError("KEEPFILTERS takes one argument.")
            self._filter_argument(arg.args[0], ctx, env, removals, filters, keep=True)
            return
        if isinstance(arg, Call) and arg.name in {"USERELATIONSHIP", "CROSSFILTER"}:
            raise DaxUnsupported(
                f"{arg.name} (changing which relationship filters) is not supported by the "
                "local engine."
            )
        if self.is_table(arg, env):
            filters.append((self.table_filters(self.table(arg, ctx, env)), keep))
            return
        # A Boolean filter: FILTER(ALL(the columns it names), condition).
        refs = sorted(self._predicate_columns(arg, env))
        for node in _nodes(arg):
            if (
                isinstance(node, ColRef)
                and node.table
                and not self.model.has_column(node.table, node.column)
                and _key(node.column) not in self.measures
            ):
                raise DaxEngineError(
                    f"Column '{node.column}' in table '{node.table}' cannot be found or "
                    "may not be used in this expression."
                )
        if not refs:
            raise DaxEngineError(
                "A filter condition must refer to a column, such as 'Table'[Column] = \"value\"."
            )
        if len({t for t, _ in refs}) > 1:
            raise DaxEngineError(
                "The expression contains columns from multiple tables, but only columns from a "
                "single table can be used in a True/False expression that is used as a table "
                "filter expression."
            )
        if self._uses_measure(arg, env):
            raise DaxEngineError(
                "A function 'CALCULATE' has been used in a True/False expression that is used "
                "as a table filter expression. This is not allowed."
            )
        # 'FactSales'[DateKey] >= DATE(2009, 12, 1) keeps the same rows in every context:
        # MAN-03's query tested it for 1,096 dates per agent, 644,000 times (18 s).
        constant = self._constant_condition(arg, set(refs))
        memo_key = ("__condition__", id(arg))
        cached = self._condition_memo.get(memo_key) if constant else None
        if cached is not None and cached[0] is arg:
            filters.append((list(cached[1]), keep))
            return
        domain = self.column_table(refs, None)
        kept_rows = [
            row.values
            for row in domain.iter_rows()
            if _truthy(self.scalar(arg, ctx, replace(env, rows=(*env.rows, row))))
        ]
        found = self.table_filters(TableValue(domain.columns, kept_rows))
        if constant:
            self._condition_memo[memo_key] = (arg, found)
        filters.append((found, keep))

    def _constant_condition(self, node: Node, refs: set[tuple[str, str]]) -> bool:
        """A condition reading only its own columns and constants, never the context."""
        if isinstance(node, Num | Str | Keyword):
            return True
        if isinstance(node, ColRef):
            return bool(node.table) and (_key(node.table or ""), _key(node.column)) in refs
        if isinstance(node, Binary):
            return self._constant_condition(node.left, refs) and self._constant_condition(
                node.right, refs
            )
        if isinstance(node, Unary):
            return self._constant_condition(node.operand, refs)
        if isinstance(node, TableCtor):
            return all(self._constant_condition(v, refs) for row in node.rows for v in row)
        if isinstance(node, Call):
            return (
                node.name in _CONTEXT_FREE
                and node.name != "RELATED"
                and all(self._constant_condition(a, refs) for a in node.args)
            )
        return False

    def _predicate_columns(self, node: Node, env: Env) -> set[tuple[str, str]]:
        found: set[tuple[str, str]] = set()

        def walk(n: Node) -> None:
            if isinstance(n, ColRef) and n.table and self.model.has_column(n.table, n.column):
                found.add((_key(n.table), _key(n.column)))
            elif isinstance(n, Call):
                for a in n.args:
                    walk(a)
            elif isinstance(n, Binary):
                walk(n.left)
                walk(n.right)
            elif isinstance(n, Unary):
                walk(n.operand)
            elif isinstance(n, TableCtor):
                for row in n.rows:
                    for v in row:
                        walk(v)

        walk(node)
        return found

    def _uses_measure(self, node: Node, env: Env) -> bool:
        if isinstance(node, ColRef):
            if node.table and self.model.has_column(node.table, node.column):
                return False
            return _key(node.column) in self.measures
        if isinstance(node, Call):
            return node.name in {"CALCULATE", "CALCULATETABLE"} or any(
                self._uses_measure(a, env) for a in node.args
            )
        if isinstance(node, Binary):
            return self._uses_measure(node.left, env) or self._uses_measure(node.right, env)
        if isinstance(node, Unary):
            return self._uses_measure(node.operand, env)
        return False

    def table_filters(self, table: TableValue) -> list[Filter]:
        """A table used as a filter: one filter per model table among its columns."""
        by_table: dict[str, list[int]] = {}
        for i, col in enumerate(table.columns):
            if col.lineage:
                by_table.setdefault(col.lineage[0], []).append(i)
        filters = []
        rows = table.rows
        for table_key, positions in by_table.items():
            cols = tuple(cast(tuple[str, str], table.columns[i].lineage)[1] for i in positions)
            values: frozenset[FilterValue]
            if len(positions) == 1:
                values = frozenset(r[positions[0]] for r in rows)
            else:
                values = frozenset(tuple(r[i] for i in positions) for r in rows)
            filters.append(Filter(table_key, cols, values))
        return filters

    def _table_name(self, node: Node) -> str:
        if not isinstance(node, Name):
            raise DaxEngineError("The function expects a table reference.")
        key = _key(node.name)
        if key not in self.model.tables:
            raise DaxEngineError(f"Cannot find table '{node.name}'.")
        return key

    # ---------------------------------------------------------- iteration
    def iterate(
        self,
        table_node: Node,
        expr: Node,
        ctx: Ctx,
        env: Env,
    ) -> list[Scalar]:
        """Evaluate expr once per row of the table, in that row's context."""
        return self.per_row(self.table(table_node, ctx, env), expr, ctx, env)

    def per_row(self, table: TableValue, expr: Node, ctx: Ctx, env: Env) -> list[Scalar]:
        """expr for each row of a table, each in its own row context.

        When the row acts only through context transition (measures, CALCULATE), the
        value depends on the row and on the context without the row's columns, so it
        is cached on that: the dashboard's table visual ranks 294 agents against all
        294 agents per row, and recomputing the inner ranking for each outer row took
        four million measure calls and a minute.
        """
        vector = self.vector(expr, table, ctx, env)
        if vector is not None:
            return vector
        cache: dict[RowValues, Scalar] | None = None
        lineage = tuple(c.lineage for c in table.columns if c.lineage)
        if (
            table.columns
            and len(lineage) == len(table.columns)
            and self._row_pure(expr, env, frozenset(t for t, _ in lineage))
        ):
            variables = self._referenced_variables(expr, env)
            if variables is not None:
                key = (
                    "__rows__",
                    id(expr),
                    lineage,
                    self._reduced(ctx, lineage),
                    variables,
                    tuple(id(r) for r in env.rows),
                    env.scope,
                    env.shadow,
                )
                cache = self._row_memo.setdefault(key, {})
                if table.token:
                    whole = ("__column__", key, table.token)
                    done = self._value_memo.get(whole)
                    if done is not None:
                        return done
                    values = self._fill(table, expr, ctx, env, cache)
                    self._value_memo[whole] = values
                    return values
        return self._fill(table, expr, ctx, env, cache)

    def _fill(
        self,
        table: TableValue,
        expr: Node,
        ctx: Ctx,
        env: Env,
        cache: dict[RowValues, Scalar] | None,
    ) -> list[Scalar]:
        out: list[Scalar] = []
        for row in table.iter_rows():
            self._tick()
            if cache is not None and row.values in cache:
                out.append(cache[row.values])
                continue
            value = self.scalar(expr, ctx, replace(env, rows=(*env.rows, row)))
            if cache is not None:
                cache[row.values] = value
            out.append(value)
        return out

    def _reduced(self, ctx: Ctx, lineage: Sequence[tuple[str, str]]) -> Ctx:
        """The context without the filters a row of these columns would replace."""
        current = set(ctx)
        by_table: dict[str, set[str]] = {}
        for table, column in lineage:
            by_table.setdefault(table, set()).add(column)
        for table, columns in by_table.items():
            whole = len(columns) >= len(self.model.tables[table].columns)
            if whole or any((table, c) in self.model.date_keys for c in columns):
                current = {f for f in current if f.table != table}
            else:
                current = self._without_columns(current, table, columns)
        return frozenset(current)

    def _row_pure(self, node: Node, env: Env, iterated: frozenset[str]) -> bool:
        """Whether node sees the iterated tables' filters only through context transition.

        Measures and CALCULATE transition the row; other reads of the filter context
        are allowed when no iterated table's filters reach them: SELECTEDVALUE of the
        'Actuals-vs-PctChange' slicer is the same for every agent.
        """

        def pure(n: Node, variables: frozenset[str]) -> bool:
            if isinstance(n, Num | Str | Keyword | ColRef):
                return True
            if isinstance(n, Name):
                return n.name == "" or _key(n.name) in variables
            if isinstance(n, Binary):
                return pure(n.left, variables) and pure(n.right, variables)
            if isinstance(n, Unary):
                return pure(n.operand, variables)
            if isinstance(n, TableCtor):
                return all(pure(v, variables) for row in n.rows for v in row)
            if isinstance(n, VarBlock):
                inner = set(variables)
                for name, value in n.variables:
                    if not pure(value, frozenset(inner)):
                        return False
                    inner.add(_key(name))
                return pure(n.body, frozenset(inner))
            if isinstance(n, Call):
                if n.name in _CONTEXT_FREE or n.name == "CALCULATE":
                    return all(pure(a, variables) for a in n.args)
                tables = self._tables_read(n)
                return (
                    tables is not None
                    and not any(self.model.closure(t) & iterated for t in tables)
                    and all(pure(a, variables) for a in n.args if not self._is_column(a))
                )
            return False

        return pure(node, frozenset(env.variables))

    def _tables_read(self, node: Node) -> set[str] | None:
        """Model tables a context-reading call names, or None if it names something else."""
        tables: set[str] = set()

        def walk(n: Node) -> bool:
            if isinstance(n, ColRef):
                if n.table and self.model.has_column(n.table, n.column):
                    tables.add(_key(n.table))
                    return True
                return n.table is None and _key(n.column) not in self.measures
            if isinstance(n, Name):
                if _key(n.name) in self.model.tables:
                    tables.add(_key(n.name))
                return True
            if isinstance(n, Call):
                return all(walk(a) for a in n.args)
            if isinstance(n, Binary):
                return walk(n.left) and walk(n.right)
            if isinstance(n, Unary):
                return walk(n.operand)
            return isinstance(n, Num | Str | Keyword)

        return tables if walk(node) else None

    def _referenced_variables(self, node: Node, env: Env) -> tuple[tuple[str, Scalar], ...] | None:
        names: set[str] = set()

        def walk(n: Node) -> None:
            if isinstance(n, Name) and _key(n.name) in env.variables:
                names.add(_key(n.name))
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
            elif isinstance(n, TableCtor):
                for row in n.rows:
                    for v in row:
                        walk(v)

        walk(node)
        values = []
        for name in sorted(names):
            value = env.variables[name]
            if isinstance(value, TableValue):
                return None
            values.append((name, value))
        return tuple(values)

    def vector(self, expr: Node, table: TableValue, ctx: Ctx, env: Env) -> list[Scalar] | None:
        """Whole-column evaluation over a model table's rows when expr only reads that
        row's columns (SUMX(FactSales, FactSales[SalesAmount] - FactSales[TotalCost]))."""
        if table.base is None or table.indices is None or len(table) < 2000:
            return None
        arrays = _Vector(self, table, env).evaluate(expr)
        if arrays is None:
            return None
        return arrays

    # ------------------------------------------------------------ dates
    def dates(self, node: Node, ctx: Ctx, env: Env) -> tuple[str, str, list[datetime]]:
        """(table, column, visible dates) for a time-intelligence dates argument."""
        if isinstance(node, ColRef) and node.table:
            table, column = self._column_ref(node, "date column")
            if self.model.tables[table].columns[column].kind != "date":
                raise DaxEngineError(
                    "A time intelligence function needs a column of dates, such as 'Date'[Date]."
                )
            # 'Date'[Date] here is CALCULATETABLE(DISTINCT('Date'[Date])): the current row
            # becomes a filter, so ADDCOLUMNS(VALUES('Date'[Year]), "PY", CALCULATE([Sales],
            # SAMEPERIODLASTYEAR('Date'[Date]))) is each year's previous year (Desktop).
            if env.rows:
                ctx = self.transition(ctx, env.rows)
            closure = self.model.closure(table)
            memo_key = (
                "__visible_dates__",
                table,
                column,
                frozenset(f for f in ctx if f.table in closure),
            )
            cached = self._date_memo.get(memo_key)
            if cached is None:
                cached = sorted(
                    {
                        _day(v)
                        for v in self.visible_values(table, column, ctx, blank_row=False)
                        if isinstance(v, datetime)
                    }
                )
                self._date_memo[memo_key] = cached
            return table, column, cached
        result = self.table(node, ctx, env)
        if len(result.columns) != 1 or not result.columns[0].lineage:
            raise DaxEngineError("The dates argument must be a column of dates.")
        table, column = result.columns[0].lineage
        values = sorted({_day(r[0]) for r in result.rows if isinstance(r[0], datetime)})
        return table, column, values

    def all_dates(self, table: str, column: str) -> list[datetime]:
        cached = self._date_memo.get(("__dates__", table, column))
        if cached is None:
            values = self.visible_values(table, column, EMPTY, blank_row=False)
            cached = sorted({_day(v) for v in values if isinstance(v, datetime)})
            self._date_memo[("__dates__", table, column)] = cached
        return cached

    def date_table(self, table: str, column: str, values: Iterable[datetime]) -> TableValue:
        existing = self._date_sets.get(("__dateset__", table, column))
        if existing is None:
            existing = frozenset(self.all_dates(table, column))
            self._date_sets[("__dateset__", table, column)] = existing
        kept = sorted({_day(v) for v in values} & existing)
        name = self.model.tables[table].columns[column].name
        return TableValue([Col(name, (table, column))], [(v,) for v in kept])


# Scalar functions whose result depends only on their arguments, not on the filter context.
_CONTEXT_FREE = {
    "IF",
    "IF.EAGER",
    "SWITCH",
    "AND",
    "OR",
    "NOT",
    "TRUE",
    "FALSE",
    "BLANK",
    "ISBLANK",
    "ISNUMBER",
    "ISTEXT",
    "ISNONTEXT",
    "ISLOGICAL",
    "COALESCE",
    "DIVIDE",
    "ABS",
    "ROUND",
    "ROUNDUP",
    "ROUNDDOWN",
    "INT",
    "TRUNC",
    "MOD",
    "SQRT",
    "POWER",
    "EXP",
    "LN",
    "LOG10",
    "LOG",
    "SIGN",
    "CEILING",
    "FLOOR",
    "MROUND",
    "QUOTIENT",
    "FORMAT",
    "REPT",
    "CONCATENATE",
    "LEN",
    "UPPER",
    "LOWER",
    "TRIM",
    "LEFT",
    "RIGHT",
    "MID",
    "SUBSTITUTE",
    "UNICHAR",
    "EXACT",
    "CONTAINSSTRING",
    "CONTAINSSTRINGEXACT",
    "SEARCH",
    "FIND",
    "VALUE",
    "CONVERT",
    "DATE",
    "YEAR",
    "MONTH",
    "DAY",
    "QUARTER",
    "HOUR",
    "MINUTE",
    "WEEKDAY",
    "EOMONTH",
    "EDATE",
    "DATEDIFF",
    "RELATED",
}


class _Vector:
    """Numpy evaluation of a row expression over a model table's rows."""

    def __init__(self, engine: Engine, table: TableValue, env: Env) -> None:
        self.engine, self.table, self.env = engine, table, env
        self.base = table.base or ""
        self.indices = table.indices if table.indices is not None else np.array([], int)
        self.model_table = engine.model.tables[self.base]

    def evaluate(self, node: Node) -> list[Scalar] | None:
        result = self._eval(node)
        if result is None:
            return None
        kind, values = result
        if kind == "number":
            return [None if math.isnan(v) else float(v) for v in values]
        if kind == "bool":
            return [bool(v) for v in values]
        return None

    def _column(self, ref: ColRef) -> tuple[str, np.ndarray] | None:
        table_key = _key(ref.table) if ref.table else self.base
        column_key = _key(ref.column)
        if table_key != self.base or column_key not in self.model_table.columns:
            return None
        if ref.table is None and column_key in self.engine.measures:
            return None
        column = self.model_table.columns[column_key]
        if column.kind not in {"number", "integer"}:
            return None
        values = column.values.astype("float64")
        extended = np.append(values, np.nan)
        return "number", extended[self.indices]

    def _eval(self, node: Node) -> tuple[str, np.ndarray] | None:
        n = len(self.indices)
        if isinstance(node, Num):
            return "number", np.full(n, float(node.value))
        if isinstance(node, ColRef):
            return self._column(node)
        if isinstance(node, Name):
            key = _key(node.name)
            value = self.env.variables.get(key)
            if isinstance(value, bool):
                return "bool", np.full(n, value)
            if isinstance(value, int | float):
                return "number", np.full(n, float(value))
            return None
        if isinstance(node, Unary) and node.op == "-":
            inner = self._eval(node.operand)
            if inner is None or inner[0] != "number":
                return None
            return "number", -inner[1]
        if isinstance(node, Unary) and node.op == "NOT":
            inner = self._eval(node.operand)
            if inner is None or inner[0] != "bool":
                return None
            return "bool", ~inner[1]
        if isinstance(node, Binary):
            left, right = self._eval(node.left), self._eval(node.right)
            if left is None or right is None:
                return None
            if node.op in {"&&", "||"}:
                if left[0] != "bool" or right[0] != "bool":
                    return None
                return "bool", (left[1] & right[1]) if node.op == "&&" else (left[1] | right[1])
            if left[0] != "number" or right[0] != "number":
                return None
            a, b = left[1], right[1]
            a_blank, b_blank = np.isnan(a), np.isnan(b)
            if node.op in {"+", "-"}:
                out = np.nan_to_num(a) + (np.nan_to_num(b) if node.op == "+" else -np.nan_to_num(b))
                out[a_blank & b_blank] = np.nan
                return "number", out
            if node.op == "*":
                return "number", a * b
            if node.op == "/":
                with np.errstate(divide="ignore", invalid="ignore"):
                    out = np.nan_to_num(a) / np.nan_to_num(b)
                out[a_blank & b_blank] = np.nan
                return "number", out
            if node.op in {"=", "<>", "<", ">", "<=", ">="}:
                x, y = np.nan_to_num(a), np.nan_to_num(b)
                ops: dict[str, Callable[[Any, Any], np.ndarray]] = {
                    "=": np.equal,
                    "<>": np.not_equal,
                    "<": np.less,
                    ">": np.greater,
                    "<=": np.less_equal,
                    ">=": np.greater_equal,
                }
                return "bool", ops[node.op](x, y)
            return None
        if isinstance(node, Call):
            if node.name == "ISBLANK" and len(node.args) == 1:
                inner = self._eval(node.args[0])
                if inner is None or inner[0] != "number":
                    return None
                return "bool", np.isnan(inner[1])
            if node.name in {"TRUE", "FALSE"} and not node.args:
                return "bool", np.full(n, node.name == "TRUE")
            if node.name == "DIVIDE" and len(node.args) in {2, 3}:
                parts = [self._eval(a) for a in node.args]
                if any(p is None or p[0] != "number" for p in parts):
                    return None
                numeric_parts = [p[1] for p in parts if p is not None]
                a, b = numeric_parts[0], numeric_parts[1]
                alt = numeric_parts[2] if len(numeric_parts) == 3 else np.full(n, np.nan)
                with np.errstate(divide="ignore", invalid="ignore"):
                    out = np.where((np.nan_to_num(b) == 0), alt, np.nan_to_num(a) / b)
                out[np.isnan(a)] = np.where(np.nan_to_num(b) == 0, alt, np.nan)[np.isnan(a)]
                return "number", out
            if node.name == "ABS" and len(node.args) == 1:
                inner = self._eval(node.args[0])
                return (
                    None if inner is None or inner[0] != "number" else ("number", np.abs(inner[1]))
                )
            if node.name == "IF" and len(node.args) in {2, 3}:
                cond = self._eval(node.args[0])
                then = self._eval(node.args[1])
                other = (
                    self._eval(node.args[2])
                    if len(node.args) == 3
                    else ("number", np.full(n, np.nan))
                )
                if cond is None or then is None or other is None:
                    return None
                if cond[0] != "bool" or then[0] != other[0]:
                    return None
                return then[0], np.where(cond[1], then[1], other[1])
            if node.name == "RELATED" and len(node.args) == 1:
                return self._related(node.args[0])
        return None

    def _related(self, ref: Node) -> tuple[str, np.ndarray] | None:
        if not isinstance(ref, ColRef) or not ref.table:
            return None
        target = _key(ref.table)
        path = _related_path(self.engine.model, self.base, target)
        if path is None:
            return None
        rows = self.indices.copy()
        current = self.base
        for rel in path:
            many = self.engine.model.tables[current].columns[rel.many_column]
            one_table = self.engine.model.tables[rel.one]
            one_column = one_table.columns[rel.one_column]
            one_keys = one_column.folded() if one_column.kind == "string" else one_column.values
            lookup = pd.Series(np.arange(one_table.rows), index=one_keys)
            lookup = lookup[~lookup.index.duplicated()]
            many_values = many.folded() if many.kind == "string" else many.values
            valid = rows < len(many.values)
            mapped = np.full(len(rows), one_table.rows)
            mapped[valid] = (
                pd.Series(many_values[rows[valid]])
                .map(lookup)
                .fillna(one_table.rows)
                .to_numpy(dtype=int)
            )
            rows = mapped
            current = rel.one
        column = self.engine.model.tables[target].columns.get(_key(ref.column))
        if column is None or column.kind not in {"number", "integer"}:
            return None
        return "number", np.append(column.values.astype("float64"), np.nan)[rows]


def _nodes(node: Node) -> list[Node]:
    found = [node]
    if isinstance(node, Call):
        for arg in node.args:
            found.extend(_nodes(arg))
    elif isinstance(node, Binary):
        found.extend(_nodes(node.left) + _nodes(node.right))
    elif isinstance(node, Unary):
        found.extend(_nodes(node.operand))
    return found


def _related_path(model: Model, start: str, target: str) -> list[EngineRelationship] | None:
    frontier: list[tuple[str, list[EngineRelationship]]] = [(start, [])]
    seen = {start}
    while frontier:
        table, path = frontier.pop(0)
        if table == target:
            return path
        for rel in model.upstream.get(table, []):
            if rel.one not in seen:
                seen.add(rel.one)
                frontier.append((rel.one, [*path, rel]))
    return None


def _fold(value: Scalar) -> Scalar:
    if isinstance(value, str):
        return value.casefold()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    return value


def _isin(column: EngineColumn, values: frozenset[FilterValue]) -> np.ndarray:
    wanted = [v for v in values if v is not None]
    if column.kind == "string":
        folded = {v.casefold() for v in wanted if isinstance(v, str)}
        mask = pd.Series(column.folded(), dtype=object).isin(folded).to_numpy()
    elif column.kind == "date":
        stamps = [pd.Timestamp(v) for v in wanted if isinstance(v, datetime | date)]
        mask = pd.Series(column.values).isin(stamps).to_numpy()
    elif column.kind == "boolean":
        bools = {v for v in wanted if isinstance(v, bool)}
        mask = (
            np.array([v in bools for v in column.values], dtype=bool)
            if bools
            else np.zeros(len(column.values), dtype=bool)
        )
    else:
        numbers = [v for v in wanted if isinstance(v, int | float) and not isinstance(v, bool)]
        mask = pd.Series(column.values).isin(numbers).to_numpy()
    if None in values:
        mask = mask | column.nulls()
    return mask


def _distinct(column: EngineColumn, mask: np.ndarray, blank_row: bool) -> list[Scalar]:
    selected = column.values[mask]
    values: list[Scalar]
    if column.kind == "date":
        unique = pd.unique(pd.Series(selected).dropna())
        values = list(sorted(pd.Timestamp(v).to_pydatetime() for v in unique))
        has_null = bool(np.isnat(selected).any())
    elif column.kind in {"number", "integer"}:
        series = pd.Series(selected)
        unique = pd.unique(series.dropna())
        # A numeric column with missing values removed normalizes to Python numbers.
        values = list(sorted(cast(int | float, _python(v, column.kind)) for v in unique))
        has_null = bool(series.isna().any())
    else:
        series = pd.Series(selected, dtype=object)
        unique = pd.unique(series.dropna())
        values = sorted((_python(v, column.kind) for v in unique), key=_sort_key)
        has_null = bool(series.isna().any())
    if has_null or blank_row:
        values = [None, *values]
    return values


# -------------------------------------------------------------- scalar functions

Handler = Callable[[Engine, Sequence[Node], Ctx, Env], Scalar]
TableHandler = Callable[[Engine, Sequence[Node], Ctx, Env], TableValue]
_SCALAR_HANDLERS: dict[str, Handler] = {}
_TABLE_HANDLERS: dict[str, TableHandler] = {}


def _scalar(*names: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        for name in names:
            _SCALAR_HANDLERS[name] = fn
        return fn

    return register


def _table(*names: str) -> Callable[[TableHandler], TableHandler]:
    def register(fn: TableHandler) -> TableHandler:
        for name in names:
            _TABLE_HANDLERS[name] = fn
        return fn

    return register


def _args(name: str, args: Sequence[Node], low: int, high: int | None = None) -> None:
    high = low if high is None else high
    if not low <= len(args) <= high:
        expected = str(low) if low == high else f"{low} to {high}"
        raise DaxEngineError(
            f"Too {'few' if len(args) < low else 'many'} arguments were passed to the {name} "
            f"function. The expected number of arguments is {expected}."
        )


def _values_array(column: EngineColumn, mask: np.ndarray) -> list[Scalar]:
    selected = column.values[mask]
    if column.kind in {"number", "integer"}:
        return [
            None if (isinstance(v, float) and math.isnan(v)) else _python(v, column.kind)
            for v in selected.tolist()
        ]
    return [_python(v, column.kind) for v in selected]


def _aggregated_column(e: Engine, node: Node, fn: str) -> tuple[str, str]:
    if not isinstance(node, ColRef) or not node.table:
        raise DaxEngineError(f"The {fn} function only accepts a column reference as an argument.")
    if not e.model.has_column(node.table, node.column):
        raise DaxEngineError(f"Column '{node.column}' in table '{node.table}' cannot be found.")
    return _key(node.table), _key(node.column)


def column_aggregate(e: Engine, fn: str, table_key: str, column_key: str, ctx: Ctx) -> Scalar:
    """SUM/AVERAGE/MIN/MAX/COUNT/... of a column over its visible rows, whole-column.

    Memoized on the filters that reach the table: _ShowValueForDates asks for
    MAX(FactSales[DateKey]) with all filters removed once per employee, and reading
    200,000 rows value by value each time took five minutes for one card.
    """
    closure = e.model.closure(table_key)
    relevant = frozenset(f for f in ctx if f.table in closure)
    memo_key = ("__agg__", fn, table_key, column_key, relevant)
    if memo_key in e._memo:
        return e._memo[memo_key]
    table = e.model.tables[table_key]
    column = table.columns[column_key]
    visible = e.mask(table_key, relevant)
    mask = visible[: table.rows]
    nulls = column.nulls()[mask]
    present = column.values[mask][~nulls]
    numeric = column.kind in {"number", "integer"}
    result: Scalar
    if fn in {"SUM", "AVERAGE"}:
        if not numeric:
            kind = {"string": "String", "date": "Date", "boolean": "Boolean"}[column.kind]
            raise DaxEngineError(f"The function {fn} cannot work with values of type {kind}.")
        if not len(present):
            result = None
        elif fn == "SUM":
            total = float(present.astype("float64").sum())
            result = int(total) if column.kind == "integer" and total.is_integer() else total
        else:
            result = float(present.astype("float64").mean())
    elif fn in {"MIN", "MAX"}:
        if not len(present):
            result = None
        elif numeric or column.kind == "date":
            picked = present.min() if fn == "MIN" else present.max()
            result = _python(picked, column.kind)
        else:
            values = [_python(v, column.kind) for v in present]
            result = (min if fn == "MIN" else max)(values, key=_sort_key)
    elif fn in {"COUNT", "COUNTA"}:
        result = int(len(present)) or None
    elif fn == "COUNTBLANK":
        empty = int(nulls.sum())
        if column.kind == "string":
            empty += int(sum(1 for v in present if v == ""))
        result = empty or None
    elif fn == "DISTINCTCOUNT":
        # Data blanks count as one value; the relationship's blank row does not
        # (Desktop: DISTINCTCOUNT(DimEmployee[EmployeeKey]) is 293, VALUES has 294).
        count = int(len(pd.unique(pd.Series(present)))) + (1 if nulls.any() else 0)
        result = count or None
    elif fn == "DISTINCTCOUNTNOBLANK":
        result = int(len(pd.unique(pd.Series(present)))) or None
    else:
        raise DaxUnsupported(f"{fn} is not supported here.")
    e._memo[memo_key] = result
    return result


def _column_function(fn: str) -> Handler:
    def handler(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
        if fn in {"MIN", "MAX"} and len(args) == 2:
            a, b = e.scalar(args[0], ctx, env), e.scalar(args[1], ctx, env)
            if a is None:
                return b
            if b is None:
                return a
            return (min if fn == "MIN" else max)([a, b], key=_sort_key)
        _args(fn, args, 1)
        node = args[0]
        if (
            isinstance(node, ColRef)
            and node.table is not None
            and not e.model.has_column(node.table, node.column)
        ):
            # A column of a table constructor or variable (''[Value]) in a row context.
            return e.scalar(node, ctx, env)
        table_key, column_key = _aggregated_column(e, node, fn)
        return column_aggregate(e, fn, table_key, column_key, ctx)

    return handler


for _fn in (
    "SUM",
    "AVERAGE",
    "MIN",
    "MAX",
    "COUNT",
    "COUNTA",
    "COUNTBLANK",
    "DISTINCTCOUNT",
    "DISTINCTCOUNTNOBLANK",
):
    _SCALAR_HANDLERS[_fn] = _column_function(_fn)


@_scalar("COUNTROWS")
def _countrows(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("COUNTROWS", args, 0, 1)
    if not args:
        raise DaxUnsupported("COUNTROWS() without a table is not supported here.")
    node = args[0]
    if (
        isinstance(node, Name)
        and _key(node.name) in e.model.tables
        and _key(node.name) not in env.variables
    ):
        table_key = _key(node.name)
        count = int(e.mask(table_key, ctx)[: e.model.tables[table_key].rows].sum())
    else:
        count = len(e.table(node, ctx, env))
    return count or None


def _aggregate_x(fn: str, reducer: Callable[[list[Scalar]], Scalar]) -> Handler:
    def handler(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
        _args(fn, args, 2)
        values: list[Scalar] = [v for v in e.iterate(args[0], args[1], ctx, env) if v is not None]
        return reducer(values) if values else None

    return handler


def _sum_values(values: list[Scalar]) -> Scalar:
    total = sum(_number(v) for v in values)
    return total


_SCALAR_HANDLERS["SUMX"] = _aggregate_x("SUMX", _sum_values)
_SCALAR_HANDLERS["AVERAGEX"] = _aggregate_x(
    "AVERAGEX", lambda vs: sum(_number(v) for v in vs) / len(vs)
)
_SCALAR_HANDLERS["MINX"] = _aggregate_x("MINX", lambda vs: min(vs, key=_sort_key))
_SCALAR_HANDLERS["MAXX"] = _aggregate_x("MAXX", lambda vs: max(vs, key=_sort_key))
_SCALAR_HANDLERS["COUNTX"] = _aggregate_x("COUNTX", len)
_SCALAR_HANDLERS["COUNTAX"] = _aggregate_x("COUNTAX", len)
_SCALAR_HANDLERS["PRODUCTX"] = _aggregate_x(
    "PRODUCTX", lambda vs: math.prod(_number(v) for v in vs)
)


@_scalar("CONCATENATEX")
def _concatenatex(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("CONCATENATEX", args, 2, 20)
    table = e.table(args[0], ctx, env)
    delimiter = _text(e.scalar(args[2], ctx, env)) if len(args) > 2 else ""
    entries = []
    for row in table.iter_rows():
        inner = replace(env, rows=(*env.rows, row))
        keys = []
        for i in range(3, len(args), 2):
            ascending = len(args) > i + 1 and _order_ascending(e, args[i + 1], ctx, env)
            keys.append((e.scalar(args[i], ctx, inner), ascending))
        entries.append((keys, _text(e.scalar(args[1], ctx, inner))))
    for position in reversed(range(len(entries[0][0]) if entries else 0)):
        entries.sort(
            key=lambda item: _sort_key(item[0][position][0]),
            reverse=not entries[0][0][position][1],
        )
    return delimiter.join(text for _, text in entries)


def _order_ascending(e: Engine, node: Node, ctx: Ctx, env: Env) -> bool:
    if isinstance(node, Keyword):
        return node.word == "ASC"
    if isinstance(node, Name) and node.name == "":
        return False
    value = e.scalar(node, ctx, env)
    if isinstance(value, str):
        return value.upper() == "ASC"
    return bool(value)


@_scalar("RANKX")
def _rankx(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("RANKX", args, 2, 5)
    values = e.iterate(args[0], args[1], ctx, env)
    has_value = len(args) > 2 and not (isinstance(args[2], Name) and args[2].name == "")
    value = e.scalar(args[2] if has_value else args[1], ctx, env)
    ascending = len(args) > 3 and _order_ascending(e, args[3], ctx, env)
    dense = False
    if len(args) > 4:
        tie = args[4]
        dense = (isinstance(tie, Keyword) and tie.word == "DENSE") or (
            isinstance(tie, Name) and tie.name.upper() == "DENSE"
        )

    def number(v: Scalar) -> int | float | str:
        if v is None:
            return 0 if not isinstance(value, str) else ""
        return (
            v.casefold() if isinstance(v, str) else (_serial(v) if isinstance(v, datetime) else v)
        )

    target = number(value)
    sorted_key = ("__ranked__", id(values), dense, isinstance(value, str))
    ranked = e._rank_memo.get(sorted_key)
    if ranked is None or ranked[0] is not values:
        others = sorted(number(v) for v in values)
        ranked = (values, sorted(set(others)) if dense else others)
        e._rank_memo[sorted_key] = ranked
    ordered = ranked[1]
    if ascending:
        return 1 + bisect.bisect_left(ordered, target)
    return 1 + len(ordered) - bisect.bisect_right(ordered, target)


@_scalar("CALCULATE")
def _calculate(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    if not args:
        raise DaxEngineError("CALCULATE needs an expression.")
    new = e.calculate_context(args[1:], ctx, env)
    # The rows are filters now: a measure inside must not apply them again (Desktop:
    # ADDCOLUMNS(VALUES('Date'[Year]), "PY", CALCULATE([Sales], SAMEPERIODLASTYEAR(...)))
    # is the previous year, not the row's year intersected with it).
    inner = replace(env, rows=())
    expr = args[0]
    if e.is_table(expr, inner):
        return e.as_scalar(e.table(expr, new, inner))
    return e.scalar(expr, new, inner)


def _totals(fn: str) -> Handler:
    dates_fn = {"TOTALYTD": "DATESYTD", "TOTALMTD": "DATESMTD", "TOTALQTD": "DATESQTD"}[fn]

    def handler(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
        _args(fn, args, 2, 4)
        dates_args: tuple[Node, ...] = (args[1],)
        extra = list(args[2:])
        if fn == "TOTALYTD" and extra and isinstance(extra[-1], Str):
            dates_args = (args[1], extra.pop())
        filters = [Call(dates_fn, dates_args), *extra]
        return _calculate(e, [args[0], *filters], ctx, env)

    return handler


for _fn in ("TOTALYTD", "TOTALMTD", "TOTALQTD"):
    _SCALAR_HANDLERS[_fn] = _totals(_fn)


@_scalar("IF")
def _if(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("IF", args, 2, 3)
    if _truthy(e.scalar(args[0], ctx, env)):
        return e.scalar(args[1], ctx, env)
    return e.scalar(args[2], ctx, env) if len(args) == 3 else None


_SCALAR_HANDLERS["IF.EAGER"] = _if


@_scalar("SWITCH")
def _switch(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    if len(args) < 3:
        raise DaxEngineError("SWITCH needs an expression and at least one value/result pair.")
    subject = e.scalar(args[0], ctx, env)
    pairs = args[1:]
    for i in range(0, len(pairs) - 1, 2):
        if compare("=", subject, e.scalar(pairs[i], ctx, env)):
            return e.scalar(pairs[i + 1], ctx, env)
    return e.scalar(pairs[-1], ctx, env) if len(pairs) % 2 == 1 else None


@_scalar("AND")
def _and(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("AND", args, 2)
    return _truthy(e.scalar(args[0], ctx, env)) and _truthy(e.scalar(args[1], ctx, env))


@_scalar("OR")
def _or(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("OR", args, 2)
    return _truthy(e.scalar(args[0], ctx, env)) or _truthy(e.scalar(args[1], ctx, env))


@_scalar("NOT")
def _not(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("NOT", args, 1)
    return not _truthy(e.scalar(args[0], ctx, env))


@_scalar("TRUE")
def _true(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    return True


@_scalar("FALSE")
def _false(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    return False


@_scalar("BLANK")
def _blank(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    return None


@_scalar("ISBLANK")
def _isblank(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("ISBLANK", args, 1)
    return e.scalar(args[0], ctx, env) is None


@_scalar("ISNUMBER")
def _isnumber(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    value = e.scalar(args[0], ctx, env)
    return isinstance(value, int | float) and not isinstance(value, bool)


@_scalar("ISTEXT")
def _istext(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    return isinstance(e.scalar(args[0], ctx, env), str)


@_scalar("ISNONTEXT")
def _isnontext(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    return not isinstance(e.scalar(args[0], ctx, env), str)


@_scalar("ISLOGICAL")
def _islogical(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    return isinstance(e.scalar(args[0], ctx, env), bool)


def _failed(value: Scalar) -> bool:
    # 1/0 shows as Infinity, yet Desktop's IFERROR(1/0, -1) is -1: an error there.
    return isinstance(value, float) and not math.isfinite(value)


@_scalar("ISERROR")
def _iserror(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    try:
        return _failed(e.scalar(args[0], ctx, env))
    except DaxUnsupported:
        raise
    except DaxEngineError:
        return True


@_scalar("IFERROR")
def _iferror(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("IFERROR", args, 2)
    try:
        value = e.scalar(args[0], ctx, env)
    except DaxUnsupported:
        raise
    except DaxEngineError:
        return e.scalar(args[1], ctx, env)
    return e.scalar(args[1], ctx, env) if _failed(value) else value


@_scalar("COALESCE")
def _coalesce(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    for arg in args:
        value = e.scalar(arg, ctx, env)
        if value is not None:
            return value
    return None


@_scalar("DIVIDE")
def _divide(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("DIVIDE", args, 2, 3)
    numerator = e.scalar(args[0], ctx, env)
    denominator = e.scalar(args[1], ctx, env)
    if denominator is None or _number(denominator) == 0:
        return e.scalar(args[2], ctx, env) if len(args) == 3 else None
    if numerator is None:
        return None
    return _number(numerator) / _number(denominator)


def _math(name: str, fn: Callable[..., int | float], low: int = 1, high: int | None = None) -> None:
    def handler(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
        _args(name, args, low, high)
        values = [e.scalar(a, ctx, env) for a in args]
        if values[0] is None and name not in {"POWER"}:
            return None
        return fn(*[_number(v) for v in values])

    _SCALAR_HANDLERS[name] = handler


def _round_half_away(value: float, digits: int = 0) -> float:
    from decimal import ROUND_HALF_UP, Decimal

    quantum = Decimal(1).scaleb(-int(digits))
    result = float(Decimal(repr(value)).quantize(quantum, rounding=ROUND_HALF_UP))
    return int(result) if digits <= 0 else result


_math("ABS", abs)
_math("ROUND", lambda v, d=0: _round_half_away(v, int(d)), 1, 2)
_math("ROUNDUP", lambda v, d=0: math.copysign(math.ceil(abs(v) * 10**d) / 10**d, v), 1, 2)
_math("ROUNDDOWN", lambda v, d=0: math.copysign(math.floor(abs(v) * 10**d) / 10**d, v), 1, 2)
_math("INT", lambda v: math.floor(v))
_math("TRUNC", lambda v, d=0: math.trunc(v * 10**d) / 10**d, 1, 2)
_math("MOD", lambda a, b: a - b * math.floor(a / b), 2)
_math("SQRT", math.sqrt)
_math("POWER", lambda a, b: float(a) ** b, 2)
_math("EXP", math.exp)
_math("LN", math.log)
_math("LOG10", math.log10)
_math("LOG", lambda v, b=10: math.log(v, b), 1, 2)
_math("SIGN", lambda v: (v > 0) - (v < 0))
_math("CEILING", lambda v, s=1: math.ceil(v / s) * s, 1, 2)
_math("FLOOR", lambda v, s=1: math.floor(v / s) * s, 1, 2)
_math("MROUND", lambda v, s: _round_half_away(v / s) * s, 2)
_math("QUOTIENT", lambda a, b: math.trunc(a / b), 2)


@_scalar("FORMAT")
def _format(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("FORMAT", args, 2, 3)
    value = e.scalar(args[0], ctx, env)
    fmt = _text(e.scalar(args[1], ctx, env))
    return format_value(value, fmt)


@_scalar("REPT")
def _rept(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("REPT", args, 2)
    return _text(e.scalar(args[0], ctx, env)) * max(0, int(_number(e.scalar(args[1], ctx, env))))


@_scalar("CONCATENATE")
def _concatenate(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("CONCATENATE", args, 2)
    return _text(e.scalar(args[0], ctx, env)) + _text(e.scalar(args[1], ctx, env))


def _text_fn(name: str, fn: Callable[..., Scalar], low: int, high: int | None = None) -> None:
    def handler(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
        _args(name, args, low, high)
        values = [e.scalar(a, ctx, env) for a in args]
        return fn(*values)

    _SCALAR_HANDLERS[name] = handler


_text_fn("LEN", lambda s: len(_text(s)), 1)
_text_fn("UPPER", lambda s: _text(s).upper(), 1)
_text_fn("LOWER", lambda s: _text(s).lower(), 1)
_text_fn("TRIM", lambda s: " ".join(_text(s).split()), 1)
_text_fn("LEFT", lambda s, n=1: _text(s)[: int(_number(n))], 1, 2)
_text_fn("RIGHT", lambda s, n=1: _text(s)[-int(_number(n)) :] if int(_number(n)) else "", 1, 2)
_text_fn(
    "MID", lambda s, a, n: _text(s)[int(_number(a)) - 1 : int(_number(a)) - 1 + int(_number(n))], 3
)
_text_fn(
    "SUBSTITUTE",
    lambda s, old, new, *n: _text(s).replace(_text(old), _text(new)),
    3,
    4,
)
_text_fn("UNICHAR", lambda n: chr(int(_number(n))), 1)
_text_fn("EXACT", lambda a, b: _text(a) == _text(b), 2)
_text_fn("CONTAINSSTRING", lambda s, find: _text(find).casefold() in _text(s).casefold(), 2)
_text_fn("CONTAINSSTRINGEXACT", lambda s, find: _text(find) in _text(s), 2)


def _search(text: Scalar, find: Scalar, start: Scalar = 1, missing: Scalar = "__error__") -> Scalar:
    at = _text(text).casefold().find(_text(find).casefold(), int(_number(start or 1)) - 1)
    if at < 0:
        if missing == "__error__":
            raise DaxEngineError(
                "The search Text provided to function 'SEARCH' could not be found in the "
                "given text."
            )
        return missing
    return at + 1


_text_fn("SEARCH", lambda f, t, s=1, m="__error__": _search(t, f, s, m), 2, 4)
_text_fn(
    "FIND",
    lambda f, t, s=1, m="__error__": (
        (_text(t).find(_text(f), int(_number(s or 1)) - 1) + 1)
        if _text(f) in _text(t)[int(_number(s or 1)) - 1 :]
        else (
            m
            if m != "__error__"
            else _raise(
                "The search Text provided to function 'FIND' could not be found in the given text."
            )
        )
    ),
    2,
    4,
)


def _raise(message: str) -> NoReturn:
    raise DaxEngineError(message)


@_scalar("VALUE")
def _value(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("VALUE", args, 1)
    value = e.scalar(args[0], ctx, env)
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        try:
            return float(cleaned) if any(c in cleaned for c in ".eE") else int(cleaned)
        except ValueError:
            raise DaxEngineError(
                f"Cannot convert value '{value}' of type Text to type Number."
            ) from None
    return None if value is None else _number(value)


@_scalar("CONVERT")
def _convert(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("CONVERT", args, 2)
    value = e.scalar(args[0], ctx, env)
    target = (
        args[1].word
        if isinstance(args[1], Keyword)
        else (args[1].name.upper() if isinstance(args[1], Name) else "")
    )
    if value is None:
        return None
    if target == "STRING":
        return _text(value)
    if target in {"INTEGER", "INT64"}:
        return int(round(_number(value)))
    if target in {"DOUBLE", "DECIMAL", "CURRENCY"}:
        return float(_number(value))
    if target == "BOOLEAN":
        return _truthy(value)
    if target == "DATETIME":
        return value if isinstance(value, datetime) else _from_serial(_number(value))
    raise DaxUnsupported(f"CONVERT to {target or 'that type'} is not supported here.")


@_scalar("DATE")
def _date(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("DATE", args, 3)
    year, month, day = (int(_number(e.scalar(a, ctx, env))) for a in args)
    base = _add_months(datetime(year, 1, 1), month - 1)
    return base + timedelta(days=day - 1)


def _date_part(name: str, fn: Callable[[datetime], int]) -> None:
    def handler(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
        _args(name, args, 1)
        value = e.scalar(args[0], ctx, env)
        if value is None:
            return None
        if not isinstance(value, datetime):
            value = _from_serial(_number(value))
        return fn(value)

    _SCALAR_HANDLERS[name] = handler


_date_part("YEAR", lambda d: d.year)
_date_part("MONTH", lambda d: d.month)
_date_part("DAY", lambda d: d.day)
_date_part("QUARTER", lambda d: (d.month - 1) // 3 + 1)
_date_part("HOUR", lambda d: d.hour)
_date_part("MINUTE", lambda d: d.minute)


@_scalar("WEEKDAY")
def _weekday(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("WEEKDAY", args, 1, 2)
    value = e.scalar(args[0], ctx, env)
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise DaxEngineError("WEEKDAY needs a date.")
    kind = int(_number(e.scalar(args[1], ctx, env))) if len(args) > 1 else 1
    monday = value.weekday()
    if kind == 2:
        return monday + 1
    if kind == 3:
        return monday
    return (monday + 1) % 7 + 1


@_scalar("EOMONTH")
def _eomonth(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("EOMONTH", args, 2)
    value = e.scalar(args[0], ctx, env)
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise DaxEngineError("EOMONTH needs a date.")
    return _month_end(_add_months(_day(value), int(_number(e.scalar(args[1], ctx, env)))))


@_scalar("EDATE")
def _edate(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("EDATE", args, 2)
    value = e.scalar(args[0], ctx, env)
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise DaxEngineError("EDATE needs a date.")
    return _add_months(_day(value), int(_number(e.scalar(args[1], ctx, env))))


@_scalar("DATEDIFF")
def _datediff(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("DATEDIFF", args, 3)
    a, b = e.scalar(args[0], ctx, env), e.scalar(args[1], ctx, env)
    if a is None or b is None:
        return None
    if not isinstance(a, datetime) or not isinstance(b, datetime):
        raise DaxEngineError("DATEDIFF needs two dates.")
    unit = (
        args[2].word if isinstance(args[2], Keyword) else _text(e.scalar(args[2], ctx, env)).upper()
    )
    if unit == "DAY":
        return (_day(b) - _day(a)).days
    if unit in _INTERVAL_MONTHS:
        months = (b.year - a.year) * 12 + b.month - a.month
        return months // _INTERVAL_MONTHS[unit]
    if unit == "WEEK":
        return (_day(b) - _day(a)).days // 7
    raise DaxUnsupported(f"DATEDIFF by {unit} is not supported here.")


@_scalar("TODAY")
def _today(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    return _day(e.now)


@_scalar("NOW")
def _now(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    return e.now


@_scalar("SELECTEDVALUE")
def _selectedvalue(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("SELECTEDVALUE", args, 1, 2)
    table_key, column_key = e._column_ref(args[0])
    values = e.visible_values(table_key, column_key, ctx)
    if len(values) == 1:
        return values[0]
    return e.scalar(args[1], ctx, env) if len(args) == 2 else None


@_scalar("HASONEVALUE")
def _hasonevalue(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("HASONEVALUE", args, 1)
    table_key, column_key = e._column_ref(args[0])
    return len(e.visible_values(table_key, column_key, ctx)) == 1


@_scalar("HASONEFILTER")
def _hasonefilter(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    table_key, column_key = e._column_ref(args[0])
    direct = [f for f in ctx if f.table == table_key and column_key in f.columns]
    return bool(direct) and len(e.visible_values(table_key, column_key, ctx)) == 1


@_scalar("ISFILTERED")
def _isfiltered(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("ISFILTERED", args, 1)
    node = args[0]
    if isinstance(node, ColRef) and node.table:
        table_key, column_key = e._column_ref(node)
        return any(f.table == table_key and column_key in f.columns for f in ctx)
    table_key = e._table_name(node)
    return any(f.table == table_key for f in ctx)


@_scalar("ISCROSSFILTERED")
def _iscrossfiltered(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("ISCROSSFILTERED", args, 1)
    node = args[0]
    table_key = _key(node.table) if isinstance(node, ColRef) and node.table else e._table_name(node)
    closure = e.model.closure(table_key)
    return any(f.table in closure for f in ctx)


@_scalar("ISINSCOPE")
def _isinscope(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("ISINSCOPE", args, 1)
    return e._column_ref(args[0]) in env.scope


@_scalar("RELATED")
def _related(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    _args("RELATED", args, 1)
    target, column = e._column_ref(args[0])
    for row in reversed(env.rows):
        if row.base is None or row.index is None:
            # A row of a derived table: the related column may already be in it.
            for col, value in zip(row.columns, row.values, strict=False):
                if col.lineage == (target, column):
                    return value
            continue
        path = _related_path(e.model, row.base, target)
        if path is None:
            continue
        index: int | None = row.index
        current = row.base
        for rel in path:
            table = e.model.tables[current]
            if index is None or index >= table.rows:
                index = None
                break
            key = table.columns[rel.many_column].value(index)
            one = e.model.tables[rel.one]
            matches = np.flatnonzero(_isin(one.columns[rel.one_column], frozenset([key])))
            index = int(matches[0]) if len(matches) else None
            current = rel.one
            if index is None:
                break
        return e.model.tables[target].columns[column].value(index) if index is not None else None
    ref = args[0]
    assert isinstance(ref, ColRef)  # _column_ref above checked the reference.
    raise DaxEngineError(
        f"The column '{ref.column}' either doesn't exist or doesn't have a relationship "
        "to any table available in the current context."
    )


@_scalar("LOOKUPVALUE")
def _lookupvalue(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    if len(args) < 3:
        raise DaxEngineError("LOOKUPVALUE needs a result column and search column/value pairs.")
    result_table, result_column = e._column_ref(args[0])
    pairs = list(args[1:])
    alternate = pairs.pop() if len(pairs) % 2 == 1 else None
    table = e.model.tables[result_table]
    mask = np.ones(table.rows, dtype=bool)
    for i in range(0, len(pairs), 2):
        search_table, search_column = e._column_ref(pairs[i])
        if search_table != result_table:
            raise DaxUnsupported("LOOKUPVALUE across tables is not supported here.")
        value = e.scalar(pairs[i + 1], ctx, env)
        mask &= _isin(table.columns[search_column], frozenset([value]))
    values = {v for v in _values_array(table.columns[result_column], mask) if v is not None}
    if len(values) == 1:
        return values.pop()
    if not values:
        return e.scalar(alternate, ctx, env) if alternate is not None else None
    if alternate is not None:
        return e.scalar(alternate, ctx, env)
    raise DaxEngineError(
        "A table of multiple values was supplied where a single value was expected."
    )


@_scalar("CONTAINSROW")
def _containsrow(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    table = e.table(args[0], ctx, env)
    wanted = tuple(e.scalar(a, ctx, env) for a in args[1:])
    return any(
        all(compare("==", a, b) for a, b in zip(row, wanted, strict=True)) for row in table.rows
    )


@_scalar("USERELATIONSHIP", "CROSSFILTER")
def _relationship_modifier(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    raise DaxEngineError("USERELATIONSHIP and CROSSFILTER can only be used inside CALCULATE.")


@_scalar("FIRSTNONBLANK", "LASTNONBLANK")
def _nonblank(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> Scalar:
    raise DaxUnsupported("FIRSTNONBLANK and LASTNONBLANK are not supported here yet.")


# ---------------------------------------------------------------- table functions


def _all_rows(blank_row: bool) -> TableHandler:
    def handler(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
        if not args:
            raise DaxEngineError("ALL() without arguments can only be used as a CALCULATE filter.")
        first = args[0]
        if isinstance(first, Name):
            table_key = e._table_name(first)
            table = e.model.tables[table_key]
            mask = np.ones(table.rows + 1, dtype=bool)
            mask[table.rows] = table.blank_row and blank_row
            return e._base_rows(table_key, mask)
        refs = [e._column_ref(a) for a in args]
        return e.column_table(refs, None, blank_row=blank_row)

    return handler


_TABLE_HANDLERS["ALL"] = _all_rows(True)
_TABLE_HANDLERS["REMOVEFILTERS"] = _all_rows(True)
_TABLE_HANDLERS["ALLNOBLANKROW"] = _all_rows(False)


@_table("ALLSELECTED")
def _allselected_table(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    shadow = env.shadow if env.shadow is not None else ctx
    if not args:
        raise DaxUnsupported("ALLSELECTED() without arguments is only supported inside CALCULATE.")
    first = args[0]
    if isinstance(first, Name):
        table_key = e._table_name(first)
        return e._base_rows(table_key, e.mask(table_key, shadow))
    refs = [e._column_ref(a) for a in args]
    return e.column_table(refs, shadow)


@_table("ALLEXCEPT")
def _allexcept_table(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    table_key = e._table_name(args[0])
    kept = {e._column_ref(c)[1] for c in args[1:]}
    reduced = e.remove(ctx, table_key)
    reduced = frozenset(
        list(reduced) + [f for f in ctx if f.table == table_key and set(f.columns) <= kept]
    )
    return e._base_rows(table_key, e.mask(table_key, reduced))


def _distinct_rows(fn: str, blank_row: bool) -> TableHandler:
    # VALUES includes the relationship's blank row, DISTINCT does not (Desktop: 294, 293).
    def handler(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
        _args(fn, args, 1)
        node = args[0]
        if isinstance(node, Name):
            if _key(node.name) in env.variables:
                table = e.table(node, ctx, env)
                unique = list(dict.fromkeys(table.rows))
                return TableValue(table.columns, unique)
            table_key = e._table_name(node)
            return e._base_rows(table_key, e.mask(table_key, ctx), blank_row=blank_row)
        if isinstance(node, ColRef) and node.table and e._is_column(node):
            return e.column_table([e._column_ref(node)], ctx, blank_row=blank_row)
        table = e.table(node, ctx, env)
        return TableValue(table.columns, list(dict.fromkeys(table.rows)))

    return handler


_TABLE_HANDLERS["VALUES"] = _distinct_rows("VALUES", True)
_TABLE_HANDLERS["DISTINCT"] = _distinct_rows("DISTINCT", False)


@_table("FILTER")
def _filter(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    _args("FILTER", args, 2)
    table = e.table(args[0], ctx, env)
    vector = e.vector(args[1], table, ctx, env)
    if vector is not None:
        assert table.indices is not None  # vectorization needs indexed model rows.
        keep = np.array([bool(v) for v in vector], dtype=bool)
        return TableValue(
            table.columns,
            base=table.base,
            indices=table.indices[keep],
            model=e.model,
        )
    kept_rows, kept_indices = [], []
    rows = table.rows
    for position, value in enumerate(e.per_row(table, args[1], ctx, env)):
        if _truthy(value):
            kept_rows.append(rows[position])
            kept_indices.append(position)
    if table.indices is not None:
        return TableValue(
            table.columns,
            kept_rows,
            base=table.base,
            indices=table.indices[np.array(kept_indices, dtype=int)],
            model=e.model,
        )
    return TableValue(table.columns, kept_rows)


@_table("CALCULATETABLE")
def _calculatetable(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    if not args:
        raise DaxEngineError("CALCULATETABLE needs a table expression.")
    new = e.calculate_context(args[1:], ctx, env)
    return e.table(args[0], new, replace(env, rows=()))


@_table("KEEPFILTERS")
def _keepfilters_table(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    _args("KEEPFILTERS", args, 1)
    return e.table(args[0], ctx, env)


@_table("RELATEDTABLE")
def _relatedtable(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    _args("RELATEDTABLE", args, 1)
    new = e.transition(ctx, env.rows) if env.rows else ctx
    return e.table(args[0], new, replace(env, rows=()))


def _pairs(args: Sequence[Node], start: int, fn: str) -> list[tuple[str, Node]]:
    pairs = []
    rest = args[start:]
    if len(rest) % 2:
        raise DaxEngineError(f"{fn} expects name and expression pairs.")
    for i in range(0, len(rest), 2):
        name_node = rest[i]
        if not isinstance(name_node, Str):
            raise DaxEngineError(f'{fn} expects a column name in quotes, such as "Total".')
        pairs.append((name_node.value, rest[i + 1]))
    return pairs


@_table("ADDCOLUMNS")
def _addcolumns(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    if len(args) < 3:
        raise DaxEngineError("ADDCOLUMNS needs a table and at least one name/expression pair.")
    table = e.table(args[0], ctx, env)
    pairs = _pairs(args, 1, "ADDCOLUMNS")
    rows = []
    for row in table.iter_rows():
        e._tick()
        inner = replace(env, rows=(*env.rows, row))
        rows.append(row.values + tuple(e.scalar(expr, ctx, inner) for _, expr in pairs))
    columns = table.columns + [Col(name) for name, _ in pairs]
    return TableValue(columns, rows, base=None)


@_table("SELECTCOLUMNS")
def _selectcolumns(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    if len(args) < 2:
        raise DaxEngineError("SELECTCOLUMNS needs a table and at least one column.")
    table = e.table(args[0], ctx, env)
    items: list[tuple[str, Node]] = []
    explicit: dict[str, bool] = {}
    rest = list(args[1:])
    while rest:
        if isinstance(rest[0], Str):
            if len(rest) < 2:
                raise DaxEngineError("SELECTCOLUMNS expects name and expression pairs.")
            items.append((rest[0].value, rest[1]))
            explicit[rest[0].value] = True
            rest = rest[2:]
        elif isinstance(rest[0], ColRef):
            items.append((rest[0].column, rest[0]))
            explicit[rest[0].column] = False
            rest = rest[1:]
        else:
            raise DaxEngineError("SELECTCOLUMNS expects name and expression pairs.")
    columns = []
    for name, expr in items:
        lineage = None
        if isinstance(expr, ColRef):
            key = (_key(expr.table), _key(expr.column)) if expr.table else None
            match = next(
                (
                    c
                    for c in table.columns
                    if (key is not None and c.lineage == key)
                    or (key is None and _key(c.name) == _key(expr.column))
                ),
                None,
            )
            lineage = match.lineage if match is not None else None
        columns.append(Col(name, lineage, renamed=lineage is not None and explicit[name]))
    rows = []
    for row in table.iter_rows():
        e._tick()
        inner = replace(env, rows=(*env.rows, row))
        rows.append(tuple(e.scalar(expr, ctx, inner) for _, expr in items))
    return TableValue(columns, rows)


@_table("ROW")
def _row(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    pairs = _pairs(args, 0, "ROW")
    inner = replace(env, shadow=ctx if env.shadow is None else env.shadow)
    return TableValue(
        [Col(name) for name, _ in pairs], [tuple(e.scalar(expr, ctx, inner) for _, expr in pairs)]
    )


@_table("SUMMARIZECOLUMNS")
def _summarizecolumns(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    groups: list[tuple[str, str]] = []
    filter_nodes: list[Node] = []
    index = 0
    while index < len(args) and not isinstance(args[index], Str):
        node = args[index]
        if isinstance(node, ColRef) and e._is_column(node):
            groups.append(e._column_ref(node))
        elif isinstance(node, Call) and node.name in {
            "ROLLUPADDISSUBTOTAL",
            "ROLLUPGROUP",
            "ROLLUP",
            "NONVISUAL",
            "IGNORE",
        }:
            raise DaxUnsupported(f"{node.name} inside SUMMARIZECOLUMNS is not supported here.")
        else:
            filter_nodes.append(node)
        index += 1
    pairs = _pairs(args, index, "SUMMARIZECOLUMNS")
    base = e.calculate_context(filter_nodes, ctx, replace(env, rows=())) if filter_nodes else ctx
    shadow = base
    by_table: dict[str, list[str]] = {}
    for table_key, column_key in groups:
        by_table.setdefault(table_key, []).append(column_key)
    combos: list[list[tuple[str, tuple[str, ...], RowValues]]] = [[]]
    for table_key, cols in by_table.items():
        part = e.column_table([(table_key, c) for c in cols], base)
        combos = [
            combo + [(table_key, tuple(cols), values)] for combo in combos for values in part.rows
        ]
        if len(combos) > 1_000_000:
            raise DaxEngineLimit(
                "SUMMARIZECOLUMNS would produce too many rows for the local engine."
            )
    columns = [
        Col(e.model.tables[t].columns[c].name, (t, c)) for t, cols in by_table.items() for c in cols
    ]
    columns += [Col(name) for name, _ in pairs]
    scope = frozenset(groups)
    rows = []
    for combo in combos:
        e._tick()
        filters = [
            Filter(t, cols, frozenset([values[0]]) if len(cols) == 1 else frozenset([values]))
            for t, cols, values in combo
        ]
        group_ctx = e.apply(base, filters)
        inner = Env(variables=env.variables, scope=scope, shadow=shadow)
        values = tuple(e.scalar(expr, group_ctx, inner) for _, expr in pairs)
        if pairs and all(v is None for v in values):
            continue
        key_values = tuple(v for _, _, vs in combo for v in vs)
        rows.append(key_values + values)
    return TableValue(columns, rows)


@_table("SUMMARIZE")
def _summarize(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    if len(args) < 2:
        raise DaxEngineError("SUMMARIZE needs a table and at least one column.")
    table = e.table(args[0], ctx, env)
    groups: list[tuple[str, str]] = []
    index = 1
    while index < len(args) and not isinstance(args[index], Str):
        node = args[index]
        if not (isinstance(node, ColRef) and e._is_column(node)):
            raise DaxEngineError(
                f"Function SUMMARIZE expects a column name as argument number {index + 1}."
            )
        groups.append(e._column_ref(node))
        index += 1
    pairs = _pairs(args, index, "SUMMARIZE")
    if table.base is None:
        raise DaxUnsupported(
            "SUMMARIZE over a computed table is not supported here; use a model table."
        )
    base = table.base
    indices = table.indices if table.indices is not None else np.array([], int)
    group_columns = []
    for table_key, column_key in groups:
        if table_key == base:
            column = e.model.tables[base].columns[column_key]
            group_columns.append(
                np.append(pd.Series(column.values, dtype=object).to_numpy(), [None])[indices]
            )
            continue
        path = _related_path(e.model, base, table_key)
        if path is None:
            raise DaxEngineError(
                f"The column '{e.model.tables[table_key].columns[column_key].name}' specified in "
                "the 'SUMMARIZE' function was not found in the input table."
            )
        rows = indices.copy()
        current = base
        for rel in path:
            many = e.model.tables[current].columns[rel.many_column]
            one_table = e.model.tables[rel.one]
            one_column = one_table.columns[rel.one_column]
            one_keys = one_column.folded() if one_column.kind == "string" else one_column.values
            lookup = pd.Series(np.arange(one_table.rows), index=one_keys)
            lookup = lookup[~lookup.index.duplicated()]
            many_values = many.folded() if many.kind == "string" else many.values
            valid = rows < len(many.values)
            mapped_rows = np.full(len(rows), one_table.rows)
            mapped_rows[valid] = (
                pd.Series(many_values[rows[valid]])
                .map(lookup)
                .fillna(one_table.rows)
                .to_numpy(dtype=int)
            )
            rows = mapped_rows
            current = rel.one
        column = e.model.tables[table_key].columns[column_key]
        group_columns.append(
            np.append(pd.Series(column.values, dtype=object).to_numpy(), [None])[rows]
        )
    frame = pd.DataFrame({i: col for i, col in enumerate(group_columns)}).drop_duplicates()
    columns = [Col(e.model.tables[t].columns[c].name, (t, c)) for t, c in groups]
    columns += [Col(name) for name, _ in pairs]
    rows_out = []
    for record in frame.itertuples(index=False, name=None):
        e._tick()
        values = tuple(
            _python(v, e.model.tables[t].columns[c].kind)
            for v, (t, c) in zip(record, groups, strict=True)
        )
        if pairs:
            filters = [
                Filter(t, (c,), frozenset([v])) for (t, c), v in zip(groups, values, strict=True)
            ]
            group_ctx = e.apply(ctx, filters)
            extra = tuple(e.scalar(expr, group_ctx, replace(env, rows=())) for _, expr in pairs)
        else:
            extra = ()
        rows_out.append(values + extra)
    return TableValue(columns, rows_out)


@_table("TOPN")
def _topn(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    if len(args) < 2:
        raise DaxEngineError("TOPN needs a count and a table.")
    count = e.scalar(args[0], ctx, env)
    table = e.table(args[1], ctx, env)
    if count is None or _number(count) <= 0:
        return TableValue(table.columns, [])
    n = int(_number(count))
    orders: list[tuple[Node, bool]] = []
    rest = list(args[2:])
    while rest:
        expr = rest.pop(0)
        ascending = False
        if rest and (isinstance(rest[0], Keyword) or _is_order_literal(rest[0])):
            ascending = _order_ascending(e, rest.pop(0), ctx, env)
        orders.append((expr, ascending))
    rows = list(table.iter_rows())
    if not orders or n >= len(rows):
        if n >= len(rows):
            return TableValue(table.columns, [r.values for r in rows])
        return TableValue(table.columns, [r.values for r in rows[:n]])
    columns = [e.per_row(table, expr, ctx, env) for expr, _ in orders]
    keyed = [([column[i] for column in columns], row) for i, row in enumerate(rows)]
    for position in reversed(range(len(orders))):
        ascending = orders[position][1]
        keyed.sort(key=lambda item: _sort_key(item[0][position]), reverse=not ascending)
    cutoff = [_sort_key(v) for v in keyed[n - 1][0]]
    chosen = [row for keys, row in keyed[:n]]
    for keys, row in keyed[n:]:
        if [_sort_key(v) for v in keys] == cutoff:
            chosen.append(row)
        else:
            break
    return TableValue(table.columns, [r.values for r in chosen])


def _is_order_literal(node: Node) -> bool:
    return (isinstance(node, Num) and node.value in {0, 1}) or (
        isinstance(node, Call) and node.name in {"TRUE", "FALSE"} and not node.args
    )


@_table("UNION")
def _union(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    if len(args) < 2:
        raise DaxEngineError("UNION needs at least two tables.")
    tables = [e.table(a, ctx, env) for a in args]
    width = len(tables[0].columns)
    if any(len(t.columns) != width for t in tables):
        raise DaxEngineError("Each table argument of 'UNION' must have the same number of columns.")
    columns = [
        Col(
            tables[0].columns[i].name,
            tables[0].columns[i].lineage
            if all(t.columns[i].lineage == tables[0].columns[i].lineage for t in tables)
            else None,
        )
        for i in range(width)
    ]
    rows = [row for t in tables for row in t.rows]
    return TableValue(columns, rows)


@_table("CROSSJOIN")
def _crossjoin(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    tables = [e.table(a, ctx, env) for a in args]
    size = math.prod(len(t) for t in tables)
    if size > 1_000_000:
        raise DaxEngineLimit("CROSSJOIN would produce too many rows for the local engine.")
    rows: list[RowValues] = [()]
    for t in tables:
        rows = [r + s for r in rows for s in t.rows]
    return TableValue([c for t in tables for c in t.columns], rows)


def _set_operation(fn: str, keep_matches: bool) -> TableHandler:
    def handler(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
        _args(fn, args, 2)
        left, right = e.table(args[0], ctx, env), e.table(args[1], ctx, env)
        if len(left.columns) != len(right.columns):
            raise DaxEngineError(
                f"Each table argument of '{fn}' must have the same number of columns."
            )
        other = {tuple(_fold(v) for v in r) for r in right.rows}
        rows = [r for r in left.rows if (tuple(_fold(v) for v in r) in other) == keep_matches]
        return TableValue(left.columns, list(dict.fromkeys(rows)))

    return handler


_TABLE_HANDLERS["EXCEPT"] = _set_operation("EXCEPT", False)
_TABLE_HANDLERS["INTERSECT"] = _set_operation("INTERSECT", True)


@_table("GENERATESERIES")
def _generateseries(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    _args("GENERATESERIES", args, 2, 3)
    start, end = (_number(e.scalar(a, ctx, env)) for a in args[:2])
    step = _number(e.scalar(args[2], ctx, env)) if len(args) == 3 else 1
    if step <= 0:
        raise DaxEngineError("The increment value of GENERATESERIES must be positive.")
    count = int(math.floor((end - start) / step + 1e-9)) + 1
    if count > 1_000_000:
        raise DaxEngineLimit("GENERATESERIES would produce too many rows.")
    values = [start + i * step for i in range(max(count, 0))]
    return TableValue([Col("Value")], [(v,) for v in values])


@_table("TREATAS")
def _treatas(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    if len(args) < 2:
        raise DaxEngineError("TREATAS needs a table and target columns.")
    table = e.table(args[0], ctx, env)
    targets = [e._column_ref(a) for a in args[1:]]
    if len(targets) != len(table.columns):
        raise DaxEngineError(
            "The number of columns in the table expression does not match the number of target "
            "columns in TREATAS."
        )
    columns = [Col(e.model.tables[t].columns[c].name, (t, c)) for t, c in targets]
    return TableValue(columns, table.rows)


@_table("CALENDAR")
def _calendar(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    _args("CALENDAR", args, 2)
    start, end = (e.scalar(a, ctx, env) for a in args)
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        raise DaxEngineError("CALENDAR needs two dates.")
    days = (_day(end) - _day(start)).days
    if days > 200_000:
        raise DaxEngineLimit("CALENDAR would produce too many rows.")
    return TableValue([Col("Date")], [(_day(start) + timedelta(days=i),) for i in range(days + 1)])


# time intelligence -------------------------------------------------------------


def _to_date(fn: str, months: int | None) -> TableHandler:
    def handler(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
        _args(fn, args, 1, 2)
        table, column, visible = e.dates(args[0], ctx, env)
        if not visible:
            return e.date_table(table, column, [])
        last = visible[-1]
        if months is None:  # year to date, optional year end "6/30"
            year_end = (12, 31)
            if len(args) == 2:
                text = _text(e.scalar(args[1], ctx, env))
                parts = [int(p) for p in text.replace("-", "/").split("/") if p.strip().isdigit()]
                if len(parts) >= 2:
                    year_end = (parts[0], parts[1])
            start_year = last.year if (last.month, last.day) > year_end else last.year - 1
            start = datetime(
                start_year,
                year_end[0],
                min(year_end[1], calendar.monthrange(start_year, year_end[0])[1]),
            ) + timedelta(days=1)
        else:
            start = _period_start(last, months)
        memo_key = ("__todate__", table, column, start, last)
        cached = e._table_memo.get(memo_key)
        if cached is None:
            dates = e.all_dates(table, column)
            low, high = bisect.bisect_left(dates, start), bisect.bisect_right(dates, last)
            cached = e.date_table(table, column, dates[low:high])
            e._table_memo[memo_key] = cached
        return cached

    return handler


_TABLE_HANDLERS["DATESMTD"] = _to_date("DATESMTD", 1)
_TABLE_HANDLERS["DATESQTD"] = _to_date("DATESQTD", 3)
_TABLE_HANDLERS["DATESYTD"] = _to_date("DATESYTD", None)


def _interval(e: Engine, node: Node, ctx: Ctx, env: Env) -> str:
    if isinstance(node, Keyword):
        return node.word
    if isinstance(node, Name):
        return node.name.upper()
    return _text(e.scalar(node, ctx, env)).upper()


def _shift(visible: list[datetime], n: int, interval: str) -> set[datetime]:
    return set(_shift_cached(tuple(visible), n, interval))


@lru_cache(maxsize=4096)
def _shift_cached(visible: tuple[datetime, ...], n: int, interval: str) -> frozenset[datetime]:
    return frozenset(_shift_dates(visible, n, interval))


def _shift_dates(visible: Sequence[datetime], n: int, interval: str) -> set[datetime]:
    if interval == "DAY":
        return {d + timedelta(days=n) for d in visible}
    months = n * _INTERVAL_MONTHS.get(interval, 0)
    if not months and interval not in _INTERVAL_MONTHS:
        raise DaxUnsupported(f"Shifting by {interval} is not supported here.")
    # As Power BI does: a whole month maps to the whole target month (Feb 1-28 a month on
    # is Mar 1-31); other dates move day by day, clamped to the target month's length
    # (28 Feb 2009 a year back is 28 Feb 2008 only, not the 29th too).
    by_month: dict[tuple[int, int], set[int]] = {}
    for d in visible:
        by_month.setdefault((d.year, d.month), set()).add(d.day)
    out: set[datetime] = set()
    for (year, month), days in by_month.items():
        if len(days) == calendar.monthrange(year, month)[1]:
            start = _add_months(datetime(year, month, 1), months)
            end = _month_end(start)
            out.update(start + timedelta(days=i) for i in range((end - start).days + 1))
        else:
            out.update(_add_months(datetime(year, month, day), months) for day in days)
    return out


def _shift_edge(value: datetime, months: int) -> datetime:
    """A period edge moved by months; a month's last day stays its last day."""
    moved = _add_months(value, months)
    return _month_end(moved) if _is_month_end(value) else moved


@_table("DATEADD")
def _dateadd(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    _args("DATEADD", args, 3)
    table, column, visible = e.dates(args[0], ctx, env)
    n = int(_number(e.scalar(args[1], ctx, env)))
    interval = _interval(e, args[2], ctx, env)
    shifted = _shift_cached(tuple(visible), n, interval)
    memo_key = ("__shifted__", table, column, shifted)
    cached = e._table_memo.get(memo_key)
    if cached is None:
        cached = e.date_table(table, column, shifted)
        e._table_memo[memo_key] = cached
    return cached


@_table("SAMEPERIODLASTYEAR")
def _sameperiodlastyear(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    _args("SAMEPERIODLASTYEAR", args, 1)
    table, column, visible = e.dates(args[0], ctx, env)
    return e.date_table(table, column, _shift(visible, -1, "YEAR"))


@_table("PARALLELPERIOD")
def _parallelperiod(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    _args("PARALLELPERIOD", args, 3)
    table, column, visible = e.dates(args[0], ctx, env)
    if not visible:
        return e.date_table(table, column, [])
    n = int(_number(e.scalar(args[1], ctx, env)))
    interval = _interval(e, args[2], ctx, env)
    months = _INTERVAL_MONTHS.get(interval)
    if months is None:
        raise DaxUnsupported(f"PARALLELPERIOD by {interval} is not supported here.")
    start = _period_start(_add_months(visible[0], n * months), months)
    end = _period_end(_add_months(visible[-1], n * months), months)
    return e.date_table(table, column, [d for d in e.all_dates(table, column) if start <= d <= end])


@_table("DATESINPERIOD")
def _datesinperiod(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    _args("DATESINPERIOD", args, 4)
    if isinstance(args[0], ColRef):
        table, column = e._column_ref(args[0], "date column")
    else:
        table, column, _ = e.dates(args[0], ctx, env)
    start = e.scalar(args[1], ctx, env)
    dates = e.all_dates(table, column)
    if not dates:
        return e.date_table(table, column, [])
    # As Power BI does: a blank start, or one before the calendar, starts at its first
    # date (a moving annual total before the data begins still sums from 1 Jan 2007);
    # one after the calendar returns no dates.
    if start is not None and not isinstance(start, datetime):
        raise DaxEngineError("DATESINPERIOD needs a date for its start.")
    start = dates[0] if start is None else max(_day(start), dates[0])
    if start > dates[-1]:
        return e.date_table(table, column, [])
    n = int(_number(e.scalar(args[2], ctx, env)))
    interval = _interval(e, args[3], ctx, env)
    if interval == "DAY":
        other = start + timedelta(days=n)
    else:
        # 28 Feb 2009 back a year is 29 Feb 2008, so the year starts on 1 Mar 2008.
        other = _shift_edge(start, n * _INTERVAL_MONTHS[interval])
    if n < 0:
        low, high = other + timedelta(days=1), start
    else:
        low, high = start, other - timedelta(days=1)
    return e.date_table(table, column, [d for d in e.all_dates(table, column) if low <= d <= high])


@_table("DATESBETWEEN")
def _datesbetween(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
    _args("DATESBETWEEN", args, 3)
    table, column = e._column_ref(args[0], "date column")
    low, high = e.scalar(args[1], ctx, env), e.scalar(args[2], ctx, env)
    dates = e.all_dates(table, column)
    low_day = _day(low) if isinstance(low, datetime) else (dates[0] if dates else None)
    high_day = _day(high) if isinstance(high, datetime) else (dates[-1] if dates else None)
    return e.date_table(
        table,
        column,
        [
            d
            for d in dates
            if low_day is not None and high_day is not None and low_day <= d <= high_day
        ],
    )


def _neighbour(fn: str, direction: int, months: int | None) -> TableHandler:
    def handler(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
        _args(fn, args, 1, 2)
        table, column, visible = e.dates(args[0], ctx, env)
        if not visible:
            return e.date_table(table, column, [])
        anchor = visible[0] if direction < 0 else visible[-1]
        if months is None:
            day = anchor + timedelta(days=direction)
            return e.date_table(table, column, [day])
        target = _add_months(_period_start(anchor, months), direction * months)
        start, end = _period_start(target, months), _period_end(target, months)
        return e.date_table(
            table, column, [d for d in e.all_dates(table, column) if start <= d <= end]
        )

    return handler


for _name, _dir, _months in (
    ("PREVIOUSDAY", -1, None),
    ("PREVIOUSMONTH", -1, 1),
    ("PREVIOUSQUARTER", -1, 3),
    ("PREVIOUSYEAR", -1, 12),
    ("NEXTDAY", 1, None),
    ("NEXTMONTH", 1, 1),
    ("NEXTQUARTER", 1, 3),
    ("NEXTYEAR", 1, 12),
):
    _TABLE_HANDLERS[_name] = _neighbour(_name, _dir, _months)


def _edge(fn: str, first: bool, months: int | None) -> TableHandler:
    def handler(e: Engine, args: Sequence[Node], ctx: Ctx, env: Env) -> TableValue:
        _args(fn, args, 1, 2)
        table, column, visible = e.dates(args[0], ctx, env)
        if not visible:
            return e.date_table(table, column, [])
        if months is None:
            return e.date_table(table, column, [visible[0] if first else visible[-1]])
        anchor = visible[0] if first else visible[-1]
        start, end = _period_start(anchor, months), _period_end(anchor, months)
        in_period = [d for d in e.all_dates(table, column) if start <= d <= end]
        if not in_period:
            return e.date_table(table, column, [])
        return e.date_table(table, column, [in_period[0] if first else in_period[-1]])

    return handler


for _name, _first, _months in (
    ("FIRSTDATE", True, None),
    ("LASTDATE", False, None),
    ("STARTOFMONTH", True, 1),
    ("STARTOFQUARTER", True, 3),
    ("STARTOFYEAR", True, 12),
    ("ENDOFMONTH", False, 1),
    ("ENDOFQUARTER", False, 3),
    ("ENDOFYEAR", False, 12),
):
    _TABLE_HANDLERS[_name] = _edge(_name, _first, _months)


# ------------------------------------------------------------------- results


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Scalar]]
    elapsed_ms: int


def column_label(model: Model, col: Col) -> str:
    """Power BI's result column name: Table[Column] for model columns, [Name] otherwise."""
    if col.lineage and not col.renamed:
        table = model.tables[col.lineage[0]]
        return f"{table.name}[{table.columns[col.lineage[1]].name}]"
    return f"[{col.name}]"


def run_query(model: Model, text: str, *, time_budget: float = 30.0) -> QueryResult:
    started = time.monotonic()
    engine = Engine(model, time_budget=time_budget)
    table = engine.run_query(text)
    return QueryResult(
        [column_label(model, c) for c in table.columns],
        [list(r) for r in table.rows],
        int((time.monotonic() - started) * 1000),
    )


def evaluate_measure(
    model: Model, expression: str, filters: Iterable[Filter] = (), *, time_budget: float = 30.0
) -> Scalar:
    engine = Engine(model, time_budget=time_budget)
    ctx = engine.apply(EMPTY, list(filters))
    return engine.evaluate_scalar(expression, ctx)
