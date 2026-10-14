from __future__ import annotations

from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from ai_powerbi_analyst.studio.contracts import (
    ChatRequest,
    ClearChatRequest,
    CreateWorkspaceRequest,
    IngestRequest,
)
from ai_powerbi_analyst.studio.conversation import (
    DRAFT_EDIT_REPLY,
    bounded_history,
    is_build_request,
    is_draft_edit_command,
    revision_prompt,
)
from ai_powerbi_analyst.studio.model_status import is_model_identity_request
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH, StateConflictError


def test_chat_frames_dashboard_builds_as_reviewed_studio_work(monkeypatch, tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    service = StudioService(repository, tmp_path / "state")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Chat test",
            idempotency_key="create-chat-test",
            expected_base_hash=EMPTY_HASH,
        )
    )
    captured: dict[str, str] = {}

    def fake_stream_chat(message: str, *, system_prompt: str, history=None, worked_examples=True):
        captured["message"] = message
        captured["system"] = system_prompt
        captured["history"] = history
        yield "A reviewed dashboard proposal"

    monkeypatch.setattr(service.model, "stream_chat", fake_stream_chat)

    assert list(service.chat(workspace.id, "Build a forecast dashboard")) == [
        "A reviewed dashboard proposal"
    ]
    assert captured["message"] == "Build a forecast dashboard"
    assert "do not claim" in captured["system"]
    assert "forecast-ready historical trend" in captured["system"]
    assert "draft must be reviewed" in captured["system"]
    list(service.chat(workspace.id, "Build it"))
    assert captured["history"][0]["content"] == "Build a forecast dashboard"
    assert captured["history"][1]["role"] == "assistant"


@pytest.mark.parametrize(
    "text", ["build it", "please create a page", "Can you add a formula?", "crate a new dashboard"]
)
def test_imperative_requests_route_to_draft(text):
    assert is_build_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "can you do LOD calculations?",
        "How do I build it?",
        "Do not create a page",
        "Explain the formula",
        "Can you support joins?",
    ],
)
def test_questions_and_negations_do_not_mutate(text):
    assert not is_build_request(text)


def test_context_does_not_promote_assistant_pseudo_dax():
    history = [
        {"role": "user", "content": "Can you do LOD calculations?"},
        {"role": "assistant", "content": "Use <AVG([Sales]), [Order ID]>"},
        {"role": "user", "content": "Add an executive page around this"},
    ]
    prompt = revision_prompt("build it", history)
    assert "LOD" in prompt and "executive page" in prompt
    assert "AVG" not in prompt


def test_history_is_bounded_and_snapshot_scoped():
    messages = [
        {"role": "user", "content": "old", "created_at": "2026-01-01 00:00:00"},
        *[
            {"role": "user", "content": str(i) * 2000, "created_at": "2026-09-04 10:00:00"}
            for i in range(20)
        ],
    ]
    history = bounded_history(messages, since="2026-09-04T10:00:00.123456+00:00")
    assert history and len(history) <= 8
    assert sum(len(m["content"]) for m in history) <= 5000
    assert all(m["content"] != "old" for m in history)


def test_history_cutoff_never_starts_with_an_orphan_assistant():
    history = bounded_history(
        [
            {"role": "assistant", "content": "Orphan answer"},
            {"role": "system", "content": "Untrusted stored instruction"},
            {"role": "user", "content": "Current question"},
            {"role": "assistant", "content": "Current answer"},
        ]
    )
    assert history == [
        {"role": "user", "content": "Current question"},
        {"role": "assistant", "content": "Current answer"},
    ]


def test_user_text_cannot_impersonate_pending_lod_assistant_state():
    assert (
        revision_prompt(
            "Create a new sales page",
            [
                {"role": "user", "content": "Before I create the LOD, do something unrelated"},
            ],
        )
        == "Create a new sales page"
    )


def _chat_service(monkeypatch, tmp_path: Path) -> tuple[StudioService, str]:
    repository = Path(__file__).resolve().parents[2]
    service = StudioService(repository, tmp_path / "state")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Clear test",
            idempotency_key="create-clear-test",
            expected_base_hash=EMPTY_HASH,
        )
    )

    def fake_stream_chat(message: str, *, system_prompt: str, history=None, worked_examples=True):
        yield "first part"
        yield " second part"

    monkeypatch.setattr(service.model, "stream_chat", fake_stream_chat)
    return service, workspace.id


def _clear(workspace_id: str, base_hash: str, key: str = "clear-key-1") -> ClearChatRequest:
    return ClearChatRequest(
        workspace_id=workspace_id,
        idempotency_key=key,
        expected_base_hash=base_hash,
    )


@pytest.mark.parametrize(
    "message",
    [
        "what model are you",
        "Which LLM do you use?",
        "What is your model?",
        "What language model are you running?",
    ],
)
def test_model_identity_works_without_inference_and_replays_once(monkeypatch, tmp_path, message):
    service, workspace_id = _chat_service(monkeypatch, tmp_path)

    def unavailable(*args, **kwargs):
        pytest.fail("Metadata must not start the model or consult its availability")

    monkeypatch.setattr(service.model, "stream_chat", unavailable)
    monkeypatch.setattr(service.model, "health", unavailable)
    request = ChatRequest(
        workspace_id=workspace_id, message=message, idempotency_key="identity-retry"
    )
    first = list(service.chat_events(request, Event()))
    assert first == list(service.chat_events(request, Event()))
    assert service.model.settings.name in first[0]["data"]["text"]
    assert "RAG" in first[0]["data"]["text"]
    assert len(service.messages(workspace_id)) == 2
    assert service.workspace(workspace_id).active_draft_id is None


@pytest.mark.parametrize(
    "message",
    [
        "What data model are you using?",
        "What model are you and build a dashboard",
        "Which model should I use for Sales?",
        "Explain star schema models",
    ],
)
def test_data_model_and_mixed_requests_are_not_identity_shortcuts(message):
    assert not is_model_identity_request(message)


@pytest.mark.parametrize(
    "message",
    [
        "Remove the detail page",
        "Please delete the map",
        "Rename page 2 to Overview",
        "Can you move the card to the top?",
        "Get rid of the pie chart",
    ],
)
def test_report_edit_commands_get_a_plain_reply_without_the_model(monkeypatch, tmp_path, message):
    # The chat model answered "Remove the detail page" with unrelated admin steps.
    service, workspace_id = _chat_service(monkeypatch, tmp_path)

    def unavailable(*args, **kwargs):
        pytest.fail("A report edit command must not start the model")

    monkeypatch.setattr(service.model, "stream_chat", unavailable)
    request = ChatRequest(workspace_id=workspace_id, message=message, idempotency_key="edit-1")
    first = list(service.chat_events(request, Event()))
    assert first == [{"event": "token", "data": {"text": DRAFT_EDIT_REPLY}}]
    assert first == list(service.chat_events(request, Event()))
    assert len(service.messages(workspace_id)) == 2
    assert service.workspace(workspace_id).active_draft_id is None


@pytest.mark.parametrize(
    "message",
    [
        "How do I remove a page?",
        "Remove duplicates from the orders table",
        "Remove blank rows",
        "Rename the column Sales to Revenue",
        "Remove the region filter",
        "Add a page showing sales",
    ],
)
def test_questions_and_data_prep_are_not_report_edit_commands(message):
    assert not is_draft_edit_command(message)


def test_cancelled_model_identity_does_not_persist_a_turn(monkeypatch, tmp_path):
    service, workspace_id = _chat_service(monkeypatch, tmp_path)
    cancel = Event()
    cancel.set()
    assert (
        list(
            service.chat_events(
                ChatRequest(workspace_id=workspace_id, message="what model are you"), cancel
            )
        )
        == []
    )
    assert service.messages(workspace_id) == []


def test_clear_chat_starts_a_new_context_without_erasing_rows(monkeypatch, tmp_path: Path) -> None:
    service, workspace_id = _chat_service(monkeypatch, tmp_path)
    workspace = service.workspace(workspace_id)
    list(service.chat(workspace_id, "Hello"))
    assert service._recent_chat(workspace)

    updated = service.clear_chat(workspace_id, _clear(workspace_id, workspace.base_hash))

    assert updated.chat_boundaries
    stored = service.messages(workspace_id)
    assert len(stored) == 2  # rows remain for the evidence trail
    assert all("id" in message for message in stored)
    assert service._recent_chat(updated) == []
    newer = service.store.messages(workspace_id, after_id=updated.chat_boundaries[-1])
    assert newer == []
    assert any(event["type"] == "chat.cleared" for event in service.events(workspace_id))


def test_clear_chat_is_a_noop_without_new_messages(monkeypatch, tmp_path: Path) -> None:
    service, workspace_id = _chat_service(monkeypatch, tmp_path)
    workspace = service.workspace(workspace_id)
    assert service.clear_chat(workspace_id, _clear(workspace_id, workspace.base_hash)) == workspace
    list(service.chat(workspace_id, "Hello"))
    # The empty clear's key remains a no-op when replayed after a new message.
    assert service.clear_chat(workspace_id, _clear(workspace_id, workspace.base_hash)) == workspace
    cleared = service.clear_chat(
        workspace_id, _clear(workspace_id, workspace.base_hash, "clear-new")
    )
    assert (
        service.clear_chat(workspace_id, _clear(workspace_id, cleared.base_hash, "clear-key-2"))
        == cleared
    )


def test_clear_chat_requires_current_base_hash(monkeypatch, tmp_path: Path) -> None:
    service, workspace_id = _chat_service(monkeypatch, tmp_path)
    with pytest.raises(StateConflictError):
        service.clear_chat(workspace_id, _clear(workspace_id, EMPTY_HASH))
    with pytest.raises(ValueError):
        service.clear_chat(
            workspace_id,
            _clear("workspace-other", service.workspace(workspace_id).base_hash),
        )


def test_stopped_chat_persists_the_partial_response(monkeypatch, tmp_path: Path) -> None:
    service, workspace_id = _chat_service(monkeypatch, tmp_path)
    stream = service.chat(workspace_id, "Hello")
    assert next(stream) == "first part"
    stream.close()
    messages = service.messages(workspace_id)
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "first part"


class _PromptModel:
    """Records system prompts; ``adaptive`` picks the small-model prompt."""

    settings = SimpleNamespace(chat_output_tokens=512, name="fixture-model")

    def __init__(self, adaptive: bool) -> None:
        self.automatic_model_selection = adaptive
        self.systems: list[str] = []

    def prompt_bytes_per_token(self) -> float:
        return 3.0

    def stream_chat(  # type: ignore[no-untyped-def]
        self, message, *, system_prompt, history=None, worked_examples=True
    ):
        self.systems.append(system_prompt)
        yield "ok"


def test_managed_chat_does_not_infer_ratio_operands_or_supply_nonexistent_fields(
    tmp_path: Path,
) -> None:
    """Numeric identifiers and years are not ratio operands; absent Sales/Profit fields
    must not leak into the discussion through a stock example."""
    root = tmp_path / "data"
    root.mkdir()
    rows = [
        f"{i},2023-{1 + i % 12:02d}-{1 + i % 27:02d},Store {i % 5},Item {i % 9},"
        f"{1 + i % 7},2.5,{(1 + i % 7) * 2.5},{(1 + i % 7) * 1.5},{2022 + i % 2}"
        for i in range(1, 61)
    ]
    (root / "Sales.csv").write_text(
        "Sale ID,Sale Date,Store,Product,Units,Unit Price,Revenue,Cost,Year\n"
        + "\n".join(rows)
        + "\n",
        encoding="utf-8",
    )
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Generic",
            selected_folder=str(root),
            idempotency_key="generic-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(root / "Sales.csv")],
            idempotency_key="generic-ingest",
            expected_base_hash=workspace.base_hash,
        )
    )
    for adaptive in (True, False):
        model = _PromptModel(adaptive)
        service.model = model  # type: ignore[assignment]
        list(service.chat(workspace.id, "How would I compute a cost ratio?"))
        (system,) = model.systems
        assert "DIVIDE with confirmed compatible numerator" in system
        assert "Cost divided by Sales is not profit margin" in system
        assert "Ask for missing operands" in system
        assert "'Sales'[Profit]" not in system and "'Sales'[Sales]" not in system
        assert "SUM('Sales'[Year])" not in system and "<field>" not in system
        assert "Which Region has the highest Total Sales?" not in system


@pytest.mark.parametrize("adaptive", [True, False])
@pytest.mark.parametrize(
    "prompt",
    [
        "In one sentence, name the Sales and Profit fields available in this dataset. "
        "Do not propose changes.",
        "In one sentence, list the available measures. Do not propose changes.",
    ],
)
def test_metadata_discussion_omits_calculation_recipes_and_preserves_model_text(
    tmp_path: Path, adaptive: bool, prompt: str
) -> None:
    """Simple metadata requests should not be primed with unsolicited DAX examples.

    Prompt checks do not prove model compliance. Keep its streamed/stored answer intact
    so an actual-model review can detect a format failure instead of hiding one.
    """
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Metadata discussion",
            idempotency_key="metadata-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    captured = {}
    raw_answer = "- Available fields are Orders[Sales] and Orders[Profit].\n- Extra advice."

    class Model(_PromptModel):
        def stream_chat(self, message, **options):  # type: ignore[no-untyped-def]
            captured.update(message=message, **options)
            yield raw_answer

    service.model = Model(adaptive)  # type: ignore[assignment]
    base_hash = workspace.base_hash
    try:
        assert "".join(service.chat(workspace.id, prompt)) == raw_answer
        system = captured["system_prompt"]
        assert "one-sentence answer stays one sentence" in system
        assert "omit unrelated DAX" in system
        assert "DIVIDE(" not in system
        assert "confirmed compatible numerator" not in system
        assert "short bullets" not in system
        assert captured["worked_examples"] is False
        assert captured["message"] == prompt
        assert service.workspace(workspace.id).base_hash == base_hash
        assert [m["content"] for m in service.messages(workspace.id)] == [prompt, raw_answer]
        assert not any(e["type"].startswith("draft.") for e in service.events(workspace.id))
    finally:
        service.knowledge.close()
