from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ai_powerbi_analyst.analytics.compiler import (
    AnalysisValidationError,
    compile_analysis,
    validate_generated_sql,
)
from ai_powerbi_analyst.contracts import AnalysisOperation, AnalysisPlan, TimeWindow
from ai_powerbi_analyst.data.catalog import load_semantic_catalog

CATALOG_PATH = Path(__file__).parents[2] / "fixtures" / "data" / "semantic_catalog.yaml"


@pytest.fixture
def catalog():  # type: ignore[no-untyped-def]
    return load_semantic_catalog(CATALOG_PATH)


@pytest.mark.parametrize(
    ("operation", "metric_ids", "dimension_ids", "extra"),
    [
        (AnalysisOperation.KPI, ["net_sales"], [], {}),
        (AnalysisOperation.COMPARISON, ["gross_margin_pct"], [], {}),
        (AnalysisOperation.TREND, ["net_sales"], ["region"], {}),
        (AnalysisOperation.RANKING, ["net_sales"], ["category"], {}),
        (AnalysisOperation.CONTRIBUTION, ["gross_profit"], ["region"], {}),
        (AnalysisOperation.DRIVER_BRIDGE, ["gross_profit"], [], {"filters": {"region": "West"}}),
        (AnalysisOperation.FORECAST, ["net_sales", "gross_profit"], [], {}),
        (
            AnalysisOperation.SCENARIO,
            ["net_sales", "units", "gross_profit", "gross_margin_pct"],
            [],
            {"scenario_price_change_pct": 3},
        ),
    ],
)
def test_compiler_supports_all_eight_governed_operations(
    catalog,
    operation,
    metric_ids,
    dimension_ids,
    extra,  # type: ignore[no-untyped-def]
) -> None:
    plan = AnalysisPlan(
        operation=operation,
        metric_ids=metric_ids,
        dimension_ids=dimension_ids,
        **extra,
    )

    query = compile_analysis(plan, catalog, data_through=date(2025, 12, 31))

    assert query.operation == operation
    assert query.sql.lstrip().upper().startswith(("SELECT", "WITH"))
    assert ";" not in query.sql
    assert len(query.plan_hash) == 64


def test_compiler_rejects_unknown_metric_before_sql_generation(catalog) -> None:  # type: ignore[no-untyped-def]
    plan = AnalysisPlan(operation="kpi", metric_ids=["drop_everything"])

    with pytest.raises(AnalysisValidationError, match="unknown metric"):
        compile_analysis(plan, catalog, data_through=date(2025, 12, 31))


def test_compiler_rejects_filter_that_is_not_a_governed_dimension(catalog) -> None:  # type: ignore[no-untyped-def]
    plan = AnalysisPlan(
        operation="trend",
        metric_ids=["net_sales"],
        filters={"raw_sql": "1=1"},
    )

    with pytest.raises(AnalysisValidationError, match="unknown filter dimension"):
        compile_analysis(plan, catalog, data_through=date(2025, 12, 31))


def test_compiler_parameterizes_filter_values(catalog) -> None:  # type: ignore[no-untyped-def]
    attack = "West' OR 1=1 --"
    plan = AnalysisPlan(
        operation="ranking",
        metric_ids=["net_sales"],
        dimension_ids=["category"],
        filters={"region": attack},
    )

    query = compile_analysis(plan, catalog, data_through=date(2025, 12, 31))

    assert attack not in query.sql
    assert attack in query.parameters
    assert "?" in query.sql


def test_explicit_time_window_is_reflected_in_compiled_window(catalog) -> None:  # type: ignore[no-untyped-def]
    plan = AnalysisPlan(
        operation="kpi",
        metric_ids=["net_sales"],
        time_window=TimeWindow(start=date(2025, 1, 1), end=date(2025, 3, 31)),
    )

    query = compile_analysis(plan, catalog, data_through=date(2025, 12, 31))

    assert query.current_start == date(2025, 1, 1)
    assert query.current_end == date(2025, 3, 31)


@pytest.mark.parametrize(
    ("start", "end", "expected_start", "expected_end"),
    [
        (
            date(2025, 11, 1),
            date(2025, 11, 30),
            date(2025, 10, 1),
            date(2025, 10, 31),
        ),
        (
            date(2025, 4, 1),
            date(2025, 6, 30),
            date(2025, 1, 1),
            date(2025, 3, 31),
        ),
    ],
)
def test_prior_period_keeps_complete_calendar_windows_aligned(
    catalog,  # type: ignore[no-untyped-def]
    start: date,
    end: date,
    expected_start: date,
    expected_end: date,
) -> None:
    plan = AnalysisPlan(
        operation="comparison",
        metric_ids=["net_sales"],
        time_window=TimeWindow(start=start, end=end),
        comparison="prior_period",
    )

    query = compile_analysis(plan, catalog, data_through=date(2025, 12, 31))

    assert query.comparison_start == expected_start
    assert query.comparison_end == expected_end


def test_contribution_rejects_nonadditive_ratio_metric(catalog) -> None:  # type: ignore[no-untyped-def]
    plan = AnalysisPlan(
        operation="contribution",
        metric_ids=["gross_margin_pct"],
        dimension_ids=["category"],
    )

    with pytest.raises(AnalysisValidationError, match="additive metrics"):
        compile_analysis(plan, catalog, data_through=date(2025, 12, 31))


def test_sql_defense_rejects_functions_outside_the_frozen_allowlist() -> None:
    with pytest.raises(AnalysisValidationError, match="forbidden functions: md5"):
        validate_generated_sql("SELECT MD5(region) FROM sales")
