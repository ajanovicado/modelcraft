"""Content-addressed numerical goldens and independently derived exact-answer scores.

The golden file freezes the complete governed plan and ``EvidenceAnswer`` for
the 24 numerical-analysis and eight forecast/scenario cases in the 64-case
model corpus.  Evaluation never accepts a model/evaluator supplied summary:
every score is recomputed from exact plans, rows, display text, charts, and
provenance produced from the frozen local fixture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from time import perf_counter
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from ai_powerbi_analyst.analytics import AnalyticsService
from ai_powerbi_analyst.contracts import (
    AnalysisOperation,
    AnalysisPlan,
    ClosedModel,
    EvidenceAnswer,
    Sha256,
)
from ai_powerbi_analyst.data import load_semantic_catalog
from ai_powerbi_analyst.evaluation import EvaluationCase, load_cases
from ai_powerbi_analyst.orchestration.analysis_planner import RuleFirstAnalysisPlanner

NumericalCategory = Literal["numerical_analysis", "forecast_scenario"]
_NUMERICAL_CATEGORIES = {"numerical_analysis", "forecast_scenario"}
_EXPECTED_CATEGORY_COUNTS = {"numerical_analysis": 24, "forecast_scenario": 8}
_REQUIRED_FIXTURE_FILES = {
    "catalog",
    "driver_parquet",
    "duckdb",
    "forecast_backtest_parquet",
    "forecast_parquet",
    "forecast_selection_parquet",
    "sales_parquet",
    "scenario_parquet",
}
_PROVENANCE_FIELDS = (
    "data_hash",
    "catalog_hash",
    "catalog_version",
    "data_through",
    "query_plan_hash",
    "metric_definition_refs",
)


class GoldenEvidenceError(ValueError):
    """The frozen corpus, fixture, or golden evidence is incomplete or inconsistent."""


class FrozenFixtureIdentity(ClosedModel):
    """Exact local inputs from which every expected answer was derived."""

    data_hash: Sha256
    catalog_hash: Sha256
    catalog_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    data_through: date
    forecast_version: str = Field(min_length=1)
    scenario_version: str = Field(min_length=1)
    manifest_sha256: Sha256
    source_hashes: dict[str, Sha256]

    @model_validator(mode="after")
    def has_the_complete_source_inventory(self) -> FrozenFixtureIdentity:
        if set(self.source_hashes) != _REQUIRED_FIXTURE_FILES:
            raise ValueError("fixture source inventory is incomplete")
        if self.source_hashes["catalog"] != self.catalog_hash:
            raise ValueError("fixture catalog identity is inconsistent")
        return self


class FrozenGoldenCase(ClosedModel):
    """One exact expected analysis result bound to a corpus case and fixture."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    case_id: str = Field(pattern=r"^(?:AN|FS)-\d{2}$")
    category: NumericalCategory
    prompt: str = Field(min_length=1)
    fixture: FrozenFixtureIdentity
    expected_plan: AnalysisPlan
    expected_plan_sha256: Sha256
    expected_answer: EvidenceAnswer
    expected_answer_sha256: Sha256

    @model_validator(mode="after")
    def content_addresses_and_provenance_match(self) -> FrozenGoldenCase:
        plan_hash = canonical_sha256(self.expected_plan.model_dump(mode="json"))
        answer_hash = canonical_sha256(self.expected_answer.model_dump(mode="json"))
        if plan_hash != self.expected_plan_sha256:
            raise ValueError("expected plan content address does not match")
        if answer_hash != self.expected_answer_sha256:
            raise ValueError("expected answer content address does not match")
        answer = self.expected_answer
        if (
            answer.data_hash != self.fixture.data_hash
            or answer.catalog_hash != self.fixture.catalog_hash
            or answer.catalog_version != self.fixture.catalog_version
            or answer.data_through != self.fixture.data_through
        ):
            raise ValueError("expected answer provenance differs from fixture identity")
        return self


class GoldenCaseOutcome(ClosedModel):
    """Measured comparison for one case; no summary field can override it."""

    case_id: str
    category: NumericalCategory
    expected_plan_sha256: Sha256
    actual_plan_sha256: Sha256 | None = None
    expected_answer_sha256: Sha256
    actual_answer_sha256: Sha256 | None = None
    plan_exact: bool
    rows_exact: bool
    provenance_exact: bool
    exact_answer: bool
    mismatches: list[str]
    latency_seconds: float = Field(ge=0)
    error: str | None = None


class GoldenEvaluationSummary(ClosedModel):
    evaluated_case_count: int
    exact_answer_count: int
    exact_answer_rate: float = Field(ge=0, le=1)
    critical_fabrications: int


class GoldenEvaluationResult(ClosedModel):
    """Repo-owned numerical result suitable for inclusion in a model-run receipt."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    kind: Literal["golden_evidence_evaluation"] = "golden_evidence_evaluation"
    status: Literal["passed", "failed"]
    evaluator_source_path: str
    evaluator_source_sha256: Sha256
    corpus_path: str
    corpus_sha256: Sha256
    golden_path: str
    golden_sha256: Sha256
    fixture: FrozenFixtureIdentity
    outcomes: list[GoldenCaseOutcome]
    summary: GoldenEvaluationSummary


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        while chunk := handle.read(1024 * 1024):
            total += len(chunk)
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    if (
        total != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise GoldenEvidenceError(f"file changed while it was hashed: {path}")
    return digest.hexdigest()


def canonical_json(value: object) -> str:
    """Serialize a JSON-compatible value for stable content addressing."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _safe_fixture_child(fixture_root: Path, relative: object, *, label: str) -> Path:
    if not isinstance(relative, str):
        raise GoldenEvidenceError(f"fixture manifest {label} path is invalid")
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts or not relative_path.parts:
        raise GoldenEvidenceError(f"fixture manifest {label} path escapes the fixture")
    candidate = fixture_root / relative_path
    if candidate.is_symlink() or not candidate.is_file():
        raise GoldenEvidenceError(f"fixture manifest {label} file is missing or linked")
    resolved_root = fixture_root.resolve(strict=True)
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(resolved_root):
        raise GoldenEvidenceError(f"fixture manifest {label} path escapes the fixture")
    return resolved


def load_fixture_identity(fixture_root: Path) -> FrozenFixtureIdentity:
    """Verify every governed fixture file and return its closed identity."""

    root = fixture_root.resolve(strict=True)
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise GoldenEvidenceError("frozen fixture manifest is missing or linked")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GoldenEvidenceError("frozen fixture manifest is invalid") from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise GoldenEvidenceError("frozen fixture manifest has no file inventory")
    files = manifest["files"]
    assert isinstance(files, dict)
    if set(files) != _REQUIRED_FIXTURE_FILES:
        raise GoldenEvidenceError("frozen fixture file inventory is incomplete")
    source_hashes: dict[str, str] = {}
    for label in sorted(_REQUIRED_FIXTURE_FILES):
        record = files.get(label)
        if not isinstance(record, dict):
            raise GoldenEvidenceError(f"fixture manifest {label} record is invalid")
        path = _safe_fixture_child(root, record.get("path"), label=label)
        recorded_hash = record.get("sha256")
        actual_hash = _file_sha256(path)
        if recorded_hash != actual_hash:
            raise GoldenEvidenceError(f"frozen fixture {label} hash does not match")
        source_hashes[label] = actual_hash
    try:
        identity = FrozenFixtureIdentity(
            data_hash=manifest["data_hash"],
            catalog_hash=manifest["catalog_hash"],
            catalog_version=manifest["version"],
            data_through=manifest["data_through"],
            forecast_version=manifest["forecast_version"],
            scenario_version=manifest["scenario_version"],
            manifest_sha256=_file_sha256(manifest_path),
            source_hashes=source_hashes,
        )
    except (KeyError, ValidationError, ValueError) as error:
        raise GoldenEvidenceError("frozen fixture identity is invalid") from error
    return identity


def _numerical_cases(corpus_path: Path) -> list[EvaluationCase]:
    cases = [case for case in load_cases(corpus_path) if case.category in _NUMERICAL_CATEGORIES]
    counts = {
        category: sum(case.category == category for case in cases)
        for category in _EXPECTED_CATEGORY_COUNTS
    }
    if len(cases) != 32 or counts != _EXPECTED_CATEGORY_COUNTS:
        raise GoldenEvidenceError("corpus must contain exactly 24 analysis and 8 forecast cases")
    if len({case.id for case in cases}) != len(cases):
        raise GoldenEvidenceError("numerical corpus case IDs must be unique")
    return cases


def _assert_plan_matches_case(case: EvaluationCase, plan: AnalysisPlan) -> None:
    if plan.operation != case.expected_operation:
        raise GoldenEvidenceError(f"{case.id} plan operation differs from the frozen corpus")
    if plan.operation is AnalysisOperation.DRIVER_BRIDGE:
        if plan.metric_ids != ["gross_profit"]:
            raise GoldenEvidenceError(f"{case.id} driver plan is not governed Gross Profit")
    elif not set(case.expected_metric_ids).issubset(plan.metric_ids):
        raise GoldenEvidenceError(f"{case.id} plan metrics differ from the frozen corpus")
    if not set(case.expected_dimension_ids).issubset(plan.dimension_ids):
        raise GoldenEvidenceError(f"{case.id} plan dimensions differ from the frozen corpus")


def _frozen_analyst(
    fixture_root: Path,
    identity: FrozenFixtureIdentity,
) -> tuple[RuleFirstAnalysisPlanner, AnalyticsService]:
    catalog = load_semantic_catalog(fixture_root / "semantic_catalog.yaml")
    if catalog.version != identity.catalog_version:
        raise GoldenEvidenceError("semantic catalog version differs from fixture identity")

    def integrity_guard() -> None:
        if load_fixture_identity(fixture_root) != identity:
            raise GoldenEvidenceError("frozen fixture changed during golden evaluation")

    analyst = AnalyticsService(
        fixture_root / "analytics.duckdb",
        catalog,
        identity.data_hash,
        identity.catalog_hash,
        identity.data_through,
        timeout_seconds=18.0,
        integrity_guard=integrity_guard,
    )
    return RuleFirstAnalysisPlanner(catalog), analyst


def _golden_payload(
    case: EvaluationCase,
    fixture: FrozenFixtureIdentity,
    plan: AnalysisPlan,
    answer: EvidenceAnswer,
) -> FrozenGoldenCase:
    plan_payload = plan.model_dump(mode="json")
    answer_payload = answer.model_dump(mode="json")
    if case.category == "numerical_analysis":
        category: NumericalCategory = "numerical_analysis"
    elif case.category == "forecast_scenario":
        category = "forecast_scenario"
    else:
        raise GoldenEvidenceError(f"{case.id} is not a numerical golden case")
    return FrozenGoldenCase(
        case_id=case.id,
        category=category,
        prompt=case.prompt,
        fixture=fixture,
        expected_plan=plan,
        expected_plan_sha256=canonical_sha256(plan_payload),
        expected_answer=answer,
        expected_answer_sha256=canonical_sha256(answer_payload),
    )


def generate_frozen_goldens(*, repository_root: Path, output_path: Path) -> list[FrozenGoldenCase]:
    """Regenerate all 32 goldens from the verified frozen fixture, atomically."""

    repository = repository_root.resolve(strict=True)
    fixture_root = repository / "fixtures/data"
    corpus_path = repository / "evals/cases/model_contract_cases.jsonl"
    fixture = load_fixture_identity(fixture_root)
    cases = _numerical_cases(corpus_path)
    planner, analyst = _frozen_analyst(fixture_root, fixture)
    goldens: list[FrozenGoldenCase] = []
    for case in cases:
        plan = planner.plan(case.prompt)
        _assert_plan_matches_case(case, plan)
        answer = analyst.answer(plan)
        goldens.append(_golden_payload(case, fixture, plan, answer))
    destination = output_path.resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = "".join(canonical_json(golden.model_dump(mode="json")) + "\n" for golden in goldens)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(lines)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return goldens


def load_frozen_goldens(
    path: Path,
    *,
    corpus_path: Path,
    fixture_root: Path,
) -> list[FrozenGoldenCase]:
    """Load and cross-check exact goldens against both corpus and fixture."""

    cases = _numerical_cases(corpus_path)
    fixture = load_fixture_identity(fixture_root)
    goldens: list[FrozenGoldenCase] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            golden = FrozenGoldenCase.model_validate_json(raw_line)
        except (ValidationError, ValueError) as error:
            raise GoldenEvidenceError(
                f"invalid frozen golden at line {line_number}: {error}"
            ) from error
        goldens.append(golden)
    if len(goldens) != 32 or len({golden.case_id for golden in goldens}) != 32:
        raise GoldenEvidenceError("golden evidence must contain 32 unique cases")
    for case, golden in zip(cases, goldens, strict=True):
        if (
            golden.case_id != case.id
            or golden.category != case.category
            or golden.prompt != case.prompt
        ):
            raise GoldenEvidenceError("golden evidence does not match corpus order and prompts")
        if golden.fixture != fixture:
            raise GoldenEvidenceError(f"{golden.case_id} fixture identity differs from frozen data")
        _assert_plan_matches_case(case, golden.expected_plan)
    return goldens


def _diff_paths(expected: object, actual: object, path: str) -> list[str]:
    if type(expected) is not type(actual):
        return [path]
    if isinstance(expected, Mapping):
        assert isinstance(actual, Mapping)
        differences: list[str] = []
        for key in sorted(set(expected) | set(actual), key=str):
            child = f"{path}.{key}"
            if key not in expected or key not in actual:
                differences.append(child)
            else:
                differences.extend(_diff_paths(expected[key], actual[key], child))
        return differences
    if isinstance(expected, list):
        assert isinstance(actual, list)
        differences = []
        for index in range(max(len(expected), len(actual))):
            child = f"{path}[{index}]"
            if index >= len(expected) or index >= len(actual):
                differences.append(child)
            else:
                differences.extend(_diff_paths(expected[index], actual[index], child))
        return differences
    return [] if expected == actual else [path]


def compare_golden_case(
    golden: FrozenGoldenCase,
    *,
    actual_plan: AnalysisPlan | None,
    actual_answer: EvidenceAnswer | None,
    latency_seconds: float,
    error: str | None = None,
) -> GoldenCaseOutcome:
    """Compare one measured result exactly and expose every differing path."""

    expected_plan = golden.expected_plan.model_dump(mode="json")
    expected_answer = golden.expected_answer.model_dump(mode="json")
    actual_plan_payload = actual_plan.model_dump(mode="json") if actual_plan else None
    actual_answer_payload = actual_answer.model_dump(mode="json") if actual_answer else None
    mismatches: list[str] = []
    if actual_plan_payload is None:
        mismatches.append("actual_plan")
    else:
        mismatches.extend(_diff_paths(expected_plan, actual_plan_payload, "expected_plan"))
    if actual_answer_payload is None:
        mismatches.append("actual_answer")
    else:
        mismatches.extend(_diff_paths(expected_answer, actual_answer_payload, "expected_answer"))
    plan_exact = actual_plan_payload == expected_plan
    rows_exact = bool(
        actual_answer_payload is not None
        and actual_answer_payload.get("rows") == expected_answer.get("rows")
    )
    provenance_exact = bool(
        actual_answer_payload is not None
        and all(
            actual_answer_payload.get(field) == expected_answer.get(field)
            for field in _PROVENANCE_FIELDS
        )
    )
    answer_exact = actual_answer_payload == expected_answer
    return GoldenCaseOutcome(
        case_id=golden.case_id,
        category=golden.category,
        expected_plan_sha256=golden.expected_plan_sha256,
        actual_plan_sha256=(
            canonical_sha256(actual_plan_payload) if actual_plan_payload is not None else None
        ),
        expected_answer_sha256=golden.expected_answer_sha256,
        actual_answer_sha256=(
            canonical_sha256(actual_answer_payload) if actual_answer_payload is not None else None
        ),
        plan_exact=plan_exact,
        rows_exact=rows_exact,
        provenance_exact=provenance_exact,
        exact_answer=plan_exact and answer_exact and provenance_exact,
        mismatches=mismatches,
        latency_seconds=max(0.0, latency_seconds),
        error=error,
    )


def summarize_golden_outcomes(
    outcomes: Sequence[GoldenCaseOutcome],
) -> GoldenEvaluationSummary:
    """Derive the numerical family solely from per-case measured outcomes."""

    if len(outcomes) != 32 or len({outcome.case_id for outcome in outcomes}) != 32:
        raise GoldenEvidenceError("exact-answer scoring requires 32 unique outcomes")
    exact = sum(outcome.exact_answer for outcome in outcomes)
    fabrications = sum(not outcome.rows_exact for outcome in outcomes)
    return GoldenEvaluationSummary(
        evaluated_case_count=32,
        exact_answer_count=exact,
        exact_answer_rate=exact / 32,
        critical_fabrications=fabrications,
    )


def evaluate_frozen_goldens(
    *,
    repository_root: Path,
    golden_path: Path | None = None,
) -> GoldenEvaluationResult:
    """Recompute all 32 answers and compare them with the frozen expected evidence."""

    repository = repository_root.resolve(strict=True)
    fixture_root = repository / "fixtures/data"
    corpus_path = repository / "evals/cases/model_contract_cases.jsonl"
    selected_golden_path = (
        golden_path.resolve(strict=True)
        if golden_path is not None
        else (repository / "evidence/golden-answers.jsonl").resolve(strict=True)
    )
    evaluator_source = Path(__file__).resolve(strict=True)
    evaluator_source_hash = _file_sha256(evaluator_source)
    corpus_hash = _file_sha256(corpus_path)
    golden_hash = _file_sha256(selected_golden_path)
    fixture = load_fixture_identity(fixture_root)
    goldens = load_frozen_goldens(
        selected_golden_path,
        corpus_path=corpus_path,
        fixture_root=fixture_root,
    )
    planner, analyst = _frozen_analyst(fixture_root, fixture)
    outcomes: list[GoldenCaseOutcome] = []
    for golden in goldens:
        started = perf_counter()
        plan: AnalysisPlan | None = None
        answer: EvidenceAnswer | None = None
        error: str | None = None
        try:
            plan = planner.plan(golden.prompt)
            answer = analyst.answer(plan)
        except Exception as caught:  # noqa: BLE001 - failure is evidence, not a crash
            error = f"{type(caught).__name__}: {caught}"
        outcomes.append(
            compare_golden_case(
                golden,
                actual_plan=plan,
                actual_answer=answer,
                latency_seconds=max(0.0, perf_counter() - started),
                error=error,
            )
        )
    summary = summarize_golden_outcomes(outcomes)
    if (
        _file_sha256(evaluator_source) != evaluator_source_hash
        or _file_sha256(corpus_path) != corpus_hash
        or _file_sha256(selected_golden_path) != golden_hash
        or load_fixture_identity(fixture_root) != fixture
    ):
        raise GoldenEvidenceError("evaluation inputs changed during the measured run")
    return GoldenEvaluationResult(
        status=(
            "passed"
            if summary.exact_answer_rate == 1.0 and summary.critical_fabrications == 0
            else "failed"
        ),
        evaluator_source_path=str(evaluator_source),
        evaluator_source_sha256=evaluator_source_hash,
        corpus_path=str(corpus_path),
        corpus_sha256=corpus_hash,
        golden_path=str(selected_golden_path),
        golden_sha256=golden_hash,
        fixture=fixture,
        outcomes=outcomes,
        summary=summary,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("generate", "validate"))
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--golden-path", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    repository = arguments.repository_root.resolve(strict=True)
    golden_path = arguments.golden_path or repository / "evidence/golden-answers.jsonl"
    if arguments.command == "generate":
        goldens = generate_frozen_goldens(
            repository_root=repository,
            output_path=golden_path,
        )
        payload: object = {
            "status": "generated",
            "case_count": len(goldens),
            "golden_path": str(golden_path.resolve()),
            "golden_sha256": _file_sha256(golden_path.resolve()),
        }
        return_code = 0
    else:
        result = evaluate_frozen_goldens(
            repository_root=repository,
            golden_path=golden_path,
        )
        payload = result.model_dump(mode="json")
        if arguments.output is not None:
            output = arguments.output.resolve(strict=False)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return_code = 0 if result.status == "passed" else 3
    print(json.dumps(payload, indent=2, sort_keys=True))
    return return_code


if __name__ == "__main__":  # pragma: no cover - exercised through the public functions
    raise SystemExit(main())
