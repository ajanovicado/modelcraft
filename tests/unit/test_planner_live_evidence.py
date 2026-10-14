"""The shareable live eval keeps metrics, but excludes formula bodies."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any


def test_live_evidence_hashes_formulas_without_changing_scoring(tmp_path: Path) -> None:
    path = Path(__file__).resolve().parents[2] / "evals/imported_edit/planner_live.py"
    spec = importlib.util.spec_from_file_location("live_evidence_privacy", path)
    assert spec is not None and spec.loader is not None
    live = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live)
    body = 'SUM(Orders[Sales]) + LEN("PRIVATE-FORMULA-SENTINEL")'
    report: dict[str, Any] = {
        "exact_plans": 1,
        "rows": [
            {
                "id": "1",
                "message": f"create measure Total = {body}",
                "steps": [
                    {
                        "operation": {"op": "create_measure", "expression": body},
                        "summary": f"Create Total: expression {body}.",
                    }
                ],
                "calls": [{"answer": {"expression": body}}],
            }
        ],
    }
    out = tmp_path / "live.json"
    live._write(out, report)
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert "PRIVATE-FORMULA-SENTINEL" not in out.read_text(encoding="utf-8")
    assert saved["exact_plans"] == 1 and saved["rows"][0]["id"] == "1"
    assert saved["rows"][0]["calls"][0]["answer"]["expression"] == {
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "length": len(body),
    }
    assert report["rows"][0]["steps"][0]["operation"]["expression"] == body


def test_literal_formula_does_not_redact_page_numbers() -> None:
    path = Path(__file__).resolve().parents[2] / "evals/imported_edit/planner_live.py"
    spec = importlib.util.spec_from_file_location("live_evidence_literal", path)
    assert spec is not None and spec.loader is not None
    live = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live)
    saved = live._shareable(
        {
            "rows": [
                {
                    "id": "1",
                    "steps": [
                        {
                            "operation": {"op": "create_measure", "expression": "1", "page": "1"},
                            "summary": "Create Total: expression 1.",
                        }
                    ],
                }
            ]
        }
    )
    assert saved["rows"][0]["id"] == "1"
    assert saved["rows"][0]["steps"][0]["operation"]["page"] == "1"
    assert isinstance(saved["rows"][0]["steps"][0]["operation"]["expression"], dict)
    assert isinstance(saved["rows"][0]["steps"][0]["summary"], dict)


def test_failed_draft_fragments_and_scalar_requests_stay_out_of_evidence() -> None:
    path = Path(__file__).resolve().parents[2] / "evals/imported_edit/planner_live.py"
    spec = importlib.util.spec_from_file_location("live_evidence_errors", path)
    assert spec is not None and spec.loader is not None
    live = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live)
    saved = live._shareable(
        {
            "rows": [
                {
                    "message": "create measure Answer = 42",
                    "reason": "Syntax error at PRIVATE-ERROR-FRAGMENT",
                    "notes": ["Formula had PRIVATE-ERROR-FRAGMENT"],
                    "calls": [{"error": "ValidationError: unexpected PRIVATE-ERROR-FRAGMENT"}],
                }
            ]
        }
    )
    assert "PRIVATE-ERROR-FRAGMENT" not in json.dumps(saved)
    assert saved["rows"][0]["message"]["text"] == ("create measure Answer = [formula kept locally]")
    assert "42" not in saved["rows"][0]["message"]["text"]
