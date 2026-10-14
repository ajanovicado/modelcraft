"""Load and safety-check the governed semantic catalog."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

from ai_powerbi_analyst.contracts import SemanticCatalog

_SALES_COLUMNS = {
    "units",
    "list_price",
    "discount_rate",
    "net_sales",
    "unit_cost",
    "cogs",
    "gross_profit",
}
_GOVERNED_METRIC_EXPRESSIONS = {
    "net_sales": "SUM(net_sales)",
    "units": "SUM(units)",
    "gross_profit": "SUM(gross_profit)",
    "gross_margin_pct": "SUM(gross_profit) / NULLIF(SUM(net_sales), 0)",
    "average_selling_price": "SUM(net_sales) / NULLIF(SUM(units), 0)",
    "discount_rate": "1 - SUM(net_sales) / NULLIF(SUM(units * list_price), 0)",
    "unit_cost": "SUM(cogs) / NULLIF(SUM(units), 0)",
    "sales_growth": "SUM(net_sales)",
}
_GOVERNED_DIMENSION_COLUMNS = {
    "month": "month_start",
    "region": "region",
    "product": "product",
    "category": "category",
    "segment": "segment",
    "channel": "channel",
}
_GOVERNED_REPORT_OBJECTS = {
    "overview_latest_quarter_net_sales": (
        "'Measures'[Latest Quarter Net Sales]",
        "measure",
        "overview",
    ),
    "overview_latest_quarter_gross_profit": (
        "'Measures'[Latest Quarter Gross Profit]",
        "measure",
        "overview",
    ),
    "overview_latest_quarter_gross_margin_pct": (
        "'Measures'[Latest Quarter Gross Margin %]",
        "measure",
        "overview",
    ),
    "overview_latest_quarter_sales_growth": (
        "'Measures'[Sales Growth]",
        "measure",
        "overview",
    ),
    "overview_executive_finding": (
        "'Measures'[Executive Finding]",
        "measure",
        "overview",
    ),
    "driver": ("'Driver Contributions'[Driver]", "column", "driver"),
    "driver_contribution_all": (
        "'Driver Contributions'[Driver Contribution — All]",
        "measure",
        "driver",
    ),
    "driver_contribution_west": (
        "'Driver Contributions'[Driver Contribution — West]",
        "measure",
        "driver",
    ),
    "driver_net_sales_change_pct": (
        "'Driver Contributions'[Latest Quarter Net Sales Change %]",
        "measure",
        "driver",
    ),
    "driver_gross_profit_change": (
        "'Driver Contributions'[Latest Quarter Gross Profit Change]",
        "measure",
        "driver",
    ),
    "driver_gross_margin_change_pp": (
        "'Driver Contributions'[Latest Quarter Gross Margin Change pp]",
        "measure",
        "driver",
    ),
    "driver_discount_contribution": (
        "'Driver Contributions'[Discount Contribution]",
        "measure",
        "driver",
    ),
    "forecast_period": ("'Forecast'[Period]", "column", "forecast"),
    "forecast_net_sales": ("'Forecast'[Forecast Net Sales]", "measure", "forecast"),
    "forecast_net_sales_lower_80": (
        "'Forecast'[Forecast Net Sales Lower 80]",
        "measure",
        "forecast",
    ),
    "forecast_net_sales_upper_80": (
        "'Forecast'[Forecast Net Sales Upper 80]",
        "measure",
        "forecast",
    ),
    "forecast_net_sales_lower_95": (
        "'Forecast'[Forecast Net Sales Lower 95]",
        "measure",
        "forecast",
    ),
    "forecast_net_sales_upper_95": (
        "'Forecast'[Forecast Net Sales Upper 95]",
        "measure",
        "forecast",
    ),
    "forecast_gross_profit": (
        "'Forecast'[Forecast Gross Profit]",
        "measure",
        "forecast",
    ),
    "next_quarter_forecast_net_sales": (
        "'Forecast'[Next Quarter Forecast Net Sales]",
        "measure",
        "forecast",
    ),
    "next_quarter_forecast_gross_profit": (
        "'Forecast'[Next Quarter Forecast Gross Profit]",
        "measure",
        "forecast",
    ),
    "forecast_provenance": (
        "'Forecast'[Forecast Provenance]",
        "measure",
        "forecast",
    ),
    "scenario_net_sales": (
        "'Scenario Grid'[Approved Scenario Net Sales]",
        "measure",
        "scenario",
    ),
    "scenario_units": (
        "'Scenario Grid'[Approved Scenario Units]",
        "measure",
        "scenario",
    ),
    "scenario_gross_profit": (
        "'Scenario Grid'[Approved Scenario Gross Profit]",
        "measure",
        "scenario",
    ),
    "scenario_gross_margin_pct": (
        "'Scenario Grid'[Approved Scenario Gross Margin %]",
        "measure",
        "scenario",
    ),
    "scenario_assumption": (
        "'Scenario Grid'[Approved Scenario Assumption]",
        "measure",
        "scenario",
    ),
}


class CatalogSafetyError(ValueError):
    """Raised when a local catalog contains an unsafe SQL expression."""


def _canonical_expression(expression: str) -> str:
    try:
        parsed = sqlglot.parse_one(expression, read="duckdb")
    except sqlglot.errors.ParseError as error:
        raise CatalogSafetyError("metric expression is not valid DuckDB SQL") from error
    return parsed.sql(dialect="duckdb", normalize=True)


def _validate_metric_expression(metric_id: str, expression: str) -> None:
    lowered = expression.casefold()
    if ";" in expression or "--" in expression or "/*" in expression or "*/" in expression:
        raise CatalogSafetyError("metric expression contains statement or comment syntax")
    forbidden_keywords = (" drop ", " delete ", " update ", " insert ", " copy ")
    if any(keyword in lowered for keyword in forbidden_keywords):
        raise CatalogSafetyError("metric expression contains a forbidden operation")

    expected = _GOVERNED_METRIC_EXPRESSIONS.get(metric_id)
    if expected is None:
        raise CatalogSafetyError(f"metric {metric_id!r} is not in the v1 governed allowlist")
    if _canonical_expression(expression) != _canonical_expression(expected):
        raise CatalogSafetyError(
            f"metric {metric_id!r} does not use its canonical governed expression"
        )

    try:
        statements = sqlglot.parse(f"SELECT {expression} AS metric_value FROM sales", read="duckdb")  # noqa: S608 - parses the catalog's own governed expression for validation
    except sqlglot.errors.ParseError as error:
        raise CatalogSafetyError("metric expression is not valid DuckDB SQL") from error
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise CatalogSafetyError("metric expression must produce one SELECT expression")

    statement = statements[0]
    if len(list(statement.find_all(exp.Select))) != 1:
        raise CatalogSafetyError("metric expression cannot contain a subquery")
    tables = {table.name.casefold() for table in statement.find_all(exp.Table)}
    if tables != {"sales"}:
        raise CatalogSafetyError("metric expression references an unexpected table")
    columns = {column.name.casefold() for column in statement.find_all(exp.Column)}
    if not columns.issubset(_SALES_COLUMNS):
        raise CatalogSafetyError("metric expression references an unexpected column")


def catalog_sha256(path: str | Path) -> str:
    """Hash exact catalog bytes for ProjectBundle/manifest provenance."""

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_semantic_catalog(path: str | Path) -> SemanticCatalog:
    """Load a YAML catalog and reject executable or out-of-scope SQL."""

    catalog_path = Path(path)
    payload: Any = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    catalog = SemanticCatalog.model_validate(payload)
    metric_ids = {metric.id for metric in catalog.metrics}
    if metric_ids != set(_GOVERNED_METRIC_EXPRESSIONS):
        raise CatalogSafetyError("catalog must contain the exact v1 governed metric set")
    for metric in catalog.metrics:
        _validate_metric_expression(metric.id, metric.sql_expression)
    dimension_ids = {dimension.id for dimension in catalog.dimensions}
    if dimension_ids != set(_GOVERNED_DIMENSION_COLUMNS):
        raise CatalogSafetyError("catalog must contain the exact v1 governed dimension set")
    for dimension in catalog.dimensions:
        if dimension.column != _GOVERNED_DIMENSION_COLUMNS[dimension.id]:
            raise CatalogSafetyError(
                f"dimension {dimension.id!r} does not use its governed source column"
            )
    report_objects = {
        item.id: (item.powerbi_ref, item.kind, item.source) for item in catalog.report_objects
    }
    if report_objects != _GOVERNED_REPORT_OBJECTS:
        raise CatalogSafetyError("catalog must contain the exact v1 report-object set")
    return catalog
