from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from ai_powerbi_analyst.contracts import RouteDecision, RouteIntent
from ai_powerbi_analyst.orchestration.router import RuleFirstRouter


class StubModelRouter:
    def __init__(self, decision: RouteDecision | Exception) -> None:
        self.decision = decision
        self.calls: list[str] = []

    def route(self, message: str) -> RouteDecision:
        self.calls.append(message)
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision


def test_build_requests_are_routed_without_using_the_model() -> None:
    model = StubModelRouter(RouteDecision(intent=RouteIntent.ANALYZE, confidence=0.99))
    router = RuleFirstRouter(model_fallback=model)

    decision = router.route("Build a three-page executive dashboard for sales and margin")

    assert decision.intent is RouteIntent.BUILD
    assert decision.confidence >= 0.95
    assert model.calls == []


def test_add_evidence_takes_priority_over_generic_modify_language() -> None:
    router = RuleFirstRouter()

    decision = router.route("Add this evidence to the Margin Drivers page")

    assert decision.intent is RouteIntent.ADD_EVIDENCE


def test_unsupported_model_changes_fail_closed() -> None:
    router = RuleFirstRouter()

    decision = router.route("Create a new DAX measure and change the relationships")

    assert decision.intent is RouteIntent.UNSUPPORTED
    assert decision.confidence == 1.0


def test_ambiguous_request_uses_structured_model_fallback() -> None:
    expected = RouteDecision(
        intent=RouteIntent.EXPLAIN,
        confidence=0.82,
        reason="The user asks about the meaning of a report object.",
    )
    model = StubModelRouter(expected)
    router = RuleFirstRouter(model_fallback=model)

    assert router.route("Tell me about this").intent is RouteIntent.EXPLAIN
    assert model.calls == ["Tell me about this"]


def test_model_failure_returns_clarification_instead_of_guessing() -> None:
    model = StubModelRouter(RuntimeError("ollama unavailable"))
    router = RuleFirstRouter(model_fallback=model)

    decision = router.route("Can you help with it?")

    assert decision.intent is RouteIntent.CLARIFY
    assert decision.missing_information


def test_plural_forecast_and_scenario_forms_keep_the_closed_routes() -> None:
    router = RuleFirstRouter()

    assert router.route("Show revenue forecasts").intent is RouteIntent.FORECAST
    assert router.route("Show gross profit projections").intent is RouteIntent.FORECAST
    assert router.route("Compare the 3% and 5% price scenarios").intent is RouteIntent.SCENARIO


def test_rule_first_router_meets_frozen_corpus_gate() -> None:
    corpus = Path(__file__).parents[2] / "evals" / "cases" / "model_contract_cases.jsonl"
    cases = [json.loads(line) for line in corpus.read_text(encoding="utf-8").splitlines()]
    router = RuleFirstRouter()
    labels = {intent.value for intent in RouteIntent}
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    write_intents = {RouteIntent.BUILD, RouteIntent.MODIFY, RouteIntent.ADD_EVIDENCE}

    for case in cases:
        predicted = router.route(case["prompt"]).intent
        expected = case["expected_intent"]
        for label in labels:
            if predicted.value == label and expected == label:
                counts[label]["tp"] += 1
            elif predicted.value == label:
                counts[label]["fp"] += 1
            elif expected == label:
                counts[label]["fn"] += 1
        if not case["write_allowed"]:
            assert predicted not in write_intents

    f1_scores: list[float] = []
    for label in labels:
        count = counts[label]
        denominator = 2 * count["tp"] + count["fp"] + count["fn"]
        if denominator:
            f1_scores.append(2 * count["tp"] / denominator)
    assert sum(f1_scores) / len(f1_scores) >= 0.95
