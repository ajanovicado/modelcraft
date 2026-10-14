"""Bounded read-only aggregate tool compiled from a closed plan."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore, Timer
from typing import Any

import duckdb
import psutil

from ai_powerbi_analyst.contracts import ColumnProfile

from .contracts import (
    AggregateFilter,
    DatasetSnapshot,
    QueryReceipt,
    ReadOnlyAggregatePlan,
    RelationshipProposal,
)


class AggregatePlanError(RuntimeError):
    """An aggregate plan cannot be compiled against the reviewed snapshot."""


# Many-to-one hops from the aggregate's table outwards: FactSales -> DimStore ->
# DimEmployee. A single relationship is a one-hop path.
type RelationshipPath = tuple[RelationshipProposal, ...]
type RelationshipRoute = RelationshipProposal | Sequence[RelationshipProposal]


def _as_path(route: RelationshipRoute) -> RelationshipPath:
    return (route,) if isinstance(route, RelationshipProposal) else tuple(route)


def _path_key(path: RelationshipPath) -> str | list[str]:
    """Receipt identity: one hop keeps its plain relationship id."""
    return path[0].id if len(path) == 1 else [item.id for item in path]


class AggregateService:
    def __init__(self, spill_root: str | Path) -> None:
        self.spill_root = Path(spill_root).expanduser().resolve(strict=False)
        self.spill_root.mkdir(parents=True, exist_ok=True)

    def execute(
        self,
        snapshot: DatasetSnapshot,
        plan: ReadOnlyAggregatePlan,
        *,
        order_by: list[tuple[str, str]] | None = None,
        grand_total: bool = False,
    ) -> QueryReceipt:
        table_snapshot = next(
            (item for item in snapshot.tables if item.table.id == plan.table_id),
            None,
        )
        if table_snapshot is None:
            raise AggregatePlanError(f"Unknown table ID: {plan.table_id}")
        table = table_snapshot.table
        columns = {column.id: column for column in table.columns}
        requested = [*plan.group_by_column_ids]
        if plan.value_column_id:
            requested.append(plan.value_column_id)
        if plan.denominator_column_id:
            requested.append(plan.denominator_column_id)
        requested.extend(item.column_id for item in plan.filters)
        requested.extend(item.column_id for item in plan.numerator_filters)
        unknown = sorted(set(requested) - set(columns))
        if unknown:
            raise AggregatePlanError(f"Unknown column IDs: {', '.join(unknown)}")
        if plan.time_bucket and not plan.group_by_column_ids:
            raise AggregatePlanError("Time bucketing requires a grouped date column")
        source = "read_parquet(?)"
        source_parameters: list[object] = [table_snapshot.normalized_parquet_path]
        numerator_where = None
        if plan.numerator_filters:
            # Flag the numerator's rows once, so each FILTER clause needs no parameters.
            flag = " AND ".join(
                _filter_condition(
                    item, _quote(columns[item.column_id].name), columns[item.column_id]
                )
                for item in plan.numerator_filters
            )
            source = (
                f"(SELECT *, coalesce({flag}, false) AS {_quote(_NUMERATOR_FLAG)} "  # noqa: S608 - quoted catalog identifiers; values parameterized
                "FROM read_parquet(?))"
            )
            source_parameters = [
                *(item.value for item in plan.numerator_filters),
                table_snapshot.normalized_parquet_path,
            ]
            numerator_where = _quote(_NUMERATOR_FLAG)
        group_selects: list[str] = []
        group_aliases: list[str] = []
        group_expressions: list[str] = []
        for index, column_id in enumerate(plan.group_by_column_ids):
            column = columns[column_id]
            quoted = _quote(column.name)
            alias = f"group_{index + 1}"
            expression, group_expression = _group_expression(
                column, quoted, grand_total=grand_total
            )
            if index == 0 and plan.time_bucket:
                if column.data_type.value not in {"date", "datetime"}:
                    raise AggregatePlanError("Time bucket field must be a date or datetime")
                expression = f"date_trunc('{plan.time_bucket}', {quoted})"
                group_expression = expression
            group_selects.append(f"{expression} AS {_quote(alias)}")
            group_aliases.append(alias)
            group_expressions.append(group_expression)
        aggregate = _aggregate_expression(plan, columns, numerator_where)
        select = ", ".join([*group_selects, f"{aggregate} AS value"])
        if grand_total:
            if not group_aliases or plan.time_bucket:
                raise AggregatePlanError("Grand totals require unbucketed group columns")
            select += f", GROUPING({group_expressions[0]}) > 0 AS is_grand_total"
        allowed_sorts = {*group_aliases, "value", *(["is_grand_total"] if grand_total else [])}
        if len(order_by or []) > 5 or any(
            alias not in allowed_sorts or direction not in {"asc", "desc"}
            for alias, direction in order_by or []
        ):
            raise AggregatePlanError("Sort keys must be bounded result columns and ASC/DESC")
        where_parts: list[str] = []
        parameters: list[object] = [*source_parameters]
        for item in plan.filters:
            where_parts.append(
                _filter_condition(
                    item, _quote(columns[item.column_id].name), columns[item.column_id]
                )
            )
            parameters.append(item.value)
        where = " WHERE " + " AND ".join(where_parts) if where_parts else ""
        group = " GROUP BY " + ", ".join(group_expressions) if group_selects else ""
        if grand_total:
            group = " GROUP BY GROUPING SETS ((" + ", ".join(group_expressions) + "), ())"
        if group_selects and plan.numerator_filters:
            # A share is BLANK where its numerator is; SUMMARIZECOLUMNS omits those groups.
            group += f" HAVING ({aggregate}) IS NOT NULL"
        order = (
            " ORDER BY 1 ASC"
            if plan.time_bucket and group_selects
            else " ORDER BY value DESC, "
            + ", ".join(str(i) + " ASC" for i in range(1, len(group_selects) + 1))
            if group_selects
            else ""
        )
        if order_by:
            order = " ORDER BY " + ", ".join(
                f"{_quote(alias)} {direction.upper()} NULLS FIRST"
                if direction == "asc"
                else f"{_quote(alias)} DESC NULLS LAST"
                for alias, direction in order_by
            )
        query = (
            f"SELECT {select} FROM {source}"  # noqa: S608 - only catalog identifiers are interpolated
            f"{where}{group}{order} LIMIT {plan.top_n}"
        )
        started = time.perf_counter()
        try:
            with _bounded_connection(self.spill_root, plan.timeout_seconds) as connection:
                result = connection.execute(query, parameters)
                names = [item[0] for item in result.description]
                rows = [
                    dict(zip(names, (_scalar(value) for value in row), strict=True))
                    for row in result.fetchall()
                ]
        except duckdb.Error as error:
            raise AggregatePlanError(f"Bounded aggregate failed: {error}") from error
        elapsed = int((time.perf_counter() - started) * 1000)
        plan_payload = plan.model_dump_json()
        if order_by or grand_total:
            plan_payload = json.dumps(
                {
                    "plan": plan.model_dump(mode="json"),
                    "order_by": order_by,
                    "grand_total": grand_total,
                },
                sort_keys=True,
            )
        plan_hash = hashlib.sha256(plan_payload.encode("utf-8")).hexdigest()
        result_hash = hashlib.sha256(
            json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return QueryReceipt(
            id=f"query-{uuid.uuid4().hex}",
            created_at=datetime.now(UTC),
            plan_hash=plan_hash,
            result_hash=result_hash,
            row_count=len(rows),
            elapsed_ms=elapsed,
            rows=rows,
        )

    def execute_related(
        self,
        snapshot: DatasetSnapshot,
        plan: ReadOnlyAggregatePlan,
        *,
        group_table_id: str,
        relationship: RelationshipProposal,
    ) -> QueryReceipt:
        """Run an aggregate through one reviewed many-to-one relationship.

        The plan's table is always the value/fact side. Grouping fields belong to
        ``group_table_id``. Only a direct, approved, unblocked relationship from
        the value table to the verified one-side table can be used. The caller
        supplies catalog IDs only; no SQL or path is accepted from the model.
        """

        if relationship.review_status != "approved" or relationship.blockers:
            raise AggregatePlanError("Cross-table preview requires an approved relationship")
        if relationship.cardinality not in {"many_to_one", "one_to_one"}:
            raise AggregatePlanError("Cross-table preview blocks many-to-many relationships")
        if (
            relationship.cardinality == "one_to_one"
            and relationship.cross_filter_direction != "both"
        ):
            raise AggregatePlanError("One-to-one relationships require Both filter direction")
        if not relationship.is_active:
            raise AggregatePlanError("Cross-table preview requires an active relationship")
        if (
            relationship.from_table_id != plan.table_id
            or relationship.to_table_id != group_table_id
        ):
            raise AggregatePlanError(
                "Cross-table preview requires the reviewed value-to-dimension direction"
            )
        if not plan.group_by_column_ids:
            raise AggregatePlanError("Cross-table preview requires a grouped dimension field")
        if plan.numerator_filters:
            raise AggregatePlanError("Cross-table preview does not filter one operand")
        if plan.time_bucket and len(plan.group_by_column_ids) != 1:
            raise AggregatePlanError("Time bucketing supports one reviewed date field")

        value_snapshot = next(
            (item for item in snapshot.tables if item.table.id == plan.table_id),
            None,
        )
        group_snapshot = next(
            (item for item in snapshot.tables if item.table.id == group_table_id),
            None,
        )
        if value_snapshot is None or group_snapshot is None:
            raise AggregatePlanError("Cross-table preview references an unknown table")

        value_columns = {column.id: column for column in value_snapshot.table.columns}
        group_columns = {column.id: column for column in group_snapshot.table.columns}
        value_join = value_columns.get(relationship.from_column_id)
        group_join = group_columns.get(relationship.to_column_id)
        if value_join is None or group_join is None:
            raise AggregatePlanError("Relationship join fields are outside the reviewed catalog")
        if value_join.data_type != group_join.data_type:
            raise AggregatePlanError("Relationship join fields have incompatible data types")

        value_requested: list[str] = []
        if plan.value_column_id:
            value_requested.append(plan.value_column_id)
        value_requested.extend(item.column_id for item in plan.filters)
        unknown_value = sorted(set(value_requested) - set(value_columns))
        unknown_group = sorted(set(plan.group_by_column_ids) - set(group_columns))
        if unknown_value or unknown_group:
            unknown = [*unknown_value, *unknown_group]
            raise AggregatePlanError(f"Unknown cross-table column IDs: {', '.join(unknown)}")

        group_selects: list[str] = []
        group_expressions: list[str] = []
        for index, column_id in enumerate(plan.group_by_column_ids):
            column = group_columns[column_id]
            quoted = f"g.{_quote(column.name)}"
            expression, group_expression = _group_expression(column, quoted)
            if index == 0 and plan.time_bucket:
                if column.data_type.value not in {"date", "datetime"}:
                    raise AggregatePlanError("Time bucket field must be a date or datetime")
                expression = f"date_trunc('{plan.time_bucket}', {quoted})"
                group_expression = expression
            group_selects.append(f"{expression} AS {_quote(f'group_{index + 1}')}")
            group_expressions.append(group_expression)

        aggregate = _related_aggregate_expression(plan, value_columns)
        select = ", ".join([*group_selects, f"{aggregate} AS value"])
        where_parts: list[str] = []
        parameters: list[object] = [
            value_snapshot.normalized_parquet_path,
            group_snapshot.normalized_parquet_path,
        ]
        for item in plan.filters:
            quoted = f"m.{_quote(value_columns[item.column_id].name)}"
            where_parts.append(_filter_condition(item, quoted, value_columns[item.column_id]))
            parameters.append(item.value)
        where = " WHERE " + " AND ".join(where_parts) if where_parts else ""
        group = " GROUP BY " + ", ".join(group_expressions)
        order = (
            " ORDER BY 1 ASC"
            if plan.time_bucket
            else " ORDER BY value DESC, "
            + ", ".join(str(i) + " ASC" for i in range(1, len(group_selects) + 1))
        )
        query = (
            f"SELECT {select} FROM read_parquet(?) AS m "  # noqa: S608
            f"LEFT JOIN read_parquet(?) AS g ON "
            f"{_join_key(value_join, 'm')} = {_join_key(group_join, 'g')}"
            f"{where}{group}{order} LIMIT {plan.top_n}"
        )
        started = time.perf_counter()
        try:
            with _bounded_connection(self.spill_root, plan.timeout_seconds) as connection:
                _verify_one_side_key(connection, group_snapshot.normalized_parquet_path, group_join)
                if relationship.cardinality == "one_to_one":
                    _verify_one_side_key(
                        connection, value_snapshot.normalized_parquet_path, value_join
                    )
                result = connection.execute(query, parameters)
                names = [item[0] for item in result.description]
                rows = [
                    dict(zip(names, (_scalar(value) for value in row), strict=True))
                    for row in result.fetchall()
                ]
        except duckdb.Error as error:
            raise AggregatePlanError(f"Bounded cross-table aggregate failed: {error}") from error

        elapsed = int((time.perf_counter() - started) * 1000)
        plan_payload = {
            "aggregate": plan.model_dump(mode="json"),
            "group_table_id": group_table_id,
            "relationship_id": relationship.id,
            "relationship_direction": [
                relationship.from_table_id,
                relationship.to_table_id,
            ],
        }
        plan_hash = hashlib.sha256(
            json.dumps(plan_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        result_hash = hashlib.sha256(
            json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return QueryReceipt(
            id=f"query-{uuid.uuid4().hex}",
            created_at=datetime.now(UTC),
            plan_hash=plan_hash,
            result_hash=result_hash,
            row_count=len(rows),
            elapsed_ms=elapsed,
            rows=rows,
        )

    def execute_through(
        self,
        snapshot: DatasetSnapshot,
        plan: ReadOnlyAggregatePlan,
        *,
        group_relationship: RelationshipRoute | None = None,
        related_filters: Sequence[tuple[RelationshipRoute, AggregateFilter]] | None = None,
        numerator_related_filters: Sequence[tuple[RelationshipRoute, AggregateFilter]]
        | None = None,
        order_by: list[tuple[str, str]] | None = None,
    ) -> QueryReceipt:
        """Run a DAX-lab aggregate that filters or groups through related tables.

        The plan's table is the value (many) side. ``related_filters`` apply
        ``=``/``<>`` conditions on one-side columns, and ``group_relationship``
        groups by the one-side table, each through its approved relationship or
        chain of them (FactSales -> DimStore -> DimEmployee).
        ``numerator_related_filters`` (with ``plan.numerator_filters``) restrict a
        ratio's numerator alone: returned orders / all orders. Fact rows without a
        matching key at any hop take Power BI's (Blank) member: they group under
        NULL and a BLANK value equals FALSE, 0 and "".
        """

        group_path = _as_path(group_relationship) if group_relationship is not None else None
        filters = [(_as_path(route), condition) for route, condition in related_filters or []]
        numerator_related = [
            (_as_path(route), condition) for route, condition in numerator_related_filters or []
        ]
        if group_path is None and not filters and not numerator_related:
            raise AggregatePlanError("A related aggregate needs a relationship")
        if plan.time_bucket:
            raise AggregatePlanError("Related aggregates do not bucket time")
        tables = {item.table.id: item for item in snapshot.tables}
        fact = tables.get(plan.table_id)
        if fact is None:
            raise AggregatePlanError(f"Unknown table ID: {plan.table_id}")
        fact_columns = {column.id: column for column in fact.table.columns}
        checked_keys: dict[tuple[str, str], ColumnProfile] = {}

        def hops(path: RelationshipPath) -> list[tuple[Any, Any, Any, dict[str, Any]]]:
            """Each hop's (one-side snapshot, many-side key, one-side key, one-side columns).

            The path starts at the aggregate's table and each hop leaves the table
            the previous one reached, so the joins can be chained in order.
            """
            if not path:
                raise AggregatePlanError("A related aggregate needs a relationship")
            resolved: list[tuple[Any, Any, Any, dict[str, Any]]] = []
            here, here_columns, visited = plan.table_id, fact_columns, {plan.table_id}
            for relationship in path:
                if relationship.review_status != "approved" or relationship.blockers:
                    raise AggregatePlanError("Related aggregates require an approved relationship")
                if relationship.cardinality not in {"many_to_one", "one_to_one"}:
                    raise AggregatePlanError("Related aggregates block many-to-many relationships")
                if (
                    relationship.cardinality == "one_to_one"
                    and relationship.cross_filter_direction != "both"
                ):
                    raise AggregatePlanError(
                        "One-to-one relationships require Both filter direction"
                    )
                # Power BI propagates no filter through an inactive relationship.
                if not relationship.is_active:
                    raise AggregatePlanError("Related aggregates require an active relationship")
                if relationship.from_table_id != here:
                    raise AggregatePlanError(
                        "The relationship must start at the aggregate's table"
                        if here == plan.table_id
                        else "Each relationship must start where the previous one ends"
                    )
                if relationship.to_table_id in visited:
                    raise AggregatePlanError("A relationship path may not revisit a table")
                other = tables.get(relationship.to_table_id)
                if other is None:
                    raise AggregatePlanError("Related aggregate references an unknown table")
                other_columns = {column.id: column for column in other.table.columns}
                many_key = here_columns.get(relationship.from_column_id)
                one_key = other_columns.get(relationship.to_column_id)
                if many_key is None or one_key is None:
                    raise AggregatePlanError("Relationship join fields are outside the catalog")
                if many_key.data_type != one_key.data_type:
                    raise AggregatePlanError(
                        "Relationship join fields have incompatible data types"
                    )
                checked_keys[(other.normalized_parquet_path, one_key.name)] = one_key
                if relationship.cardinality == "one_to_one":
                    checked_keys[(tables[here].normalized_parquet_path, many_key.name)] = many_key
                resolved.append((other, many_key, one_key, other_columns))
                here, here_columns = relationship.to_table_id, other_columns
                visited.add(here)
            return resolved

        def related_condition(
            alias: str, path: RelationshipPath, condition: AggregateFilter
        ) -> tuple[str, list[object], str, object]:
            """(LEFT JOIN, its snapshot paths in order, boolean condition, its parameter)."""
            steps = hops(path)
            column = steps[-1][3].get(condition.column_id)
            if column is None:
                raise AggregatePlanError(f"Unknown related column ID: {condition.column_id}")
            if condition.operator not in {"eq", "neq"}:
                raise AggregatePlanError("Related filters support = and <> only")
            # One derived row per first-hop key carrying the filtered column from the
            # end of the chain. A store without an employee reads NULL there, exactly
            # like a sale without a store after the outer LEFT JOIN.
            chain = f"read_parquet(?) AS {alias}_0"
            for index, (_, many_key, one_key, _) in enumerate(steps[1:], start=1):
                chain += (
                    f" LEFT JOIN read_parquet(?) AS {alias}_{index} ON "
                    f"{_join_key(many_key, f'{alias}_{index - 1}')} = "
                    f"{_join_key(one_key, f'{alias}_{index}')}"
                )
            _, first_many, first_one, _ = steps[0]
            join = (
                f" LEFT JOIN (SELECT {_join_key(first_one, f'{alias}_0')} AS k, "  # noqa: S608 - quoted catalog identifiers; paths parameterized
                f"{alias}_{len(steps) - 1}.{_quote(column.name)} AS v FROM {chain}) AS {alias} "
                f"ON {_join_key(first_many, 'm')} = {alias}.k"
            )
            value = condition.value
            blank_equal = value == "" if isinstance(value, str) else value == 0
            left, right = _filter_operands(column, f"{alias}.v", value)
            matched = (
                f"coalesce({left} = {right}, {alias}.v IS NULL)"
                if blank_equal
                else f"coalesce({left} = {right}, false)"
            )
            return (
                join,
                [other.normalized_parquet_path for other, *_ in steps],
                matched if condition.operator == "eq" else f"NOT {matched}",
                value,
            )

        requested = [*([plan.value_column_id] if plan.value_column_id else [])]
        if plan.denominator_column_id:
            requested.append(plan.denominator_column_id)
        requested.extend(item.column_id for item in plan.filters)
        requested.extend(item.column_id for item in plan.numerator_filters)
        group_columns = fact_columns
        joins: list[str] = []
        source = "read_parquet(?)"
        parameters: list[object] = [fact.normalized_parquet_path]
        numerator_where = None
        if plan.numerator_filters or numerator_related:
            # Flag the numerator's rows once in a derived table named m, so the
            # aggregate's FILTER clauses need no parameters of their own.
            flag_parts: list[str] = []
            flag_parameters: list[object] = []
            inner_joins: list[str] = []
            inner_paths: list[object] = []
            for item in plan.numerator_filters:
                column = fact_columns.get(item.column_id)
                if column is None:
                    raise AggregatePlanError(f"Unknown column IDs: {item.column_id}")
                flag_parts.append(_filter_condition(item, f"m.{_quote(column.name)}", column))
                flag_parameters.append(item.value)
            for index, (path, condition) in enumerate(numerator_related):
                join, paths, flag, value = related_condition(f"n{index}", path, condition)
                inner_joins.append(join)
                inner_paths.extend(paths)
                flag_parts.append(flag)
                flag_parameters.append(value)
            source = (
                f"(SELECT m.*, coalesce({' AND '.join(flag_parts)}, false) AS "  # noqa: S608 - quoted catalog identifiers; values parameterized
                f"{_quote(_NUMERATOR_FLAG)} FROM read_parquet(?) AS m{''.join(inner_joins)})"
            )
            parameters = [*flag_parameters, fact.normalized_parquet_path, *inner_paths]
            numerator_where = f"m.{_quote(_NUMERATOR_FLAG)}"
        if group_path is not None:
            # Chained LEFT JOINs: the grouped table is g, the tables between are g1..;
            # a sale whose store or store's employee is missing groups under NULL.
            steps = hops(group_path)
            previous = "m"
            for index, (other, many_key, one_key, _) in enumerate(steps, start=1):
                alias = "g" if index == len(steps) else f"g{index}"
                joins.append(
                    f" LEFT JOIN read_parquet(?) AS {alias} ON "
                    f"{_join_key(many_key, previous)} = {_join_key(one_key, alias)}"
                )
                parameters.append(other.normalized_parquet_path)
                previous = alias
            group_columns = steps[-1][3]
        else:
            requested.extend(plan.group_by_column_ids)
        unknown = sorted(set(requested) - set(fact_columns))
        unknown += sorted(set(plan.group_by_column_ids) - set(group_columns))
        if unknown:
            raise AggregatePlanError(f"Unknown column IDs: {', '.join(unknown)}")

        prefix = "g" if group_path is not None else "m"
        group_selects = []
        group_expressions = []
        for index, column_id in enumerate(plan.group_by_column_ids):
            column = group_columns[column_id]
            expression, group_expression = _group_expression(
                column, f"{prefix}.{_quote(column.name)}"
            )
            group_selects.append(f"{expression} AS {_quote(f'group_{index + 1}')}")
            group_expressions.append(group_expression)
        where_parts: list[str] = []
        condition_parameters: list[object] = []
        for index, (path, condition) in enumerate(filters):
            join, paths, matched, value = related_condition(f"f{index}", path, condition)
            joins.append(join)
            parameters.extend(paths)
            where_parts.append(matched)
            condition_parameters.append(value)
        for item in plan.filters:
            where_parts.append(
                _filter_condition(
                    item,
                    f"m.{_quote(fact_columns[item.column_id].name)}",
                    fact_columns[item.column_id],
                )
            )
            condition_parameters.append(item.value)
        parameters.extend(condition_parameters)

        aggregate = _related_aggregate_expression(plan, fact_columns, numerator_where)
        select = ", ".join([*group_selects, f"{aggregate} AS value"])
        group_aliases = [f"group_{index + 1}" for index in range(len(group_selects))]
        if len(order_by or []) > 5 or any(
            alias not in {*group_aliases, "value"} or direction not in {"asc", "desc"}
            for alias, direction in order_by or []
        ):
            raise AggregatePlanError("Sort keys must be bounded result columns and ASC/DESC")
        where = " WHERE " + " AND ".join(where_parts) if where_parts else ""
        positions = ", ".join(str(index) for index in range(1, len(group_selects) + 1))
        # SUMMARIZECOLUMNS omits groups whose value is BLANK (a manager with no returns).
        group = (
            f" GROUP BY {', '.join(group_expressions)} HAVING ({aggregate}) IS NOT NULL"
            if group_selects
            else ""
        )
        order = f" ORDER BY value DESC, {positions}" if group_selects else ""
        if order_by:
            order = " ORDER BY " + ", ".join(
                f"{_quote(alias)} {direction.upper()} NULLS FIRST"
                if direction == "asc"
                else f"{_quote(alias)} DESC NULLS LAST"
                for alias, direction in order_by
            )
        query = (
            f"SELECT {select} FROM {source} AS m"  # noqa: S608 - catalog identifiers only
            f"{''.join(joins)}{where}{group}{order} LIMIT {plan.top_n}"
        )
        started = time.perf_counter()
        try:
            with _bounded_connection(self.spill_root, plan.timeout_seconds) as connection:
                # Recheck actual snapshot keys, once per distinct one-side key, in
                # the same memory/spill/deadline budget as the aggregate itself.
                for (key_path, _), column in checked_keys.items():
                    _verify_one_side_key(connection, key_path, column)
                result = connection.execute(query, parameters)
                names = [item[0] for item in result.description]
                rows = [
                    dict(zip(names, (_scalar(value) for value in row), strict=True))
                    for row in result.fetchall()
                ]
        except duckdb.Error as error:
            raise AggregatePlanError(f"Bounded related aggregate failed: {error}") from error
        elapsed = int((time.perf_counter() - started) * 1000)
        plan_payload = {
            "aggregate": plan.model_dump(mode="json"),
            "group_relationship_id": _path_key(group_path) if group_path else None,
            "related_filters": [
                [_path_key(path), condition.model_dump(mode="json")] for path, condition in filters
            ],
            "numerator_related_filters": [
                [_path_key(path), condition.model_dump(mode="json")]
                for path, condition in numerator_related
            ],
            "order_by": order_by,
        }
        plan_hash = hashlib.sha256(
            json.dumps(plan_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        result_hash = hashlib.sha256(
            json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return QueryReceipt(
            id=f"query-{uuid.uuid4().hex}",
            created_at=datetime.now(UTC),
            plan_hash=plan_hash,
            result_hash=result_hash,
            row_count=len(rows),
            elapsed_ms=elapsed,
            rows=rows,
        )

    def detail(
        self,
        snapshot: DatasetSnapshot,
        table_id: str,
        column_ids: list[str],
        *,
        row_limit: int = 20,
    ) -> QueryReceipt:
        table_snapshot = next(
            (item for item in snapshot.tables if item.table.id == table_id),
            None,
        )
        if table_snapshot is None:
            raise AggregatePlanError(f"Unknown table ID: {table_id}")
        columns = {column.id: column for column in table_snapshot.table.columns}
        if not column_ids or len(column_ids) > 8 or not set(column_ids).issubset(columns):
            raise AggregatePlanError("Detail preview fields are outside the reviewed catalog")
        limit = min(max(row_limit, 1), 50)
        projection = ", ".join(
            f"{_quote(columns[column_id].name)} AS {_quote(columns[column_id].name)}"
            for column_id in column_ids
        )
        query = f"SELECT {projection} FROM read_parquet(?) LIMIT {limit}"  # noqa: S608
        started = time.perf_counter()
        try:
            with _bounded_connection(self.spill_root, 12) as connection:
                result = connection.execute(query, [table_snapshot.normalized_parquet_path])
                names = [item[0] for item in result.description]
                rows = [
                    dict(zip(names, (_scalar(value) for value in row), strict=True))
                    for row in result.fetchall()
                ]
        except duckdb.Error as error:
            raise AggregatePlanError(f"Bounded detail preview failed: {error}") from error
        elapsed = int((time.perf_counter() - started) * 1000)
        plan_payload = {
            "table_id": table_id,
            "column_ids": column_ids,
            "row_limit": limit,
        }
        plan_hash = hashlib.sha256(
            json.dumps(plan_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        result_hash = hashlib.sha256(
            json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return QueryReceipt(
            id=f"query-{uuid.uuid4().hex}",
            created_at=datetime.now(UTC),
            plan_hash=plan_hash,
            result_hash=result_hash,
            row_count=len(rows),
            elapsed_ms=elapsed,
            rows=rows,
        )

    def scan(
        self,
        snapshot: DatasetSnapshot,
        table_id: str,
        *,
        row_limit: int = 50,
        filters: list[AggregateFilter] | None = None,
        null_column_id: str | None = None,
    ) -> QueryReceipt:
        """Bounded full-width row preview for the DAX lab's EVALUATE 'Table'."""
        table_snapshot = next(
            (item for item in snapshot.tables if item.table.id == table_id),
            None,
        )
        if table_snapshot is None:
            raise AggregatePlanError(f"Unknown table ID: {table_id}")
        limit = min(max(row_limit, 1), 100)
        columns = {column.id: column for column in table_snapshot.table.columns}
        if len(filters or []) + (1 if null_column_id else 0) > 8:
            raise AggregatePlanError("Row previews support at most 8 filters")
        parts: list[str] = []
        if null_column_id is not None:
            if null_column_id not in columns:
                raise AggregatePlanError("Unknown row-preview column")
            parts.append(f"{_quote(columns[null_column_id].name)} IS NULL")
        parameters: list[object] = [table_snapshot.normalized_parquet_path]
        for item in filters or []:
            if item.column_id not in columns:
                raise AggregatePlanError("Unknown row-preview column")
            quoted = _quote(columns[item.column_id].name)
            parts.append(_filter_condition(item, quoted, columns[item.column_id]))
            parameters.append(item.value)
        where = " WHERE " + " AND ".join(parts) if parts else ""
        query = "SELECT * FROM read_parquet(?)" + where + " LIMIT " + str(limit)  # noqa: S608
        started = time.perf_counter()
        try:
            with _bounded_connection(self.spill_root, 12) as connection:
                result = connection.execute(query, parameters)
                names = [item[0] for item in result.description]
                rows = [
                    dict(zip(names, (_scalar(value) for value in row), strict=True))
                    for row in result.fetchall()
                ]
        except duckdb.Error as error:
            raise AggregatePlanError(f"Bounded table scan failed: {error}") from error
        elapsed = int((time.perf_counter() - started) * 1000)
        plan_payload = {
            "scan": table_id,
            "row_limit": limit,
            "filters": [item.model_dump() for item in filters or []],
            "null_column_id": null_column_id,
        }
        plan_hash = hashlib.sha256(
            json.dumps(plan_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        result_hash = hashlib.sha256(
            json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return QueryReceipt(
            id=f"query-{uuid.uuid4().hex}",
            created_at=datetime.now(UTC),
            plan_hash=plan_hash,
            result_hash=result_hash,
            row_count=len(rows),
            elapsed_ms=elapsed,
            rows=rows,
        )


_QUERY_SLOTS = BoundedSemaphore(2)
_NUMERATOR_FLAG = "__studio_numerator"


def _group_expression(
    column: ColumnProfile, quoted: str, *, grand_total: bool = False
) -> tuple[str, str]:
    """Select an original text label while grouping with Import comparison semantics.

    Keep NULL separate from empty text and retain leading spaces. GROUPING SETS
    need an explicit NULL label for the total because its text label is aggregated.
    """
    if column.data_type.value != "string":
        return quoted, quoted
    normalized = f"lower(rtrim({quoted}))"
    label = f"first(rtrim({quoted}))"
    if grand_total:
        label = f"CASE WHEN GROUPING({normalized}) > 0 THEN NULL ELSE {label} END"
    return label, normalized


def _join_key(column: ColumnProfile, alias: str | None = None) -> str:
    """Power BI Import text keys ignore case and trailing spaces, preserving leading spaces."""
    quoted = _quote(column.name)
    if alias is not None:
        quoted = f"{alias}.{quoted}"
    return f"NULLIF(lower(rtrim({quoted})), '')" if column.data_type.value == "string" else quoted


def _verify_one_side_key(
    connection: duckdb.DuckDBPyConnection, path: str, column: ColumnProfile
) -> None:
    key = _join_key(column)
    row = connection.execute(
        f"SELECT COUNT(*), COUNT({key}), COUNT(DISTINCT {key}) FROM read_parquet(?)",  # noqa: S608 - catalog identifiers quoted; path parameterized
        [path],
    ).fetchone()
    if row is None or row[0] != row[1] or row[1] != row[2]:
        raise AggregatePlanError(
            f"Relationship requires a unique non-blank one-side key: {column.name}. "
            "Review the relationship and source keys before querying again."
        )


def _query_memory_mib() -> int:
    # Leave room for Desktop and the local model. DuckDB can spill supported
    # sorts/joins/aggregations to the local SSD instead of growing a 2 GB buffer.
    return max(128, min(512, psutil.virtual_memory().available // (8 * 1024**2)))


@contextmanager
def _bounded_connection(
    spill_root: Path,
    timeout_seconds: float,
) -> Iterator[duckdb.DuckDBPyConnection]:
    if not _QUERY_SLOTS.acquire(timeout=timeout_seconds):
        raise AggregatePlanError("Data queries are busy. Retry when the current preview finishes.")
    try:
        spill_root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="query-", dir=spill_root) as query_spill:
            connection = duckdb.connect(
                database=":memory:",
                config={
                    "autoinstall_known_extensions": "false",
                    "autoload_known_extensions": "false",
                    "memory_limit": f"{_query_memory_mib()}MiB",
                    "threads": "2",
                    "temp_directory": query_spill,
                    "max_temp_directory_size": "8GiB",
                },
            )
            timer = Timer(timeout_seconds, connection.interrupt)
            timer.daemon = True
            try:
                timer.start()
                yield connection
            finally:
                timer.cancel()
                if timer.ident is not None:
                    timer.join()
                connection.close()
    finally:
        _QUERY_SLOTS.release()


def _aggregate_expression(
    plan: ReadOnlyAggregatePlan,
    columns: dict[str, Any],
    numerator_where: str | None = None,
) -> str:
    column = _quote(columns[plan.value_column_id].name) if plan.value_column_id else None
    denominator = (
        _quote(columns[plan.denominator_column_id].name) if plan.denominator_column_id else None
    )
    return _dax_aggregate_expression(plan, column, denominator, numerator_where)


def _related_aggregate_expression(
    plan: ReadOnlyAggregatePlan,
    columns: dict[str, Any],
    numerator_where: str | None = None,
) -> str:
    column = f"m.{_quote(columns[plan.value_column_id].name)}" if plan.value_column_id else None
    denominator = (
        f"m.{_quote(columns[plan.denominator_column_id].name)}"
        if plan.denominator_column_id
        else None
    )
    return _dax_aggregate_expression(plan, column, denominator, numerator_where)


def _filter_operands(column: ColumnProfile | None, quoted: str, value: object) -> tuple[str, str]:
    if column is not None and column.data_type.value == "string" and isinstance(value, str):
        return f"lower(rtrim({quoted}))", "lower(rtrim(?))"
    return quoted, "?"


def _filter_condition(
    item: AggregateFilter, quoted: str, column: ColumnProfile | None = None
) -> str:
    """One parameterized filter over a quoted column reference."""
    quoted, parameter = _filter_operands(column, quoted, item.value)
    if item.operator == "contains":
        return f"contains(lower(CAST({quoted} AS VARCHAR)), lower({parameter}))"
    operator = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[
        item.operator
    ]
    return f"{quoted} {operator} {parameter}"


def _dax_aggregate_expression(
    plan: ReadOnlyAggregatePlan,
    column: str | None,
    denominator: str | None = None,
    numerator_where: str | None = None,
) -> str:
    if plan.operation in {"ratio", "difference"}:
        left = _operand_expression(plan.numerator_operation or "sum", column, numerator_where)
        right = _operand_expression(plan.denominator_operation or "sum", denominator)
        if plan.operation == "difference":
            # BLANK - BLANK stays BLANK; one blank operand behaves as zero.
            return (
                f"CASE WHEN ({left}) IS NULL AND ({right}) IS NULL THEN NULL "
                f"ELSE coalesce({left}, 0) - coalesce({right}, 0) END"
            )
        # DAX DIVIDE returns BLANK when the denominator is zero or missing.
        return (
            f"CASE WHEN ({right}) IS NULL OR ({right}) = 0 THEN NULL ELSE ({left}) / ({right}) END"
        )
    return _operand_expression(plan.operation, column)


def _operand_expression(operation: str, column: str | None, where: str | None = None) -> str:
    """One DAX aggregate in SQL, with BLANK represented as NULL.

    ``where`` restricts this operand alone (the CALCULATE side of a share).
    """
    only = f" FILTER (WHERE {where})" if where else ""
    if operation == "count":
        # COUNTA ignores blanks; COUNTROWS counts rows. Both return BLANK on no count.
        return f"nullif(count({column or '*'}){only}, 0)"
    assert column is not None
    if operation == "distinct_count":
        # DAX DISTINCTCOUNT includes one BLANK; SQL COUNT(DISTINCT) excludes NULL.
        # https://learn.microsoft.com/en-us/dax/distinctcount-function-dax
        return (
            f"CASE WHEN count(*){only} = 0 THEN NULL ELSE count(DISTINCT {column}){only} "
            f"+ CASE WHEN count(*){only} > count({column}){only} THEN 1 ELSE 0 END END"
        )
    function = {
        "sum": "sum",
        "average": "avg",
        "minimum": "min",
        "maximum": "max",
    }[operation]
    return f"{function}({column}){only}"


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")


def _scalar(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)
