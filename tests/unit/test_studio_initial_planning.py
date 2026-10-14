from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ai_powerbi_analyst.llm.runtime import ModelHealth
from ai_powerbi_analyst.studio.contracts import (
    CreateWorkspaceRequest,
    DatasetNarrativePlan,
    DraftRequest,
    IngestRequest,
)
from ai_powerbi_analyst.studio.planner import StudioPlanningService, compile_powerbi_plan
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


class _NarrativeModel:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []
        self.health_calls = 0

    def health(self) -> ModelHealth:
        self.health_calls += 1
        return ModelHealth(
            service_available=True,
            model_available=True,
            endpoint="http://127.0.0.1:11434",
            available_memory_gb=8,
        )

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[Any],
        *,
        max_output_tokens: int | None = None,
        timeout_seconds: int | None = None,
        repair_attempts: int | None = None,
    ) -> Any:
        payload = json.loads(user_prompt)
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "payload": payload,
                "response_model": response_model,
                "max_output_tokens": max_output_tokens,
                "timeout_seconds": timeout_seconds,
                "repair_attempts": repair_attempts,
            }
        )
        if self.fail:
            raise TimeoutError("bounded local narrative timed out")
        assert response_model is DatasetNarrativePlan
        # The compact profile lists field names; tables keep IDs for citations.
        first_id = payload["profile"]["tables"][0]["id"]
        return DatasetNarrativePlan(
            summary=f"The reviewed profile includes [{first_id}] and remains local.",
            suggested_questions=[f"How does [{first_id}] vary across reviewed records?"],
            caveats=["Confirm business meaning before approving report changes."],
        )


def _analyze(
    tmp_path: Path,
    model: _NarrativeModel,
) -> tuple[StudioService, object, object, DraftRequest]:
    repository = Path(__file__).resolve().parents[2]
    service = StudioService(repository, tmp_path / "state")
    service.planner = StudioPlanningService(model, service.aggregates)  # type: ignore[arg-type]
    source_root = tmp_path / "selected"
    source_root.mkdir()
    source = source_root / "orders.csv"
    source.write_text(
        "Order Date,Region,Sales,Profit\n"
        "2025-01-01,East,100.50,20.25\n"
        "2025-02-01,West,250.00,40.00\n",
        encoding="utf-8",
    )
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Initial planning",
            selected_folder=str(source_root),
            idempotency_key="create-initial-planning-workspace",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="ingest-initial-planning-source",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    request = DraftRequest(
        workspace_id=workspace.id,
        snapshot_id=snapshot.id,
        prompt="Build an executive sales dashboard",
        idempotency_key="analyze-initial-planning-source",
        expected_base_hash=workspace.base_hash,
    )
    draft = service.create_draft(request)
    return service, snapshot, draft, request


def test_initial_planning_uses_one_bounded_narrative_call_and_records_provenance(
    tmp_path: Path,
) -> None:
    model = _NarrativeModel()
    service, snapshot, draft, request = _analyze(tmp_path, model)

    assert model.health_calls == 1
    assert len(model.calls) == 1
    call = model.calls[0]
    assert call["response_model"] is DatasetNarrativePlan
    assert call["max_output_tokens"] == 260
    assert call["timeout_seconds"] == 75
    assert call["repair_attempts"] == 0
    assert "samples" not in json.dumps(call["payload"])
    assert call["payload"]["profile"]["tables"][0]["fields"][0] == "Order Date (date)"
    assert draft.planning_provenance.narrative == "local_model"
    # Validated table IDs are shown to people by name.
    assert draft.understanding.summary == "The reviewed profile includes orders and remains local."
    assert draft.planning_provenance.semantic_model == "deterministic_compiler"
    assert draft.planning_provenance.measures == "deterministic_compiler"
    assert draft.planning_provenance.pages == "deterministic_compiler"
    assert draft.planning_provenance.previews == "deterministic_compiler"

    events = [
        event
        for event in service.events(draft.workspace_id)
        if event["type"] == "draft.planning_provenance"
    ]
    assert len(events) == 1
    assert events[0]["payload"]["draft_id"] == draft.id
    assert events[0]["payload"]["stages"]["narrative"] == "local_model"
    assert events[0]["payload"]["stages"]["measures"] == "deterministic_compiler"

    # A client retry replays the stored draft without paying another model call.
    assert service.create_draft(request) == draft
    assert len(model.calls) == 1
    assert (
        len(
            [
                event
                for event in service.events(draft.workspace_id)
                if event["type"] == "draft.planning_provenance"
            ]
        )
        == 1
    )

    manifest = service.ingestion.load_manifest(snapshot)
    approved = draft.model_copy(
        update={
            "status": "approved",
            "semantic_model": draft.semantic_model.model_copy(update={"approved": True}),
        }
    )
    assert compile_powerbi_plan(approved, manifest).generation_source == ("deterministic_fallback")


def test_failed_narrative_call_falls_back_with_truthful_provenance(tmp_path: Path) -> None:
    model = _NarrativeModel(fail=True)
    service, _snapshot, draft, _request = _analyze(tmp_path, model)

    assert len(model.calls) == 1
    assert draft.planning_provenance.narrative == "deterministic_compiler"
    assert draft.understanding.summary.startswith("The selection contains ")
    event = next(
        item
        for item in service.events(draft.workspace_id)
        if item["type"] == "draft.planning_provenance"
    )
    assert event["payload"]["stages"]["narrative"] == "deterministic_compiler"


def test_initial_narrative_contract_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        DatasetNarrativePlan.model_validate(
            {
                "summary": "Reviewed data",
                "suggested_questions": [],
                "caveats": [],
                "unexpected": "not allowed",
            }
        )
