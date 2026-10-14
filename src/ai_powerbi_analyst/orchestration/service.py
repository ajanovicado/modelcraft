"""Application use cases with injected analytics and Power BI boundaries."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from threading import Event, Thread
from typing import Any, Protocol

from ai_powerbi_analyst.analytics.service import AnalyticsNoEvidenceError
from ai_powerbi_analyst.contracts import (
    AnalysisPlan,
    EvidenceAnswer,
    ReportChangePlan,
    RouteDecision,
    RouteIntent,
)
from ai_powerbi_analyst.jobs.journal import EventRecord, JobJournal, JobRecord
from ai_powerbi_analyst.jobs.runner import JobRunner
from ai_powerbi_analyst.jobs.runner import ProgressReporter as ProgressReporter

from .analysis_planner import AnalysisPlanningError
from .conversation import ConversationState, ConversationTurn
from .explainer import GroundedExplanationError
from .interpretation import (
    RequestInterpretation,
    RequestInterpreter,
    deterministic_interpretation,
)


class IntentRouter(Protocol):
    def route(self, message: str) -> RouteDecision: ...


class ReportPlanner(Protocol):
    def plan(self, message: str, state: ConversationState) -> ReportChangePlan: ...


class Analyst(Protocol):
    def answer(self, message: str, state: ConversationState) -> EvidenceAnswer | AnalysisResult: ...


class Explainer(Protocol):
    def explain(self, message: str, state: ConversationState) -> str: ...


class ReportComposer(Protocol):
    def apply_report(
        self, plan: ReportChangePlan, report: ProgressReporter
    ) -> Mapping[str, Any]: ...

    def add_evidence(
        self,
        evidence: EvidenceAnswer,
        report: ProgressReporter,
        *,
        report_plan: ReportChangePlan | None,
        analysis_plan: AnalysisPlan | None,
    ) -> Mapping[str, Any]: ...

    def undo(self, report: ProgressReporter) -> Mapping[str, Any]: ...

    def open_power_bi(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    plan: AnalysisPlan
    evidence: EvidenceAnswer


class ReportPlanningError(ValueError):
    """A report request cannot be represented by the approved recipe library."""


@dataclass(frozen=True, slots=True)
class PromptOutcome:
    decision: RouteDecision
    state: ConversationState
    interpretation: RequestInterpretation | None = None
    plan: ReportChangePlan | None = None
    evidence: EvidenceAnswer | None = None
    analysis_plan: AnalysisPlan | None = None
    explanation: str | None = None
    message: str | None = None
    requires_review: bool = False


class ApplicationOrchestrator:
    def __init__(
        self,
        *,
        router: IntentRouter,
        planner: ReportPlanner,
        analyst: Analyst,
        explainer: Explainer,
        composer: ReportComposer,
        journal: JobJournal,
        interpreter: RequestInterpreter | None = None,
    ) -> None:
        self.router = router
        self.planner = planner
        self.analyst = analyst
        self.explainer = explainer
        self.composer = composer
        self.journal = journal
        self.interpreter = interpreter
        self.runner = JobRunner(journal)

    def review(self, message: str, state: ConversationState) -> PromptOutcome:
        decision = self.router.route(message)
        interpretation = self._interpret(message, decision)
        next_state = state.record_message("user", message).record_route(decision.intent)

        if decision.intent in {RouteIntent.BUILD, RouteIntent.MODIFY}:
            next_state = next_state.clear_evidence()
            try:
                plan = self.planner.plan(message, next_state)
            except ReportPlanningError as error:
                clarify = RouteDecision(
                    intent=RouteIntent.CLARIFY,
                    confidence=1.0,
                    missing_information=[str(error)],
                    reason="The requested report change is outside the approved recipes.",
                )
                return self._remember(
                    PromptOutcome(
                        decision=clarify,
                        state=next_state.record_route(RouteIntent.CLARIFY),
                        interpretation=deterministic_interpretation(clarify),
                        message=str(error),
                    )
                )
            return self._remember(
                PromptOutcome(
                    decision=decision,
                    state=next_state,
                    interpretation=interpretation,
                    plan=plan,
                    requires_review=True,
                )
            )

        if decision.intent in {
            RouteIntent.ANALYZE,
            RouteIntent.FORECAST,
            RouteIntent.SCENARIO,
        }:
            try:
                analytical = self.analyst.answer(message, next_state)
            except (AnalysisPlanningError, AnalyticsNoEvidenceError) as error:
                next_state = next_state.clear_evidence()
                clarify = RouteDecision(
                    intent=RouteIntent.CLARIFY,
                    confidence=1.0,
                    missing_information=[str(error)],
                    reason="The request is outside the prepared analytical definition.",
                )
                return self._remember(
                    PromptOutcome(
                        decision=clarify,
                        state=next_state.record_route(RouteIntent.CLARIFY),
                        interpretation=deterministic_interpretation(clarify),
                        message=str(error),
                    )
                )
            if isinstance(analytical, AnalysisResult):
                evidence = analytical.evidence
                analysis_plan = analytical.plan
            else:
                evidence = analytical
                analysis_plan = None
            next_state = next_state.record_evidence(evidence, analysis_plan=analysis_plan)
            return self._remember(
                PromptOutcome(
                    decision=decision,
                    state=next_state,
                    interpretation=interpretation,
                    evidence=evidence,
                    analysis_plan=analysis_plan,
                )
            )

        if decision.intent is RouteIntent.EXPLAIN:
            next_state = next_state.clear_evidence()
            try:
                explanation = self.explainer.explain(message, next_state)
            except GroundedExplanationError as error:
                clarify = RouteDecision(
                    intent=RouteIntent.CLARIFY,
                    confidence=1.0,
                    missing_information=[str(error)],
                    reason="The explanation could not be proven from the active PBIR/TMDL.",
                )
                return self._remember(
                    PromptOutcome(
                        decision=clarify,
                        state=next_state.record_route(RouteIntent.CLARIFY),
                        interpretation=deterministic_interpretation(clarify),
                        message=str(error),
                    )
                )
            next_state = next_state.record_message("assistant", explanation)
            return self._remember(
                PromptOutcome(
                    decision=decision,
                    state=next_state,
                    interpretation=interpretation,
                    explanation=explanation,
                )
            )

        if decision.intent is RouteIntent.ADD_EVIDENCE:
            if state.last_evidence is None or state.last_route is not RouteIntent.ANALYZE:
                clarify = RouteDecision(
                    intent=RouteIntent.CLARIFY,
                    confidence=1.0,
                    missing_information=[
                        "Ask a supported analytical question immediately before adding evidence."
                    ],
                    reason=(
                        "There is no immediately preceding grounded evidence referent in the "
                        "current conversation."
                    ),
                )
                return self._remember(
                    PromptOutcome(
                        decision=clarify,
                        state=next_state.record_route(RouteIntent.CLARIFY),
                        interpretation=deterministic_interpretation(clarify),
                        message=clarify.missing_information[0],
                    )
                )
            return self._remember(
                PromptOutcome(
                    decision=decision,
                    state=next_state,
                    interpretation=interpretation,
                    evidence=state.last_evidence,
                    requires_review=True,
                )
            )

        message_text = (
            decision.missing_information[0]
            if decision.missing_information
            else "That request is outside this prepared local demo."
        )
        next_state = next_state.clear_evidence()
        return self._remember(
            PromptOutcome(
                decision=decision,
                state=next_state.record_message("assistant", message_text),
                interpretation=interpretation,
                message=message_text,
            )
        )

    def _interpret(
        self,
        message: str,
        decision: RouteDecision,
    ) -> RequestInterpretation:
        if self.interpreter is None:
            return deterministic_interpretation(decision)
        try:
            interpretation = self.interpreter.interpret(message, decision)
        except Exception:  # noqa: BLE001 - interpreter failures fall back to deterministic output
            return deterministic_interpretation(decision)
        if interpretation.intent is not decision.intent:
            return deterministic_interpretation(decision)
        return interpretation

    def _remember(self, outcome: PromptOutcome) -> PromptOutcome:
        self.persist_state(outcome.state)
        return outcome

    def persist_state(self, state: ConversationState) -> None:
        self.journal.save_context(state.project_id, _conversation_payload(state))

    def restore_state(
        self,
        project_id: str,
        fallback: ConversationState | None = None,
    ) -> ConversationState:
        payload = self.journal.load_context(project_id)
        if payload is None:
            return fallback or ConversationState(project_id=project_id)
        try:
            return _conversation_from_payload(project_id, payload)
        except (KeyError, TypeError, ValueError):
            return fallback or ConversationState(project_id=project_id)

    @property
    def undo_available(self) -> bool:
        return bool(getattr(self.composer, "undo_available", False))

    def report_plan_installed(self, plan: ReportChangePlan) -> bool:
        checker = getattr(self.composer, "report_plan_installed", None)
        return bool(checker(plan)) if callable(checker) else False

    def evidence_slot_ready(self, plan: ReportChangePlan | None) -> bool:
        if plan is None:
            return False
        checker = getattr(self.composer, "evidence_slot_ready", None)
        return bool(checker(plan)) if callable(checker) else False

    def apply_plan(
        self,
        project_id: str,
        plan: ReportChangePlan,
        *,
        idempotency_key: str,
        progress_listener: Callable[[str, str, float], None] | None = None,
    ) -> JobRecord:
        job = self.journal.create_or_get(
            "apply_report",
            idempotency_key,
            {"project_id": project_id, "plan": plan.model_dump(mode="json")},
        )

        def handler(payload: dict[str, Any], report: ProgressReporter) -> Mapping[str, Any]:
            parsed = ReportChangePlan.model_validate(payload["plan"])
            return self.composer.apply_report(parsed, report)

        return self.runner.run(job.id, handler, progress_listener=progress_listener)

    def apply_evidence(
        self,
        project_id: str,
        evidence: EvidenceAnswer,
        *,
        idempotency_key: str,
        report_plan: ReportChangePlan | None = None,
        analysis_plan: AnalysisPlan | None = None,
        progress_listener: Callable[[str, str, float], None] | None = None,
    ) -> JobRecord:
        job = self.journal.create_or_get(
            "add_evidence",
            idempotency_key,
            {
                "project_id": project_id,
                "evidence": evidence.model_dump(mode="json"),
                "report_plan": report_plan.model_dump(mode="json") if report_plan else None,
                "analysis_plan": analysis_plan.model_dump(mode="json") if analysis_plan else None,
            },
        )

        def handler(payload: dict[str, Any], report: ProgressReporter) -> Mapping[str, Any]:
            parsed = EvidenceAnswer.model_validate(payload["evidence"])
            parsed_report_plan = (
                ReportChangePlan.model_validate(payload["report_plan"])
                if payload["report_plan"] is not None
                else None
            )
            parsed_analysis_plan = (
                AnalysisPlan.model_validate(payload["analysis_plan"])
                if payload["analysis_plan"] is not None
                else None
            )

            return self.composer.add_evidence(
                parsed,
                report,
                report_plan=parsed_report_plan,
                analysis_plan=parsed_analysis_plan,
            )

        return self.runner.run(job.id, handler, progress_listener=progress_listener)

    def undo(
        self,
        project_id: str,
        *,
        idempotency_key: str,
        progress_listener: Callable[[str, str, float], None] | None = None,
    ) -> JobRecord:
        job = self.journal.create_or_get("undo_report", idempotency_key, {"project_id": project_id})

        def handler(_payload: dict[str, Any], report: ProgressReporter) -> Mapping[str, Any]:
            return self.composer.undo(report)

        return self.runner.run(job.id, handler, progress_listener=progress_listener)

    def open_power_bi(
        self,
        project_id: str,
        *,
        idempotency_key: str,
        progress_listener: Callable[[str, str, float], None] | None = None,
    ) -> JobRecord:
        job = self.journal.create_or_get(
            "open_power_bi",
            idempotency_key,
            {"project_id": project_id},
        )

        def handler(_payload: dict[str, Any], report: ProgressReporter) -> Mapping[str, Any]:
            report(
                "reveal",
                "Verifying the validated report and releasing local model memory",
                0.08,
            )
            finished = Event()
            result: dict[str, Mapping[str, Any]] = {}
            failure: dict[str, Exception] = {}

            def launch() -> None:
                try:
                    result["value"] = self.composer.open_power_bi()
                except Exception as error:  # noqa: BLE001 - preserve worker failure for fail-closed job
                    failure["error"] = error
                finally:
                    finished.set()

            worker = Thread(target=launch, name="powerbi-desktop-reveal", daemon=True)
            worker.start()
            progress = 0.18
            while not finished.wait(timeout=4.0):
                report(
                    "reveal",
                    "Still preparing the local Power BI Desktop reveal",
                    progress,
                )
                progress = min(progress + 0.08, 0.82)
            worker.join(timeout=0.1)
            if "error" in failure:
                raise failure["error"]
            opened = result.get("value")
            if opened is None:
                raise RuntimeError("Power BI Desktop launcher returned no result")
            if not bool(opened.get("opened", False)):
                raise RuntimeError(
                    str(opened.get("reason", "Power BI Desktop launch was not confirmed"))
                )
            report("reveal", "Power BI Desktop launch confirmed", 0.95)
            return opened

        return self.runner.run(job.id, handler, progress_listener=progress_listener)

    def job_events(self, job_id: str) -> list[EventRecord]:
        return self.journal.events(job_id)


def _conversation_payload(state: ConversationState) -> dict[str, Any]:
    return {
        "active_page_key": state.active_page_key,
        "active_filters": state.active_filters,
        "last_evidence": (
            state.last_evidence.model_dump(mode="json") if state.last_evidence else None
        ),
        "last_analysis_plan": (
            state.last_analysis_plan.model_dump(mode="json") if state.last_analysis_plan else None
        ),
        "last_route": state.last_route.value if state.last_route else None,
        "report_hash": state.report_hash,
        "turns": [
            {
                "role": turn.role,
                "content": turn.content,
                "created_at": turn.created_at.isoformat(),
            }
            for turn in state.turns
        ],
        "max_turns": state.max_turns,
    }


def _conversation_from_payload(
    project_id: str,
    payload: Mapping[str, Any],
) -> ConversationState:
    evidence_raw = payload.get("last_evidence")
    plan_raw = payload.get("last_analysis_plan")
    route_raw = payload.get("last_route")
    turns_raw = payload.get("turns", [])
    if not isinstance(turns_raw, list):
        raise TypeError("conversation turns must be a list")
    turns = tuple(
        ConversationTurn(
            role=item["role"],
            content=str(item["content"]),
            created_at=datetime.fromisoformat(str(item["created_at"])),
        )
        for item in turns_raw
        if isinstance(item, dict)
    )
    active_filters = payload.get("active_filters", {})
    if not isinstance(active_filters, dict):
        raise TypeError("active filters must be an object")
    active_page_key = payload.get("active_page_key")
    report_hash = payload.get("report_hash")
    return ConversationState(
        project_id=project_id,
        active_page_key=str(active_page_key) if active_page_key is not None else None,
        active_filters=dict(active_filters),
        last_evidence=(
            EvidenceAnswer.model_validate(evidence_raw) if evidence_raw is not None else None
        ),
        last_analysis_plan=(
            AnalysisPlan.model_validate(plan_raw) if plan_raw is not None else None
        ),
        last_route=RouteIntent(str(route_raw)) if route_raw is not None else None,
        report_hash=str(report_hash) if report_hash is not None else None,
        turns=turns,
        max_turns=int(payload.get("max_turns", 20)),
    )
