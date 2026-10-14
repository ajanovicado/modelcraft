"""Unsupported source-row requests do not become unrelated draft measures or pages."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from ai_powerbi_analyst.studio.contracts import (
    ApproveDraftRequest,
    ChatRequest,
    CreateWorkspaceRequest,
    DraftRequest,
    IngestRequest,
)
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


@pytest.fixture
def local_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[StudioService, str, Path]]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "FactSales.csv").write_text(
        "ProductKey,DateKey,Sales,Cost,Profit\n"
        "1,2025-01-01,100,40,20\n2,2025-01-01,200,160,50\n"
        "1,2025-01-02,0,10,-5\n2,2025-01-02,50,45,3\n"
        "1,2025-01-03,20,10,8\n2,2025-01-03,80,20,24\n",
        encoding="utf-8",
    )
    (source / "DimProduct.csv").write_text(
        "ProductKey,Category\n1,Goods\n2,Services\n", encoding="utf-8"
    )
    (source / "DimDate.csv").write_text(
        "DateKey\n2025-01-01\n2025-01-02\n2025-01-03\n", encoding="utf-8"
    )
    model = SimpleNamespace(
        generate=lambda *_a, **_k: pytest.fail("Unsupported column request invoked AI"),
        stream_chat=lambda *_a, **_k: pytest.fail("Unsupported column request streamed AI"),
        unload=lambda: None,
    )
    monkeypatch.setattr("ai_powerbi_analyst.studio.service.create_model_runtime", lambda *_a: model)
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    try:
        workspace = service.create_workspace(
            CreateWorkspaceRequest(
                name="Local column boundary",
                selected_folder=str(source),
                idempotency_key="column-workspace",
                expected_base_hash=EMPTY_HASH,
            )
        )
        snapshot = service.ingest(
            IngestRequest(
                workspace_id=workspace.id,
                item_paths=[str(path) for path in source.glob("*.csv")],
                expected_base_hash=workspace.base_hash,
                idempotency_key="column-ingest",
            )
        )
        service.planner.model = None
        workspace = service.workspace(workspace.id)
        draft = service.create_draft(
            DraftRequest(
                workspace_id=workspace.id,
                snapshot_id=snapshot.id,
                prompt="Sales trends and product category comparisons",
                expected_base_hash=workspace.base_hash,
                idempotency_key="column-draft",
            )
        )
        joins = [join.id for join in draft.semantic_model.relationships if not join.blockers]
        assert joins
        service.approve_draft(
            draft.id,
            ApproveDraftRequest(
                workspace_id=workspace.id,
                approved_relationship_ids=joins,
                expected_base_hash=draft.base_hash,
                idempotency_key="column-review",
            ),
        )
        yield service, workspace.id, source
    finally:
        service.editing.close()
        service.knowledge.close()


@pytest.mark.parametrize(
    "goal",
    [
        "Add a calculated column Gross Margin to FactSales that is Sales minus Cost.",
        "Can you create a computed field Gross Margin in FactSales using Sales minus Cost?",
        "Please add a derived column Net Amount to FactSales using Sales minus Cost.",
        "Create a row-level calculation Gross Margin using Sales minus Cost.",
    ],
)
def test_local_column_creation_preserves_full_approved_draft_and_source(
    local_draft: tuple[StudioService, str, Path], goal: str
) -> None:
    service, wid, source = local_draft
    workspace = service.workspace(wid)
    assert workspace.active_draft_id and workspace.active_snapshot_id
    draft = service.draft(workspace.active_draft_id)
    snapshot = service.snapshot(workspace.active_snapshot_id)
    assert draft.status == "approved" and draft.semantic_model.approved
    before_source = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir()
    }
    before_sequence = service.events(wid)[-1]["sequence"]
    request = ChatRequest(
        workspace_id=wid,
        message=goal,
        idempotency_key="unsupported-column",
        expected_base_hash=draft.base_hash,
        chat_boundary=0,
    )

    events = list(service.chat_events(request, Event()))

    assert len(events) == 1 and events[0]["event"] == "token"
    text = events[0]["data"]["text"]
    assert "cannot create calculated columns" in text
    assert "Power Query, then reimport" in text and "measure explicitly" in text
    assert "No data, draft or Power BI report was changed" in text
    assert service.workspace(wid) == workspace
    assert service.draft(draft.id) == draft
    assert service.snapshot(snapshot.id) == snapshot
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir()
    } == before_source
    audit = service.events(wid, after=before_sequence)
    assert [item["type"] for item in audit] == ["chat.action"]
    assert audit[0]["payload"]["draft_id"] is None
    messages = service.messages(wid)
    assert messages[-2]["content"] == goal and messages[-1]["content"] == text
    assert not service.builds(wid)
    # The same recovery receipt stays a single unchanged conversation turn.
    assert list(service.chat_events(request, Event())) == events
    assert service.messages(wid) == messages


@pytest.mark.parametrize(
    "goal",
    [
        "Explain how to create a calculated column in Power Query.",
        "Do not add a calculated column; explain measures.",
        "Add a column chart of Total Sales by Category.",
        "Add a Profit Margin % measure and a Margin Analysis page.",
        "Add the existing Sales column to the table visual.",
    ],
)
def test_column_boundary_does_not_block_explanations_or_visual_measure_requests(goal: str) -> None:
    assert StudioService._snapshot_column_reply(goal) is None


def test_an_explicit_margin_measure_still_revises_the_local_draft(
    local_draft: tuple[StudioService, str, Path],
) -> None:
    service, wid, _source = local_draft
    workspace = service.workspace(wid)
    assert workspace.active_draft_id
    draft = service.draft(workspace.active_draft_id)
    events = list(
        service.chat_events(
            ChatRequest(
                workspace_id=wid,
                message="Add a Profit Margin % measure and a new Margin Analysis page.",
                idempotency_key="explicit-margin-measure",
                expected_base_hash=draft.base_hash,
                chat_boundary=0,
            ),
            Event(),
        )
    )
    assert any(event["event"] == "draft" for event in events)
    revised = service.draft(draft.id)
    assert revised.base_hash != draft.base_hash
    assert len(revised.pages) == len(draft.pages) + 1


def test_imported_column_requests_keep_the_existing_model_edit_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_imported_report_chat import _import

    service, wid = _import(tmp_path, monkeypatch)
    captured: list[str] = []
    response = {"event": "token", "data": {"text": "Imported model edit offered for review."}}

    def imported_events(request: ChatRequest, *_a: object) -> Iterator[dict[str, object]]:
        captured.append(request.message)
        return iter([response])

    monkeypatch.setattr(
        service,
        "_snapshot_column_reply",
        lambda _message: pytest.fail("Imported column edit was routed to the local-data refusal"),
    )
    monkeypatch.setattr(service, "_imported_edit_events", imported_events)
    goal = "Add a calculated column Gross Margin to FactSales using SalesAmount minus TotalCost."
    try:
        events = list(
            service.chat_events(
                ChatRequest(workspace_id=wid, message=goal, idempotency_key="imported-column-lane"),
                Event(),
            )
        )
        assert captured == [goal]
        assert events == [response]
    finally:
        service.editing.close()
        service.knowledge.close()
