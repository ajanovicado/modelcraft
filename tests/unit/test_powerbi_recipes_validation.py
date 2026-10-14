from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_powerbi_analyst.contracts import (
    DimensionDefinition,
    MetricDefinition,
    ReportChangePlan,
    ReportObjectDefinition,
    ReportPagePlan,
    SemanticCatalog,
    VisualRecipe,
)
from ai_powerbi_analyst.data.catalog import load_semantic_catalog
from ai_powerbi_analyst.powerbi.cli import CommandReceipt
from ai_powerbi_analyst.powerbi.mock_backend import MockPowerBIBackend
from ai_powerbi_analyst.powerbi.recipes import RecipeCompiler, RecipeError
from ai_powerbi_analyst.powerbi.validation import (
    ValidationReport,
    ValidationSeverity,
    aggregate_validation,
    validate_blueprint_postconditions,
    validate_report,
    validate_semantic_catalog_tmdl,
    validation_from_receipt,
)

HASH = "a" * 64


@pytest.fixture
def catalog() -> SemanticCatalog:
    return SemanticCatalog(
        version="1.0.0",
        metrics=[
            MetricDefinition(
                id="net_sales",
                name="Net Sales",
                aliases=["revenue"],
                description="Sales after discount",
                format="$#,0",
                powerbi_ref="Sales[Net Sales]",
                sql_expression="sum(net_sales)",
                allowed_dimensions=["month", "region", "category"],
            ),
            MetricDefinition(
                id="gross_profit",
                name="Gross Profit",
                description="Net sales less COGS",
                format="$#,0",
                powerbi_ref="Sales[Gross Profit]",
                sql_expression="sum(gross_profit)",
                allowed_dimensions=["month", "region", "category"],
            ),
            MetricDefinition(
                id="gross_margin_pct",
                name="Gross Margin %",
                description="Gross profit divided by net sales",
                format="0.0%",
                powerbi_ref="Sales[Gross Margin %]",
                sql_expression="sum(gross_profit) / sum(net_sales)",
                allowed_dimensions=["month", "region", "category"],
            ),
        ],
        dimensions=[
            DimensionDefinition(
                id="month",
                name="Month",
                column="month",
                powerbi_ref="Date[Month]",
                data_type="date",
            ),
            DimensionDefinition(
                id="region",
                name="Region",
                column="region",
                powerbi_ref="Geography[Region]",
                data_type="string",
            ),
            DimensionDefinition(
                id="category",
                name="Category",
                column="category",
                powerbi_ref="Product[Category]",
                data_type="string",
            ),
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
            ReportObjectDefinition(
                id="driver",
                name="Driver",
                powerbi_ref="'Driver Contributions'[Driver]",
                kind="column",
                source="driver",
            ),
            ReportObjectDefinition(
                id="driver_contribution_all",
                name="Driver Contribution — All",
                powerbi_ref="'Driver Contributions'[Driver Contribution — All]",
                kind="measure",
                source="driver",
            ),
            ReportObjectDefinition(
                id="driver_contribution_west",
                name="Driver Contribution — West",
                powerbi_ref="'Driver Contributions'[Driver Contribution — West]",
                kind="measure",
                source="driver",
            ),
            ReportObjectDefinition(
                id="driver_net_sales_change_pct",
                name="Latest Quarter Net Sales Change %",
                powerbi_ref="'Driver Contributions'[Latest Quarter Net Sales Change %]",
                kind="measure",
                source="driver",
            ),
            ReportObjectDefinition(
                id="driver_gross_profit_change",
                name="Latest Quarter Gross Profit Change",
                powerbi_ref="'Driver Contributions'[Latest Quarter Gross Profit Change]",
                kind="measure",
                source="driver",
            ),
            ReportObjectDefinition(
                id="driver_gross_margin_change_pp",
                name="Latest Quarter Gross Margin Change pp",
                powerbi_ref="'Driver Contributions'[Latest Quarter Gross Margin Change pp]",
                kind="measure",
                source="driver",
            ),
            ReportObjectDefinition(
                id="driver_discount_contribution",
                name="Discount Contribution",
                powerbi_ref="'Driver Contributions'[Discount Contribution]",
                kind="measure",
                source="driver",
            ),
            ReportObjectDefinition(
                id="forecast_period",
                name="Forecast Period",
                powerbi_ref="'Forecast'[Period]",
                kind="column",
                source="forecast",
            ),
            ReportObjectDefinition(
                id="forecast_net_sales",
                name="Forecast Net Sales",
                powerbi_ref="'Forecast'[Forecast Net Sales]",
                kind="measure",
                source="forecast",
            ),
            ReportObjectDefinition(
                id="forecast_net_sales_lower_80",
                name="Forecast Net Sales Lower 80",
                powerbi_ref="'Forecast'[Forecast Net Sales Lower 80]",
                kind="measure",
                source="forecast",
            ),
            ReportObjectDefinition(
                id="forecast_net_sales_upper_80",
                name="Forecast Net Sales Upper 80",
                powerbi_ref="'Forecast'[Forecast Net Sales Upper 80]",
                kind="measure",
                source="forecast",
            ),
            ReportObjectDefinition(
                id="forecast_net_sales_lower_95",
                name="Forecast Net Sales Lower 95",
                powerbi_ref="'Forecast'[Forecast Net Sales Lower 95]",
                kind="measure",
                source="forecast",
            ),
            ReportObjectDefinition(
                id="forecast_net_sales_upper_95",
                name="Forecast Net Sales Upper 95",
                powerbi_ref="'Forecast'[Forecast Net Sales Upper 95]",
                kind="measure",
                source="forecast",
            ),
            ReportObjectDefinition(
                id="forecast_gross_profit",
                name="Forecast Gross Profit",
                powerbi_ref="'Forecast'[Forecast Gross Profit]",
                kind="measure",
                source="forecast",
            ),
            ReportObjectDefinition(
                id="next_quarter_forecast_net_sales",
                name="Next Quarter Forecast Net Sales",
                powerbi_ref="'Forecast'[Next Quarter Forecast Net Sales]",
                kind="measure",
                source="forecast",
            ),
            ReportObjectDefinition(
                id="next_quarter_forecast_gross_profit",
                name="Next Quarter Forecast Gross Profit",
                powerbi_ref="'Forecast'[Next Quarter Forecast Gross Profit]",
                kind="measure",
                source="forecast",
            ),
            ReportObjectDefinition(
                id="forecast_provenance",
                name="Forecast Provenance",
                powerbi_ref="'Forecast'[Forecast Provenance]",
                kind="measure",
                source="forecast",
            ),
            ReportObjectDefinition(
                id="scenario_net_sales",
                name="Approved Scenario Net Sales",
                powerbi_ref="'Scenario Grid'[Approved Scenario Net Sales]",
                kind="measure",
                source="scenario",
            ),
            ReportObjectDefinition(
                id="scenario_units",
                name="Approved Scenario Units",
                powerbi_ref="'Scenario Grid'[Approved Scenario Units]",
                kind="measure",
                source="scenario",
            ),
            ReportObjectDefinition(
                id="scenario_gross_profit",
                name="Approved Scenario Gross Profit",
                powerbi_ref="'Scenario Grid'[Approved Scenario Gross Profit]",
                kind="measure",
                source="scenario",
            ),
            ReportObjectDefinition(
                id="scenario_gross_margin_pct",
                name="Approved Scenario Gross Margin %",
                powerbi_ref="'Scenario Grid'[Approved Scenario Gross Margin %]",
                kind="measure",
                source="scenario",
            ),
            ReportObjectDefinition(
                id="scenario_assumption",
                name="Approved Scenario Assumption",
                powerbi_ref="'Scenario Grid'[Approved Scenario Assumption]",
                kind="measure",
                source="scenario",
            ),
        ],
    )


@pytest.fixture
def dashboard_plan() -> ReportChangePlan:
    return ReportChangePlan(
        base_report_hash=HASH,
        catalog_version="1.0.0",
        pages=[
            ReportPagePlan(
                key="executive_pulse",
                title="Executive Pulse",
                visual_recipes=[
                    VisualRecipe.KPI_ROW,
                    VisualRecipe.MONTHLY_TREND,
                    VisualRecipe.CATEGORY_BAR,
                    VisualRecipe.SLICER_ROW,
                ],
                metric_ids=["net_sales", "gross_profit", "gross_margin_pct"],
                dimension_ids=["month", "region", "category"],
            ),
            ReportPagePlan(
                key="margin_drivers",
                title="Margin Drivers",
                visual_recipes=[
                    VisualRecipe.KPI_ROW,
                    VisualRecipe.CONTRIBUTION_BRIDGE,
                    VisualRecipe.DETAIL_MATRIX,
                    VisualRecipe.EVIDENCE_SLOT,
                ],
                metric_ids=["gross_profit", "gross_margin_pct"],
                dimension_ids=["region", "category"],
            ),
            ReportPagePlan(
                key="outlook_decisions",
                title="Outlook & Decisions",
                visual_recipes=[
                    VisualRecipe.FORECAST_LINE,
                    VisualRecipe.SCENARIO_SUMMARY,
                    VisualRecipe.KPI_ROW,
                ],
                metric_ids=["net_sales", "gross_profit"],
                dimension_ids=["month"],
            ),
        ],
    )


def test_recipe_compiler_builds_stable_three_page_blueprint(
    catalog: SemanticCatalog, dashboard_plan: ReportChangePlan
) -> None:
    compiler = RecipeCompiler()

    first = compiler.compile(dashboard_plan, catalog)
    second = compiler.compile(dashboard_plan, catalog)

    assert first == second
    assert [page.key for page in first.pages] == [
        "executive_pulse",
        "margin_drivers",
        "outlook_decisions",
    ]
    assert all(len(page.page_id) == 20 for page in first.pages)
    all_keys = [visual.logical_key for page in first.pages for visual in page.visuals]
    assert len(all_keys) == len(set(all_keys))
    evidence = next(
        visual for page in first.pages for visual in page.visuals if visual.is_evidence_slot
    )
    assert evidence.logical_key == "margin_drivers.evidence_slot"
    assert evidence.visual_type == "textbox"


def test_governed_report_recipes_bind_only_prepared_report_objects(
    catalog: SemanticCatalog, dashboard_plan: ReportChangePlan
) -> None:
    blueprint = RecipeCompiler().compile(dashboard_plan, catalog)
    visuals = {visual.logical_key: visual for page in blueprint.pages for visual in page.visuals}

    def binding_signature(logical_key: str) -> list[tuple[str, str, str]]:
        return [
            (binding.role, binding.reference, binding.kind)
            for binding in visuals[logical_key].bindings
        ]

    assert binding_signature("margin_drivers.contribution_bridge") == [
        ("category", "'Driver Contributions'[Driver]", "column"),
        ("value", "'Driver Contributions'[Driver Contribution — All]", "measure"),
    ]
    assert binding_signature("executive_pulse.kpi.overview_latest_quarter_net_sales") == [
        ("field", "'Measures'[Latest Quarter Net Sales]", "measure")
    ]
    assert binding_signature("executive_pulse.kpi.overview_latest_quarter_gross_profit") == [
        ("field", "'Measures'[Latest Quarter Gross Profit]", "measure")
    ]
    assert binding_signature("executive_pulse.kpi.overview_latest_quarter_gross_margin_pct") == [
        ("field", "'Measures'[Latest Quarter Gross Margin %]", "measure")
    ]
    assert binding_signature("executive_pulse.kpi.overview_latest_quarter_sales_growth") == [
        ("field", "'Measures'[Sales Growth]", "measure")
    ]
    assert binding_signature("executive_pulse.computed_finding") == [
        ("field", "'Measures'[Executive Finding]", "measure")
    ]
    assert binding_signature("executive_pulse.category_bar") == [
        ("category", "Geography[Region]", "column"),
        ("value", "'Measures'[Latest Quarter Net Sales]", "measure"),
    ]
    assert binding_signature("margin_drivers.kpi.driver_net_sales_change_pct") == [
        ("field", "'Driver Contributions'[Latest Quarter Net Sales Change %]", "measure")
    ]
    assert binding_signature("margin_drivers.kpi.driver_gross_profit_change") == [
        ("field", "'Driver Contributions'[Latest Quarter Gross Profit Change]", "measure")
    ]
    assert binding_signature("margin_drivers.kpi.driver_gross_margin_change_pp") == [
        ("field", "'Driver Contributions'[Latest Quarter Gross Margin Change pp]", "measure")
    ]
    assert binding_signature("margin_drivers.kpi.driver_discount_contribution") == [
        ("field", "'Driver Contributions'[Discount Contribution]", "measure")
    ]
    assert binding_signature("margin_drivers.detail_matrix") == [
        ("row", "Geography[Region]", "column"),
        ("row", "Product[Category]", "column"),
        ("value", "'Measures'[Latest Quarter Net Sales]", "measure"),
        ("value", "'Measures'[Latest Quarter Gross Profit]", "measure"),
        ("value", "'Measures'[Latest Quarter Gross Margin %]", "measure"),
    ]
    assert binding_signature("outlook_decisions.forecast") == [
        ("category", "'Forecast'[Period]", "column"),
        ("value", "'Forecast'[Forecast Net Sales Lower 95]", "measure"),
        ("value", "'Forecast'[Forecast Net Sales Upper 95]", "measure"),
        ("value", "'Forecast'[Forecast Net Sales]", "measure"),
        ("value", "'Forecast'[Forecast Net Sales Lower 80]", "measure"),
        ("value", "'Forecast'[Forecast Net Sales Upper 80]", "measure"),
        ("value", "'Forecast'[Forecast Gross Profit]", "measure"),
    ]
    assert binding_signature("outlook_decisions.kpi.next_quarter_forecast_net_sales") == [
        ("field", "'Forecast'[Next Quarter Forecast Net Sales]", "measure")
    ]
    assert binding_signature("outlook_decisions.kpi.next_quarter_forecast_gross_profit") == [
        ("field", "'Forecast'[Next Quarter Forecast Gross Profit]", "measure")
    ]
    assert binding_signature("outlook_decisions.kpi.forecast_provenance") == [
        ("field", "'Forecast'[Forecast Provenance]", "measure")
    ]
    assert binding_signature("outlook_decisions.scenario_summary") == [
        ("field", "'Scenario Grid'[Approved Scenario Net Sales]", "measure"),
        ("field", "'Scenario Grid'[Approved Scenario Units]", "measure"),
        ("field", "'Scenario Grid'[Approved Scenario Gross Profit]", "measure"),
        ("field", "'Scenario Grid'[Approved Scenario Gross Margin %]", "measure"),
        ("field", "'Scenario Grid'[Approved Scenario Assumption]", "measure"),
    ]


def test_executive_recipe_visual_titles_are_exact(
    catalog: SemanticCatalog, dashboard_plan: ReportChangePlan
) -> None:
    blueprint = RecipeCompiler().compile(dashboard_plan, catalog)
    titles = {
        visual.logical_key: visual.title for page in blueprint.pages for visual in page.visuals
    }

    assert titles == {
        "executive_pulse.kpi.overview_latest_quarter_net_sales": "2025 Q4 Net Sales",
        "executive_pulse.kpi.overview_latest_quarter_gross_profit": ("2025 Q4 Gross Profit"),
        "executive_pulse.kpi.overview_latest_quarter_gross_margin_pct": ("2025 Q4 Gross Margin"),
        "executive_pulse.kpi.overview_latest_quarter_sales_growth": ("Net Sales growth vs 2024 Q4"),
        "executive_pulse.monthly_trend": "Monthly Net Sales trend",
        "executive_pulse.computed_finding": "Computed finding · latest complete quarter",
        "executive_pulse.category_bar": "2025 Q4 regional performance",
        "executive_pulse.slicer.month": "Filter by Month",
        "executive_pulse.slicer.region": "Filter by Region",
        "executive_pulse.slicer.category": "Filter by Category",
        "margin_drivers.kpi.driver_net_sales_change_pct": ("Latest quarter Net Sales change"),
        "margin_drivers.kpi.driver_gross_profit_change": ("Latest quarter Gross Profit change"),
        "margin_drivers.kpi.driver_gross_margin_change_pp": "Gross Margin change",
        "margin_drivers.kpi.driver_discount_contribution": "Discount contribution",
        "margin_drivers.contribution_bridge": "Gross Profit change by governed driver",
        "margin_drivers.detail_matrix": "2025 Q4 category and region detail",
        "margin_drivers.evidence_slot": "Conversational evidence",
        "outlook_decisions.forecast": ("Approved six-month outlook · 80% and 95% uncertainty"),
        "outlook_decisions.scenario_summary": (
            "Approved +3% price scenario · mechanical, not a prediction"
        ),
        "outlook_decisions.kpi.next_quarter_forecast_net_sales": ("Next-quarter Net Sales"),
        "outlook_decisions.kpi.next_quarter_forecast_gross_profit": ("Next-quarter Gross Profit"),
        "outlook_decisions.kpi.forecast_provenance": "Forecast provenance",
    }


def test_fixture_catalog_and_prepared_tmdl_cover_exact_governed_report_objects() -> None:
    repository_root = Path(__file__).parents[2]
    prepared_catalog = load_semantic_catalog(
        repository_root / "fixtures/data/semantic_catalog.yaml"
    )
    expected_objects = {
        "overview_latest_quarter_net_sales": (
            "'Measures'[Latest Quarter Net Sales]",
            "measure",
            "overview",
        ),
        "overview_latest_quarter_gross_profit": (
            "'Measures'[Latest Quarter Gross Profit]",
            "measure",
            "overview",
        ),
        "overview_latest_quarter_gross_margin_pct": (
            "'Measures'[Latest Quarter Gross Margin %]",
            "measure",
            "overview",
        ),
        "overview_latest_quarter_sales_growth": (
            "'Measures'[Sales Growth]",
            "measure",
            "overview",
        ),
        "overview_executive_finding": (
            "'Measures'[Executive Finding]",
            "measure",
            "overview",
        ),
        "driver": ("'Driver Contributions'[Driver]", "column", "driver"),
        "driver_contribution_all": (
            "'Driver Contributions'[Driver Contribution — All]",
            "measure",
            "driver",
        ),
        "driver_contribution_west": (
            "'Driver Contributions'[Driver Contribution — West]",
            "measure",
            "driver",
        ),
        "driver_net_sales_change_pct": (
            "'Driver Contributions'[Latest Quarter Net Sales Change %]",
            "measure",
            "driver",
        ),
        "driver_gross_profit_change": (
            "'Driver Contributions'[Latest Quarter Gross Profit Change]",
            "measure",
            "driver",
        ),
        "driver_gross_margin_change_pp": (
            "'Driver Contributions'[Latest Quarter Gross Margin Change pp]",
            "measure",
            "driver",
        ),
        "driver_discount_contribution": (
            "'Driver Contributions'[Discount Contribution]",
            "measure",
            "driver",
        ),
        "forecast_period": ("'Forecast'[Period]", "column", "forecast"),
        "forecast_net_sales": (
            "'Forecast'[Forecast Net Sales]",
            "measure",
            "forecast",
        ),
        "forecast_net_sales_lower_80": (
            "'Forecast'[Forecast Net Sales Lower 80]",
            "measure",
            "forecast",
        ),
        "forecast_net_sales_upper_80": (
            "'Forecast'[Forecast Net Sales Upper 80]",
            "measure",
            "forecast",
        ),
        "forecast_net_sales_lower_95": (
            "'Forecast'[Forecast Net Sales Lower 95]",
            "measure",
            "forecast",
        ),
        "forecast_net_sales_upper_95": (
            "'Forecast'[Forecast Net Sales Upper 95]",
            "measure",
            "forecast",
        ),
        "forecast_gross_profit": (
            "'Forecast'[Forecast Gross Profit]",
            "measure",
            "forecast",
        ),
        "next_quarter_forecast_net_sales": (
            "'Forecast'[Next Quarter Forecast Net Sales]",
            "measure",
            "forecast",
        ),
        "next_quarter_forecast_gross_profit": (
            "'Forecast'[Next Quarter Forecast Gross Profit]",
            "measure",
            "forecast",
        ),
        "forecast_provenance": (
            "'Forecast'[Forecast Provenance]",
            "measure",
            "forecast",
        ),
        "scenario_net_sales": (
            "'Scenario Grid'[Approved Scenario Net Sales]",
            "measure",
            "scenario",
        ),
        "scenario_units": (
            "'Scenario Grid'[Approved Scenario Units]",
            "measure",
            "scenario",
        ),
        "scenario_gross_profit": (
            "'Scenario Grid'[Approved Scenario Gross Profit]",
            "measure",
            "scenario",
        ),
        "scenario_gross_margin_pct": (
            "'Scenario Grid'[Approved Scenario Gross Margin %]",
            "measure",
            "scenario",
        ),
        "scenario_assumption": (
            "'Scenario Grid'[Approved Scenario Assumption]",
            "measure",
            "scenario",
        ),
    }
    actual_objects = {
        item.id: (item.powerbi_ref, item.kind, item.source)
        for item in prepared_catalog.report_objects
    }

    assert actual_objects == expected_objects
    model = repository_root / "fixtures/powerbi/client-wow-template/ClientWow.SemanticModel"
    assert validate_semantic_catalog_tmdl(model, prepared_catalog).passed


def test_recipe_compiler_rejects_unknown_objects(
    catalog: SemanticCatalog, dashboard_plan: ReportChangePlan
) -> None:
    bad_page = dashboard_plan.pages[0].model_copy(update={"metric_ids": ["invented_metric"]})
    bad_plan = dashboard_plan.model_copy(update={"pages": [bad_page]})

    with pytest.raises(RecipeError, match="unknown metric"):
        RecipeCompiler().compile(bad_plan, catalog)


def test_recipe_compiler_rejects_duplicate_or_unsupported_page_keys(
    catalog: SemanticCatalog, dashboard_plan: ReportChangePlan
) -> None:
    duplicate = dashboard_plan.model_copy(
        update={"pages": [dashboard_plan.pages[0], dashboard_plan.pages[0]]}
    )
    with pytest.raises(RecipeError, match="duplicate page key"):
        RecipeCompiler().compile(duplicate, catalog)

    unsupported_page = dashboard_plan.pages[0].model_copy(update={"key": "freeform_page"})
    unsupported = dashboard_plan.model_copy(update={"pages": [unsupported_page]})
    with pytest.raises(RecipeError, match="unsupported page key"):
        RecipeCompiler().compile(unsupported, catalog)


def test_recipe_compiler_rejects_recipe_combinations_that_overlap(
    catalog: SemanticCatalog, dashboard_plan: ReportChangePlan
) -> None:
    margin = dashboard_plan.pages[1].model_copy(
        update={
            "visual_recipes": [
                VisualRecipe.CATEGORY_BAR,
                VisualRecipe.EVIDENCE_SLOT,
            ]
        }
    )
    overlapping = dashboard_plan.model_copy(update={"pages": [margin]})

    with pytest.raises(RecipeError, match="overlap"):
        RecipeCompiler().compile(overlapping, catalog)


def test_recipe_compiler_rejects_unprepared_evidence_bindings(
    catalog: SemanticCatalog, dashboard_plan: ReportChangePlan
) -> None:
    compiler = RecipeCompiler()
    original = compiler.compile(dashboard_plan, catalog)

    with pytest.raises(RecipeError, match="only the approved West driver bridge"):
        compiler.replace_evidence_slot(
            original,
            catalog=catalog,
            metric_id="net_sales",
            dimension_id="region",
            chart_kind="bar",
        )


def test_recipe_compiler_places_only_the_governed_west_driver_bridge_in_slot(
    catalog: SemanticCatalog, dashboard_plan: ReportChangePlan
) -> None:
    compiler = RecipeCompiler()
    original = compiler.compile(dashboard_plan, catalog)

    updated = compiler.replace_evidence_slot(
        original,
        catalog=catalog,
        metric_id="driver_contribution_west",
        dimension_id="driver",
        chart_kind="bar",
    )

    for original_page, updated_page in zip(original.pages, updated.pages, strict=True):
        for original_visual, updated_visual in zip(
            original_page.visuals, updated_page.visuals, strict=True
        ):
            if original_visual.is_evidence_slot:
                assert updated_visual.visual_id == original_visual.visual_id
                assert updated_visual.position == original_visual.position
                assert original_visual.title == "Conversational evidence"
                assert updated_visual.title == ("West margin change · governed contributions")
                assert [binding.reference for binding in updated_visual.bindings] == [
                    "'Driver Contributions'[Driver]",
                    "'Driver Contributions'[Driver Contribution — West]",
                ]
            else:
                assert updated_visual == original_visual


def test_evidence_slot_rejects_noncanonical_prepared_object_pair(
    catalog: SemanticCatalog, dashboard_plan: ReportChangePlan
) -> None:
    blueprint = RecipeCompiler().compile(dashboard_plan, catalog)

    with pytest.raises(RecipeError, match="only the approved West driver bridge"):
        RecipeCompiler().replace_evidence_slot(
            blueprint,
            catalog=catalog,
            metric_id="driver_contribution_all",
            dimension_id="driver",
            chart_kind="bar",
        )


def test_mock_backend_is_idempotent_and_produces_structurally_valid_pbir(
    tmp_path: Path, catalog: SemanticCatalog, dashboard_plan: ReportChangePlan
) -> None:
    report = tmp_path / "Sales.Report"
    blueprint = RecipeCompiler().compile(dashboard_plan, catalog)
    backend = MockPowerBIBackend()

    first = backend.apply(report, blueprint)
    second = backend.apply(report, blueprint)
    validation = validate_report(report, catalog=catalog)

    assert first.report_hash == second.report_hash
    assert first.blueprint_hash == second.blueprint_hash
    assert validation.passed, validation.issues
    pages = json.loads((report / "definition/pages/pages.json").read_text())
    assert pages["pageOrder"] == [page.page_id for page in blueprint.pages]
    assert validate_blueprint_postconditions(report, blueprint).passed


def test_semantic_catalog_binding_audit_reads_active_tmdl_declarations(
    tmp_path: Path, catalog: SemanticCatalog
) -> None:
    model = tmp_path / "Sales.SemanticModel"
    tables = model / "definition/tables"
    tables.mkdir(parents=True)
    declarations: dict[str, list[tuple[str, str]]] = {}
    for item in (*catalog.metrics, *catalog.dimensions, *catalog.report_objects):
        table, member = item.powerbi_ref[:-1].split("[", 1)
        table = table.strip("'")
        kind = (
            "measure" if isinstance(item, (MetricDefinition, ReportObjectDefinition)) else "column"
        )
        if isinstance(item, ReportObjectDefinition):
            kind = item.kind
        declarations.setdefault(table, []).append((kind, member))
    for index, (table, members) in enumerate(sorted(declarations.items())):
        lines = [f"table '{table}'"]
        for kind, member in members:
            lines.append(
                f"\t{kind} '{member}' = 1" if kind == "measure" else f"\tcolumn '{member}'"
            )
        (tables / f"{index}.tmdl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert validate_semantic_catalog_tmdl(model, catalog).passed
    sales_declaration = next(
        path for path in tables.glob("*.tmdl") if "Net Sales" in path.read_text(encoding="utf-8")
    )
    sales_declaration.write_text(
        sales_declaration.read_text(encoding="utf-8").replace("Net Sales", "Missing Net Sales"),
        encoding="utf-8",
    )
    invalid = validate_semantic_catalog_tmdl(model, catalog)

    assert not invalid.passed
    assert any(issue.code == "TMDL_BINDING_MISSING" for issue in invalid.errors)


def test_semantic_catalog_audit_rejects_wrong_kind_and_missing_measure_formula(
    tmp_path: Path, catalog: SemanticCatalog
) -> None:
    model = tmp_path / "Sales.SemanticModel"
    tables = model / "definition/tables"
    tables.mkdir(parents=True)
    lines_by_table: dict[str, list[str]] = {}
    for item in (*catalog.metrics, *catalog.dimensions, *catalog.report_objects):
        table, member = item.powerbi_ref[:-1].split("[", 1)
        table = table.strip("'")
        if item is catalog.metrics[0]:
            declaration = f"\tcolumn '{member}'"
        elif item is catalog.metrics[1]:
            declaration = f"\tmeasure '{member}' ="
        else:
            kind = "measure" if isinstance(item, MetricDefinition) else "column"
            if isinstance(item, ReportObjectDefinition):
                kind = item.kind
            declaration = (
                f"\tmeasure '{member}' = 1" if kind == "measure" else f"\tcolumn '{member}'"
            )
        lines_by_table.setdefault(table, [f"table '{table}'"]).append(declaration)
    for index, lines in enumerate(lines_by_table.values()):
        (tables / f"{index}.tmdl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = validate_semantic_catalog_tmdl(model, catalog)

    assert not report.passed
    assert any(issue.code == "TMDL_BINDING_KIND_MISMATCH" for issue in report.errors)
    assert any(issue.code == "TMDL_MEASURE_FORMULA_MISSING" for issue in report.errors)


def test_binding_validation_rejects_unapproved_semantic_object(
    tmp_path: Path, catalog: SemanticCatalog, dashboard_plan: ReportChangePlan
) -> None:
    report = tmp_path / "Sales.Report"
    blueprint = RecipeCompiler().compile(dashboard_plan, catalog)
    MockPowerBIBackend().apply(report, blueprint)
    visual_path = next((report / "definition/pages").glob("*/visuals/*/visual.json"))
    visual = json.loads(visual_path.read_text())
    projection = next(iter(visual["visual"]["query"]["queryState"].values()))["projections"][0]
    field = projection["field"]
    semantic_kind = next(iter(field))
    field[semantic_kind]["Property"] = "Invented Measure"
    visual_path.write_text(json.dumps(visual), encoding="utf-8")

    validation = validate_report(report, catalog=catalog)

    assert not validation.passed
    assert any(issue.code == "PBIR_BINDING_UNKNOWN" for issue in validation.errors)


def test_layout_validation_rejects_out_of_bounds_visual(
    tmp_path: Path, catalog: SemanticCatalog, dashboard_plan: ReportChangePlan
) -> None:
    report = tmp_path / "Sales.Report"
    blueprint = RecipeCompiler().compile(dashboard_plan, catalog)
    MockPowerBIBackend().apply(report, blueprint)
    visual_path = next((report / "definition/pages").glob("*/visuals/*/visual.json"))
    visual = json.loads(visual_path.read_text())
    visual["position"]["x"] = 1279
    visual["position"]["width"] = 300
    visual_path.write_text(json.dumps(visual), encoding="utf-8")

    validation = validate_report(report, catalog=catalog)

    assert not validation.passed
    assert any(issue.code == "PBIR_LAYOUT_BOUNDS" for issue in validation.errors)


def test_validation_aggregation_fails_closed_on_warnings_and_errors() -> None:
    warning = ValidationReport.single(
        source="official",
        severity=ValidationSeverity.WARNING,
        code="KNOWN_FALSE_POSITIVE",
        message="Review this warning",
    )
    error = ValidationReport.single(
        source="binding",
        severity=ValidationSeverity.ERROR,
        code="BAD_BINDING",
        message="Binding does not resolve",
    )

    assert not aggregate_validation(warning).passed
    combined = aggregate_validation(warning, error)
    assert not combined.passed
    assert [issue.code for issue in combined.errors] == ["BAD_BINDING"]


def test_cli_validation_warning_counter_fails_closed_without_detail_rows() -> None:
    payload = {
        "data": {
            "result": "succeededWithWarnings",
            "errorCount": 0,
            "warningCount": 1,
            "reportPath": "/reports/Sales.Report",
        }
    }
    receipt = CommandReceipt(
        argv=("powerbi-report-author", "validate", "/reports/Sales.Report"),
        cwd=None,
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
        parsed_json=payload,
        duration_ms=5,
    )

    report = validation_from_receipt("microsoft-authoring-cli", receipt)

    assert not report.passed
    assert {issue.code for issue in report.warnings} == {
        "CLI_SUCCEEDED_WITH_WARNINGS",
        "CLI_WARNING_COUNT",
    }


def test_cli_validation_success_with_warnings_flag_fails_closed() -> None:
    payload = {
        "valid": True,
        "errors": [],
        "warnings": [{"message": "Review warning"}],
        "info": [],
        "summary": {"errors": 0, "warnings": 1, "info": 0},
    }
    receipt = CommandReceipt(
        argv=(
            "pbi",
            "--json",
            "report",
            "--path",
            "/reports/Sales.Report",
            "validate",
            "--full",
        ),
        cwd=None,
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
        parsed_json=payload,
        duration_ms=5,
    )

    report = validation_from_receipt("pbi-cli", receipt)

    assert not report.passed
    assert any(issue.severity is ValidationSeverity.WARNING for issue in report.issues)


def test_cli_validation_rejects_unstructured_exit_zero_output() -> None:
    receipt = CommandReceipt(
        argv=("powerbi-report-author", "validate", "Sales.Report"),
        cwd=None,
        returncode=0,
        stdout="Validation completed successfully",
        stderr="",
        parsed_json=None,
        duration_ms=5,
    )

    report = validation_from_receipt("microsoft-authoring-cli", receipt)

    assert not report.passed
    assert [issue.code for issue in report.errors] == ["CLI_VALIDATION_CONTRACT_INVALID"]


@pytest.mark.parametrize("payload", [{}, []])
def test_cli_validation_rejects_empty_json_contract(payload: object) -> None:
    receipt = CommandReceipt(
        argv=("pbi", "--json", "report", "validate"),
        cwd=None,
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
        parsed_json=payload,  # type: ignore[arg-type]
        duration_ms=5,
    )

    report = validation_from_receipt("pbi-cli", receipt)

    assert not report.passed
    assert [issue.code for issue in report.errors] == ["CLI_VALIDATION_CONTRACT_INVALID"]


def test_cli_validation_accepts_only_exact_pinned_success_envelopes() -> None:
    pbi_payload = {
        "valid": True,
        "errors": [],
        "warnings": [],
        "info": [],
        "summary": {"errors": 0, "warnings": 0, "info": 0},
    }
    microsoft_payload = {
        "data": {
            "result": "succeeded",
            "errorCount": 0,
            "warningCount": 0,
            "reportPath": "/reports/Sales.Report",
        }
    }

    assert validation_from_receipt(
        "pbi-cli",
        CommandReceipt(
            argv=(
                "pbi",
                "--json",
                "report",
                "--path",
                "/reports/Sales.Report",
                "validate",
                "--full",
            ),
            cwd=None,
            returncode=0,
            stdout=json.dumps(pbi_payload),
            stderr="",
            parsed_json=pbi_payload,
            duration_ms=1,
        ),
    ).passed
    assert validation_from_receipt(
        "microsoft-authoring-cli",
        CommandReceipt(
            argv=("powerbi-report-author", "validate", "/reports/Sales.Report"),
            cwd=None,
            returncode=0,
            stdout=json.dumps(microsoft_payload),
            stderr="",
            parsed_json=microsoft_payload,
            duration_ms=1,
        ),
    ).passed


def test_microsoft_validator_receipt_rejects_success_for_a_different_report() -> None:
    payload = {
        "data": {
            "result": "succeeded",
            "errorCount": 0,
            "warningCount": 0,
            "reportPath": "/reports/Other.Report",
        }
    }
    receipt = CommandReceipt(
        argv=("powerbi-report-author", "validate", "/reports/Sales.Report"),
        cwd=None,
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
        parsed_json=payload,
        duration_ms=1,
    )

    report = validation_from_receipt(
        "microsoft-authoring-cli",
        receipt,
        expected_report_path=Path("/reports/Sales.Report"),
    )

    assert not report.passed
    assert [issue.code for issue in report.errors] == ["CLI_VALIDATION_CONTRACT_INVALID"]


def test_pbi_validator_receipt_rejects_argv_for_a_different_staged_report() -> None:
    payload = {
        "valid": True,
        "errors": [],
        "warnings": [],
        "info": [],
        "summary": {"errors": 0, "warnings": 0, "info": 0},
    }
    receipt = CommandReceipt(
        argv=(
            "pbi",
            "--json",
            "report",
            "--path",
            "/reports/Other.Report",
            "validate",
            "--full",
        ),
        cwd=None,
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
        parsed_json=payload,
        duration_ms=1,
    )

    report = validation_from_receipt(
        "pbi-cli",
        receipt,
        expected_report_path=Path("/reports/Sales.Report"),
    )

    assert not report.passed
    assert [issue.code for issue in report.errors] == ["CLI_VALIDATION_CONTRACT_INVALID"]
