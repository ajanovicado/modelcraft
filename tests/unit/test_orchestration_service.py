from __future__ import annotations

from typing import Any

from ai_powerbi_analyst.analytics.service import AnalyticsNoEvidenceError
from ai_powerbi_analyst.contracts import (
    ChartSpec,
    EvidenceAnswer,
    EvidenceVisualPlan,
    ReportChangePlan,
    ReportPagePlan,
    RouteDecision,
    RouteIntent,
    VisualRecipe,
)
from ai_powerbi_analyst.jobs.journal import JobJournal, JobStatus
from ai_powerbi_analyst.orchestration.analysis_planner import AnalysisPlanningError
from ai_powerbi_analyst.orchestration.conversation import ConversationState
from ai_powerbi_analyst.orchestration.service import ApplicationOrchestrator


class StubRouter:
    def __init__(self, intent: RouteIntent) -> None:
        self.intent = intent

    def route(self, _message: str) -> RouteDecision:
        return RouteDecision(intent=self.intent, confidence=1.0)


class StubPlanner:
    def __init__(self, plan: ReportChangePlan) -> None:
        self.plan_value = plan
        self.calls: list[str] = []

    def plan(self, message: str, _state: ConversationState) -> ReportChangePlan:
        self.calls.append(message)
        return self.plan_value


class StubAnalyst:
    def __init__(self, answer: EvidenceAnswer) -> None:
        self.answer_value = answer
        self.calls: list[str] = []

    def answer(self, message: str, _state: ConversationState) -> EvidenceAnswer:
        self.calls.append(message)
        return self.answer_value


class RejectingAnalyst:
    def answer(self, _message: str, _state: ConversationState) -> EvidenceAnswer:
        raise AnalysisPlanningError("The approved scenario supports only -3% through +5%.")


class EmptyAnalyst:
    def answer(self, _message: str, _state: ConversationState) -> EvidenceAnswer:
        raise AnalyticsNoEvidenceError(
            "No prepared data exists for that period and filter combination."
        )


class StubExplainer:
    def explain(self, message: str, _state: ConversationState) -> str:
        return f"Grounded explanation: {message}"


class StubComposer:
    def __init__(self) -> None:
        self.applied: list[ReportChangePlan] = []
        self.evidence: list[EvidenceAnswer] = []
        self.undo_calls = 0
        self.open_calls = 0

    def apply_report(self, plan, report) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        self.applied.append(plan)
        report("stage", "Staged safe copy", 0.25)
        report("validate", "Validated", 0.9)
        return {"validated": True, "undo_available": True}

    def add_evidence(  # type: ignore[no-untyped-def]
        self, evidence, report, *, report_plan=None, analysis_plan=None
    ) -> dict[str, Any]:
        self.evidence.append(evidence)
        report("compose", "Updated evidence slot", 0.7)
        return {"validated": True}

    def undo(self, report) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        self.undo_calls += 1
        report("restore", "Restored previous report", 0.8)
        return {"restored": True}

    def open_power_bi(self) -> dict[str, Any]:
        self.open_calls += 1
        return {"opened": True}


def _plan() -> ReportChangePlan:
    return ReportChangePlan(
        base_report_hash="a" * 64,
        catalog_version="1.0.0",
        pages=[
            ReportPagePlan(
                key="executive_pulse",
                title="Executive Pulse",
                visual_recipes=[VisualRecipe.KPI_ROW],
                metric_ids=["net_sales"],
            )
        ],
    )


def _answer() -> EvidenceAnswer:
    return EvidenceAnswer(
        conclusion="Input costs contributed most to the margin decline.",
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
        add_to_report_supported=True,
        target_page_key="margin_drivers",
        report_visual_plan=EvidenceVisualPlan(
            binding_id="west_driver_bridge",
            metric_object_id="driver_contribution_west",
            dimension_object_id="driver",
        ),
    )


def _service(tmp_path, intent: RouteIntent) -> tuple[ApplicationOrchestrator, StubComposer]:
    composer = StubComposer()
    service = ApplicationOrchestrator(
        router=StubRouter(intent),
        planner=StubPlanner(_plan()),
        analyst=StubAnalyst(_answer()),
        explainer=StubExplainer(),
        composer=composer,
        journal=JobJournal(tmp_path / "jobs.sqlite3"),
    )
    return service, composer


def test_build_review_returns_plan_without_applying_it(tmp_path) -> None:
    service, composer = _service(tmp_path, RouteIntent.BUILD)

    outcome = service.review(
        "Build an executive dashboard", ConversationState(project_id="sales-margin-demo")
    )

    assert outcome.plan == _plan()
    assert outcome.requires_review is True
    assert composer.applied == []
    assert outcome.state.last_route is RouteIntent.BUILD


def test_analysis_review_records_evidence_for_follow_up(tmp_path) -> None:
    service, _ = _service(tmp_path, RouteIntent.ANALYZE)

    outcome = service.review(
        "Why did margin fall?", ConversationState(project_id="sales-margin-demo")
    )

    assert outcome.evidence == _answer()
    assert outcome.state.last_evidence == _answer()
    assert outcome.state.active_filters == {"region": "West"}


def test_unsupported_analysis_parameters_return_clarification_instead_of_crashing(
    tmp_path,
) -> None:
    service, _ = _service(tmp_path, RouteIntent.SCENARIO)
    service.analyst = RejectingAnalyst()

    outcome = service.review(
        "What happens with a 20% price increase?",
        ConversationState(project_id="sales-margin-demo"),
    )

    assert outcome.decision.intent is RouteIntent.CLARIFY
    assert outcome.message == "The approved scenario supports only -3% through +5%."


def test_no_data_returns_safe_clarification_and_invalidates_prior_evidence(tmp_path) -> None:
    service, _ = _service(tmp_path, RouteIntent.ANALYZE)
    service.analyst = EmptyAnalyst()
    existing = _answer()
    state = ConversationState(project_id="sales-margin-demo").record_evidence(existing)

    outcome = service.review("What was net sales in 2010?", state)

    assert outcome.decision.intent is RouteIntent.CLARIFY
    assert outcome.message == "No prepared data exists for that period and filter combination."
    assert outcome.state.last_evidence is None
    assert outcome.state.last_analysis_plan is None


def test_add_evidence_without_previous_answer_requests_clarification(tmp_path) -> None:
    service, _ = _service(tmp_path, RouteIntent.ADD_EVIDENCE)

    outcome = service.review("Add this evidence", ConversationState(project_id="sales-margin-demo"))

    assert outcome.decision.intent is RouteIntent.CLARIFY
    assert outcome.evidence is None


def test_intervening_clarification_invalidates_the_deictic_evidence_referent(
    tmp_path,
) -> None:
    service, _ = _service(tmp_path, RouteIntent.ADD_EVIDENCE)
    stale = ConversationState(project_id="sales-margin-demo").record_evidence(_answer())
    stale = stale.record_route(RouteIntent.CLARIFY)

    outcome = service.review("Add this evidence", stale)

    assert outcome.decision.intent is RouteIntent.CLARIFY
    assert outcome.evidence is None
    assert "immediately" in (outcome.message or "")


def test_open_power_bi_is_a_durable_idempotent_job_with_progress(tmp_path) -> None:
    service, composer = _service(tmp_path, RouteIntent.BUILD)
    progress: list[tuple[str, str, float]] = []

    first = service.open_power_bi(
        "sales-margin-demo",
        idempotency_key="open-1",
        progress_listener=lambda phase, message, value: progress.append((phase, message, value)),
    )
    duplicate = service.open_power_bi(
        "sales-margin-demo",
        idempotency_key="open-1",
    )

    assert first.status is JobStatus.SUCCEEDED
    assert duplicate.id == first.id
    assert composer.open_calls == 1
    assert any(phase == "reveal" for phase, _message, _value in progress)


def test_apply_plan_is_durable_and_idempotent(tmp_path) -> None:
    service, composer = _service(tmp_path, RouteIntent.BUILD)

    first = service.apply_plan("sales-margin-demo", _plan(), idempotency_key="apply-1")
    duplicate = service.apply_plan("sales-margin-demo", _plan(), idempotency_key="apply-1")

    assert first.status is JobStatus.SUCCEEDED
    assert duplicate.id == first.id
    assert len(composer.applied) == 1
    assert [event.phase for event in service.job_events(first.id)] == [
        "start",
        "stage",
        "validate",
        "complete",
    ]


def test_evidence_apply_uses_last_grounded_answer(tmp_path) -> None:
    service, composer = _service(tmp_path, RouteIntent.ADD_EVIDENCE)

    job = service.apply_evidence("sales-margin-demo", _answer(), idempotency_key="evidence-1")

    assert job.status is JobStatus.SUCCEEDED
    assert composer.evidence == [_answer()]
