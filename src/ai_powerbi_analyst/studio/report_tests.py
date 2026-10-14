"""Report tests: numbers a Power BI developer pins, checked again after every model change.

A developer knows a refactor did not change the answers because they test known numbers
("Total Sales for 2024 is 1.23M"). A test is one number of the report (a measure, or a DAX
expression the user typed) under a fixed filter context, with what it should be. Tests are
pinned from a card or from a data point of a chart or table (``pin_from_visual``: the measure
and every filter in effect, as the canvas computes the visual), or typed by the user, and kept
per report in ``reports/<report id>/tests.json`` under Studio's state root (``TestStore``).

``run_tests`` evaluates them with an evaluator: Studio's DAX engine over the imported data
(``LocalEvaluator``) or any DAX-query callable, such as Power BI Desktop's model through the
TOM runner's ``query`` action (``QueryEvaluator``). A number an evaluator cannot compute is
"not checked" with the reason, never "failed": only a number that really differs, or an error
Power BI itself would show, fails. ``gate`` compares a run before an edit with a run after it
and returns the tests the edit broke; ``ReportTestsBroken`` is the confirmation the edit
pipeline raises for them (kind ``report_tests_broken``).

A test's query is the visual's own (``live_report._grouped``): the filters wrap the value the
same way, so ALLSELECTED and KEEPFILTERS see the filter context the canvas gives them, and a
data point is read from its row of the visual's grouping (FILTER over SUMMARIZECOLUMNS), not
recomputed under a filter on its category, which ALLSELECTED would read differently.

Deterministic code only: the language model may phrase results, it never computes them.
Specified in docs/imported-report-tests.md.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Literal

from filelock import FileLock, Timeout
from pydantic import (
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from ai_powerbi_analyst.contracts import ClosedModel

from . import live_report
from .contracts import DatasetSnapshot
from .dax_engine import DaxEngineError, DaxEngineLimit, DaxUnsupported, Engine, Model, run_query
from .dax_format import format_value
from .dax_syntax import (
    Binary,
    Call,
    ColRef,
    DaxParseError,
    Node,
    Num,
    Str,
    TableCtor,
    Unary,
    is_query,
    parse_expression,
    to_dax,
)
from .pbir_document import PbirDocument
from .render_rules import display_format, is_percent
from .report_edit_context import NeedsConfirmation
from .report_edit_model import (
    MODEL_FAMILY_OPERATIONS,
    RenameColumn,
    RenameTable,
    rename_dax_column,
    rename_dax_table,
)
from .report_edit_ops import CreateMeasure, DeleteMeasure, RenameMeasure, UpdateMeasure
from .report_engine import ENGINE_LABEL, report_model, unavailable_reference
from .report_import import ImportedReport
from .report_refs import rename_dax_references
from .report_revisions import _write_atomic

SCHEMA_VERSION = 1
FILE_NAME = "tests.json"
# A developer pins the headline numbers of a report, not every cell; 200 keeps a run of all
# of them on the local engine within a few seconds for a dashboard like Agents Performance.
MAX_TESTS = 200
MAX_FILTERS = 24
MAX_VALUES = 100
MAX_NAME = 200
MAX_EXPRESSION = 4000
# Two engines (or one engine summing in another order) differ in the 15th-16th significant
# digit; a pinned number that moved less than a billionth of itself did not change.
DEFAULT_TOLERANCE_REL = 1e-9
DEFAULT_TOLERANCE_ABS = 1e-9
# One test query on the local engine; a slower one is "not checked", never failed.
QUERY_BUDGET = 30.0
# How long a writer waits for another thread or process to finish writing tests.json.
LOCK_TIMEOUT = 10.0
# suggest_tests proposes this many tests at most, and fills up to the minimum from charts
# when a report has few cards.
MAX_SUGGESTED = 8
MIN_SUGGESTED = 5
DESKTOP_LABEL = "Power BI Desktop"
CONFIRM_KIND = "report_tests_broken"

_REPORT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_TEST_ID = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_ISO_DAY = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:T00:00:00(?:\.0+)?)?")
_NUMBER_TEXT = re.compile(r"[-+]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][-+]?\d+)?")

Value = StrictBool | StrictInt | StrictFloat | Annotated[StrictStr, Field(max_length=500)] | None
Name = Annotated[str, Field(min_length=1, max_length=MAX_NAME)]


# --- definitions ------------------------------------------------------------------------------


class MeasureRef(ClosedModel):
    table: Name
    name: Name


class TestFilter(ClosedModel):
    """One condition of a test's filter context. Every filter of a test holds at once.

    ``values``: ``in``/``not_in`` list the values; ``between`` is ``[min, max]``, both ends
    included, ``null`` for an open end. ``date``: the column holds dates, and each value is an
    ISO day (``2024-01-31``). ``point``: a category of the pinned data point (the chart's axis
    value, the table's row), which groups the query instead of filtering it. ``measure``:
    ``column`` names a measure (a visual filter on a measure, kept for a card: it decides
    whether the card shows its value at all).
    """

    table: Name
    column: Name
    op: Literal["in", "not_in", "between"]
    values: list[Value] = Field(min_length=1, max_length=MAX_VALUES)
    date: bool = False
    point: bool = False
    measure: bool = False

    @model_validator(mode="after")
    def _shape(self) -> TestFilter:
        if self.op == "between":
            if len(self.values) != 2 or all(v is None for v in self.values):
                raise ValueError("A range filter needs a lowest and a highest value")
            low, high = self.values
            for bound in (low, high):
                if bound is not None and (
                    isinstance(bound, bool) or not isinstance(bound, int | float | str)
                ):
                    raise ValueError("A range filter's ends must be numbers or dates")
            if (
                isinstance(low, int | float)
                and isinstance(high, int | float)
                and not isinstance(low, bool)
                and not isinstance(high, bool)
                and low > high
            ):
                raise ValueError("A range filter's lowest value is above its highest")
        if self.point and (self.op != "in" or len(self.values) != 1 or self.measure):
            raise ValueError("A data point's category has exactly one value")
        if self.date:
            for value in self.values:
                if value is not None and (
                    not isinstance(value, str) or _ISO_DAY.fullmatch(value) is None
                ):
                    raise ValueError("A date filter's values are days written YYYY-MM-DD")
        for value in self.values:
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("A filter value must be a finite number")
        return self


class ExpectEquals(ClosedModel):
    """The number equals ``equals`` within the tolerances (the larger of the two applies);
    ``equals`` null expects a blank. Text and true/false compare exactly."""

    equals: Value
    tolerance_abs: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    tolerance_rel: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None

    @field_validator("equals")
    @classmethod
    def _finite(cls, value: Value) -> Value:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("A test expects a finite number")
        return value


class ExpectBetween(ClosedModel):
    between: tuple[float | None, float | None]

    @field_validator("between")
    @classmethod
    def _bounds(cls, value: tuple[float | None, float | None]) -> tuple[float | None, float | None]:
        low, high = value
        if low is None and high is None:
            raise ValueError("A range needs a lowest or a highest value")
        if any(b is not None and not math.isfinite(b) for b in value):
            raise ValueError("A range's ends must be finite numbers")
        if low is not None and high is not None and low > high:
            raise ValueError("A range's lowest value is above its highest")
        return value


class ExpectNotBlank(ClosedModel):
    not_blank: Literal[True]


Expectation = ExpectEquals | ExpectBetween | ExpectNotBlank


class Origin(ClosedModel):
    """Where a pinned test came from: the page and visual the number was on."""

    page: Annotated[str, Field(min_length=1, max_length=200)]
    visual: Annotated[str, Field(min_length=1, max_length=200)]


class TestDefinition(ClosedModel):
    """One pinned or typed number and what it should be.

    Exactly one of ``measure`` (a measure of the model) and ``expression`` (a DAX expression
    the user typed; never generated) says what is computed. ``format`` is the number format
    the value is shown with (the field's own, or the model's), for plain-language answers.
    """

    __test__ = False  # not a pytest test class, despite its name

    id: Annotated[str, Field(pattern=_TEST_ID)]
    name: Name
    measure: MeasureRef | None = None
    expression: Annotated[str, Field(min_length=1, max_length=MAX_EXPRESSION)] | None = None
    filters: list[TestFilter] = Field(default_factory=list, max_length=MAX_FILTERS)
    expect: Expectation
    created_at: Annotated[str, Field(min_length=10, max_length=40)]
    source: Literal["user", "pinned"]
    format: Annotated[str, Field(max_length=200)] | None = None
    origin: Origin | None = None

    @model_validator(mode="after")
    def _what(self) -> TestDefinition:
        if (self.measure is None) == (self.expression is None):
            raise ValueError("A test checks either a measure or a DAX expression")
        if self.expression is not None:
            if self.source != "user":
                raise ValueError("Only a formula the user typed is tested as an expression")
            if is_query(self.expression):
                raise ValueError("A test's formula is an expression, not a query")
            parse_expression(self.expression)  # DaxParseError is a ValueError
        if self.measure is None and any(f.point for f in self.filters):
            raise ValueError("Only a measure pinned from a visual has a data point")
        return self


class TestResult(ClosedModel):
    """The outcome of one test: ``value`` is what the evaluator gave (null: blank, or not
    computed), ``shown`` that value as the report formats it, ``message`` one plain sentence."""

    __test__ = False

    id: str
    name: str
    status: Literal["passed", "failed", "not_checked"]
    value: Value = None
    shown: str | None = None
    expected: Expectation
    message: str
    engine: str | None = None


class TestRun(ClosedModel):
    """The last run kept with the tests: for the head revision ``revision``/``hash``."""

    __test__ = False

    revision: str | None = None
    hash: str | None = None
    engine: str
    ran_at: str
    results: list[TestResult] = Field(default_factory=list, max_length=MAX_TESTS)


class TestFile(ClosedModel):
    """``tests.json``."""

    __test__ = False

    version: Literal[1] = 1
    tests: list[TestDefinition] = Field(default_factory=list, max_length=MAX_TESTS)
    last_run: TestRun | None = None

    @model_validator(mode="after")
    def _unique(self) -> TestFile:
        ids = [t.id for t in self.tests]
        if len(set(ids)) != len(ids):
            raise ValueError("Two tests have the same id")
        return self


def now_text(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")


def definition_id(
    measure: MeasureRef | None, expression: str | None, filters: Sequence[TestFilter]
) -> str:
    """An id from what a test computes: pinning the same number twice gives the same id, so
    the store can say the number already has a test."""
    identity = {
        "measure": [measure.table.casefold(), measure.name.casefold()] if measure else None,
        "expression": " ".join(expression.split()) if expression else None,
        "filters": [f.model_dump(mode="json") for f in filters],
    }
    text = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
    return "t" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# --- the store -------------------------------------------------------------------------------


class TestStoreError(ValueError):
    """tests.json cannot be read or changed as asked. A damaged file is reported, never reset:
    a reset would drop the user's tests."""

    __test__ = False


class TestStore:
    """The tests of each imported report, in ``reports/<report id>/tests.json`` under
    Studio's state root. Every change is a whole-file atomic replace (temporary file, flush,
    ``os.replace`` with Windows retries: ``report_revisions._write_atomic``) under a lock."""

    __test__ = False

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path(self, report_id: str) -> Path:
        if not _REPORT_ID.fullmatch(report_id):
            raise TestStoreError("This is not a report Studio knows")
        return self.root / "reports" / report_id / FILE_NAME

    def read(self, report_id: str) -> TestFile:
        path = self.path(report_id)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return TestFile()
        except OSError as error:
            raise TestStoreError(f"Studio could not read this report's tests ({error})") from error
        try:
            return TestFile.model_validate_json(data)
        except ValidationError as error:
            raise TestStoreError(
                "This report's tests file is damaged, so Studio left it as it is. Restore it "
                f"or remove it ({path})."
            ) from error

    def tests(self, report_id: str) -> list[TestDefinition]:
        return list(self.read(report_id).tests)

    def last_run(self, report_id: str) -> TestRun | None:
        return self.read(report_id).last_run

    def add(self, report_id: str, test: TestDefinition) -> TestDefinition:
        def change(current: TestFile) -> TestFile:
            if any(t.id == test.id for t in current.tests):
                raise TestStoreError(f"This number already has a test ({_existing(current, test)})")
            if len(current.tests) >= MAX_TESTS:
                raise TestStoreError(
                    f"A report keeps at most {MAX_TESTS} tests; remove one before adding another"
                )
            return current.model_copy(update={"tests": [*current.tests, test]})

        self._change(report_id, change)
        return test

    def replace(self, report_id: str, test: TestDefinition) -> TestDefinition:
        """Put ``test`` in place of the test with its id (an edited expectation or name)."""

        def change(current: TestFile) -> TestFile:
            if not any(t.id == test.id for t in current.tests):
                raise TestStoreError("There is no such test in this report")
            return current.model_copy(
                update={"tests": [test if t.id == test.id else t for t in current.tests]}
            )

        self._change(report_id, change)
        return test

    def replace_all(self, report_id: str, tests: Sequence[TestDefinition]) -> None:
        """Every test at once (``follow_renames`` after a rename was committed)."""
        self._change(report_id, lambda current: current.model_copy(update={"tests": list(tests)}))

    def remove(self, report_id: str, test_id: str) -> bool:
        removed = False

        def change(current: TestFile) -> TestFile:
            nonlocal removed
            kept = [t for t in current.tests if t.id != test_id]
            removed = len(kept) != len(current.tests)
            return current.model_copy(update={"tests": kept})

        self._change(report_id, change)
        return removed

    def record_run(self, report_id: str, run: TestRun) -> None:
        self._change(report_id, lambda current: current.model_copy(update={"last_run": run}))

    def _change(self, report_id: str, change: Callable[[TestFile], TestFile]) -> None:
        path = self.path(report_id)
        if not path.parent.is_dir():
            raise TestStoreError("This report is not imported in Studio")
        lock = FileLock(str(path.with_name(FILE_NAME + ".lock")), timeout=LOCK_TIMEOUT)
        try:
            with lock:
                updated = TestFile.model_validate(change(self.read(report_id)).model_dump())
                _write_atomic(path, updated.model_dump_json(indent=1).encode("utf-8"))
        except Timeout as error:
            raise TestStoreError(
                "Another change to this report's tests is still being written; try again"
            ) from error
        except ValidationError as error:
            raise TestStoreError(f"The tests could not be saved ({_first_error(error)})") from error


def _existing(current: TestFile, test: TestDefinition) -> str:
    found = next((t for t in current.tests if t.id == test.id), None)
    return f"'{found.name}'" if found is not None else test.id


def _first_error(error: ValidationError) -> str:
    errors = error.errors()
    return str(errors[0].get("msg", "invalid")) if errors else "invalid"


# --- the query of a test ---------------------------------------------------------------------


def _column_ref(table: str, column: str) -> str:
    return to_dax(ColRef(table, column))


def _literal(value: Value, dated: bool) -> str:
    if dated and isinstance(value, str):
        match = _ISO_DAY.fullmatch(value)
        if match is None:
            raise NotComputable(f"{value!r} is not a day written YYYY-MM-DD")
        year, month, day = (int(part) for part in match.groups())
        return f"DATE({year}, {month}, {day})"
    return live_report.literal(value)


def _condition(item: TestFilter) -> str:
    ref = (
        to_dax(ColRef(None, item.column)) if item.measure else _column_ref(item.table, item.column)
    )
    if item.op == "between":
        low, high = item.values
        parts = [f"{ref} >= {_literal(low, item.date)}"] if low is not None else []
        parts += [f"{ref} <= {_literal(high, item.date)}"] if high is not None else []
        return " && ".join(f"({p})" for p in parts) if len(parts) > 1 else parts[0]
    listed = f"{ref} IN {{{', '.join(_literal(v, item.date) for v in item.values)}}}"
    return f"NOT({listed})" if item.op == "not_in" else listed


def query_of(test: TestDefinition) -> str:
    """The DAX query computing ``test``: one column, ``[Value]``, and one row (none for a
    blank data point).

    Built like the visual's own query (``live_report._grouped``): the filters in their order,
    a column filter as ``KEEPFILTERS(FILTER(ALL(table), ...))``, a filter on a measure as a
    row filter. A pinned data point is its row of ``SUMMARIZECOLUMNS`` over its categories.
    A measure is named bare (``[Total Sales]``): measure names are unique in a model, and a
    measure moved to another table is still the same number."""
    value = (
        to_dax(ColRef(None, test.measure.name))
        if test.measure is not None
        else f"({test.expression})"
    )
    point = [f for f in test.filters if f.point]
    predicates: list[live_report.Predicate] = [(_condition(f), {f.table}, True) for f in point]
    predicates += [
        (_condition(f), {f.table}, f.measure)
        for f in test.filters
        # A filter on a measure only decides which rows a grouped visual shows.
        if not f.point and not (f.measure and point)
    ]
    groups = [_column_ref(f.table, f.column) for f in point]
    base = live_report.grouped_query(groups, [(0, value)], [], predicates)
    return f'EVALUATE SELECTCOLUMNS({base}, "Value", [M0])'


# --- evaluating ------------------------------------------------------------------------------


class NotComputable(Exception):
    """The evaluator cannot compute this test here (a Power BI only feature, a slow query,
    Desktop not answering): the test is "not checked" with this plain reason."""


class TestBroken(Exception):
    """The number no longer exists as the test names it (a measure or column gone, an error
    Power BI reports): the test fails with this plain reason."""

    __test__ = False


Evaluate = Callable[[TestDefinition], Value]


def _plain_error(error: BaseException) -> str:
    text = " ".join(str(error).split()) or type(error).__name__
    return text if len(text) <= 300 else text[:297] + "..."


def _cell(value: object) -> Value:
    """A result cell as a test value: numbers as float (int stays int), a date as its ISO
    text, NaN and infinities blank."""
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float | Decimal):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _single(rows: Sequence[Sequence[object]]) -> Value:
    if not rows:
        return None
    if len(rows) > 1:
        raise NotComputable("the test's filters match more than one row of the visual")
    return _cell(rows[0][0]) if rows[0] else None


class LocalEvaluator:
    """Tests on Studio's DAX engine over the imported data: the head-aware model of
    ``report_engine`` (what the canvas draws with), built once.

    One engine answers every test of a run (``prepare``): the measures several tests share
    (the Agents cards all read [MTD Total Sales] per employee) are computed once, which
    halves a run of the Agents suggestions (4.6 s to 2.3 s). Each test still has its own
    time budget."""

    label = ENGINE_LABEL

    def __init__(
        self,
        report: ImportedReport,
        snapshot: DatasetSnapshot | None = None,
        *,
        model: Model | None = None,
        time_budget: float = QUERY_BUDGET,
    ) -> None:
        if model is None and snapshot is None:
            raise ValueError("Give the report's snapshot or its model")
        self.report, self.snapshot, self.time_budget = report, snapshot, time_budget
        self._model = model
        self._failure: str | None = None
        self._engine: Engine | None = None

    def prepare(self, tests: Sequence[TestDefinition]) -> None:
        try:
            self._engine = Engine(self.model(), time_budget=self.time_budget)
        except NotComputable:
            self._engine = None

    def model(self) -> Model:
        if self._model is not None:
            return self._model
        if self._failure is None:
            assert self.snapshot is not None
            try:
                self._model = report_model(self.report, self.snapshot)
                return self._model
            except (DaxEngineError, OSError, ValueError) as error:
                self._failure = _plain_error(error)
        raise NotComputable(f"Studio cannot calculate this report's model ({self._failure})")

    def __call__(self, test: TestDefinition) -> Value:
        model = self.model()
        query = query_of(test)
        why = unavailable_reference(model, query)
        if why is not None:
            raise NotComputable(why)
        _check_names(test, model)
        engine = self._engine
        if engine is None or engine.model is not model:
            return _single(run_query(model, query, time_budget=self.time_budget).rows)
        engine.deadline = time.monotonic() + self.time_budget
        return _single(engine.run_query(query).rows)


def _check_names(test: TestDefinition, model: Model) -> None:
    """Fail a test whose measure or filter column the model no longer has, in words the user
    knows (the engine's own message names DAX internals)."""
    if test.measure is not None and model.measures.get(_fold(test.measure.name)) is None:
        raise TestBroken(f"The measure [{test.measure.name}] is no longer in the model")
    for item in test.filters:
        if item.measure:
            if model.measures.get(_fold(item.column)) is None:
                raise TestBroken(f"The measure [{item.column}] this test filters on is gone")
        elif not model.has_column(item.table, item.column):
            raise TestBroken(
                f"The column '{item.table}'[{item.column}] this test filters on is no longer "
                "in the model"
            )


def _fold(name: str) -> str:
    return name.strip().casefold()


QueryRun = Callable[[Mapping[str, str]], Mapping[str, Any]]


class QueryEvaluator:
    """Tests through a DAX-query callable: ``run({id: query}) -> {id: {ok, rows | error}}``,
    the shape of ``desktop_model.query(runner, server, queries)`` (Power BI Desktop's own model
    through Studio's TOM runner; ``pbi.exe`` behind an adapter works the same). ``prepare``
    sends every test's query in one call; ``run_tests`` calls it first."""

    def __init__(self, run: QueryRun, *, label: str = DESKTOP_LABEL) -> None:
        self.run, self.label = run, label
        self._answers: dict[str, Any] = {}
        self._failure: str | None = None

    def prepare(self, tests: Sequence[TestDefinition]) -> None:
        queries = {t.id: query_of(t) for t in tests if t.id not in self._answers}
        if not queries or self._failure is not None:
            return
        try:
            self._answers.update(self.run(queries))
        except (RuntimeError, OSError, ValueError, TimeoutError) as error:
            self._failure = _plain_error(error)

    def __call__(self, test: TestDefinition) -> Value:
        if test.id not in self._answers:
            self.prepare([test])
        if self._failure is not None:
            raise NotComputable(f"{self.label} did not answer ({self._failure})")
        answer = self._answers.get(test.id)
        if not isinstance(answer, Mapping):
            raise NotComputable(f"{self.label} gave no answer for this test")
        if not answer.get("ok"):
            error = _plain_error(Exception(answer.get("error") or "no details")).rstrip(".")
            raise TestBroken(f"Power BI reports an error in this number ({error})")
        rows = answer.get("rows")
        if not isinstance(rows, list):
            raise NotComputable(f"{self.label} gave no rows for this test")
        numeric = _expects_number(test.expect)
        return _single(
            [
                [_desktop_cell(v, numeric) for v in row.values()]
                for row in rows
                if isinstance(row, Mapping)
            ]
        )


def _expects_number(expect: Expectation) -> bool:
    if isinstance(expect, ExpectBetween):
        return True
    return isinstance(expect, ExpectEquals) and _number(expect.equals) is not None


def _desktop_cell(value: object, numeric: bool) -> object:
    """A value as the runner prints it: numbers, booleans and text. A decimal (a currency or
    fixed decimal result) comes as its text, so for a test of a number such text is read back
    as the number; a test of text keeps it as text ("02134" stays a code).
    UNVERIFIED: the runner's text of a decimal under a Windows culture with a decimal comma."""
    if not numeric or not isinstance(value, str):
        return value
    text = value.strip()
    if _NUMBER_TEXT.fullmatch(text) is None:
        return value
    if "," in text and "." not in text:
        text = text.replace(",", ".")  # a decimal comma: the runner's culture
    elif "," in text:
        return value
    try:
        return Decimal(text)
    except InvalidOperation:
        return value


# --- running and comparing --------------------------------------------------------------------


def run_tests(
    tests: Sequence[TestDefinition], evaluate: Evaluate, *, engine: str | None = None
) -> list[TestResult]:
    """Each test's result, in the tests' order. ``evaluate`` returns the number or raises
    ``NotComputable``/``DaxUnsupported`` (not checked, with the reason: never failed);
    ``TestBroken`` or a DAX error Power BI would show too fails the test."""
    label = engine or getattr(evaluate, "label", None)
    prepare = getattr(evaluate, "prepare", None)
    if callable(prepare):
        prepare(tests)
    return [_run_one(test, evaluate, label) for test in tests]


Status = Literal["passed", "failed", "not_checked"]


def _run_one(test: TestDefinition, evaluate: Evaluate, label: str | None) -> TestResult:
    def result(status: Status, message: str, value: Value = None) -> TestResult:
        return TestResult(
            id=test.id,
            name=test.name,
            status=status,
            value=value,
            shown=shown_value(value, test.format) if status != "not_checked" else None,
            expected=test.expect,
            message=message,
            engine=label,
        )

    try:
        value = _cell(evaluate(test))
    except (NotComputable, DaxUnsupported, DaxEngineLimit, DaxParseError) as error:
        return result("not_checked", f"{test.name} was not checked: {_reason(error)}.")
    except TestBroken as error:
        return result("failed", f"{test.name} cannot be calculated: {_reason(error)}.")
    except DaxEngineError as error:
        if isinstance(error.__cause__, DaxParseError):
            # Studio's reader does not know this formula's syntax; Power BI may well.
            return result("not_checked", f"{test.name} was not checked: {_reason(error)}.")
        return result(
            "failed",
            f"{test.name} cannot be calculated: Power BI would show an error ({_reason(error)}).",
        )
    except (ValueError, ArithmeticError, LookupError, TypeError) as error:
        return result("not_checked", f"{test.name} was not checked: {_reason(error)}.")
    passed, message = compare(test, value)
    return result("passed" if passed else "failed", message, value)


def _reason(error: BaseException) -> str:
    text = _plain_error(error)
    return text[:-1] if text.endswith(".") else text


def _number(value: Value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def compare(test: TestDefinition, value: Value) -> tuple[bool, str]:
    """Whether ``value`` meets the test's expectation, and the sentence that says so."""
    expect, name = test.expect, test.name
    shown = shown_value(value, test.format)
    if isinstance(expect, ExpectNotBlank):
        if value is None or value == "":
            return False, f"{name} is blank now."
        return True, f"{name} is still not blank ({shown})."
    if isinstance(expect, ExpectBetween):
        low, high = expect.between
        amount = _number(value)
        wanted_text = expectation_text(test)
        if amount is None:
            what = "blank" if value is None else shown
            return False, f"{name} is {what}, not {wanted_text}."
        if (low is None or amount >= low) and (high is None or amount <= high):
            return True, f"{name} is still {wanted_text}: {shown}."
        return False, f"{name} is {shown}, not {wanted_text}."
    expected = expect.equals
    target = shown_value(expected, test.format)
    if expected is None:
        if value is None or value == "":
            return True, f"{name} is still blank."
        return False, f"{name} is {shown} now, not blank."
    if value is None:
        return False, f"{name} is blank now instead of {target}."
    number, wanted = _number(value), _number(expected)
    if wanted is not None:
        if number is None:
            return False, f"{name} is {shown} now instead of {target}."
        allowed = max(
            expect.tolerance_abs if expect.tolerance_abs is not None else DEFAULT_TOLERANCE_ABS,
            (expect.tolerance_rel if expect.tolerance_rel is not None else DEFAULT_TOLERANCE_REL)
            * abs(wanted),
        )
        if abs(number - wanted) <= allowed:
            return True, f"{name} is still {shown}."
        if shown == target:
            # The short forms hide what moved: show the digits.
            shown, target = f"{number:,.10g}", f"{wanted:,.10g}"
        return False, f"{name} is {shown} now instead of {target}."
    if value == expected and type(value) is type(expected):
        return True, f"{name} is still {shown}."
    return False, f"{name} is {shown} now instead of {target}."


def gate(before: Sequence[TestResult], after: Sequence[TestResult]) -> list[TestResult]:
    """The tests an edit broke: passed before it and fail after it (``after``'s results, in
    its order). A test that was not checked on either side, or already failed, never blocks."""
    passed = {r.id for r in before if r.status == "passed"}
    return [r for r in after if r.id in passed and r.status == "failed"]


class ReportTestsBroken(NeedsConfirmation):
    """The edit would change numbers the user pinned: nothing was written. ``results`` are
    the broken tests; the edit is applied only with the ``report_tests_broken`` confirmation."""

    def __init__(self, results: Sequence[TestResult]) -> None:
        super().__init__(CONFIRM_KIND, confirmation_text(results))
        self.results = list(results)


def check_gate(
    before: Sequence[TestResult], after: Sequence[TestResult], *, confirmed: bool = False
) -> list[TestResult]:
    """``gate``, raising ``ReportTestsBroken`` for broken tests unless ``confirmed``; returns
    the broken tests (for the receipt when the user confirmed)."""
    broken = gate(before, after)
    if broken and not confirmed:
        raise ReportTestsBroken(broken)
    return broken


def confirmation_text(results: Sequence[TestResult]) -> str:
    count = len(results)
    head = (
        "This change would break a report test"
        if count == 1
        else f"This change would break {count} report tests"
    )
    lines = [f"{head}:"] + [f"- {r.message}" for r in results[:6]]
    if count > 6:
        lines.append(f"- and {count - 6} more.")
    lines.append("Apply it anyway?")
    return "\n".join(lines)


# --- plain words -------------------------------------------------------------------------------


def _plain_number(value: float | int, *, grouped: bool = True) -> str:
    """A number without a format: thousands grouped for an amount ("3,698"), not for a
    filter value, which is often a year or a key ("2024")."""
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        value = int(value)
    if isinstance(value, int):
        return f"{value:,}" if grouped else str(value)
    return f"{value:,.6g}" if grouped else f"{value:.6g}"


def shown_value(value: Value, fmt: str | None = None) -> str:
    """A value as a person reads it: the report's format, millions and billions shortened
    ("$18.91M"), "blank" for a blank."""
    if value is None:
        return "blank"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    pattern = fmt or ""
    try:
        if abs(value) >= 1e6 and not (pattern and is_percent(pattern)):
            units = 1e9 if abs(value) >= 1e9 else 1e6
            custom = display_format(pattern, units, 2)
            if custom is not None:
                return format_value(value / custom[0], custom[1])
        if pattern:
            text = format_value(value, pattern)
            if text.strip():
                return text.strip()
    except (ValueError, ArithmeticError, IndexError):
        pass
    return _plain_number(value)


def expectation_text(test: TestDefinition) -> str:
    expect = test.expect
    if isinstance(expect, ExpectNotBlank):
        return "not blank"
    if isinstance(expect, ExpectBetween):
        low, high = (None if b is None else shown_value(b, test.format) for b in expect.between)
        if low is not None and high is not None:
            return f"between {low} and {high}"
        return f"at least {low}" if low is not None else f"at most {high}"
    shown = shown_value(expect.equals, test.format)
    tolerance = []
    if expect.tolerance_abs:
        tolerance.append(f"give or take {_plain_number(expect.tolerance_abs)}")
    if expect.tolerance_rel:
        tolerance.append(f"give or take {expect.tolerance_rel * 100:g}%")
    return shown + (f" ({' or '.join(tolerance)})" if tolerance else "")


def _value_text(value: Value) -> str:
    if value is None:
        return "(Blank)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return _plain_number(value, grouped=False)
    return value


def _filter_text(item: TestFilter) -> str:
    what = f"[{item.column}]" if item.measure else item.column
    values = [_value_text(v) for v in item.values]
    if item.op == "between":
        low, high = item.values
        if low is not None and high is not None:
            return f"{what} is between {values[0]} and {values[1]}"
        if low is not None:
            return f"{what} is at least {values[0]}"
        return f"{what} is at most {values[1]}"
    listed = values[0] if len(values) == 1 else ", ".join(values[:-1]) + " or " + values[-1]
    if len(values) > 6:
        listed = ", ".join(values[:5]) + f" or {len(values) - 5} other values"
    return f"{what} is {'not ' if item.op == 'not_in' else ''}{listed}"


def filters_text(test: TestDefinition, *, point: bool = True) -> str:
    """The filter context in words: " for Others when Top-Bottom-N is 6 and ...", or ""."""
    values = [_value_text(f.values[0]) for f in test.filters if f.point]
    others = [_filter_text(f) for f in test.filters if not f.point]
    text = f" for {', '.join(values)}" if values and point else ""
    if others:
        joined = others[0] if len(others) == 1 else ", ".join(others[:-1]) + " and " + others[-1]
        text += f" when {joined}"
    return text


def describe(test: TestDefinition) -> str:
    """One sentence: "Agents Avg MTD Sales ([Employees Avg MTD Sales]) should be $73,514.80
    when Top-Bottom-N is 6"."""
    expect = test.expect
    if isinstance(expect, ExpectEquals):
        wanted = "be blank" if expect.equals is None else f"be {expectation_text(test)}"
    elif isinstance(expect, ExpectBetween):
        wanted = f"be {expectation_text(test)}"
    else:
        wanted = "not be blank"
    what = test.name
    if test.measure is not None and _fold(test.measure.name) not in _fold(test.name):
        what += f" ([{test.measure.name}])"
    # A pinned point's name already says which point ("... for Others").
    named = _fold(filters_text(test).split(" when ")[0]) in _fold(f" {test.name}")
    return f"{what} should {wanted}{filters_text(test, point=not named)}."


def describe_results(results: Sequence[TestResult]) -> str:
    """The results for a person: a first line that counts them, then a line per test that
    failed or was not checked, and the passing ones while there are few."""
    if not results:
        return "This report has no tests yet."
    failed = [r for r in results if r.status == "failed"]
    unchecked = [r for r in results if r.status == "not_checked"]
    passed = [r for r in results if r.status == "passed"]
    total = len(results)
    noun = "report test" if total == 1 else "report tests"
    if not failed and not unchecked:
        head = f"The {noun} passes." if total == 1 else f"All {total} {noun} pass."
    else:
        parts = [f"{len(passed)} of {total} {noun} pass"]
        if failed:
            parts.append(f"{len(failed)} {'fails' if len(failed) == 1 else 'fail'}")
        if unchecked:
            parts.append(f"{len(unchecked)} {'was' if len(unchecked) == 1 else 'were'} not checked")
        head = (", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0]) + "."
    lines = [head]
    lines += [f"- {r.message}" for r in failed]
    lines += [f"- {r.message}" for r in unchecked]
    if len(passed) <= 8:
        lines += [f"- {r.message}" for r in passed]
    return "\n".join(lines)


# --- pinning a number from a visual ----------------------------------------------------------


class PinError(ValueError):
    """The number cannot be pinned as asked; the message says why, in plain words."""


PointValues = Mapping[str, Value]


def pin_from_visual(
    document: PbirDocument | None,
    page_id: str,
    visual_id: str,
    point: PointValues | None,
    *,
    value: Value,
    report: ImportedReport,
    measure: str | None = None,
    name: str | None = None,
    now: datetime | None = None,
) -> TestDefinition:
    """A test that ``value`` is what the visual shows: its measure under every filter in
    effect (report, page and visual filters, the slicers that reach it, as ``live_report``
    compiles them for the canvas) and, for a chart or table, the data point's categories.

    ``point`` maps each category column of the visual (by its ``query_ref`` as the render
    payload's columns give it, or ``Table.Column``) to the row's value; it is empty for a
    card. ``measure`` picks the measure (``query_ref`` or name) when the visual shows several;
    a hidden helper measure (a card's reference label) can be named too. ``document`` is the
    caller's view of the report, checked to still have the visual; the filters are read from
    the head (``report``), where the number was computed. Raises ``PinError``.
    """
    return _pin(
        document,
        _page_plan(report, page_id),
        visual_id,
        point,
        value=value,
        measure=measure,
        name=name,
        now=now,
    )


@dataclass
class _PagePlan:
    """A page as the renderer resolves it for its queries (``live_report._page_context`` and
    ``_scope``), shared by several pins of the same page."""

    report: ImportedReport
    ctx: live_report.PageContext
    scope: live_report.PageScope
    request: live_report.LiveReportRequest


def _page_plan(
    report: ImportedReport, page_id: str, plans: dict[str, _PagePlan] | None = None
) -> _PagePlan:
    if plans is not None and page_id in plans:
        return plans[page_id]
    request = live_report.LiveReportRequest()
    try:
        ctx = live_report.page_context(report, page_id, request)
        scope = live_report.page_scope(ctx, request)
    except live_report.Unsupported as error:
        raise PinError(
            f"Studio cannot reproduce this page's filters ({error}), so it cannot pin its numbers"
        ) from error
    except (KeyError, TypeError, AttributeError, IndexError) as error:
        raise PinError(
            "Studio cannot read this page's filters, so it cannot pin its numbers"
        ) from error
    except ValueError as error:
        raise PinError(str(error)) from error
    plan = _PagePlan(report, ctx, scope, request)
    if plans is not None:
        plans[page_id] = plan
    return plan


def _pin(
    document: PbirDocument | None,
    page: _PagePlan,
    visual_id: str,
    point: PointValues | None,
    *,
    value: Value,
    measure: str | None,
    name: str | None,
    now: datetime | None,
) -> TestDefinition:
    page_id, report, ctx = page.ctx.page.id, page.report, page.ctx
    if document is not None:
        doc_page = document.pages.get(page_id)
        if doc_page is None or visual_id not in doc_page.visuals:
            raise PinError("That visual is no longer on this page")
    visual, fields, filters = _visual_plan(page, visual_id)
    field = _chosen_measure(fields, measure, report)
    groups = [f for f in fields if f["kind"] == "column"]
    dated = _date_columns(report)
    given = dict(point or {})
    if groups and not given:
        raise PinError("Choose a data point of this visual to pin: a bar, a point or a row")
    pinned: list[TestFilter] = []
    for group in groups:
        keys = (group.get("query_ref"), f"{group['table']}.{group['name']}")
        key = next((k for k in keys if k in given), None)
        if key is None:
            raise PinError(f"Say which {group.get('label') or group['name']} the number is for")
        is_date = (group["table"], group["name"]) in dated
        pinned.append(
            _filter(
                group["table"],
                group["name"],
                "in",
                [_day(given[key]) if is_date else given[key]],
                date=is_date,
                point=True,
            )
        )
    context: list[TestFilter] = []
    for condition, _tables, is_measure in filters:
        if is_measure and groups:
            continue  # it decides which rows the visual shows, not the value of this one
        context.extend(_structured(condition, is_measure, dated))
    reference = MeasureRef(table=field["table"], name=field["name"])
    all_filters = [*pinned, *context]
    fmt = field.get("_format") or ctx.formats.get((field["table"], field["name"])) or None
    label = name or _default_name(visual, field, fields, pinned)
    try:
        return TestDefinition(
            id=definition_id(reference, None, all_filters),
            name=label[:MAX_NAME],
            measure=reference,
            filters=all_filters,
            expect=ExpectEquals(equals=_cell(value)),
            created_at=now_text(now),
            source="pinned",
            format=str(fmt)[:200] if fmt else None,
            origin=Origin(page=page_id, visual=visual_id),
        )
    except ValidationError as error:
        raise PinError(f"This number cannot be pinned ({_first_error(error)})") from error


def _visual_plan(
    page: _PagePlan, visual_id: str
) -> tuple[Any, list[dict[str, Any]], list[live_report.Predicate]]:
    """The visual, its fields and the filters its query applies, from the renderer itself: a
    test must hold the same filter context the canvas computes the visual with."""
    visual = next((v for v in page.ctx.page.visuals if v.id == visual_id), None)
    if visual is None:
        raise PinError("That visual is not on this page")
    kind = live_report.kind_of(visual.visual_type)
    if kind not in live_report.DATA_KINDS or kind == "slicer":
        raise PinError("Pin a number from a card, a chart or a table")
    try:
        fields = live_report.projections(page.ctx.compiler, visual)
        plan = live_report.visual_plan(page.ctx, page.scope, visual, kind, fields, page.request)
    except (live_report.Unsupported, KeyError, TypeError, AttributeError, IndexError) as error:
        reason = str(error) if isinstance(error, live_report.Unsupported) else "it is not supported"
        raise PinError(
            f"Studio cannot reproduce this visual's filters ({reason}), so it cannot pin its "
            "numbers"
        ) from error
    except ValueError as error:
        raise PinError(str(error)) from error
    return visual, fields, plan.filters


def _chosen_measure(
    fields: list[dict[str, Any]], measure: str | None, report: ImportedReport
) -> dict[str, Any]:
    """The field whose number is pinned. A measure the visual's formatting adds (a card's
    reference label, a conditional color) is not a projection: it is found in the model and
    computed under the same filters, as the canvas computes it."""
    measures = [f for f in fields if f["kind"] == "measure"]
    if measure is not None:
        wanted = _fold(measure)
        found = next(
            (
                f
                for f in measures
                if _fold(str(f.get("query_ref") or "")) == wanted or _fold(f["name"]) == wanted
            ),
            None,
        )
        if found is not None:
            return found
        if any(_fold(f["name"]) == wanted for f in fields if f["kind"] == "column"):
            raise PinError("Pin a number: that field is a category, not a measure")
        helper = next(
            (
                m
                for m in report.measures
                if wanted
                in {
                    _fold(str(m.get("Name") or "")),
                    _fold(f"{m.get('TableName')}.{m.get('Name')}"),
                }
            ),
            None,
        )
        if helper is None:
            raise PinError(f"The model has no measure {measure!r}")
        name = str(helper.get("Name"))
        return {
            "kind": "measure",
            "table": str(helper.get("TableName") or ""),
            "name": name,
            "label": name,
            "hidden": True,
        }
    shown = [f for f in measures if not f.get("hidden")]
    if len(shown) == 1:
        return shown[0]
    if not shown:
        raise PinError("This visual shows no measure to pin")
    names = ", ".join(f"[{f['name']}]" for f in shown[:6])
    raise PinError(f"Say which number to pin: this visual shows {names}")


def _date_columns(report: ImportedReport) -> set[tuple[str, str]]:
    return {
        (str(t.get("TableName")), str(t.get("ColumnName")))
        for t in report.tables
        if "datetime" in str(t.get("PandasDataType") or "")
    }


def _day(value: Value) -> Value:
    """A date category as the day a filter holds (the row carries ISO text)."""
    if value is None:
        return None
    match = _ISO_DAY.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise PinError("Studio can pin a date category only when it is a whole day")
    return "-".join(match.groups())


def _filter(
    table: str,
    column: str,
    op: Literal["in", "not_in", "between"],
    values: list[Value],
    *,
    date: bool = False,
    point: bool = False,
    measure: bool = False,
) -> TestFilter:
    try:
        return TestFilter(
            table=table,
            column=column,
            op=op,
            values=values,
            date=date,
            point=point,
            measure=measure,
        )
    except ValidationError as error:
        raise PinError(f"This filter cannot be held by a test ({_first_error(error)})") from error


def _default_name(
    visual: Any, field: dict[str, Any], fields: list[dict[str, Any]], pinned: list[TestFilter]
) -> str:
    """The card's caption for its own number; else the field's label (a header can be blank:
    the Agents table's are spaces), with the point's categories: "Total MTD Sales for Others"."""
    shown = [f for f in fields if f["kind"] == "measure" and not f.get("hidden")]
    title = str(getattr(visual, "caption", None) or getattr(visual, "title", "") or "").strip()
    label = str(field.get("label") or "").strip() or str(field["name"])
    if not pinned and len(shown) == 1 and field is shown[0] and title:
        return title
    if pinned:
        return f"{label} for {', '.join(_value_text(f.values[0]) for f in pinned)}"
    return label


def _structured(condition: str, is_measure: bool, dated: set[tuple[str, str]]) -> list[TestFilter]:
    """A filter the renderer compiled (``live_report.Compiler``: ``IN`` lists, comparisons,
    ``&&``, ``||`` and ``NOT``) as the test's own conditions; ``PinError`` for one a test cannot
    hold (an ``||`` across values, a strict comparison)."""
    try:
        node = parse_expression(condition)
    except DaxParseError as error:
        raise PinError("A filter of this visual cannot be held by a test") from error
    found: list[TestFilter] = []
    bounds: dict[tuple[str, str], list[Value]] = {}
    for part in _conjuncts(node):
        negated = False
        if isinstance(part, Call) and part.name == "NOT" and len(part.args) == 1:
            part, negated = part.args[0], True
        elif isinstance(part, Unary) and part.op == "NOT":
            part, negated = part.operand, True
        if not isinstance(part, Binary) or not isinstance(part.left, ColRef):
            raise PinError("A filter of this visual combines conditions a test cannot hold")
        ref = part.left
        table = ref.table or ""
        is_date = (table, ref.column) in dated
        if part.op == "IN" and isinstance(part.right, TableCtor):
            values = [_literal_value(row[0]) for row in part.right.rows if len(row) == 1]
            if len(values) != len(part.right.rows):
                raise PinError("A filter of this visual lists values a test cannot hold")
            op: Literal["in", "not_in"] = "not_in" if negated else "in"
            found.append(_filter(table, ref.column, op, values, date=is_date, measure=is_measure))
        elif part.op in {"=", "=="}:
            op = "not_in" if negated else "in"
            found.append(
                _filter(
                    table,
                    ref.column,
                    op,
                    [_literal_value(part.right)],
                    date=is_date,
                    measure=is_measure,
                )
            )
        elif part.op in {">=", "<="} and not negated:
            pair = bounds.setdefault((table, ref.column), [None, None])
            pair[0 if part.op == ">=" else 1] = _literal_value(part.right)
        else:
            raise PinError(
                "A filter of this visual uses a condition a test cannot hold (only lists of "
                "values and ranges that include their ends)"
            )
    for (table, column), pair in bounds.items():
        found.append(
            _filter(
                table,
                column,
                "between",
                pair,
                date=(table, column) in dated,
                measure=is_measure,
            )
        )
    return found


def _conjuncts(node: Node) -> list[Node]:
    if isinstance(node, Binary) and node.op == "&&":
        return _conjuncts(node.left) + _conjuncts(node.right)
    return [node]


def _literal_value(node: Node) -> Value:
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Unary) and node.op == "-" and isinstance(node.operand, Num):
        return -node.operand.value
    if isinstance(node, Str):
        return node.value
    if isinstance(node, Call) and not node.args and node.name in {"BLANK", "TRUE", "FALSE"}:
        return {"BLANK": None, "TRUE": True, "FALSE": False}[node.name]
    if isinstance(node, Call) and node.name == "DATE" and len(node.args) == 3:
        parts = [n.value for n in node.args if isinstance(n, Num)]
        if len(parts) == 3:
            return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    raise PinError("A filter of this visual uses a value a test cannot hold")


# --- proposing tests ------------------------------------------------------------------------


def suggest_tests(
    document: PbirDocument | None,
    payloads: Sequence[Mapping[str, Any]],
    *,
    report: ImportedReport,
    limit: int = MAX_SUGGESTED,
    now: datetime | None = None,
) -> list[TestDefinition]:
    """Tests worth pinning, proposed and never stored: each card's number (big cards first),
    then the numbers its reference labels show (a fixed target is left out: it cannot
    change), then, for a report with few cards, the first data point of each chart.

    ``payloads`` are page responses of ``live_report.render_page`` (the values the user sees).
    A visual Studio could not draw, a hidden one or a blank number is skipped."""
    constants = _constant_measures(report)
    main: list[_Candidate] = []
    references: list[_Candidate] = []
    charts: list[_Candidate] = []
    for payload in payloads:
        page = payload.get("page")
        page_id = page.get("id") if isinstance(page, Mapping) else None
        visuals = payload.get("visuals")
        if not isinstance(page_id, str) or not isinstance(visuals, list):
            continue
        for visual in visuals:
            if not _drawn(visual):
                continue
            columns, rows = visual["columns"], visual["rows"]
            first = next(
                (
                    i
                    for i, c in enumerate(columns)
                    if c.get("kind") == "measure" and not c.get("hidden")
                ),
                None,
            )
            if visual.get("kind") == "card" and len(rows) == 1:
                if first is not None:
                    main.append(_Candidate(page_id, visual, first, rows[0]))
                for index, label in _reference_labels(visual):
                    if _fold(str(columns[index].get("name"))) not in constants:
                        references.append(_Candidate(page_id, visual, index, rows[0], label))
            elif visual.get("kind") in _CHART_KINDS and first is not None and rows:
                charts.append(_Candidate(page_id, visual, first, rows[0]))
    suggestions: list[TestDefinition] = []
    plans: dict[str, _PagePlan] = {}

    def offer(candidates: list[_Candidate], until: int) -> None:
        for candidate in sorted(candidates, key=_Candidate.order):
            if len(suggestions) >= until:
                return
            test = _suggestion(document, report, candidate, plans, now)
            if test is not None and all(s.id != test.id for s in suggestions):
                suggestions.append(test)

    offer(main, limit)
    offer(references, limit)
    if len(suggestions) < MIN_SUGGESTED:
        offer(charts, min(limit, MIN_SUGGESTED))
    return suggestions


_CHART_KINDS = frozenset({"column", "bar", "line", "area", "pie", "donut", "table"})


@dataclass(frozen=True)
class _Candidate:
    """A number of a rendered visual that could become a test: ``row[index]``."""

    page: str
    visual: Mapping[str, Any]
    index: int
    row: Any
    label: str = ""

    def order(self) -> tuple[float, float, float]:
        """Big visuals first (the headline cards), then top to bottom, left to right."""
        box = self.visual.get("position")
        box = box if isinstance(box, Mapping) else {}
        return (-_size(box, "width") * _size(box, "height"), _size(box, "y"), _size(box, "x"))


def _size(box: Mapping[str, Any], key: str) -> float:
    value = box.get(key)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def _drawn(visual: Any) -> bool:
    return (
        isinstance(visual, Mapping)
        and not visual.get("error")
        and not visual.get("hidden")
        and visual.get("fidelity") != "none"
        and isinstance(visual.get("columns"), list)
        and isinstance(visual.get("rows"), list)
        and isinstance(visual.get("id"), str)
    )


def _reference_labels(visual: Mapping[str, Any]) -> list[tuple[int, str]]:
    """(column index, label) of a card's reference labels whose value is a measure."""
    formatting = visual.get("formatting")
    refs = formatting.get("references") if isinstance(formatting, Mapping) else None
    columns = visual["columns"]
    found: list[tuple[int, str]] = []
    for ref in refs if isinstance(refs, list) else []:
        index = ref.get("index") if isinstance(ref, Mapping) else None
        if (
            isinstance(index, int)
            and 0 <= index < len(columns)
            and columns[index].get("kind") == "measure"
        ):
            found.append((index, str(ref.get("label") or "").strip().rstrip(":").strip()))
    return found


def _suggestion(
    document: PbirDocument | None,
    report: ImportedReport,
    candidate: _Candidate,
    plans: dict[str, _PagePlan],
    now: datetime | None,
) -> TestDefinition | None:
    visual, row, index = candidate.visual, candidate.row, candidate.index
    if not isinstance(row, list) or index >= len(row):
        return None
    value = row[index]
    if value is None or (isinstance(value, str) and (not value.strip() or "data:" in value[:5])):
        return None  # a blank, or a picture a measure draws
    columns = visual["columns"]
    point = {
        str(c.get("query_ref") or f"{c.get('table')}.{c.get('name')}"): row[i]
        for i, c in enumerate(columns)
        if c.get("kind") == "column" and i < len(row)
    }
    title = str(visual.get("title") or "").strip()
    name = f"{title}, {candidate.label}" if candidate.label and title else None
    column = columns[index]
    try:
        return _pin(
            document,
            _page_plan(report, candidate.page, plans),
            str(visual["id"]),
            point,
            value=_cell(value),
            measure=str(column.get("query_ref") or column.get("name")),
            name=name,
            now=now,
        )
    except PinError:
        return None


def _constant_measures(report: ImportedReport) -> set[str]:
    """Measures whose formula is a number (a target): a test of them tests nothing."""
    found: set[str] = set()
    for measure in report.measures:
        text = str(measure.get("Expression") or "")
        try:
            node = parse_expression(text)
        except DaxParseError:
            continue
        if isinstance(node, Unary) and node.op in {"-", "+"}:
            node = node.operand
        if isinstance(node, Num | Str):
            found.add(_fold(str(measure.get("Name") or "")))
    return found


# --- edits: the model as an edit would leave it, and names that move ---------------------------


def projected_report(report: ImportedReport, operations: Iterable[Any]) -> ImportedReport | None:
    """The report as its measures would be after ``operations``, to check tests before Power
    BI Desktop is opened for the edit. Report-layer operations change no number and are
    ignored. None when an operation changes more of the model than its measures (a column,
    table, relationship...): only the model Power BI saves can say what that does."""
    measures = [dict(m) for m in report.measures]

    def find(name: str) -> int | None:
        return next(
            (i for i, m in enumerate(measures) if _fold(str(m.get("Name") or "")) == _fold(name)),
            None,
        )

    for op in operations:
        if isinstance(op, MODEL_FAMILY_OPERATIONS):
            return None
        if isinstance(op, CreateMeasure):
            measures.append({"TableName": op.table, "Name": op.name, "Expression": op.expression})
        elif isinstance(op, UpdateMeasure):
            at = find(op.name)
            if at is not None and op.expression is not None:
                measures[at]["Expression"] = op.expression
        elif isinstance(op, RenameMeasure):
            at = find(op.name)
            if at is not None:
                measures[at]["Name"] = op.new_name
        elif isinstance(op, DeleteMeasure):
            at = find(op.name)
            if at is not None:
                del measures[at]
    return report.model_copy(update={"measures": measures})


def follow_renames(
    tests: Sequence[TestDefinition], operations: Iterable[Any]
) -> list[TestDefinition]:
    """The tests with the names a batch renames (measures, columns, tables) written the new
    way: a rename is a refactor, and the numbers it keeps must keep passing. Ids stay, so a
    run before the edit and a run after it compare test by test."""
    updated = list(tests)
    for op in operations:
        updated = [_renamed(test, op) for test in updated]
    return updated


def _renamed(test: TestDefinition, op: Any) -> TestDefinition:
    measure, expression, filters = test.measure, test.expression, list(test.filters)
    if isinstance(op, RenameMeasure):
        if measure is not None and _fold(measure.name) == _fold(op.name):
            measure = measure.model_copy(update={"name": op.new_name})
        if expression is not None:
            expression = rename_dax_references(expression, "", op.name, op.new_name)[0]
        filters = [
            f.model_copy(update={"column": op.new_name})
            if f.measure and _fold(f.column) == _fold(op.name)
            else f
            for f in filters
        ]
    elif isinstance(op, RenameColumn):
        filters = [
            f.model_copy(update={"column": op.new_name})
            if not f.measure
            and _fold(f.table) == _fold(op.table)
            and _fold(f.column) == _fold(op.name)
            else f
            for f in filters
        ]
        if expression is not None:
            expression = rename_dax_column(expression, op.table, op.name, op.new_name)[0]
    elif isinstance(op, RenameTable):
        if measure is not None and _fold(measure.table) == _fold(op.name):
            measure = measure.model_copy(update={"table": op.new_name})
        filters = [
            f.model_copy(update={"table": op.new_name}) if _fold(f.table) == _fold(op.name) else f
            for f in filters
        ]
        if expression is not None:
            expression = rename_dax_table(expression, op.name, op.new_name)[0]
    else:
        return test
    if (measure, expression, filters) == (test.measure, test.expression, list(test.filters)):
        return test
    return TestDefinition.model_validate(
        {
            **test.model_dump(),
            "measure": measure.model_dump() if measure is not None else None,
            "expression": expression,
            "filters": [f.model_dump() for f in filters],
        }
    )


def new_user_test(
    name: str,
    *,
    expect: Expectation,
    measure: MeasureRef | None = None,
    expression: str | None = None,
    filters: Sequence[TestFilter] = (),
    fmt: str | None = None,
    now: datetime | None = None,
) -> TestDefinition:
    """A test the user wrote (a measure or a DAX expression, filters and an expectation).
    Raises ``pydantic.ValidationError`` (a ValueError) for an invalid one."""
    return TestDefinition(
        id=definition_id(measure, expression, filters),
        name=name,
        measure=measure,
        expression=expression,
        filters=list(filters),
        expect=expect,
        created_at=now_text(now),
        source="user",
        format=fmt,
    )
