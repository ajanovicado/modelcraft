from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ai_powerbi_analyst.analytics.compiler import compile_analysis
from ai_powerbi_analyst.analytics.executor import AnalyticsExecutor
from ai_powerbi_analyst.contracts import AnalysisOperation, AnalysisPlan
from ai_powerbi_analyst.data.build import build_demo_data
from ai_powerbi_analyst.data.catalog import load_semantic_catalog

CATALOG_PATH = Path(__file__).parents[2] / "fixtures" / "data" / "semantic_catalog.yaml"


@pytest.fixture(scope="module")
def analytics_runtime(tmp_path_factory):  # type: ignore[no-untyped-def]
    output = tmp_path_factory.mktemp("analytics-runtime")
    artifacts = build_demo_data(output)
    return artifacts, load_semantic_catalog(CATALOG_PATH)


@pytest.mark.parametrize(
    "plan",
    [
        AnalysisPlan(operation="kpi", metric_ids=["net_sales", "gross_margin_pct"]),
        AnalysisPlan(operation="comparison", metric_ids=["net_sales"]),
        AnalysisPlan(operation="trend", metric_ids=["net_sales"], dimension_ids=["region"]),
        AnalysisPlan(operation="ranking", metric_ids=["net_sales"], dimension_ids=["category"]),
        AnalysisPlan(
            operation="contribution",
            metric_ids=["gross_profit"],
            dimension_ids=["region"],
        ),
        AnalysisPlan(
            operation="driver_bridge",
            metric_ids=["gross_profit"],
            filters={"region": "West"},
        ),
        AnalysisPlan(operation="forecast", metric_ids=["net_sales", "gross_profit"]),
        AnalysisPlan(
            operation="scenario",
            metric_ids=["net_sales", "units", "gross_profit", "gross_margin_pct"],
            scenario_price_change_pct=3,
        ),
    ],
)
def test_governed_queries_execute_for_every_operation(analytics_runtime, plan) -> None:  # type: ignore[no-untyped-def]
    artifacts, catalog = analytics_runtime
    query = compile_analysis(plan, catalog, data_through=date(2025, 12, 31))

    rows = AnalyticsExecutor(artifacts.database, timeout_seconds=10).execute(query)

    assert rows
    assert len(rows) <= plan.row_limit
    if plan.operation == AnalysisOperation.SCENARIO:
        assert rows[0]["price_change_pct"] == 3
