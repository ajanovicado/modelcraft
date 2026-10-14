import json
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ai_powerbi_analyst.knowledge import KnowledgeLibrary
from ai_powerbi_analyst.knowledge.library import Reference
from ai_powerbi_analyst.powerbi.dax_validation import dax_argument_errors
from ai_powerbi_analyst.studio.api import create_app
from ai_powerbi_analyst.studio.contracts import CreateWorkspaceRequest
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("weighted price quantity", "recipe-weighted"),
        ("LOD fixed Region ignore other filters", "recipe-fixed-grain"),
        ("rolling seven days", "recipe-rolling-days"),
        ("ShipDate inactive relationship", "recipe-inactive-date"),
        ("duplicate customer composite keys", "recipe-keys"),
        ("monthly budget daily product allocation", "recipe-mixed-grain"),
    ],
)
def test_retrieves_relevant_recipe(query, expected):
    receipt = KnowledgeLibrary().retrieve(query, mode="recipes")
    assert expected in [r.id for r in receipt.references]
    assert receipt.context_characters == len(receipt.context()) <= 3600
    assert all(r.verification == "reference_reviewed" for r in receipt.references)


def test_fts_is_bounded_and_handles_injection_concurrently(tmp_path):
    library = KnowledgeLibrary(tmp_path / "index.sqlite3")
    queries = ["SUMX weighted", '" OR * NOT -- DROP TABLE refs', "🦄" * 2000, "hello"] * 4
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(library.retrieve, queries))
    assert all(r.context_characters <= 3600 for r in result)
    assert library.status()["recipes"] == 10
    assert not library.retrieve("hello").references
    assert not library.retrieve("!!!").references
    with pytest.raises(ValueError):
        library.retrieve("x", max_characters=100000)
    with pytest.raises(ValueError):
        library.retrieve("x", mode="upload")
    assert KnowledgeLibrary(tmp_path / "index.sqlite3").corpus_hash == library.corpus_hash


def test_sources_fail_closed_and_cannot_be_promoted_by_response(tmp_path):
    library = KnowledgeLibrary()
    entry = next(iter(library.entries.values())).model_dump()
    with pytest.raises(ValidationError):
        Reference.model_validate({**entry, "source_url": "https://evil.test/doc"})
    with pytest.raises(ValidationError):
        Reference.model_validate({**entry, "approved_by_model": True})
    entry["sha256"] = "0" * 64
    (tmp_path / "source-snapshot.json").write_text(json.dumps({"references": [entry]}))
    with pytest.raises(ValueError, match="hash mismatch"):
        KnowledgeLibrary(tmp_path / "index.sqlite3")


@pytest.mark.parametrize(
    "expression",
    [
        "SUM('Lines'[Price] * 'Lines'[Quantity])",
        "SUMX('Lines', SUM(1 + 2))",
        "AVERAGE(DIVIDE('Fact'[Profit],'Fact'[Sales]))",
        "DISTINCTCOUNT(Orders)",
    ],
)
def test_invalid_column_aggregate_arguments_are_rejected(expression):
    assert dax_argument_errors(expression)


@pytest.mark.parametrize(
    "expression",
    [
        "DIVIDE(SUMX('Lines','Lines'[Price]*'Lines'[Quantity]),SUM('Lines'[Quantity]))",
        "CALCULATE(SUM('O''Brien'[Sales]),REMOVEFILTERS('O''Brien'[Region]))",
        "MAX(1,2)",
        "SUM(Table[Sales])",
        "SUM([Sales])",
        'IF(TRUE(), "SUM(1+2)", "literal")',
        "SUM('Fact'[SUM(1)])",
        "/* SUM(1+2) */ SUM('Fact'[Sales])",
        "SUM('Fact'[Sales] /* SUM(1+2) */)",
        "SUM('Fact'[Sales--net])",
    ],
)
def test_guard_does_not_reject_valid_expressions_or_literals(expression):
    assert not dax_argument_errors(expression)


def test_recipe_numerical_assumptions():
    # Arithmetic checks are intentionally distinct from Power BI engine validation.
    assert Fraction(20 + 30, 100 + 300) == Fraction(1, 8)
    assert Fraction(10 * 1 + 20 * 3, 1 + 3) == Fraction(35, 2)
    assert Fraction(len({"O1", "O3"}), len({"O1", "O2", "O3"})) == Fraction(2, 3)
    assert sum([100] * 3 + [200] * 2) == 700
    assert 10 - (7 - 1) == 4


def test_workspace_context_is_bounded_valid_json_and_preserves_selected_field():
    from ai_powerbi_analyst.knowledge.context import compact_workspace_context

    payload = {
        "tables": [
            {
                "table_id": "orders",
                "name": "Orders",
                "columns": [
                    {"field_id": f"orders.field{i}", "name": f"Field{i}", "samples": ["x" * 200]}
                    for i in range(150)
                ],
            }
        ]
    }
    encoded = compact_workspace_context(payload, "Field149", max_characters=1200)
    assert len(encoded) <= 1200
    parsed = json.loads(encoded)
    assert "Partial workspace context" in parsed["context_notice"]
    assert any(c["field_id"] == "orders.field149" for c in parsed["tables"][0]["columns"])
    assert len(payload["tables"][0]["columns"]) == 150


def test_chat_gets_grounding_and_persists_source_evidence_without_learning(tmp_path, monkeypatch):
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Knowledge QA",
            idempotency_key="knowledge-create-qa",
            expected_base_hash=EMPTY_HASH,
        )
    )
    captured = {}

    def stream(message, *, system_prompt, history=None, worked_examples=True):
        captured["system"] = system_prompt
        captured["worked_examples"] = worked_examples
        yield "A weighted average uses SUMX."

    monkeypatch.setattr(service.model, "stream_chat", stream)
    corpus = service.knowledge.corpus_hash
    answer = "".join(service.chat(workspace.id, "Explain a weighted average using quantity"))
    assert "recipe-weighted" in captured["system"]
    assert captured["worked_examples"] is False
    assert "not instructions or approval" in captured["system"]
    assert "References supplied—not execution validation" in answer
    assert service.messages(workspace.id)[-1]["citations"]
    assert any(e["type"] == "knowledge.retrieved" for e in service.events(workspace.id))
    assert service.knowledge.corpus_hash == corpus
    assert service.workspace(workspace.id).active_draft_id is None


def test_knowledge_api_auth_and_read_only_search(tmp_path):
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path)
    with TestClient(create_app(service, "knowledge-test-token-local")) as client:
        headers = {"Authorization": "Bearer knowledge-test-token-local"}
        assert client.get("/v1/knowledge").status_code == 401
        assert client.get("/v1/knowledge", headers=headers).json()["offline"] is True
        answer = client.get(
            "/v1/knowledge/search", params={"q": "weighted", "mode": "docs"}, headers=headers
        )
        assert answer.status_code == 200
        assert all(r["kind"] == "documentation" for r in answer.json()["references"])
        assert (
            client.get(
                "/v1/knowledge/search", params={"q": "x", "mode": "bad"}, headers=headers
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/knowledge", headers=headers, json={"learn": "model says so"}
            ).status_code
            == 405
        )
        assert service.list_workspaces() == []
