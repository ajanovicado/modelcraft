"""Raw-data chat refuses styling before model drafting or dashboard replacement."""

from __future__ import annotations

import hashlib
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pandas as pd
import pytest

from ai_powerbi_analyst.studio.contracts import (
    ChatRequest,
    CreateWorkspaceRequest,
    DraftRequest,
    IngestRequest,
)
from ai_powerbi_analyst.studio.edit_capabilities import STYLING_REASON
from ai_powerbi_analyst.studio.planner import StudioPlanningService
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


@pytest.fixture(params=["csv", "xlsx"])
def raw_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request):
    source = tmp_path / f"orders.{request.param}"
    frame = pd.DataFrame(
        {
            "Sales": [100, 200],
            "Color": ["Blue", "Green"],
            "Font family": ["A", "B"],
            "Title": ["East", "West"],
        }
    )
    if request.param == "csv":
        frame.to_csv(source, index=False)
    else:
        frame.to_excel(source, index=False)
    before_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    model = SimpleNamespace(
        generate=lambda *_args, **_kwargs: pytest.fail("Styling called the local model"),
        stream_chat=lambda *_args, **_kwargs: pytest.fail("Styling called model chat"),
        unload=lambda: None,
    )
    monkeypatch.setattr(
        "ai_powerbi_analyst.studio.service.create_model_runtime",
        lambda *_args: model,
    )
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Raw styling gate",
            selected_folder=str(tmp_path),
            idempotency_key="create-raw-style",
            expected_base_hash=EMPTY_HASH,
        )
    )
    service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="import-raw-style",
            expected_base_hash=workspace.base_hash,
        )
    )
    return service, workspace.id, source, before_hash


@pytest.mark.parametrize(
    "message",
    [
        "use a dark theme",
        "make the chart blue",
        "make the title bold",
        "use Arial everywhere",
        "show data labels",
        "hide the legend",
        "change the page background to white",
        "add a shadow to the slicer",
        "copy the chart formatting to the table",
        "translate every chart title to Spanish",
        "nudge the bars closer together",
        "make the Rank column wider",
    ],
)
def test_raw_data_chat_refuses_styling_without_model_or_draft_mutation(raw_workspace, message):
    service, workspace_id, source, source_hash = raw_workspace
    before = service.workspace(workspace_id)
    request = ChatRequest(
        workspace_id=workspace_id,
        message=message,
        idempotency_key="raw-style-refusal",
    )
    events = list(service.chat_events(request, Event()))
    assert events == [{"event": "token", "data": {"text": STYLING_REASON}}]
    assert service.workspace(workspace_id) == before
    assert service.workspace(workspace_id).active_draft_id is None
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    assert len(service.messages(workspace_id)) == 2
    assert list(service.chat_events(request, Event())) == events
    assert len(service.messages(workspace_id)) == 2


@pytest.mark.parametrize(
    "message",
    [
        "add Color to the table",
        "add Font family to the table",
        "add Title to the table",
        "what font is used in this chart?",
    ],
)
def test_data_field_names_and_read_only_questions_are_not_style_mutations(raw_workspace, message):
    service, workspace_id, _, _ = raw_workspace
    assert service._snapshot_styling_reply(service.workspace(workspace_id), message) is None


def test_existing_raw_dashboard_and_proposals_survive_styling_request(raw_workspace):
    service, workspace_id, source, source_hash = raw_workspace
    service.planner = StudioPlanningService(None, service.aggregates)
    workspace = service.workspace(workspace_id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace_id,
            snapshot_id=workspace.active_snapshot_id,
            prompt="Sales by Color",
            idempotency_key="raw-style-dashboard",
            expected_base_hash=workspace.base_hash,
        )
    )
    before = service.workspace(workspace_id)
    draft_before = service.draft(draft.id)
    events = list(
        service.chat_events(
            ChatRequest(
                workspace_id=workspace_id,
                message="use a dark theme",
                idempotency_key="raw-existing-style",
                expected_base_hash=draft.base_hash,
            ),
            Event(),
        )
    )
    assert events == [{"event": "token", "data": {"text": STYLING_REASON}}]
    assert service.workspace(workspace_id) == before
    assert service.draft(draft.id) == draft_before
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash


@pytest.mark.parametrize(
    "message",
    [
        "Discuss the sales results",
        "Add a bar chart of Sales by Region",
        "Calculate Sales divided by Profit",
        "Build a dashboard from these orders",
    ],
)
def test_ordinary_requests_do_not_hydrate_styling_context(raw_workspace, monkeypatch, message):
    service, workspace_id, _, _ = raw_workspace
    workspace = service.workspace(workspace_id).model_copy(
        update={"active_draft_id": "draft-unneeded-for-styling"},
    )
    monkeypatch.setattr(service, "snapshot", lambda *_: pytest.fail("Unneeded snapshot read"))
    monkeypatch.setattr(service, "draft", lambda *_: pytest.fail("Unneeded draft read"))
    assert service._snapshot_styling_reply(workspace, message) is None
