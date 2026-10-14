from __future__ import annotations

from datetime import date
from pathlib import Path

from ai_powerbi_analyst.analytics.compiler import compile_analysis
from ai_powerbi_analyst.contracts import AnalysisOperation
from ai_powerbi_analyst.data import load_semantic_catalog
from ai_powerbi_analyst.evaluation import load_cases
from ai_powerbi_analyst.orchestration.analysis_planner import RuleFirstAnalysisPlanner

PROJECT_ROOT = Path(__file__).parents[2]


def test_all_governed_analysis_cases_compile_with_expected_semantics() -> None:
    catalog = load_semantic_catalog(PROJECT_ROOT / "fixtures/data/semantic_catalog.yaml")
    planner = RuleFirstAnalysisPlanner(catalog)
    cases = [
        case
        for case in load_cases(PROJECT_ROOT / "evals/cases/model_contract_cases.jsonl")
        if case.expected_operation is not None
    ]

    failures: list[str] = []
    for case in cases:
        try:
            plan = planner.plan(case.prompt)
            if plan.operation != case.expected_operation:
                failures.append(
                    f"{case.id}: operation {plan.operation.value}, expected "
                    f"{case.expected_operation.value}"
                )
                continue
            if plan.operation is AnalysisOperation.DRIVER_BRIDGE:
                if plan.metric_ids != ["gross_profit"]:
                    failures.append(f"{case.id}: driver metrics {plan.metric_ids}")
            elif not set(case.expected_metric_ids).issubset(plan.metric_ids):
                failures.append(
                    f"{case.id}: metrics {plan.metric_ids}, expected {case.expected_metric_ids}"
                )
            if not set(case.expected_dimension_ids).issubset(plan.dimension_ids):
                failures.append(
                    f"{case.id}: dimensions {plan.dimension_ids}, expected "
                    f"{case.expected_dimension_ids}"
                )
            compile_analysis(plan, catalog, data_through=date(2025, 12, 31))
        except Exception as error:  # noqa: BLE001 - aggregate corpus diagnostics
            failures.append(f"{case.id}: {type(error).__name__}: {error}")

    assert not failures, "\n" + "\n".join(failures)
