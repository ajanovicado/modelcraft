from __future__ import annotations

from datetime import date
from pathlib import Path

from ai_powerbi_analyst.analytics.compiler import compile_analysis
from ai_powerbi_analyst.analytics.evidence import _period_label, render_evidence_answer
from ai_powerbi_analyst.contracts import AnalysisPlan, TimeWindow
from ai_powerbi_analyst.data.catalog import load_semantic_catalog

CATALOG_PATH = Path(__file__).parents[2] / "fixtures" / "data" / "semantic_catalog.yaml"
DATA_HASH = "d" * 64
CATALOG_HASH = "e" * 64


def test_period_label_requires_the_exact_calendar_quarter_end() -> None:
    assert _period_label(date(2025, 1, 1), date(2025, 3, 31)) == "2025 Q1"
    assert _period_label(date(2025, 1, 1), date(2025, 3, 28)) == ("2025-01-01 to 2025-03-28")


def test_comparison_answer_is_deterministic_and_provenanced() -> None:
    catalog = load_semantic_catalog(CATALOG_PATH)
    plan = AnalysisPlan(operation="comparison", metric_ids=["gross_margin_pct"])
    query = compile_analysis(plan, catalog, data_through=date(2025, 12, 31))
    rows = [
        {
            "metric_id": "gross_margin_pct",
            "current_value": 0.312,
            "comparison_value": 0.328,
            "absolute_change": -0.016,
            "pct_change": -0.0487804878,
        }
    ]

    first = render_evidence_answer(plan, query, rows, catalog, DATA_HASH, CATALOG_HASH)
    second = render_evidence_answer(plan, query, rows, catalog, DATA_HASH, CATALOG_HASH)

    assert first == second
    assert first.data_hash == DATA_HASH
    assert first.catalog_hash == CATALOG_HASH
    assert first.query_plan_hash == query.plan_hash
    assert "31.2%" in first.conclusion
    assert "1.6 percentage points" in first.conclusion
    assert first.metric_ids == ["gross_margin_pct"]
    assert first.chart.x_field == "current_value"
    assert first.chart.y_field == "metric_id"


def test_driver_answer_uses_noncausal_governed_wording() -> None:
    catalog = load_semantic_catalog(CATALOG_PATH)
    plan = AnalysisPlan(
        operation="driver_bridge",
        metric_ids=["gross_profit"],
        filters={"region": "West"},
    )
    query = compile_analysis(plan, catalog, data_through=date(2025, 12, 31))
    rows = [
        {
            "driver": "Cost",
            "contribution_value": -125000.0,
            "gross_profit_change": -80000.0,
            "current_period": "2025 Q4",
            "comparison_period": "2024 Q4",
            "current_net_sales": 1_200_000.0,
            "comparison_net_sales": 1_000_000.0,
            "current_gross_margin_pct": 0.30,
            "comparison_gross_margin_pct": 0.38,
        },
        {
            "driver": "Volume",
            "contribution_value": 45000.0,
            "gross_profit_change": -80000.0,
            "current_period": "2025 Q4",
            "comparison_period": "2024 Q4",
            "current_net_sales": 1_200_000.0,
            "comparison_net_sales": 1_000_000.0,
            "current_gross_margin_pct": 0.30,
            "comparison_gross_margin_pct": 0.38,
        },
    ]

    answer = render_evidence_answer(plan, query, rows, catalog, DATA_HASH, CATALOG_HASH)

    assert "contributed" in answer.conclusion
    assert "caused" not in answer.conclusion.casefold()
    assert "Net Sales rose 20.0%" in answer.conclusion
    assert "Gross Margin fell 8.0 percentage points" in answer.conclusion
    assert "more than offsetting" in answer.conclusion
    assert answer.chart.kind == "contribution_bar"
    assert answer.add_to_report_supported is True
    assert answer.target_page_key == "margin_drivers"


def test_forecast_answer_is_labeled_as_approved_forecast() -> None:
    catalog = load_semantic_catalog(CATALOG_PATH)
    plan = AnalysisPlan(operation="forecast", metric_ids=["net_sales"])
    query = compile_analysis(plan, catalog, data_through=date(2025, 12, 31))
    rows = [
        {
            "period": "2026-01-01",
            "metric_id": "net_sales",
            "point_forecast": 1000000.0,
            "lower_80": 900000.0,
            "upper_80": 1100000.0,
            "lower_95": 800000.0,
            "upper_95": 1200000.0,
            "method": "seasonal_drift",
            "mean_wape": 0.025,
            "wins_vs_seasonal_naive": 3,
        }
    ]

    answer = render_evidence_answer(plan, query, rows, catalog, DATA_HASH, CATALOG_HASH)

    assert "approved forecast" in answer.conclusion.casefold()
    assert "sum of the three monthly 80% bounds" in answer.conclusion
    assert "not a calibrated aggregate-quarter interval" in answer.conclusion
    assert "2.5%" in " ".join(answer.facts)
    assert answer.add_to_report_supported is False


def test_scenario_answer_includes_revenue_volume_profit_and_mechanical_label() -> None:
    catalog = load_semantic_catalog(CATALOG_PATH)
    plan = AnalysisPlan(
        operation="scenario",
        metric_ids=["net_sales", "units", "gross_profit"],
        scenario_price_change_pct=3,
    )
    query = compile_analysis(plan, catalog, data_through=date(2025, 12, 31))
    rows = [
        {
            "price_change_pct": 3,
            "elasticity": -0.8,
            "baseline_period_start": "2026-01-01",
            "baseline_period_end": "2026-03-31",
            "baseline_source": "approved_next_quarter_forecast",
            "baseline_net_sales": 100000.0,
            "baseline_gross_profit": 40000.0,
            "baseline_units": 1000.0,
            "baseline_price": 100.0,
            "baseline_discount_rate": 0.0,
            "baseline_unit_cost": 60.0,
            "scenario_units": 976.0,
            "scenario_price": 103.0,
            "net_sales": 100528.0,
            "cogs": 58560.0,
            "gross_profit": 41968.0,
            "gross_margin_pct": 0.41748,
            "assumption": "Mechanical price scenario; not a prediction.",
        }
    ]

    answer = render_evidence_answer(plan, query, rows, catalog, DATA_HASH, CATALOG_HASH)

    assert "100,528" in answer.conclusion
    assert "976 units" in answer.conclusion
    assert "41,968" in answer.conclusion
    assert "next-quarter" in answer.conclusion
    assert "approved next-quarter" in " ".join(answer.facts)
    assert "not a prediction" in " ".join(answer.facts)
    assert answer.data_through == date(2025, 12, 31)
    assert answer.metric_definition_refs


def test_evidence_freshness_is_fixture_cutoff_not_query_window_end() -> None:
    catalog = load_semantic_catalog(CATALOG_PATH)
    plan = AnalysisPlan(
        operation="kpi",
        metric_ids=["net_sales"],
        time_window=TimeWindow(start=date(2024, 1, 1), end=date(2024, 12, 31)),
    )
    query = compile_analysis(plan, catalog, data_through=date(2025, 12, 31))

    answer = render_evidence_answer(
        plan,
        query,
        [{"metric_id": "net_sales", "value": 1_000_000.0}],
        catalog,
        DATA_HASH,
        CATALOG_HASH,
    )

    assert answer.data_through == date(2025, 12, 31)
    assert "2024" in answer.conclusion
