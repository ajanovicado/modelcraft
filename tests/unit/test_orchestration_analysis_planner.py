from __future__ import annotations

from datetime import date

import pytest

from ai_powerbi_analyst.contracts import (
    AnalysisOperation,
    DimensionDefinition,
    MetricDefinition,
    SemanticCatalog,
)
from ai_powerbi_analyst.orchestration.analysis_planner import (
    AnalysisPlanningError,
    RuleFirstAnalysisPlanner,
)


def _catalog() -> SemanticCatalog:
    return SemanticCatalog(
        version="1.0.0",
        metrics=[
            MetricDefinition(
                id="net_sales",
                name="Net Sales",
                aliases=["revenue", "sales"],
                description="Revenue after discounts.",
                format="$,.0f",
                powerbi_ref="'Measures'[Net Sales]",
                sql_expression="SUM(net_sales)",
                allowed_dimensions=["region", "month", "segment"],
            ),
            MetricDefinition(
                id="gross_margin_pct",
                name="Gross Margin %",
                aliases=["margin", "margin percent"],
                description="Gross profit divided by net sales.",
                format=".1%",
                powerbi_ref="'Measures'[Gross Margin %]",
                sql_expression="SUM(gross_profit) / SUM(net_sales)",
                allowed_dimensions=["region", "month", "segment"],
            ),
            MetricDefinition(
                id="gross_profit",
                name="Gross Profit",
                aliases=["profit"],
                description="Net sales less cost of goods sold.",
                format="$,.0f",
                powerbi_ref="'Measures'[Gross Profit]",
                sql_expression="SUM(gross_profit)",
                allowed_dimensions=["region", "month", "segment", "category"],
            ),
            MetricDefinition(
                id="units",
                name="Units",
                aliases=["volume"],
                description="Units sold.",
                format=",.0f",
                powerbi_ref="'Measures'[Units]",
                sql_expression="SUM(units)",
                allowed_dimensions=["region", "month", "segment", "category"],
            ),
            MetricDefinition(
                id="sales_growth",
                name="Sales Growth",
                aliases=["revenue growth", "growth"],
                description="Change in Net Sales versus the governed comparison period.",
                format=".1%",
                powerbi_ref="'Measures'[Sales Growth]",
                sql_expression="SUM(net_sales)",
                allowed_dimensions=["region", "month", "segment", "category"],
                dependencies=["net_sales"],
            ),
        ],
        dimensions=[
            DimensionDefinition(
                id="region",
                name="Region",
                aliases=["territory"],
                column="region",
                powerbi_ref="'Region'[Region]",
                data_type="string",
            ),
            DimensionDefinition(
                id="month",
                name="Month",
                aliases=[],
                column="month",
                powerbi_ref="'Date'[Month]",
                data_type="date",
            ),
            DimensionDefinition(
                id="segment",
                name="Segment",
                aliases=["customer segment"],
                column="segment",
                powerbi_ref="'Customer'[Segment]",
                data_type="string",
            ),
            DimensionDefinition(
                id="category",
                name="Category",
                aliases=[],
                column="category",
                powerbi_ref="'Product'[Category]",
                data_type="string",
            ),
        ],
    )


def test_driver_question_compiles_to_governed_driver_bridge() -> None:
    planner = RuleFirstAnalysisPlanner(_catalog())

    plan = planner.plan("Why did margin fall in the West last quarter?")

    assert plan.operation is AnalysisOperation.DRIVER_BRIDGE
    assert plan.metric_ids == ["gross_profit"]
    assert plan.filters == {"region": "West"}
    assert plan.time_window.preset == "latest_quarter"


def test_canonical_contrast_treats_revenue_as_context_for_margin_driver_focus() -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan(
        "Revenue is up, so why did margin fall in the West last quarter?"
    )

    assert plan.operation is AnalysisOperation.DRIVER_BRIDGE
    assert plan.metric_ids == ["gross_profit"]
    assert plan.filters == {"region": "West"}


def test_governed_bridge_allows_a_separate_identify_instruction() -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan(
        "Show the governed margin bridge for the West and identify the largest "
        "arithmetic contribution."
    )

    assert plan.operation is AnalysisOperation.DRIVER_BRIDGE
    assert plan.filters == {"region": "West"}


@pytest.mark.parametrize(
    "question",
    [
        "What drove revenue growth last quarter?",
        "Why did net sales increase last quarter?",
        "What drove units last quarter?",
        "Why did revenue fall in the West last quarter?",
    ],
)
def test_driver_bridge_never_overwrites_an_explicit_unsupported_metric(
    question: str,
) -> None:
    with pytest.raises(AnalysisPlanningError, match="decomposes Gross Profit only"):
        RuleFirstAnalysisPlanner(_catalog()).plan(question)


@pytest.mark.parametrize(
    "question",
    [
        "What was sales growth last quarter?",
        "Show revenue growth in 2025 Q4",
        "How much did sales grow last quarter?",
    ],
)
def test_sales_growth_language_uses_the_comparison_growth_metric(question: str) -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan(question)

    assert plan.operation is AnalysisOperation.COMPARISON
    assert plan.metric_ids == ["sales_growth"]


@pytest.mark.parametrize(
    "question",
    [
        "What is net profit last quarter?",
        "What is operating margin last quarter?",
        "What was EBIT last quarter?",
        "What was ARR last quarter?",
        "What is customer acquisition cost?",
    ],
)
def test_unknown_or_qualified_metrics_never_fall_back_to_a_governed_metric(
    question: str,
) -> None:
    with pytest.raises(AnalysisPlanningError, match="approved metric"):
        RuleFirstAnalysisPlanner(_catalog()).plan(question)


@pytest.mark.parametrize(
    ("question", "filters"),
    [
        ("Show the margin drivers for West last quarter", {"region": "West"}),
        ("What were the gross profit drivers in West last quarter?", {"region": "West"}),
        (
            "Decompose the gross profit variance into price, volume, mix, discount, and cost.",
            {},
        ),
    ],
)
def test_plural_driver_questions_use_the_governed_bridge(
    question: str,
    filters: dict[str, str],
) -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan(question)

    assert plan.operation is AnalysisOperation.DRIVER_BRIDGE
    assert plan.filters == filters


def test_price_scenario_is_bounded_and_deterministic() -> None:
    planner = RuleFirstAnalysisPlanner(_catalog())

    plan = planner.plan("What happens to revenue with a 3% price increase?")

    assert plan.operation is AnalysisOperation.SCENARIO
    assert plan.metric_ids == ["net_sales", "units", "gross_profit", "gross_margin_pct"]
    assert plan.scenario_price_change_pct == 3


def test_scenario_allows_request_to_state_the_governed_elasticity() -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan(
        "Show the mechanical scenario at a 5% price change and state the elasticity assumption."
    )

    assert plan.operation is AnalysisOperation.SCENARIO
    assert plan.scenario_price_change_pct == 5


def test_top_region_question_compiles_to_ranking() -> None:
    planner = RuleFirstAnalysisPlanner(_catalog())

    plan = planner.plan("Show the top 5 regions by revenue")

    assert plan.operation is AnalysisOperation.RANKING
    assert plan.dimension_ids == ["region"]
    assert plan.ranking_limit == 5


def test_lowest_ranking_uses_ascending_sort() -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan(
        "Which categories had the lowest gross margin last quarter?"
    )

    assert plan.operation is AnalysisOperation.RANKING
    assert plan.sort_direction == "asc"


def test_prior_quarter_comparison_is_not_silently_changed_to_prior_year() -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan("Compare revenue with the prior quarter")

    assert plan.comparison == "prior_period"


def test_over_the_last_year_uses_twelve_month_window_and_segment_filter() -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan(
        "Show revenue for Enterprise customers over the last year"
    )

    assert plan.time_window.preset == "last_12_months"
    assert plan.filters == {"segment": "Enterprise"}


def test_out_of_range_price_scenario_is_rejected_instead_of_clamped() -> None:
    with pytest.raises(AnalysisPlanningError, match=r"-3% through \+5%"):
        RuleFirstAnalysisPlanner(_catalog()).plan("What happens with a 20% price increase?")


def test_driver_bridge_rejects_nonprepared_time_window() -> None:
    with pytest.raises(AnalysisPlanningError, match="latest complete quarter"):
        RuleFirstAnalysisPlanner(_catalog()).plan("Why did margin fall in the West last month?")


def test_unknown_orders_metric_is_rejected_instead_of_becoming_net_sales() -> None:
    with pytest.raises(AnalysisPlanningError, match="not an approved metric"):
        RuleFirstAnalysisPlanner(_catalog()).plan("How many orders did we receive?")


def test_unknown_customer_filter_is_rejected_instead_of_dropped() -> None:
    with pytest.raises(AnalysisPlanningError, match="Customer-level filters"):
        RuleFirstAnalysisPlanner(_catalog()).plan("Show revenue for Acme customers last quarter")


def test_forecast_rejects_recognized_filter_instead_of_dropping_it() -> None:
    with pytest.raises(AnalysisPlanningError, match="does not support filters"):
        RuleFirstAnalysisPlanner(_catalog()).plan("Forecast next quarter revenue for the West")


def test_kpi_grouping_is_rejected_instead_of_silently_removed() -> None:
    with pytest.raises(AnalysisPlanningError, match="grouping dimension"):
        RuleFirstAnalysisPlanner(_catalog()).plan("Show revenue by region")


@pytest.mark.parametrize(
    "question",
    [
        "Show net sales and warranty claims last quarter.",
        "Show net sales and headcount last quarter.",
        "Show gross profit and marketing spend last quarter.",
        "Trend net sales by region and department for the last 12 months.",
        "Top 5 regions by net sales and office last quarter.",
    ],
)
def test_mixed_known_and_unknown_semantics_never_answer_only_the_known_subset(
    question: str,
) -> None:
    with pytest.raises(AnalysisPlanningError, match="not partially applied"):
        RuleFirstAnalysisPlanner(_catalog()).plan(question)


@pytest.mark.parametrize(
    "question",
    [
        "Trend net sales and gross profit for the last 12 months.",
        "Trend net sales and gross profit by region for the last 12 months.",
    ],
)
def test_trend_requires_exactly_one_metric_to_avoid_mixed_axis_units(
    question: str,
) -> None:
    with pytest.raises(AnalysisPlanningError, match="exactly one metric"):
        RuleFirstAnalysisPlanner(_catalog()).plan(question)


def test_comparison_requires_exactly_one_metric_to_avoid_mixed_axis_units() -> None:
    with pytest.raises(AnalysisPlanningError, match="exactly one metric"):
        RuleFirstAnalysisPlanner(_catalog()).plan(
            "Compare net sales and gross margin percent last quarter versus last year."
        )


def test_explicit_calendar_year_is_not_silently_changed_to_latest_quarter() -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan("What was revenue in 2024?")

    assert plan.time_window.start == date(2024, 1, 1)
    assert plan.time_window.end == date(2024, 12, 31)
    assert plan.time_window.preset is None


@pytest.mark.parametrize(
    "question",
    [
        "What was revenue in Q2 2025?",
        "What was revenue for 2025 Q2?",
        "What was revenue in the second quarter of 2025?",
    ],
)
def test_explicit_calendar_quarter_is_parsed(question: str) -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan(question)

    assert plan.time_window.start == date(2025, 4, 1)
    assert plan.time_window.end == date(2025, 6, 30)
    assert plan.time_window.preset is None


def test_explicit_month_is_not_confused_with_a_grouping_request() -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan("What was revenue in November 2025?")

    assert plan.operation is AnalysisOperation.KPI
    assert plan.dimension_ids == []
    assert plan.time_window.start == date(2025, 11, 1)
    assert plan.time_window.end == date(2025, 11, 30)


def test_explicit_iso_date_range_is_parsed_exactly() -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan(
        "What was revenue from 2025-02-03 to 2025-02-28?"
    )

    assert plan.time_window.start == date(2025, 2, 3)
    assert plan.time_window.end == date(2025, 2, 28)


def test_named_calendar_date_is_parsed_exactly() -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan("What was revenue on March 15, 2025?")

    assert plan.time_window.start == date(2025, 3, 15)
    assert plan.time_window.end == date(2025, 3, 15)


def test_latest_month_kpi_regression_remains_supported() -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan("What was revenue last month?")

    assert plan.operation is AnalysisOperation.KPI
    assert plan.dimension_ids == []
    assert plan.time_window.preset == "latest_month"


@pytest.mark.parametrize(
    "question",
    [
        "What was revenue in the most recent month?",
        "Compare revenue month-over-month",
        "Compare revenue with the prior month",
    ],
)
def test_recent_month_comparisons_use_a_month_window(question: str) -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan(question)

    assert plan.time_window.preset == "latest_month"
    assert plan.operation is not AnalysisOperation.RANKING
    if plan.operation is AnalysisOperation.COMPARISON:
        assert plan.comparison == "prior_period"


def test_latest_period_kpi_is_not_treated_as_a_grouping_request() -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan("What was revenue in the latest period?")

    assert plan.operation is AnalysisOperation.KPI
    assert plan.dimension_ids == []
    assert plan.time_window.preset == "latest_quarter"


@pytest.mark.parametrize(
    "question",
    [
        "What was revenue on 03/04/2025?",
        "What was revenue in FY2025?",
        "What was revenue in Q5 2025?",
        "What was revenue in spring 2025?",
        "What was revenue in January?",
        "What was revenue this week?",
        "What was revenue in quarter 2 of 2025?",
        "What was revenue on 05-01-2025?",
        "What was revenue over the last 90 days?",
        "Compare revenue in Q4 2025 versus Q4 2024",
        "What was revenue before 2025?",
        "What was revenue after 2024?",
        "What was revenue since January 2025?",
        "What was revenue until March 2025?",
        "What was revenue in the first half of 2025?",
        "What were net sales last year?",
        "What were net sales previous year?",
        "What were net sales for the prior year?",
        "What were net sales two years ago?",
    ],
)
def test_unsupported_or_ambiguous_date_forms_fail_closed(question: str) -> None:
    with pytest.raises(AnalysisPlanningError, match="date or period"):
        RuleFirstAnalysisPlanner(_catalog()).plan(question)


@pytest.mark.parametrize(
    "question",
    [
        "Compare gross margin last quarter versus last year.",
        "Compare net sales last quarter vs last year.",
    ],
)
def test_prior_year_comparison_accepts_common_last_year_synonym(question: str) -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan(question)

    assert plan.operation is AnalysisOperation.COMPARISON
    assert plan.comparison == "prior_year"
    assert plan.time_window.preset == "latest_quarter"


@pytest.mark.parametrize(
    ("question", "filters"),
    [
        ("Show net sales for Region West last quarter.", {"region": "West"}),
        (
            "Show net sales filtered to Region West last quarter.",
            {"region": "West"},
        ),
        ("Show net sales where Region is West last quarter.", {"region": "West"}),
        (
            "Show gross margin for Category Software last quarter.",
            {"category": "Software"},
        ),
    ],
)
def test_dimension_label_and_governed_value_are_parsed_as_a_filter(
    question: str,
    filters: dict[str, str],
) -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan(question)

    assert plan.operation is AnalysisOperation.KPI
    assert plan.dimension_ids == []
    assert plan.filters == filters


@pytest.mark.parametrize(
    "question",
    [
        "Compare net sales in West last quarter versus prior year.",
        "Compare net sales in West last quarter to prior year.",
    ],
)
def test_prior_year_comparison_accepts_prior_year_qualifier(question: str) -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan(question)

    assert plan.operation is AnalysisOperation.COMPARISON
    assert plan.comparison == "prior_year"
    assert plan.time_window.preset == "latest_quarter"
    assert plan.filters == {"region": "West"}


@pytest.mark.parametrize(
    "question",
    [
        "Show net sales and cash flow last quarter.",
        "Show revenue and bookings last quarter.",
        "Show net sales and customer satisfaction last quarter.",
        "Trend gross profit and taxes over the last 12 months.",
        "Top 5 categories by net sales and returns last quarter.",
    ],
)
def test_mixed_approved_and_unknown_metrics_are_never_partially_applied(
    question: str,
) -> None:
    with pytest.raises(AnalysisPlanningError, match="not an approved metric"):
        RuleFirstAnalysisPlanner(_catalog()).plan(question)


def test_fixed_forecast_rejects_explicit_calendar_period() -> None:
    with pytest.raises(AnalysisPlanningError, match="date or period"):
        RuleFirstAnalysisPlanner(_catalog()).plan("Forecast revenue for 2027")


@pytest.mark.parametrize("limit", [0, 26, 99, 100])
def test_ranking_limit_outside_governed_range_is_not_silently_clamped(
    limit: int,
) -> None:
    with pytest.raises(AnalysisPlanningError, match="1 through 25"):
        RuleFirstAnalysisPlanner(_catalog()).plan(f"Show the top {limit} regions by revenue")


@pytest.mark.parametrize(
    "question",
    [
        "What is revenue in Boston last quarter?",
        "What is revenue in the Northeast last quarter?",
        "What is revenue in boston last quarter?",
        "What is revenue in northeast last quarter?",
        "Show the sales trend in boston",
        "Show revenue for small businesses",
        "Compare revenue for West and East last quarter",
        "Compare revenue for Software and Services last quarter",
        "What were net sales in the West for customers in Boston last quarter?",
    ],
)
def test_unknown_or_multiple_filter_values_fail_closed(question: str) -> None:
    with pytest.raises(AnalysisPlanningError, match="filter|one .* value"):
        RuleFirstAnalysisPlanner(_catalog()).plan(question)


@pytest.mark.parametrize(
    "question",
    [
        "Show net sales in West and Northeast last quarter.",
        "Show net sales in West and Boston last quarter.",
        "Show net sales in Boston and West last quarter.",
        "Show net sales for Enterprise and Acme customers last quarter.",
    ],
)
def test_mixed_known_and_unknown_filters_are_never_partially_applied(
    question: str,
) -> None:
    with pytest.raises(AnalysisPlanningError, match="mixed known and unknown filters"):
        RuleFirstAnalysisPlanner(_catalog()).plan(question)


@pytest.mark.parametrize(
    "question",
    [
        "Trend net sales by region and city for the last 12 months.",
        "Top 5 regions by net sales and city last quarter.",
        "Show net sales contribution by category and brand last quarter.",
    ],
)
def test_unknown_grouping_terms_are_never_silently_ignored(question: str) -> None:
    with pytest.raises(AnalysisPlanningError, match="not an approved grouping"):
        RuleFirstAnalysisPlanner(_catalog()).plan(question)


@pytest.mark.parametrize(
    "question",
    [
        "Forecast gross margin next quarter",
        "Forecast revenue and units next quarter",
        "Forecast gross profit and margin next quarter",
    ],
)
def test_forecast_never_drops_explicit_unsupported_metrics(question: str) -> None:
    with pytest.raises(AnalysisPlanningError, match="supports only Net Sales and Gross Profit"):
        RuleFirstAnalysisPlanner(_catalog()).plan(question)


def test_metric_free_forecast_defaults_to_both_approved_metrics() -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan("Show the approved forecast")

    assert plan.metric_ids == ["net_sales", "gross_profit"]


@pytest.mark.parametrize(
    ("question", "metric_ids"),
    [
        ("Show the approved forecasts", ["net_sales", "gross_profit"]),
        ("Show revenue forecasts", ["net_sales"]),
        ("Show gross profit projections", ["gross_profit"]),
    ],
)
def test_plural_forecast_language_uses_the_frozen_lookup(
    question: str,
    metric_ids: list[str],
) -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan(question)

    assert plan.operation is AnalysisOperation.FORECAST
    assert plan.metric_ids == metric_ids


def test_driver_bridge_rejects_prior_quarter_comparison() -> None:
    with pytest.raises(AnalysisPlanningError, match="same quarter last year"):
        RuleFirstAnalysisPlanner(_catalog()).plan(
            "Why did margin fall in West last quarter versus prior quarter?"
        )


@pytest.mark.parametrize(
    ("question", "message"),
    [
        ("Show the revenue trend by region and category", "at most one"),
        ("Show the weekly revenue trend", "monthly time grain"),
        ("Show the contribution of revenue", "exactly one"),
        ("Show top revenue by region and category", "exactly one"),
        ("Show top revenue and gross profit by region", "exactly one"),
    ],
)
def test_operation_cardinality_and_grain_fail_closed(
    question: str,
    message: str,
) -> None:
    with pytest.raises(AnalysisPlanningError, match=message):
        RuleFirstAnalysisPlanner(_catalog()).plan(question)


@pytest.mark.parametrize(
    "question",
    [
        "Forecast net sales by region",
        "Forecast region breakdown",
        "Revenue forecast with a regional breakdown",
        "Show a category breakdown of the approved forecast",
        "Under the approved 3% price increase, show results by category",
        "Why did margin fall by category last quarter?",
    ],
)
def test_fixed_analytics_never_discard_explicit_grouping(question: str) -> None:
    with pytest.raises(AnalysisPlanningError, match="does not support grouping"):
        RuleFirstAnalysisPlanner(_catalog()).plan(question)


def test_more_than_three_dimensions_is_rejected_instead_of_truncated() -> None:
    with pytest.raises(AnalysisPlanningError, match="at most three"):
        RuleFirstAnalysisPlanner(_catalog()).plan(
            "Show revenue by region, month, segment, and category"
        )


@pytest.mark.parametrize(
    "question",
    [
        "What were net sales excluding West last quarter?",
        "What were net sales not in West last quarter?",
        "What were net sales in West except Enterprise last quarter?",
    ],
)
def test_negated_or_exclusion_filters_fail_closed(question: str) -> None:
    with pytest.raises(AnalysisPlanningError, match="Exclusion and negated filters"):
        RuleFirstAnalysisPlanner(_catalog()).plan(question)


@pytest.mark.parametrize(
    "question",
    [
        "Run price scenario at 3.5%.",
        "Run price scenario at 2.5%.",
        "Run price scenario at minus two percent.",
        "Run price scenario at plus four percent.",
        "Run price scenario at 10 basis points.",
        "Run a price scenario.",
    ],
)
def test_scenario_never_substitutes_an_unsupported_price_value(question: str) -> None:
    with pytest.raises(AnalysisPlanningError):
        RuleFirstAnalysisPlanner(_catalog()).plan(question)


def test_explicit_default_scenario_uses_the_governed_three_percent_value() -> None:
    plan = RuleFirstAnalysisPlanner(_catalog()).plan("Run the approved default price scenario")

    assert plan.scenario_price_change_pct == 3


@pytest.mark.parametrize(
    "question",
    [
        "Compare the 3% and 5% price scenarios",
        "What happens at 3% versus 4% price increase?",
        "Run a +3% price increase and -2% price decrease scenario",
    ],
)
def test_multi_value_scenarios_require_one_unambiguous_value(question: str) -> None:
    with pytest.raises(AnalysisPlanningError, match="exactly one"):
        RuleFirstAnalysisPlanner(_catalog()).plan(question)


def test_trend_cardinality_never_silently_hits_the_sql_row_limit() -> None:
    planner = RuleFirstAnalysisPlanner(
        _catalog(),
        dimension_values={"category": tuple(f"Category {index}" for index in range(8))},
    )
    with pytest.raises(AnalysisPlanningError, match="200-row"):
        planner.plan("Show the monthly net sales trend by category from 2022-01-01 to 2025-12-31")
