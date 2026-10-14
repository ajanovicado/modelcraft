from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from ai_powerbi_analyst.contracts import (
    AnalysisOperation,
    AnalysisPlan,
    ChartSpec,
    EvidenceAnswer,
    MetricDefinition,
    ProjectBundle,
    ReportChangePlan,
    ReportPagePlan,
    RouteDecision,
    RouteIntent,
    SemanticCatalog,
    VisualRecipe,
)


def test_route_decision_rejects_unknown_intent() -> None:
    with pytest.raises(ValidationError):
        RouteDecision(intent="DELETE_MODEL", confidence=1.0)  # type: ignore[arg-type]


def test_analysis_plan_rejects_raw_sql_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AnalysisPlan.model_validate(
            {
                "operation": "trend",
                "metric_ids": ["net_sales"],
                "sql": "DROP TABLE fact_sales",
            }
        )


def test_catalog_resolves_aliases_to_stable_metric_ids() -> None:
    catalog = SemanticCatalog(
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
                allowed_dimensions=["region"],
            )
        ],
        dimensions=[],
    )

    assert catalog.resolve_metric("Revenue").id == "net_sales"
    assert catalog.resolve_metric("net_sales").id == "net_sales"


def test_catalog_rejects_duplicate_aliases() -> None:
    metric = MetricDefinition(
        id="net_sales",
        name="Net Sales",
        aliases=["revenue"],
        description="Revenue after discounts.",
        format="$,.0f",
        powerbi_ref="'Measures'[Net Sales]",
        sql_expression="SUM(net_sales)",
        allowed_dimensions=[],
    )
    duplicate = metric.model_copy(
        update={"id": "gross_sales", "name": "Gross Sales", "aliases": ["Revenue"]}
    )

    with pytest.raises(ValidationError, match="duplicate metric alias"):
        SemanticCatalog(version="1.0.0", metrics=[metric, duplicate], dimensions=[])


def test_report_change_plan_has_only_recipe_level_controls() -> None:
    plan = ReportChangePlan(
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
    )

    payload = plan.model_dump()
    assert "sql" not in payload
    assert "cli" not in payload
    assert "coordinates" not in payload["pages"][0]


def test_evidence_answer_requires_provenance() -> None:
    answer = EvidenceAnswer(
        conclusion="Margin fell 1.6 percentage points.",
        facts=["Input costs contributed -2.3 percentage points."],
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

    assert answer.chart.kind == "contribution_bar"
    assert answer.filters == {"region": "West"}


def test_project_bundle_requires_sha256_hashes() -> None:
    with pytest.raises(ValidationError):
        ProjectBundle(
            id="sales-margin-demo",
            name="Sales & Margin",
            pbip_path="fixtures/demo.pbip",
            report_path="fixtures/Demo.Report",
            semantic_model_path="fixtures/Demo.SemanticModel",
            duckdb_path="fixtures/demo.duckdb",
            catalog_path="fixtures/catalog.yaml",
            data_hash="short",
            catalog_version="1.0.0",
            data_through=date(2025, 4, 30),
        )


def test_analysis_plan_defaults_to_safe_row_limit() -> None:
    plan = AnalysisPlan(
        operation=AnalysisOperation.TREND,
        metric_ids=["net_sales"],
        dimension_ids=["month"],
    )

    assert plan.row_limit == 200
    assert plan.row_limit <= 200
    assert RouteIntent.ANALYZE.value == "ANALYZE"
