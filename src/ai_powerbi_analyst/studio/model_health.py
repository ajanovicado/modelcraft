"""Model health: the checks an experienced Power BI developer runs over a semantic model.

Tabular Editor's Best Practice Analyzer is the reference for *what* a developer checks; the rules,
their thresholds and their wording here are Studio's own. Everything is deterministic: a finding
never comes from a language model (the local model may only phrase or pick among findings).

Inputs, all read elsewhere:
- the model facts (``pbix_extract.read_model_facts``, the full facts: the stored copy in
  ``ImportedReport.model`` drops measure DAX, and without it every "used nowhere" rule would be
  wrong, so ``check_model`` skips those rules and says so);
- how the report uses the model (``report_usage``: visuals, filters, sorts, conditional
  formatting, bookmarks, the automatic date hierarchy's ``PropertyVariationSource``, report
  measures and saved DAX queries);
- optionally, column statistics from the rows Studio decoded (``stats_from_frames``).

Findings are grouped the way a developer reads them: one per relationship, one per table for a
column or measure rule (every object is listed), one for the model. A finding's ``fix`` is a
list of operations of the model and measure families. Every fix goes through Power BI Desktop
and asks first; nothing here applies anything. DAX rules name the measure and the pattern and
leave the rewrite to the planner (``fix`` None). Messages carry object names only: never a
formula, Power Query text, an error text (it can quote the formula) or a data value.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from . import dax_syntax as ds
from .pbir_document import PbirDocument
from .pbix_package import PackageError
from .report_edit_model import (
    MICROSOFT_SUPPORT,
    OUTSIDE_SUPPORT,
    RelationshipFact,
    dax_uses_relationship,
    report_uses,
)
from .report_refs import dax_references

Severity = Literal["problem", "suggestion"]
Category = Literal["correctness", "performance", "usability", "formatting", "naming"]
CATEGORIES: tuple[Category, ...] = (
    "correctness",
    "performance",
    "usability",
    "formatting",
    "naming",
)

# --- thresholds (each with its reason) --------------------------------------------------------

# ``report_edits.parse_operations`` takes at most 50 steps in one batch; a larger fix is split
# into several findings so each one's fix still parses and applies as one batch.
MAX_FIX_STEPS = 50
# Display folders pay off once a table's field list scrolls: past 15 measures (the brief's
# number, and about what the Data pane shows without scrolling on a laptop screen).
FOLDER_THRESHOLD = 15
# A text column with this many different values, nearly one per row, is usually the largest
# structure of a desktop-sized model (each distinct string is stored once, plus its index).
TEXT_DISTINCT = 100_000
NEAR_UNIQUE = 0.5
# A date/time column with more different values than days in a century must carry times of day.
DAYS_IN_CENTURY = 36_525
# How many names a message lists before "and N more".
LISTED = 3

# --- rules --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    id: str
    category: Category
    severity: Severity  # the usual severity; a finding may say otherwise (see the rule)
    title: str


RULES: dict[str, Rule] = {
    r.id: r
    for r in (
        Rule("broken_object", "correctness", "problem", "Objects Power BI marks as in error"),
        Rule(
            "date_table_not_marked",
            "correctness",
            "problem",
            "Time intelligence on a table not marked as a date table",
        ),
        Rule(
            "relationship_key_duplicates",
            "correctness",
            "problem",
            "Repeated values on the one side of a relationship",
        ),
        Rule(
            "relationship_key_blanks",
            "correctness",
            "suggestion",
            "Blank values on the one side of a relationship",
        ),
        Rule(
            "two_way_relationship",
            "correctness",
            "suggestion",
            "Relationships filtering both ways",
        ),
        Rule(
            "many_to_many_relationship",
            "correctness",
            "suggestion",
            "Many-to-many relationships",
        ),
        Rule(
            "floating_point_column",
            "correctness",
            "suggestion",
            "Money or quantities stored as decimal (floating point) numbers",
        ),
        Rule("dax_division", "correctness", "suggestion", "Division with / instead of DIVIDE"),
        Rule("auto_date_time", "performance", "suggestion", "Auto date/time is on"),
        Rule("unused_column", "performance", "suggestion", "Visible columns used nowhere"),
        Rule("unused_hidden_column", "performance", "suggestion", "Hidden columns used nowhere"),
        Rule(
            "unused_calculated_column",
            "performance",
            "suggestion",
            "Hidden calculated columns used nowhere",
        ),
        Rule("high_cardinality_column", "performance", "suggestion", "High-cardinality columns"),
        Rule(
            "calculated_column_as_measure",
            "performance",
            "suggestion",
            "Calculated columns that are only ever added up",
        ),
        Rule("dax_iferror", "performance", "suggestion", "IFERROR or ISERROR in measures"),
        Rule(
            "dax_filter_table",
            "performance",
            "suggestion",
            "FILTER over a whole table where a column filter would do",
        ),
        Rule("visible_key_column", "usability", "suggestion", "Visible key columns"),
        Rule(
            "summarized_key_column",
            "usability",
            "suggestion",
            "Keys, years and sort orders that add up",
        ),
        Rule(
            "unused_inactive_relationship",
            "usability",
            "suggestion",
            "Inactive relationships no formula uses",
        ),
        Rule("unused_measure", "usability", "suggestion", "Visible measures used nowhere"),
        Rule("unused_hidden_measure", "usability", "suggestion", "Hidden measures used nowhere"),
        Rule(
            "measure_folders",
            "usability",
            "suggestion",
            "Measures outside display folders in a long list",
        ),
        Rule("missing_descriptions", "usability", "suggestion", "Objects without descriptions"),
        Rule("measure_no_format", "formatting", "suggestion", "Measures without a format string"),
        Rule("name_whitespace", "naming", "suggestion", "Names with stray spaces or line breaks"),
        Rule("name_case_clash", "naming", "suggestion", "One name spelled in different cases"),
    )
}
_RULE_ORDER = {rule: index for index, rule in enumerate(RULES)}

# --- results ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ObjectRef:
    """A model object. ``table`` is the table the object lives in (a table's own name for a
    table, the measure's home table, a calculation item's group); a relationship is
    ``table``/``name`` (its from end) to ``to_table``/``to_column``."""

    kind: Literal["model", "table", "column", "measure", "calculation_item", "relationship"]
    table: str | None = None
    name: str | None = None
    to_table: str | None = None
    to_column: str | None = None

    def label(self) -> str:
        if self.kind == "column":
            return f"{_quote(self.table or '')}[{self.name}]"
        if self.kind == "measure":
            return f"[{self.name}]"
        if self.kind == "table":
            return _quote(self.name or "")
        if self.kind == "calculation_item":
            return f"the calculation item {self.name} of {_quote(self.table or '')}"
        if self.kind == "relationship":
            return (
                f"{_quote(self.table or '')}[{self.name}] to "
                f"{_quote(self.to_table or '')}[{self.to_column}]"
            )
        return "the model"

    def as_dict(self) -> dict[str, Any]:
        found: dict[str, Any] = {"kind": self.kind, "table": self.table, "name": self.name}
        if self.kind == "relationship":
            found.update(to_table=self.to_table, to_column=self.to_column)
        return found


@dataclass(frozen=True)
class Finding:
    id: str
    rule: str
    category: Category
    severity: Severity
    objects: tuple[ObjectRef, ...]
    message: str  # one plain sentence, object names only
    why: str  # one plain sentence
    fix: list[dict[str, Any]] | None  # model or measure family operations, or None
    fix_summary: str  # what the fix does, or what to do by hand
    fix_needs_desktop: bool  # applying ``fix`` goes through Power BI Desktop
    fix_confirm: bool = True  # always: a model change asks the user first
    # The fix uses a change Microsoft does not list for external tools (Studio makes it only
    # where a model probe passed for the running Desktop version).
    fix_outside_supported: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rule": self.rule,
            "category": self.category,
            "severity": self.severity,
            "objects": [o.as_dict() for o in self.objects],
            "message": self.message,
            "why": self.why,
            "fix": [dict(op) for op in self.fix] if self.fix is not None else None,
            "fix_summary": self.fix_summary,
            "fix_needs_desktop": self.fix_needs_desktop,
            "fix_confirm": self.fix_confirm,
            "fix_outside_supported": self.fix_outside_supported,
        }


@dataclass(frozen=True)
class Health:
    known: bool  # False: the facts say nothing of the model's tables and columns
    findings: list[Finding]
    checked: dict[str, int]  # tables, columns, measures, relationships looked at
    notes: list[str]  # what was not checked and why, in plain words

    @property
    def summary(self) -> dict[str, Any]:
        by_category = {c: 0 for c in CATEGORIES}
        by_rule: dict[str, int] = {}
        for finding in self.findings:
            by_category[finding.category] += 1
            by_rule[finding.rule] = by_rule.get(finding.rule, 0) + 1
        return {
            "problems": sum(f.severity == "problem" for f in self.findings),
            "suggestions": sum(f.severity == "suggestion" for f in self.findings),
            "by_category": by_category,
            "by_rule": by_rule,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "known": self.known,
            "findings": [f.as_dict() for f in self.findings],
            "summary": self.summary,
            "checked": dict(self.checked),
            "notes": list(self.notes),
        }


# --- how the report uses the model ------------------------------------------------------------


@dataclass(frozen=True)
class ColumnUse:
    fields: int = 0  # grouped, filtered, sorted or formatted by the column itself
    aggregated: int = 0  # summarized by a visual ("Sum of Amount")
    date_hierarchy: int = 0  # through its automatic date hierarchy


@dataclass(frozen=True)
class Usage:
    """Every model object a report refers to, by folded name. ``known`` False: no report was
    read, so no rule may conclude that something is used nowhere."""

    columns: Mapping[tuple[str, str], ColumnUse] = field(
        default_factory=dict[tuple[str, str], ColumnUse]
    )
    measures: frozenset[str] = frozenset()
    hierarchies: frozenset[tuple[str, str]] = frozenset()
    levels: frozenset[tuple[str, str, str]] = frozenset()  # (table, hierarchy, level)
    date_hierarchy_places: tuple[str, ...] = ()  # where the report uses an automatic hierarchy
    dax: tuple[str, ...] = ()  # report measures and saved DAX queries
    known: bool = True

    @classmethod
    def unknown(cls) -> Usage:
        return cls(known=False)


def _fold(value: object) -> str:
    return str(value or "").casefold()


def _aggregated_bodies(node: Any, found: set[int]) -> None:
    """The Column expressions a visual summarizes (``Aggregation > Expression > Column``), by
    identity: ``report_uses`` hands the same live dicts to its callback."""
    if isinstance(node, list):
        for item in node:
            _aggregated_bodies(item, found)
    elif isinstance(node, dict):
        aggregation = node.get("Aggregation")
        if isinstance(aggregation, dict):
            inner = aggregation.get("Expression")
            column = inner.get("Column") if isinstance(inner, dict) else None
            if isinstance(column, dict):
                found.add(id(column))
        for value in node.values():
            _aggregated_bodies(value, found)


def _text(data: bytes) -> str:
    # Desktop writes DAX query files as UTF-16LE (no BOM) or UTF-8.
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data[:16]:
        return data.decode("utf-16-le", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def report_usage(document: PbirDocument) -> Usage:
    """How ``document`` uses the model: fields in visuals (and whether a visual summarizes a
    column), filters at every level, sorts, conditional formatting, bookmarks, user and automatic
    date hierarchies, plus the DAX of report measures and saved DAX queries."""
    aggregated: set[int] = set()
    for _, data, _ in document.iter_json_parts():
        _aggregated_bodies(data, aggregated)
    counts: dict[tuple[str, str], list[int]] = {}
    measures: set[str] = set()
    hierarchies: set[tuple[str, str]] = set()
    levels: set[tuple[str, str, str]] = set()

    def collect(kind: str, body: dict[str, Any], entity: str | None) -> bool:
        if entity is None:
            return False
        prop = body.get("Property")
        if kind == "Column" and isinstance(prop, str):
            slot = counts.setdefault((_fold(entity), _fold(prop)), [0, 0, 0])
            # A PropertyVariationSource (the automatic date hierarchy) is visited as a Column
            # and is the only one with a Name ("Variation").
            slot[2 if "Name" in body else 1 if id(body) in aggregated else 0] += 1
        elif kind == "Measure" and isinstance(prop, str):
            measures.add(_fold(prop))
        elif kind == "Hierarchy" and isinstance(body.get("Hierarchy"), str):
            hierarchies.add((_fold(entity), _fold(body["Hierarchy"])))
        elif kind == "HierarchyLevel":
            inner = body.get("Expression")
            hierarchy = inner.get("Hierarchy") if isinstance(inner, dict) else None
            name = hierarchy.get("Hierarchy") if isinstance(hierarchy, dict) else None
            if isinstance(name, str) and isinstance(body.get("Level"), str):
                hierarchies.add((_fold(entity), _fold(name)))
                levels.add((_fold(entity), _fold(name), _fold(body["Level"])))
        return False

    report_uses(document, collect)
    places = report_uses(document, lambda kind, body, _: kind == "Column" and "Name" in body)
    dax: list[str] = []
    extensions = document.extensions
    for entity in extensions.get("entities") or [] if isinstance(extensions, dict) else []:
        for measure in entity.get("measures") or [] if isinstance(entity, dict) else []:
            if isinstance(measure, dict) and isinstance(measure.get("expression"), str):
                dax.append(measure["expression"])
    for name in document.package.names():
        if name.startswith("DAXQueries/") and name.lower().endswith(".dax"):
            try:
                dax.append(_text(document.package.read(name)))
            except (KeyError, PackageError):
                continue
    return Usage(
        columns={k: ColumnUse(*v) for k, v in counts.items()},
        measures=frozenset(measures),
        hierarchies=frozenset(hierarchies),
        levels=frozenset(levels),
        date_hierarchy_places=tuple(places),
        dax=tuple(dax),
    )


# --- column statistics ------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnStats:
    rows: int
    distinct: int  # different non-blank values
    blanks: int  # null values, and empty texts

    @property
    def duplicates(self) -> bool:
        return self.distinct < self.rows - self.blanks


def column_stats(values: pd.Series) -> ColumnStats:
    """Rows, distinct non-blank values and blanks of one decoded column."""
    missing = values.isna()
    if values.dtype == object or str(values.dtype) in ("string", "str"):
        missing = missing | (values.astype("string") == "").fillna(False)
    present = values[~missing]
    return ColumnStats(len(values), int(present.nunique()), int(missing.sum()))


def stats_from_frames(
    frames: Mapping[str, pd.DataFrame],
    *,
    wanted: Mapping[str, Collection[str]] | None = None,
) -> dict[tuple[str, str], ColumnStats]:
    """``{(table, column) folded: ColumnStats}`` for the decoded tables ``{table: frame}`` (the
    snapshot's rows); ``wanted`` limits it to some columns per table (``stats_wanted``)."""
    found: dict[tuple[str, str], ColumnStats] = {}
    limits = {_fold(t): {_fold(c) for c in cols} for t, cols in (wanted or {}).items()}
    for table, frame in frames.items():
        keep = limits.get(_fold(table)) if wanted is not None else None
        if wanted is not None and keep is None:
            continue
        for column in frame.columns:
            if keep is not None and _fold(column) not in keep:
                continue
            found[(_fold(table), _fold(column))] = column_stats(frame[column])
    return found


def stats_from_parquet(
    files: Mapping[str, str | Path],
    *,
    wanted: Mapping[str, Collection[str]] | None = None,
) -> dict[tuple[str, str], ColumnStats]:
    """``stats_from_frames`` over the snapshot's Parquet files ``{table: path}``, reading only
    the ``wanted`` columns."""
    frames: dict[str, pd.DataFrame] = {}
    limits = {_fold(t): {_fold(c) for c in cols} for t, cols in (wanted or {}).items()}
    for table, path in files.items():
        keep = limits.get(_fold(table))
        if wanted is not None and keep is None:
            continue
        frame = pd.read_parquet(path)
        if keep is not None:
            frame = frame[[c for c in frame.columns if _fold(c) in keep]]
        frames[table] = frame
    return stats_from_frames(frames)


def stats_wanted(facts: Mapping[str, Any]) -> dict[str, list[str]]:
    """The columns whose statistics a rule can use: the one side of each relationship, and
    every text and date/time column. Read only these from the rows."""
    wanted: dict[str, list[str]] = {}

    def add(table: object, column: object) -> None:
        if isinstance(table, str) and isinstance(column, str):
            names = wanted.setdefault(table, [])
            if column not in names:
                names.append(column)

    for rel in _items(facts, "relationships"):
        if rel.get("toCardinality") == "one":
            add(rel.get("toTable"), rel.get("toColumn"))
        if rel.get("fromCardinality") == "one":
            add(rel.get("fromTable"), rel.get("fromColumn"))
    for column in _items(facts, "columns"):
        if column.get("dataType") in (_TEXT, _DATETIME):
            add(column.get("table"), column.get("name"))
    return wanted


# --- reading the facts ------------------------------------------------------------------------

_TEXT, _WHOLE, _DOUBLE, _DATETIME, _FIXED, _BOOLEAN = 2, 6, 8, 9, 10, 11
_NUMERIC = (_WHOLE, _DOUBLE, _FIXED)
_ERROR_STATES = frozenset({"semanticError", "evaluationError", "dependencyError"})
# Name words (camelCase and spaces split) that say what a column holds.
_MONEY = frozenset(
    {
        "amount", "price", "cost", "costs", "sales", "revenue", "discount", "tax", "profit",
        "fee", "fees", "freight", "salary", "wage", "wages", "budget", "spend", "income",
        "expense", "expenses", "payment", "balance", "charge",
    }
)  # fmt: skip
_QUANTITY = frozenset({"quantity", "qty", "units"})
_NOT_MONEY = frozenset(
    {"rate", "ratio", "pct", "percent", "percentage", "margin", "share", "weight", "score"}
)
# The last word of a number column that is a key, a date part or a sort order: adding it up
# means nothing.
_KEYISH = frozenset(
    {
        "key", "id", "code", "year", "month", "quarter", "week", "day", "number", "no", "order",
        "sort", "index", "rank", "zip", "postcode", "phone",
    }
)  # fmt: skip
_SURROGATE = frozenset({"key", "id", "sk"})
_AGGREGATES = frozenset({"SUM", "AVERAGE", "MIN", "MAX", "MEDIAN", "PRODUCT"})
_ITERATORS = frozenset({"SUMX", "AVERAGEX", "MINX", "MAXX", "MEDIANX", "PRODUCTX"})
# Time intelligence functions and the position of their dates argument.
_TIME_INTELLIGENCE: dict[str, int] = {
    **dict.fromkeys(
        (
            "DATESYTD", "DATESQTD", "DATESMTD", "SAMEPERIODLASTYEAR", "DATEADD",
            "PARALLELPERIOD", "DATESINPERIOD", "DATESBETWEEN", "PREVIOUSDAY", "PREVIOUSMONTH",
            "PREVIOUSQUARTER", "PREVIOUSYEAR", "NEXTDAY", "NEXTMONTH", "NEXTQUARTER",
            "NEXTYEAR", "STARTOFMONTH", "STARTOFQUARTER", "STARTOFYEAR", "ENDOFMONTH",
            "ENDOFQUARTER", "ENDOFYEAR", "FIRSTDATE", "LASTDATE",
        ),
        0,
    ),
    **dict.fromkeys(
        (
            "TOTALYTD", "TOTALQTD", "TOTALMTD", "OPENINGBALANCEMONTH", "OPENINGBALANCEQUARTER",
            "OPENINGBALANCEYEAR", "CLOSINGBALANCEMONTH", "CLOSINGBALANCEQUARTER",
            "CLOSINGBALANCEYEAR",
        ),
        1,
    ),
}  # fmt: skip
# Functions a FILTER predicate may call and still be a plain condition on the table's own
# columns (anything else can depend on other tables or on context transition).
_SCALAR = frozenset(
    {
        "AND", "OR", "NOT", "IF", "SWITCH", "COALESCE", "ISBLANK", "BLANK", "TRUE", "FALSE",
        "DATE", "TIME", "YEAR", "MONTH", "DAY", "LEFT", "RIGHT", "MID", "UPPER", "LOWER", "LEN",
        "SEARCH", "FIND", "CONTAINSSTRING", "CONTAINSSTRINGEXACT", "VALUE", "INT", "ABS",
        "ROUND", "TRIM", "FORMAT",
    }
)  # fmt: skip
_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_CONTROL = re.compile(r"[\t\r\n\x00-\x1f]")


def _items(facts: Mapping[str, Any] | None, section: str) -> list[dict[str, Any]]:
    value = facts.get(section) if facts is not None else None
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _words(name: str) -> list[str]:
    return [w.casefold() for w in _WORDS.findall(name)]


def _quote(table: str) -> str:
    return "'" + table.replace("'", "''") + "'"


def _names(names: Sequence[str]) -> str:
    """'A', 'A and B', 'A, B and C', 'A, B, C and 9 more'."""
    if not names:
        return ""
    if len(names) <= LISTED:
        return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return ", ".join(names[:LISTED]) + f" and {len(names) - LISTED} more"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


@dataclass(frozen=True)
class _Formula:
    kind: str  # measure | format | column | table | item | role | report
    table: str | None
    name: str
    text: str


def _source_column(text: object) -> tuple[str, str] | None:
    """'Table[Column]' of a calculated table column copied from another table."""
    if not isinstance(text, str):
        return None
    match = re.fullmatch(r"\s*('(?:[^']|'')*'|[^\[']+)\[(.+)\]\s*", text)
    if not match:
        return None
    table = match.group(1).strip()
    if table.startswith("'"):
        table = table[1:-1].replace("''", "'")
    return table, match.group(2).replace("]]", "]")


_Visit = tuple[ds.Node, tuple[ds.Node, ...]]


def _walk(node: ds.Node, parents: tuple[ds.Node, ...] = ()) -> Iterator[_Visit]:
    """Every node of a parsed formula with its ancestors (nearest last)."""
    yield node, parents
    children: list[ds.Node] = []
    if isinstance(node, ds.Call):
        children = list(node.args)
    elif isinstance(node, ds.Binary):
        children = [node.left, node.right]
    elif isinstance(node, ds.Unary):
        children = [node.operand]
    elif isinstance(node, ds.VarBlock):
        children = [value for _, value in node.variables] + [node.body]
    elif isinstance(node, ds.TableCtor):
        children = [cell for row in node.rows for cell in row]
    for child in children:
        yield from _walk(child, (*parents, node))


class _Model:
    """The facts, indexed, with everything that uses each column and measure."""

    def __init__(
        self,
        facts: Mapping[str, Any],
        usage: Usage | None,
        stats: Mapping[tuple[str, str], ColumnStats] | None,
    ) -> None:
        self.facts = facts
        self.usage = usage if usage is not None and usage.known else None
        self.stats = stats or {}
        self.notes: list[str] = []
        self.tables = {_fold(t.get("name")): t for t in _items(facts, "tables") if t.get("name")}
        self.columns = {
            (_fold(c.get("table")), _fold(c.get("name"))): c
            for c in _items(facts, "columns")
            if c.get("table") and c.get("name")
        }
        self.measures = {
            _fold(m.get("name")): m for m in _items(facts, "measures") if m.get("name")
        }
        ends = ("fromTable", "fromColumn", "toTable", "toColumn")
        # Auto date/time's own joins are the auto_date_time rule's business.
        self.relationships = [
            r
            for r in _items(facts, "relationships")
            if all(isinstance(r.get(k), str) for k in ends)
            and not (self.auto_date(r["fromTable"]) or self.auto_date(r["toTable"]))
        ]
        self.special = self._special_tables()
        self.formulas, self.dax_complete = self._formulas()
        self.trees: dict[int, ds.Node | None] = {}
        self.column_uses: dict[tuple[str, str], set[str]] = {}
        self.measure_uses: dict[str, set[str]] = {}
        self._find_uses()

    # --- objects ------------------------------------------------------------------------------

    def table(self, name: object) -> dict[str, Any]:
        return self.tables.get(_fold(name), {})

    def auto_date(self, table: object) -> bool:
        row = self.table(table)
        return bool(row.get("autoDateTable") or row.get("showAsVariationsOnly"))

    def _special_tables(self) -> set[str]:
        """Tables Desktop wires itself: auto date/time tables, calculation groups, field and
        what-if parameters. Their columns are used by the machinery, not listed as fields."""
        found = {name for name in self.tables if self.auto_date(name)}
        found |= {name for name, t in self.tables.items() if t.get("calculationGroup")}
        for section in ("calculationGroups", "fieldParameters", "whatIfParameters"):
            found |= {_fold(item.get("table")) for item in _items(self.facts, section)}
        return found

    def hidden(self, column: Mapping[str, Any]) -> bool:
        table = self.table(column.get("table"))
        return bool(column.get("hidden") or table.get("hidden") or table.get("private"))

    def date_table(self, table: object) -> bool:
        return bool(self.table(table).get("isDateTable"))

    def columns_of(self, table: object) -> list[dict[str, Any]]:
        return [c for k, c in self.columns.items() if k[0] == _fold(table)]

    def measure_ref(self, measure: Mapping[str, Any]) -> ObjectRef:
        return ObjectRef("measure", str(measure.get("table") or ""), str(measure["name"]))

    @staticmethod
    def column_ref(column: Mapping[str, Any]) -> ObjectRef:
        return ObjectRef("column", str(column["table"]), str(column["name"]))

    # --- formulas and uses --------------------------------------------------------------------

    def _formulas(self) -> tuple[list[_Formula], bool]:
        """Every DAX text in the model and the report, and whether they are all known (a
        measure whose DAX was left out or cut could use anything)."""
        complete = isinstance(self.facts.get("measures"), list)
        truncated = self.facts.get("truncated")
        if isinstance(truncated, list) and {"measures", "columns", "tables", "calculationGroups",
                                            "roles"} & set(truncated):  # fmt: skip
            complete = False
        found: list[_Formula] = []

        def add(kind: str, table: object, name: object, text: object) -> None:
            if isinstance(text, str) and text.strip():
                found.append(_Formula(kind, str(table) if table else None, str(name or ""), text))

        for measure in self.measures.values():
            if "expression" not in measure or measure.get("truncated"):
                complete = False
            add("measure", measure.get("table"), measure.get("name"), measure.get("expression"))
            add("format", measure.get("table"), measure.get("name"),
                measure.get("formatStringExpression"))  # fmt: skip
        for column in self.columns.values():
            if column.get("kind") == "calculated":
                add("column", column.get("table"), column.get("name"), column.get("expression"))
        for table in self.tables.values():
            if table.get("calculated"):
                add("table", table.get("name"), table.get("name"), table.get("expression"))
        for group in _items(self.facts, "calculationGroups"):
            for item in _items(group, "items"):
                add("item", group.get("table"), item.get("name"), item.get("expression"))
                add("format", group.get("table"), item.get("name"),
                    item.get("formatStringExpression"))  # fmt: skip
            for selection in _items(group, "selectionExpressions"):
                add("item", group.get("table"), group.get("table"), selection.get("expression"))
        for role in _items(self.facts, "roles"):
            for permission in _items(role, "tables"):
                add("role", permission.get("table"), role.get("name"), permission.get("filter"))
        for text in self.usage.dax if self.usage is not None else ():
            add("report", None, "", text)
        return found, complete

    def tree(self, formula: _Formula) -> ds.Node | None:
        key = id(formula)
        if key not in self.trees:
            try:
                self.trees[key] = ds.parse_expression(formula.text)
            except (ds.DaxParseError, RecursionError, ValueError):
                self.trees[key] = None
        return self.trees[key]

    def _use_column(self, table: object, column: object, how: str) -> None:
        key = (_fold(table), _fold(column))
        if key in self.columns:
            self.column_uses.setdefault(key, set()).add(how)

    def _find_uses(self) -> None:
        by_name: dict[str, list[tuple[str, str]]] = {}
        for key in self.columns:
            by_name.setdefault(key[1], []).append(key)
        for formula in self.formulas:
            for ref in dax_references(formula.text, tables=True):
                if ref.name is None:
                    # A calculated table that names a table on its own (Date = DateAutoTemplate)
                    # can return every column of it.
                    if formula.kind == "table" and _fold(ref.table) in self.tables:
                        for column in self.columns_of(ref.table):
                            self._use_column(column["table"], column["name"], "dax")
                    continue
                name = _fold(ref.name)
                if ref.table and (_fold(ref.table), name) in self.columns:
                    self._use_column(ref.table, ref.name, "dax")
                elif name in self.measures:
                    if not (formula.kind == "measure" and _fold(formula.name) == name):
                        self.measure_uses.setdefault(name, set()).add("dax")
                elif not ref.table:
                    # A bare [Name] that is no measure is a column of whatever table the row
                    # context iterates: count every column of that name as used.
                    for key in by_name.get(name, []):
                        self.column_uses.setdefault(key, set()).add("dax")
        for rel in self.relationships:
            self._use_column(rel["fromTable"], rel["fromColumn"], "relationship")
            self._use_column(rel["toTable"], rel["toColumn"], "relationship")
        for column in self.columns.values():
            if column.get("sortByColumn"):
                self._use_column(column["table"], column["sortByColumn"], "sort")
            source = _source_column(column.get("sourceColumn"))
            if column.get("kind") == "calculatedTableColumn" and source is not None:
                self._use_column(source[0], source[1], "source")
        for table in self.tables.values():
            if table.get("isDateTable") and table.get("dateColumn"):
                self._use_column(table["name"], table["dateColumn"], "date_table")
        levels: dict[tuple[str, str, str], str] = {}
        for entry in _items(self.facts, "hierarchies"):
            for level in _items(entry, "levels"):
                self._use_column(entry.get("table"), level.get("column"), "hierarchy")
                levels[(_fold(entry.get("table")), _fold(entry.get("name")),
                        _fold(level.get("name")))] = str(level.get("column"))  # fmt: skip
        if self.usage is None:
            return
        for key, use in self.usage.columns.items():
            if key in self.columns and (use.fields or use.aggregated or use.date_hierarchy):
                self.column_uses.setdefault(key, set()).add("report")
        for level_key in self.usage.levels:
            level_column = levels.get(level_key)
            if level_column is not None:
                self._use_column(level_key[0], level_column, "report")
        for measure in self.usage.measures:
            if measure in self.measures:
                self.measure_uses.setdefault(measure, set()).add("report")

    def uses_known(self) -> bool:
        return self.usage is not None and self.dax_complete

    def column_used(self, column: Mapping[str, Any]) -> bool:
        return bool(self.column_uses.get((_fold(column["table"]), _fold(column["name"]))))

    def reported_unused(self, column: Mapping[str, Any]) -> bool:
        """Whether the unused-column rules report the column: the other column rules then leave
        it to them (fixing the type of a column that should go is wasted work)."""
        return (
            self.uses_known()
            and not self.column_used(column)
            and _fold(column["table"]) not in self.special
            and not self.date_table(column["table"])
        )


# --- building findings ------------------------------------------------------------------------


def _outside(fix: Sequence[Mapping[str, Any]] | None) -> list[str]:
    """What in ``fix`` Microsoft does not list for external tools, in plain words."""
    found: list[str] = []
    for op in fix or []:
        name = str(op.get("op"))
        if MICROSOFT_SUPPORT.get(name) == "outside":
            what = OUTSIDE_SUPPORT.get(name, (name.replace("_", " ").capitalize(), ""))[0]
            if what not in found:
                found.append(what)
    return found


def _finding(
    rule: str,
    key: str,
    objects: Sequence[ObjectRef],
    message: str,
    why: str,
    fix: list[dict[str, Any]] | None,
    fix_summary: str,
    *,
    severity: Severity | None = None,
) -> Finding:
    info = RULES[rule]
    outside = _outside(fix)
    if outside:
        fix_summary += (
            f" {' and '.join(outside)} is outside the changes Microsoft supports for tools like "
            "Studio; Studio makes it only where it has tested it with your Power BI Desktop "
            "version."
        )
    return Finding(
        id=f"{rule}:{key}" if key else rule,
        rule=rule,
        category=info.category,
        severity=severity or info.severity,
        objects=tuple(objects),
        message=message,
        why=why,
        fix=fix or None,
        fix_summary=fix_summary,
        fix_needs_desktop=bool(fix),
        fix_outside_supported=bool(outside),
    )


def _per_table(
    rule: str,
    items: Sequence[tuple[ObjectRef, dict[str, Any] | None]],
    build: Callable[[str, list[ObjectRef], list[dict[str, Any]] | None], tuple[str, str, str]],
) -> list[Finding]:
    """One finding per table (in first-seen order) over ``(object, fix op)`` pairs:
    ``build(table, objects, fix)`` gives (message, why, fix_summary). A table's objects whose
    fix is None make their own finding (``<table>:by-hand``); a fix longer than one batch is
    split (``<table>:2``...)."""
    groups: dict[tuple[str, bool], list[tuple[ObjectRef, dict[str, Any] | None]]] = {}
    for ref, op in items:
        groups.setdefault((ref.table or "", op is not None), []).append((ref, op))
    tables = list(dict.fromkeys(ref.table or "" for ref, _ in items))
    found: list[Finding] = []
    for table in tables:
        for fixable in (True, False):
            group = groups.get((table, fixable))
            if not group:
                continue
            chunks = [group[i : i + MAX_FIX_STEPS] for i in range(0, len(group), MAX_FIX_STEPS)]
            for index, chunk in enumerate(chunks, start=1):
                objects = [ref for ref, _ in chunk]
                fix = [op for _, op in chunk if op is not None] or None
                key = table + ("" if fixable or not groups.get((table, True)) else ":by-hand")
                key += f":{index}" if len(chunks) > 1 else ""
                message, why, summary = build(table, objects, fix)
                found.append(_finding(rule, key, objects, message, why, fix, summary))
    return found


def _column_names(objects: Sequence[ObjectRef]) -> str:
    return _names([o.name or "" for o in objects])


# --- the rules ------------------------------------------------------------------------------------


def _broken_objects(model: _Model) -> list[Finding]:
    found: list[Finding] = []
    for measure in model.measures.values():
        if measure.get("state") in _ERROR_STATES or measure.get("error"):
            ref = model.measure_ref(measure)
            found.append(
                _finding(
                    "broken_object",
                    ref.label(),
                    [ref],
                    f"The measure {ref.label()} is in an error state in Power BI Desktop.",
                    "Every visual that uses it shows an error instead of a value.",
                    None,
                    "Open the measure in Power BI Desktop to see the error, then fix its formula.",
                )
            )
    for column in model.columns.values():
        if column.get("state") in _ERROR_STATES or column.get("error"):
            ref = model.column_ref(column)
            found.append(
                _finding(
                    "broken_object",
                    ref.label(),
                    [ref],
                    f"The column {ref.label()} is in an error state in Power BI Desktop.",
                    "Visuals and measures that use it show errors or wrong values.",
                    None,
                    "Open the column in Power BI Desktop to see the error, then fix its formula "
                    "or its query.",
                )
            )
    return found


def _relationship_ref(rel: Mapping[str, Any]) -> ObjectRef:
    return ObjectRef(
        "relationship", rel["fromTable"], rel["fromColumn"], rel["toTable"], rel["toColumn"]
    )


def _relationship_key(rel: Mapping[str, Any]) -> str:
    return f"{rel['fromTable']}[{rel['fromColumn']}]>{rel['toTable']}[{rel['toColumn']}]"


def _ends(rel: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "from_table": rel["fromTable"],
        "from_column": rel["fromColumn"],
        "to_table": rel["toTable"],
        "to_column": rel["toColumn"],
    }


def _relationship_rules(model: _Model) -> list[Finding]:
    found: list[Finding] = []
    for rel in model.relationships:
        ref = _relationship_ref(rel)
        key = _relationship_key(rel)
        cardinality = str(rel.get("cardinality") or "")
        if rel.get("crossFilter") == "both" and cardinality != "1:1":
            one = rel["toTable"] if rel.get("toCardinality") == "one" else rel["fromTable"]
            many = rel["fromTable"] if one == rel["toTable"] else rel["toTable"]
            direction = (
                f" (from {_quote(one)} to {_quote(many)})" if cardinality in ("M:1", "1:M") else ""
            )
            found.append(
                _finding(
                    "two_way_relationship",
                    key,
                    [ref],
                    f"The relationship from {ref.label()} filters in both directions.",
                    "Filters that flow both ways can reach a table by more than one path, which "
                    "makes some totals hard to predict and every visual slower.",
                    [{"op": "update_relationship", **_ends(rel), "cross_filter": "single"}],
                    f"Make it filter in one direction{direction}; check the visuals that relied "
                    "on the other direction, their numbers change.",
                )
            )
        if cardinality == "M:M":
            found.append(
                _finding(
                    "many_to_many_relationship",
                    key,
                    [ref],
                    f"The relationship from {ref.label()} is many-to-many.",
                    "Many-to-many joins give totals that are not the sum of their rows and are "
                    "slower than a many-to-one join.",
                    None,
                    "Check whether one of the two columns should hold each value once; if so, "
                    "remove its repeats in Power Query and make the relationship many-to-one, "
                    "otherwise join through a table with one row per value.",
                )
            )
        # A report measure can turn it on too, so the report must be known as well.
        if not rel.get("active") and model.uses_known():
            fact = RelationshipFact(
                rel["fromTable"], rel["fromColumn"], rel["toTable"], rel["toColumn"], active=False
            )
            if not any(dax_uses_relationship(f.text, fact) for f in model.formulas):
                found.append(
                    _finding(
                        "unused_inactive_relationship",
                        key,
                        [ref],
                        f"The inactive relationship from {ref.label()} is not turned on by any "
                        "formula.",
                        "An inactive relationship only filters inside a measure that uses "
                        "USERELATIONSHIP, so one that no formula uses does nothing.",
                        [{"op": "delete_relationship", **_ends(rel)}],
                        "Delete the relationship, or write the measure that was meant to use it.",
                    )
                )
        for side in ("to", "from"):
            if rel.get(f"{side}Cardinality") != "one":
                continue
            table, column = rel[f"{side}Table"], rel[f"{side}Column"]
            stats = model.stats.get((_fold(table), _fold(column)))
            other = rel["fromTable"] if side == "to" else rel["toTable"]
            label = f"{_quote(table)}[{column}]"
            if stats is not None and stats.duplicates:
                found.append(
                    _finding(
                        "relationship_key_duplicates",
                        f"{key}:{side}",
                        [ref],
                        f"{label} repeats values ({stats.distinct:,} different values in "
                        f"{stats.rows - stats.blanks:,} rows) but is the one side of its "
                        f"relationship with {_quote(other)}.",
                        "Power BI needs each value once on the one side; the next refresh "
                        "fails, and rows can match the wrong ones until then.",
                        None,
                        "Remove the repeated values in Power Query, or make the relationship "
                        "many-to-many if both sides really repeat.",
                    )
                )
            if stats is not None and stats.blanks:
                found.append(
                    _finding(
                        "relationship_key_blanks",
                        f"{key}:{side}",
                        [ref],
                        f"{label} has {_plural(stats.blanks, 'blank value')} on the one side of "
                        f"its relationship with {_quote(other)}.",
                        "A blank key cannot be told apart from the blank row Power BI adds for "
                        "unmatched rows, so some rows land under (Blank).",
                        None,
                        "Replace or remove the blank keys in Power Query.",
                    )
                )
    return found


def _floating_point_columns(model: _Model) -> list[Finding]:
    items: list[tuple[ObjectRef, dict[str, Any] | None]] = []
    kinds: dict[tuple[str, str], str] = {}
    keys = {(_fold(r[f"{s}Table"]), _fold(r[f"{s}Column"])) for r in model.relationships
            for s in ("from", "to")}  # fmt: skip
    for key, column in model.columns.items():
        if column.get("dataType") != _DOUBLE or key[0] in model.special or key in keys:
            continue
        if model.reported_unused(column):
            continue  # the unused-column rules say what to do with it
        words = set(_words(str(column["name"])))
        if words & _NOT_MONEY:
            continue
        kind = "fixed_decimal" if words & _MONEY else "whole_number" if words & _QUANTITY else ""
        if not kind:
            continue
        kinds[key] = kind
        op = (
            {"op": "set_column_properties", "table": column["table"], "name": column["name"],
             "data_type": kind}
            if column.get("kind") == "calculated"
            else None
        )  # fmt: skip
        items.append((model.column_ref(column), op))

    def build(table: str, objects: list[ObjectRef], fix: Any) -> tuple[str, str, str]:
        money = [o for o in objects if kinds[(_fold(o.table), _fold(o.name))] == "fixed_decimal"]
        counts = [o for o in objects if o not in money]
        what = "money" if not counts else "quantities" if not money else "money or quantities"
        advice = []
        if money:
            advice.append(f"{_column_names(money)} to Fixed decimal number")
        if counts:
            advice.append(f"{_column_names(counts)} to Whole number")
        where = (
            "Change the calculated column's type" + ("s" if len(objects) > 1 else "")
            if fix
            else "In Power Query (Transform data), change the type of"
        )
        return (
            f"{_plural(len(objects), 'column')} of {_quote(table)} "
            f"{'holds' if len(objects) == 1 else 'hold'} {what} as decimal (floating point) "
            f"numbers: {_column_names(objects)}.",
            "Floating point numbers are binary fractions, so sums of money can be off by tiny "
            "amounts; Fixed decimal keeps four exact decimal places and Whole number keeps "
            "counts exact, and both usually compress better.",
            f"{where}: {' and '.join(advice)}.",
        )

    return _per_table("floating_point_column", items, build)


def _auto_date_time(model: _Model) -> list[Finding]:
    settings = model.facts.get("model")
    on = isinstance(settings, dict) and settings.get("autoDateTime") is True
    local = [t for t in model.tables.values() if t.get("autoDateTable") == "local"
             or (t.get("showAsVariationsOnly") and not t.get("autoDateTable"))]  # fmt: skip
    variations = _items(model.facts, "variations")
    if not (on or local or variations):
        return []
    count = len(local) or len({(_fold(v.get("table")), _fold(v.get("column"))) for v in variations})
    if count:
        message = (
            f"Auto date/time is on, so Power BI keeps {_plural(count, 'hidden date table')}, "
            "one for each date column."
        )
    else:
        message = "Auto date/time is on, so every new date column gets a hidden date table."
    places = model.usage.date_hierarchy_places if model.usage is not None else ()
    first = (
        f" First move the {_plural(len(places), 'visual')} that use an automatic date "
        f"hierarchy ({_names(list(places))}) to a date table's columns."
        if places
        else ""
    )
    return [
        _finding(
            "auto_date_time",
            "",
            [ObjectRef("model")],
            message,
            "Each hidden date table is stored and refreshed with the file, which makes the "
            "model bigger and slower; one date table marked as such does the same job.",
            None,
            "In Power BI Desktop, open File > Options and settings > Options > Current file > "
            f"Data load and clear Auto date/time.{first}",
        )
    ]


def _time_intelligence(model: _Model) -> list[Finding]:
    """Time intelligence over a column whose table is not marked as a date table."""
    targets: dict[str, set[str]] = {}
    for formula in model.formulas:
        if formula.kind not in ("measure", "column", "item"):
            continue
        tree = model.tree(formula)
        for node, _ in _walk(tree) if tree is not None else ():
            if not isinstance(node, ds.Call) or node.name not in _TIME_INTELLIGENCE:
                continue
            position = _TIME_INTELLIGENCE[node.name]
            dates = node.args[position] if len(node.args) > position else None
            if isinstance(dates, ds.ColRef) and dates.table:
                column = model.columns.get((_fold(dates.table), _fold(dates.column)))
                if column is not None and column.get("dataType") == _DATETIME:
                    targets.setdefault(_fold(dates.table), set()).add(str(column["name"]))
    found: list[Finding] = []
    for folded, columns in targets.items():
        table = model.tables.get(folded)
        if table is None or table.get("isDateTable") or folded in model.special:
            continue
        name = str(table["name"])
        many_side = [
            r
            for r in model.relationships
            for side in ("from", "to")
            if _fold(r[f"{side}Table"]) == folded and r.get(f"{side}Cardinality") == "many"
        ]
        joins = [
            str(r["fromColumn"] if _fold(r["fromTable"]) == folded else r["toColumn"])
            for r in model.relationships
            if folded in (_fold(r["fromTable"]), _fold(r["toTable"]))
        ]
        non_date = [
            c
            for c in joins
            if (model.columns.get((folded, _fold(c))) or {}).get("dataType") != _DATETIME
        ]
        listed = sorted(columns)
        refs = [ObjectRef("table", name, name)] + [ObjectRef("column", name, c) for c in listed]
        if many_side:
            found.append(
                _finding(
                    "date_table_not_marked",
                    name,
                    refs,
                    f"Measures use time intelligence on {_quote(name)}[{_names(listed)}], a "
                    "table on the many side of a relationship rather than a date table.",
                    "Time intelligence needs a table with one row per day and no gaps; over the "
                    "dates of a fact table it skips the days without rows, so some totals come "
                    "out wrong.",
                    None,
                    f"Add a date table with one row per day, relate it to {_quote(name)}"
                    f"[{listed[0]}], mark it as a date table and use its date column in the "
                    "measures.",
                )
            )
            continue
        problem = bool(non_date)
        why = (
            f"{_quote(name)} joins the other tables on {_names(non_date)}, not on a date, so "
            "time intelligence returns wrong totals until the table is marked."
            if problem
            else "Marking it tells Power BI which column holds the dates, which time "
            "intelligence and Power BI Desktop's date features rely on."
        )
        fix = (
            [{"op": "mark_date_table", "table": name, "column": listed[0]}]
            if len(listed) == 1
            else None
        )
        found.append(
            _finding(
                "date_table_not_marked",
                name,
                refs,
                f"Measures use time intelligence on {_quote(name)}[{_names(listed)}], but "
                f"{_quote(name)} is not marked as a date table.",
                why,
                fix,
                f"Mark {_quote(name)} as a date table on [{listed[0]}] (Power BI Desktop "
                "checks that it holds each date once)."
                if fix
                else f"Mark {_quote(name)} as a date table on the column the measures use.",
                severity="problem" if problem else "suggestion",
            )
        )
    return found


def _unused_columns(model: _Model) -> list[Finding]:
    visible: list[tuple[ObjectRef, dict[str, Any] | None]] = []
    hidden_data: list[tuple[ObjectRef, dict[str, Any] | None]] = []
    hidden_calculated: list[tuple[ObjectRef, dict[str, Any] | None]] = []
    for key, column in model.columns.items():
        if key[0] in model.special or model.date_table(column["table"]):
            continue
        if model.column_used(column):
            continue
        ref = model.column_ref(column)
        if not model.hidden(column):
            visible.append(
                (ref, {"op": "set_column_properties", "table": ref.table, "name": ref.name,
                       "hidden": True})
            )  # fmt: skip
        elif column.get("kind") == "calculated":
            hidden_calculated.append(
                (ref, {"op": "delete_column", "table": ref.table, "name": ref.name})
            )
        else:
            hidden_data.append((ref, None))
    uses = "visual, filter, measure, relationship or hierarchy"

    def build_visible(table: str, objects: list[ObjectRef], fix: Any) -> tuple[str, str, str]:
        return (
            f"{_plural(len(objects), 'column')} of {_quote(table)} "
            f"{'is' if len(objects) == 1 else 'are'} not used by any {uses}: "
            f"{_column_names(objects)}.",
            "Every loaded column makes the file bigger and refreshes slower, and unused ones "
            "crowd the field list.",
            f"Hide {'it' if len(objects) == 1 else 'them'}; to also make the file smaller, "
            "remove them in Power Query (Home > Transform data) instead.",
        )

    def build_data(table: str, objects: list[ObjectRef], fix: Any) -> tuple[str, str, str]:
        calculated_table = model.table(table).get("calculated")
        return (
            f"{_plural(len(objects), 'hidden column')} of {_quote(table)} "
            f"{'is' if len(objects) == 1 else 'are'} not used by any {uses}: "
            f"{_column_names(objects)}.",
            "Hidden columns nobody uses still take memory and refresh time.",
            f"Change the formula of the calculated table {_quote(table)} so it leaves them out."
            if calculated_table
            else "Remove them in Power Query (Home > Transform data) so Power BI stops loading "
            "them.",
        )

    def build_calculated(table: str, objects: list[ObjectRef], fix: Any) -> tuple[str, str, str]:
        return (
            f"{_plural(len(objects), 'hidden calculated column')} of {_quote(table)} "
            f"{'is' if len(objects) == 1 else 'are'} not used by any {uses}: "
            f"{_column_names(objects)}.",
            "A calculated column is computed and stored for every row at each refresh, even "
            "when nothing reads it.",
            f"Delete {'it' if len(objects) == 1 else 'them'}.",
        )

    return (
        _per_table("unused_column", visible, build_visible)
        + _per_table("unused_hidden_column", hidden_data, build_data)
        + _per_table("unused_calculated_column", hidden_calculated, build_calculated)
    )


def _unused_measures(model: _Model) -> list[Finding]:
    visible: list[tuple[ObjectRef, dict[str, Any] | None]] = []
    hidden: list[tuple[ObjectRef, dict[str, Any] | None]] = []
    for name, measure in model.measures.items():
        if model.measure_uses.get(name):
            continue
        ref = model.measure_ref(measure)
        if measure.get("hidden"):
            hidden.append((ref, {"op": "delete_measure", "name": ref.name}))
        else:
            visible.append((ref, None))
    where = "by any visual, filter or other measure in this report"

    def build_visible(table: str, objects: list[ObjectRef], fix: Any) -> tuple[str, str, str]:
        return (
            f"{_plural(len(objects), 'measure')} in {_quote(table)} "
            f"{'is' if len(objects) == 1 else 'are'} not used {where}: {_column_names(objects)}.",
            "Unused measures make the field list longer to search; other reports built on this "
            "model may still need some of them.",
            "Delete the ones nobody needs (Studio can delete a measure for you), or hide them.",
        )

    def build_hidden(table: str, objects: list[ObjectRef], fix: Any) -> tuple[str, str, str]:
        return (
            f"{_plural(len(objects), 'hidden measure')} in {_quote(table)} "
            f"{'is' if len(objects) == 1 else 'are'} not used {where}: {_column_names(objects)}.",
            "A hidden measure that nothing uses cannot be seen or reached, so it only adds to "
            "what the next developer has to read.",
            f"Delete {'it' if len(objects) == 1 else 'them'}.",
        )

    return _per_table("unused_measure", visible, build_visible) + _per_table(
        "unused_hidden_measure", hidden, build_hidden
    )


def _visible_keys(model: _Model) -> list[Finding]:
    items: list[tuple[ObjectRef, dict[str, Any] | None]] = []
    foreign: set[tuple[str, str]] = set()
    primary: set[tuple[str, str]] = set()
    for rel in model.relationships:
        ends = {
            side: (_fold(rel[f"{side}Table"]), _fold(rel[f"{side}Column"]))
            for side in ("from", "to")
        }
        if rel.get("fromCardinality") == "many" and rel.get("toCardinality") == "one":
            foreign.add(ends["from"])
            primary.add(ends["to"])
        elif rel.get("fromCardinality") == "one" and rel.get("toCardinality") == "many":
            foreign.add(ends["to"])
            primary.add(ends["from"])
    for key, column in model.columns.items():
        if key[0] in model.special or model.hidden(column):
            continue
        if key in foreign:
            pass
        elif key in primary and model.usage is not None:
            words = _words(str(column["name"]))
            use = model.usage.columns.get(key)
            if (
                column.get("dataType") != _WHOLE
                or not words
                or words[-1] not in _SURROGATE
                or (use is not None and (use.fields or use.aggregated))
            ):
                continue
        else:
            continue
        ref = model.column_ref(column)
        items.append(
            (ref, {"op": "set_column_properties", "table": ref.table, "name": ref.name,
                   "hidden": True})
        )  # fmt: skip

    def build(table: str, objects: list[ObjectRef], fix: Any) -> tuple[str, str, str]:
        one = len(objects) == 1
        return (
            f"{_plural(len(objects), 'key column')} of {_quote(table)} "
            f"{'is' if one else 'are'} visible in the field list: {_column_names(objects)}.",
            "Keys only connect tables; hiding them points people to the related tables' own "
            "columns, which filter every table as expected.",
            f"Hide {'it' if one else 'them'} (visuals that already use "
            f"{'it' if one else 'them'} keep working).",
        )

    return _per_table("visible_key_column", items, build)


def _summarized_keys(model: _Model) -> list[Finding]:
    items: list[tuple[ObjectRef, dict[str, Any] | None]] = []
    joined = {(_fold(r[f"{s}Table"]), _fold(r[f"{s}Column"])) for r in model.relationships
              for s in ("from", "to")}  # fmt: skip
    sorts = {(_fold(c["table"]), _fold(c["sortByColumn"])) for c in model.columns.values()
             if c.get("sortByColumn")}  # fmt: skip
    for key, column in model.columns.items():
        if column.get("dataType") not in _NUMERIC or key[0] in model.special:
            continue
        if column.get("summarizeBy") not in ("sum", "default") or model.hidden(column):
            continue
        if model.reported_unused(column):
            continue
        words = _words(str(column["name"]))
        if not (key in joined or key in sorts or (words and words[-1] in _KEYISH)):
            continue
        ref = model.column_ref(column)
        items.append(
            (ref, {"op": "set_column_properties", "table": ref.table, "name": ref.name,
                   "summarize_by": "none"})
        )  # fmt: skip

    def build(table: str, objects: list[ObjectRef], fix: Any) -> tuple[str, str, str]:
        one = len(objects) == 1
        what = (
            "it is a key, a year or a sort order" if one else "they are keys, years or sort orders"
        )
        return (
            f"{_plural(len(objects), 'number column')} of {_quote(table)} "
            f"{'adds' if one else 'add'} up when dropped into a visual although {what}: "
            f"{_column_names(objects)}.",
            "Adding up keys, years or sort orders gives meaningless totals (2024 + 2024 = 4048).",
            f"Set Summarize by to None for {'it' if one else 'them'}.",
        )

    return _per_table("summarized_key_column", items, build)


def _high_cardinality(model: _Model) -> list[Finding]:
    found: list[Finding] = []
    if not model.stats:
        return found
    joined = {(_fold(r[f"{s}Table"]), _fold(r[f"{s}Column"])) for r in model.relationships
              for s in ("from", "to")}  # fmt: skip
    for key, column in model.columns.items():
        stats = model.stats.get(key)
        if stats is None or key in joined or key[0] in model.special:
            continue
        if model.reported_unused(column):
            continue
        ref = model.column_ref(column)
        rows = stats.rows - stats.blanks
        if (
            column.get("dataType") == _TEXT
            and stats.distinct >= TEXT_DISTINCT
            and stats.distinct >= NEAR_UNIQUE * rows
        ):
            found.append(
                _finding(
                    "high_cardinality_column",
                    ref.label(),
                    [ref],
                    f"{ref.label()} holds {stats.distinct:,} different texts in "
                    f"{stats.rows:,} rows.",
                    "Text that is nearly different on every row is stored almost once per row "
                    "and is often the largest part of a model.",
                    None,
                    "Keep it only if people filter by it or read it; otherwise remove it in "
                    "Power Query.",
                )
            )
        elif (
            column.get("dataType") == _DATETIME
            and column.get("dateTimeType") != "date"
            and stats.distinct > DAYS_IN_CENTURY
        ):
            found.append(
                _finding(
                    "high_cardinality_column",
                    ref.label(),
                    [ref],
                    f"{ref.label()} holds dates with times of day ({stats.distinct:,} different "
                    "values).",
                    "Every different moment is stored separately; a date column (and, if "
                    "needed, a separate time column) is far smaller.",
                    None,
                    "In Power Query, split it into a date and a time column, or change its type "
                    "to Date if the time is not needed.",
                )
            )
    return found


def _aggregate_only(model: _Model, column: Mapping[str, Any]) -> int | None:
    """How many places add up the calculated column, or None when any use is not a plain
    sum/average/min/max (or cannot be read)."""
    key = (_fold(column["table"]), _fold(column["name"]))
    if model.column_uses.get(key, set()) - {"dax", "report"}:
        return None
    count = 0
    if model.usage is not None:
        use = model.usage.columns.get(key)
        if use is not None:
            if use.fields or use.date_hierarchy:
                return None
            count += use.aggregated
    for formula in model.formulas:
        named = [
            ref
            for ref in dax_references(formula.text)
            if ref.name is not None
            and _fold(ref.name) == key[1]
            and (
                _fold(ref.table) == key[0]
                or (not ref.table and _fold(ref.name) not in model.measures)
            )
        ]
        if not named:
            continue
        tree = model.tree(formula)
        if tree is None:
            return None
        for node, parents in _walk(tree):
            if not isinstance(node, ds.ColRef) or _fold(node.column) != key[1]:
                continue
            if node.table and _fold(node.table) != key[0]:
                continue
            parent = parents[-1] if parents else None
            if isinstance(parent, ds.Call) and (
                (parent.name in _AGGREGATES and parent.args[:1] == (node,))
                or (parent.name in _ITERATORS and parent.args[1:2] == (node,))
            ):
                count += 1
            else:
                return None
    return count


def _calculated_as_measures(model: _Model) -> list[Finding]:
    found: list[Finding] = []
    if not model.uses_known():
        return found
    for column in model.columns.values():
        if column.get("kind") != "calculated" or _fold(column["table"]) in model.special:
            continue
        count = _aggregate_only(model, column)
        if not count:
            continue
        ref = model.column_ref(column)
        found.append(
            _finding(
                "calculated_column_as_measure",
                ref.label(),
                [ref],
                f"The calculated column {ref.label()} is only ever added up "
                f"({_plural(count, 'place')}).",
                "A calculated column is computed and stored for every row at each refresh; a "
                "measure computes the same total only when a visual needs it.",
                None,
                "Replace it with a measure that sums the same formula over the table (SUMX), "
                "then point the visuals at the measure.",
            )
        )
    return found


def _constant_divisor(node: ds.Node) -> bool:
    if isinstance(node, ds.Num):
        return node.value != 0
    if isinstance(node, ds.Unary) and node.op in ("-", "+"):
        return _constant_divisor(node.operand)
    return False


def _filter_over_table(model: _Model, call: ds.Call, variables: set[str]) -> bool:
    """FILTER('T', <condition on T's own columns>) as a CALCULATE filter."""
    if call.name != "FILTER" or len(call.args) != 2 or not isinstance(call.args[0], ds.Name):
        return False
    table = _fold(call.args[0].name)
    if table not in model.tables or table in variables:
        return False
    columns = 0
    for node, _ in _walk(call.args[1]):
        if isinstance(node, ds.ColRef):
            if node.table is not None and _fold(node.table) != table:
                return False
            if (table, _fold(node.column)) not in model.columns:
                return False
            if node.table is None and _fold(node.column) in model.measures:
                return False
            columns += 1
        elif (isinstance(node, ds.Call) and node.name not in _SCALAR) or isinstance(
            node, ds.VarBlock
        ):
            return False
    return columns > 0


def _dax_patterns(model: _Model) -> list[Finding]:
    hits: dict[str, list[tuple[ObjectRef, dict[str, Any] | None]]] = {
        "dax_division": [],
        "dax_iferror": [],
        "dax_filter_table": [],
    }
    for formula in model.formulas:
        if formula.kind not in ("measure", "column", "item"):
            continue
        tree = model.tree(formula)
        if tree is None:
            continue
        variables = {
            _fold(name)
            for node, _ in _walk(tree)
            if isinstance(node, ds.VarBlock)
            for name, _ in node.variables
        }
        found: set[str] = set()
        for node, _ in _walk(tree):
            if isinstance(node, ds.Binary) and node.op == "/" and not _constant_divisor(node.right):
                found.add("dax_division")
            elif isinstance(node, ds.Call) and node.name in ("IFERROR", "ISERROR"):
                found.add("dax_iferror")
            elif isinstance(node, ds.Call) and node.name in ("CALCULATE", "CALCULATETABLE"):
                for argument in node.args[1:]:
                    inner = (
                        argument.args[0]
                        if isinstance(argument, ds.Call)
                        and argument.name == "KEEPFILTERS"
                        and argument.args
                        else argument
                    )
                    if isinstance(inner, ds.Call) and _filter_over_table(model, inner, variables):
                        found.add("dax_filter_table")
        if formula.kind == "measure":
            ref = ObjectRef("measure", formula.table, formula.name)
        elif formula.kind == "column":
            ref = ObjectRef("column", formula.table, formula.name)
        else:
            ref = ObjectRef("calculation_item", formula.table, formula.name)
        for rule in sorted(found):
            if ref not in [r for r, _ in hits[rule]]:
                hits[rule].append((ref, None))
    # (what the formulas do: one, many), why, what to do.
    texts = {
        "dax_division": (
            ("divides with / instead of DIVIDE", "divide with / instead of DIVIDE"),
            "/ returns Infinity or NaN when the divisor is zero or blank; DIVIDE returns blank "
            "(or a value you choose) instead.",
            "Ask Studio to rewrite {them} with DIVIDE.",
        ),
        "dax_iferror": (
            ("uses IFERROR or ISERROR", "use IFERROR or ISERROR"),
            "IFERROR and ISERROR stop Power BI from calculating in bulk, so every visual that "
            "uses them gets slower; checking the inputs (or DIVIDE) avoids the error instead.",
            "Ask Studio to rewrite {them} without IFERROR or ISERROR.",
        ),
        "dax_filter_table": (
            (
                "filters a whole table inside CALCULATE where a column filter would do",
                "filter a whole table inside CALCULATE where a column filter would do",
            ),
            "FILTER over a table walks every row and column of it; a condition on the column "
            "itself (inside KEEPFILTERS to keep the same meaning) is faster.",
            "Ask Studio to rewrite {them} with column filters.",
        ),
    }
    findings: list[Finding] = []
    for rule, items in hits.items():
        does, why, summary = texts[rule]

        def build(
            table: str,
            objects: list[ObjectRef],
            fix: Any,
            does: tuple[str, str] = does,
            why: str = why,
            summary: str = summary,
        ) -> tuple[str, str, str]:
            noun = "measure" if {o.kind for o in objects} == {"measure"} else "formula"
            one = len(objects) == 1
            return (
                f"{_plural(len(objects), noun)} in {_quote(table)} {does[0 if one else 1]}: "
                f"{_names([o.name or '' for o in objects])}.",
                why,
                summary.format(them="it" if one else "them"),
            )

        findings += _per_table(rule, items, build)
    return findings


def _measure_formats(model: _Model) -> list[Finding]:
    items: list[tuple[ObjectRef, dict[str, Any] | None]] = []
    for measure in model.measures.values():
        if measure.get("hidden") or measure.get("dataType") not in (_DOUBLE, _FIXED):
            continue
        if measure.get("formatString") or measure.get("formatStringExpression"):
            continue
        items.append((model.measure_ref(measure), None))

    def build(table: str, objects: list[ObjectRef], fix: Any) -> tuple[str, str, str]:
        one = len(objects) == 1
        return (
            f"{_plural(len(objects), 'measure')} in {_quote(table)} "
            f"{'has' if one else 'have'} no format string: {_column_names(objects)}.",
            "Without a format string, decimal results show with all their digits in tables, "
            "tooltips and exports unless each visual sets a format of its own.",
            f"Give {'it' if one else 'each'} a format (a currency, a percentage or #,0.00) in "
            "the measure's properties, or ask Studio, for example: format "
            f"{objects[0].name} as a whole number.",
        )

    return _per_table("measure_no_format", items, build)


def _measure_folders(model: _Model) -> list[Finding]:
    by_table: dict[str, list[dict[str, Any]]] = {}
    for measure in model.measures.values():
        if not measure.get("hidden"):
            by_table.setdefault(str(measure.get("table") or ""), []).append(measure)
    items: list[tuple[ObjectRef, dict[str, Any] | None]] = []
    totals: dict[str, int] = {}
    for table, measures in by_table.items():
        if len(measures) <= FOLDER_THRESHOLD:
            continue
        totals[table] = len(measures)
        items += [(model.measure_ref(m), None) for m in measures if not m.get("displayFolder")]

    def build(table: str, objects: list[ObjectRef], fix: Any) -> tuple[str, str, str]:
        return (
            f"{len(objects)} of the {totals[table]} measures in {_quote(table)} "
            f"{'is' if len(objects) == 1 else 'are'} not in a display folder: "
            f"{_column_names(objects)}.",
            f"With more than {FOLDER_THRESHOLD} measures in one table, folders make the field "
            "list much easier to browse.",
            "Put them in display folders (the measure's properties), or ask Studio, for "
            f"example: move {objects[0].name} to the folder Base measures.",
        )

    return _per_table("measure_folders", items, build)


def _descriptions(model: _Model) -> list[Finding]:
    tables = [
        ObjectRef("table", str(t["name"]), str(t["name"]))
        for key, t in model.tables.items()
        if not (t.get("hidden") or t.get("private") or model.auto_date(key))
        and not t.get("description")
    ]
    columns = [
        model.column_ref(c)
        for key, c in model.columns.items()
        if not model.hidden(c) and not model.auto_date(key[0]) and not c.get("description")
    ]
    measures = [
        model.measure_ref(m)
        for m in model.measures.values()
        if not m.get("hidden") and not m.get("description")
    ]
    objects = tables + columns + measures
    if not objects:
        return []
    parts = [
        _plural(len(group), noun)
        for group, noun in ((tables, "table"), (columns, "column"), (measures, "measure"))
        if group
    ]
    listing = ", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0]
    return [
        _finding(
            "missing_descriptions",
            "",
            objects,
            f"{listing} that people see in the field list "
            f"{'has' if len(objects) == 1 else 'have'} no description.",
            "A description shows as a tooltip in the field list and helps people pick the "
            "right field.",
            None,
            "Add descriptions to the fields people use most, starting with the measures on "
            "the report's pages.",
        )
    ]


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", _CONTROL.sub(" ", name)).strip()


def _naming(model: _Model) -> list[Finding]:
    items: list[tuple[ObjectRef, dict[str, Any] | None]] = []
    taken = (
        {("table", k) for k in model.tables}
        | {("measure", k) for k in model.measures}
        | {("column", f"{k[0]}[{k[1]}]") for k in model.columns}
    )

    def dirty(name: str) -> bool:
        return name != name.strip() or bool(_CONTROL.search(name))

    for table in model.tables.values():
        name = str(table["name"])
        if dirty(name) and not model.auto_date(name):
            new = _clean_name(name)
            op = None if ("table", _fold(new)) in taken or not new else {
                "op": "rename_table", "name": name, "new_name": new}  # fmt: skip
            items.append((ObjectRef("table", name, name), op))
    for measure in model.measures.values():
        name = str(measure["name"])
        if dirty(name):
            new = _clean_name(name)
            op = None if ("measure", _fold(new)) in taken or not new else {
                "op": "rename_measure", "name": name, "new_name": new}  # fmt: skip
            items.append((model.measure_ref(measure), op))
    for key, column in model.columns.items():
        name = str(column["name"])
        if dirty(name) and not model.auto_date(key[0]):
            new = _clean_name(name)
            clash = ("column", f"{key[0]}[{_fold(new)}]") in taken
            op = None if clash or not new else {
                "op": "rename_column", "table": column["table"], "name": name,
                "new_name": new}  # fmt: skip
            items.append((model.column_ref(column), op))

    def build(table: str, objects: list[ObjectRef], fix: Any) -> tuple[str, str, str]:
        one = len(objects) == 1
        labels = [repr(o.name) for o in objects]
        return (
            f"{_plural(len(objects), 'name')} in {_quote(table)} "
            f"{'starts or ends' if one else 'start or end'} with spaces or "
            f"{'holds' if one else 'hold'} tabs or line breaks: {_names(labels)}.",
            "Stray spaces make names look alike while formulas and searches treat them as "
            "different.",
            f"Rename {'it' if one else 'them'} without the extra spaces."
            if fix
            else "Rename them without the extra spaces (another object already has the clean "
            "name, so pick a new one).",
        )

    found = _per_table("name_whitespace", items, build)
    groups: dict[str, list[ObjectRef]] = {}
    for table in model.tables.values():
        if not model.auto_date(table["name"]):
            groups.setdefault(_fold(str(table["name"]).strip()), []).append(
                ObjectRef("table", str(table["name"]), str(table["name"]))
            )
    for measure in model.measures.values():
        groups.setdefault(_fold(str(measure["name"]).strip()), []).append(
            model.measure_ref(measure)
        )
    for key, column in model.columns.items():
        if not model.auto_date(key[0]):
            groups.setdefault(_fold(str(column["name"]).strip()), []).append(
                model.column_ref(column)
            )
    for name, objects in groups.items():
        spellings = list(dict.fromkeys(str(o.name).strip() for o in objects))
        if len(spellings) < 2:
            continue
        labels = [o.label() for o in objects]
        found.append(
            _finding(
                "name_case_clash",
                name,
                objects,
                f"{_names(labels)} have the same name spelled differently "
                f"({', '.join(spellings)}).",
                "Power BI does not tell names apart by case, so two spellings of one name are "
                "confusing in formulas and in the field list.",
                None,
                "Rename one of them so the name is spelled the same everywhere.",
            )
        )
    return found


_CHECKS: tuple[Callable[[_Model], list[Finding]], ...] = (
    _broken_objects,
    _relationship_rules,
    _time_intelligence,
    _floating_point_columns,
    _auto_date_time,
    _high_cardinality,
    _calculated_as_measures,
    _dax_patterns,
    _visible_keys,
    _summarized_keys,
    _measure_folders,
    _descriptions,
    _measure_formats,
    _naming,
)
_USAGE_CHECKS: tuple[Callable[[_Model], list[Finding]], ...] = (_unused_columns, _unused_measures)
_SEVERITY_ORDER = {"problem": 0, "suggestion": 1}


def check_model(
    facts: Mapping[str, Any] | None,
    *,
    usage: Usage | None,
    stats: Mapping[tuple[str, str], ColumnStats] | None = None,
) -> Health:
    """Check the model the ``facts`` describe (``pbix_extract.read_model_facts``: the full facts,
    with measure DAX). ``usage``: how the report uses it (``report_usage``), None when unknown;
    ``stats``: column statistics keyed by folded (table, column) (``stats_from_frames``).

    Findings come in a fixed order: problems first, then by category (correctness,
    performance, usability, formatting, naming), rule and id."""
    if facts is None or not (
        isinstance(facts.get("tables"), list) and isinstance(facts.get("columns"), list)
    ):
        return Health(
            False,
            [],
            {},
            ["Studio could not read this model's tables and columns, so it checked nothing."],
        )
    model = _Model(facts, usage, stats)
    findings: list[Finding] = []
    for check in _CHECKS:
        findings += check(model)
    notes: list[str] = []
    if model.uses_known():
        for check in _USAGE_CHECKS:
            findings += check(model)
    elif model.usage is None:
        notes.append(
            "Studio did not read the report, so it did not look for columns and measures that "
            "nothing uses."
        )
    else:
        notes.append(
            "Some formulas were not available in full, so Studio did not look for columns, "
            "measures and inactive relationships that nothing uses."
        )
    if not isinstance(facts.get("relationships"), list):
        notes.append("Studio could not read the relationships, so it did not check them.")
    if not stats:
        notes.append(
            "Studio has no statistics of the data, so it did not check relationship keys for "
            "blanks and repeats or columns for too many different values."
        )
    unread = sorted(
        {f.name for f in model.formulas if f.kind == "measure" and model.tree(f) is None}
    )
    if unread:
        notes.append(
            f"Studio could not read the formula of {_plural(len(unread), 'measure')} "
            f"({_names(unread)}), so the formula checks skipped "
            f"{'it' if len(unread) == 1 else 'them'}."
        )
    findings.sort(
        key=lambda f: (
            _SEVERITY_ORDER[f.severity],
            CATEGORIES.index(f.category),
            _RULE_ORDER[f.rule],
            f.id,
        )
    )
    checked = {
        "tables": len(model.tables),
        "columns": len(model.columns),
        "measures": len(model.measures),
        "relationships": len(model.relationships),
    }
    return Health(True, findings, checked, notes)


# --- chat -------------------------------------------------------------------------------------

CHAT_TRIGGERS: tuple[str, ...] = (
    "model health",
    "health check",
    "check the model",
    "check my model",
    "check this model",
    "review the model",
    "review my model",
    "best practice",
    "best practices",
    "best practice analyzer",
    "bpa",
    "model problems",
    "problems in the model",
    "what's wrong with the model",
    "what is wrong with the model",
    "improve the model",
    "optimize the model",
    "optimise the model",
)
_TRIGGER = re.compile(
    r"(?<![\w'])(?:"
    + "|".join(re.escape(t).replace(r"\ ", r"\s+") for t in CHAT_TRIGGERS)
    + r")(?![\w'])",
    re.IGNORECASE,
)


def is_health_request(message: str) -> bool:
    """Whether a chat message asks for the model's health check."""
    return bool(_TRIGGER.search(message.replace("’", "'")))


def describe(health: Health, *, limit: int = 8) -> str:
    """The health check in plain words for chat: a count line, then the first ``limit``
    findings (problems first), then what was not checked."""
    if not health.known:
        return health.notes[0] if health.notes else "Studio could not read this model."
    checked = health.checked
    summary = health.summary
    head = (
        f"Studio checked {_plural(checked.get('tables', 0), 'table')}, "
        f"{_plural(checked.get('columns', 0), 'column')}, "
        f"{_plural(checked.get('measures', 0), 'measure')} and "
        f"{_plural(checked.get('relationships', 0), 'relationship')}: "
    )
    if not health.findings:
        lines = [head + "nothing to fix."]
    else:
        problems = summary["problems"]
        lines = [
            head
            + ("no problems" if not problems else _plural(problems, "problem"))
            + f" and {_plural(summary['suggestions'], 'suggestion')}."
        ]
        for finding in health.findings[:limit]:
            marker = "Problem" if finding.severity == "problem" else "Suggestion"
            lines.append(f"- {marker}: {finding.message}")
        rest = len(health.findings) - limit
        if rest > 0:
            lines.append(f"...and {rest} more in the model health list.")
    lines += health.notes
    return "\n".join(lines)


def findings_for(health: Health, objects: Iterable[ObjectRef]) -> list[Finding]:
    """The findings that name any of ``objects`` (same kind, table and name, folded): for a
    model edit's receipt, or the Fields pane's badges."""
    wanted = {(o.kind, _fold(o.table), _fold(o.name)) for o in objects}
    return [
        f
        for f in health.findings
        if any((o.kind, _fold(o.table), _fold(o.name)) in wanted for o in f.objects)
    ]
