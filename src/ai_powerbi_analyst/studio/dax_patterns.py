"""Business calculations compiled from closed choices, never model-written DAX.

The compiler is independent of evaluation. Its notes describe filter semantics and
requirements; only the caller's existing DAX evaluation receipt proves a result.
Catalog numbers are request scoped and retain their identities when a prompt is trimmed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, cast

from pydantic import Field

from ai_powerbi_analyst.contracts import ClosedModel

from .chat_context import m_prompt_text
from .dax_syntax import Binary, Call, ColRef, Name, Node, Unary, VarBlock, parse_expression

if TYPE_CHECKING:
    from .dax_engine import Model
    from .edit_context import EditContext
    from .report_import import ImportedReport

PatternId = Literal[
    "ratio",
    "share_total",
    "fixed_grain",
    "period_to_date",
    "percent_change",
    "running_total",
    "moving_average",
    "rank",
    "top_n",
    "distinct_count",
    "average_per",
    "target_actual",
    "previous_period",
]
Aggregation = Literal[
    "sum", "average", "minimum", "maximum", "count", "distinct_count", "count_rows"
]
Period = Literal["day", "month", "quarter", "year"]


class MetricSelection(ClosedModel):
    field: int = Field(ge=1, strict=True)
    aggregation: Aggregation | None = None


class PatternSelection(ClosedModel):
    """The only information a local model may choose. No expression/code slot."""

    worked_examples: ClassVar[bool] = False
    pattern: PatternId | None
    base: MetricSelection | None = None
    numerator: MetricSelection | None = None
    denominator: MetricSelection | None = None
    actual: MetricSelection | None = None
    target: MetricSelection | None = None
    date: int | None = Field(default=None, ge=1, strict=True)
    group: int | None = Field(default=None, ge=1, strict=True)
    entity: int | None = Field(default=None, ge=1, strict=True)
    period: Period | None = None
    window: int | None = Field(default=None, ge=1, le=366, strict=True)
    n: int | None = Field(default=None, ge=1, le=100, strict=True)
    scope: Literal["all", "selected"] | None = None
    direction: Literal["ascending", "descending"] | None = None
    ties: Literal["dense", "skip"] | None = None
    comparison: Literal["variance", "variance_percent", "attainment"] | None = None
    previous_mode: Literal["shifted", "calendar"] | None = None


_PATTERN_SLOTS: dict[str, set[str]] = {
    "ratio": {"numerator", "denominator"},
    "share_total": {"base", "group", "scope"},
    "fixed_grain": {"base", "group"},
    "period_to_date": {"base", "date", "period"},
    "percent_change": {"base", "date", "period", "comparison"},
    "running_total": {"base", "date", "scope"},
    "moving_average": {"base", "date", "period", "window"},
    "rank": {"base", "group", "scope", "direction", "ties"},
    "top_n": {"base", "group", "scope", "direction", "n"},
    "distinct_count": {"entity"},
    "average_per": {"base", "entity"},
    "target_actual": {"actual", "target", "comparison"},
    "previous_period": {"base", "date", "period", "previous_mode"},
}


@dataclass(frozen=True)
class PatternField:
    index: int
    kind: Literal["measure", "column", "table"]
    table: str
    name: str
    data_type: str = "unknown"
    source_tables: tuple[str, ...] = ()
    available: bool = True
    unavailable_reason: str = ""
    date_key: bool = False
    calendar_contiguous: bool | None = None
    format_string: str = ""

    @property
    def reference(self) -> str:
        table = "'" + self.table.replace("'", "''") + "'"
        name = "[" + self.name.replace("]", "]]") + "]"
        return name if self.kind == "measure" else table if self.kind == "table" else table + name


@dataclass(frozen=True)
class PatternRelationship:
    many_table: str
    one_table: str
    active: bool = True


@dataclass(frozen=True)
class PatternCatalog:
    fields: tuple[PatternField, ...]
    relationships: tuple[PatternRelationship, ...] = ()
    relationships_complete: bool | None = None
    revision: str | None = None

    def field(self, index: int | None, slot: str = "field") -> PatternField:
        found = next((field for field in self.fields if field.index == index), None)
        if found is None:
            raise PatternError(f"Choose a known {slot} from this model.", slot)
        if not found.available:
            raise PatternError(
                found.unavailable_reason or f"{found.reference} is unavailable in the local model.",
                slot,
            )
        return found


class PatternError(ValueError):
    def __init__(self, message: str, slot: str = "", choices: Sequence[int] = ()) -> None:
        super().__init__(message)
        self.slot = slot
        self.choices = tuple(choices)


@dataclass(frozen=True)
class BuiltPattern:
    expression: str
    kind: Literal["scalar", "query"]
    format_string: str
    dependencies: tuple[PatternField, ...]
    notes: tuple[str, ...]


def _dtype(value: str) -> str:
    folded = value.casefold()
    if any(word in folded for word in ("date", "time")):
        return "date"
    if any(word in folded for word in ("int", "float", "double", "decimal", "number", "currency")):
        return "number"
    if "bool" in folded:
        return "boolean"
    if any(word in folded for word in ("str", "object", "text")):
        return "string"
    return "unknown"


def _source_tables(
    expression: str, measures: Mapping[str, str], seen: frozenset[str]
) -> tuple[str, ...]:
    """Resolve dependencies locally; formulas are never included in model prompts."""
    found: set[str] = set()

    def walk(node: Node) -> None:
        if isinstance(node, ColRef):
            if node.table:
                found.add(node.table)
            elif node.column.casefold() not in seen:
                name = node.column.casefold()
                if name in measures:
                    found.update(_source_tables(measures[name], measures, seen | {name}))
        elif isinstance(node, Call):
            if (
                node.name in {"COUNTROWS", "SUMX", "AVERAGEX", "MINX", "MAXX"}
                and node.args
                and isinstance(node.args[0], Name)
            ):
                found.add(node.args[0].name)
            for arg in node.args:
                walk(arg)
        elif isinstance(node, Binary):
            walk(node.left)
            walk(node.right)
        elif isinstance(node, Unary):
            walk(node.operand)
        elif isinstance(node, VarBlock):
            for _, value in node.variables:
                walk(value)
            walk(node.body)

    try:
        walk(parse_expression(expression))
    except ValueError:
        return ()
    return tuple(sorted(found, key=str.casefold))


def catalog_from_context(
    context: EditContext,
    *,
    relationships: Iterable[PatternRelationship] = (),
    relationships_complete: bool | None = None,
    date_keys: Iterable[tuple[str, str]] = (),
    unavailable: Mapping[tuple[str, str], str] | None = None,
) -> PatternCatalog:
    """Names/types from EditContext, with separately supplied relationship/date evidence.

    EditContext itself does not prove relationships or that a date is a calendar key.
    Prefer catalog_from_model for imported local evaluation, including unavailable fields.
    """
    keys = {(table.casefold(), column.casefold()) for table, column in date_keys}
    blocked = {
        (table.casefold(), name.casefold()): why
        for (table, name), why in (unavailable or {}).items()
    }
    formulas = {measure.name.casefold(): measure.expression for measure in context.measures}
    fields: list[PatternField] = []
    for measure in context.measures:
        reason = blocked.get((measure.table.casefold(), measure.name.casefold()), "")
        fields.append(
            PatternField(
                len(fields) + 1,
                "measure",
                measure.table,
                measure.name,
                source_tables=_source_tables(
                    measure.expression, formulas, frozenset({measure.name.casefold()})
                ),
                available=not reason,
                unavailable_reason=reason,
                format_string=measure.format_string or "",
            )
        )
    for table, columns in context.tables.items():
        for column in columns:
            reason = blocked.get((table.casefold(), column.casefold()), "")
            fields.append(
                PatternField(
                    len(fields) + 1,
                    "column",
                    table,
                    column,
                    _dtype(context.column_types.get(f"{table}.{column}", "")),
                    (table,),
                    not reason,
                    reason,
                    (table.casefold(), column.casefold()) in keys,
                )
            )
        fields.append(PatternField(len(fields) + 1, "table", table, table, source_tables=(table,)))
    return PatternCatalog(
        tuple(fields), tuple(relationships), relationships_complete, context.head_revision
    )


def _calendar(values: Any) -> bool:
    try:
        days = [date.fromisoformat(str(value)[:10]) for value in values]
    except (TypeError, ValueError):
        return False
    ordered = sorted(set(days))
    return (
        bool(ordered)
        and len(ordered) == len(days)
        and all(
            later - earlier == timedelta(days=1)
            for earlier, later in zip(ordered, ordered[1:], strict=False)
        )
    )


def catalog_from_model(model: Model, *, revision: str | None = None) -> PatternCatalog:
    """The report_engine model is the authority for joins, dates and local availability."""
    from .report_engine import unavailable_reference

    formulas = {measure.name.casefold(): measure.text for measure in model.measures.values()}
    fields: list[PatternField] = []
    for measure in model.measures.values():
        item = PatternField(
            len(fields) + 1,
            "measure",
            measure.table,
            measure.name,
            source_tables=_source_tables(
                measure.text, formulas, frozenset({measure.name.casefold()})
            ),
            format_string=measure.format_string,
        )
        why = unavailable_reference(model, f'EVALUATE ROW("Value", {item.reference})')
        fields.append(replace(item, available=why is None, unavailable_reason=why or ""))
    for table_key, table in model.tables.items():
        for column_key, column in table.columns.items():
            key = (table_key, column_key) in model.date_keys
            item = PatternField(
                len(fields) + 1,
                "column",
                table.name,
                column.name,
                _dtype(column.kind),
                (table.name,),
                date_key=key,
                calendar_contiguous=_calendar(column.values) if key else None,
            )
            why = unavailable_reference(model, f"EVALUATE VALUES({item.reference})")
            fields.append(replace(item, available=why is None, unavailable_reason=why or ""))
        item = PatternField(
            len(fields) + 1, "table", table.name, table.name, source_tables=(table.name,)
        )
        why = unavailable_reference(model, f"EVALUATE {item.reference}")
        fields.append(replace(item, available=why is None, unavailable_reason=why or ""))
    # Unbuilt columns/tables have no EngineColumn; retain their names as unavailable choices.
    for (table, column), why in getattr(model, "unavailable", {}).items():
        if not any(
            field.table.casefold() == table
            and field.kind == ("column" if column is not None else "table")
            and (column is None or field.name.casefold() == column)
            for field in fields
        ):
            fields.append(
                PatternField(
                    len(fields) + 1,
                    "column" if column is not None else "table",
                    table,
                    column or table,
                    available=False,
                    unavailable_reason=why,
                )
            )
    joins = tuple(PatternRelationship(rel.many, rel.one, rel.active) for rel in model.relationships)
    return PatternCatalog(tuple(fields), joins, True, revision)


def catalog_for_native_proposal(
    report: ImportedReport,
    *,
    local: PatternCatalog | None = None,
    revision: str | None = None,
) -> PatternCatalog:
    """Current-head names for proposal-only DAX; never evidence of executable local rows.

    Local choices keep their availability. Only explicitly recognized non-Import tables
    can add metadata-only choices. The caller must neither preview nor remember a local
    result from this catalog: Power BI must validate a proposal that uses native choices.
    """
    from .dax_pattern_metadata import catalog_for_native_proposal as from_metadata

    return from_metadata(report, local=local, revision=revision)


def _metric(
    selection: MetricSelection | None, catalog: PatternCatalog, slot: str
) -> tuple[str, PatternField]:
    if selection is None:
        raise PatternError(f"Choose the {slot} for this calculation.", slot)
    item = catalog.field(selection.field, slot)
    if item.kind == "measure":
        if selection.aggregation is not None:
            raise PatternError(
                "Use an existing measure directly, without a column aggregation.", slot
            )
        return item.reference, item
    if item.kind == "table":
        if selection.aggregation != "count_rows":
            raise PatternError("A table can supply a row count, not a numeric column value.", slot)
        return f"COUNTROWS({item.reference})", item
    aggregate = selection.aggregation
    if aggregate is None or aggregate == "count_rows":
        raise PatternError(f"Choose how to aggregate {item.reference}.", slot)
    if aggregate in {"sum", "average", "minimum", "maximum"} and item.data_type != "number":
        raise PatternError(f"{item.reference} needs a reviewed numeric type for {aggregate}.", slot)
    function = {
        "sum": "SUM",
        "average": "AVERAGE",
        "minimum": "MIN",
        "maximum": "MAX",
        "count": "COUNTA",
        "distinct_count": "DISTINCTCOUNT",
    }[aggregate]
    return f"{function}({item.reference})", item


def _column(index: int | None, catalog: PatternCatalog, slot: str) -> PatternField:
    item = catalog.field(index, slot)
    if item.kind != "column":
        raise PatternError(f"Choose a column for the {slot}.", slot)
    return item


def _reaches(group: PatternField, metric: PatternField, catalog: PatternCatalog) -> None:
    if not metric.source_tables:
        raise PatternError(
            f"Review which data tables {metric.reference} reads before using {group.reference}.",
            "group",
        )
    for fact in metric.source_tables:
        if fact.casefold() == group.table.casefold():
            continue
        if catalog.relationships_complete is not True:
            raise PatternError(
                "Review the model relationships before applying this calculation's context.",
                "group",
            )
        pending, seen = [fact.casefold()], set()
        while pending:
            table = pending.pop()
            if table in seen:
                continue
            seen.add(table)
            pending.extend(
                rel.one_table.casefold()
                for rel in catalog.relationships
                if rel.active and rel.many_table.casefold() == table
            )
        if group.table.casefold() not in seen:
            raise PatternError(
                f"No active relationship lets {group.reference} filter '{fact}'.", "group"
            )


def _date(
    selection: PatternSelection, metric: PatternField, catalog: PatternCatalog
) -> PatternField:
    item = _column(selection.date, catalog, "date")
    if item.data_type != "date" or not item.date_key:
        raise PatternError("Choose the related calendar's date key for time intelligence.", "date")
    if item.calendar_contiguous is False:
        raise PatternError("Time intelligence needs a unique, complete daily calendar.", "date")
    _reaches(item, metric, catalog)
    return item


def _required(value: Any, slot: str, message: str) -> Any:
    if value is None:
        raise PatternError(message, slot)
    return value


def _calculation_text(goal: str) -> str:
    declaration = re.search(
        r"\b(?:create|make|add|write)\b.*?\bmeasure\b|"
        r"\b(?:measure|calculation|formula)\s+(?:called|named)\b",
        goal,
        re.I,
    )
    if declaration:
        named = goal[declaration.end() :]
        # The first definition delimiter separates a new measure's name from its
        # calculation. Quoted names can themselves contain "for" or a colon.
        tokens = re.finditer(
            r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`|\[[^\]]*\]|"
            r"(?P<body>\b(?:that is|which is|defined as|that calculates|which calculates|"
            r"(?:that|which)\s+(?=(?:sums?|averages?|counts?|returns?)\b)|"
            r"equal to|for)\b|:)",
            named,
            re.I,
        )
        for token in tokens:
            if token.group("body"):
                return named[token.end() :].strip()
    return goal


def _words(text: str) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return " ".join(re.findall(r"\w+", text.casefold()))


def _references(field: PatternField) -> tuple[str, ...]:
    """DAX allows an unquoted simple table identifier as well as its quoted spelling."""
    if field.kind == "column" and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field.table):
        return field.reference, field.table + field.reference[field.reference.index("[") :]
    return (field.reference,)


def _mentions_reference(text: str, field: PatternField) -> bool:
    return any(
        re.search(r"(?<![\w'])" + re.escape(reference) + r"(?!\w)", text, re.I)
        for reference in _references(field)
    )


def _qualified_fields(text: str, catalog: PatternCatalog) -> list[PatternField]:
    qualified: dict[int, PatternField] = {}
    for match in re.finditer(
        r"(?<![\w'])(?P<table>'(?:[^']|'')+'|[A-Za-z_]\w*)\s*"
        r"\[(?P<name>(?:[^\]]|\]\])+?)\]",
        text,
    ):
        raw_table = match["table"]
        table = (
            raw_table[1:-1].replace("''", "'") if raw_table.startswith("'") else raw_table
        ).casefold()
        name = match["name"].replace("]]", "]").casefold()
        named = [
            field
            for field in catalog.fields
            if field.kind in {"column", "measure"}
            and field.name.casefold() == name
            and field.table.casefold() == table
        ]
        if not named:
            raise PatternError(
                "Choose a known table and field; this qualified reference is unknown.", "field"
            )
        qualified.update({field.index: field for field in named})
    return list(qualified.values())


def resolve_field(
    text: str,
    catalog: PatternCatalog,
    *,
    kinds: Sequence[str] = ("measure", "column"),
    references: PatternCatalog | None = None,
) -> PatternField:
    qualified = _qualified_fields(text, references or catalog)
    eligible = {field.index for field in catalog.fields if field.kind in kinds}
    qualified = [field for field in qualified if field.index in eligible]
    explicit = qualified or [
        field
        for field in catalog.fields
        if field.kind in kinds and _mentions_reference(text, field)
    ]
    # A qualified column also contains a bare [name]; prefer the full qualified spelling.
    if any(field.kind == "column" for field in explicit):
        explicit = [field for field in explicit if field.kind == "column"]
    candidates = explicit or [
        field
        for field in catalog.fields
        if field.kind in kinds
        and re.search(r"(?:^| )" + re.escape(_words(field.name)) + r"(?: |$)", _words(text))
    ]
    if not candidates:
        raise PatternError(f"Name the model field for '{text.strip()}'.", "field")
    if not explicit:
        length = max(len(_words(field.name)) for field in candidates)
        candidates = [field for field in candidates if len(_words(field.name)) == length]
    if len(candidates) != 1:
        raise PatternError(
            "Choose the intended field; this name matches several model fields.",
            "field",
            [field.index for field in candidates],
        )
    return catalog.field(candidates[0].index)


def _resolve_metric(
    text: str, catalog: PatternCatalog, *, references: PatternCatalog | None = None
) -> MetricSelection:
    item = resolve_field(text, catalog, references=references)
    if item.kind == "measure":
        return MetricSelection(field=item.index)
    aggregate: Aggregation = (
        "distinct_count"
        if re.search(r"\b(?:distinct|unique)\b", text, re.I)
        else "count"
        if re.search(r"\bcount\b", text, re.I)
        else "average"
        if re.search(r"\b(?:average|avg|mean)\b", text, re.I)
        else "minimum"
        if re.search(r"\b(?:minimum|min)\b", text, re.I)
        else "maximum"
        if re.search(r"\b(?:maximum|max)\b", text, re.I)
        else "sum"
    )
    return MetricSelection(field=item.index, aggregation=aggregate)


def _metric_clause_remainder(text: str, field: PatternField) -> str:
    identities = sorted({field.name, *_references(field)}, key=len, reverse=True)
    for identity in identities:
        remainder, count = re.subn(
            r"(?<!\w)" + re.escape(identity) + r"(?!\w)", " ", text, count=1, flags=re.I
        )
        if count:
            break
    else:
        # Match a spaced spelling of a camel-case field, retaining every other
        # token and operator. Do not erase repeated references or arithmetic.
        spelling = r"\s*".join(map(re.escape, _words(field.name).split()))
        remainder = re.sub(r"(?<!\w)" + spelling + r"(?!\w)", " ", text, count=1, flags=re.I)
    if re.search(r"[-+*/<>=]", remainder):
        raise PatternError("Resolve the requested operand arithmetic before calculating.", "base")
    return _words(remainder)


def _plain_field_clause(text: str, field: PatternField) -> bool:
    clause = re.sub(r"^the\s+", "", text.strip(), flags=re.I)
    return any(
        re.fullmatch(re.escape(identity).replace(r"\[", r"\s*\["), clause, re.I)
        for identity in (field.name, *_references(field))
    )


def _ratio_parts(goal: str) -> tuple[str, str] | None:
    text = _calculation_text(goal)
    parts = re.split(r"\bdivided\s+by\b", text, maxsplit=1, flags=re.I)
    if len(parts) == 2:
        return parts[0], parts[1]
    ratio = re.search(r"\bratio\s+of\s+(.+?)\s+to\s+(.+)", text, re.I)
    return (ratio[1], ratio[2]) if ratio else None


def _pattern_text(goal: str, catalog: PatternCatalog) -> str:
    text = _calculation_text(goal)
    blank = re.search(
        r"(?:[.;]\s*)?\breturn\s+blank(?:\(\))?\s+(?:when|if)\s+(.+?)\s+"
        r"(?:is\s+(?:zero|0)|=\s*0)\s*\.?$",
        text,
        re.I,
    )
    if blank is None:
        return text
    calculation = text[: blank.start()].strip(" .;")
    parts = _ratio_parts(calculation)
    if parts is None:
        return text
    denominator = _resolve_metric(parts[1], catalog)
    field = catalog.field(denominator.field)
    condition = _words(blank[1]).removeprefix("the ")
    if condition not in {"denominator", _words(field.name), *map(_words, _references(field))}:
        return text
    # DIVIDE already returns BLANK for a zero denominator. Only this exact
    # denominator instruction is represented; a filter on other rows still clarifies.
    return calculation


def _ground_ratio(selection: PatternSelection, catalog: PatternCatalog, goal: str) -> None:
    parts = _ratio_parts(_pattern_text(goal, catalog))
    if parts:
        for slot, text in zip(("numerator", "denominator"), parts, strict=True):
            expected = _resolve_metric(text, catalog)
            field = catalog.field(expected.field)
            remainder = re.sub(re.escape(_words(field.name)), " ", _words(text), flags=re.I)
            if re.search(
                r"\b(?:ytd|qtd|mtd|yoy|qoq|mom|cumulative|running|moving|previous|prior)\b",
                remainder,
            ):
                raise PatternError(
                    "Resolve the requested period calculation for each ratio operand first.", slot
                )
            remaining = _metric_clause_remainder(text, field)
            allowed = {
                "calculate",
                "the",
                "of",
                "total",
                "with",
                "percent",
                "percentage",
                "currency",
                "format",
                "formatting",
            }
            if field.kind == "column":
                allowed |= {
                    "sum",
                    "summed",
                    "average",
                    "avg",
                    "mean",
                    "count",
                    "distinct",
                    "unique",
                    "minimum",
                    "min",
                    "maximum",
                    "max",
                }
            if set(remaining.split()) - allowed:
                raise PatternError("Resolve the requested ratio operand calculation first.", slot)
            actual = getattr(selection, slot)
            if actual != expected:
                raise PatternError(
                    f"The {slot} must use the field and aggregation named in that clause.",
                    slot,
                    (expected.field,),
                )


def _period_change_comparison(
    goal: str,
    catalog: PatternCatalog,
    base: MetricSelection | None,
) -> Literal["variance", "variance_percent"] | None:
    text = _pattern_text(goal, catalog)
    if base is not None:
        item = catalog.field(base.field, "base")
        for identity in (item.reference, item.name):
            text = re.sub(
                r"(?<!\w)" + re.escape(identity) + r"(?!\w)",
                " ",
                text,
                flags=re.I,
            )
    words = _words(text)
    absolute = bool(
        re.search(
            r"\b(?:absolute|currency|dollars?|amount|numeric)\s+(?:change|difference|variance)\b|"
            r"\b(?:change|difference|variance)\s+(?:in\s+)?(?:currency|dollars?|amount)\b",
            words,
        )
    )
    percentage = bool(re.search(r"\bpercent(?:age)?\b|%", text, re.I))
    if absolute and percentage:
        raise PatternError("Choose absolute or percentage period change.", "comparison")
    if absolute or not percentage and re.search(r"\b(?:difference|delta|variance)\b", words):
        return "variance"
    return "variance_percent" if percentage else None


def _check_goal(
    goal: str, catalog: PatternCatalog, selection: PatternSelection | None = None
) -> None:
    # These selectors are not represented by this closed contract; never drop them.
    text = _pattern_text(goal, catalog)
    selected = []
    if selection is not None:
        for slot in ("base", "numerator", "denominator", "actual", "target"):
            value = getattr(selection, slot)
            if value is not None:
                selected.append(value.field)
        selected.extend(
            index
            for index in (selection.entity, selection.group, selection.date)
            if index is not None
        )
    prepositions = (
        r"not\s+including|for|in|with|during|within|on|at|among|between|except|without|"
        r"when|unless|that|who|using|across|over"
    )
    qualifier_text = text
    # A literal reviewed identity such as "Sales in West" is a field, not a new
    # request-time filter. Protect it only when that identity is represented.
    for item in sorted(catalog.fields, key=lambda field: -len(field.name)):
        if (selection is None or item.index in selected) and re.search(
            rf"\b(?:{prepositions}|where|excluding|exclude|only|before|after|since|fiscal)\b"
            r"|[<>=]",
            item.name,
            re.I,
        ):
            for identity in (item.reference, item.name):
                qualifier_text = re.sub(
                    r"(?<!\w)" + re.escape(identity) + r"(?!\w)",
                    " ",
                    qualifier_text,
                    flags=re.I,
                )
    qualifier_text = re.sub(
        r"\b(year|quarter|month)\s+over\s+\1\b",
        lambda match: {"year": "YoY", "quarter": "QoQ", "month": "MoM"}[match[1].casefold()],
        qualifier_text,
        flags=re.I,
    )
    if re.search(
        r"\b(?:where|excluding|exclude|only|before|after|since)\b|\bfiscal\b|"
        r"\b(?:in|for)\s+(?:19|20)\d{2}\b|"
        r"[<>=]|\b(?:equals?|matches?)\b",
        qualifier_text,
        re.I,
    ) or any(
        re.search(r"(?<!\w)" + re.escape(item.name) + r"\s+(?:is|are)\b", qualifier_text, re.I)
        for item in catalog.fields
        if item.kind == "column"
    ):
        raise PatternError(
            "This calculation also needs the requested filter or fiscal calendar resolved.",
            "context",
        )
    temporal = _words(qualifier_text)
    # Named reviewed metrics can contain "Current Year" or "2024"; an additional
    # request-time selector still needs a represented filter.
    for item in sorted(catalog.fields, key=lambda field: -len(_words(field.name))):
        if (selection is None or item.index in selected) and (
            item.kind == "measure" or re.search(r"\d", item.name)
        ):
            temporal = re.sub(r"(?<!\w)" + re.escape(_words(item.name)) + r"(?!\w)", " ", temporal)
    if re.search(
        r"\b(?:today|yesterday|this\s+(?:day|week|month|quarter|year)|"
        r"current\s+(?:day|week|month|quarter|year)|(?:19|20)\d{2})\b",
        temporal,
    ):
        raise PatternError("Resolve the requested date or current period filter first.", "context")
    if re.search(r"\b(?:moving|rolling)\s+(?:average|avg)\b", temporal) and re.search(
        r"\b(?:monthly|quarterly|weekly)\b|"
        r"\b(?:by|per|each|every)\s+(?:a\s+)?(?:week|month|quarter)\b",
        temporal,
    ):
        raise PatternError(
            "This moving average supports daily metric values. Resolve the requested "
            "weekly, monthly or quarterly averaging grain first; a window unit does not "
            "change that grain.",
            "grain",
        )
    qualifiers = re.finditer(
        rf"\b({prepositions})\s+(.+?)(?=\b(?:{prepositions})\s+|[;]|\.(?:\s|$)|$)",
        qualifier_text,
        re.I,
    )
    for match in qualifiers:
        phrase = _words(match[2]).removeprefix("the ")
        window = re.fullmatch(r"(?:last |trailing )?(\d+) (day|month|quarter|year)s?", phrase)
        if window and re.search(r"\bmoving\s+(?:average|avg)\b", text, re.I):
            if selection is None or (
                selection.pattern == "moving_average"
                and selection.window == int(window[1])
                and selection.period == window[2]
            ):
                continue
            raise PatternError("Use the moving window named in the request.", "window")
        grain = phrase.removeprefix("each ").removeprefix("every ")
        candidates = [
            item
            for item in catalog.fields
            if grain in {_words(item.name), _words(item.reference)}
            or item.kind == "table"
            and _words(item.table) == grain
        ]
        if selection is None and candidates:
            continue
        if any(item.index in selected for item in candidates):
            continue
        # "for Total Sales" names the represented metric. A table name can also
        # describe its row grain, but a data value such as West cannot.
        if candidates and selection is not None:
            sources = {
                table.casefold()
                for index in selected
                for table in catalog.field(index).source_tables
            }
            if all(
                item.kind == "table" and item.table.casefold() in sources for item in candidates
            ):
                continue
        periods = {
            "ytd": "year",
            "qtd": "quarter",
            "mtd": "month",
            "year to date": "year",
            "quarter to date": "quarter",
            "month to date": "month",
        }
        if phrase in periods and (
            selection is None
            or (selection.pattern == "period_to_date" and selection.period == periods[phrase])
        ):
            continue
        scope = re.fullmatch(r"(?:scope )?(all|selected|visible) (dates|time|groups)", phrase)
        if scope and (
            selection is None
            or (
                selection.scope == ("all" if scope[1] == "all" else "selected")
                and (
                    selection.date is not None
                    if scope[2] in {"dates", "time"}
                    else selection.group is not None
                )
            )
        ):
            continue
        group_scope = re.fullmatch(
            r"(all|selected|visible) (.+?) values(?: (ascending|descending) (dense|skip) rank)?",
            phrase,
        )
        if group_scope:
            matching_groups = [
                item
                for item in catalog.fields
                if item.kind == "column"
                and group_scope[2] in {_words(item.name), *map(_words, _references(item))}
            ]
            if matching_groups and (
                selection is None
                or selection.scope == ("all" if group_scope[1] == "all" else "selected")
                and any(item.index == selection.group for item in matching_groups)
                and (
                    not group_scope[3]
                    or selection.pattern == "rank"
                    and selection.direction == group_scope[3]
                    and selection.ties == group_scope[4]
                )
            ):
                continue
        formatting = re.fullmatch(
            r"(percent(?:age)?s?|currency)(?: format(?:ting)?)?",
            phrase,
        )
        if formatting:
            fmt = ""
            if selection is not None:
                if (
                    selection.pattern == "share_total"
                    or (
                        selection.pattern == "percent_change" and selection.comparison != "variance"
                    )
                    or (
                        selection.pattern == "ratio"
                        and re.search(r"\b(?:percent|percentage|share|rate)\b", goal, re.I)
                    )
                    or (
                        selection.pattern == "target_actual"
                        and selection.comparison in {"variance_percent", "attainment"}
                    )
                ):
                    fmt = "0.0%"
                elif selection.base is not None:
                    fmt = catalog.field(selection.base.field).format_string
            if selection is None or (
                "%" in fmt
                if formatting[1].startswith("percent")
                else bool(re.search(r"[$€£¥]", fmt))
            ):
                continue
        raise PatternError(
            f"Resolve the requested '{match[1].casefold()}' filter or grain before calculating.",
            "context",
        )


def is_fixed_grain_request(goal: str, catalog: PatternCatalog | None = None) -> bool:
    text = _calculation_text(goal)
    if catalog is not None:
        # A field named Fixed Cost is a metric, not an instruction to change filter grain.
        for field in sorted(catalog.fields, key=lambda item: -len(item.name)):
            if re.search(r"\b(?:fixed|lod)\b", field.name, re.I):
                text = re.sub(re.escape(field.name), " ", text, flags=re.I)
    return bool(re.search(r"\bfixed(?:[ -]grain)?\b|\blod\b|\blevel[ -]of[ -]detail\b", text, re.I))


def _fixed_grain_selection(goal: str, catalog: PatternCatalog) -> PatternSelection:
    text = _calculation_text(goal)
    grouping = re.search(
        r"\b(?:by|at|per|on)\s+(.+?)(?=[;.\n]|(?:,\s*|\s+(?:and\s+)?)"
        r"(?:ignore|ignoring|remove|removing|keep|keeping|preserve|respect|"
        r"don't|do not|never)\b|$)",
        text,
        re.I,
    )
    if grouping is None:
        raise PatternError("Which model field should the calculation stay fixed by?", "group")
    group = resolve_field(grouping[1], catalog, kinds=("column",))
    if not _plain_field_clause(grouping[1], group):
        raise PatternError(
            "Choose one source-table grouping column for this fixed-grain calculation.", "group"
        )
    numeric = replace(
        catalog,
        fields=tuple(
            field
            for field in catalog.fields
            if field.kind == "measure" or field.data_type == "number"
        ),
    )
    base = _resolve_metric(text[: grouping.start()], numeric, references=catalog)
    metric = catalog.field(base.field, "base")
    masked = text
    for field in (metric, group):
        for identity in (field.reference, field.name):
            masked = re.sub(re.escape(identity), " ", masked, flags=re.I)
    if re.search(
        r"\b(?:ratio|divided\s+by|share|percent(?:age)?|ytd|qtd|mtd|yoy|qoq|mom|"
        r"year.to.date|quarter.to.date|month.to.date|year.over.year|quarter.over.quarter|"
        r"month.over.month|"
        r"running\s+total|cumulative|moving\s+average|rolling\s+average|rank|top\s+\d+|"
        r"previous\s+(?:day|month|quarter|year|period)|target|attainment|variance|"
        r"minus|subtract|plus|multiply)\b",
        masked,
        re.I,
    ):
        raise PatternError(
            "Choose the fixed-grain calculation or the other calculation first. "
            "Combining them needs a separate filter and calculation decision.",
            "pattern",
        )
    if tuple(table.casefold() for table in metric.source_tables) != (group.table.casefold(),):
        raise PatternError(
            "This fixed-grain recipe needs a grouping column from the metric's single source "
            "table. Choose that column, or describe the related-table filters you need to keep.",
            "group",
        )
    ignore = re.search(
        r"\b(?:ignore|ignoring|remove|removing)\s+(?:all\s+)?other\s+filters\b", text, re.I
    )
    keep = re.search(
        r"\b(?:keep|keeping|preserve|respect)\s+(?:all\s+)?(?:other|current)\s+filters\b"
        r"|\b(?:don't|do not|never)\s+(?:ignore|remove)\b",
        text,
        re.I,
    )
    if keep:
        raise PatternError(
            "This fixed-grain recipe removes other filters on the source table and its "
            "expanded related tables. For a total that keeps them, ask for an ordinary "
            "grouped total, or name the filters to preserve.",
            "context",
        )
    if ignore is None:
        raise PatternError(
            f"Should {metric.reference} ignore other filters while keeping {group.reference}? "
            "Reply 'ignore other filters', or describe the filters to keep.",
            "context",
        )
    if text[ignore.end() :].strip(" ,;.\n!?"):
        raise PatternError(
            "This fixed-grain recipe keeps only the named grouping column. "
            "Resolve any additional filters or calculation steps before calculating.",
            "context",
        )
    return PatternSelection(pattern="fixed_grain", base=base, group=group.index)


def build_pattern(
    selection: PatternSelection, catalog: PatternCatalog, *, goal: str = ""
) -> BuiltPattern:
    """Compile and validate slot meaning. This does not evaluate or mutate the model."""
    _check_goal(goal, catalog, selection)
    pattern = selection.pattern
    if is_fixed_grain_request(goal, catalog) or pattern == "fixed_grain":
        expected = _fixed_grain_selection(goal, catalog)
        if pattern != "fixed_grain":
            raise PatternError(
                "This request needs a fixed-grain calculation, not a share or time calculation. "
                "Confirm the metric, fixed grouping, and which other filters to ignore.",
                "pattern",
            )
        if selection.base != expected.base or selection.group != expected.group:
            raise PatternError("Use the metric and fixed grouping named in the request.", "group")
    if pattern is None:
        raise PatternError("No reusable calculation pattern was selected.")
    if pattern == "percent_change":
        if selection.comparison == "attainment":
            raise PatternError("Choose absolute or percentage period change.", "comparison")
        requested = _period_change_comparison(goal, catalog, selection.base)
        if requested is not None and selection.comparison != requested:
            raise PatternError(
                "The period change mode must match the requested absolute or percentage change.",
                "comparison",
            )
    allowed = _PATTERN_SLOTS[pattern]
    extra = selection.model_dump(exclude_none=True).keys() - allowed - {"pattern"}
    if extra:
        option = sorted(extra)[0]
        labels = {
            "group": "grouping",
            "date": "a date axis",
            "period": "a time period",
            "scope": "filter scope",
            "direction": "sorting",
            "window": "a moving window",
            "n": "a top count",
            "ties": "ranking ties",
            "comparison": "a comparison",
            "previous_mode": "a previous-period mode",
        }
        raise PatternError(
            f"I couldn't combine {labels.get(option, option.replace('_', ' '))} with this "
            f"{pattern.replace('_', ' ')} calculation. Which grouping or filter behavior "
            "should the calculation use? Your current formula is unchanged.",
            option,
        )
    dependencies: list[PatternField] = []
    notes = [
        "Uses the supplied DAX filter context; report page slicer selections "
        "are not supplied automatically."
    ]
    kind: Literal["scalar", "query"] = "scalar"
    fmt = ""

    def metric(slot: str) -> str:
        value, item = _metric(getattr(selection, slot), catalog, slot)
        dependencies.append(item)
        return value

    if pattern == "ratio":
        _ground_ratio(selection, catalog, goal)
        numerator, denominator = metric("numerator"), metric("denominator")
        if numerator.casefold() == denominator.casefold():
            raise PatternError(
                "The numerator and denominator are the same calculation, "
                "so this ratio is constant.",
                "denominator",
            )
        expression = f"DIVIDE({numerator}, {denominator})"
        notes.append(
            "A blank or zero denominator returns BLANK; the formula does not multiply by 100."
        )
        fmt = "0.0%" if re.search(r"\b(?:percent|percentage|share|rate)\b", goal, re.I) else ""
    elif pattern == "target_actual":
        actual, target = metric("actual"), metric("target")
        if actual.casefold() == target.casefold():
            raise PatternError("Actual and target must identify different calculations.", "target")
        comparison = _required(
            selection.comparison,
            "comparison",
            "Choose difference, percentage variance, or target attainment.",
        )
        expression = (
            f"{actual} - {target}"
            if comparison == "variance"
            else (
                f"DIVIDE({actual} - {target}, {target})"
                if comparison == "variance_percent"
                else f"DIVIDE({actual}, {target})"
            )
        )
        fmt = "" if comparison == "variance" else "0.0%"
    elif pattern == "distinct_count":
        item = _column(selection.entity, catalog, "entity")
        dependencies.append(item)
        expression, fmt = f"DISTINCTCOUNT({item.reference})", "#,0"
        notes.append(
            "Counts distinct values including BLANK, following DAX DISTINCTCOUNT semantics."
        )
    else:
        base = metric("base")
        base_field = dependencies[0]
        fmt = base_field.format_string
        if pattern == "fixed_grain":
            group = _column(selection.group, catalog, "group")
            dependencies.append(group)
            table = "'" + group.table.replace("'", "''") + "'"
            expression = f"CALCULATE({base}, ALLEXCEPT({table}, {group.reference}))"
            notes.append(
                "Keeps existing filters on the chosen grouping column and removes other "
                "filters on the source table and its expanded related tables. A grand total "
                "still keeps any existing filter on that grouping."
            )
        elif pattern == "average_per":
            item = _column(selection.entity, catalog, "entity")
            _reaches(item, base_field, catalog)
            dependencies.append(item)
            expression = f"DIVIDE({base}, DISTINCTCOUNT({item.reference}))"
            notes.append(
                "Divides by distinct entity values, including BLANK; "
                "this is not a quantity or row-count denominator."
            )
        elif pattern in {"share_total", "rank", "top_n"}:
            group = _column(selection.group, catalog, "group")
            _reaches(group, base_field, catalog)
            dependencies.append(group)
            scope = _required(
                selection.scope,
                "scope",
                "Choose the selected groups or all groups for the comparison.",
            )
            scope_table = f"{'ALLSELECTED' if scope == 'selected' else 'ALL'}({group.reference})"
            if pattern == "share_total":
                clear = (
                    f"ALLSELECTED({group.reference})"
                    if scope == "selected"
                    else f"REMOVEFILTERS({group.reference})"
                )
                expression = f"DIVIDE({base}, CALCULATE({base}, {clear}))"
                fmt = "0.0%"
                notes.append(
                    "The denominator changes only the chosen grouping column; "
                    "other filters remain in context."
                )
            else:
                direction = _required(
                    selection.direction, "direction", "Choose ascending or descending order."
                )
                order = "DESC" if direction == "descending" else "ASC"
                if pattern == "rank":
                    ties = _required(
                        selection.ties, "ties", "Choose dense or skip ranking for ties."
                    )
                    expression = (
                        f"RANKX({scope_table}, CALCULATE({base}), , {order}, {ties.upper()})"
                    )
                    fmt = "#,0"
                else:
                    count = _required(selection.n, "n", "Choose how many groups to return.")
                    expression = (
                        f"EVALUATE TOPN({count}, ADDCOLUMNS({scope_table}, "
                        f'"__PatternValue", CALCULATE({base})), [__PatternValue], {order}) '
                        f"ORDER BY [__PatternValue] {order}"
                    )
                    kind = "query"
                    notes.append(
                        "TOPN includes all groups tied at the boundary, "
                        "so it may return more than N rows."
                    )
        else:
            calendar = _date(selection, base_field, catalog)
            dependencies.append(calendar)
            ref = calendar.reference
            if calendar.calendar_contiguous is None:
                notes.append(
                    "The supplied calendar key is reviewed; its daily continuity "
                    "still needs evaluation or Power BI validation."
                )
            if pattern == "running_total":
                scope = _required(
                    selection.scope,
                    "scope",
                    "Choose the selected dates or all dates for the running total.",
                )
                dates = f"{'ALLSELECTED' if scope == 'selected' else 'ALL'}({ref})"
                expression = f"CALCULATE({base}, FILTER({dates}, {ref} <= MAX({ref})))"
            else:
                period = _required(
                    selection.period, "period", "Choose day, month, quarter, or year."
                )
                if pattern == "period_to_date":
                    if period == "day":
                        raise PatternError("Choose month, quarter, or year to date.", "period")
                    function = {"month": "TOTALMTD", "quarter": "TOTALQTD", "year": "TOTALYTD"}[
                        period
                    ]
                    expression = f"{function}({base}, {ref})"
                elif pattern == "moving_average":
                    window = _required(
                        selection.window, "window", "Choose the moving average's window length."
                    )
                    dates = f"DATESINPERIOD({ref}, MAX({ref}), -{window}, {period.upper()})"
                    expression = f"AVERAGEX({dates}, CALCULATE({base}))"
                    notes.append(
                        "Averages daily metric values over the trailing window ending "
                        "on the visible last date; BLANK days are excluded and available "
                        "partial windows are used."
                    )
                else:
                    if pattern == "percent_change":
                        previous = f"CALCULATE({base}, DATEADD({ref}, -1, {period.upper()}))"
                        absolute = selection.comparison == "variance"
                        result = (
                            "IF(ISBLANK(PreviousValue), BLANK(), CurrentValue - PreviousValue)"
                            if absolute
                            else "DIVIDE(CurrentValue - PreviousValue, PreviousValue)"
                        )
                        expression = (
                            f"VAR CurrentValue = {base} VAR PreviousValue = {previous} "
                            f"RETURN {result}"
                        )
                        if absolute:
                            notes.append(
                                "Returns the absolute difference from the shifted previous period "
                                "in the base metric's units; a missing previous value "
                                "returns BLANK."
                            )
                        else:
                            fmt = "0.0%"
                            notes.append(
                                "Returns percentage change relative to the shifted "
                                "previous period; "
                                "a blank or zero previous value returns BLANK."
                            )
                    else:
                        mode = _required(
                            selection.previous_mode,
                            "previous_mode",
                            "Choose shifted dates or the previous complete calendar period.",
                        )
                        if mode == "calendar" and period == "day":
                            raise PatternError(
                                "Use shifted dates for the previous day.", "previous_mode"
                            )
                        dates = (
                            f"DATEADD({ref}, -1, {period.upper()})"
                            if mode == "shifted"
                            else f"PREVIOUS{period.upper()}({ref})"
                        )
                        expression = f"CALCULATE({base}, {dates})"
    # Parsing is a final compiler invariant, never a claim that the formula was evaluated.
    if kind == "scalar":
        parse_expression(expression)
    return BuiltPattern(expression, kind, fmt, tuple(dict.fromkeys(dependencies)), tuple(notes))


def _calendar_choice(catalog: PatternCatalog, text: str) -> int:
    dates = [
        item
        for item in catalog.fields
        if item.kind == "column" and item.date_key and item.available
    ]
    named = [item for item in dates if _mentions_reference(text, item)]
    choices = named or dates
    if len(choices) != 1:
        raise PatternError(
            "Choose the related calendar date key for this calculation.",
            "date",
            [item.index for item in choices],
        )
    return choices[0].index


def _group_pattern_selection(text: str, catalog: PatternCatalog) -> PatternSelection | None:
    match = re.fullmatch(
        r"(?:create\s+)?(?P<family>share(?:\s+of)?|rank)\s+(?P<metric>.+?)\s+by\s+"
        r"(?P<group>.+?)(?:\s+across\s+(?P<scope>all|selected|visible)\s+"
        r"(?P<scope_group>.+?)\s+values)?"
        r"(?:,?\s+(?P<direction>ascending|descending)\s+(?P<ties>dense|skip)\s+rank)?[.!?]?",
        text,
        re.I,
    )
    if match is None:
        return None
    numeric = replace(
        catalog,
        fields=tuple(
            item for item in catalog.fields if item.kind == "measure" or item.data_type == "number"
        ),
    )
    base = _resolve_metric(match["metric"], numeric, references=catalog)
    metric = catalog.field(base.field)
    remainder = _metric_clause_remainder(match["metric"], metric)
    allowed_words = {"the", "of", "total"}
    if metric.kind == "column":
        allowed_words |= {
            "sum",
            "average",
            "avg",
            "mean",
            "count",
            "distinct",
            "unique",
            "minimum",
            "min",
            "maximum",
            "max",
        }
    if set(remainder.split()) - allowed_words:
        raise PatternError(
            "Resolve the requested base calculation before comparing groups.", "base"
        )
    group = resolve_field(match["group"], catalog, kinds=("column",))
    if not _plain_field_clause(match["group"], group):
        raise PatternError("Choose one model grouping column for this calculation.", "group")
    scope = match["scope"].casefold() if match["scope"] else None
    if scope and not _plain_field_clause(match["scope_group"], group):
        raise PatternError("The comparison scope must use the named grouping column.", "scope")
    rank = match["family"].casefold() == "rank"
    if not rank and (match["direction"] or match["ties"]):
        raise PatternError("A share calculation does not rank groups.", "pattern")
    return PatternSelection(
        pattern="rank" if rank else "share_total",
        base=base,
        group=group.index,
        scope="all" if scope == "all" else "selected" if scope else None,
        direction=cast(
            Literal["ascending", "descending"] | None,
            match["direction"].casefold() if match["direction"] else None,
        ),
        ties=cast(
            Literal["dense", "skip"] | None, match["ties"].casefold() if match["ties"] else None
        ),
    )


def parse_business_pattern(goal: str, catalog: PatternCatalog) -> PatternSelection | None:
    """Instant exact common cases; decline unfamiliar wording rather than drop intent."""
    text = _pattern_text(goal, catalog)
    if is_fixed_grain_request(text, catalog):
        return _fixed_grain_selection(text, catalog)
    group_selection = _group_pattern_selection(text, catalog)
    if group_selection is not None:
        return group_selection
    parts = _ratio_parts(text)
    if parts:
        _check_goal(text, catalog)
        return PatternSelection(
            pattern="ratio",
            numerator=_resolve_metric(parts[0], catalog),
            denominator=_resolve_metric(parts[1], catalog),
        )
    if re.search(
        r"\b(?:distinct|unique)\s+count|\bcount\s+(?:of\s+)?(?:distinct|unique)\b", text, re.I
    ):
        return PatternSelection(
            pattern="distinct_count", entity=resolve_field(text, catalog, kinds=("column",)).index
        )
    to_date = re.search(r"\b(ytd|qtd|mtd|year.to.date|quarter.to.date|month.to.date)\b", text, re.I)
    change = re.search(
        r"\b(yoy|qoq|mom|year.over.year|quarter.over.quarter|month.over.month)\b", text, re.I
    )
    running = re.search(r"\b(?:running total|cumulative)\b", text, re.I)
    moving = re.search(
        r"\b(\d+)\s*[- ]\s*(day|month|quarter|year)s?\s+moving\s+(?:average|avg)\b", text, re.I
    )
    if to_date or change or running or moving:
        _check_goal(text, catalog)
        if sum(bool(item) for item in (to_date, change, running, moving)) > 1:
            raise PatternError(
                "Choose one calculation pattern before combining periods.", "pattern"
            )
        # The averaging word describes the window, not the base column's aggregation.
        metric_text = text[: moving.start()] + text[moving.end() :] if moving else text
        numeric = replace(
            catalog,
            fields=tuple(
                item
                for item in catalog.fields
                if item.kind == "measure" or item.data_type == "number"
            ),
        )
        base = _resolve_metric(metric_text, numeric, references=catalog)
        calendar = _calendar_choice(catalog, text)
        if to_date:
            period: Literal["day", "month", "quarter", "year"] = (
                "year"
                if to_date[1].casefold().startswith(("y", "year"))
                else "quarter"
                if to_date[1].casefold().startswith("q")
                else "month"
            )
            return PatternSelection(
                pattern="period_to_date", base=base, date=calendar, period=period
            )
        if change:
            period = (
                "year"
                if change[1].casefold().startswith("y")
                else "quarter"
                if change[1].casefold().startswith("q")
                else "month"
            )
            return PatternSelection(
                pattern="percent_change",
                base=base,
                date=calendar,
                period=period,
                comparison=_period_change_comparison(text, catalog, base),
            )
        if moving:
            return PatternSelection(
                pattern="moving_average",
                base=base,
                date=calendar,
                period=cast(Literal["day", "month", "quarter", "year"], moving[2].casefold()),
                window=int(moving[1]),
            )
        scope: Literal["all", "selected"] | None = (
            "selected"
            if re.search(r"\b(?:selected|selection|visible)\b", text, re.I)
            else "all"
            if re.search(r"\ball\s+(?:dates|time)\b", text, re.I)
            else None
        )
        return PatternSelection(pattern="running_total", base=base, date=calendar, scope=scope)
    return None


PATTERN_SYSTEM = (
    "Select a calculation pattern and numbered model fields. Never write DAX, M or code. "
    "Use existing measures directly; numeric columns need aggregation. Numerator and denominator "
    "come from their own request clauses. Do not invent fields, dates, filters or scope. "
    "Leave unspecified decisions null; pattern null if none fits. Patterns: ratio, share_total, "
    "fixed_grain, period_to_date, percent_change, running_total, moving_average, rank, top_n, "
    "distinct_count, average_per, target_actual, previous_period. "
    "moving_average means average DAILY metric values; period is its window unit. "
    "previous_mode shifted means shift current dates; calendar means previous complete period. "
    "percent_change comparison variance means absolute difference, variance_percent means "
    "percentage change; unqualified YoY defaults to percentage. "
    "scope all removes the named grouping filter; selected retains outer selections. "
    "fixed_grain uses base and a grouping from its single source table; requires explicit "
    "ignore-other-filters intent. Never substitute share_total for fixed-grain requests."
    " Slots: ratio=numerator,denominator; share_total=base,group,scope; "
    "rank=base,group,scope,direction,ties; running_total=base,date,scope. "
    "All unrelated slots must be null. Existing measure aggregation must be null."
)


@dataclass(frozen=True)
class PatternPrompt:
    system: str
    user: str
    catalog: PatternCatalog
    response_model: type[PatternSelection]

    @property
    def size(self) -> int:
        return len((self.system + self.user).encode("utf-8"))


def pattern_prompt(
    goal: str, catalog: PatternCatalog, *, exclude_measure: str | None = None
) -> PatternPrompt:
    """Under 3000 UTF-8 bytes; field numbers are not remapped by trimming."""
    request = " ".join(m_prompt_text(goal).split())
    if len(request.encode("utf-8")) > 1000:
        raise PatternError(
            "Shorten this calculation request so its field choices fit the local model prompt."
        )
    request_words = set(_words(_calculation_text(request)).split())
    fields = [
        item
        for item in catalog.fields
        if item.available
        and not (
            item.kind == "measure"
            and exclude_measure
            and item.name.casefold() == exclude_measure.casefold()
        )
    ]
    fields.sort(
        key=lambda item: (
            -len(request_words & set(_words(item.name).split())),
            not item.date_key,
            item.index,
        )
    )
    lines = ["Request: " + request, "Fields: number kind type table / name (* calendar key)"]
    selected: list[PatternField] = []
    for item in fields:
        line = (
            f"{item.index} {item.kind} {item.data_type} {item.table} / "
            f"{item.name}{' *' if item.date_key else ''}"
        )
        if len((PATTERN_SYSTEM + "\n".join([*lines, line])).encode("utf-8")) >= 3000:
            continue
        lines.append(line)
        selected.append(item)
        if len(selected) == 24:
            break
    if not selected:
        raise PatternError("No available field choices fit this calculation prompt.")
    ids = [item.index for item in selected]
    schema = PatternSelection.model_json_schema()
    schema["$defs"]["MetricSelection"]["properties"]["field"] = {"type": "integer", "enum": ids}
    metric_branches = []
    for kind, aggregations in (
        ("measure", [None]),
        ("table", ["count_rows"]),
        ("numeric", ["sum", "average", "minimum", "maximum", "count", "distinct_count"]),
        ("other", ["count", "distinct_count"]),
    ):
        metric_ids = [
            item.index
            for item in selected
            if item.kind == kind
            or item.kind == "column"
            and (
                kind == "numeric"
                and item.data_type == "number"
                or kind == "other"
                and item.data_type != "number"
            )
        ]
        if metric_ids:
            metric_branches.append(
                {
                    "properties": {
                        "field": {"enum": metric_ids},
                        "aggregation": {"enum": aggregations},
                    },
                    "required": ["field", "aggregation"],
                }
            )
    schema["$defs"]["MetricSelection"]["anyOf"] = metric_branches
    for slot in ("date", "group", "entity"):
        column_ids = [
            item.index
            for item in selected
            if item.kind == "column" and (slot != "date" or item.date_key)
        ]
        schema["properties"][slot] = {
            **(
                {"anyOf": [{"type": "integer", "enum": column_ids}, {"type": "null"}]}
                if column_ids
                else {"type": "null"}
            ),
            "default": None,
        }
    schema["anyOf"] = [
        {
            "properties": {
                "pattern": {"const": pattern},
                **{
                    slot: {"type": "null"}
                    for slot in schema["properties"]
                    if slot != "pattern" and slot not in allowed
                },
            },
            "required": ["pattern", *sorted(allowed)],
        }
        for pattern, allowed in [*sorted(_PATTERN_SLOTS.items()), (None, set())]
    ]
    frozen = json.dumps(schema)

    class CatalogSelection(PatternSelection):
        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return cast(dict[str, Any], json.loads(frozen))

    bounded = replace(catalog, fields=tuple(selected))
    return PatternPrompt(PATTERN_SYSTEM, "\n".join(lines), bounded, CatalogSelection)


class PatternModel(Protocol):
    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[PatternSelection],
        *,
        max_output_tokens: int | None = None,
        timeout_seconds: int | None = None,
        repair_attempts: int | None = None,
    ) -> PatternSelection: ...


def is_pattern_request(goal: str) -> bool:
    """Recognize the calculation family when model metadata itself is unavailable.

    This only controls truthful failure routing; it never selects fields or builds DAX.
    """
    return bool(
        re.search(
            r"\b(?:fixed(?:[ -]grain)?|lod|level[ -]of[ -]detail|"
            r"ratio|divided\s+by|share|percentage|percent\s+change|ytd|qtd|mtd|yoy|qoq|mom|"
            r"year.to.date|quarter.to.date|month.to.date|year.over.year|quarter.over.quarter|"
            r"month.over.month|running\s+total|cumulative|moving\s+(?:average|avg)|"
            r"rolling\s+(?:average|avg)|"
            r"rank|top\s+\d+|distinct|unique|(?:average|avg)\b.*\bper|per\s+customer|target|attainment|"
            r"variance|previous\s+(?:day|month|quarter|year|period)|prior\s+period)\b",
            goal,
            re.I,
        )
    )


def select_business_pattern(
    goal: str,
    catalog: PatternCatalog,
    model: PatternModel | None = None,
    *,
    exclude_measure: str | None = None,
    timeout_seconds: int | None = None,
) -> BuiltPattern | None:
    """Parse common wording first, otherwise ask the supplied local model for slots.

    PatternError means the recognized request needs clarification; callers must not
    turn it into unconstrained DAX generation. None means no pattern was selected.
    """
    usable = replace(
        catalog,
        fields=tuple(
            item
            for item in catalog.fields
            if not (
                exclude_measure
                and item.kind == "measure"
                and item.name.casefold() == exclude_measure.casefold()
            )
        ),
    )
    # An unknown explicit request reference must not reach the freeform fallback,
    # where a model might silently substitute a different known field.
    _qualified_fields(_calculation_text(goal), usable)
    if is_pattern_request(goal):
        # Missing filters must clarify even if the model would decline a pattern;
        # returning None here would let the freeform route bypass this contract.
        _check_goal(goal, usable)
    selection = parse_business_pattern(goal, usable)
    if selection is not None:
        return build_pattern(selection, usable, goal=goal)
    if re.fullmatch(
        r"(?:sums?|averages?|counts?|totals?)\s+(?:of\s+)?[^\s]+\[[^\]]+\][.!?]?",
        _calculation_text(goal),
        re.I,
    ):
        # A simple aggregation belongs to the existing formula generator, not a
        # request to select an unrelated business pattern.
        return None
    if model is None:
        return None
    prompt = pattern_prompt(goal, usable)
    answer = model.generate(
        prompt.system,
        prompt.user,
        prompt.response_model,
        max_output_tokens=350,
        timeout_seconds=timeout_seconds,
        repair_attempts=0,
    )
    selection = PatternSelection.model_validate(answer.model_dump())
    if selection.pattern is None:
        return None
    return build_pattern(selection, prompt.catalog, goal=goal)
