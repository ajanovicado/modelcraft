"""Compile closed AnalysisPlan contracts into allowlisted parameterized SQL."""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, cast

import sqlglot
from sqlglot import exp

from ai_powerbi_analyst.contracts import (
    AnalysisOperation,
    AnalysisPlan,
    MetricDefinition,
    Scalar,
    SemanticCatalog,
    TimeWindow,
)

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_ALLOWED_TABLES = {
    "sales",
    "driver_contributions",
    "forecast",
    "forecast_selection",
    "scenario_grid",
}
_ALLOWED_FUNCTIONS = {
    "abs",
    "and",  # sqlglot models boolean conjunction as a Func node
    "case",
    "cast",
    "coalesce",
    "if",  # sqlglot's internal node for CASE branches
    "nullif",
    "sum",
}


class AnalysisValidationError(ValueError):
    """An analysis plan cannot be executed within the governed capability set."""


@dataclass(frozen=True, slots=True)
class GovernedQuery:
    operation: AnalysisOperation
    sql: str
    parameters: tuple[Scalar, ...]
    plan_hash: str
    data_through: date
    current_start: date
    current_end: date
    comparison_start: date | None
    comparison_end: date | None
    row_limit: int


def _month_shift(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def _year_shift(value: date, years: int) -> date:
    target_year = value.year + years
    last_day = calendar.monthrange(target_year, value.month)[1]
    return date(target_year, value.month, min(value.day, last_day))


def resolve_time_window(window: TimeWindow, data_through: date) -> tuple[date, date]:
    """Resolve a closed time preset without querying or trusting model arithmetic."""

    if window.start is not None and window.end is not None:
        if window.start > window.end:
            raise AnalysisValidationError("time window start must not be after end")
        if window.end > data_through:
            raise AnalysisValidationError("time window extends past the prepared data")
        return window.start, window.end

    preset = window.preset or "latest_quarter"
    if preset == "latest_month":
        return date(data_through.year, data_through.month, 1), data_through
    if preset == "latest_quarter":
        quarter_month = ((data_through.month - 1) // 3) * 3 + 1
        return date(data_through.year, quarter_month, 1), data_through
    if preset == "year_to_date":
        return date(data_through.year, 1, 1), data_through
    if preset == "last_12_months":
        first = _month_shift(date(data_through.year, data_through.month, 1), -11)
        return first, data_through
    if preset == "all_time":
        return date(1900, 1, 1), data_through
    raise AnalysisValidationError(f"unsupported time preset: {preset}")


def _comparison_window(
    current_start: date,
    current_end: date,
    comparison: str,
) -> tuple[date | None, date | None]:
    if comparison == "none":
        return None, None
    if comparison == "prior_year":
        return _year_shift(current_start, -1), _year_shift(current_end, -1)
    if comparison == "prior_period":
        current_month_end = calendar.monthrange(current_end.year, current_end.month)[1]
        if current_start.day == 1 and current_end.day == current_month_end:
            month_count = (
                (current_end.year - current_start.year) * 12
                + current_end.month
                - current_start.month
                + 1
            )
            return _month_shift(current_start, -month_count), current_start - timedelta(days=1)
        length = (current_end - current_start).days + 1
        comparison_end = current_start - timedelta(days=1)
        return comparison_end - timedelta(days=length - 1), comparison_end
    raise AnalysisValidationError(f"unsupported comparison: {comparison}")


def _metric(catalog: SemanticCatalog, metric_id: str) -> MetricDefinition:
    try:
        return catalog.resolve_metric(metric_id)
    except KeyError as error:
        raise AnalysisValidationError(f"unknown metric: {metric_id}") from error


def _identifier(name: str) -> str:
    if not _IDENTIFIER.fullmatch(name):
        raise AnalysisValidationError(f"unsafe catalog identifier: {name}")
    return f'"{name}"'


def _validate_plan(plan: AnalysisPlan, catalog: SemanticCatalog) -> None:
    metrics = [_metric(catalog, metric_id) for metric_id in plan.metric_ids]
    dimension_ids = set(plan.dimension_ids)
    dimensions = []
    for dimension_id in plan.dimension_ids:
        try:
            dimensions.append(catalog.resolve_dimension(dimension_id))
        except KeyError as error:
            raise AnalysisValidationError(f"unknown dimension: {dimension_id}") from error

    for filter_id in plan.filters:
        try:
            catalog.resolve_dimension(filter_id)
        except KeyError as error:
            raise AnalysisValidationError(f"unknown filter dimension: {filter_id}") from error
        if filter_id == "month":
            raise AnalysisValidationError("month filters must use the governed time window")

    for metric in metrics:
        invalid = dimension_ids - set(metric.allowed_dimensions)
        if invalid:
            raise AnalysisValidationError(
                f"metric {metric.id} does not support dimensions: {', '.join(sorted(invalid))}"
            )

    operation = plan.operation
    if operation == AnalysisOperation.KPI and dimensions:
        raise AnalysisValidationError("KPI analysis does not accept grouping dimensions")
    if operation == AnalysisOperation.COMPARISON and dimensions:
        raise AnalysisValidationError("comparison analysis does not accept grouping dimensions")
    if operation == AnalysisOperation.TREND:
        if len(dimensions) > 1:
            raise AnalysisValidationError("trend analysis supports at most one grouping dimension")
        if dimensions and dimensions[0].id == "month":
            raise AnalysisValidationError("trend already groups by month")
    if operation in {AnalysisOperation.RANKING, AnalysisOperation.CONTRIBUTION}:
        if len(dimensions) != 1:
            raise AnalysisValidationError(f"{operation.value} requires exactly one dimension")
        if len(metrics) != 1:
            raise AnalysisValidationError(f"{operation.value} requires exactly one metric")
    if operation == AnalysisOperation.CONTRIBUTION and not set(plan.metric_ids).issubset(
        {"net_sales", "units", "gross_profit"}
    ):
        raise AnalysisValidationError(
            "contribution supports only additive metrics: net_sales, units, gross_profit"
        )
    if operation == AnalysisOperation.CONTRIBUTION and plan.comparison == "none":
        raise AnalysisValidationError("contribution requires a comparison period")
    if operation == AnalysisOperation.DRIVER_BRIDGE:
        if plan.metric_ids != ["gross_profit"] or dimensions:
            raise AnalysisValidationError("driver bridge supports gross_profit without dimensions")
        if set(plan.filters) - {"region"}:
            raise AnalysisValidationError("driver bridge only accepts a region filter")
    if operation == AnalysisOperation.FORECAST:
        if not set(plan.metric_ids).issubset({"net_sales", "gross_profit"}) or dimensions:
            raise AnalysisValidationError(
                "forecast supports net_sales/gross_profit without dimensions"
            )
        if plan.filters:
            raise AnalysisValidationError("the approved forecast does not accept filters")
    if operation == AnalysisOperation.SCENARIO:
        allowed = {"net_sales", "units", "gross_profit", "gross_margin_pct"}
        if not set(plan.metric_ids).issubset(allowed) or dimensions or plan.filters:
            raise AnalysisValidationError("scenario supports only approved summary metrics")
        if plan.scenario_price_change_pct is None:
            raise AnalysisValidationError("scenario requires scenario_price_change_pct")
    elif plan.scenario_price_change_pct is not None:
        raise AnalysisValidationError("price change is only valid for scenario analysis")
    if "sales_growth" in plan.metric_ids and operation != AnalysisOperation.COMPARISON:
        raise AnalysisValidationError("sales_growth is only valid for comparison analysis")


def _where_clause(
    plan: AnalysisPlan,
    catalog: SemanticCatalog,
    start: date,
    end: date,
) -> tuple[str, list[Scalar]]:
    clauses = [
        "transaction_date >= CAST(? AS DATE)",
        "transaction_date <= CAST(? AS DATE)",
    ]
    parameters: list[Scalar] = [start.isoformat(), end.isoformat()]
    for dimension_id, value in sorted(plan.filters.items()):
        dimension = catalog.resolve_dimension(dimension_id)
        clauses.append(f"{_identifier(dimension.column)} = ?")
        parameters.append(value)
    return " AND ".join(clauses), parameters


def _metric_expression(metric: MetricDefinition) -> str:
    return metric.sql_expression


def _compile_kpi(
    plan: AnalysisPlan,
    catalog: SemanticCatalog,
    current: tuple[date, date],
) -> tuple[str, list[Scalar]]:
    selects: list[str] = []
    parameters: list[Scalar] = []
    for metric_id in plan.metric_ids:
        metric = _metric(catalog, metric_id)
        where, values = _where_clause(plan, catalog, *current)
        selects.append(
            f"SELECT '{metric.id}' AS metric_id, {_metric_expression(metric)} AS value "  # noqa: S608 - fixed template; allowlisted catalog identifiers; values parameterized
            f"FROM sales WHERE {where}"
        )
        parameters.extend(values)
    return " UNION ALL ".join(selects), parameters


def _compile_comparison(
    plan: AnalysisPlan,
    catalog: SemanticCatalog,
    current: tuple[date, date],
    comparison: tuple[date, date],
) -> tuple[str, list[Scalar]]:
    selects: list[str] = []
    parameters: list[Scalar] = []
    for metric_id in plan.metric_ids:
        metric = _metric(catalog, metric_id)
        current_where, current_values = _where_clause(plan, catalog, *current)
        comparison_where, comparison_values = _where_clause(plan, catalog, *comparison)
        expression = _metric_expression(metric)
        selects.append(
            "SELECT "  # noqa: S608 - fixed template; allowlisted catalog identifiers; values parameterized
            f"'{metric.id}' AS metric_id, current_value, comparison_value, "
            "current_value - comparison_value AS absolute_change, "
            "CASE WHEN comparison_value = 0 THEN NULL "
            "ELSE (current_value - comparison_value) / ABS(comparison_value) END AS pct_change "
            "FROM (SELECT "
            f"(SELECT {expression} FROM sales WHERE {current_where}) AS current_value, "
            f"(SELECT {expression} FROM sales WHERE {comparison_where}) "
            "AS comparison_value) values_"
        )
        parameters.extend(current_values)
        parameters.extend(comparison_values)
    return " UNION ALL ".join(selects), parameters


def _compile_trend(
    plan: AnalysisPlan,
    catalog: SemanticCatalog,
    current: tuple[date, date],
) -> tuple[str, list[Scalar]]:
    dimension = catalog.resolve_dimension(plan.dimension_ids[0]) if plan.dimension_ids else None
    group_column = _identifier(dimension.column) if dimension else None
    selects: list[str] = []
    parameters: list[Scalar] = []
    for metric_id in plan.metric_ids:
        metric = _metric(catalog, metric_id)
        where, values = _where_clause(plan, catalog, *current)
        group_value = f"CAST({group_column} AS VARCHAR)" if group_column else "NULL"
        group_by = f", {group_column}" if group_column else ""
        selects.append(
            "SELECT CAST(month_start AS VARCHAR) AS period, "  # noqa: S608 - fixed template; allowlisted catalog identifiers; values parameterized
            f"{group_value} AS group_value, '{metric.id}' AS metric_id, "
            f"{_metric_expression(metric)} AS value FROM sales WHERE {where} "
            f"GROUP BY month_start{group_by}"
        )
        parameters.extend(values)
    direction = "ASC" if plan.sort_direction == "asc" else "DESC"
    sql = (
        f"SELECT * FROM ({' UNION ALL '.join(selects)}) trend_values "  # noqa: S608 - fixed template; allowlisted catalog identifiers; values parameterized
        f"ORDER BY period {direction}, group_value, metric_id LIMIT {plan.row_limit}"
    )
    return sql, parameters


def _compile_ranking(
    plan: AnalysisPlan,
    catalog: SemanticCatalog,
    current: tuple[date, date],
) -> tuple[str, list[Scalar]]:
    metric = _metric(catalog, plan.metric_ids[0])
    dimension = catalog.resolve_dimension(plan.dimension_ids[0])
    column = _identifier(dimension.column)
    where, parameters = _where_clause(plan, catalog, *current)
    direction = "ASC" if plan.sort_direction == "asc" else "DESC"
    sql = (
        f"SELECT CAST({column} AS VARCHAR) AS rank_key, '{metric.id}' AS metric_id, "  # noqa: S608 - fixed template; allowlisted catalog identifiers; values parameterized
        f"{_metric_expression(metric)} AS value FROM sales WHERE {where} GROUP BY {column} "
        f"ORDER BY value {direction}, rank_key LIMIT {min(plan.ranking_limit, plan.row_limit)}"
    )
    return sql, parameters


def _compile_contribution(
    plan: AnalysisPlan,
    catalog: SemanticCatalog,
    current: tuple[date, date],
    comparison: tuple[date, date],
) -> tuple[str, list[Scalar]]:
    metric = _metric(catalog, plan.metric_ids[0])
    dimension = catalog.resolve_dimension(plan.dimension_ids[0])
    column = _identifier(dimension.column)
    current_where, current_parameters = _where_clause(plan, catalog, *current)
    comparison_where, comparison_parameters = _where_clause(plan, catalog, *comparison)
    expression = _metric_expression(metric)
    direction = "ASC" if plan.sort_direction == "asc" else "DESC"
    sql = (
        "WITH current_values AS ("  # noqa: S608 - fixed template; allowlisted catalog identifiers; values parameterized
        f"SELECT {column} AS contributor, {expression} AS current_value FROM sales "
        f"WHERE {current_where} GROUP BY {column}), "
        "comparison_values AS ("
        f"SELECT {column} AS contributor, {expression} AS comparison_value FROM sales "
        f"WHERE {comparison_where} GROUP BY {column}) "
        "SELECT CAST(COALESCE(c.contributor, p.contributor) AS VARCHAR) AS contributor, "
        f"'{metric.id}' AS metric_id, COALESCE(c.current_value, 0) AS current_value, "
        "COALESCE(p.comparison_value, 0) AS comparison_value, "
        "COALESCE(c.current_value, 0) - COALESCE(p.comparison_value, 0) AS contribution "
        "FROM current_values c FULL OUTER JOIN comparison_values p USING (contributor) "
        f"ORDER BY contribution {direction}, contributor "
        f"LIMIT {min(plan.ranking_limit, plan.row_limit)}"
    )
    return sql, [*current_parameters, *comparison_parameters]


def _compile_driver(plan: AnalysisPlan) -> tuple[str, list[Scalar]]:
    region = plan.filters.get("region", "All")
    sql = (
        "SELECT driver, contribution_value, gross_profit_change, current_period, "  # noqa: S608 - fixed template; allowlisted catalog identifiers; values parameterized
        "comparison_period, "
        "current_net_sales, comparison_net_sales, current_gross_profit, "
        "comparison_gross_profit, current_gross_margin_pct, comparison_gross_margin_pct "
        "FROM driver_contributions WHERE region_filter = ? "
        f"ORDER BY driver_order LIMIT {plan.row_limit}"
    )
    return sql, [region]


def _compile_forecast(plan: AnalysisPlan) -> tuple[str, list[Scalar]]:
    placeholders = ", ".join("?" for _ in plan.metric_ids)
    sql = (
        "SELECT CAST(f.period AS VARCHAR) AS period, f.metric_id, f.point_forecast, f.lower_80, "  # noqa: S608 - fixed template; allowlisted catalog identifiers; values parameterized
        "f.upper_80, f.lower_95, f.upper_95, f.method, s.mean_wape, "
        "s.wins_vs_seasonal_naive, s.status AS approval_status "
        "FROM forecast f INNER JOIN forecast_selection s ON f.metric_id = s.metric_id "
        f"WHERE f.metric_id IN ({placeholders}) "
        f"ORDER BY f.period, f.metric_id LIMIT {plan.row_limit}"
    )
    return sql, list(plan.metric_ids)


def _compile_scenario(plan: AnalysisPlan) -> tuple[str, list[Scalar]]:
    sql = (
        "SELECT price_change_pct, elasticity, baseline_period_start, baseline_period_end, "
        "baseline_source, baseline_net_sales, baseline_gross_profit, "
        "baseline_units, baseline_price, "
        "baseline_discount_rate, baseline_unit_cost, scenario_units, scenario_price, "
        "net_sales, cogs, gross_profit, gross_margin_pct, assumption "
        "FROM scenario_grid WHERE price_change_pct = ? LIMIT 1"
    )
    assert plan.scenario_price_change_pct is not None
    return sql, [plan.scenario_price_change_pct]


def validate_generated_sql(sql: str) -> None:
    """Defense in depth for compiler output before it reaches DuckDB."""

    if ";" in sql or "--" in sql or "/*" in sql:
        raise AnalysisValidationError("generated SQL contains forbidden syntax")
    try:
        statement = sqlglot.parse_one(sql, read="duckdb")
    except sqlglot.errors.ParseError as error:
        raise AnalysisValidationError("generated SQL is invalid") from error
    if not isinstance(statement, (exp.Select, exp.Union)):
        raise AnalysisValidationError("generated SQL is not a SELECT")
    cte_names = {cte.alias_or_name.casefold() for cte in statement.find_all(exp.CTE)}
    tables = {
        table.name.casefold()
        for table in statement.find_all(exp.Table)
        if table.name.casefold() not in cte_names
    }
    if not tables or not tables.issubset(_ALLOWED_TABLES):
        raise AnalysisValidationError("generated SQL references a forbidden table")
    forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Command)
    if any(statement.find(node_type) is not None for node_type in forbidden):
        raise AnalysisValidationError("generated SQL contains a forbidden operation")
    functions = {
        cast(Callable[[], str], function.sql_name)().casefold()
        for function in statement.find_all(exp.Func)
    }
    disallowed_functions = functions - _ALLOWED_FUNCTIONS
    if disallowed_functions:
        raise AnalysisValidationError(
            "generated SQL references forbidden functions: "
            + ", ".join(sorted(disallowed_functions))
        )


def compile_analysis(
    plan: AnalysisPlan,
    catalog: SemanticCatalog,
    *,
    data_through: date,
) -> GovernedQuery:
    """Validate and compile one closed analysis request."""

    _validate_plan(plan, catalog)
    current = resolve_time_window(plan.time_window, data_through)
    comparison_values = _comparison_window(*current, plan.comparison)
    comparison: tuple[date, date] | None = None
    if comparison_values[0] is not None and comparison_values[1] is not None:
        comparison = (comparison_values[0], comparison_values[1])

    if plan.operation == AnalysisOperation.KPI:
        sql, parameters = _compile_kpi(plan, catalog, current)
    elif plan.operation == AnalysisOperation.COMPARISON:
        if comparison is None:
            raise AnalysisValidationError("comparison operation requires a comparison period")
        sql, parameters = _compile_comparison(plan, catalog, current, comparison)
    elif plan.operation == AnalysisOperation.TREND:
        sql, parameters = _compile_trend(plan, catalog, current)
    elif plan.operation == AnalysisOperation.RANKING:
        sql, parameters = _compile_ranking(plan, catalog, current)
    elif plan.operation == AnalysisOperation.CONTRIBUTION:
        if comparison is None:
            raise AnalysisValidationError("contribution operation requires a comparison period")
        sql, parameters = _compile_contribution(plan, catalog, current, comparison)
    elif plan.operation == AnalysisOperation.DRIVER_BRIDGE:
        sql, parameters = _compile_driver(plan)
    elif plan.operation == AnalysisOperation.FORECAST:
        sql, parameters = _compile_forecast(plan)
    elif plan.operation == AnalysisOperation.SCENARIO:
        sql, parameters = _compile_scenario(plan)
    else:  # pragma: no cover - StrEnum is closed, kept as a defensive guard
        raise AnalysisValidationError(f"unsupported operation: {plan.operation}")

    validate_generated_sql(sql)
    hash_payload: dict[str, Any] = {
        "plan": plan.model_dump(mode="json"),
        "catalog_version": catalog.version,
        "data_through": data_through.isoformat(),
        "current": [value.isoformat() for value in current],
        "comparison": [value.isoformat() if value else None for value in comparison_values],
    }
    plan_hash = hashlib.sha256(
        json.dumps(hash_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return GovernedQuery(
        operation=plan.operation,
        sql=sql,
        parameters=tuple(parameters),
        plan_hash=plan_hash,
        data_through=data_through,
        current_start=current[0],
        current_end=current[1],
        comparison_start=comparison_values[0],
        comparison_end=comparison_values[1],
        row_limit=plan.row_limit,
    )
