from __future__ import annotations

from collections import Counter
from pathlib import Path

from ai_powerbi_analyst.evaluation import load_cases

PROJECT_ROOT = Path(__file__).parents[2]


def test_model_contract_corpus_has_frozen_64_case_mix() -> None:
    cases = load_cases(PROJECT_ROOT / "evals" / "cases" / "model_contract_cases.jsonl")

    assert len(cases) == 64
    assert len({case.id for case in cases}) == 64
    assert Counter(case.category for case in cases) == {
        "build_modify": 12,
        "numerical_analysis": 24,
        "explain": 8,
        "forecast_scenario": 8,
        "ambiguity": 6,
        "unsupported": 6,
    }


def test_unsupported_cases_can_never_authorize_writes() -> None:
    cases = load_cases(PROJECT_ROOT / "evals" / "cases" / "model_contract_cases.jsonl")

    assert all(not case.write_allowed for case in cases if case.category == "unsupported")
