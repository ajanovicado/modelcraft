from __future__ import annotations

from ai_powerbi_analyst.contracts import RouteDecision, RouteIntent
from ai_powerbi_analyst.evaluation import EvaluationCase, run_route_evaluation


class FakeRouter:
    def route(self, message: str) -> RouteDecision:
        intent = RouteIntent.BUILD if message == "build" else RouteIntent.UNSUPPORTED
        return RouteDecision(intent=intent, confidence=1, reason="test")


def test_route_evaluation_marks_unsafe_write_and_scores_summary() -> None:
    cases = [
        EvaluationCase(
            id="BM-01",
            category="build_modify",
            prompt="build",
            expected_intent="BUILD",
            write_allowed=True,
        ),
        EvaluationCase(
            id="UN-01",
            category="unsupported",
            prompt="build",
            expected_intent="UNSUPPORTED",
            write_allowed=False,
        ),
    ]

    outcomes, summary = run_route_evaluation(cases, FakeRouter())

    assert len(outcomes) == 2
    assert summary.route_accuracy == 0.5
    assert summary.unsafe_writes == 1
    assert "1 unsafe write outcome(s)" in summary.critical_failures
