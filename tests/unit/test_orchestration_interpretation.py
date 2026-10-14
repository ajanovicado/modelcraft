from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.contracts import (
    DimensionDefinition,
    MetricDefinition,
    ReportChangePlan,
    ReportPagePlan,
    RouteDecision,
    RouteIntent,
    SemanticCatalog,
    VisualRecipe,
)
from ai_powerbi_analyst.jobs.journal import JobJournal
from ai_powerbi_analyst.orchestration.conversation import ConversationState
from ai_powerbi_analyst.orchestration.interpretation import (
    OllamaRequestInterpreter,
    RequestInterpretation,
)
from ai_powerbi_analyst.orchestration.service import ApplicationOrchestrator


class StubStructuredClient:
    def __init__(self, result: dict[str, object] | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, str, type[Any]]] = []

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[Any],
    ) -> Any:
        self.calls.append((system_prompt, user_prompt, response_model))
        if isinstance(self.result, Exception):
            raise self.result
        return response_model.model_validate(self.result)


def _catalog() -> SemanticCatalog:
    return SemanticCatalog(
        version="1.0.0",
        metrics=[
            MetricDefinition(
                id="net_sales",
                name="Net Sales",
                description="Governed net sales.",
                format="$#,##0",
                powerbi_ref="Measures.Net Sales",
                sql_expression="SUM(net_sales)",
                allowed_dimensions=["region"],
            )
        ],
        dimensions=[
            DimensionDefinition(
                id="region",
                name="Region",
                column="region",
                powerbi_ref="Region.Region",
                data_type="string",
            )
        ],
    )


def _decision(intent: RouteIntent = RouteIntent.ANALYZE) -> RouteDecision:
    return RouteDecision(intent=intent, confidence=0.97)


def test_ollama_interpretation_is_one_closed_display_only_call() -> None:
    client = StubStructuredClient(
        {
            "intent": "ANALYZE",
            "summary": "I will focus on governed sales performance by region.",
            "metric_ids": ["net_sales"],
            "dimension_ids": ["region"],
        }
    )
    interpreter = OllamaRequestInterpreter(client, _catalog())  # type: ignore[arg-type]

    result = interpreter.interpret("How are sales doing by region?", _decision())

    assert result == RequestInterpretation(
        intent=RouteIntent.ANALYZE,
        summary="I will focus on governed sales performance by region.",
        metric_ids=["net_sales"],
        dimension_ids=["region"],
        source="local_model",
    )
    assert len(client.calls) == 1
    system_prompt, raw_payload, response_model = client.calls[0]
    payload = json.loads(raw_payload)
    assert payload["authoritative_intent"] == "ANALYZE"
    assert payload["approved_metric_ids"] == ["net_sales"]
    assert payload["approved_dimension_ids"] == ["region"]
    assert payload["untrusted_request"] == "How are sales doing by region?"
    assert "cannot route, plan, calculate, or change a report" in system_prompt
    assert response_model.model_json_schema()["additionalProperties"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {
            "intent": "BUILD",
            "summary": "I will focus on governed sales performance.",
            "metric_ids": ["net_sales"],
            "dimension_ids": [],
        },
        {
            "intent": "ANALYZE",
            "summary": "I will focus on governed sales performance.",
            "metric_ids": ["invented_metric"],
            "dimension_ids": [],
        },
        {
            "intent": "ANALYZE",
            "summary": "Sales increased by twenty percent.",
            "metric_ids": ["net_sales"],
            "dimension_ids": [],
        },
    ],
)
def test_mismatched_unsafe_or_unapproved_model_metadata_falls_back(
    payload: dict[str, object],
) -> None:
    client = StubStructuredClient(payload)
    interpreter = OllamaRequestInterpreter(client, _catalog())  # type: ignore[arg-type]

    result = interpreter.interpret("untrusted request", _decision())

    assert result.intent is RouteIntent.ANALYZE
    assert result.source == "deterministic_fallback"
    assert result.metric_ids == []
    assert len(client.calls) == 1


def test_timeout_falls_back_without_interrupting_the_request() -> None:
    client = StubStructuredClient(TimeoutError("local model timed out"))
    interpreter = OllamaRequestInterpreter(client, _catalog())  # type: ignore[arg-type]

    result = interpreter.interpret("Analyze sales", _decision())

    assert result.source == "deterministic_fallback"
    assert result.summary == "I interpreted this as a grounded numerical analysis request."
    assert len(client.calls) == 1


class StubRouter:
    def route(self, _message: str) -> RouteDecision:
        return _decision(RouteIntent.BUILD)


class StubPlanner:
    def __init__(self, plan: ReportChangePlan) -> None:
        self.plan_value = plan
        self.calls = 0

    def plan(self, _message: str, _state: ConversationState) -> ReportChangePlan:
        self.calls += 1
        return self.plan_value


class UnusedBoundary:
    def answer(self, *_args: object) -> None:
        raise AssertionError("analysis must not run for a build request")

    def explain(self, *_args: object) -> str:
        raise AssertionError("explanation must not run for a build request")

    def apply_report(self, *_args: object) -> dict[str, object]:
        raise AssertionError("review must not write")

    def add_evidence(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("review must not write")

    def undo(self, *_args: object) -> dict[str, object]:
        raise AssertionError("review must not write")

    def open_power_bi(self) -> dict[str, object]:
        raise AssertionError("review must not open Desktop")


class MismatchedInterpreter:
    def __init__(self) -> None:
        self.calls = 0

    def interpret(
        self,
        _message: str,
        _decision_value: RouteDecision,
    ) -> RequestInterpretation:
        self.calls += 1
        return RequestInterpretation(
            intent=RouteIntent.ANALYZE,
            summary="I interpreted this as a numerical analysis request.",
            source="local_model",
        )


def _build_plan() -> ReportChangePlan:
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


def test_interpretation_cannot_change_route_plan_or_write_behavior(tmp_path: Path) -> None:
    planner = StubPlanner(_build_plan())
    boundary = UnusedBoundary()
    interpreter = MismatchedInterpreter()
    orchestrator = ApplicationOrchestrator(
        router=StubRouter(),
        planner=planner,
        analyst=boundary,  # type: ignore[arg-type]
        explainer=boundary,
        composer=boundary,  # type: ignore[arg-type]
        journal=JobJournal(tmp_path / "jobs.sqlite3"),
        interpreter=interpreter,
    )

    outcome = orchestrator.review(
        "Build the approved report.",
        ConversationState(project_id="sales-margin-demo"),
    )

    assert outcome.decision.intent is RouteIntent.BUILD
    assert outcome.plan == _build_plan()
    assert outcome.requires_review is True
    assert outcome.interpretation is not None
    assert outcome.interpretation.intent is RouteIntent.BUILD
    assert outcome.interpretation.source == "deterministic_fallback"
    assert interpreter.calls == 1
    assert planner.calls == 1
