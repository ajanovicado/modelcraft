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
from ai_powerbi_analyst.powerbi.backend import BackendReceipt, PbiCliBackend
from ai_powerbi_analyst.powerbi.cli import PbiCliAdapter
from ai_powerbi_analyst.powerbi.composer import (
    BaselineReportInvalid,
    BaseReportChanged,
    CatalogVersionMismatch,
    SafePowerBIComposer,
)
from ai_powerbi_analyst.powerbi.mock_backend import MockPowerBIBackend
from ai_powerbi_analyst.powerbi.transaction import PromotionPolicyViolation, hash_tree
from ai_powerbi_analyst.powerbi.validation import ValidationReport


def make_project(root: Path) -> tuple[Path, Path]:
    project = root / "Demo"
    report = project / "Sales.Report"
    pages = report / "definition/pages"
    pages.mkdir(parents=True)
    (pages / "pages.json").write_text(
        json.dumps({"pageOrder": [], "activePageName": None}) + "\n", encoding="utf-8"
    )
    (project / "Sales.pbip").write_text("{}\n", encoding="utf-8")
    return project, report


def catalog() -> SemanticCatalog:
    return SemanticCatalog(
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
            ReportObjectDefinition(
                id="driver",
                name="Driver",
                powerbi_ref="'Driver Contributions'[Driver]",
                kind="column",
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
        ],
    )


def plan(report: Path, *, catalog_version: str = "1.0.0") -> ReportChangePlan:
    return ReportChangePlan(
        base_report_hash=hash_tree(report),
        catalog_version=catalog_version,
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


def test_safe_composer_applies_validated_mock_recipe_and_can_undo(tmp_path: Path) -> None:
    project, report = make_project(tmp_path)
    before = hash_tree(report)
    composer = SafePowerBIComposer(
        project_root=project,
        report_relative_path=Path("Sales.Report"),
        state_root=tmp_path / "state",
        backend=MockPowerBIBackend(),
    )

    result = composer.apply(plan(report), catalog())

    assert result.validation.passed
    assert result.transaction.before_hash == before
    assert hash_tree(report) == result.transaction.after_hash
    undo = composer.undo(result.transaction)
    assert undo.restored_hash == before


def test_safe_composer_rejects_stale_base_hash_before_staging(tmp_path: Path) -> None:
    project, report = make_project(tmp_path)
    stale = plan(report).model_copy(update={"base_report_hash": "f" * 64})
    composer = SafePowerBIComposer(
        project,
        Path("Sales.Report"),
        tmp_path / "state",
        MockPowerBIBackend(),
    )

    with pytest.raises(BaseReportChanged):
        composer.apply(stale, catalog())


def test_safe_composer_rejects_catalog_version_mismatch(tmp_path: Path) -> None:
    project, report = make_project(tmp_path)
    composer = SafePowerBIComposer(
        project,
        Path("Sales.Report"),
        tmp_path / "state",
        MockPowerBIBackend(),
    )

    with pytest.raises(CatalogVersionMismatch):
        composer.apply(plan(report, catalog_version="2.0.0"), catalog())


def test_safe_composer_rejects_invalid_live_report_before_staging(tmp_path: Path) -> None:
    project, report = make_project(tmp_path)
    (report / "definition/pages/pages.json").write_text("{}", encoding="utf-8")
    composer = SafePowerBIComposer(
        project,
        Path("Sales.Report"),
        tmp_path / "state",
        MockPowerBIBackend(),
    )

    with pytest.raises(BaselineReportInvalid, match="PBIR_PAGE_ORDER_INVALID"):
        composer.apply(plan(report), catalog())


def test_production_backend_requires_independent_external_validator(tmp_path: Path) -> None:
    project, _ = make_project(tmp_path)

    with pytest.raises(ValueError, match="external validator"):
        SafePowerBIComposer(
            project,
            Path("Sales.Report"),
            tmp_path / "state",
            PbiCliBackend(PbiCliAdapter(Path("/tools/pbi"))),
        )


def test_safe_composer_applies_evidence_into_only_the_reserved_slot(tmp_path: Path) -> None:
    project, report = make_project(tmp_path)
    evidence_plan = ReportChangePlan(
        base_report_hash=hash_tree(report),
        catalog_version="1.0.0",
        pages=[
            ReportPagePlan(
                key="margin_drivers",
                title="Margin Drivers",
                visual_recipes=[VisualRecipe.KPI_ROW, VisualRecipe.EVIDENCE_SLOT],
                metric_ids=["net_sales"],
                dimension_ids=["month"],
            )
        ],
    )
    composer = SafePowerBIComposer(
        project,
        Path("Sales.Report"),
        tmp_path / "state",
        MockPowerBIBackend(),
    )
    composer.apply(evidence_plan, catalog())
    evidence_plan = evidence_plan.model_copy(update={"base_report_hash": hash_tree(report)})

    result = composer.apply_evidence(
        evidence_plan,
        catalog(),
        metric_id="driver_contribution_west",
        dimension_id="driver",
        chart_kind="bar",
    )

    evidence = next(
        visual
        for page in result.blueprint.pages
        for visual in page.visuals
        if visual.is_evidence_slot
    )
    written = json.loads(
        (
            report
            / "definition/pages"
            / result.blueprint.pages[0].page_id
            / "visuals"
            / evidence.visual_id
            / "visual.json"
        ).read_text()
    )
    assert written["visual"]["visualType"] == "clusteredBarChart"


def test_full_report_reapply_preserves_approved_populated_evidence_slot(
    tmp_path: Path,
) -> None:
    project, report = make_project(tmp_path)
    report_plan = ReportChangePlan(
        base_report_hash=hash_tree(report),
        catalog_version="1.0.0",
        pages=[
            ReportPagePlan(
                key="margin_drivers",
                title="Margin Drivers",
                visual_recipes=[VisualRecipe.KPI_ROW, VisualRecipe.EVIDENCE_SLOT],
                metric_ids=["net_sales"],
                dimension_ids=["month"],
            )
        ],
    )
    composer = SafePowerBIComposer(
        project,
        Path("Sales.Report"),
        tmp_path / "state",
        MockPowerBIBackend(),
    )
    first = composer.apply(report_plan, catalog())
    live_plan = report_plan.model_copy(update={"base_report_hash": hash_tree(report)})
    evidence = composer.apply_evidence(
        live_plan,
        catalog(),
        metric_id="driver_contribution_west",
        dimension_id="driver",
        chart_kind="bar",
    )
    slot = next(
        visual
        for page in evidence.blueprint.pages
        for visual in page.visuals
        if visual.is_evidence_slot
    )
    slot_path = (
        report
        / "definition/pages"
        / first.blueprint.pages[0].page_id
        / "visuals"
        / slot.visual_id
        / "visual.json"
    )
    populated_bytes = slot_path.read_bytes()

    reapplied = composer.apply(
        report_plan.model_copy(update={"base_report_hash": hash_tree(report)}),
        catalog(),
    )

    assert reapplied.validation.passed
    assert slot_path.read_bytes() == populated_bytes
    assert json.loads(populated_bytes)["visual"]["visualType"] == "clusteredBarChart"


def test_full_report_reapply_does_not_preserve_tampered_evidence_slot(
    tmp_path: Path,
) -> None:
    project, report = make_project(tmp_path)
    report_plan = ReportChangePlan(
        base_report_hash=hash_tree(report),
        catalog_version="1.0.0",
        pages=[
            ReportPagePlan(
                key="margin_drivers",
                title="Margin Drivers",
                visual_recipes=[VisualRecipe.KPI_ROW, VisualRecipe.EVIDENCE_SLOT],
                metric_ids=["net_sales"],
                dimension_ids=["month"],
            )
        ],
    )
    composer = SafePowerBIComposer(
        project,
        Path("Sales.Report"),
        tmp_path / "state",
        MockPowerBIBackend(),
    )
    first = composer.apply(report_plan, catalog())
    live_plan = report_plan.model_copy(update={"base_report_hash": hash_tree(report)})
    evidence = composer.apply_evidence(
        live_plan,
        catalog(),
        metric_id="driver_contribution_west",
        dimension_id="driver",
        chart_kind="bar",
    )
    slot = next(
        visual
        for page in evidence.blueprint.pages
        for visual in page.visuals
        if visual.is_evidence_slot
    )
    slot_path = (
        report
        / "definition/pages"
        / first.blueprint.pages[0].page_id
        / "visuals"
        / slot.visual_id
        / "visual.json"
    )
    value = json.loads(slot_path.read_text(encoding="utf-8"))
    value["visual"]["visualContainerObjects"]["title"][0]["properties"]["text"]["expr"]["Literal"][
        "Value"
    ] = "'Untrusted title'"
    slot_path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    reapplied = composer.apply(
        report_plan.model_copy(update={"base_report_hash": hash_tree(report)}),
        catalog(),
    )

    assert reapplied.validation.passed
    rewritten = json.loads(slot_path.read_text(encoding="utf-8"))
    assert rewritten["visual"]["visualType"] == "textbox"


def test_evidence_apply_preserves_every_non_slot_visual_byte_for_byte(tmp_path: Path) -> None:
    project, report = make_project(tmp_path)
    initial_plan = ReportChangePlan(
        base_report_hash=hash_tree(report),
        catalog_version="1.0.0",
        pages=[
            ReportPagePlan(
                key="margin_drivers",
                title="Margin Drivers",
                visual_recipes=[VisualRecipe.KPI_ROW, VisualRecipe.EVIDENCE_SLOT],
                metric_ids=["net_sales"],
                dimension_ids=["month"],
            )
        ],
    )
    composer = SafePowerBIComposer(
        project,
        Path("Sales.Report"),
        tmp_path / "state",
        MockPowerBIBackend(),
    )
    first = composer.apply(initial_plan, catalog())
    page = first.blueprint.pages[0]
    slot = next(visual for visual in page.visuals if visual.is_evidence_slot)
    non_slot = next(visual for visual in page.visuals if not visual.is_evidence_slot)
    non_slot_path = (
        report / "definition/pages" / page.page_id / "visuals" / non_slot.visual_id / "visual.json"
    )
    non_slot_before = non_slot_path.read_bytes()
    manual_path = (
        report / "definition/pages" / page.page_id / "visuals/manual-client-note/visual.json"
    )
    manual_path.parent.mkdir(parents=True)
    manual_payload = {
        "name": "manual-client-note",
        "position": {"x": 1000, "y": 520, "z": 99, "height": 100, "width": 200},
        "visual": {"visualType": "textbox"},
    }
    manual_path.write_text(json.dumps(manual_payload, indent=2) + "\n", encoding="utf-8")
    manual_before = manual_path.read_bytes()

    evidence_plan = initial_plan.model_copy(update={"base_report_hash": hash_tree(report)})
    result = composer.apply_evidence(
        evidence_plan,
        catalog(),
        metric_id="driver_contribution_west",
        dimension_id="driver",
        chart_kind="bar",
    )

    assert result.validation.passed
    assert non_slot_path.read_bytes() == non_slot_before
    assert manual_path.read_bytes() == manual_before
    slot_path = (
        report / "definition/pages" / page.page_id / "visuals" / slot.visual_id / "visual.json"
    )
    assert json.loads(slot_path.read_text())["visual"]["visualType"] == "clusteredBarChart"


def test_composer_rejects_successful_backend_noop_that_misses_blueprint(tmp_path: Path) -> None:
    project, report = make_project(tmp_path)

    class NoOpBackend:
        requires_external_validation = False

        @staticmethod
        def apply(report_path, blueprint):  # type: ignore[no-untyped-def]
            return BackendReceipt(
                backend="noop",
                blueprint_hash=blueprint.fingerprint,
                report_hash=hash_tree(report_path),
            )

        apply_evidence = apply

    composer = SafePowerBIComposer(
        project,
        Path("Sales.Report"),
        tmp_path / "state",
        NoOpBackend(),
    )

    with pytest.raises(PromotionPolicyViolation, match="validation failed"):
        composer.apply(plan(report), catalog())


def test_write_guard_is_rechecked_after_staging_and_aborts_without_promotion(
    tmp_path: Path,
) -> None:
    project, report = make_project(tmp_path)
    before = hash_tree(report)
    checks = 0

    def guard() -> None:
        nonlocal checks
        checks += 1
        if checks >= 2:
            raise RuntimeError("Power BI Desktop opened")

    composer = SafePowerBIComposer(
        project,
        Path("Sales.Report"),
        tmp_path / "state",
        MockPowerBIBackend(),
        write_guard=guard,
    )

    with pytest.raises(RuntimeError, match="Desktop opened"):
        composer.apply(plan(report), catalog())

    assert checks == 2
    assert hash_tree(report) == before


def test_write_guard_is_rechecked_at_the_locked_promotion_swap_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, report = make_project(tmp_path)
    before = hash_tree(report)
    checks = 0
    desktop_open = False

    def guard() -> None:
        nonlocal checks
        checks += 1
        if desktop_open:
            raise RuntimeError("Power BI Desktop opened immediately before promotion")

    composer = SafePowerBIComposer(
        project,
        Path("Sales.Report"),
        tmp_path / "state",
        MockPowerBIBackend(),
        write_guard=guard,
    )
    real_write_intent = composer.transactions._write_recovery_intent

    def write_intent_then_open_desktop(transaction_id: str, before_hash: str) -> None:
        nonlocal desktop_open
        real_write_intent(transaction_id, before_hash)
        desktop_open = True

    monkeypatch.setattr(
        composer.transactions,
        "_write_recovery_intent",
        write_intent_then_open_desktop,
    )

    with pytest.raises(RuntimeError, match="immediately before promotion"):
        composer.apply(plan(report), catalog())

    assert checks >= 4
    assert hash_tree(report) == before


def test_write_guard_is_rechecked_at_the_locked_undo_swap_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, report = make_project(tmp_path)
    before = hash_tree(report)
    undo_mode = False
    undo_checks = 0
    desktop_open = False

    def guard() -> None:
        nonlocal undo_checks
        if not undo_mode:
            return
        undo_checks += 1
        if desktop_open:
            raise RuntimeError("Power BI Desktop opened immediately before undo")

    composer = SafePowerBIComposer(
        project,
        Path("Sales.Report"),
        tmp_path / "state",
        MockPowerBIBackend(),
        write_guard=guard,
    )
    result = composer.apply(plan(report), catalog())
    promoted_hash = hash_tree(report)
    undo_mode = True
    real_write_intent = composer.transactions._write_recovery_intent

    def write_intent_then_open_desktop(transaction_id: str, before_hash: str) -> None:
        nonlocal desktop_open
        real_write_intent(transaction_id, before_hash)
        desktop_open = True

    monkeypatch.setattr(
        composer.transactions,
        "_write_recovery_intent",
        write_intent_then_open_desktop,
    )

    with pytest.raises(RuntimeError, match="immediately before undo"):
        composer.undo(result.transaction)

    assert undo_checks == 2
    assert hash_tree(report) == promoted_hash
    assert promoted_hash != before


def test_external_validator_cannot_mutate_the_staged_report_after_local_validation(
    tmp_path: Path,
) -> None:
    project, report = make_project(tmp_path)
    before = hash_tree(report)

    def mutating_validator(
        report_path: Path,
        _catalog: SemanticCatalog,
    ) -> ValidationReport:
        (report_path / "definition/pages/pages.json").write_text(
            "{}\n",
            encoding="utf-8",
        )
        return ValidationReport(source="mutating-external-validator", issues=())

    composer = SafePowerBIComposer(
        project,
        Path("Sales.Report"),
        tmp_path / "state",
        MockPowerBIBackend(),
        external_validator=mutating_validator,
    )

    with pytest.raises(PromotionPolicyViolation, match="validation failed"):
        composer.apply(plan(report), catalog())

    assert hash_tree(report) == before


def test_stage_mutation_after_validation_cannot_cross_the_promotion_boundary(
    tmp_path: Path,
) -> None:
    project, report = make_project(tmp_path)
    before = hash_tree(report)
    state = tmp_path / "state"
    checks = 0

    def guard() -> None:
        nonlocal checks
        checks += 1
        if checks != 3:
            return
        staged_pages = next(
            (state / "stages").glob("*/project/Sales.Report/definition/pages/pages.json")
        )
        staged_pages.write_text("{}\n", encoding="utf-8")

    composer = SafePowerBIComposer(
        project,
        Path("Sales.Report"),
        state,
        MockPowerBIBackend(),
        write_guard=guard,
    )

    with pytest.raises(PromotionPolicyViolation, match="sealed staged project changed"):
        composer.apply(plan(report), catalog())

    assert checks == 3
    assert hash_tree(report) == before
