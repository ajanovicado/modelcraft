from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_powerbi_analyst.contracts import (
    AnalysisOperation,
    AnalysisPlan,
    ChartSpec,
    DimensionDefinition,
    EvidenceAnswer,
    EvidenceVisualPlan,
    MetricDefinition,
    ReportChangePlan,
    ReportObjectDefinition,
    ReportPagePlan,
    RouteIntent,
    SemanticCatalog,
    VisualRecipe,
)
from ai_powerbi_analyst.orchestration.conversation import ConversationState
from ai_powerbi_analyst.orchestration.integrations import (
    AnalyticsServiceAdapter,
    EvidencePlacementUnsupported,
    SafeComposerAdapter,
    ThreePageReportPlanner,
)
from ai_powerbi_analyst.orchestration.service import AnalysisResult
from ai_powerbi_analyst.powerbi.transaction import TransactionReceipt, UndoReceipt, hash_tree


def _catalog() -> SemanticCatalog:
    metrics = []
    for identifier, name, reference in (
        ("net_sales", "Net Sales", "'Measures'[Net Sales]"),
        ("units", "Units", "'Measures'[Units]"),
        ("gross_profit", "Gross Profit", "'Measures'[Gross Profit]"),
        ("gross_margin_pct", "Gross Margin %", "'Measures'[Gross Margin %]"),
        ("sales_growth", "Sales Growth", "'Measures'[Sales Growth]"),
        ("average_selling_price", "Average Selling Price", "'Measures'[Average Selling Price]"),
        ("unit_cost", "Unit Cost", "'Measures'[Unit Cost]"),
    ):
        metrics.append(
            MetricDefinition(
                id=identifier,
                name=name,
                description=f"Governed {name}.",
                format=".1%" if identifier.endswith("pct") else "$,.0f",
                powerbi_ref=reference,
                sql_expression="SUM(value)",
                allowed_dimensions=["month", "region", "category"],
            )
        )
    return SemanticCatalog(
        version="1.0.0",
        metrics=metrics,
        dimensions=[
            DimensionDefinition(
                id="month",
                name="Month",
                column="month_start",
                powerbi_ref="'Date'[Month]",
                data_type="date",
            ),
            DimensionDefinition(
                id="region",
                name="Region",
                column="region",
                powerbi_ref="'Region'[Region]",
                data_type="string",
            ),
            DimensionDefinition(
                id="category",
                name="Category",
                column="category",
                powerbi_ref="'Product'[Category]",
                data_type="string",
            ),
        ],
        report_objects=[
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
        ],
    )


def _plan() -> ReportChangePlan:
    return ReportChangePlan(
        base_report_hash="a" * 64,
        catalog_version="1.0.0",
        pages=[
            ReportPagePlan(
                key="margin_drivers",
                title="Margin Drivers",
                visual_recipes=[VisualRecipe.EVIDENCE_SLOT],
                metric_ids=["net_sales"],
                dimension_ids=["region"],
            )
        ],
    )


def _answer() -> EvidenceAnswer:
    return EvidenceAnswer(
        conclusion="West contributed the largest change.",
        facts=["West contributed $2.1M."],
        metric_ids=["net_sales"],
        comparison_period="Q1 2025 vs Q1 2024",
        filters={},
        data_hash="b" * 64,
        catalog_hash="d" * 64,
        catalog_version="1.0.0",
        query_plan_hash="c" * 64,
        chart=ChartSpec(
            kind="contribution_bar",
            title="Change by region",
            x_field="value",
            y_field="region",
        ),
        rows=[{"region": "West", "value": 2_100_000}],
        add_to_report_supported=False,
    )


def _driver_answer() -> EvidenceAnswer:
    return EvidenceAnswer(
        conclusion="Costs more than offset positive volume in the West.",
        facts=["Cost contributed -$136,578."],
        metric_ids=["net_sales", "gross_profit", "gross_margin_pct"],
        comparison_period="2025 Q4 vs 2024 Q4",
        filters={"region": "West"},
        data_hash="b" * 64,
        catalog_hash="d" * 64,
        catalog_version="1.0.0",
        query_plan_hash="c" * 64,
        chart=ChartSpec(
            kind="contribution_bar",
            title="Gross Profit variance by governed driver",
            x_field="contribution_value",
            y_field="driver",
        ),
        rows=[
            {
                "driver": "Cost",
                "contribution_value": -136_577.56,
                "current_period": "2025 Q4",
                "comparison_period": "2024 Q4",
            }
        ],
        add_to_report_supported=True,
        target_page_key="margin_drivers",
        report_visual_plan=EvidenceVisualPlan(
            binding_id="west_driver_bridge",
            metric_object_id="driver_contribution_west",
            dimension_object_id="driver",
        ),
    )


class StubAnalysisPlanner:
    def plan(self, _message: str) -> AnalysisPlan:
        return AnalysisPlan(
            operation=AnalysisOperation.CONTRIBUTION,
            metric_ids=["net_sales"],
            dimension_ids=["region"],
        )


class StubAnalyticsService:
    def answer(self, _plan: AnalysisPlan) -> EvidenceAnswer:
        return _answer()


def test_analytics_adapter_preserves_plan_for_evidence_follow_up() -> None:
    adapter = AnalyticsServiceAdapter(StubAnalysisPlanner(), StubAnalyticsService())

    result = adapter.answer("What changed by region?", ConversationState(project_id="demo"))

    assert isinstance(result, AnalysisResult)
    assert result.plan.dimension_ids == ["region"]
    assert result.evidence == _answer()


def test_three_page_planner_uses_current_report_hash_and_catalog() -> None:
    planner = ThreePageReportPlanner(_catalog(), report_hash=lambda: "d" * 64)

    plan = planner.plan("Build the standard report", ConversationState(project_id="demo"))

    assert plan.base_report_hash == "d" * 64
    assert [page.key for page in plan.pages] == [
        "executive_pulse",
        "margin_drivers",
        "outlook_decisions",
    ]
    assert plan.pages[0].metric_ids == [
        "net_sales",
        "gross_profit",
        "gross_margin_pct",
        "sales_growth",
    ]


@pytest.mark.parametrize(
    ("prompt", "expected_page"),
    [
        (
            "Update the Executive Pulse page using the approved KPI and trend recipe.",
            "executive_pulse",
        ),
        (
            "Refresh the Margin Drivers layout with the governed bridge and detail matrix.",
            "margin_drivers",
        ),
        (
            "Replace the existing Outlook page with the approved forecast and scenario recipe.",
            "outlook_decisions",
        ),
    ],
)
def test_report_planner_limits_modifications_to_the_named_approved_page(
    prompt: str,
    expected_page: str,
) -> None:
    planner = ThreePageReportPlanner(_catalog(), report_hash=lambda: "d" * 64)

    plan = planner.plan(
        prompt,
        ConversationState(project_id="demo", last_route=RouteIntent.MODIFY),
    )

    assert [page.key for page in plan.pages] == [expected_page]


def test_report_planner_rejects_arbitrary_modifications() -> None:
    planner = ThreePageReportPlanner(_catalog(), report_hash=lambda: "d" * 64)

    with pytest.raises(ValueError, match="Supported report changes"):
        planner.plan(
            "Move the chart and rename the page",
            ConversationState(project_id="demo", last_route=RouteIntent.MODIFY),
        )


@pytest.mark.parametrize(
    "prompt",
    [
        "Build a one-page sales dashboard",
        "Build a five-page sales dashboard",
        "Build a seven page report",
        "Build a sales dashboard without a forecast",
        "Build a report without slicers",
        "Build a dashboard with a pie chart",
        "Build a finance dashboard",
        "Build an inventory dashboard",
    ],
)
def test_report_planner_rejects_build_scope_that_would_be_silently_changed(
    prompt: str,
) -> None:
    planner = ThreePageReportPlanner(_catalog(), report_hash=lambda: "d" * 64)

    with pytest.raises(ValueError):
        planner.plan(
            prompt,
            ConversationState(project_id="demo", last_route=RouteIntent.BUILD),
        )


@dataclass(frozen=True)
class FakeProgress:
    stage: str
    message: str
    percent: int


class FakeSafeComposer:
    def __init__(self, project_root: Path, state_root: Path) -> None:
        self.transactions = SimpleNamespace(
            project_root=project_root,
            report_relative_path=Path("Demo.Report"),
            report_path=project_root / "Demo.Report",
            state_root=state_root,
        )
        self.apply_calls = 0
        self.no_op_next = False
        self.evidence_calls: list[dict[str, object]] = []
        self.undo_calls: list[TransactionReceipt] = []

    def _result(self) -> SimpleNamespace:
        no_op = self.no_op_next
        self.no_op_next = False
        transaction = TransactionReceipt(
            transaction_id=f"tx-{self.apply_calls + len(self.evidence_calls)}",
            project_root=self.transactions.project_root,
            report_relative_path=self.transactions.report_relative_path,
            before_hash="1" * 64,
            after_hash="1" * 64 if no_op else "2" * 64,
            changed_paths=() if no_op else ("definition/pages/pages.json",),
            backup_path=(None if no_op else self.transactions.state_root / "undo" / "report"),
            validation_sources=("local",),
        )
        return SimpleNamespace(
            transaction=transaction,
            validation=SimpleNamespace(passed=True),
            backend=SimpleNamespace(backend="fake"),
            blueprint=SimpleNamespace(fingerprint="f" * 64),
        )

    def apply(self, _plan, _catalog, *, progress=None):  # type: ignore[no-untyped-def]
        self.apply_calls += 1
        if progress:
            progress(FakeProgress("validating", "Validated", 90))
        return self._result()

    def apply_evidence(self, plan, _catalog, **kwargs):  # type: ignore[no-untyped-def]
        self.evidence_calls.append({"plan": plan, **kwargs})
        return self._result()

    def undo(self, receipt, *, progress=None):  # type: ignore[no-untyped-def]
        self.undo_calls.append(receipt)
        return UndoReceipt(
            transaction_id=receipt.transaction_id,
            replaced_hash=receipt.after_hash,
            restored_hash=receipt.before_hash,
        )


def test_safe_composer_adapter_persists_receipt_and_reloads_for_undo(tmp_path) -> None:
    project = tmp_path / "project"
    report = project / "Demo.Report"
    report.mkdir(parents=True)
    (report / "seed.txt").write_text("seed")
    state = tmp_path / "state"
    fake = FakeSafeComposer(project, state)
    adapter = SafeComposerAdapter(
        fake, _catalog(), catalog_hash="d" * 64, data_hash="b" * 64, state_root=state
    )

    result = adapter.apply_report(_plan(), lambda *_: None)
    restarted = SafeComposerAdapter(
        fake, _catalog(), catalog_hash="d" * 64, data_hash="b" * 64, state_root=state
    )
    undo = restarted.undo(lambda *_: None)

    assert result["validated"] is True
    assert undo["restored"] is True
    assert len(fake.undo_calls) == 1


def test_noop_reapply_preserves_the_last_meaningful_undo_receipt(tmp_path: Path) -> None:
    project = tmp_path / "project"
    report = project / "Demo.Report"
    report.mkdir(parents=True)
    (report / "seed.txt").write_text("seed")
    state = tmp_path / "state"
    fake = FakeSafeComposer(project, state)
    adapter = SafeComposerAdapter(
        fake, _catalog(), catalog_hash="d" * 64, data_hash="b" * 64, state_root=state
    )

    first = adapter.apply_report(_plan(), lambda *_: None)
    first_receipt = adapter._load_receipt()
    fake.no_op_next = True
    no_op = adapter.apply_report(_plan(), lambda *_: None)
    preserved = adapter._load_receipt()
    undo = adapter.undo(lambda *_: None)

    assert first["undo_available"] is True
    assert no_op["undo_available"] is True
    assert preserved.transaction_id == first_receipt.transaction_id
    assert undo["transaction_id"] == first_receipt.transaction_id


def test_consumed_transaction_journal_cannot_resurrect_undo(tmp_path: Path) -> None:
    project = tmp_path / "project"
    report = project / "Demo.Report"
    report.mkdir(parents=True)
    (report / "seed.txt").write_text("current", encoding="utf-8")
    state = tmp_path / "state"
    journal = state / "journal"
    backup = state / "undo" / "report"
    journal.mkdir(parents=True)
    backup.mkdir(parents=True)
    transaction_id = "a" * 32
    live_hash = hash_tree(report)
    committed = {
        "transaction_id": transaction_id,
        "project_root": str(project),
        "report_relative_path": "Demo.Report",
        "before_hash": "1" * 64,
        "after_hash": live_hash,
        "changed_paths": ["seed.txt"],
        "backup_path": str(backup),
        "validation_sources": ["local"],
        "status": "committed",
    }
    (journal / f"{transaction_id}.json").write_text(json.dumps(committed), encoding="utf-8")
    (journal / "undo-entry.undo.json").write_text(
        json.dumps(
            {
                "kind": "undo",
                "status": "undone",
                "transaction_id": transaction_id,
            }
        ),
        encoding="utf-8",
    )
    fake = FakeSafeComposer(project, state)

    adapter = SafeComposerAdapter(
        fake, _catalog(), catalog_hash="d" * 64, data_hash="b" * 64, state_root=state
    )

    assert adapter.undo_available is False
    with pytest.raises(FileNotFoundError, match="no reversible"):
        adapter.undo(lambda *_: None)


def test_safe_composer_adapter_rejects_generic_unprepared_evidence(tmp_path) -> None:
    project = tmp_path / "project"
    report = project / "Demo.Report"
    report.mkdir(parents=True)
    (report / "seed.txt").write_text("seed")
    state = tmp_path / "state"
    fake = FakeSafeComposer(project, state)
    adapter = SafeComposerAdapter(
        fake, _catalog(), catalog_hash="d" * 64, data_hash="b" * 64, state_root=state
    )
    analysis = AnalysisPlan(
        operation=AnalysisOperation.CONTRIBUTION,
        metric_ids=["net_sales"],
        dimension_ids=["region"],
    )

    with pytest.raises(EvidencePlacementUnsupported, match="no explicit prepared"):
        adapter.add_evidence(
            _answer(),
            lambda *_: None,
            report_plan=_plan(),
            analysis_plan=analysis,
        )

    assert fake.evidence_calls == []


def test_safe_composer_adapter_places_canonical_west_driver_bridge(tmp_path) -> None:
    project = tmp_path / "project"
    report = project / "Demo.Report"
    report.mkdir(parents=True)
    (report / "seed.txt").write_text("seed")
    state = tmp_path / "state"
    fake = FakeSafeComposer(project, state)
    adapter = SafeComposerAdapter(
        fake, _catalog(), catalog_hash="d" * 64, data_hash="b" * 64, state_root=state
    )
    driver = AnalysisPlan(
        operation=AnalysisOperation.DRIVER_BRIDGE,
        metric_ids=["gross_profit"],
        filters={"region": "West"},
    )

    result = adapter.add_evidence(
        _driver_answer(),
        lambda *_: None,
        report_plan=_plan().model_copy(update={"base_report_hash": hash_tree(report)}),
        analysis_plan=driver,
    )

    assert result["validated"] is True
    assert fake.evidence_calls[0]["metric_id"] == "driver_contribution_west"
    assert fake.evidence_calls[0]["dimension_id"] == "driver"
    assert fake.evidence_calls[0]["chart_kind"] == "bar"


@pytest.mark.parametrize(
    "mismatch",
    ["filters", "chart", "period", "data_hash", "catalog_hash"],
)
def test_safe_composer_adapter_rejects_mismatched_prepared_evidence(
    tmp_path: Path, mismatch: str
) -> None:
    project = tmp_path / "project"
    report = project / "Demo.Report"
    report.mkdir(parents=True)
    (report / "seed.txt").write_text("seed")
    state = tmp_path / "state"
    fake = FakeSafeComposer(project, state)
    adapter = SafeComposerAdapter(
        fake, _catalog(), catalog_hash="d" * 64, data_hash="b" * 64, state_root=state
    )
    evidence = _driver_answer()
    if mismatch == "filters":
        evidence = evidence.model_copy(update={"filters": {"region": "East"}})
    elif mismatch == "chart":
        evidence = evidence.model_copy(
            update={"chart": evidence.chart.model_copy(update={"x_field": "value"})}
        )
    elif mismatch == "period":
        evidence = evidence.model_copy(
            update={
                "rows": [
                    {
                        **evidence.rows[0],
                        "current_period": "2025 Q3",
                    }
                ]
            }
        )
    elif mismatch == "data_hash":
        evidence = evidence.model_copy(update={"data_hash": "e" * 64})
    else:
        evidence = evidence.model_copy(update={"catalog_hash": "e" * 64})
    driver = AnalysisPlan(
        operation=AnalysisOperation.DRIVER_BRIDGE,
        metric_ids=["gross_profit"],
        filters={"region": "West"},
    )

    with pytest.raises(EvidencePlacementUnsupported, match="does not match"):
        adapter.add_evidence(
            evidence,
            lambda *_: None,
            report_plan=_plan(),
            analysis_plan=driver,
        )

    assert fake.evidence_calls == []


@pytest.mark.parametrize(
    "filters",
    [
        {},
        {"region": "East"},
        {"region": "West", "category": "Hardware"},
    ],
)
def test_safe_composer_adapter_rejects_unsupported_driver_filters(
    tmp_path: Path, filters: dict[str, str]
) -> None:
    project = tmp_path / "project"
    report = project / "Demo.Report"
    report.mkdir(parents=True)
    (report / "seed.txt").write_text("seed")
    state = tmp_path / "state"
    adapter = SafeComposerAdapter(
        FakeSafeComposer(project, state),
        _catalog(),
        catalog_hash="d" * 64,
        data_hash="b" * 64,
        state_root=state,
    )
    driver = AnalysisPlan(
        operation=AnalysisOperation.DRIVER_BRIDGE,
        metric_ids=["gross_profit"],
        filters=filters,
    )

    with pytest.raises(EvidencePlacementUnsupported):
        adapter.add_evidence(
            _driver_answer(),
            lambda *_: None,
            report_plan=_plan(),
            analysis_plan=driver,
        )
