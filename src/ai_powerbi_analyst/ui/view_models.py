"""Pure presentation helpers shared by the Streamlit UI and its tests."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from ai_powerbi_analyst.contracts import EvidenceAnswer, ReportChangePlan, VisualRecipe


@dataclass(frozen=True, slots=True)
class PageSummary:
    number: int
    key: str
    title: str
    description: str
    metric_labels: tuple[str, ...]
    dimension_labels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TrustIndicator:
    label: str
    ok: bool
    value: str


@dataclass(frozen=True, slots=True)
class PlanProvenanceItem:
    label: str
    value: str
    detail: str


@dataclass(frozen=True, slots=True)
class ContributionBarData:
    frame: pd.DataFrame
    x_field: str
    y_field: str
    domain: tuple[float, float]


@dataclass(frozen=True, slots=True)
class ScenarioMetricCard:
    label: str
    value: str
    delta: str


@dataclass(frozen=True, slots=True)
class ScenarioEvidenceSummary:
    cards: tuple[ScenarioMetricCard, ...]
    baseline: str
    assumptions: str


_PAGE_DESCRIPTIONS = {
    "executive_pulse": "High-level sales and margin performance overview",
    "margin_drivers": "Decomposition of margin variance and key drivers",
    "outlook_decisions": "Forward outlook, risks and recommended actions",
}

_LABELS = {
    "net_sales": "Net Sales",
    "revenue": "Revenue",
    "gross_profit": "Gross Profit",
    "gross_margin_pct": "Gross Margin %",
    "sales_growth": "Sales Growth",
    "units": "Units",
    "orders": "Orders",
    "average_selling_price": "Average Selling Price",
    "discount_rate": "Discount Rate",
    "unit_cost": "Unit Cost",
    "region": "Region",
    "category": "Category",
    "month": "Month",
    "driver": "Driver",
}


def human_label(identifier: str) -> str:
    return _LABELS.get(identifier, identifier.replace("_", " ").title())


def page_summaries(plan: ReportChangePlan) -> tuple[PageSummary, ...]:
    return tuple(
        PageSummary(
            number=index,
            key=page.key,
            title=page.title,
            description=_PAGE_DESCRIPTIONS.get(page.key, "Approved report page"),
            metric_labels=tuple(human_label(value) for value in page.metric_ids),
            dimension_labels=tuple(human_label(value) for value in page.dimension_ids),
        )
        for index, page in enumerate(plan.pages, start=1)
    )


def idempotency_key_for_plan(project_id: str, plan: ReportChangePlan) -> str:
    payload = {
        "project_id": project_id,
        "plan": plan.model_dump(mode="json"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def evidence_chart_frame(answer: EvidenceAnswer) -> pd.DataFrame:
    return pd.DataFrame([dict(row) for row in answer.rows])


def evidence_slot_ready(
    report_plan: ReportChangePlan | None,
    *,
    validated: bool,
) -> bool:
    """Require an installed, validated reserved slot before offering a write."""

    if not validated or report_plan is None:
        return False
    return any(
        page.key == "margin_drivers" and VisualRecipe.EVIDENCE_SLOT in page.visual_recipes
        for page in report_plan.pages
    )


def contribution_bar_data(answer: EvidenceAnswer) -> ContributionBarData:
    """Return finite, explicitly bounded contribution data for Vega-Lite.

    Vega emits an ``[Infinity, -Infinity]`` extent warning when a transient or
    malformed contribution dataset has no finite quantitative values.  The UI
    receives governed rows, but still filters defensively and supplies a finite
    scale domain so browser rendering fails closed instead of producing noise.
    """

    frame = evidence_chart_frame(answer)
    if frame.empty:
        raise ValueError("contribution evidence has no rows")
    x_field = answer.chart.x_field or str(frame.columns[-1])
    y_field = answer.chart.y_field or str(frame.columns[0])
    if x_field not in frame or y_field not in frame:
        raise ValueError("contribution evidence does not match its chart fields")

    numeric = pd.to_numeric(frame[x_field], errors="coerce")
    finite = numeric.map(lambda value: bool(pd.notna(value) and math.isfinite(float(value))))
    clean = frame.loc[finite, [y_field, x_field]].copy()
    if clean.empty:
        raise ValueError("contribution evidence has no finite values")
    clean[x_field] = numeric.loc[finite].astype(float)

    minimum = float(clean[x_field].min())
    maximum = float(clean[x_field].max())
    low = min(0.0, minimum)
    high = max(0.0, maximum)
    span = high - low
    if span == 0:
        domain = (-1.0, 1.0)
    else:
        padding = span * 0.06
        domain = (low - padding, high + padding)
    return ContributionBarData(
        frame=clean,
        x_field=x_field,
        y_field=y_field,
        domain=domain,
    )


def _matching_assumption(plan: ReportChangePlan, *needles: str) -> str:
    for assumption in plan.assumptions:
        normalized = assumption.casefold()
        if any(needle in normalized for needle in needles):
            return assumption
    return ""


def _format_signed(value: float, *, digits: int = 1) -> str:
    rendered = f"{value:+.{digits}f}"
    return rendered.replace("-", "−")


def plan_provenance(
    plan: ReportChangePlan,
    *,
    forecast_version: str,
    approved_price_change_pct: int,
    price_elasticity: float,
) -> tuple[PlanProvenanceItem, ...]:
    """Produce the compact, presenter-safe assumption strip for a plan."""

    scope_detail = _matching_assumption(plan, "quarter") or (
        "Uses the latest complete quarter in the prepared local data."
    )
    forecast_detail = _matching_assumption(plan, "forecast", "six-month") or (
        "Forecast values come from the approved frozen six-month model."
    )
    scenario_detail = _matching_assumption(plan, "scenario", "elasticity") or (
        "The price scenario uses the governed elasticity assumption."
    )
    mechanical = "Mechanical scenario; not a prediction."
    if "mechanical" not in scenario_detail.casefold():
        scenario_detail = f"{scenario_detail.rstrip()} {mechanical}"

    price = f"{approved_price_change_pct:+d}%".replace("-", "−")
    elasticity = _format_signed(price_elasticity)
    return (
        PlanProvenanceItem("Scope", "Latest complete quarter", scope_detail),
        PlanProvenanceItem(
            "Forecast",
            f"Frozen v{forecast_version} · six-month",
            forecast_detail,
        ),
        PlanProvenanceItem(
            "Scenario",
            f"{price} · {elasticity} elasticity · mechanical",
            scenario_detail,
        ),
    )


def _number(row: Mapping[str, Any], key: str) -> float | None:
    value = row.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _delta(current: float, baseline: float) -> str:
    if baseline == 0:
        return "Baseline unavailable"
    change = (current / baseline - 1) * 100
    return f"{_format_signed(change)}% vs baseline"


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _date_range(start_value: Any, end_value: Any) -> str:
    start = _parse_date(start_value)
    end = _parse_date(end_value)
    if start is None or end is None:
        return "Prepared next-quarter period"

    start_text = f"{start.strftime('%b')} {start.day}"
    if start.year != end.year:
        start_text += f", {start.year}"
    end_text = f"{end.strftime('%b')} {end.day}, {end.year}"
    return f"{start_text}–{end_text}"


def scenario_evidence_summary(answer: EvidenceAnswer) -> ScenarioEvidenceSummary | None:
    """Translate the governed scenario row into compact business-facing cards."""

    if answer.chart.kind != "table" or len(answer.rows) != 1:
        return None
    row = answer.rows[0]
    required = {
        "price_change_pct",
        "elasticity",
        "baseline_net_sales",
        "baseline_gross_profit",
        "baseline_units",
        "scenario_units",
        "net_sales",
        "gross_profit",
    }
    if not required.issubset(row):
        return None

    values = {key: _number(row, key) for key in required}
    if any(value is None for value in values.values()):
        return None
    price_change = values["price_change_pct"]
    elasticity = values["elasticity"]
    net_sales = values["net_sales"]
    units = values["scenario_units"]
    gross_profit = values["gross_profit"]
    baseline_sales = values["baseline_net_sales"]
    baseline_units = values["baseline_units"]
    baseline_profit = values["baseline_gross_profit"]
    assert price_change is not None
    assert elasticity is not None
    assert net_sales is not None
    assert units is not None
    assert gross_profit is not None
    assert baseline_sales is not None
    assert baseline_units is not None
    assert baseline_profit is not None

    source = str(row.get("baseline_source", "")).casefold()
    source_label = (
        "Approved next-quarter forecast"
        if source == "approved_next_quarter_forecast"
        else "Approved prepared baseline"
    )
    baseline = (
        f"{source_label} · "
        f"{_date_range(row.get('baseline_period_start'), row.get('baseline_period_end'))}"
    )
    assumption = str(row.get("assumption") or "Mechanical price scenario; not a prediction.")
    price_label = f"{price_change:+.0f}%".replace("-", "−")
    elasticity_label = _format_signed(elasticity)
    return ScenarioEvidenceSummary(
        cards=(
            ScenarioMetricCard(
                "Net Sales",
                f"${net_sales:,.0f}",
                _delta(net_sales, baseline_sales),
            ),
            ScenarioMetricCard(
                "Volume",
                f"{units:,.0f} units",
                _delta(units, baseline_units),
            ),
            ScenarioMetricCard(
                "Gross Profit",
                f"${gross_profit:,.0f}",
                _delta(gross_profit, baseline_profit),
            ),
        ),
        baseline=baseline,
        assumptions=f"{price_label} price · {elasticity_label} elasticity · {assumption}",
    )


def trust_indicators(
    *,
    is_local: bool,
    grounded: bool,
    validated: bool,
    undo_available: bool,
) -> tuple[TrustIndicator, ...]:
    return (
        TrustIndicator("Local", is_local, "Yes" if is_local else "No"),
        TrustIndicator("Grounded", grounded, "Yes" if grounded else "No"),
        TrustIndicator("Validated", validated, "Yes" if validated else "Pending"),
        TrustIndicator("Undo", undo_available, "Available" if undo_available else "Unavailable"),
    )
