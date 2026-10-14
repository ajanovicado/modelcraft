from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest
from fastapi.testclient import TestClient

from ai_powerbi_analyst.knowledge.library import Reference, RetrievalReceipt
from ai_powerbi_analyst.studio.api import create_app
from ai_powerbi_analyst.studio.contracts import CreateWorkspaceRequest, DashboardVisualProposal
from ai_powerbi_analyst.studio.report_capabilities import (
    minimum_visual_sizes,
    model_visual_capabilities,
    report_capabilities,
    report_chat_guidance,
    visual_types_by_recipe,
)
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


def test_registry_cannot_advertise_types_outside_the_closed_draft_schema() -> None:
    catalog = report_capabilities()
    advertised = [v["id"] for v in catalog["visuals"]]
    schema_types = get_args(DashboardVisualProposal.model_fields["visual_type"].annotation)
    assert len(advertised) == len(set(advertised)) == 13
    assert set(advertised) == set(schema_types)
    assert set(catalog["families"]) == set(
        get_args(DashboardVisualProposal.model_fields["recipe"].annotation)
    )
    for visual in catalog["visuals"]:
        assert visual["id"] in visual_types_by_recipe()[visual["recipe"]]


def test_registry_preserves_layout_and_binding_limits() -> None:
    catalog = report_capabilities()
    assert minimum_visual_sizes() == {
        "kpi": (4, 2),
        "trend": (6, 5),
        "category": (4, 5),
        "detail": (8, 6),
    }
    assert catalog["limits"] == {
        "pages": 8,
        "visuals_per_page": 12,
        "grid_columns": 12,
        "grid_rows": 12,
        "detail_fields": 8,
    }
    assert catalog["families"]["category"]["roles"]["Y"]["max"] == 1
    assert catalog["families"]["detail"]["roles"]["Values"]["kind"] == "column"


def test_model_context_differentiates_current_authoring_from_native_power_bi() -> None:
    context = model_visual_capabilities()
    assert "pivotTable" not in json.dumps(context["types_by_recipe"])
    assert "Matrix" in " ".join(context["limitations"])
    assert "approval" in context["write_policy"]
    assert len(json.dumps(context)) < 3000
    assert len(report_chat_guidance()) < 1200
    assert "not silently substitute" in report_chat_guidance()
    catalog = report_capabilities()
    catalog["visuals"].clear()
    assert len(report_capabilities()["visuals"]) == 13


def test_capability_endpoint_is_authenticated_read_only_and_has_no_discovery_side_effects(
    tmp_path: Path,
) -> None:
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    token = "isolated-report-capability-test"
    with TestClient(create_app(service, token)) as client:
        assert client.get("/v1/report-capabilities").status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        response = client.get("/v1/report-capabilities", headers=headers)
        assert response.status_code == 200
        assert response.json() == report_capabilities()
        assert response.headers["Cache-Control"] == "no-store"
        assert client.post("/v1/report-capabilities", headers=headers).status_code == 405
        assert service.list_workspaces() == []


@pytest.mark.parametrize("followup", [False, True])
def test_report_chat_keeps_required_context_and_cites_only_references_that_fit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    followup: bool,
) -> None:
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Context budget fixture",
            selected_folder=str(tmp_path),
            expected_base_hash=EMPTY_HASH,
            idempotency_key="context-budget-fixture",
        )
    )
    receipt = RetrievalReceipt(
        captured_at=datetime.now(UTC),
        corpus_hash="a" * 64,
        query_hash="b" * 64,
        mode="docs",
        context_characters=1600,
        references=[
            Reference(
                id=f"report-ref-{i}",
                title=f"Reference number {i}",
                kind="documentation",
                source_url=f"https://learn.microsoft.com/en-us/power-bi/visuals/example-{i}",
                revision="test",
                text="Reference sentence. " * 30,
                sha256="c" * 64,
                verification="source_snapshot",
            )
            for i in range(3)
        ],
    )
    monkeypatch.setattr(service.knowledge, "retrieve", lambda *_args, **_kwargs: receipt)
    monkeypatch.setattr(service, "_chat_context", lambda *_args, **_kwargs: "Reviewed field. " * 62)
    captured = {}

    class ChatModel:
        automatic_model_selection = True
        settings = SimpleNamespace(chat_output_tokens=512)

        def stream_chat(self, message, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(message=message, **kwargs)
            yield "Test discussion only."

    service.model = ChatModel()
    request = "Which report charts can I build? Keep this exact request."
    selected = None
    if followup:
        request = "i want you to make the decision"
        selected = (
            request + "\nSelected visual: Employees with Positive Change %"
            "\nDAX: DIVIDE([Positive], [Total])"
        )
        for content in ["界" * 330, "old answer", "change this visual", "Which type?"]:
            service.store.add_message(
                workspace.id,
                "assistant" if content in {"old answer", "Which type?"} else "user",
                content,
            )
    response = "".join(service.chat(workspace.id, request, selected_context=selected))
    assert captured["message"] == (selected or request)
    system = str(captured["system_prompt"])
    assert "Reviewed field. " * 62 in system
    if not followup:
        assert report_chat_guidance() in system
    history = captured["history"]
    total = len(system.encode()) + len((selected or request).encode())
    total += sum(len(item["content"].encode()) for item in history)
    assert total + 512 + max(128, 64 * (len(history) + 2)) <= 4096
    if followup:
        assert history[-2:] == [
            {"role": "user", "content": "change this visual"},
            {"role": "assistant", "content": "Which type?"},
        ]
        assert history[0]["role"] == "user"
        assert len(service.messages(workspace.id)) == 6
    included = [r for r in receipt.references if r.id in system]
    assert len(included) < len(receipt.references)
    for reference in receipt.references:
        assert (reference.title in response) == (reference in included)


def test_token_counted_backend_keeps_retrieved_references_and_dax_naming_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Token budget fixture",
            selected_folder=str(tmp_path),
            expected_base_hash=EMPTY_HASH,
            idempotency_key="token-budget-fixture",
        )
    )
    receipt = RetrievalReceipt(
        captured_at=datetime.now(UTC),
        corpus_hash="a" * 64,
        query_hash="b" * 64,
        mode="docs",
        context_characters=1600,
        references=[
            Reference(
                id=f"ratio-ref-{i}",
                title=f"Ratio reference {i}",
                kind="documentation",
                source_url=f"https://learn.microsoft.com/en-us/dax/example-{i}",
                revision="test",
                text="DIVIDE keeps ratios correct in the current filter context. " * 8,
                sha256="c" * 64,
                verification="source_snapshot",
            )
            for i in range(3)
        ],
    )
    monkeypatch.setattr(service.knowledge, "retrieve", lambda *_args, **_kwargs: receipt)
    monkeypatch.setattr(service, "_chat_context", lambda *_args, **_kwargs: "Reviewed field. " * 62)
    captured: dict[str, object] = {}

    class TokenCountedModel:
        automatic_model_selection = True
        settings = SimpleNamespace(chat_output_tokens=512)

        def prompt_bytes_per_token(self) -> float:
            return 3.0

        def stream_chat(self, message, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(message=message, **kwargs)
            yield "Use DIVIDE."

    service.model = TokenCountedModel()
    "".join(service.chat(workspace.id, "How do I write a profit margin % measure in DAX?"))

    system = str(captured["system_prompt"])
    # The byte-bounded engine would have dropped these; a token-counted one fits them.
    assert all(reference.id in system for reference in receipt.references)
    assert "'Table name'[Column name]" in system and "never field IDs" in system
    assert "confirmed compatible numerator and denominator aggregations" in system
    assert "evaluated in the current filter context" in system
    assert "Use ALL or REMOVEFILTERS only" in system
    assert "share of total or a fixed grain" in system
    assert "without CALCULATE or ALL" not in system
    assert captured["worked_examples"] is False


def test_chat_time_limit_keeps_the_partial_answer_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_powerbi_analyst.llm.runtime import LocalModelError

    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Chat time limit",
            selected_folder=str(tmp_path),
            expected_base_hash=EMPTY_HASH,
            idempotency_key="chat-time-limit",
        )
    )
    monkeypatch.setattr(service, "_chat_context", lambda *_args, **_kwargs: "Reviewed field.")
    produced: list[str] = []

    class SlowModel:
        automatic_model_selection = True
        settings = SimpleNamespace(chat_output_tokens=512)

        def stream_chat(self, message, **kwargs):  # type: ignore[no-untyped-def]
            yield from produced
            raise LocalModelError("Local model response did not finish: timeout")

    service.model = SlowModel()
    produced.append("Sales relates to Customers through CustomerKey.")
    answer = "".join(service.chat(workspace.id, "How are these tables related?"))
    assert answer.startswith("Sales relates to Customers")
    assert "time limit" in answer
    assert service.messages(workspace.id)[-1]["content"] == answer
    # Nothing written yet: still a failure the user must retry.
    produced.clear()
    with pytest.raises(LocalModelError):
        "".join(service.chat(workspace.id, "How are these tables related?"))
