from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_powerbi_analyst.evaluation_goldens import (
    GoldenEvidenceError,
    canonical_sha256,
    evaluate_frozen_goldens,
    generate_frozen_goldens,
    load_frozen_goldens,
)

PROJECT_ROOT = Path(__file__).parents[2]
CORPUS_PATH = PROJECT_ROOT / "evals/cases/model_contract_cases.jsonl"
GOLDEN_PATH = PROJECT_ROOT / "evidence/golden-answers.jsonl"


def _jsonl_rows(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_frozen_goldens_cover_all_32_numerical_cases_with_exact_provenance() -> None:
    goldens = load_frozen_goldens(
        GOLDEN_PATH,
        corpus_path=CORPUS_PATH,
        fixture_root=PROJECT_ROOT / "fixtures/data",
    )

    assert len(goldens) == 32
    assert [golden.case_id for golden in goldens[:2]] == ["AN-01", "AN-02"]
    assert [golden.case_id for golden in goldens[-2:]] == ["FS-07", "FS-08"]
    assert all(golden.expected_answer.catalog_hash for golden in goldens)
    assert all(golden.expected_answer.rows for golden in goldens)
    comparison = next(golden for golden in goldens if golden.case_id == "AN-05")
    assert comparison.expected_answer.chart.x_field == "current_value"
    assert comparison.expected_answer.chart.y_field == "metric_id"


def test_frozen_golden_evaluator_recomputes_every_exact_answer() -> None:
    result = evaluate_frozen_goldens(
        repository_root=PROJECT_ROOT,
        golden_path=GOLDEN_PATH,
    )

    assert result.status == "passed"
    assert result.summary.evaluated_case_count == 32
    assert result.summary.exact_answer_count == 32
    assert result.summary.exact_answer_rate == 1.0
    assert result.summary.critical_fabrications == 0
    assert len(result.outcomes) == 32
    assert Path(result.evaluator_source_path).is_file()
    assert len(result.evaluator_source_sha256) == 64
    assert all(outcome.exact_answer for outcome in result.outcomes)
    assert all(outcome.expected_answer_sha256 for outcome in result.outcomes)
    assert all(outcome.actual_answer_sha256 for outcome in result.outcomes)


def test_frozen_golden_evaluator_derives_failure_from_tampered_expected_row(
    tmp_path: Path,
) -> None:
    rows = _jsonl_rows(GOLDEN_PATH)
    expected_answer = rows[0]["expected_answer"]
    assert isinstance(expected_answer, dict)
    answer_rows = expected_answer["rows"]
    assert isinstance(answer_rows, list)
    first_answer_row = answer_rows[0]
    assert isinstance(first_answer_row, dict)
    first_answer_row["value"] = float(first_answer_row["value"]) + 1.0
    rows[0]["expected_answer_sha256"] = canonical_sha256(expected_answer)
    tampered = tmp_path / "tampered-goldens.jsonl"
    _write_rows(tampered, rows)

    result = evaluate_frozen_goldens(
        repository_root=PROJECT_ROOT,
        golden_path=tampered,
    )

    assert result.status == "failed"
    assert result.summary.evaluated_case_count == 32
    assert result.summary.exact_answer_count == 31
    assert result.summary.exact_answer_rate == 31 / 32
    failed = [outcome for outcome in result.outcomes if not outcome.exact_answer]
    assert [outcome.case_id for outcome in failed] == ["AN-01"]
    assert any("expected_answer.rows[0].value" in path for path in failed[0].mismatches)


def test_loader_rejects_golden_provenance_that_differs_from_frozen_fixture(
    tmp_path: Path,
) -> None:
    rows = _jsonl_rows(GOLDEN_PATH)
    fixture = rows[0]["fixture"]
    assert isinstance(fixture, dict)
    fixture["catalog_hash"] = "0" * 64
    source_hashes = fixture["source_hashes"]
    assert isinstance(source_hashes, dict)
    source_hashes["catalog"] = "0" * 64
    expected_answer = rows[0]["expected_answer"]
    assert isinstance(expected_answer, dict)
    expected_answer["catalog_hash"] = "0" * 64
    rows[0]["expected_answer_sha256"] = canonical_sha256(expected_answer)
    tampered = tmp_path / "wrong-provenance.jsonl"
    _write_rows(tampered, rows)

    with pytest.raises(GoldenEvidenceError, match="fixture identity"):
        load_frozen_goldens(
            tampered,
            corpus_path=CORPUS_PATH,
            fixture_root=PROJECT_ROOT / "fixtures/data",
        )


def test_golden_generation_is_byte_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"

    generate_frozen_goldens(repository_root=PROJECT_ROOT, output_path=first)
    generate_frozen_goldens(repository_root=PROJECT_ROOT, output_path=second)

    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes() == GOLDEN_PATH.read_bytes()
