"""An imported report's model in the local DAX engine, and the engine's query surface.

The model is the imported snapshot's tables, the author's relationships and measures.
It is built once per snapshot and kept for the next question: loading the Agents
Performance model takes a tenth of a second, its dashboard page 3.6 s (Power BI
Desktop: 5.3 s), with every value equal to Desktop's.

A model the engine cannot reproduce exactly (a two-way or many-to-many relationship,
more rows than it should hold in memory) raises ``DaxUnsupported`` naming why, so
callers fall back to Power BI Desktop instead of showing a number that could differ.

Once the head's model facts are known (``ImportedReport.model``), the model follows the head,
not the import: renamed tables and columns take their new names, deleted ones are gone, a
calculated column or table the rows do not reflect (new, or its DAX or what it reads changed
since the rows were decoded) is calculated here when the engine can do it exactly, and is
otherwise *unavailable*: a query that touches it raises ``DaxUnsupported`` naming it, so its
visuals need Power BI instead of showing an error or a stale number. Calculation groups and
field parameters are unavailable too (the engine applies neither), and a data table or column
the rows lack (added in Power Query after the data was read) waits for the data to be read again.
"""

from __future__ import annotations

import math
import re
import time
import zipfile
from collections import OrderedDict
from collections.abc import Iterable
from datetime import datetime
from threading import Lock
from typing import Any

import pandas as pd

from . import model_facts
from .contracts import DatasetSnapshot, DaxEvaluateRequest, DaxResult
from .dax_engine import (
    EMPTY,
    DaxEngineError,
    DaxEngineLimit,
    DaxModelIncomplete,
    DaxUnsupported,
    Engine,
    EngineColumn,
    EngineMeasure,
    EngineRelationship,
    EngineTable,
    Env,
    Model,
    QueryResult,
    _column_from_series,
    _key,
    run_query,
)
from .dax_syntax import (
    Binary,
    Call,
    ColRef,
    Name,
    Node,
    Str,
    TableCtor,
    Unary,
    VarBlock,
    is_query,
    parse_expression,
    parse_query,
)
from .report_import import (
    ImportedReport,
    model_fingerprint,
    model_key,
    relationships_complete,
    report_relationships,
)

# Rows the engine holds in memory for one report. Business laptops run it next to the
# model and other apps; a larger report stays with Power BI Desktop.
MAX_ROWS = 5_000_000
ENGINE_LABEL = "Studio's DAX engine"

# Models kept for undo and redo to alternate between. Each holds its own copy of the
# tables, so a big report keeps fewer: the total stays within twice one report's limit.
MAX_CACHED_MODELS = 4
MAX_CACHED_ROWS = 2 * MAX_ROWS

_models: OrderedDict[tuple[str, str, str], Model] = OrderedDict()
_lock = Lock()


def _rows(model: Model) -> int:
    return sum(table.rows for table in model.tables.values())


def report_model(report: ImportedReport, snapshot: DatasetSnapshot) -> Model:
    """The report's model for the local engine, built once per distinct model.

    Keyed by what the answers depend on (``model_fingerprint``: measures, schema,
    relationships, snapshot), not by the file: a formatting edit, an undo of one or a page
    reorder writes a new head but the same model, and must not pay a rebuild. base_hash
    never moves for an imported report, so the old key also kept serving a model whose
    measures had since changed.
    """
    key = (report.id, model_fingerprint(report), snapshot.id)
    with _lock:
        if key in _models:
            _models.move_to_end(key)
            return _models[key]
    model = _build(report, snapshot)
    with _lock:
        _models[key] = model
        _models.move_to_end(key)
        while len(_models) > 1 and (
            len(_models) > MAX_CACHED_MODELS
            or sum(_rows(m) for m in _models.values()) > MAX_CACHED_ROWS
        ):
            _models.popitem(last=False)
    return model


class DaxUnavailable(DaxUnsupported):
    """The question reads something the local model could not build from the imported rows
    (``unavailable_reference``): only Power BI can answer it. Never answered from the rows
    another way (the lab subset would read the same stale rows)."""


class ReportModel(Model):
    """A report's model built from its head's facts, with what the engine could not build.

    ``unavailable`` maps ``(table key, column key)`` (``None`` for a whole table) to why, in
    words for the user, and ``unavailable_measures`` a measure key to why (Power BI reports an
    error in it); ``computed`` names the calculated columns and tables the engine calculated
    itself because the imported rows do not reflect them.
    """

    def __init__(
        self,
        tables: Iterable[EngineTable],
        relationships: Iterable[EngineRelationship],
        measures: Iterable[EngineMeasure],
        *,
        unavailable: dict[tuple[str, str | None], str] | None = None,
        unavailable_measures: dict[str, str] | None = None,
        computed: list[str] | None = None,
    ) -> None:
        super().__init__(tables, relationships, measures)
        self.unavailable = dict(unavailable or {})
        self.unavailable_measures = dict(unavailable_measures or {})
        self.computed = list(computed or [])


def _build(report: ImportedReport, snapshot: DatasetSnapshot) -> Model:
    """The head's known model, or the saved snapshot of a genuinely legacy import.

    An attempted current read with unknown facts is unavailable. Legacy snapshot evaluation
    is also refused when its digest identifies a different model from the head.
    """
    facts = getattr(report, "model", None)  # a report-like object may predate the field
    if model_facts.known(facts):
        assert facts is not None
        return _HeadModel(report, snapshot, facts).build()
    if getattr(report, "model_read_version", 0) >= 1:
        raise DaxUnavailable(
            "Studio could not read the current model's metadata, so saved rows cannot verify "
            "its storage modes or calculations. Use Power BI Desktop, or retry reading the "
            "model in Studio before calculating from saved rows."
        )
    digest = getattr(report, "snapshot_model", None)
    if isinstance(digest, dict):
        try:
            head = model_key(report)
        except (OSError, ValueError, zipfile.BadZipFile):
            head = None
        if head != digest.get("model_key"):
            raise DaxModelIncomplete(
                "Studio could not read the model Power BI Desktop saved for this report, so it "
                "cannot tell what changed since it read the data; Power BI Desktop calculates "
                "this report until Studio reads the model again."
            )
    return _snapshot_model(report, snapshot, digest if isinstance(digest, dict) else None)


def _check_rows(snapshot: DatasetSnapshot) -> None:
    total = sum(item.table.row_count for item in snapshot.tables)
    if total > MAX_ROWS:
        raise DaxEngineLimit(
            f"This report has {total:,} rows, more than Studio's engine holds in memory "
            f"({MAX_ROWS:,})."
        )


def _snapshot_model(
    report: ImportedReport, snapshot: DatasetSnapshot, digest: dict[str, Any] | None = None
) -> Model:
    """The model as the snapshot has it: for a report whose head facts are unknown. With the
    snapshot's own digest known (and the head's model the same), its calculation groups and
    field parameters stay Power BI's."""
    if not relationships_complete(report, snapshot):
        # Without all of the author's joins a filter would silently not reach the fact
        # table and every total would be the unfiltered one.
        raise DaxModelIncomplete(
            "The imported copy may lack some of this report's relationships; re-import the "
            "report or use Power BI Desktop."
        )
    _check_rows(snapshot)
    tables = []
    for item in snapshot.tables:
        frame = pd.read_parquet(item.normalized_parquet_path)
        tables.append(
            EngineTable(
                item.table.name,
                {_key(c): _column_from_series(c, frame[c]) for c in frame.columns},
                len(frame),
            )
        )
    names = {_key(t.name) for t in tables}
    relationships = []
    for raw in report_relationships(report)[0]:
        found = _relationship(raw, lambda table, _column: table in names)
        if found is not None:
            relationships.append(found)
    if digest is None:
        return Model(tables, relationships, _measures(report))
    unavailable: dict[tuple[str, str | None], str] = {}
    for entry in digest.get("tables") or []:
        name = str(entry.get("name") or "") if isinstance(entry, dict) else ""
        if not name:
            continue
        if entry.get("source") == "calculationGroup":
            unavailable[(_key(name), None)] = _CALCULATION_GROUP.format(name=name)
        elif _NAMEOF.search(str(entry.get("expression") or "")):
            unavailable[(_key(name), None)] = _FIELD_PARAMETER.format(name=name)
    kept = [t for t in tables if (_key(t.name), None) not in unavailable]
    return ReportModel(kept, relationships, _measures(report), unavailable=unavailable)


_CALCULATION_GROUP = (
    "'{name}' is a calculation group, which Studio's canvas does not apply; Power BI shows "
    "visuals that use it."
)
_FIELD_PARAMETER = (
    "'{name}' is a field parameter, which Studio's canvas does not switch; Power BI shows "
    "visuals that use it."
)
_NAMEOF = re.compile(r"\bNAMEOF\s*\(", re.IGNORECASE)


def _measures(report: ImportedReport) -> list[EngineMeasure]:
    return [
        EngineMeasure(str(m["Name"]), str(m.get("TableName", "")), str(m["Expression"]))
        for m in report.measures
        if m.get("Name") and m.get("Expression")
    ]


def _ends(raw: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (
            _key(str(raw.get(f"{side}TableName", ""))),
            _key(str(raw.get(f"{side}ColumnName", ""))),
        )
        for side in ("From", "To")
    ]


def _relationship(raw: dict[str, Any], present: Any) -> EngineRelationship | None:
    """One author relationship for the engine; None when ``present(table, column)`` says an
    end is missing, ``DaxUnsupported`` for a kind the engine cannot evaluate exactly."""
    shape = str(raw.get("Cardinality", "")).upper()
    ends = _ends(raw)
    if not all(present(table, column) for table, column in ends):
        return None
    active = bool(raw.get("IsActive", 1))
    both = str(raw.get("CrossFilteringBehavior", "")).casefold() in {"both", "2"}
    if active and (both or shape in {"M:M", "1:1"}):
        kind = "two-way" if both else ("many-to-many" if shape == "M:M" else "one-to-one")
        raise DaxUnsupported(
            f"The {kind} relationship between '{raw.get('FromTableName')}' and "
            f"'{raw.get('ToTableName')}' needs Power BI Desktop to calculate exactly."
        )
    if shape == "1:M":
        ends.reverse()
    (many, many_column), (one, one_column) = ends
    return EngineRelationship(many, many_column, one, one_column, active)


# Time the engine may spend, when it builds a report's model, calculating the calculated columns
# and tables the imported rows do not reflect: in all, and for one of them.
CALCULATION_BUDGET = 30.0
ONE_CALCULATION_BUDGET = 10.0
# Functions through which a calculated column or table reads filters or relationships: when the
# relationships changed since the rows were decoded, the rows' values may no longer be right.
# VALUES, ALL and the like count too: a new join can give the one side a blank row (a sale of an
# unknown store), which they return.
_RELATIONSHIP_FUNCTIONS = frozenset(
    {
        "RELATED",
        "RELATEDTABLE",
        "CALCULATE",
        "CALCULATETABLE",
        "USERELATIONSHIP",
        "CROSSFILTER",
        "SUMMARIZE",
        "SUMMARIZECOLUMNS",
        "VALUES",
        "ALL",
        "DISTINCT",
        "ALLSELECTED",
        "ALLEXCEPT",
        "COUNTBLANK",
        "HASONEVALUE",
        "SELECTEDVALUE",
        "FILTERS",
        "ISFILTERED",
        "ISCROSSFILTERED",
        "LOOKUPVALUE",
    }
)
# Results Power BI fixed when it calculated the column (the refresh's clock, random numbers, the
# signed-in user): Studio's own would differ, so it never calculates them.
_VOLATILE_FUNCTIONS = frozenset(
    {
        "TODAY",
        "NOW",
        "UTCNOW",
        "UTCTODAY",
        "RAND",
        "RANDBETWEEN",
        "USERNAME",
        "USERPRINCIPALNAME",
        "USEROBJECTID",
        "USERCULTURE",
        "CUSTOMDATA",
    }
)
# Words DAX reads as types, not tables (DATATABLE's column types).
_TYPE_WORDS = frozenset(
    {"STRING", "INTEGER", "DOUBLE", "CURRENCY", "BOOLEAN", "DATETIME", "DECIMAL", "INT64"}
)
# Studio's FORMAT and text conversions follow en-US; a model of another culture formats
# numbers and dates its own way (month names, separators).
_CULTURE_FUNCTIONS = frozenset({"FORMAT", "VALUE", "DATEVALUE", "TIMEVALUE"})
_STUDIO_CULTURE = "en-us"
# A partition read live from its source (DirectQuery) or both ways (dual) is not in the rows.
_LIVE_MODES = {
    "direct_query": "is read live from its source (DirectQuery)",
    "dual": "switches between its stored copy and its live source (dual mode)",
    "direct_lake": "uses Direct Lake storage, which Power BI loads from the lake",
    "hybrid": "combines cached Import and DirectQuery partitions (hybrid mode)",
    "unknown": (
        "has unknown or incomplete storage metadata; cached rows cannot establish its full data"
    ),
}
# Context transition turns a row into filters on every column of it. The engine filters that one
# row; Power BI filters every row with the same values, so on a table with repeated rows the two
# differ and the engine refuses.
_TRANSITION_FUNCTIONS = frozenset({"CALCULATE", "CALCULATETABLE", "RELATEDTABLE"})
_CALCULATION_ERRORS = (DaxEngineError, ValueError, ArithmeticError, LookupError, TypeError)


def _all_nodes(node: Node) -> list[Node]:
    found: list[Node] = [node]
    if isinstance(node, Call):
        for arg in node.args:
            found.extend(_all_nodes(arg))
    elif isinstance(node, Binary):
        found.extend(_all_nodes(node.left) + _all_nodes(node.right))
    elif isinstance(node, Unary):
        found.extend(_all_nodes(node.operand))
    elif isinstance(node, VarBlock):
        for _, value in node.variables:
            found.extend(_all_nodes(value))
        found.extend(_all_nodes(node.body))
    elif isinstance(node, TableCtor):
        for row in node.rows:
            for value in row:
                found.extend(_all_nodes(value))
    return found


def _short(error: BaseException) -> str:
    text = " ".join(str(error).split()) or type(error).__name__
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return first if len(first) <= 160 else first[:157] + "..."


def _label_key(label: str) -> tuple[str, str] | None:
    """``'Table'[Column]``, ``Table[Column]`` or ``[Column]`` as folded (table, column)."""
    text = label.strip()
    if not text.endswith("]") or "[" not in text:
        return None
    at = text.index("[")
    table, column = text[:at].strip(), text[at + 1 : -1].replace("]]", "]")
    if len(table) >= 2 and table.startswith("'") and table.endswith("'"):
        table = table[1:-1].replace("''", "'")
    return _key(table), _key(column)


class _HeadModel:
    """Builds a ``ReportModel`` that follows the head's facts (see the module docstring)."""

    def __init__(
        self, report: ImportedReport, snapshot: DatasetSnapshot, facts: dict[str, Any]
    ) -> None:
        self.report, self.snapshot, self.facts = report, snapshot, facts
        self.digest = report.snapshot_model
        self.unavailable: dict[tuple[str, str | None], str] = {}
        self.computed: list[str] = []
        self.deadline = time.monotonic() + CALCULATION_BUDGET
        self.head_tables = [t for t in facts.get("tables", []) if isinstance(t, dict)]
        self.columns: dict[str, list[dict[str, Any]]] = {}
        for column in facts.get("columns", []):
            if isinstance(column, dict) and column.get("table") and column.get("name"):
                self.columns.setdefault(_key(str(column["table"])), []).append(column)
        self.by_table = {_key(str(t.get("name", ""))): t for t in self.head_tables}
        self.measures = _measures(report)
        self.measure_nodes: dict[str, Node | None] = {}
        self.head_hashes = model_facts.measure_hashes(report.measures)
        relationships, _ = report_relationships(report)
        self.relationships = relationships
        self.joins_hash_changed = (
            self.digest is not None
            and self.digest.get("relationships") is not None
            and model_facts.relationships_hash(relationships) != self.digest.get("relationships")
        )
        self.joins_changed: bool | None = None
        self.changed: dict[tuple[str, ...], bool] = {}
        power_bi_only = model_facts.power_bi_only(facts) or {}
        self.field_parameters = {
            _key(t) for t, kind in power_bi_only.items() if kind == "fieldParameter"
        }
        self.calculation_groups = {
            _key(t) for t, kind in power_bi_only.items() if kind == "calculationGroup"
        }
        settings = facts.get("model") if isinstance(facts.get("model"), dict) else {}
        culture = settings.get("culture") if isinstance(settings, dict) else None
        self.culture = str(culture).casefold() if isinstance(culture, str) else None
        self.modes = {
            _key(table["name"]): table["mode"] for table in model_facts.table_storage_modes(facts)
        }
        self.unavailable_measures: dict[str, str] = {}
        for measure in facts.get("measures") or []:
            if isinstance(measure, dict) and (measure.get("error") or measure.get("state")):
                name = str(measure.get("name"))
                problem = measure.get("error") or "it needs to be calculated again"
                self.unavailable_measures[_key(name)] = (
                    f"Power BI reports a problem with the measure [{name}] ({problem}); Power "
                    "BI shows visuals that use it."
                )
        self.partition_problems: dict[str, str] = {}
        for partition in facts.get("partitions") or []:
            if isinstance(partition, dict) and (partition.get("error") or partition.get("state")):
                self.partition_problems.setdefault(
                    _key(str(partition.get("table"))),
                    str(partition.get("error") or partition.get("state")),
                )

    def _partition_problem(self, table: str) -> str | None:
        return self.partition_problems.get(table)

    @staticmethod
    def _column_problem(column: dict[str, Any]) -> str | None:
        """What Power BI says is wrong with a column (an error, or a state other than ready)."""
        if column.get("error"):
            return str(column["error"])
        if column.get("state"):
            return f"its state is {column['state']}"
        return None

    # --- the plan -------------------------------------------------------------------------

    def build(self) -> ReportModel:
        _check_rows(self.snapshot)
        for group in self.facts.get("calculationGroups") or []:
            if isinstance(group, dict) and group.get("selectionExpressions"):
                # A selection expression applies when no item is chosen: to every measure.
                raise DaxUnsupported(
                    f"The calculation group '{group.get('table')}' changes every measure when "
                    "none of its items is selected; Power BI Desktop calculates this report."
                )
        items = {_key(item.table.name): item for item in self.snapshot.tables}
        tables: dict[str, EngineTable] = {}
        pending_tables: list[dict[str, Any]] = []
        pending_columns: list[tuple[str, dict[str, Any]]] = []
        for table in self.head_tables:
            name = str(table.get("name") or "")
            key = _key(name)
            if not name:
                continue
            if table.get("calculationGroup") or key in self.calculation_groups:
                self._unavailable(name, None, _CALCULATION_GROUP.format(name=name))
                continue
            if key in self.field_parameters:
                self._unavailable(name, None, _FIELD_PARAMETER.format(name=name))
                continue
            mode = self.modes.get(key, "unknown")
            if mode in _LIVE_MODES:
                self._unavailable(
                    name, None, f"'{name}' {_LIVE_MODES[mode]}; Power BI shows visuals that use it."
                )
                continue
            problem = self._partition_problem(key)
            if table.get("calculated") and problem is not None:
                self._unavailable(
                    name,
                    None,
                    f"Power BI reports a problem with the calculated table '{name}' ({problem}); "
                    "Power BI shows visuals that use it.",
                )
                continue
            source = self._snapshot_name(table)
            item = items.get(_key(source)) if source is not None else None
            if table.get("calculated") and (item is None or self._table_changed(table)):
                pending_tables.append(table)
                pending_columns.extend(
                    (name, c) for c in self.columns.get(key, []) if c.get("kind") == "calculated"
                )
                continue
            if item is None:
                # A table of measures only has no rows to read (the snapshot never has it).
                if self.columns.get(key):
                    self._unavailable(
                        name,
                        None,
                        f"'{name}' was added or changed in the model after Studio read its "
                        "data; read the data again, or use Power BI.",
                    )
                continue
            tables[key] = self._from_snapshot(table, item, pending_columns)
        for name, column in list(pending_columns):
            problem = self._column_problem(column)
            if problem is not None:
                pending_columns.remove((name, column))
                self._unavailable(
                    name,
                    str(column["name"]),
                    f"Power BI reports a problem with the calculated column "
                    f"'{name}'[{column['name']}] ({problem}); Power BI shows visuals that use it.",
                )
        self._calculate(tables, pending_tables, pending_columns)
        return self._finish(tables)

    def _unavailable(self, table: str, column: str | None, reason: str) -> None:
        self.unavailable[(_key(table), _key(column) if column is not None else None)] = reason

    def _snapshot_name(self, table: dict[str, Any]) -> str | None:
        if self.digest is None:
            return str(table.get("name"))
        entry = model_facts.snapshot_table(self.digest, table)
        return str(entry.get("name")) if entry is not None else None

    def _from_snapshot(
        self,
        table: dict[str, Any],
        item: Any,
        pending: list[tuple[str, dict[str, Any]]],
    ) -> EngineTable:
        """The head table from the snapshot's rows: its columns under their head names, a
        column the head no longer has left out, a changed calculated column queued."""
        name = str(table["name"])
        frame = pd.read_parquet(item.normalized_parquet_path)
        stored = {_key(str(c)): str(c) for c in frame.columns}
        columns: dict[str, EngineColumn] = {}
        for column in self.columns.get(_key(name), []):
            head = str(column["name"])
            before = self._snapshot_column(table, column)
            present = stored.get(_key(before)) if before is not None else None
            if column.get("kind") == "calculated":
                if (
                    present is None
                    or self._column_problem(column) is not None
                    or self._column_changed(table, column)
                ):
                    pending.append((name, column))
                    continue
            elif present is None:
                self._unavailable(
                    name,
                    head,
                    f"'{name}'[{head}] was added or changed in the model after Studio read its "
                    "data; read the data again, or use Power BI.",
                )
                continue
            columns[_key(head)] = _column_from_series(head, frame[present])
        return EngineTable(name, columns, len(frame))

    def _snapshot_column(self, table: dict[str, Any], column: dict[str, Any]) -> str | None:
        if self.digest is None:
            return str(column.get("name"))
        entry = model_facts.snapshot_column(self.digest, table, column)
        return str(entry.get("name")) if entry is not None else None

    # --- what changed since the rows were decoded -----------------------------------------

    def _table_changed(self, table: dict[str, Any]) -> bool:
        if self.digest is None:
            # The rows' own model is unknown: nothing says their calculated values still hold.
            return True
        key = ("table", _key(str(table.get("name"))))
        if key not in self.changed:
            self.changed[key] = False  # a cycle reads as unchanged while it is resolved
            entry = model_facts.snapshot_table(self.digest, table)
            text = table.get("expression")
            self.changed[key] = (
                entry is None
                or entry.get("expression") != text
                or not isinstance(text, str)
                or self._reads_changed(text, None)
            )
        return self.changed[key]

    def _column_changed(self, table: dict[str, Any], column: dict[str, Any]) -> bool:
        if self.digest is None:
            return True
        key = ("column", _key(str(table.get("name"))), _key(str(column.get("name"))))
        if key not in self.changed:
            self.changed[key] = False
            entry = model_facts.snapshot_column(self.digest, table, column)
            text = column.get("expression")
            self.changed[key] = (
                entry is None
                or entry.get("expression") != text
                or not isinstance(text, str)
                or self._reads_changed(text, str(table.get("name")))
            )
        return self.changed[key]

    def _measure_node(self, name: str) -> Node | None:
        key = _key(name)
        if key not in self.measure_nodes:
            measure = next((m for m in self.measures if _key(m.name) == key), None)
            try:
                self.measure_nodes[key] = measure.node() if measure is not None else None
            except DaxEngineError:
                self.measure_nodes[key] = None
        return self.measure_nodes[key]

    def _reads_changed(
        self, text: str, home: str | None, seen: frozenset[str] = frozenset()
    ) -> bool:
        """Whether anything the DAX reads changed since the rows were decoded: a measure (its
        DAX, or what it reads), a calculated column or table, or, for DAX that reads filters
        or relationships, the relationships. ``home`` is the table a calculated column's bare
        ``[Column]`` belongs to."""
        assert self.digest is not None
        before = self.digest.get("measures")
        try:
            node = parse_expression(text)
        except ValueError:
            # Unreadable here: unchanged only if nothing it could read changed.
            changed = isinstance(before, dict) and any(
                before[k] != self.head_hashes.get(k) for k in before
            )
            return changed or self._joins_changed()
        nodes = _all_nodes(node)
        variables = {_key(n) for v in nodes if isinstance(v, VarBlock) for n, _ in v.variables}
        # Names the DAX gives columns it builds itself (ADDCOLUMNS "Year", ...), and the ones
        # GENERATESERIES, CALENDAR and table constructors give theirs.
        built = {_key(n.value) for n in nodes if isinstance(n, Str)} | {"value", "date"}
        uses_relationships = False
        for child in nodes:
            if isinstance(child, Call) and child.name in _RELATIONSHIP_FUNCTIONS:
                uses_relationships = True
            elif isinstance(child, Name):
                key = _key(child.name)
                if not child.name or key in variables:
                    continue
                table = self.by_table.get(key)
                if table is None:
                    if not child.quoted and child.name.upper() in _TYPE_WORDS:
                        continue  # DATATABLE("Kind", STRING, ...)
                    return True  # a table the model no longer has: Power BI shows an error
                if table.get("calculated") and self._table_changed(table):
                    return True
            elif isinstance(child, ColRef):
                if child.table == "":
                    continue  # ''[Value]: a column of a table the DAX writes itself
                column = self._column_facts(child, home)
                if column is not None:
                    owner = self.by_table.get(_key(str(column["table"])))
                    if (
                        column.get("kind") == "calculated"
                        and owner is not None
                        and self._column_changed(owner, column)
                    ):
                        return True
                    continue
                measure = _key(child.column)
                if self._measure_node(child.column) is not None:
                    if measure in seen:
                        continue
                    uses_relationships = True
                    if isinstance(before, dict) and before.get(measure) != self.head_hashes.get(
                        measure
                    ):
                        return True
                    found = next((m for m in self.measures if _key(m.name) == measure), None)
                    if found is not None and self._reads_changed(
                        found.text, None, seen | {measure}
                    ):
                        return True
                    continue
                if self._built_column(child, home, built):
                    continue
                # Neither a column nor a measure of the model: an input was deleted (Power BI
                # now reports an error), so the stored values are no longer what it shows.
                return True
        return uses_relationships and self._joins_changed()

    def _built_column(self, ref: ColRef, home: str | None, built: set[str]) -> bool:
        """Whether a reference that is no column or measure of the model is a column the DAX
        builds itself. Only a bare ``[Name]`` can be: a calculated column's belongs to its own
        table unless the DAX names it, and in a calculated table a bare name another table of
        the model has may be the row of a table being iterated."""
        if ref.table is not None:
            return False
        name = _key(ref.column)
        if name in built:
            return True
        if home is not None:
            return False
        return any(_key(str(c.get("name"))) == name for cs in self.columns.values() for c in cs)

    def _joins_changed(self) -> bool:
        """Whether the joins filters travel on changed since the rows were decoded: the joins
        themselves, or an end of an active one that is a calculated column or table Studio
        must calculate again (its keys are not the stored ones)."""
        if self.joins_changed is None:
            self.joins_changed = False  # a cycle reads as unchanged while it is resolved
            changed = self.joins_hash_changed
            for raw in self.relationships:
                if changed:
                    break
                if bool(raw.get("IsActive", 1)):
                    changed = any(self._end_changed(t, c) for t, c in _ends(raw))
            self.joins_changed = changed
        return self.joins_changed

    def _end_changed(self, table_key: str, column_key: str) -> bool:
        table = self.by_table.get(table_key)
        if table is None:
            return False
        if table.get("calculated") and self._table_changed(table):
            return True
        column = next(
            (c for c in self.columns.get(table_key, []) if _key(str(c.get("name"))) == column_key),
            None,
        )
        return (
            column is not None
            and column.get("kind") == "calculated"
            and self._column_changed(table, column)
        )

    def _column_facts(self, ref: ColRef, home: str | None = None) -> dict[str, Any] | None:
        """The head column a reference names; a bare ``[Column]`` is looked up in ``home``,
        where a calculated column reads the row it is calculated for (a measure of the same
        name wins, as in DAX)."""
        table = ref.table
        if table is None:
            if home is None or self._measure_node(ref.column) is not None:
                return None
            table = home
        return next(
            (
                c
                for c in self.columns.get(_key(table), [])
                if _key(str(c.get("name"))) == _key(ref.column)
            ),
            None,
        )

    # --- calculating what the rows do not reflect -------------------------------------------

    def _assemble(self, tables: dict[str, EngineTable]) -> ReportModel:
        def present(table: str, column: str) -> bool:
            return table in tables and column in tables[table].columns

        relationships = []
        for raw in self.relationships:
            found = _relationship(raw, present)
            if found is not None:
                relationships.append(found)
        return ReportModel(
            tables.values(),
            relationships,
            self.measures,
            unavailable=self.unavailable,
            unavailable_measures=self.unavailable_measures,
            computed=self.computed,
        )

    def _budget(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise DaxEngineLimit("Studio ran out of time calculating this report's model.")
        return min(ONE_CALCULATION_BUDGET, left)

    def _calculate(
        self,
        tables: dict[str, EngineTable],
        pending_tables: list[dict[str, Any]],
        pending_columns: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """Calculate what is pending, in passes until nothing more can be: a calculated column
        can read a calculated table, and the other way round.

        A relationship's own key columns come first. DAX that reads filters or relationships
        (a measure, RELATED, VALUES...) waits while an active relationship still lacks an end:
        calculated then, it would see the model without that join (a store's sales would be
        the grand total) and never be calculated again."""
        errors: dict[tuple[str, ...], BaseException] = {}
        ends = {end for raw in self.relationships if raw.get("IsActive", 1) for end in _ends(raw)}
        pending_columns.sort(key=lambda e: (_key(e[0]), _key(str(e[1]["name"]))) not in ends)
        progress = True
        while progress and (pending_tables or pending_columns):
            progress = False
            model = self._assemble(tables)
            for table in list(pending_tables):
                name = str(table["name"])
                waiting = self._waiting(table, None, tables)
                if waiting is not None:
                    errors[("table", _key(name))] = DaxUnsupported(waiting)
                    continue
                try:
                    tables[_key(name)] = self._calculate_table(model, table)
                except _CALCULATION_ERRORS as error:
                    errors[("table", _key(name))] = error
                    continue
                pending_tables.remove(table)
                self.computed.append(f"'{name}'")
                model = self._assemble(tables)
                progress = True
            for entry in list(pending_columns):
                name, column = entry
                if _key(name) not in tables:
                    continue
                waiting = self._waiting(column, name, tables)
                if waiting is not None:
                    errors[("column", _key(name), _key(str(column["name"])))] = DaxUnsupported(
                        waiting
                    )
                    continue
                try:
                    values = self._calculate_column(model, name, column)
                except _CALCULATION_ERRORS as error:
                    errors[("column", _key(name), _key(str(column["name"])))] = error
                    continue
                tables[_key(name)].columns[_key(str(column["name"]))] = values
                pending_columns.remove(entry)
                self.computed.append(f"'{name}'[{column['name']}]")
                model = self._assemble(tables)
                progress = True
        for table in pending_tables:
            name = str(table["name"])
            failure = errors.get(("table", _key(name)))
            why = f" ({_short(failure)})" if failure is not None else ""
            self._unavailable(
                name,
                None,
                f"Studio cannot calculate the calculated table '{name}'{why}; Power BI shows "
                "visuals that use it.",
            )
        for name, column in pending_columns:
            head = str(column["name"])
            failure = errors.get(("column", _key(name), _key(head)))
            why = f" ({_short(failure)})" if failure is not None else ""
            self._unavailable(
                name,
                head,
                f"Studio cannot calculate the calculated column '{name}'[{head}]{why}; Power BI "
                "shows visuals that use it.",
            )

    def _dax(self, item: dict[str, Any]) -> str:
        text = item.get("expression")
        if not isinstance(text, str) or not text.strip() or item.get("truncated"):
            raise DaxUnsupported("its DAX was not read in full")
        return text

    def _missing_join(self, tables: dict[str, EngineTable]) -> dict[str, Any] | None:
        for raw in self.relationships:
            if raw.get("IsActive", 1) and any(
                t not in tables or c not in tables[t].columns for t, c in _ends(raw)
            ):
                return raw
        return None

    def _waiting(
        self, item: dict[str, Any], home: str | None, tables: dict[str, EngineTable]
    ) -> str | None:
        """Why a calculation must wait: it reads filters or relationships while an active
        relationship still lacks an end."""
        missing = self._missing_join(tables)
        if missing is None:
            return None
        try:
            node = parse_expression(self._dax(item))
        except (ValueError, DaxEngineError):
            return None  # the calculation itself says what is wrong
        reads = any(
            (isinstance(n, Call) and n.name in _RELATIONSHIP_FUNCTIONS)
            or (
                isinstance(n, ColRef)
                and self._column_facts(n, home) is None
                and self._measure_node(n.column) is not None
            )
            for n in _all_nodes(node)
        )
        if not reads:
            return None
        return (
            "it reads through relationships, and the relationship between "
            f"'{missing.get('FromTableName')}' and '{missing.get('ToTableName')}' cannot be "
            "built from the imported data"
        )

    def _refuse_unstable(self, node: Node) -> None:
        """Refuse DAX whose result Power BI fixed with what Studio does not have: the clock at
        its refresh, random numbers, the signed-in user, or the model's culture."""
        for child in _all_nodes(node):
            if not isinstance(child, Call):
                continue
            if child.name in _VOLATILE_FUNCTIONS:
                raise DaxUnsupported(
                    f"it uses {child.name}, whose value Power BI fixed when it calculated it"
                )
            if child.name in _CULTURE_FUNCTIONS and self.culture != _STUDIO_CULTURE:
                locale = child.args[2] if child.name == "FORMAT" and len(child.args) > 2 else None
                if isinstance(locale, Str) and locale.value.casefold() == _STUDIO_CULTURE:
                    continue
                raise DaxUnsupported(
                    f"it uses {child.name}, whose result follows the model's culture "
                    f"({self.culture or 'unknown'}) and not Studio's"
                )

    def _calculate_table(self, model: Model, table: dict[str, Any]) -> EngineTable:
        name = str(table["name"])
        self._refuse_unstable(parse_expression(self._dax(table)))
        result = run_query(model, f"EVALUATE {self._dax(table)}", time_budget=self._budget())
        labels = [_label_key(label) for label in result.columns]
        columns: dict[str, EngineColumn] = {}
        for column in self.columns.get(_key(name), []):
            if column.get("kind") == "calculated":
                continue  # a calculated column of the table: calculated after it
            head = str(column["name"])
            wanted = _label_key(str(column.get("sourceColumn") or f"[{head}]"))
            index = next((i for i, label in enumerate(labels) if label == wanted), None)
            if index is None:
                # Desktop names a result column Table[Column] or [Name]; match the name alone
                # when that is unambiguous.
                bare = [i for i, label in enumerate(labels) if label and label[1] == _key(head)]
                index = bare[0] if len(bare) == 1 else None
            if index is None:
                raise DaxUnsupported(f"Studio's result has no column for [{head}]")
            columns[_key(head)] = _typed(column, [row[index] for row in result.rows])
        return EngineTable(name, columns, len(result.rows))

    def _calculate_column(self, model: Model, name: str, column: dict[str, Any]) -> EngineColumn:
        node = parse_expression(self._dax(column))
        self._refuse_unstable(node)
        engine = Engine(model, time_budget=self._budget())
        rows = engine.table(Name(name), EMPTY, Env())
        if self._transitions(node) and _repeats_rows(model.tables[_key(name)]):
            raise DaxUnsupported(
                f"it filters rows of '{name}', which has repeated rows Power BI filters together"
            )
        return _typed(column, engine.per_row(rows, node, EMPTY, Env()))

    def _transitions(self, node: Node) -> bool:
        for child in _all_nodes(node):
            if isinstance(child, Call) and child.name in _TRANSITION_FUNCTIONS:
                return True
            if (
                isinstance(child, ColRef)
                and self._column_facts(child) is None
                and self._measure_node(child.column) is not None
            ):
                return True
        return False

    def _finish(self, tables: dict[str, EngineTable]) -> ReportModel:
        dropped: set[int] = set()
        gone: set[str] = set()
        for index, raw in enumerate(self.relationships):
            ends = _ends(raw)
            if (
                bool(raw.get("IsActive", 1))
                and raw.get("JoinOnDateBehavior") == "datePartOnly"
                and all(t in tables and c in tables[t].columns for t, c in ends)
                and any(_has_times(tables[t].columns[c]) for t, c in ends)
            ):
                # Power BI matches such a join (auto date/time's) on the date alone; the engine
                # matches whole values, which agree only while every value is a midnight. The
                # date table leaves the model: only a question about it needs Power BI.
                name = str(raw.get("ToTableName"))
                column = f"'{raw.get('FromTableName')}'[{raw.get('FromColumnName')}]"
                self._unavailable(
                    name,
                    None,
                    f"'{name}' is joined to {column} on the date part only, and those dates "
                    "carry times of day; Power BI shows visuals that use it.",
                )
                tables.pop(_key(name), None)
                gone.add(_key(name))
                dropped.add(index)
        model = self._assemble(tables)
        for index, raw in enumerate(self.relationships):
            if not bool(raw.get("IsActive", 1)) or index in dropped:
                continue
            missing = [
                (table, column)
                for table, column in _ends(raw)
                if table not in tables or column not in tables[table].columns
            ]
            # A date table that left the model is only ever a join's one side: filters from
            # it are Power BI's alone, and nothing it holds reaches another table.
            if missing and not all(
                table in gone and table == _key(str(raw.get("ToTableName"))) for table, _ in missing
            ):
                # Filters would stop at the missing end and totals would be unfiltered.
                raise DaxModelIncomplete(
                    f"The relationship between '{raw.get('FromTableName')}' and "
                    f"'{raw.get('ToTableName')}' uses a table or column Studio cannot build "
                    "from the imported data; Power BI Desktop calculates this report."
                )
        return model


def _has_times(column: EngineColumn) -> bool:
    """Whether a column holds a date/time that is not a midnight (a text or number column, or
    one of dates, has none)."""
    if column.kind != "date":
        return False
    values = column.values[~pd.isna(column.values)]
    return bool(len(values)) and bool((values != values.astype("datetime64[D]")).any())


def _repeats_rows(table: EngineTable) -> bool:
    if table.rows < 2 or not table.columns:
        return False
    frame = pd.DataFrame(
        {key: pd.Series(column.values, dtype=object) for key, column in table.columns.items()}
    )
    return bool(frame.duplicated().any())


def _typed(column: dict[str, Any], values: list[Any]) -> EngineColumn:
    """A calculated result as the engine column of the model's data type; DaxUnsupported when
    a value does not fit it (the engine's result would not be the column Power BI stores)."""
    name = str(column["name"])
    code = column.get("dataType")
    present = [v for v in values if v is not None]

    def fits(kind: type | tuple[type, ...]) -> bool:
        return all(isinstance(v, kind) and not isinstance(v, bool) for v in present)

    if code is None:
        code = next(
            (
                inferred
                for inferred, kind in ((2, str), (9, datetime), (6, int), (8, (int, float)))
                if present and fits(kind)
            ),
            11 if present and all(isinstance(v, bool) for v in present) else None,
        )
        if code is None and present:
            raise DaxUnsupported(f"[{name}] mixes values of different types")
    if code == 2 and fits(str):
        series = pd.Series(values, dtype=object)
    elif code == 6 and fits((int, float)) and all(float(v).is_integer() for v in present):
        series = pd.Series([None if v is None else int(v) for v in values], dtype="Int64")
    elif code == 8 and fits((int, float)):
        series = pd.Series([None if v is None else float(v) for v in values], dtype="Float64")
    elif (
        code == 10
        and fits((int, float))
        and all(abs(v * 10_000 - round(v * 10_000)) < 1e-6 for v in present)
    ):
        # A fixed decimal column stores four decimal places; a result that needs rounding to
        # fit them is left to Power BI.
        series = pd.Series([None if v is None else float(v) for v in values], dtype="Float64")
    elif code == 9 and fits(datetime):
        series = pd.Series(pd.to_datetime(pd.Series(values, dtype=object)))
    elif code == 11 and all(isinstance(v, bool) for v in present):
        series = pd.Series(values, dtype="boolean")
    elif code is None:
        series = pd.Series(values, dtype=object)
    else:
        raise DaxUnsupported(
            f"[{name}] gives values that are not the column's data type as Power BI stores it"
        )
    return _column_from_series(name, series)


def unavailable_fields(model: Model) -> dict[tuple[str, str | None], str]:
    """What the model could not build: ``{(table key, column key or None): why}``, with the
    keys folded as ``dax_engine._key`` folds them. Empty for a model built from the snapshot
    alone. The live report marks a visual that uses one of these "needs Power BI"."""
    found = getattr(model, "unavailable", None)
    return dict(found) if isinstance(found, dict) else {}


def unavailable_reference(model: Model, query: str) -> str | None:
    """Why ``query`` cannot be answered by the local engine because it reads something the
    model could not build, or a measure Power BI reports a problem with (directly or through
    a measure), or None. A bare ``[Name]`` that is no measure is taken for the column of that
    name in any table (in a row context it is one): refusing too often is safe, a stale
    number is not."""
    unavailable = unavailable_fields(model)
    found = getattr(model, "unavailable_measures", None)
    broken: dict[str, str] = dict(found) if isinstance(found, dict) else {}
    if not unavailable and not broken:
        return None
    by_column = {column: why for (_, column), why in unavailable.items() if column is not None}
    try:
        parsed = parse_query(query) if is_query(query) else None
        roots: list[Node] = (
            [e.table for e in parsed.evaluates]
            + [expr for e in parsed.evaluates for expr, _ in e.order_by]
            + [m.expression for m in parsed.measures]
            + [value for _, value in parsed.variables]
            if parsed is not None
            else [parse_expression(query)]
        )
    except ValueError:
        return None
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        for node in _all_nodes(stack.pop()):
            if isinstance(node, Name):
                why = unavailable.get((_key(node.name), None))
                if why is not None:
                    return why
            elif isinstance(node, ColRef):
                measure = model.measures.get(_key(node.column))
                if node.table is not None:
                    table = _key(node.table)
                    why = unavailable.get((table, _key(node.column)))
                    # 'Table'[Measure] names a measure of that table, not a column of it.
                    home = measure is not None and _key(measure.table) == table
                    if why is None and not home:
                        why = unavailable.get((table, None))
                    if why is not None:
                        return why
                    if model.has_column(node.table, node.column):
                        continue
                if measure is not None:
                    why = broken.get(_key(measure.name))
                    if why is not None:
                        return why
                    if measure.name not in seen:
                        seen.add(measure.name)
                        try:
                            stack.append(measure.node())
                        except DaxEngineError:
                            continue
                elif node.table is None and _key(node.column) in by_column:
                    return by_column[_key(node.column)]
    return None


# What a measure may use to count as a plain aggregate the chat planner can filter by
# period and member: no time intelligence, no ALL or ranking that would change meaning
# under the planner's own filters.
_PLAIN = frozenset(
    {
        "SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "COUNTROWS", "DISTINCTCOUNT",
        "SUMX", "AVERAGEX", "MINX", "MAXX", "COUNTX", "FILTER", "DIVIDE", "ABS", "ROUND",
        "IF", "ISBLANK", "BLANK", "COALESCE", "TRUE", "FALSE",
    }
)  # fmt: skip


def measure_fact_table(model: Model, name: str) -> str | None:
    """The one model table a plain measure aggregates ([Profit] -> FactSales), or None."""
    from .dax_syntax import Binary, Call, ColRef, Name, Num, Str, Unary, VarBlock

    tables: set[str] = set()
    seen: set[str] = set()

    def walk(node: Node, variables: frozenset[str]) -> bool:
        if isinstance(node, Num | Str):
            return True
        if isinstance(node, ColRef):
            if node.table and model.has_column(node.table, node.column):
                tables.add(_key(node.table))
                return True
            measure = model.measures.get(_key(node.column))
            return measure is not None and follow(measure.name)
        if isinstance(node, Name):
            key = _key(node.name)
            if key in variables:
                return True
            if key in model.tables:
                tables.add(key)
                return True
            return False
        if isinstance(node, Binary):
            return node.op != "IN" and walk(node.left, variables) and walk(node.right, variables)
        if isinstance(node, Unary):
            return walk(node.operand, variables)
        if isinstance(node, VarBlock):
            inner = set(variables)
            for var, value in node.variables:
                if not walk(value, frozenset(inner)):
                    return False
                inner.add(_key(var))
            return walk(node.body, frozenset(inner))
        if isinstance(node, Call):
            return node.name in _PLAIN and all(walk(a, variables) for a in node.args)
        return False

    def follow(measure_name: str) -> bool:
        key = _key(measure_name)
        if key in seen:
            return True
        seen.add(key)
        measure = model.measures.get(key)
        if measure is None:
            return False
        try:
            return walk(measure.node(), frozenset())
        except DaxEngineLimit:
            return False
        except ValueError:
            return False

    if not follow(name) or len(tables) != 1:
        return None
    return model.tables[next(iter(tables))].name


class LocalReportEngine:
    """The live report's query engine without Power BI Desktop."""

    label = ENGINE_LABEL

    def __init__(self, model: Model, time_budget: float = 60.0) -> None:
        self.model, self.time_budget = model, time_budget

    def query(
        self, report: ImportedReport | None, query: str, max_rows: int = 101
    ) -> dict[str, Any]:
        unavailable = unavailable_reference(self.model, query)
        if unavailable is not None:
            raise DaxUnavailable(unavailable)
        result = run_query(self.model, query, time_budget=self.time_budget)
        return {
            "columns": result.columns,
            "rows": [dict(zip(result.columns, row, strict=True)) for row in result.rows[:max_rows]],
        }


def _cell(value: object) -> str | int | float | bool | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value  # type: ignore[return-value]


def _column_names(labels: list[str]) -> list[str]:
    """Result column names as DAX Lab shows them: "Value", "FirstName"; a model column
    keeps its table ("DimEmployee[FirstName]") only when two share a name."""
    bare = [label[label.index("[") + 1 : -1] if label.endswith("]") else label for label in labels]
    return [b if bare.count(b) == 1 else label for b, label in zip(bare, labels, strict=True)]


def unrelated_filter(model: Model, query: str) -> str | None:
    """Why a filter or grouping cannot change the result, or None.

    Power BI silently ignores a filter on a table with no relationship to the one being
    summed: CALCULATE([Total Sales], 'Date'[Year] = 2009) without the Date join returns
    every year's sales. The lab subset rejected that ("No active many-to-one
    relationship"), which gave the model a correction turn; this keeps it.
    """
    from .dax_syntax import Binary, Call, ColRef, Name, Unary, VarBlock, parse_query

    try:
        parsed = parse_query(query)
    except ValueError:
        return None
    measures = {name: m for name, m in model.measures.items()}
    aggregated: set[str] = set()
    referenced: set[str] = set()
    seen: set[str] = set()
    aggregates = {"SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "DISTINCTCOUNT", "COUNTBLANK"}
    iterators = {"SUMX", "AVERAGEX", "MINX", "MAXX", "COUNTX", "COUNTROWS", "FILTER"}

    def dependencies(node: Node, inside: bool = False) -> None:
        """Aggregated tables anywhere; tables read inside the measures it uses."""
        if isinstance(node, ColRef):
            if node.table and model.has_column(node.table, node.column):
                if inside:
                    referenced.add(_key(node.table))
            else:
                measure = measures.get(_key(node.column))
                if measure is not None and measure.name not in seen:
                    seen.add(measure.name)
                    try:
                        dependencies(measure.node(), True)
                    except DaxEngineError:
                        return
        elif isinstance(node, Name) and _key(node.name) in model.tables:
            if inside:
                referenced.add(_key(node.name))
        elif isinstance(node, Call):
            if node.name in aggregates and node.args and isinstance(node.args[0], ColRef):
                table = node.args[0].table
                if table and model.has_column(table, node.args[0].column):
                    aggregated.add(_key(table))
            first = node.args[0] if node.args else None
            if (
                node.name in iterators
                and isinstance(first, Name)
                and _key(first.name) in (model.tables)
            ):
                aggregated.add(_key(first.name))
            for arg in node.args:
                dependencies(arg, inside)
        elif isinstance(node, Binary):
            dependencies(node.left, inside)
            dependencies(node.right, inside)
        elif isinstance(node, Unary):
            dependencies(node.operand, inside)
        elif isinstance(node, VarBlock):
            for _, value in node.variables:
                dependencies(value, inside)
            dependencies(node.body, inside)

    filtered: list[tuple[str, str]] = []

    def filters(node: Node) -> None:
        """Boolean CALCULATE filters and SUMMARIZECOLUMNS groups of the query itself."""
        if isinstance(node, Call):
            if node.name in {"CALCULATE", "CALCULATETABLE"}:
                for arg in node.args[1:]:
                    columns = [
                        n
                        for n in _walk(arg)
                        if isinstance(n, ColRef) and n.table and model.has_column(n.table, n.column)
                    ]
                    if isinstance(arg, Binary) and columns:
                        filtered.append((_key(columns[0].table or ""), columns[0].column))
            if node.name == "SUMMARIZECOLUMNS":
                for arg in node.args:
                    if (
                        isinstance(arg, ColRef)
                        and arg.table
                        and model.has_column(arg.table, arg.column)
                    ):
                        filtered.append((_key(arg.table), arg.column))
            for arg in node.args:
                filters(arg)
        elif isinstance(node, Binary):
            filters(node.left)
            filters(node.right)
        elif isinstance(node, Unary):
            filters(node.operand)
        elif isinstance(node, VarBlock):
            for _, value in node.variables:
                filters(value)
            filters(node.body)

    for evaluate in parsed.evaluates:
        dependencies(evaluate.table)
        filters(evaluate.table)
    if not aggregated:
        return None
    for table, column in filtered:
        reaches = any(table in model.closure(fact) for fact in aggregated)
        read = table in aggregated or any(table in model.closure(other) for other in referenced)
        if not reaches and not read:
            facts = ", ".join(f"'{model.tables[f].name}'" for f in sorted(aggregated))
            return (
                f"No active many-to-one relationship connects '{model.tables[table].name}' to "
                f"{facts}: filtering or grouping by '{model.tables[table].name}'[{column}] "
                "would not change the result."
            )
    return None


def _walk(node: object) -> list[Any]:
    from .dax_syntax import Binary, Call, Unary, VarBlock

    found = [node]
    if isinstance(node, Call):
        for arg in node.args:
            found.extend(_walk(arg))
    elif isinstance(node, Binary):
        found.extend(_walk(node.left) + _walk(node.right))
    elif isinstance(node, Unary):
        found.extend(_walk(node.operand))
    elif isinstance(node, VarBlock):
        for _, value in node.variables:
            found.extend(_walk(value))
        found.extend(_walk(node.body))
    return found


def evaluate(
    report: ImportedReport, snapshot: DatasetSnapshot, request: DaxEvaluateRequest
) -> DaxResult:
    """A DAX Lab formula or query over the imported data, as Power BI would compute it."""
    model = report_model(report, snapshot)
    started = time.monotonic()
    expression = request.expression.strip()
    query = expression if is_query(expression) else f'EVALUATE ROW("Value", {expression})'
    unavailable = unavailable_reference(model, query)
    if unavailable is not None:
        raise DaxUnavailable(unavailable)
    unrelated = unrelated_filter(model, query)
    if unrelated:
        raise DaxEngineError(unrelated)
    result: QueryResult = run_query(model, query, time_budget=30.0)
    if len(result.columns) > 40:
        raise ValueError("Limit this query to 40 columns")
    columns = _column_names(result.columns)
    rows = [
        {c: _cell(v) for c, v in zip(columns, row, strict=True)}
        for row in result.rows[: request.top_n]
    ]
    return DaxResult(
        engine="snapshot",
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=len(result.rows) > request.top_n,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        plan=(
            f"{ENGINE_LABEL} on the imported data, with the report's own measures and "
            "relationships. Page slicer selections are not applied to this query."
        ),
    )
