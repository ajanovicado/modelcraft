"""Timed, isolated execution of compiler-owned DuckDB queries."""

from __future__ import annotations

import multiprocessing
from datetime import date, datetime
from decimal import Decimal
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from ai_powerbi_analyst.analytics.compiler import GovernedQuery, validate_generated_sql
from ai_powerbi_analyst.contracts import Scalar
from ai_powerbi_analyst.data.build import open_hardened_read_only


class AnalyticsExecutionError(RuntimeError):
    """The governed query could not execute safely."""


class AnalyticsTimeoutError(AnalyticsExecutionError):
    """The isolated query exceeded its execution budget."""


def _scalar(value: Any) -> Scalar | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        return value
    # DuckDB can return NumPy scalar values for some extension types.
    if hasattr(value, "item"):
        return _scalar(value.item())
    raise TypeError(f"unsupported analytical scalar: {type(value).__name__}")


def _query_worker(
    database: str,
    query: GovernedQuery,
    sender: Connection,
) -> None:
    try:
        validate_generated_sql(query.sql)
        with open_hardened_read_only(database) as connection:
            cursor = connection.execute(query.sql, list(query.parameters))
            columns = [description[0] for description in cursor.description]
            values = cursor.fetchmany(query.row_limit + 1)
            if len(values) > query.row_limit:
                raise AnalyticsExecutionError("query exceeded its governed row limit")
            rows = [
                {column: _scalar(value) for column, value in zip(columns, row, strict=True)}
                for row in values
            ]
        sender.send((True, rows))
    except Exception as error:  # noqa: BLE001 - boundary converts worker failures to data
        sender.send((False, f"{type(error).__name__}: {error}"))
    finally:
        sender.close()


class AnalyticsExecutor:
    """Execute a GovernedQuery in a disposable process with a hard timeout."""

    def __init__(self, database: str | Path, *, timeout_seconds: float = 10.0) -> None:
        self.database = Path(database).resolve()
        self.timeout_seconds = timeout_seconds
        if not self.database.is_file():
            raise FileNotFoundError(self.database)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    def execute(self, query: GovernedQuery) -> list[dict[str, Scalar | None]]:
        validate_generated_sql(query.sql)
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_query_worker,
            args=(str(self.database), query, sender),
            daemon=True,
        )
        process.start()
        sender.close()
        try:
            if not receiver.poll(self.timeout_seconds):
                process.terminate()
                process.join(timeout=1)
                raise AnalyticsTimeoutError(
                    f"analysis exceeded the {self.timeout_seconds:g}-second execution budget"
                )
            succeeded, payload = receiver.recv()
            process.join(timeout=1)
        finally:
            receiver.close()
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)

        if not succeeded:
            raise AnalyticsExecutionError(str(payload))
        return payload  # type: ignore[no-any-return]
