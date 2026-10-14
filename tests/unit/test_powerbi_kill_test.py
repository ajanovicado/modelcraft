from __future__ import annotations

import json
from pathlib import Path

from ai_powerbi_analyst.contracts import (
    DimensionDefinition,
    MetricDefinition,
    ReportChangePlan,
    ReportObjectDefinition,
    ReportPagePlan,
    SemanticCatalog,
    VisualRecipe,
)
from ai_powerbi_analyst.powerbi.composer import SafePowerBIComposer
from ai_powerbi_analyst.powerbi.kill_test import (
    DesktopConfirmation,
    Phase0KillTestRunner,
)
from ai_powerbi_analyst.powerbi.mock_backend import MockPowerBIBackend


def make_runner(tmp_path: Path) -> tuple[Phase0KillTestRunner, SemanticCatalog]:
    project = tmp_path / "Demo"
    report = project / "Sales.Report"
    pages = report / "definition/pages"
    pages.mkdir(parents=True)
    (pages / "pages.json").write_text(
        json.dumps({"pageOrder": [], "activePageName": None}) + "\n", encoding="utf-8"
    )
    (project / "Sales.pbip").write_text("{}\n", encoding="utf-8")
    catalog = SemanticCatalog(
        version="1.0.0",
        metrics=[
            MetricDefinition(
                id="net_sales",
                name="Net Sales",
                description="Net sales",
                format="$#,0",
                powerbi_ref="Sales[Net Sales]",
                sql_expression="sum(net_sales)",
                allowed_dimensions=["month"],
            )
        ],
        dimensions=[
            DimensionDefinition(
                id="month",
                name="Month",
                column="month",
                powerbi_ref="Date[Month]",
                data_type="date",
            )
        ],
        report_objects=[
            ReportObjectDefinition(
                id="overview_latest_quarter_net_sales",
                name="Latest Quarter Net Sales",
                powerbi_ref="'Measures'[Latest Quarter Net Sales]",
                kind="measure",
                source="overview",
            ),
            ReportObjectDefinition(
                id="overview_latest_quarter_gross_profit",
                name="Latest Quarter Gross Profit",
                powerbi_ref="'Measures'[Latest Quarter Gross Profit]",
                kind="measure",
                source="overview",
            ),
            ReportObjectDefinition(
                id="overview_latest_quarter_gross_margin_pct",
                name="Latest Quarter Gross Margin %",
                powerbi_ref="'Measures'[Latest Quarter Gross Margin %]",
                kind="measure",
                source="overview",
            ),
            ReportObjectDefinition(
                id="overview_latest_quarter_sales_growth",
                name="Latest Quarter Sales Growth",
                powerbi_ref="'Measures'[Sales Growth]",
                kind="measure",
                source="overview",
            ),
            ReportObjectDefinition(
                id="overview_executive_finding",
                name="Executive Finding",
                powerbi_ref="'Measures'[Executive Finding]",
                kind="measure",
                source="overview",
            ),
        ],
    )
    composer = SafePowerBIComposer(
        project,
        Path("Sales.Report"),
        tmp_path / "state",
        MockPowerBIBackend(),
    )
    return Phase0KillTestRunner(composer, report), catalog


def plan_factory(base_hash: str, _: int) -> ReportChangePlan:
    return ReportChangePlan(
        base_report_hash=base_hash,
        catalog_version="1.0.0",
        pages=[
            ReportPagePlan(
                key="executive_pulse",
                title="Executive Pulse",
                visual_recipes=[VisualRecipe.KPI_ROW, VisualRecipe.MONTHLY_TREND],
                metric_ids=["net_sales"],
                dimension_ids=["month"],
            )
        ],
    )


def test_kill_test_records_repeatable_mutation_validation_and_exact_rollback(
    tmp_path: Path,
) -> None:
    runner, catalog = make_runner(tmp_path)

    report = runner.run(iterations=3, plan_factory=plan_factory, catalog=catalog)

    assert report.structurally_passed
    assert len(report.iterations) == 3
    assert all(item.before_hash == item.rollback_hash for item in report.iterations)
    assert all(item.validation_passed for item in report.iterations)
    assert all(item.mutation_recipe_hash for item in report.iterations)
    assert all(item.mutation_output_hash == item.promoted_hash for item in report.iterations)
    assert not report.desktop_reopen_verified
    assert not report.phase_gate_passed
    assert "explicit Desktop confirmation hook was not supplied" in report.blockers


def test_kill_test_only_claims_desktop_reopen_with_explicit_confirmation_hook(
    tmp_path: Path,
) -> None:
    runner, catalog = make_runner(tmp_path)
    calls: list[int] = []

    def confirm(_: Path, iteration: int) -> DesktopConfirmation:
        calls.append(iteration)
        return DesktopConfirmation(
            reopened=True,
            evidence="Operator confirmed Desktop reopened the staged recipe on Windows",
        )

    report = runner.run(
        iterations=2,
        plan_factory=plan_factory,
        catalog=catalog,
        desktop_confirmation=confirm,
    )

    assert calls == [1, 2]
    assert report.desktop_reopen_verified
    assert report.phase_gate_passed


def test_kill_test_rejects_nonpositive_iteration_count(tmp_path: Path) -> None:
    runner, catalog = make_runner(tmp_path)

    try:
        runner.run(iterations=0, plan_factory=plan_factory, catalog=catalog)
    except ValueError as exc:
        assert "positive" in str(exc)
    else:
        raise AssertionError("expected ValueError")
