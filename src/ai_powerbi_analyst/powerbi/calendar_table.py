"""Deterministic local calendar-table planning for reviewed source profiles."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from ai_powerbi_analyst.contracts import (
    ColumnDataType,
    ColumnProfile,
    RelationshipCandidate,
    SourceManifest,
    TableProfile,
)

_CALENDAR_COLUMN_ID = "date"
_MAX_CALENDAR_DAYS = 366 * 200
# Trend axes bind here: the Studio preview buckets trends by month, and a raw
# datetime axis drew 1,237 daily spikes in Desktop instead of 48 monthly points.
MONTH_START_COLUMN = "Month Start"


class CalendarTableError(RuntimeError):
    """A reviewed date profile cannot safely produce a local calendar table."""


@dataclass(frozen=True, slots=True)
class CalendarSourceBinding:
    """The reviewed source date chosen for one source table."""

    table_id: str
    column_id: str
    column_name: str
    data_type: ColumnDataType


@dataclass(frozen=True, slots=True)
class CalendarRelationshipSpec:
    """A governed date relationship, including date-part join semantics."""

    relationship: RelationshipCandidate
    join_on_date_behavior: Literal["datePartOnly"] = "datePartOnly"

    @property
    def tmdl_property(self) -> str:
        return f"joinOnDateBehavior: {self.join_on_date_behavior}"


@dataclass(frozen=True, slots=True)
class CalendarTablePlan:
    """A complete, deterministic calendar table ready for project integration."""

    table_id: str
    table_name: str
    start_date: date
    end_date: date
    tmdl: str
    model_reference_tmdl: str
    source_bindings: tuple[CalendarSourceBinding, ...]
    relationship_specs: tuple[CalendarRelationshipSpec, ...]

    @property
    def relationships(self) -> tuple[RelationshipCandidate, ...]:
        """Compatibility view of the governed candidates for semantic planning."""

        return tuple(spec.relationship for spec in self.relationship_specs)


def daily_date_key(table: TableProfile) -> ColumnProfile | None:
    """The unique, gap-free daily column that makes ``table`` a date dimension.

    Power BI marks a table as a date table only on such a column; a fact's
    OrderDate (repeated, with gaps) never qualifies.
    """
    for column in table.columns:
        if (
            column.data_type not in {ColumnDataType.DATE, ColumnDataType.DATETIME}
            or not column.candidate_key
            or column.distinct_count < 28
        ):
            continue
        try:
            first = date.fromisoformat(str(column.minimum)[:10])
            last = date.fromisoformat(str(column.maximum)[:10])
        except ValueError:
            continue
        if (last - first).days + 1 == column.distinct_count:
            return column
    return None


def plan_calendar_table(
    manifest: SourceManifest,
    *,
    reviewed_date_fields: Mapping[str, Sequence[str]] | None = None,
    preferred_name: str = "Calendar",
    max_calendar_days: int = _MAX_CALENDAR_DAYS,
) -> CalendarTablePlan | None:
    """Plan a complete-year local calendar from reviewed date/datetime profiles.

    With no explicit ``reviewed_date_fields`` mapping, date fields in the reviewed
    manifest are considered in profile order. When a mapping is supplied, its
    first column ID for each table is the reviewed field used for that table.
    Tables without a selected date field do not receive a relationship.
    """

    if max_calendar_days < 1:
        raise ValueError("max_calendar_days must be positive")
    bindings = _source_bindings(manifest, reviewed_date_fields)
    if not bindings:
        return None

    table_by_id = {table.id: table for table in manifest.tables}
    bounds: list[tuple[date, date]] = []
    for binding in bindings:
        table = table_by_id[binding.table_id]
        column = _column_by_id(table, binding.column_id)
        if column.minimum is None or column.maximum is None:
            raise CalendarTableError(
                f"Reviewed date field {table.id}.{column.id} has no bounded values"
            )
        minimum = _as_date(column.minimum, f"{table.id}.{column.id} minimum")
        maximum = _as_date(column.maximum, f"{table.id}.{column.id} maximum")
        if minimum > maximum:
            raise CalendarTableError(
                f"Reviewed date field {table.id}.{column.id} has inverted bounds"
            )
        bounds.append((minimum, maximum))

    minimum = min(item[0] for item in bounds)
    maximum = max(item[1] for item in bounds)
    start = date(minimum.year, 1, 1)
    end = date(maximum.year, 12, 31)
    calendar_days = (end - start).days + 1
    if calendar_days > max_calendar_days:
        raise CalendarTableError(
            f"Calendar range would contain {calendar_days:,} rows; review the source bounds"
        )

    table_name, table_id = _available_calendar_name(manifest, preferred_name)
    relationship_specs = tuple(
        _calendar_relationship(binding, table_id, start, end) for binding in bindings
    )
    return CalendarTablePlan(
        table_id=table_id,
        table_name=table_name,
        start_date=start,
        end_date=end,
        tmdl=_calendar_tmdl(table_name, start, end),
        model_reference_tmdl=f"ref table {_quote_name(table_name)}",
        source_bindings=bindings,
        relationship_specs=relationship_specs,
    )


def _source_bindings(
    manifest: SourceManifest,
    reviewed_date_fields: Mapping[str, Sequence[str]] | None,
) -> tuple[CalendarSourceBinding, ...]:
    table_by_id = {table.id: table for table in manifest.tables}
    if reviewed_date_fields is not None:
        unknown_tables = sorted(set(reviewed_date_fields) - set(table_by_id))
        if unknown_tables:
            raise CalendarTableError(
                "Reviewed date fields reference unknown table(s): " + ", ".join(unknown_tables)
            )

    result: list[CalendarSourceBinding] = []
    for table in manifest.tables:
        if reviewed_date_fields is None:
            candidates = [
                column
                for column in table.columns
                if column.data_type in {ColumnDataType.DATE, ColumnDataType.DATETIME}
                and column.minimum is not None
                and column.maximum is not None
            ]
            if not candidates:
                continue
            selected = candidates[0]
        else:
            reviewed_ids = reviewed_date_fields.get(table.id, ())
            if isinstance(reviewed_ids, str):
                raise CalendarTableError(
                    f"Reviewed date fields for {table.id} must be an ordered sequence"
                )
            if not reviewed_ids:
                continue
            selected = _column_by_id(table, reviewed_ids[0])
            if selected.data_type not in {ColumnDataType.DATE, ColumnDataType.DATETIME}:
                raise CalendarTableError(
                    f"Reviewed field {table.id}.{selected.id} is not a date or datetime"
                )
        result.append(
            CalendarSourceBinding(
                table_id=table.id,
                column_id=selected.id,
                column_name=selected.name,
                data_type=selected.data_type,
            )
        )
    return tuple(result)


def _column_by_id(table: TableProfile, column_id: str) -> ColumnProfile:
    column = next((item for item in table.columns if item.id == column_id), None)
    if column is None:
        raise CalendarTableError(f"Reviewed date field {table.id}.{column_id} does not exist")
    return column


def _as_date(value: str | int | float | bool, label: str) -> date:
    if isinstance(value, (bool, int, float)):
        raise CalendarTableError(f"{label} is not a valid ISO date")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        try:
            return date.fromisoformat(normalized)
        except ValueError as error:
            raise CalendarTableError(f"{label} is not a valid ISO date") from error


def _available_calendar_name(
    manifest: SourceManifest,
    preferred_name: str,
) -> tuple[str, str]:
    base = " ".join(preferred_name.split())
    if not base:
        raise ValueError("preferred_name cannot be blank")
    existing_names = {table.name.casefold() for table in manifest.tables}
    existing_ids = {table.id for table in manifest.tables}
    suffix = 1
    while True:
        candidate = base if suffix == 1 else f"{base} {suffix}"
        candidate_id = _identifier(candidate)
        if candidate.casefold() not in existing_names and candidate_id not in existing_ids:
            return candidate, candidate_id
        suffix += 1


def _calendar_relationship(
    binding: CalendarSourceBinding,
    calendar_table_id: str,
    start: date,
    end: date,
) -> CalendarRelationshipSpec:
    raw = f"{binding.table_id}.{binding.column_id}->{calendar_table_id}.date"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    evidence = [
        f"Reviewed source date field: {binding.table_id}.{binding.column_id}",
        f"Calendar contains every date from {start.isoformat()} through {end.isoformat()}",
        "Single-direction active many-to-one relationship toward the unique calendar date",
    ]
    evidence.append("Date and datetime values join on their date part")
    return CalendarRelationshipSpec(
        relationship=RelationshipCandidate(
            id=f"rel_calendar_{digest}",
            from_table_id=binding.table_id,
            from_column_id=binding.column_id,
            to_table_id=calendar_table_id,
            to_column_id=_CALENDAR_COLUMN_ID,
            cardinality="many_to_one",
            confidence=0.99,
            evidence=evidence,
            is_active=True,
            cross_filter_direction="single",
            rely_on_referential_integrity=False,
        )
    )


def _calendar_tmdl(table_name: str, start: date, end: date) -> str:
    quoted_table = _quote_name(table_name)
    return "\n".join(
        [
            f"table {quoted_table}",
            "\tdataCategory: Time",
            "",
            "\tcolumn Date",
            "\t\tdataType: dateTime",
            "\t\tformatString: Short Date",
            "\t\tisKey",
            "\t\tsummarizeBy: none",
            "\t\tsourceColumn: Date",
            "",
            "\tcolumn Year",
            "\t\tdataType: int64",
            "\t\tformatString: 0",
            "\t\tsummarizeBy: none",
            "\t\tsourceColumn: Year",
            "",
            "\tcolumn 'Month Number'",
            "\t\tdataType: int64",
            "\t\tformatString: 0",
            "\t\tsummarizeBy: none",
            "\t\tsourceColumn: Month Number",
            "",
            "\tcolumn Month",
            "\t\tdataType: string",
            "\t\tsortByColumn: 'Month Number'",
            "\t\tsummarizeBy: none",
            "\t\tsourceColumn: Month",
            "",
            "\tcolumn Quarter",
            "\t\tdataType: string",
            "\t\tsummarizeBy: none",
            "\t\tsourceColumn: Quarter",
            "",
            "\tcolumn 'Year Month'",
            "\t\tdataType: string",
            "\t\tsummarizeBy: none",
            "\t\tsourceColumn: Year Month",
            "",
            f"\tcolumn '{MONTH_START_COLUMN}'",
            "\t\tdataType: dateTime",
            "\t\tformatString: mmm yyyy",
            "\t\tsummarizeBy: none",
            f"\t\tsourceColumn: {MONTH_START_COLUMN}",
            "",
            f"\tpartition {quoted_table} = m",
            "\t\tmode: import",
            "\t\tsource =",
            "\t\t\tlet",
            (f"\t\t\t\tStartDate = #date({start.year}, {start.month}, {start.day}),"),
            f"\t\t\t\tEndDate = #date({end.year}, {end.month}, {end.day}),",
            (
                "\t\t\t\tDates = List.Dates(StartDate, "
                "Duration.Days(EndDate - StartDate) + 1, #duration(1, 0, 0, 0)),"
            ),
            (
                "\t\t\t\tDateTable = Table.FromList(Dates, Splitter.SplitByNothing(), "
                '{"Date"}, null, ExtraValues.Error),'
            ),
            ('\t\t\t\tTypedDate = Table.TransformColumnTypes(DateTable, {{"Date", type date}}),'),
            (
                '\t\t\t\tAddYear = Table.AddColumn(TypedDate, "Year", '
                "each Date.Year([Date]), Int64.Type),"
            ),
            (
                '\t\t\t\tAddMonthNumber = Table.AddColumn(AddYear, "Month Number", '
                "each Date.Month([Date]), Int64.Type),"
            ),
            (
                '\t\t\t\tAddMonth = Table.AddColumn(AddMonthNumber, "Month", '
                'each Date.MonthName([Date], "en-US"), type text),'
            ),
            (
                '\t\t\t\tAddQuarter = Table.AddColumn(AddMonth, "Quarter", '
                'each "Q" & Text.From(Date.QuarterOfYear([Date]), "en-US"), type text),'
            ),
            (
                '\t\t\t\tAddYearMonth = Table.AddColumn(AddQuarter, "Year Month", '
                'each Date.ToText([Date], "yyyy-MM", "en-US"), type text),'
            ),
            (
                f'\t\t\t\tAddMonthStart = Table.AddColumn(AddYearMonth, "{MONTH_START_COLUMN}", '
                "each Date.StartOfMonth([Date]), type date)"
            ),
            "\t\t\tin",
            "\t\t\t\tAddMonthStart",
            "",
        ]
    )


def _identifier(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    if not normalized:
        normalized = "calendar"
    if not normalized[0].isalpha():
        normalized = "calendar_" + normalized
    return normalized[:96]


def _quote_name(value: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        return value
    return "'" + value.replace("'", "''") + "'"
