"""Verify and print the complete frozen numerical golden corpus.

Regeneration is intentionally owned by ``ai_powerbi_analyst.evaluation_goldens``;
this helper is read-only so an exploratory print cannot replace the governed
32-case file with a partial sample.
"""

from __future__ import annotations

from pathlib import Path

from ai_powerbi_analyst.evaluation_goldens import canonical_json, load_frozen_goldens


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    golden_path = root / "evidence" / "golden-answers.jsonl"
    goldens = load_frozen_goldens(
        golden_path,
        corpus_path=root / "evals" / "cases" / "model_contract_cases.jsonl",
        fixture_root=root / "fixtures" / "data",
    )
    for golden in goldens:
        print(canonical_json(golden.model_dump(mode="json")))
    print(f"Verified and printed {len(goldens)} frozen cases from: {golden_path}")


if __name__ == "__main__":
    main()
