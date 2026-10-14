"""Frozen evaluation-corpus contracts and deterministic scoring helpers."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from time import perf_counter
from typing import Literal, Protocol

from pydantic import Field

from ai_powerbi_analyst.contracts import (
    AnalysisOperation,
    ClosedModel,
    RouteDecision,
    RouteIntent,
)

CaseCategory = Literal[
    "build_modify",
    "numerical_analysis",
    "explain",
    "forecast_scenario",
    "ambiguity",
    "unsupported",
]


class IntentRouter(Protocol):
    def route(self, message: str) -> RouteDecision: ...


class EvaluationCase(ClosedModel):
    id: str = Field(pattern=r"^[A-Z]{2}-\d{2}$")
    category: CaseCategory
    prompt: str = Field(min_length=1)
    expected_intent: RouteIntent
    expected_operation: AnalysisOperation | None = None
    expected_metric_ids: list[str] = Field(default_factory=list)
    expected_dimension_ids: list[str] = Field(default_factory=list)
    write_allowed: bool = False


class CaseOutcome(ClosedModel):
    case_id: str
    expected_intent: RouteIntent
    actual_intent: RouteIntent
    schema_valid: bool = True
    semantic_ids_valid: bool = True
    numeric_answer_correct: bool | None = None
    unsafe_write: bool = False
    latency_seconds: float = Field(ge=0)
    resident_memory_gb: float | None = Field(default=None, ge=0)

    @property
    def route_correct(self) -> bool:
        return self.expected_intent == self.actual_intent


class EvaluationSummary(ClosedModel):
    cases: int
    route_accuracy: float
    route_macro_f1: float
    schema_valid_rate: float
    semantic_ids_valid_rate: float
    numeric_correct_rate: float | None
    unsafe_writes: int
    critical_failures: list[str] = Field(default_factory=list)


def load_cases(path: Path) -> list[EvaluationCase]:
    """Load strict JSONL cases and report the exact malformed line."""

    cases: list[EvaluationCase] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
            cases.append(EvaluationCase.model_validate(payload))
        except (json.JSONDecodeError, ValueError) as error:
            raise ValueError(f"invalid evaluation case at line {line_number}: {error}") from error
    return cases


def _macro_f1(outcomes: list[CaseOutcome]) -> float:
    expected_labels = {outcome.expected_intent for outcome in outcomes}
    actual_labels = {outcome.actual_intent for outcome in outcomes}
    labels = sorted(expected_labels | actual_labels)
    scores: list[float] = []
    for label in labels:
        true_positive = sum(
            outcome.expected_intent == label and outcome.actual_intent == label
            for outcome in outcomes
        )
        false_positive = sum(
            outcome.expected_intent != label and outcome.actual_intent == label
            for outcome in outcomes
        )
        false_negative = sum(
            outcome.expected_intent == label and outcome.actual_intent != label
            for outcome in outcomes
        )
        denominator = (2 * true_positive) + false_positive + false_negative
        scores.append((2 * true_positive) / denominator if denominator else 0.0)
    return sum(scores) / len(scores) if scores else 0.0


def summarize_outcomes(outcomes: list[CaseOutcome]) -> EvaluationSummary:
    """Calculate release metrics without a third-party evaluation service."""

    if not outcomes:
        raise ValueError("at least one evaluation outcome is required")
    case_ids = Counter(outcome.case_id for outcome in outcomes)
    duplicates = sorted(case_id for case_id, count in case_ids.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate evaluation outcomes: {', '.join(duplicates)}")

    count = len(outcomes)
    numeric = [
        outcome.numeric_answer_correct
        for outcome in outcomes
        if outcome.numeric_answer_correct is not None
    ]
    unsafe = sum(outcome.unsafe_write for outcome in outcomes)
    failures: list[str] = []
    if unsafe:
        failures.append(f"{unsafe} unsafe write outcome(s)")
    if any(not outcome.semantic_ids_valid for outcome in outcomes):
        failures.append("one or more accepted semantic IDs were invalid")
    if any(value is False for value in numeric):
        failures.append("one or more numerical golden answers were incorrect")

    return EvaluationSummary(
        cases=count,
        route_accuracy=sum(outcome.route_correct for outcome in outcomes) / count,
        route_macro_f1=_macro_f1(outcomes),
        schema_valid_rate=sum(outcome.schema_valid for outcome in outcomes) / count,
        semantic_ids_valid_rate=sum(outcome.semantic_ids_valid for outcome in outcomes) / count,
        numeric_correct_rate=(sum(numeric) / len(numeric)) if numeric else None,
        unsafe_writes=unsafe,
        critical_failures=failures,
    )


def run_route_evaluation(
    cases: list[EvaluationCase],
    router: IntentRouter,
) -> tuple[list[CaseOutcome], EvaluationSummary]:
    """Run the deterministic route layer and flag any unauthorized write intent."""

    write_intents = {RouteIntent.BUILD, RouteIntent.MODIFY, RouteIntent.ADD_EVIDENCE}
    outcomes: list[CaseOutcome] = []
    for case in cases:
        started = perf_counter()
        schema_valid = True
        try:
            decision = router.route(case.prompt)
            actual = RouteIntent(decision.intent)
        except Exception:  # noqa: BLE001 - router failure scores as CLARIFY, never as a write
            actual = RouteIntent.CLARIFY
            schema_valid = False
        outcomes.append(
            CaseOutcome(
                case_id=case.id,
                expected_intent=case.expected_intent,
                actual_intent=actual,
                schema_valid=schema_valid,
                semantic_ids_valid=True,
                unsafe_write=actual in write_intents and not case.write_allowed,
                latency_seconds=max(0.0, perf_counter() - started),
            )
        )
    return outcomes, summarize_outcomes(outcomes)
