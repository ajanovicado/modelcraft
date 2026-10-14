"""Deterministic, provenance-rich rendering of governed query results."""

from __future__ import annotations

import calendar
from collections.abc import Sequence
from datetime import date

from ai_powerbi_analyst.analytics.compiler import GovernedQuery
from ai_powerbi_analyst.contracts import (
    AnalysisOperation,
    AnalysisPlan,
    ChartSpec,
    EvidenceAnswer,
    EvidenceVisualPlan,
    MetricDefinition,
    Scalar,
    SemanticCatalog,
)


def _period_label(start: date, end: date) -> str:
    quarter_start = ((start.month - 1) // 3) * 3 + 1
    quarter_end = quarter_start + 2
    if (
        start.day == 1
        and start.month == quarter_start
        and end.year == start.year
        and end.month == quarter_end
        and end.day == calendar.monthrange(end.year, end.month)[1]
    ):
        return f"{start.year} Q{((start.month - 1) // 3) + 1}"
    if start.year == end.year and start.month == end.month:
        return start.strftime("%B %Y")
    if start == date(start.year, 1, 1) and end == date(end.year, 12, 31) and start.year == end.year:
        return str(start.year)
    return f"{start.isoformat()} to {end.isoformat()}"


def _metric(catalog: SemanticCatalog, metric_id: str) -> MetricDefinition:
    return catalog.resolve_metric(metric_id)


def _format_number(value: float | int | None, metric: MetricDefinition) -> str:
    if value is None:
        return "not available"
    numeric = float(value)
    if metric.format.endswith("%"):
        decimals = 1 if ".1" in metric.format else 0
        return f"{numeric * 100:.{decimals}f}%"
    decimals = 2 if ".2" in metric.format else 0
    absolute = abs(numeric)
    body = f"{absolute:,.{decimals}f}"
    sign = "-" if numeric < 0 else ""
    if metric.format.startswith("$"):
        return f"{sign}${body}"
    return f"{sign}{body}"


def _format_pp(value: float | int | None) -> str:
    if value is None:
        return "not available"
    return f"{abs(float(value)) * 100:.1f} percentage points"


def _row_number(row: dict[str, Scalar | None], key: str) -> float | None:
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"expected numeric result column: {key}")
    return float(value)


def _comparison_text(
    plan: AnalysisPlan,
    rows: Sequence[dict[str, Scalar | None]],
    catalog: SemanticCatalog,
    current_label: str,
    comparison_label: str,
) -> tuple[str, list[str]]:
    facts: list[str] = []
    for row in rows:
        metric_id = str(row["metric_id"])
        metric = _metric(catalog, metric_id)
        current = _row_number(row, "current_value")
        prior = _row_number(row, "comparison_value")
        change = _row_number(row, "absolute_change")
        pct_change = _row_number(row, "pct_change")
        if metric_id == "gross_margin_pct":
            direction = "down" if change is not None and change < 0 else "up"
            conclusion = (
                f"{metric.name} was {_format_number(current, metric)} in {current_label}, "
                f"{direction} {_format_pp(change)} versus {comparison_label}."
            )
        elif metric_id == "sales_growth":
            direction = "grew" if pct_change is not None and pct_change >= 0 else "declined"
            net_sales = catalog.resolve_metric("net_sales")
            conclusion = (
                f"Net Sales {direction} {abs(float(pct_change or 0)) * 100:.1f}% in "
                f"{current_label} versus {comparison_label}, reaching "
                f"{_format_number(current, net_sales)}."
            )
        else:
            direction = "increased" if change is not None and change >= 0 else "decreased"
            change_text = _format_number(abs(change) if change is not None else None, metric)
            conclusion = (
                f"{metric.name} was {_format_number(current, metric)} in {current_label}, "
                f"{direction} by {change_text} versus {comparison_label}."
            )
        facts.append(
            f"{metric.name}: {_format_number(current, metric)} vs {_format_number(prior, metric)}."
        )
    return conclusion, facts


def _render_text(
    plan: AnalysisPlan,
    query: GovernedQuery,
    rows: list[dict[str, Scalar | None]],
    catalog: SemanticCatalog,
) -> tuple[str, list[str], ChartSpec, bool, str | None, str]:
    current_label = _period_label(query.current_start, query.current_end)
    comparison_label = (
        _period_label(query.comparison_start, query.comparison_end)
        if query.comparison_start is not None and query.comparison_end is not None
        else "No comparison"
    )
    operation = plan.operation

    if operation == AnalysisOperation.KPI:
        facts = []
        for row in rows:
            metric = _metric(catalog, str(row["metric_id"]))
            facts.append(f"{metric.name}: {_format_number(_row_number(row, 'value'), metric)}.")
        conclusion = f"For {current_label}, {facts[0].rstrip('.').lower()}."
        chart = ChartSpec(kind="metric", title=f"KPI summary — {current_label}")
        return conclusion, facts, chart, False, None, current_label

    if operation == AnalysisOperation.COMPARISON:
        conclusion, facts = _comparison_text(plan, rows, catalog, current_label, comparison_label)
        chart = ChartSpec(
            kind="bar",
            title=f"{current_label} versus {comparison_label}",
            x_field="current_value",
            y_field="metric_id",
        )
        return conclusion, facts, chart, False, None, comparison_label

    if operation == AnalysisOperation.TREND:
        metric = _metric(catalog, plan.metric_ids[0])
        periods = {str(row["period"]) for row in rows}
        conclusion = f"{metric.name} trend covers {len(periods)} month(s) through {current_label}."
        facts = [f"{len(rows)} governed data points are shown."]
        chart = ChartSpec(
            kind="line",
            title=f"{metric.name} trend",
            x_field="period",
            y_field="value",
            color_field="group_value" if plan.dimension_ids else "metric_id",
        )
        return conclusion, facts, chart, False, None, current_label

    if operation == AnalysisOperation.RANKING:
        metric = _metric(catalog, plan.metric_ids[0])
        first = rows[0]
        rank_wording = "ranked lowest" if plan.sort_direction == "asc" else "ranked first"
        conclusion = (
            f"{first['rank_key']} {rank_wording} for {metric.name} at "
            f"{_format_number(_row_number(first, 'value'), metric)} in {current_label}."
        )
        facts = [
            f"{row['rank_key']}: {_format_number(_row_number(row, 'value'), metric)}."
            for row in rows[:8]
        ]
        chart = ChartSpec(
            kind="bar",
            title=f"{metric.name} by {plan.dimension_ids[0]}",
            x_field="value",
            y_field="rank_key",
        )
        return conclusion, facts, chart, False, None, current_label

    if operation == AnalysisOperation.CONTRIBUTION:
        metric = _metric(catalog, plan.metric_ids[0])
        first = max(rows, key=lambda row: abs(_row_number(row, "contribution") or 0))
        contribution = _format_number(_row_number(first, "contribution"), metric)
        conclusion = (
            f"{first['contributor']} contributed {contribution} "
            f"to the {metric.name} change versus {comparison_label}."
        )
        facts = [
            f"{row['contributor']} contributed "
            f"{_format_number(_row_number(row, 'contribution'), metric)}."
            for row in rows[:8]
        ]
        chart = ChartSpec(
            kind="contribution_bar",
            title=f"{metric.name} change by {plan.dimension_ids[0]}",
            x_field="contribution",
            y_field="contributor",
        )
        return conclusion, facts, chart, False, None, comparison_label

    if operation == AnalysisOperation.DRIVER_BRIDGE:
        metric = catalog.resolve_metric("gross_profit")
        first = rows[0]
        current_sales = _row_number(first, "current_net_sales")
        comparison_sales = _row_number(first, "comparison_net_sales")
        current_margin = _row_number(first, "current_gross_margin_pct")
        comparison_margin = _row_number(first, "comparison_gross_margin_pct")
        gross_profit_change = _row_number(first, "gross_profit_change")
        contributions = [_row_number(row, "contribution_value") or 0.0 for row in rows]
        positive_total = sum(value for value in contributions if value > 0)
        negative_total = sum(value for value in contributions if value < 0)
        region = str(plan.filters.get("region", "All regions"))

        has_comparison = (
            current_sales is not None
            and comparison_sales not in {None, 0}
            and current_margin is not None
            and comparison_margin is not None
        )
        if has_comparison:
            assert current_sales is not None
            assert comparison_sales is not None
            assert current_margin is not None
            assert comparison_margin is not None
            sales_change = (float(current_sales) - comparison_sales) / abs(comparison_sales)
            sales_direction = "rose" if sales_change >= 0 else "fell"
            margin_change = current_margin - comparison_margin
            margin_direction = "rose" if margin_change >= 0 else "fell"
            profit_direction = (
                "increased"
                if gross_profit_change is not None and gross_profit_change >= 0
                else "declined"
            )
            profit_delta = abs(gross_profit_change) if gross_profit_change is not None else None
            conclusion = (
                f"{region} Net Sales {sales_direction} {abs(sales_change) * 100:.1f}% in "
                f"{first.get('current_period', current_label)}, but Gross Margin "
                f"{margin_direction} {_format_pp(margin_change)}. Negative governed drivers "
                f"contributed {_format_number(negative_total, metric)}, more than offsetting "
                f"{_format_number(positive_total, metric)} from positive drivers; Gross Profit "
                f"{profit_direction} {_format_number(profit_delta, metric)}."
            )
            facts = [
                f"Net Sales: {_format_number(current_sales, catalog.resolve_metric('net_sales'))} "
                f"vs {_format_number(comparison_sales, catalog.resolve_metric('net_sales'))}.",
                "Gross Margin %: "
                f"{_format_number(current_margin, catalog.resolve_metric('gross_margin_pct'))} "
                "vs "
                f"{_format_number(comparison_margin, catalog.resolve_metric('gross_margin_pct'))}.",
            ]
        else:
            largest = max(
                rows,
                key=lambda row: abs(_row_number(row, "contribution_value") or 0),
            )
            conclusion = (
                f"{largest['driver']} contributed "
                f"{_format_number(_row_number(largest, 'contribution_value'), metric)} to the "
                f"governed Gross Profit bridge for {region}."
            )
            facts = []
        facts.extend(
            f"{row['driver']} contributed "
            f"{_format_number(_row_number(row, 'contribution_value'), metric)}."
            for row in rows[:8]
        )
        chart = ChartSpec(
            kind="contribution_bar",
            title="Gross Profit variance by governed driver",
            x_field="contribution_value",
            y_field="driver",
        )
        can_add = region == "West" and first.get("current_period") == "2025 Q4"
        return (
            conclusion,
            facts,
            chart,
            can_add,
            "margin_drivers" if can_add else None,
            comparison_label,
        )

    if operation == AnalysisOperation.FORECAST:
        first_metric = _metric(catalog, plan.metric_ids[0])
        matching = [row for row in rows if row["metric_id"] == first_metric.id]
        next_quarter = matching[:3]
        total = sum((_row_number(row, "point_forecast") or 0) for row in next_quarter)
        lower_80 = sum((_row_number(row, "lower_80") or 0) for row in next_quarter)
        upper_80 = sum((_row_number(row, "upper_80") or 0) for row in next_quarter)
        method = str(matching[0].get("method", "approved model")) if matching else "approved model"
        method_label = method.replace("autoarima", "AutoARIMA").replace("autoets", "AutoETS")
        conclusion = (
            f"The approved forecast ({method_label}) projects next-quarter {first_metric.name} "
            f"of {_format_number(total, first_metric)}. The sum of the three monthly 80% "
            f"bounds is "
            f"{_format_number(lower_80, first_metric)} to "
            f"{_format_number(upper_80, first_metric)}; it is not a calibrated aggregate-"
            f"quarter interval."
        )
        forecast_facts: list[str] = []
        mean_wape = _row_number(matching[0], "mean_wape") if matching else None
        wins = _row_number(matching[0], "wins_vs_seasonal_naive") if matching else None
        if mean_wape is not None:
            wins_text = (
                f" and beat seasonal naive in {int(wins)}/3 folds" if wins is not None else ""
            )
            forecast_facts.append(
                f"{method_label} backtest mean WAPE was {mean_wape * 100:.1f}%{wins_text}."
            )
        for row in rows[:8]:
            row_metric = _metric(catalog, str(row["metric_id"]))
            value = _format_number(_row_number(row, "point_forecast"), row_metric)
            forecast_facts.append(f"{row['period']}: {value} ({row_metric.name}).")
        chart = ChartSpec(
            kind="forecast",
            title="Approved six-month forecast",
            x_field="period",
            y_field="point_forecast",
            color_field="metric_id",
        )
        return (
            conclusion,
            forecast_facts,
            chart,
            False,
            None,
            "Approved six-month forecast",
        )

    if operation == AnalysisOperation.SCENARIO:
        row = rows[0]
        net_sales = catalog.resolve_metric("net_sales")
        units = catalog.resolve_metric("units")
        gross_profit = catalog.resolve_metric("gross_profit")
        change = int(_row_number(row, "price_change_pct") or 0)
        scenario_units = _row_number(row, "scenario_units")
        baseline_units = _row_number(row, "baseline_units")
        baseline_price = _row_number(row, "baseline_price")
        baseline_discount = _row_number(row, "baseline_discount_rate")
        baseline_cost = _row_number(row, "baseline_unit_cost")
        baseline_sales = _row_number(row, "baseline_net_sales")
        baseline_profit = _row_number(row, "baseline_gross_profit")
        if None not in {baseline_units, baseline_price, baseline_discount, baseline_cost}:
            assert baseline_units is not None
            assert baseline_price is not None
            assert baseline_discount is not None
            assert baseline_cost is not None
            if baseline_sales is None:
                baseline_sales = baseline_units * baseline_price * (1 - baseline_discount)
            if baseline_profit is None:
                baseline_profit = baseline_sales - baseline_units * baseline_cost
        conclusion = (
            f"Under the governed {change:+d}% next-quarter price scenario, Net Sales is "
            f"{_format_number(_row_number(row, 'net_sales'), net_sales)}, volume is "
            f"{_format_number(scenario_units, units)} units, and Gross Profit is "
            f"{_format_number(_row_number(row, 'gross_profit'), gross_profit)}."
        )
        elasticity = _row_number(row, "elasticity")
        elasticity_text = f"{elasticity:.1f}" if elasticity is not None else "not available"
        scenario_facts = [
            "The baseline is the approved next-quarter Net Sales and Gross Profit forecast.",
            f"Volume response uses the approved elasticity of {elasticity_text}.",
            "This is a mechanical scenario, not a prediction.",
        ]
        if (
            baseline_sales is not None
            and baseline_sales != 0
            and baseline_profit is not None
            and baseline_profit != 0
            and baseline_units is not None
            and baseline_units != 0
        ):
            scenario_sales = _row_number(row, "net_sales") or 0
            scenario_profit = _row_number(row, "gross_profit") or 0
            volume_change = (float(scenario_units or 0) / float(baseline_units) - 1) * 100
            scenario_facts.insert(
                0,
                "Versus baseline: Net Sales "
                f"{(scenario_sales / float(baseline_sales) - 1) * 100:+.1f}%, volume "
                f"{volume_change:+.1f}%, and Gross Profit "
                f"{(scenario_profit / float(baseline_profit) - 1) * 100:+.1f}%.",
            )
        chart = ChartSpec(kind="table", title=f"Governed {change:+d}% price scenario")
        return (
            conclusion,
            scenario_facts,
            chart,
            False,
            None,
            "Approved next-quarter price scenario",
        )

    raise ValueError(f"unsupported evidence operation: {operation}")


def render_evidence_answer(
    plan: AnalysisPlan,
    query: GovernedQuery,
    rows: list[dict[str, Scalar | None]],
    catalog: SemanticCatalog,
    data_hash: str,
    catalog_hash: str,
) -> EvidenceAnswer:
    """Render facts without asking a language model to calculate or infer numbers."""

    if not rows:
        raise ValueError("governed query returned no evidence")
    conclusion, facts, chart, can_add, target, comparison_period = _render_text(
        plan, query, rows, catalog
    )
    metric_ids = list(plan.metric_ids)
    if plan.operation is AnalysisOperation.DRIVER_BRIDGE:
        metric_ids = ["net_sales", "gross_profit", "gross_margin_pct"]
    report_visual_plan = (
        EvidenceVisualPlan(
            binding_id="west_driver_bridge",
            metric_object_id="driver_contribution_west",
            dimension_object_id="driver",
        )
        if can_add
        else None
    )
    return EvidenceAnswer(
        conclusion=conclusion,
        facts=facts[:8],
        metric_ids=metric_ids,
        comparison_period=comparison_period,
        filters=dict(plan.filters),
        data_hash=data_hash,
        catalog_hash=catalog_hash,
        catalog_version=catalog.version,
        query_plan_hash=query.plan_hash,
        data_through=query.data_through,
        metric_definition_refs=[
            catalog.resolve_metric(metric_id).powerbi_ref for metric_id in metric_ids
        ],
        chart=chart,
        rows=rows[: plan.row_limit],
        add_to_report_supported=can_add,
        target_page_key=target,
        report_visual_plan=report_visual_plan,
    )
