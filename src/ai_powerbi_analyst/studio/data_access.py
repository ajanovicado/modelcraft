"""Bounded, projected browsing of immutable local snapshots."""

from __future__ import annotations

import math
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import BoundedSemaphore
from typing import Any, Literal

from pydantic import Field

from ai_powerbi_analyst.contracts import ClosedModel

from ..contracts import ColumnProfile
from .aggregates import _bounded_connection, _quote, _scalar
from .contracts import DatasetSnapshot

_PREVIEW_SLOTS = BoundedSemaphore(2)


class PreviewFilter(ClosedModel):
    column_id: str = Field(min_length=1, max_length=200)
    operator: Literal[
        "equals", "not_equals", "contains", "gt", "gte", "lt", "lte", "is_blank", "is_not_blank"
    ]
    value: str = Field(default="", max_length=500)


class PreviewSort(ClosedModel):
    column_id: str = Field(min_length=1, max_length=200)
    direction: Literal["asc", "desc"] = "asc"


class TablePreviewRequest(ClosedModel):
    table_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    column_ids: list[str] = Field(default_factory=list, max_length=40)
    offset: int = Field(default=0, ge=0, le=1_000_000_000)
    page_size: int = Field(default=50, ge=1, le=100)
    filters: list[PreviewFilter] = Field(default_factory=list, max_length=4)
    sort: PreviewSort | None = None


def _filter_value(value: str, data_type: str) -> Any:
    try:
        if data_type in {"integer", "number"}:
            number = Decimal(value)
            approximate = float(number)
            if (
                not number.is_finite()
                or not math.isfinite(approximate)
                or (number and not approximate)
            ):
                raise ValueError
            if number == number.to_integral_value() and -(2**63) <= number < 2**63:
                return int(number)
            return approximate
        if data_type == "boolean":
            if value.lower() not in {"true", "false"}:
                raise ValueError
            return value.lower() == "true"
        if data_type == "date":
            return date.fromisoformat(value)
        if data_type == "datetime":
            return datetime.fromisoformat(value)
        return value
    except (ValueError, InvalidOperation) as error:
        raise ValueError(f"Enter a valid {data_type} filter value") from error


def _where(filters: list[PreviewFilter], by_id: dict[str, ColumnProfile]) -> tuple[str, list[Any]]:
    clauses, parameters = [], []
    operators = {"equals": "=", "not_equals": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
    for item in filters:
        column = by_id.get(item.column_id)
        if column is None:
            raise ValueError("Select filter columns from this table")
        name = _quote(column.name)
        if item.operator in {"is_blank", "is_not_blank"}:
            blank = (
                f"({name} IS NULL OR {name} = '')"
                if column.data_type == "string"
                else f"{name} IS NULL"
            )
            clauses.append(f"NOT ({blank})" if item.operator == "is_not_blank" else blank)
        elif item.operator == "contains":
            if column.data_type != "string" or not item.value:
                raise ValueError("Contains requires a text column and a non-empty value")
            clauses.append(f"contains(lower({name}), lower(?))")
            parameters.append(item.value)
        else:
            if column.data_type == "boolean" and item.operator not in {"equals", "not_equals"}:
                raise ValueError("Boolean columns support equals, not equals and blank filters")
            clauses.append(f"{name} {operators[item.operator]} ?")
            parameters.append(_filter_value(item.value, column.data_type))
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), parameters


def preview_table(
    snapshot: DatasetSnapshot, request: TablePreviewRequest, spill: Path
) -> dict[str, Any]:
    if not _PREVIEW_SLOTS.acquire(timeout=0.25):
        raise ValueError("Other previews are still running. Retry in a moment.")
    try:
        return _preview_table(snapshot, request, spill)
    finally:
        _PREVIEW_SLOTS.release()


def _preview_table(
    snapshot: DatasetSnapshot, request: TablePreviewRequest, spill: Path
) -> dict[str, Any]:
    table = next((item for item in snapshot.tables if item.id == request.table_id), None)
    if table is None:
        raise ValueError("This table is not in the selected snapshot")
    by_id = {column.id: column for column in table.table.columns}
    ids = request.column_ids or list(by_id)[:40]
    if len(ids) != len(set(ids)) or any(key not in by_id for key in ids):
        raise ValueError("Select unique columns from this table")
    started = time.perf_counter()
    # Identifiers come exclusively from reviewed metadata; values are parameters.
    projection = ", ".join(_quote(by_id[key].name) for key in ids)
    where, parameters = _where(request.filters, by_id)
    source = "read_parquet(?)"
    order = ""
    if request.sort:
        if request.sort.column_id not in by_id:
            raise ValueError("Select a sort column from this table")
        # A physical-row tie breaker keeps equal values stable across pages.
        row_alias = "__preview_row"
        names = {column.name.lower() for column in by_id.values()}
        while row_alias.lower() in names:
            row_alias += "_"
        source = f"(SELECT *, row_number() OVER () AS {_quote(row_alias)} FROM read_parquet(?))"  # noqa: S608 - generated alias, no user SQL
        direction = "ASC" if request.sort.direction == "asc" else "DESC"
        order = (
            f" ORDER BY {_quote(by_id[request.sort.column_id].name)} {direction} NULLS LAST,"
            f" {_quote(row_alias)} ASC"
        )
    with _bounded_connection(spill, 12) as connection:
        total_rows = table.table.row_count
        if request.filters:
            count_row = connection.execute(
                f"SELECT count(*) FROM read_parquet(?){where}",  # noqa: S608
                [table.normalized_parquet_path, *parameters],
            ).fetchone()
            assert count_row is not None
            total_rows = count_row[0]
        result = connection.execute(
            f"SELECT {projection} FROM {source}{where}{order} LIMIT ? OFFSET ?",  # noqa: S608
            [table.normalized_parquet_path, *parameters, request.page_size + 1, request.offset],
        ).fetchall()
    # Browser JSON numbers cannot represent large integer identifiers exactly.
    # Keep their stored integer type, sort/filter semantics and displayed digits.
    rows = [
        [
            str(value) if isinstance(value, int) and abs(value) > 2**53 - 1 else _scalar(value)
            for value in row
        ]
        for row in result[: request.page_size]
    ]
    truncated = False
    for row in rows:
        for index, value in enumerate(row):
            if isinstance(value, str) and len(value) > 500:
                row[index] = value[:500] + "…"
                truncated = True
    return {
        "columns": [
            {"id": key, "name": by_id[key].name, "data_type": by_id[key].data_type} for key in ids
        ],
        "rows": rows,
        "offset": request.offset,
        "total_rows": total_rows,
        "snapshot_rows": table.table.row_count,
        "has_more": len(result) > request.page_size,
        "truncated_cells": truncated,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    }
