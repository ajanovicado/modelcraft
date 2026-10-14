from __future__ import annotations

import pandas as pd
import pytest

from ai_powerbi_analyst.data.fixture import (
    DATA_THROUGH,
    ForecastApprovalError,
    build_driver_bridge,
    build_forecast,
    build_forecast_backtest,
    build_price_scenarios,
    generate_sales_fixture,
    select_forecast_models,
)


def test_sales_fixture_is_deterministic_and_spans_exactly_48_months() -> None:
    first = generate_sales_fixture()
    second = generate_sales_fixture()

    pd.testing.assert_frame_equal(first, second)
    assert first["month_start"].nunique() == 48
    assert first["month_start"].min() == pd.Timestamp("2022-01-01")
    assert first["month_start"].max() == pd.Timestamp("2025-12-01")
    assert first["transaction_date"].max().date() <= DATA_THROUGH
    assert first["transaction_id"].is_unique


def test_sales_fixture_financial_formulas_are_exact_after_rounding() -> None:
    sales = generate_sales_fixture()

    expected_net_sales = (
        sales["units"] * sales["list_price"] * (1 - sales["discount_rate"])
    ).round(2)
    expected_cogs = (sales["units"] * sales["unit_cost"]).round(2)

    assert (sales["net_sales"] == expected_net_sales).all()
    assert (sales["cogs"] == expected_cogs).all()
    assert (sales["gross_profit"] == (sales["net_sales"] - sales["cogs"]).round(2)).all()


def test_fixture_contains_truthful_west_margin_pressure_story() -> None:
    sales = generate_sales_fixture()
    latest = sales[sales["transaction_date"].dt.year == 2025]
    prior = sales[sales["transaction_date"].dt.year == 2024]

    west_latest_margin = (
        latest.loc[latest["region"] == "West", "gross_profit"].sum()
        / latest.loc[latest["region"] == "West", "net_sales"].sum()
    )
    west_prior_margin = (
        prior.loc[prior["region"] == "West", "gross_profit"].sum()
        / prior.loc[prior["region"] == "West", "net_sales"].sum()
    )

    assert (
        latest.loc[latest["region"] == "West", "net_sales"].sum()
        > prior.loc[prior["region"] == "West", "net_sales"].sum()
    )
    assert west_latest_margin < west_prior_margin


def test_driver_bridge_reconciles_exactly_to_gross_profit_change() -> None:
    bridge = build_driver_bridge(generate_sales_fixture())

    for _, group in bridge.groupby("region_filter"):
        assert group["contribution_value"].sum() == pytest.approx(
            group["gross_profit_change"].iloc[0], abs=0.02
        )
        assert set(group["driver"]) == {"Price", "Volume", "Mix", "Discount", "Cost"}


def test_precomputed_forecast_has_six_months_and_nested_intervals() -> None:
    forecast = build_forecast(generate_sales_fixture(), prefer_statsforecast=False)

    assert forecast["period"].nunique() == 6
    assert set(forecast["metric_id"]) == {"net_sales", "gross_profit"}
    assert (forecast["lower_95"] <= forecast["lower_80"]).all()
    assert (forecast["lower_80"] <= forecast["point_forecast"]).all()
    assert (forecast["point_forecast"] <= forecast["upper_80"]).all()
    assert (forecast["upper_80"] <= forecast["upper_95"]).all()
    assert forecast["method"].eq("seasonal_drift").all()


def test_forecast_fallback_must_pass_three_fold_quantitative_gate() -> None:
    backtest = build_forecast_backtest(generate_sales_fixture(), include_statsforecast=False)
    selection = select_forecast_models(backtest)

    assert set(selection["selected_method"]) == {"seasonal_drift"}
    assert (selection["wins_vs_seasonal_naive"] >= 2).all()
    assert (selection["mean_wape"] <= selection["wape_threshold"]).all()
    assert selection["candidate_set_complete"].eq(False).all()
    assert selection["status"].eq("fallback_approved").all()


def test_forecast_preparation_fails_when_no_candidate_passes_gate() -> None:
    backtest = build_forecast_backtest(generate_sales_fixture(), include_statsforecast=False)
    backtest.loc[backtest["method"] == "seasonal_drift", "wape"] = 0.5
    backtest.loc[backtest["method"] == "seasonal_drift", "beats_seasonal_naive"] = False

    with pytest.raises(ForecastApprovalError, match="no forecast candidate passed"):
        select_forecast_models(backtest)


def test_price_scenario_grid_is_governed_and_formula_exact() -> None:
    sales = generate_sales_fixture()
    forecast = build_forecast(sales, prefer_statsforecast=False)
    scenarios = build_price_scenarios(sales, forecast)

    assert scenarios["price_change_pct"].tolist() == list(range(-3, 6))
    assert scenarios["elasticity"].eq(-0.8).all()
    plus_three = scenarios.loc[scenarios["price_change_pct"] == 3].iloc[0]
    first_three = forecast[forecast["period"].isin(sorted(forecast["period"].unique())[:3])]
    assert plus_three["baseline_net_sales"] == pytest.approx(
        first_three.loc[first_three["metric_id"] == "net_sales", "point_forecast"].sum()
    )
    assert plus_three["baseline_gross_profit"] == pytest.approx(
        first_three.loc[first_three["metric_id"] == "gross_profit", "point_forecast"].sum()
    )
    expected_units = plus_three["baseline_units"] * (1 + (-0.8 * 0.03))
    assert plus_three["scenario_units"] == pytest.approx(expected_units)
    assert plus_three["gross_profit"] == pytest.approx(
        plus_three["net_sales"] - plus_three["cogs"], abs=0.01
    )
