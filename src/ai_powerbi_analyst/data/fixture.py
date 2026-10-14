"""Reproducible client-shaped Sales & Margin data and prepared analytics.

The fixture deliberately tells a useful but truthful demo story: sales continue
to grow while incremental discounting and cost pressure reduce West margin.
Every derived value is calculated from stored inputs; no narrative number is
hand-authored.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

DATA_START = date(2022, 1, 1)
DATA_THROUGH = date(2025, 12, 31)
RANDOM_SEED = 20250830

REGIONS: tuple[tuple[str, float], ...] = (
    ("West", 1.18),
    ("East", 1.10),
    ("Central", 0.92),
    ("South", 1.00),
)
PRODUCTS: tuple[tuple[str, str, float, float, float], ...] = (
    ("Pulse Mini", "Devices", 145.0, 0.54, 1.20),
    ("Pulse Pro", "Devices", 245.0, 0.56, 0.95),
    ("Atlas Core", "Infrastructure", 420.0, 0.62, 0.74),
    ("Atlas Edge", "Infrastructure", 610.0, 0.64, 0.58),
    ("Cloud Start", "Software", 88.0, 0.28, 1.35),
    ("Cloud Scale", "Software", 168.0, 0.30, 1.02),
    ("Care Plus", "Services", 112.0, 0.42, 1.08),
    ("Care Premier", "Services", 198.0, 0.45, 0.82),
)
SEGMENTS: tuple[tuple[str, float, float], ...] = (
    ("Enterprise", 1.16, 0.035),
    ("Mid-Market", 1.00, 0.020),
    ("SMB", 0.78, 0.005),
)
CHANNELS: tuple[tuple[str, float, float], ...] = (
    ("Direct", 1.04, 0.035),
    ("Partner", 0.96, 0.060),
    ("Online", 1.12, 0.018),
)


def generate_sales_fixture(seed: int = RANDOM_SEED) -> pd.DataFrame:
    """Generate 48 complete months of deterministic synthetic transactions."""

    rng = np.random.default_rng(seed)
    months = pd.date_range(DATA_START, periods=48, freq="MS")
    records: list[dict[str, Any]] = []
    transaction_number = 1

    for month_index, month in enumerate(months):
        year_offset = month.year - DATA_START.year
        annual_growth = 1.0 + (0.075 * year_offset)
        seasonal = 1.0 + 0.16 * np.sin(2 * np.pi * (month.month - 1) / 12)
        holiday_lift = 1.18 if month.month in {11, 12} else 1.0

        for region, region_factor in REGIONS:
            for product, category, base_price, cost_ratio, product_factor in PRODUCTS:
                for segment, segment_factor, segment_discount in SEGMENTS:
                    for channel, channel_factor, channel_discount in CHANNELS:
                        noise = float(rng.normal(1.0, 0.045))
                        units = max(
                            1,
                            round(
                                27
                                * annual_growth
                                * seasonal
                                * holiday_lift
                                * region_factor
                                * product_factor
                                * segment_factor
                                * channel_factor
                                * noise
                            ),
                        )

                        # Price grows modestly; West demand is strong enough that sales grow
                        # even while late-2025 discounting and freight costs pressure margin.
                        list_price = base_price * (1 + 0.022 * year_offset)
                        list_price *= 1 + 0.004 * ((month.month - 1) / 11)
                        discount = segment_discount + channel_discount
                        cost_pressure = 0.0
                        if region == "West" and month >= pd.Timestamp("2025-07-01"):
                            discount += 0.036
                            cost_pressure = 0.090
                            units = round(units * 1.16)
                        elif region == "West" and month.year == 2025:
                            discount += 0.012
                            cost_pressure = 0.035

                        # Small governed product promotions add realistic variation.
                        if category == "Devices" and month.month in {3, 9}:
                            discount += 0.012
                        discount = min(discount, 0.22)

                        unit_cost = base_price * cost_ratio * (1 + 0.032 * year_offset)
                        unit_cost *= 1 + cost_pressure
                        day = 2 + ((transaction_number * 7 + month_index * 3) % 25)
                        transaction_date = month.replace(day=int(day))

                        list_price = round(list_price, 2)
                        discount = round(discount, 4)
                        unit_cost = round(unit_cost, 2)
                        net_sales = round(units * list_price * (1 - discount), 2)
                        cogs = round(units * unit_cost, 2)
                        gross_profit = round(net_sales - cogs, 2)

                        records.append(
                            {
                                "transaction_id": f"TX-{transaction_number:07d}",
                                "transaction_date": transaction_date,
                                "month_start": month,
                                "year": month.year,
                                "quarter": f"Q{((month.month - 1) // 3) + 1}",
                                "region": region,
                                "product": product,
                                "category": category,
                                "segment": segment,
                                "channel": channel,
                                "units": int(units),
                                "list_price": list_price,
                                "discount_rate": discount,
                                "net_sales": net_sales,
                                "unit_cost": unit_cost,
                                "cogs": cogs,
                                "gross_profit": gross_profit,
                            }
                        )
                        transaction_number += 1

    frame = pd.DataFrame.from_records(records)
    frame["transaction_date"] = pd.to_datetime(frame["transaction_date"])
    frame["month_start"] = pd.to_datetime(frame["month_start"])
    # Recompute vectorially so persisted values follow one rounding implementation
    # across supported Python versions and platforms.
    frame["net_sales"] = (
        frame["units"] * frame["list_price"] * (1 - frame["discount_rate"])
    ).round(2)
    frame["cogs"] = (frame["units"] * frame["unit_cost"]).round(2)
    frame["gross_profit"] = (frame["net_sales"] - frame["cogs"]).round(2)
    return frame


def _period_aggregate(frame: pd.DataFrame) -> pd.DataFrame:
    keys = ["region", "product", "category", "segment", "channel"]
    prepared = frame.assign(gross_list_value=frame["units"] * frame["list_price"])
    grouped = (
        prepared.groupby(keys, observed=True, as_index=False)
        .agg(
            units=("units", "sum"),
            gross_list_value=("gross_list_value", "sum"),
            net_sales=("net_sales", "sum"),
            cogs=("cogs", "sum"),
            gross_profit=("gross_profit", "sum"),
        )
        .sort_values(keys)
    )
    grouped["price"] = grouped["gross_list_value"] / grouped["units"]
    grouped["discount"] = 1 - grouped["net_sales"] / grouped["gross_list_value"]
    grouped["cost"] = grouped["cogs"] / grouped["units"]
    return grouped


def build_driver_bridge(sales: pd.DataFrame) -> pd.DataFrame:
    """Build an exact Q4-2025 versus Q4-2024 P/V/M/Discount/Cost bridge."""

    current = sales[
        (sales["transaction_date"] >= pd.Timestamp("2025-10-01"))
        & (sales["transaction_date"] <= pd.Timestamp("2025-12-31"))
    ]
    prior = sales[
        (sales["transaction_date"] >= pd.Timestamp("2024-10-01"))
        & (sales["transaction_date"] <= pd.Timestamp("2024-12-31"))
    ]
    rows: list[dict[str, Any]] = []

    for region_filter in ("All", *(region for region, _ in REGIONS)):
        current_slice = (
            current if region_filter == "All" else current[current["region"] == region_filter]
        )
        prior_slice = prior if region_filter == "All" else prior[prior["region"] == region_filter]
        current_agg = _period_aggregate(current_slice)
        prior_agg = _period_aggregate(prior_slice)
        current_net_sales = float(current_slice["net_sales"].sum())
        comparison_net_sales = float(prior_slice["net_sales"].sum())
        current_gross_profit = float(current_slice["gross_profit"].sum())
        comparison_gross_profit = float(prior_slice["gross_profit"].sum())
        current_gross_margin_pct = (
            current_gross_profit / current_net_sales if current_net_sales else 0.0
        )
        comparison_gross_margin_pct = (
            comparison_gross_profit / comparison_net_sales if comparison_net_sales else 0.0
        )
        keys = ["region", "product", "category", "segment", "channel"]
        merged = prior_agg.merge(current_agg, on=keys, suffixes=("_0", "_1"), how="outer").fillna(0)

        unit_0 = merged["units_0"].astype(float)
        unit_1 = merged["units_1"].astype(float)
        price_0 = merged["price_0"].astype(float)
        price_1 = merged["price_1"].astype(float)
        discount_0 = merged["discount_0"].astype(float)
        discount_1 = merged["discount_1"].astype(float)
        cost_0 = merged["cost_0"].astype(float)
        cost_1 = merged["cost_1"].astype(float)

        base_margin_per_unit = float(prior_slice["gross_profit"].sum()) / float(
            prior_slice["units"].sum()
        )
        volume = float((unit_1.sum() - unit_0.sum()) * base_margin_per_unit)
        detailed_volume = float(((unit_1 - unit_0) * (price_0 * (1 - discount_0) - cost_0)).sum())
        price = float((unit_1 * (price_1 - price_0) * (1 - discount_0)).sum())
        discount = float((-unit_1 * price_1 * (discount_1 - discount_0)).sum())
        cost = float((-unit_1 * (cost_1 - cost_0)).sum())
        gross_profit_change = float(
            current_slice["gross_profit"].sum() - prior_slice["gross_profit"].sum()
        )

        # Mix is the product/segment/channel composition residual of detailed volume.
        # Reconcile to stored (cent-rounded) transactions so the bridge is exact.
        mix = detailed_volume - volume
        mix += gross_profit_change - (price + volume + mix + discount + cost)
        values = {
            "Price": price,
            "Volume": volume,
            "Mix": mix,
            "Discount": discount,
            "Cost": cost,
        }
        for order, (driver, contribution) in enumerate(values.items(), start=1):
            rows.append(
                {
                    "comparison_id": "q4_2025_vs_q4_2024",
                    "current_period": "2025 Q4",
                    "comparison_period": "2024 Q4",
                    "region_filter": region_filter,
                    "driver_order": order,
                    "driver": driver,
                    "contribution_value": contribution,
                    "gross_profit_change": gross_profit_change,
                    "current_net_sales": current_net_sales,
                    "comparison_net_sales": comparison_net_sales,
                    "current_gross_profit": current_gross_profit,
                    "comparison_gross_profit": comparison_gross_profit,
                    "current_gross_margin_pct": current_gross_margin_pct,
                    "comparison_gross_margin_pct": comparison_gross_margin_pct,
                }
            )

    return pd.DataFrame.from_records(rows)


def _monthly_metrics(sales: pd.DataFrame) -> pd.DataFrame:
    return (
        sales.groupby("month_start", as_index=False)
        .agg(net_sales=("net_sales", "sum"), gross_profit=("gross_profit", "sum"))
        .sort_values("month_start")
        .reset_index(drop=True)
    )


def _seasonal_drift_forecast(values: np.ndarray, horizon: int) -> tuple[np.ndarray, np.ndarray]:
    if len(values) < 24:
        raise ValueError("forecasting requires at least 24 monthly observations")
    annual_differences = values[12:] - values[:-12]
    annual_drift = float(np.median(annual_differences))
    residuals = annual_differences - annual_drift
    residual_scale = max(float(np.std(residuals, ddof=1)), abs(float(values[-1])) * 0.025)
    points = np.array(
        [max(0.0, float(values[-12 + index]) + annual_drift) for index in range(horizon)]
    )
    scales = np.array([residual_scale * np.sqrt(1 + (index + 1) / 6) for index in range(horizon)])
    return points, scales


def _seasonal_naive_forecast(values: np.ndarray, horizon: int) -> tuple[np.ndarray, np.ndarray]:
    if len(values) < 24:
        raise ValueError("forecasting requires at least 24 monthly observations")
    points = np.array([max(0.0, float(values[-12 + index])) for index in range(horizon)])
    residuals = values[12:] - values[:-12]
    scale = max(float(np.std(residuals, ddof=1)), abs(float(values[-1])) * 0.025)
    return points, np.full(horizon, scale, dtype=float)


def _statsforecast_candidates(values: np.ndarray, horizon: int) -> dict[str, np.ndarray]:
    """Fit the approved AutoETS primary and AutoARIMA challenger together."""

    try:
        from statsforecast import StatsForecast
        from statsforecast.models import AutoARIMA, AutoETS
    except (ImportError, ModuleNotFoundError) as error:  # pragma: no cover - environment dependent
        raise RuntimeError("StatsForecast is unavailable") from error

    frame = pd.DataFrame(
        {
            "unique_id": "metric",
            "ds": pd.date_range("2020-01-01", periods=len(values), freq="MS"),
            "y": values,
        }
    )
    model = StatsForecast(
        models=[AutoETS(season_length=12), AutoARIMA(season_length=12)],
        freq="MS",
        n_jobs=1,
    )
    predicted = model.forecast(df=frame, h=horizon, level=[80, 95])
    return {
        "autoets": predicted["AutoETS"].to_numpy(dtype=float),
        "autoarima": predicted["AutoARIMA"].to_numpy(dtype=float),
    }


def _statsforecast_predictions(
    values: np.ndarray, horizon: int, method: str
) -> tuple[np.ndarray, np.ndarray]:
    """Fit the selected statistical model and return point/interval scale."""

    try:
        from statsforecast import StatsForecast
        from statsforecast.models import AutoARIMA, AutoETS
    except (ImportError, ModuleNotFoundError) as error:  # pragma: no cover - environment dependent
        raise ForecastApprovalError("selected StatsForecast model is unavailable") from error

    models = {
        "autoets": (AutoETS(season_length=12), "AutoETS"),
        "autoarima": (AutoARIMA(season_length=12), "AutoARIMA"),
    }
    if method not in models:
        raise ForecastApprovalError(f"unsupported StatsForecast selection: {method}")
    model, column = models[method]
    frame = pd.DataFrame(
        {
            "unique_id": "metric",
            "ds": pd.date_range("2020-01-01", periods=len(values), freq="MS"),
            "y": values,
        }
    )
    forecast = StatsForecast(models=[model], freq="MS", n_jobs=1).forecast(
        df=frame,
        h=horizon,
        level=[80, 95],
    )
    points = forecast[column].to_numpy(dtype=float)
    scale = (forecast[f"{column}-hi-80"].to_numpy(dtype=float) - points) / 1.281551565545
    return points, np.maximum(scale, 0.0)


class ForecastApprovalError(RuntimeError):
    """No forecast candidate met the explicit release gate."""


def build_forecast(
    sales: pd.DataFrame,
    *,
    horizon: int = 6,
    prefer_statsforecast: bool = True,
    selection: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Precompute six months using only the backtest-approved model per metric."""

    monthly = _monthly_metrics(sales)
    if selection is None:
        backtest = build_forecast_backtest(
            sales,
            include_statsforecast=prefer_statsforecast,
        )
        selection = select_forecast_models(backtest)
    future_periods = pd.date_range(
        monthly["month_start"].max() + pd.offsets.MonthBegin(),
        periods=horizon,
        freq="MS",
    )
    rows: list[dict[str, Any]] = []

    for metric_id in ("net_sales", "gross_profit"):
        values = monthly[metric_id].to_numpy(dtype=float)
        selected = selection.loc[selection["metric_id"] == metric_id]
        if len(selected) != 1:
            raise ForecastApprovalError(f"missing unique approved model for {metric_id}")
        selected_row = selected.iloc[0]
        if not bool(selected_row["quantitative_gate_passed"]):
            raise ForecastApprovalError(f"selected model for {metric_id} did not pass its gate")
        method = str(selected_row["selected_method"])
        if method == "seasonal_drift":
            points, scales = _seasonal_drift_forecast(values, horizon)
        elif method == "seasonal_naive":
            points, scales = _seasonal_naive_forecast(values, horizon)
        elif method in {"autoets", "autoarima"}:
            points, scales = _statsforecast_predictions(values, horizon, method)
        else:
            raise ForecastApprovalError(f"unknown approved forecast method: {method}")

        for period, point, scale in zip(future_periods, points, scales, strict=True):
            rows.append(
                {
                    "period": period,
                    "metric_id": metric_id,
                    "point_forecast": float(point),
                    "lower_80": max(0.0, float(point - 1.281551565545 * scale)),
                    "upper_80": float(point + 1.281551565545 * scale),
                    "lower_95": max(0.0, float(point - 1.95996398454 * scale)),
                    "upper_95": float(point + 1.95996398454 * scale),
                    "method": method,
                    "forecast_version": "1.0.0",
                }
            )
    return pd.DataFrame.from_records(rows)


def build_forecast_backtest(
    sales: pd.DataFrame,
    *,
    include_statsforecast: bool = True,
) -> pd.DataFrame:
    """Evaluate all available candidates on three three-month rolling folds."""

    monthly = _monthly_metrics(sales)
    rows: list[dict[str, Any]] = []
    statsforecast_error = ""
    for metric_id in ("net_sales", "gross_profit"):
        full = monthly[metric_id].to_numpy(dtype=float)
        holdout_starts = (len(full) - 9, len(full) - 6, len(full) - 3)
        for fold, holdout_start in enumerate(holdout_starts, start=1):
            train = full[:holdout_start]
            actual = full[holdout_start : holdout_start + 3]
            naive, _ = _seasonal_naive_forecast(train, 3)
            drift, _ = _seasonal_drift_forecast(train, 3)
            candidates = {"seasonal_naive": naive, "seasonal_drift": drift}
            if include_statsforecast:
                try:
                    candidates.update(_statsforecast_candidates(train, 3))
                except (RuntimeError, ValueError, KeyError) as error:
                    statsforecast_error = f"{type(error).__name__}: {error}"
            for method, predicted in candidates.items():
                absolute_error = float(np.abs(actual - predicted).sum())
                denominator = float(np.abs(actual).sum())
                rows.append(
                    {
                        "metric_id": metric_id,
                        "fold": fold,
                        "method": method,
                        "wape": absolute_error / denominator if denominator else 0.0,
                        "beats_seasonal_naive": False,
                    }
                )
    result = pd.DataFrame.from_records(rows)
    for metric_id in result["metric_id"].unique():
        for fold in result["fold"].unique():
            mask = (result["metric_id"] == metric_id) & (result["fold"] == fold)
            baseline_rows = result.loc[mask & (result["method"] == "seasonal_naive"), "wape"]
            baseline = float(baseline_rows.iloc[0])
            result.loc[mask, "beats_seasonal_naive"] = result.loc[mask, "wape"] < baseline
    expected = {"seasonal_naive", "seasonal_drift", "autoets", "autoarima"}
    candidate_set_complete = all(
        set(group["method"]) == expected and len(group) == 4
        for _, group in result.groupby(["metric_id", "fold"])
    )
    result["candidate_set_complete"] = candidate_set_complete
    result["candidate_error"] = statsforecast_error
    return result


def select_forecast_models(backtest: pd.DataFrame) -> pd.DataFrame:
    """Select the lowest-WAPE model that passes both explicit release gates."""

    required_columns = {
        "metric_id",
        "fold",
        "method",
        "wape",
        "beats_seasonal_naive",
        "candidate_set_complete",
    }
    if not required_columns.issubset(backtest.columns):
        raise ForecastApprovalError("forecast backtest is missing required gate evidence")

    thresholds = {"net_sales": 0.12, "gross_profit": 0.15}
    method_priority = {
        "autoets": 0,
        "autoarima": 1,
        "seasonal_drift": 2,
        "seasonal_naive": 3,
    }
    rows: list[dict[str, Any]] = []
    for metric_id, threshold in thresholds.items():
        metric_rows = backtest[backtest["metric_id"] == metric_id]
        summaries = metric_rows.groupby("method", as_index=False).agg(
            mean_wape=("wape", "mean"),
            wins_vs_seasonal_naive=("beats_seasonal_naive", "sum"),
            folds=("fold", "nunique"),
        )
        summaries["method_priority"] = summaries["method"].map(method_priority).fillna(99)
        summaries = summaries.sort_values(["mean_wape", "method_priority", "method"])
        eligible = summaries[
            (summaries["method"] != "seasonal_naive")
            & (summaries["folds"] == 3)
            & (summaries["wins_vs_seasonal_naive"] >= 2)
            & (summaries["mean_wape"] <= threshold)
        ]
        if eligible.empty:
            raise ForecastApprovalError(
                f"no forecast candidate passed the release gate for {metric_id}"
            )
        best_wape = float(eligible["mean_wape"].min())
        # Treat sub-nanopoint differences as numerical ties and use the declared
        # primary/challenger/fallback priority, making selection reproducible.
        tied = eligible[eligible["mean_wape"] <= best_wape + 1e-9]
        winner = tied.sort_values(["method_priority", "method"]).iloc[0]
        candidate_set_complete = bool(metric_rows["candidate_set_complete"].all())
        status = "approved" if candidate_set_complete else "fallback_approved"
        if candidate_set_complete:
            reason = "Lowest mean WAPE among candidates passing both release gates."
        else:
            reason = (
                "Deterministic fallback passed the quantitative gates; the full "
                "AutoETS/AutoARIMA comparison was unavailable."
            )
        rows.append(
            {
                "metric_id": metric_id,
                "selected_method": str(winner["method"]),
                "mean_wape": float(winner["mean_wape"]),
                "wins_vs_seasonal_naive": int(winner["wins_vs_seasonal_naive"]),
                "wape_threshold": threshold,
                "quantitative_gate_passed": True,
                "candidate_set_complete": candidate_set_complete,
                "status": status,
                "reason": reason,
                "forecast_version": "1.0.0",
            }
        )
    return pd.DataFrame.from_records(rows)


def build_price_scenarios(
    sales: pd.DataFrame,
    forecast: pd.DataFrame,
    *,
    elasticity: float = -0.8,
    allowed_changes: Iterable[int] = range(-3, 6),
) -> pd.DataFrame:
    """Build the governed scenario on the approved next-quarter forecast baseline."""

    latest_quarter = sales[
        (sales["transaction_date"] >= pd.Timestamp("2025-10-01"))
        & (sales["transaction_date"] <= pd.Timestamp("2025-12-31"))
    ]
    required_forecast_columns = {"period", "metric_id", "point_forecast", "method"}
    if not required_forecast_columns.issubset(forecast.columns):
        raise ValueError("scenario preparation requires the approved forecast artifact")
    forecast_periods = sorted(pd.to_datetime(forecast["period"]).unique())[:3]
    if len(forecast_periods) != 3:
        raise ValueError("scenario preparation requires three next-quarter forecast months")
    next_quarter = forecast[pd.to_datetime(forecast["period"]).isin(forecast_periods)]

    def forecast_total(metric_id: str) -> float:
        selected = next_quarter[next_quarter["metric_id"] == metric_id]
        if len(selected) != 3:
            raise ValueError(f"scenario baseline is missing forecast rows for {metric_id}")
        return float(selected["point_forecast"].sum())

    baseline_net_sales = forecast_total("net_sales")
    baseline_gross_profit = forecast_total("gross_profit")
    if not 0 < baseline_gross_profit < baseline_net_sales:
        raise ValueError("scenario forecast baseline has invalid profit economics")

    latest_units = float(latest_quarter["units"].sum())
    latest_net_sales = float(latest_quarter["net_sales"].sum())
    latest_net_price = latest_net_sales / latest_units
    # Volume is derived from the approved revenue forecast at the latest
    # observed net selling price. Cost per unit is then solved so the baseline
    # reconciles exactly to the independently approved Gross Profit forecast.
    baseline_units = baseline_net_sales / latest_net_price
    gross_list_value = float((latest_quarter["units"] * latest_quarter["list_price"]).sum())
    baseline_discount = 1 - latest_net_sales / gross_list_value
    baseline_price = latest_net_price / (1 - baseline_discount)
    baseline_cogs = baseline_net_sales - baseline_gross_profit
    baseline_unit_cost = baseline_cogs / baseline_units
    rows: list[dict[str, Any]] = []

    for price_change_pct in allowed_changes:
        price_fraction = price_change_pct / 100
        scenario_units = baseline_units * (1 + elasticity * price_fraction)
        scenario_price = baseline_price * (1 + price_fraction)
        net_sales = scenario_units * scenario_price * (1 - baseline_discount)
        cogs = scenario_units * baseline_unit_cost
        gross_profit = net_sales - cogs
        rows.append(
            {
                "scenario_id": f"price_{price_change_pct:+d}",
                "price_change_pct": int(price_change_pct),
                "elasticity": elasticity,
                "baseline_period_start": pd.Timestamp(forecast_periods[0]),
                "baseline_period_end": pd.Timestamp(forecast_periods[-1]) + pd.offsets.MonthEnd(),
                "baseline_source": "approved_next_quarter_forecast",
                "baseline_net_sales": baseline_net_sales,
                "baseline_gross_profit": baseline_gross_profit,
                "baseline_units": baseline_units,
                "baseline_price": baseline_price,
                "baseline_discount_rate": baseline_discount,
                "baseline_unit_cost": baseline_unit_cost,
                "scenario_units": scenario_units,
                "scenario_price": scenario_price,
                "net_sales": net_sales,
                "cogs": cogs,
                "gross_profit": gross_profit,
                "gross_margin_pct": gross_profit / net_sales if net_sales else 0.0,
                "scenario_version": "1.0.0",
                "assumption": (
                    "Mechanical price scenario anchored to the approved next-quarter forecast; "
                    "not a prediction."
                ),
            }
        )
    return pd.DataFrame.from_records(rows)
