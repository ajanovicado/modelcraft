from __future__ import annotations

from datetime import date

from ai_powerbi_analyst.contracts import (
    ChartSpec,
    EvidenceAnswer,
    ReportChangePlan,
    ReportPagePlan,
    VisualRecipe,
)
from ai_powerbi_analyst.ui.models import WorkspaceConfig
from ai_powerbi_analyst.ui.view_models import (
    contribution_bar_data,
    evidence_chart_frame,
    evidence_slot_ready,
    idempotency_key_for_plan,
    page_summaries,
    plan_provenance,
    scenario_evidence_summary,
    trust_indicators,
)


def test_workspace_config_defaults_fail_closed() -> None:
    config = WorkspaceConfig(
        project_id="test",
        project_name="Test",
        data_through=date(2025, 12, 31),
        approved_metrics=(),
    )

    assert config.compatibility_label == "Not validated"
    assert config.can_open_power_bi is False


def _plan() -> ReportChangePlan:
    return ReportChangePlan(
        base_report_hash="a" * 64,
        catalog_version="1.0.0",
        pages=[
            ReportPagePlan(
                key="executive_pulse",
                title="Executive Pulse",
                visual_recipes=[VisualRecipe.KPI_ROW, VisualRecipe.MONTHLY_TREND],
                metric_ids=["net_sales", "gross_margin_pct"],
                dimension_ids=["region"],
            )
        ],
        assumptions=["Latest complete quarter"],
    )


def test_plan_summary_uses_human_labels_and_is_stable() -> None:
    summaries = page_summaries(_plan())

    assert summaries[0].title == "Executive Pulse"
    assert summaries[0].metric_labels == ("Net Sales", "Gross Margin %")
    assert summaries[0].description == "High-level sales and margin performance overview"


def test_plan_idempotency_key_changes_only_when_plan_or_project_changes() -> None:
    plan = _plan()

    first = idempotency_key_for_plan("sales-margin-demo", plan)
    same = idempotency_key_for_plan("sales-margin-demo", plan)
    changed = idempotency_key_for_plan("another-project", plan)

    assert first == same
    assert first != changed


def test_evidence_rows_are_converted_to_a_chart_frame() -> None:
    answer = EvidenceAnswer(
        conclusion="Input costs contributed most.",
        facts=["Input costs contributed -2.3 pp."],
        metric_ids=["gross_margin_pct"],
        comparison_period="Q1 2025 vs Q1 2024",
        filters={"region": "West"},
        data_hash="b" * 64,
        catalog_hash="d" * 64,
        catalog_version="1.0.0",
        query_plan_hash="c" * 64,
        chart=ChartSpec(
            kind="contribution_bar",
            title="Margin variance by driver (pp)",
            x_field="contribution_pp",
            y_field="driver",
        ),
        rows=[{"driver": "Input costs", "contribution_pp": -2.3}],
    )

    frame = evidence_chart_frame(answer)

    assert frame.to_dict("records") == [{"driver": "Input costs", "contribution_pp": -2.3}]


def test_trust_indicators_reflect_backend_capabilities() -> None:
    indicators = trust_indicators(
        is_local=True,
        grounded=True,
        validated=False,
        undo_available=True,
    )

    assert [(item.label, item.ok) for item in indicators] == [
        ("Local", True),
        ("Grounded", True),
        ("Validated", False),
        ("Undo", True),
    ]


def test_plan_provenance_compacts_governed_assumptions_without_losing_detail() -> None:
    plan = _plan().model_copy(
        update={
            "assumptions": [
                "Uses the latest complete quarter in the prepared local data.",
                "Forecast values come from the approved frozen six-month model.",
                "The price scenario uses the governed elasticity assumption.",
            ]
        }
    )

    items = plan_provenance(
        plan,
        forecast_version="1.0.0",
        approved_price_change_pct=3,
        price_elasticity=-0.8,
    )

    assert [(item.label, item.value) for item in items] == [
        ("Scope", "Latest complete quarter"),
        ("Forecast", "Frozen v1.0.0 · six-month"),
        ("Scenario", "+3% · −0.8 elasticity · mechanical"),
    ]
    assert items[0].detail == plan.assumptions[0]
    assert items[1].detail == plan.assumptions[1]
    assert items[2].detail.endswith("Mechanical scenario; not a prediction.")


def _scenario_answer() -> EvidenceAnswer:
    return EvidenceAnswer(
        conclusion="Governed scenario result.",
        facts=[
            "Versus baseline: Net Sales +0.5%, volume -2.4%, and Gross Profit +4.9%.",
            "The baseline is the approved next-quarter Net Sales and Gross Profit forecast.",
            "Volume response uses the approved elasticity of -0.8.",
            "This is a mechanical scenario, not a prediction.",
        ],
        metric_ids=["net_sales", "units", "gross_profit"],
        comparison_period="Approved next-quarter price scenario",
        filters={},
        data_hash="d" * 64,
        catalog_hash="f" * 64,
        catalog_version="1.0.0",
        query_plan_hash="e" * 64,
        chart=ChartSpec(kind="table", title="Governed +3% price scenario"),
        rows=[
            {
                "price_change_pct": 3,
                "elasticity": -0.8,
                "baseline_period_start": "2026-01-01",
                "baseline_period_end": "2026-03-31",
                "baseline_source": "approved_next_quarter_forecast",
                "baseline_net_sales": 7_489_000.0,
                "baseline_gross_profit": 3_010_000.0,
                "baseline_units": 35_330.0,
                "scenario_units": 34_482.0,
                "net_sales": 7_526_098.0,
                "gross_profit": 3_157_797.0,
                "gross_margin_pct": 0.4196,
                "assumption": "Mechanical price scenario; not a prediction.",
            }
        ],
    )


def test_scenario_summary_uses_exact_values_and_business_labels() -> None:
    summary = scenario_evidence_summary(_scenario_answer())

    assert summary is not None
    assert [(card.label, card.value, card.delta) for card in summary.cards] == [
        ("Net Sales", "$7,526,098", "+0.5% vs baseline"),
        ("Volume", "34,482 units", "−2.4% vs baseline"),
        ("Gross Profit", "$3,157,797", "+4.9% vs baseline"),
    ]
    assert summary.baseline == "Approved next-quarter forecast · Jan 1–Mar 31, 2026"
    assert summary.assumptions == (
        "+3% price · −0.8 elasticity · Mechanical price scenario; not a prediction."
    )


def test_contribution_bar_data_drops_non_finite_values_and_sets_finite_domain() -> None:
    answer = EvidenceAnswer(
        conclusion="Governed contributions.",
        facts=["Price and cost are governed contributions."],
        metric_ids=["gross_profit"],
        comparison_period="2025 Q4 vs 2024 Q4",
        filters={"region": "West"},
        data_hash="f" * 64,
        catalog_hash="2" * 64,
        catalog_version="1.0.0",
        query_plan_hash="1" * 64,
        chart=ChartSpec(
            kind="contribution_bar",
            title="Gross Profit variance by governed driver",
            x_field="contribution_value",
            y_field="driver",
        ),
        rows=[
            {"driver": "Price", "contribution_value": 124_000.0},
            {"driver": "Missing", "contribution_value": None},
            {"driver": "Cost", "contribution_value": -136_577.56},
        ],
    )

    data = contribution_bar_data(answer)

    assert data.frame["driver"].tolist() == ["Price", "Cost"]
    assert data.domain[0] < -136_577.56
    assert data.domain[1] > 124_000.0
    assert all(value not in {float("inf"), float("-inf")} for value in data.domain)


def test_comparison_bar_keeps_numeric_values_on_the_quantitative_axis() -> None:
    answer = EvidenceAnswer(
        conclusion="Net Sales increased.",
        facts=["Net Sales: $120 vs $100."],
        metric_ids=["net_sales"],
        comparison_period="2024 Q4",
        filters={},
        data_hash="f" * 64,
        catalog_hash="2" * 64,
        catalog_version="1.0.0",
        query_plan_hash="1" * 64,
        chart=ChartSpec(
            kind="bar",
            title="2025 Q4 versus 2024 Q4",
            x_field="current_value",
            y_field="metric_id",
        ),
        rows=[
            {
                "metric_id": "net_sales",
                "current_value": 120.0,
                "comparison_value": 100.0,
            }
        ],
    )

    data = contribution_bar_data(answer)

    assert data.x_field == "current_value"
    assert data.y_field == "metric_id"
    assert data.frame["current_value"].tolist() == [120.0]
    assert all(value not in {float("inf"), float("-inf")} for value in data.domain)


def test_evidence_slot_requires_an_installed_recipe_and_validated_report() -> None:
    plan_without_slot = _plan()
    plan_with_slot = plan_without_slot.model_copy(
        update={
            "pages": [
                plan_without_slot.pages[0].model_copy(
                    update={
                        "key": "margin_drivers",
                        "title": "Margin Drivers",
                        "visual_recipes": [
                            VisualRecipe.KPI_ROW,
                            VisualRecipe.EVIDENCE_SLOT,
                        ],
                    }
                )
            ]
        }
    )

    assert evidence_slot_ready(plan_with_slot, validated=True) is True
    assert evidence_slot_ready(plan_with_slot, validated=False) is False
    assert evidence_slot_ready(plan_without_slot, validated=True) is False
