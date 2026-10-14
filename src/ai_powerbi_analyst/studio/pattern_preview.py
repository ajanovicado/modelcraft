"""Read-only pattern evaluation using the same local engine as imported reports."""

from __future__ import annotations

import math
import time
from typing import Any

import pandas as pd

from ai_powerbi_analyst.contracts import DslMeasureSpec, SourceManifest
from ai_powerbi_analyst.powerbi.calendar_table import daily_date_key, plan_calendar_table
from ai_powerbi_analyst.powerbi.semantic_model import (
    _calendar_display_name,
    _remap_dax_table_names,
    compile_dsl_measure,
)

from .contracts import (
    AdvancedDaxProposal,
    DashboardDraft,
    DatasetSnapshot,
    DaxEvaluateRequest,
    DaxResult,
)
from .dax_engine import DaxEngineLimit, DaxUnsupported, Model
from .dax_format import format_number
from .dax_patterns import BuiltPattern
from .dax_syntax import is_query
from .report_engine import (
    ENGINE_LABEL,
    MAX_ROWS,
    LocalReportEngine,
    _cell,
    _column_names,
    unrelated_filter,
)


def snapshot_pattern_model(
    snapshot: DatasetSnapshot,
    manifest: SourceManifest,
    draft: DashboardDraft | None,
) -> Model:
    """Snapshot columns, governed draft measures and reviewed joins; no app mutation.

    A draft's planned calendar uses the builder's identical complete-year date plan.
    Pending relationships and unapproved advanced formulas never become preview facts.
    """
    if sum(item.table.row_count for item in snapshot.tables) > MAX_ROWS:
        raise DaxEngineLimit("This snapshot exceeds the local DAX engine's row limit.")
    tables = {item.table.id: item.table for item in snapshot.tables}
    semantic_names = {key: table.name for key, table in tables.items()}
    if draft is not None:
        semantic_names.update(
            {item.table_id: item.display_name for item in draft.semantic_model.tables}
        )
    frames = {
        semantic_names[item.table.id]: pd.read_parquet(item.normalized_parquet_path)
        for item in snapshot.tables
    }
    joins: list[tuple[str, str, str, str]] = []
    measures: dict[str, str] = {}
    measure_formats: dict[str, str] = {}
    date_refs: dict[str, dict[str, str]] = {}
    if draft is not None:
        for rel in draft.semantic_model.relationships:
            if rel.review_status != "approved" or rel.blockers or not rel.is_active:
                continue
            if rel.cardinality != "many_to_one" or rel.cross_filter_direction != "single":
                raise DaxUnsupported("This reviewed relationship needs Power BI for preview.")
            many, one = tables[rel.from_table_id], tables[rel.to_table_id]
            many_col = next(c.name for c in many.columns if c.id == rel.from_column_id)
            one_col = next(c.name for c in one.columns if c.id == rel.to_column_id)
            joins.append((semantic_names[many.id], many_col, semantic_names[one.id], one_col))
            key = daily_date_key(one)
            if key is not None and key.id == rel.to_column_id:
                reference = "'" + semantic_names[one.id].replace("'", "''") + "'"
                date_refs.setdefault(many.id, {})[rel.from_column_id] = reference + f"[{key.name}]"
        if draft.semantic_model.calendar_required:
            calendar = plan_calendar_table(
                manifest,
                preferred_name=_calendar_display_name(semantic_names.values()),
            )
            if calendar is None:
                raise DaxUnsupported("The planned calendar has no reviewed date field.")
            dates = pd.date_range(calendar.start_date, calendar.end_date)
            frames[calendar.table_name] = pd.DataFrame(
                {
                    "Date": dates,
                    "Year": dates.year,
                    "Month Number": dates.month,
                    "Month": dates.month_name(),
                    "Quarter": [f"Q{q}" for q in dates.quarter],
                    "Year Month": dates.strftime("%Y-%m"),
                    "Month Start": dates.to_period("M").to_timestamp(),
                }
            )
            calendar_ref = "'" + calendar.table_name.replace("'", "''") + "'[Date]"
            for binding in calendar.source_bindings:
                table = tables[binding.table_id]
                # The builder's calendar joins use datePartOnly, including datetime sources.
                frames[semantic_names[table.id]][binding.column_name] = pd.to_datetime(
                    frames[semantic_names[table.id]][binding.column_name]
                ).dt.normalize()
                joins.append(
                    (
                        semantic_names[table.id],
                        binding.column_name,
                        calendar.table_name,
                        "Date",
                    )
                )
                date_refs.setdefault(table.id, {})[binding.column_id] = calendar_ref
        names = {item.id: item.name for item in draft.measure_proposal.measures}
        for proposal in draft.measure_proposal.measures:
            if isinstance(proposal, AdvancedDaxProposal):
                if proposal.approved:
                    measures[proposal.name] = _remap_dax_table_names(
                        proposal.expression,
                        {table.name: semantic_names[key] for key, table in tables.items()},
                    )
                    measure_formats[proposal.name] = proposal.format_string
                continue
            spec = DslMeasureSpec.model_validate(
                {
                    key: value
                    for key, value in proposal.model_dump().items()
                    if key in DslMeasureSpec.model_fields
                }
            )
            measures[proposal.name] = compile_dsl_measure(
                spec,
                tables[spec.table_id],
                names,
                table_name=semantic_names[spec.table_id],
                date_table_refs=date_refs.get(spec.table_id),
            )
            measure_formats[proposal.name] = proposal.format_string
    model = Model.from_frames(frames, joins, measures)
    for measure in model.measures.values():
        measure.format_string = measure_formats.get(measure.name, "")
    return model


def preview_pattern(
    model: Model,
    built: BuiltPattern,
) -> tuple[str, str]:
    """Return status and actual values only after the engine executes successfully."""
    query = (
        built.expression if built.kind == "query" else f'EVALUATE ROW("Value", {built.expression})'
    )
    unrelated = unrelated_filter(model, query)
    if unrelated:
        raise DaxUnsupported(unrelated)
    # LocalReportEngine enforces unavailable fields and execution limits too.
    result = LocalReportEngine(model, time_budget=30).query(None, query, max_rows=5)
    rows: list[dict[str, Any]] = result["rows"]
    lines = []
    blank = True
    for row in rows:
        cells = []
        for key, value in row.items():
            label = key[1:-1] if key.startswith("[") and key.endswith("]") else key
            if value is None or isinstance(value, float) and not math.isfinite(value):
                shown = "blank"
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                blank = False
                metric_label = "__patternvalue" if built.kind == "query" else "value"
                cell_format = built.format_string if label.casefold() == metric_label else ""
                shown = format_number(value, cell_format)
            else:
                blank = False
                shown = str(value)
            cells.append(f"{label} = {shown}")
        lines.append(", ".join(cells))
    text = (
        "Local preview (all snapshot rows; page slicer selections are not applied): "
        + ("; ".join(lines) if lines else "no rows")
        + "."
    )
    if built.kind == "query":
        text += " Showing up to the first 5 result rows."
    return ("local-blank" if blank else "local"), text


def evaluate_pattern_model(model: Model, request: DaxEvaluateRequest) -> DaxResult:
    """Run a compiled pattern against a freshly built model, with lab result limits."""
    if request.preview is not None:
        raise ValueError("Business pattern Run does not support a grouped preview request")
    if sum(table.rows for table in model.tables.values()) > MAX_ROWS:
        raise DaxEngineLimit("This model exceeds the local DAX engine's row limit.")
    started = time.monotonic()
    expression = request.expression.strip()
    query = expression if is_query(expression) else f'EVALUATE ROW("Value", {expression})'
    unrelated = unrelated_filter(model, query)
    if unrelated:
        raise DaxUnsupported(unrelated)
    # The extra row proves truncation without exposing more than the lab's result limit.
    # LocalReportEngine also rejects unavailable references and enforces its time budget.
    result = LocalReportEngine(model, time_budget=30).query(
        None,
        query,
        max_rows=request.top_n + 1,
    )
    if len(result["columns"]) > 40:
        raise ValueError("Limit this query to 40 columns")
    columns = _column_names(result["columns"])
    rows = [
        {name: _cell(row[label]) for name, label in zip(columns, result["columns"], strict=True)}
        for row in result["rows"][: request.top_n]
    ]
    return DaxResult(
        engine="snapshot",
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=len(result["rows"]) > request.top_n,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        plan=(
            f"{ENGINE_LABEL} on the current snapshot, with reviewed draft measures and "
            "relationships. Page slicer selections are not applied to this query."
        ),
    )
