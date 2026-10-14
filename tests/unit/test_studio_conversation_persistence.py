"""Durable history and safe retries use isolated SQLite state, never a live model."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from ai_powerbi_analyst.llm.runtime import LocalModelError
from ai_powerbi_analyst.studio.api import create_app
from ai_powerbi_analyst.studio.contracts import (
    ChatRequest,
    ClearChatRequest,
    ClearDaxChatRequest,
    CreateWorkspaceRequest,
    DatasetSnapshot,
    DaxAssistRequest,
    DeleteWorkspaceRequest,
    IngestRequest,
    RenameWorkspaceRequest,
)
from ai_powerbi_analyst.studio.conversation import is_build_request, is_developer_request
from ai_powerbi_analyst.studio.dax_assistant import dax_assistant_events
from ai_powerbi_analyst.studio.planner import StudioPlanningService
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH, StateConflictError, StudioStateStore


@pytest.fixture
def studio(tmp_path, monkeypatch):
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Persistent conversations",
            idempotency_key="create-history",
            expected_base_hash=EMPTY_HASH,
        )
    )

    def answer(message, **kwargs):
        yield "A local example."

    monkeypatch.setattr(service.model, "stream_chat", answer)
    return service, workspace


def _tokens(events):
    return "".join(event["data"]["text"] for event in events if event["event"] == "token")


def _main(workspace, key="main-1", message="Explain this concept"):
    return ChatRequest(workspace_id=workspace.id, message=message, idempotency_key=key)


def _dax(workspace, key="dax-1"):
    return DaxAssistRequest(
        workspace_id=workspace.id,
        request_id=key,
        mode="explain",
        goal="Explain SUM",
        expression="SUM('Example'[Amount])",
    )


def test_paged_history_retains_all_rows_and_does_not_cross_workspaces(studio):
    service, workspace = studio
    other = service.create_workspace(
        CreateWorkspaceRequest(
            name="Separate",
            idempotency_key="create-other",
            expected_base_hash=EMPTY_HASH,
        )
    )
    for index in range(431):
        service.store.add_message(workspace.id, "user", f"Message {index}")
        if index % 100 == 0:
            service.store.add_message(other.id, "user", "Private other workspace")
    assert len(service.messages(workspace.id)) == 100
    after = 0
    history = []
    while page := service.messages(workspace.id, after=after, limit=200):
        history.extend(page)
        after = page[-1]["id"]
    assert [item["content"] for item in history] == [f"Message {i}" for i in range(431)]
    assert len({item["id"] for item in history}) == 431
    assert len(service._recent_chat(workspace)) <= 8


def test_history_paging_api_is_authenticated_and_keeps_existing_shape(studio):
    service, workspace = studio
    service.store.add_message(workspace.id, "user", "First")
    service.store.add_message(workspace.id, "assistant", "Second")
    headers = {"Authorization": "Bearer conversation-test-token"}
    with TestClient(create_app(service, "conversation-test-token")) as client:
        url = f"/v1/workspaces/{workspace.id}/messages"
        assert client.get(url).status_code == 401
        page = client.get(url, params={"after": 0, "limit": 1}, headers=headers).json()
        assert len(page) == 1 and page[0]["content"] == "First"
        assert (
            client.get(url, params={"after": page[0]["id"]}, headers=headers).json()[0]["content"]
            == "Second"
        )
        assert client.get(url, params={"after": -1}, headers=headers).status_code == 422
        assert client.get(url, params={"limit": 201}, headers=headers).status_code == 422


def test_completed_discussion_replays_after_state_restart_without_duplicate_turns(
    studio,
    monkeypatch,
):
    service, workspace = studio
    request = _main(workspace)
    answer = _tokens(list(service.chat_events(request, Event())))
    service.store = StudioStateStore(service.store.path)

    def no_inference(*args, **kwargs):
        pytest.fail("A completed discussion must replay its durable answer")

    monkeypatch.setattr(service.model, "stream_chat", no_inference)
    assert _tokens(list(service.chat_events(request, Event()))) == answer
    assert [item["role"] for item in service.messages(workspace.id)] == ["user", "assistant"]
    with pytest.raises(StateConflictError, match="different input"):
        list(service.chat_events(request.model_copy(update={"message": "Create a page"}), Event()))


@pytest.mark.parametrize("partial", [False, True])
def test_interrupted_discussion_retry_reuses_turns_and_excludes_failed_exchange(
    studio,
    monkeypatch,
    partial,
):
    service, workspace = studio
    attempts = []

    def answer(message, *, history, **kwargs):
        attempts.append(history)
        if len(attempts) == 1:
            if partial:
                yield "Incomplete"
            raise RuntimeError("Simulated local runtime interruption")
        yield "Complete answer"

    monkeypatch.setattr(service.model, "stream_chat", answer)
    request = _main(workspace)
    with pytest.raises(RuntimeError, match="Simulated"):
        list(service.chat_events(request, Event()))
    previous_ids = [item["id"] for item in service.messages(workspace.id)]
    # An engine restart releases the reservation, retaining the exact attempt envelope.
    service.store = StudioStateStore(service.store.path)
    assert _tokens(list(service.chat_events(request, Event()))) == "Complete answer"
    rows = service.messages(workspace.id)
    assert len(rows) == 2 and rows[0]["id"] == previous_ids[0]
    if partial:
        assert rows[1]["id"] == previous_ids[1]
    assert rows[1]["content"] == "Complete answer"
    assert attempts == [[], []]
    assert _tokens(list(service.chat_events(request, Event()))) == "Complete answer"
    assert len(attempts) == 2


def test_prompt_preparation_failure_remains_retryable(studio, monkeypatch):
    service, workspace = studio
    original = service._chat_context
    attempts = []

    def context(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("Simulated schema preparation failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_chat_context", context)
    request = _main(workspace)
    with pytest.raises(RuntimeError, match="schema"):
        list(service.chat_events(request, Event()))
    assert _tokens(list(service.chat_events(request, Event())))
    assert len(service.messages(workspace.id)) == 2


@pytest.mark.parametrize("kind", ["main", "dax"])
def test_stop_releases_reservation_and_archived_attempt_cannot_join_new_chat(
    studio,
    kind,
):
    service, workspace = studio
    request = _main(workspace) if kind == "main" else _dax(workspace)
    events = (
        service.chat_events if kind == "main" else lambda r, c: dax_assistant_events(service, r, c)
    )
    stream = events(request, Event())
    while next(stream)["event"] != "token":
        pass
    with pytest.raises(StateConflictError, match="running"):
        list(events(request, Event()))
    stream.close()
    if kind == "main":
        service.clear_chat(
            workspace.id,
            ClearChatRequest(
                workspace_id=workspace.id,
                expected_base_hash=workspace.base_hash,
                idempotency_key="archive-stopped-main",
            ),
        )
    else:
        service.clear_dax_chat(
            workspace.id,
            ClearDaxChatRequest(
                workspace_id=workspace.id,
                expected_base_hash=workspace.base_hash,
                idempotency_key="archive-stopped-dax",
            ),
        )
    with pytest.raises(StateConflictError, match="archived"):
        list(events(request, Event()))


def test_empty_new_chat_receipt_cannot_archive_a_later_message_when_retried(studio):
    service, workspace = studio
    request = ClearChatRequest(
        workspace_id=workspace.id,
        expected_base_hash=workspace.base_hash,
        idempotency_key="empty-clear",
    )
    assert service.clear_chat(workspace.id, request) == workspace
    list(service.chat_events(_main(workspace), Event()))
    assert service.clear_chat(workspace.id, request).chat_boundaries == ()
    assert len(service._recent_chat(workspace)) == 2


def test_snapshot_change_archives_same_second_history_without_erasing_it(studio, tmp_path):
    service, workspace = studio
    list(service.chat_events(_main(workspace), Event()))
    source = tmp_path / "source.csv"
    source.write_text("Date,Amount\n2026-01-01,3\n", encoding="utf-8")
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="snapshot",
            expected_base_hash=workspace.base_hash,
        )
    )
    current = service.workspace(workspace.id)
    assert current.active_snapshot_id == snapshot.id
    assert current.chat_boundaries == (service.messages(workspace.id)[-1]["id"],)
    assert service._recent_chat(current) == []
    assert len(service.messages(workspace.id)) == 2


@pytest.mark.parametrize("kind", ["main", "dax"])
def test_different_request_ids_cannot_interleave_the_same_conversation(studio, kind):
    service, workspace = studio
    request = _main(workspace) if kind == "main" else _dax(workspace)
    send = (
        service.chat_events if kind == "main" else lambda r, c: dax_assistant_events(service, r, c)
    )
    first = send(request, Event())
    while next(first)["event"] != "token":
        pass
    before = (service.messages(workspace.id), service.events(workspace.id))
    second = _main(workspace, "main-other") if kind == "main" else _dax(workspace, "dax-other")
    with pytest.raises(StateConflictError, match="Wait for"):
        list(send(second, Event()))
    assert (service.messages(workspace.id), service.events(workspace.id)) == before
    first.close()
    assert _tokens(list(send(second, Event())))


@pytest.mark.parametrize("kind", ["main", "dax"])
def test_clear_transaction_rechecks_active_requests_at_commit(studio, kind):
    service, workspace = studio
    request = _main(workspace) if kind == "main" else _dax(workspace)
    service.store.start_assistant_request(
        kind, "reserved", request, workspace.id, message="Pending"
    )
    clear = ClearChatRequest(
        workspace_id=workspace.id,
        expected_base_hash=workspace.base_hash,
        idempotency_key="clear-at-commit",
    )
    with pytest.raises(StateConflictError, match="Wait for"):
        if kind == "main":
            service.store.archive_main_chat(
                workspace.id,
                clear,
                expected_base_hash=workspace.base_hash,
                idempotency_key=clear.idempotency_key,
            )
        else:
            service.store.archive_dax_chat(
                workspace.id,
                None,
                clear,
                expected_base_hash=workspace.base_hash,
                idempotency_key=clear.idempotency_key,
            )
    assert not any(event["type"].endswith("chat.cleared") for event in service.events(workspace.id))
    assert service.store.idempotent_response(clear.idempotency_key, clear, type(workspace)) is None
    service.store.finish_assistant_request(kind, "reserved", {"status": "cancelled"})


def test_workspace_save_cannot_restore_history_from_before_concurrent_clear(studio, monkeypatch):
    service, workspace = studio
    list(service.chat_events(_main(workspace), Event()))
    original = service.store.put

    def clear_before_commit(kind, value, **kwargs):
        if kind == "workspace":
            service.clear_chat(
                workspace.id,
                ClearChatRequest(
                    workspace_id=workspace.id,
                    expected_base_hash=workspace.base_hash,
                    idempotency_key="clear-before-metadata-commit",
                ),
            )
        return original(kind, value, **kwargs)

    monkeypatch.setattr(service.store, "put", clear_before_commit)
    rename = RenameWorkspaceRequest(
        workspace_id=workspace.id,
        name="Renamed",
        expected_base_hash=workspace.base_hash,
        idempotency_key="rename-raced-clear",
    )
    with pytest.raises(StateConflictError, match="Chat history changed"):
        service.rename_workspace(workspace.id, rename)
    current = service.workspace(workspace.id)
    assert current.name == workspace.name
    assert current.chat_boundaries == (service.messages(workspace.id)[-1]["id"],)
    assert service._recent_chat(current) == []
    assert (
        service.store.idempotent_response(rename.idempotency_key, rename, type(workspace)) is None
    )


@pytest.mark.parametrize("kind", ["main", "dax"])
@pytest.mark.parametrize("marker", [None, 0])
def test_completed_receipt_cannot_replay_into_an_archived_conversation(studio, kind, marker):
    service, workspace = studio
    request = (_main(workspace) if kind == "main" else _dax(workspace)).model_copy(
        update={"chat_boundary": marker}
    )
    send = (
        service.chat_events if kind == "main" else lambda r, c: dax_assistant_events(service, r, c)
    )
    assert _tokens(list(send(request, Event())))
    clear_type = ClearChatRequest if kind == "main" else ClearDaxChatRequest
    clear = clear_type(
        workspace_id=workspace.id,
        expected_base_hash=workspace.base_hash,
        idempotency_key="clear-completed",
    )
    (service.clear_chat if kind == "main" else service.clear_dax_chat)(workspace.id, clear)
    before = (service.messages(workspace.id), service.events(workspace.id))
    with pytest.raises(StateConflictError, match="archived"):
        list(send(request, Event()))
    assert (service.messages(workspace.id), service.events(workspace.id)) == before


@pytest.mark.parametrize("kind", ["main", "dax"])
def test_queued_new_request_requires_current_boundary_even_with_unchanged_base_hash(studio, kind):
    service, workspace = studio
    send = (
        service.chat_events if kind == "main" else lambda r, c: dax_assistant_events(service, r, c)
    )
    request = _main(workspace) if kind == "main" else _dax(workspace)
    list(send(request, Event()))
    clear_type = ClearChatRequest if kind == "main" else ClearDaxChatRequest
    cleared = (service.clear_chat if kind == "main" else service.clear_dax_chat)(
        workspace.id,
        clear_type(
            workspace_id=workspace.id,
            expected_base_hash=workspace.base_hash,
            idempotency_key="clear-queued",
        ),
    )
    assert cleared.base_hash == workspace.base_hash
    boundary = (
        cleared.chat_boundaries[-1]
        if kind == "main"
        else next(
            event["payload"]["through_sequence"]
            for event in service.events(workspace.id)
            if event["type"] == "dax.chat.cleared"
        )
    )
    queued = _main(workspace, "queued") if kind == "main" else _dax(workspace, "queued")
    with pytest.raises(StateConflictError, match="archived"):
        list(send(queued.model_copy(update={"chat_boundary": 0}), Event()))
    assert _tokens(list(send(queued.model_copy(update={"chat_boundary": boundary}), Event())))


@pytest.mark.parametrize("kind", ["main", "dax"])
def test_initial_boundary_zero_can_replay_legacy_core_fingerprint(studio, monkeypatch, kind):
    service, workspace = studio
    request = _main(workspace) if kind == "main" else _dax(workspace)
    send = (
        service.chat_events if kind == "main" else lambda r, c: dax_assistant_events(service, r, c)
    )
    expected = _tokens(list(send(request, Event())))
    # Old durable envelopes predate the optional field, including its null value.
    legacy = request.model_dump(mode="json", exclude={"chat_boundary"})
    request_id = request.idempotency_key if kind == "main" else request.request_id
    with service.store._connect() as connection:
        connection.execute(
            "UPDATE assistant_requests SET request_hash=? WHERE kind=? AND request_id=?",
            (service.store.content_hash(legacy), kind, request_id),
        )
    service.store = StudioStateStore(service.store.path)

    def no_inference(*args, **kwargs):
        pytest.fail("A legacy completed receipt should replay without generation")

    monkeypatch.setattr(service.model, "stream_chat", no_inference)
    promoted = request.model_copy(update={"chat_boundary": 0})
    assert _tokens(list(send(promoted, Event()))) == expected
    changed = {"message": "Explain something different"} if kind == "main" else {"goal": "Other"}
    with pytest.raises(StateConflictError, match="different input"):
        list(send(promoted.model_copy(update=changed), Event()))


@pytest.mark.parametrize("kind", ["main", "dax"])
def test_new_explicit_boundary_receipt_cannot_retry_with_omitted_marker(studio, kind):
    service, workspace = studio
    request = (_main(workspace) if kind == "main" else _dax(workspace)).model_copy(
        update={"chat_boundary": 0}
    )
    send = (
        service.chat_events if kind == "main" else lambda r, c: dax_assistant_events(service, r, c)
    )
    list(send(request, Event()))
    with pytest.raises(StateConflictError, match="different input"):
        list(send(request.model_copy(update={"chat_boundary": None}), Event()))


@pytest.mark.parametrize("legacy_receipt", [False, True])
def test_action_receipt_stays_in_original_chat_even_with_omitted_marker(studio, legacy_receipt):
    service, workspace = studio
    request = _main(workspace, key="identity", message="What model are you?")
    original = list(service.chat_events(request, Event()))
    if legacy_receipt:
        with service.store._connect() as connection:
            connection.execute(
                "UPDATE chat_actions SET request_hash=?,chat_boundary=NULL WHERE request_id=?",
                (
                    service.store.content_hash(
                        request.model_dump(mode="json", exclude={"chat_boundary"})
                    ),
                    request.idempotency_key,
                ),
            )
    service.store = StudioStateStore(service.store.path)
    assert list(service.chat_events(request, Event())) == original
    service.clear_chat(
        workspace.id,
        ClearChatRequest(
            workspace_id=workspace.id,
            expected_base_hash=workspace.base_hash,
            idempotency_key="clear-action",
        ),
    )
    before = (service.messages(workspace.id), service.events(workspace.id))
    with pytest.raises(StateConflictError, match="archived"):
        list(service.chat_events(request, Event()))
    assert (service.messages(workspace.id), service.events(workspace.id)) == before
    # Legacy callers can still make and replay a fresh action in this new chat.
    fresh = request.model_copy(update={"idempotency_key": "fresh-action"})
    assert list(service.chat_events(fresh, Event())) == list(service.chat_events(fresh, Event()))


def test_legacy_action_table_upgrade_preserves_receipts_and_unknown_boundary(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE chat_actions(request_id TEXT PRIMARY KEY,request_hash TEXT NOT NULL,"
            "payload TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO chat_actions VALUES ('old','hash','{}')")
    store = StudioStateStore(path)
    with store._connect() as connection:
        row = connection.execute("SELECT * FROM chat_actions WHERE request_id='old'").fetchone()
        assert row["payload"] == "{}"
        assert row["chat_boundary"] is None
    StudioStateStore(path)  # Starting again must not repeat or fail the migration.


def test_dax_native_incomplete_response_keeps_readable_evidence_without_completion(
    studio, monkeypatch
):
    service, workspace = studio
    text = "```dax\nSUM('Example'[Amount])\n```"

    def incomplete(message, **kwargs):
        yield text
        raise LocalModelError("Local model response did not finish: length")

    monkeypatch.setattr(service.model, "stream_chat", incomplete)
    with pytest.raises(LocalModelError, match="did not finish"):
        list(dax_assistant_events(service, _dax(workspace), Event()))
    answered = next(
        event["payload"]
        for event in reversed(service.events(workspace.id))
        if event["type"] == "dax.assistant_answered"
    )
    assert text in answered["response"]
    assert answered["status"] == "interrupted"
    assert answered["calculation_request"] is None


def test_chat_creation_commits_own_graph_across_sse_workers_and_rejects_queued_old_action(
    studio, tmp_path, monkeypatch
):
    service, workspace = studio
    source = tmp_path / "orders.csv"
    source.write_text("Region,Sales\nEast,10\nWest,20\n", encoding="utf-8")
    service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="chat-source",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    service.planner = StudioPlanningService(None, service.aggregates)
    request = _main(workspace, key="create-draft", message="Create a sales dashboard by Region")
    request = request.model_copy(
        update={"expected_base_hash": workspace.base_hash, "chat_boundary": 0}
    )
    stream = service.chat_events(request, Event())
    with ThreadPoolExecutor(max_workers=1) as first, ThreadPoolExecutor(max_workers=1) as second:
        assert first.submit(next, stream).result()["event"] == "status"
        with pytest.raises(StateConflictError, match="Wait for"):
            service.clear_chat(
                workspace.id,
                ClearChatRequest(
                    workspace_id=workspace.id,
                    expected_base_hash=workspace.base_hash,
                    idempotency_key="clear-pending-build",
                ),
            )
        draft_event = second.submit(next, stream).result()
        assert draft_event["event"] == "draft"
        remaining = first.submit(list, stream).result()
    current = service.workspace(workspace.id)
    assert current.active_draft_id == draft_event["data"]["draft_id"]
    assert current.base_hash != workspace.base_hash
    assert not service.builds(workspace.id)
    messages = service.messages(workspace.id)
    # The original workspace hash now differs; the completed receipt still replays.
    assert list(service.chat_events(request, Event())) == [draft_event, *remaining]
    assert service.messages(workspace.id) == messages
    cleared = service.clear_chat(
        workspace.id,
        ClearChatRequest(
            workspace_id=workspace.id,
            expected_base_hash=current.base_hash,
            idempotency_key="clear-built-chat",
        ),
    )

    def no_mutation(*args, **kwargs):
        pytest.fail("An action queued before New chat must be rejected before planning")

    monkeypatch.setattr(service, "revise_draft", no_mutation)
    queued = request.model_copy(
        update={"idempotency_key": "queued-build", "expected_base_hash": current.base_hash}
    )
    with pytest.raises(StateConflictError, match="archived"):
        list(service.chat_events(queued, Event()))
    assert service.workspace(workspace.id) == cleared
    assert service.messages(workspace.id) == messages


def test_background_action_receipt_requires_reserved_id_and_exact_envelope(studio):
    service, workspace = studio
    request = _main(workspace)
    result = {"text": "Saved edit receipt", "draft_id": None}

    def record(envelope):
        # An edit worker has no caller ContextVar, but retains its exact request.
        service.store.record_chat_action(
            request.idempotency_key, envelope, workspace.id, envelope.message, result
        )

    with service.store.main_chat_operation(request), ThreadPoolExecutor(max_workers=1) as worker:
        changed = request.model_copy(update={"message": "Different input"})
        with pytest.raises(StateConflictError, match="Wait for"):
            worker.submit(record, changed).result()
        with (
            service.store.main_chat_call(workspace.id, request.idempotency_key),
            pytest.raises(StateConflictError, match="Wait for"),
        ):
            record(changed)
        assert service.messages(workspace.id) == []
        worker.submit(record, request).result()
    assert service.store.chat_action(request.idempotency_key, request) == result
    assert [item["content"] for item in service.messages(workspace.id)] == [
        request.message,
        result["text"],
    ]


def test_stopped_closed_code_fence_retains_status_after_reload_and_is_not_prompt_history(
    studio,
    monkeypatch,
):
    service, workspace = studio
    closed = []
    formula = "```dax\nSUM('Example'[Amount])\n```"

    def answer(message, **kwargs):
        try:
            yield formula
            yield "Remaining explanation"
        finally:
            closed.append(True)

    monkeypatch.setattr(service.model, "stream_chat", answer)
    request = _main(workspace)
    stream = service.chat_events(request, Event())
    assert next(stream)["data"]["text"] == formula
    stream.close()
    assert closed == [True]
    service.store = StudioStateStore(service.store.path)
    rows = service.messages(workspace.id)
    assert rows[-1]["content"] == formula
    assert rows[-1]["status"] == "cancelled"
    assert rows[-1]["request_id"] == request.idempotency_key
    assert service._recent_chat(workspace) == []
    token = "conversation-evidence-test-token"
    client = TestClient(create_app(service, token))
    loaded = client.get(
        f"/v1/workspaces/{workspace.id}/messages", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert loaded[-1]["status"] == "cancelled"


@pytest.mark.parametrize("failure", ["runtime", "timeout"])
def test_partial_errors_remain_incomplete_even_if_a_code_fence_closed(studio, monkeypatch, failure):
    service, workspace = studio

    def answer(message, **kwargs):
        yield "```dax\nSUM('Example'[Amount])\n```"
        if failure == "timeout":
            raise LocalModelError("Generation timeout")
        raise RuntimeError("Generation failed")

    monkeypatch.setattr(service.model, "stream_chat", answer)
    if failure == "runtime":
        with pytest.raises(RuntimeError, match="Generation failed"):
            list(service.chat_events(_main(workspace), Event()))
    else:
        list(service.chat_events(_main(workspace), Event()))
    rows = service.messages(workspace.id)
    assert rows[-1]["status"] == ("failed" if failure == "runtime" else "interrupted")
    assert service._recent_chat(workspace) == []


def test_legacy_unkeyed_discussion_still_gets_completion_evidence_and_active_guard(studio):
    service, workspace = studio
    stream = service.chat_events(
        ChatRequest(workspace_id=workspace.id, message="Explain SUM"), Event()
    )
    assert next(stream)["event"] == "token"
    with pytest.raises(StateConflictError, match="Wait for"):
        service.clear_chat(
            workspace.id,
            ClearChatRequest(
                workspace_id=workspace.id,
                expected_base_hash=workspace.base_hash,
                idempotency_key="clear-unkeyed",
            ),
        )
    stream.close()
    assert service.messages(workspace.id)[-1]["status"] == "cancelled"


def test_snapshot_change_rechecks_assistant_admission_in_atomic_pair(studio, monkeypatch, tmp_path):
    service, workspace = studio
    original = service._snapshot_chat_boundaries

    def boundaries(current):
        selected = original(current)
        service.store.start_assistant_request(
            "main", "admitted-after-preflight", _main(workspace), workspace.id, message="Pending"
        )
        return selected

    monkeypatch.setattr(service, "_snapshot_chat_boundaries", boundaries)
    source = tmp_path / "race.csv"
    source.write_text("Amount\n1\n", encoding="utf-8")
    with pytest.raises(StateConflictError, match="Wait for"):
        service.ingest(
            IngestRequest(
                workspace_id=workspace.id,
                item_paths=[str(source)],
                idempotency_key="snapshot-race",
                expected_base_hash=workspace.base_hash,
            )
        )
    assert service.workspace(workspace.id).active_snapshot_id is None
    assert service.store.list_objects("snapshot", DatasetSnapshot) == []
    service.store.finish_assistant_request(
        "main", "admitted-after-preflight", {"status": "cancelled"}
    )


@pytest.mark.parametrize(
    "message",
    [
        "Create a Python function that parses dates",
        "Add a SQL query to calculate total revenue",
        "Build a React component showing an example chart",
        "Generate a PowerShell script to rename files",
        "Create a Python forecasting script",
        "Add unit tests for this function",
        "Create a concise Python function called safe_divide(a, b) that returns None when b "
        "is zero and otherwise returns a / b. Include one short usage example.",
        "Create a SQL query called active_customer_titles that returns customer_id and title.",
        "Create an API handler called get_titles that returns a JSON list of titles.",
        "Create an API route named /titles that returns a short JSON usage example.",
        "Create an R script to chart sales",
        "Add a C# class called Measure",
        "Create a Java function called getPage",
        "Create a Kotlin function that parses dates",
        "Add a Swift class called Measure",
        "Create a PHP function called getPage",
        "Create a Ruby class called Report",
        "Create a C function that divides two numbers",
        "Create a C++ function that parses dates",
        "Create a Go function that parses dates",
        "Write a Rust function that parses dates",
        "Explain Python decorators and when to use them. Keep it concise.",
        "Explain the Python data model",
        "Explain the Java memory model",
        "Compare SQL INNER JOIN and LEFT JOIN",
        "Explain SQL indexes",
        "What is a REST API?",
        "Compare REST and GraphQL tradeoffs",
        "Explain React hooks",
        "Explain FastAPI dependency injection",
        "What is Git rebasing?",
        "Explain Docker containers compared with virtual machines",
        "What does Kubernetes do?",
        "Explain CI/CD pipelines",
        "Explain database transactions",
        "Explain algorithm complexity",
        "Compare HTTP authentication and authorization",
    ],
)
def test_developer_requests_are_discussion_without_planning_or_writes(studio, monkeypatch, message):
    service, workspace = studio
    assert not is_build_request(message)
    captured = []

    def answer(message, *, system_prompt, **kwargs):
        captured.append(system_prompt)
        assert kwargs["max_output_tokens"] == 768
        assert kwargs["worked_examples"] is False
        yield "Here is an example."

    def no_build(*args, **kwargs):
        pytest.fail("A code example must not call the dashboard planner")

    monkeypatch.setattr(service.model, "stream_chat", answer)
    monkeypatch.setattr(service, "create_draft", no_build)
    assert _tokens(list(service.chat_events(_main(workspace, message=message), Event()))) == (
        "Here is an example."
    )
    assert "general technical knowledge" in captured[0]
    assert "do not claim to have executed code" in captured[0]
    assert "No workspace facts are supplied" in captured[0]
    assert "Clearly label hypothetical names and values used in examples" in captured[0]
    assert "Do not present examples as facts about the user's workspace" in captured[0]
    assert "Power BI" not in captured[0]
    assert "DAX" not in captured[0]
    assert len(captured[0]) < 1100
    assert "at most 120 prose words" in captured[0]
    assert "without headings or checklists unless requested" in captured[0]
    assert (
        "Honor every explicit requirement and preserve requested names and operations"
        in captured[0]
    )
    assert "Finish once the requested topic is answered" in captured[0]
    assert "Modeling:" not in captured[0]
    assert "In DAX, reference columns" not in captured[0]
    assert service.workspace(workspace.id) == workspace


@pytest.mark.parametrize(
    "message",
    [
        "Change all chart titles to red",
        "Rename the chart title to Overview",
        "Make the chart font bigger",
    ],
)
def test_report_styling_guards_still_refuse_before_inference(studio, monkeypatch, message):
    service, workspace = studio

    def no_inference(*args, **kwargs):
        pytest.fail("A report styling request must retain its deterministic refusal")

    monkeypatch.setattr(service.model, "stream_chat", no_inference)
    reply = _tokens(list(service.chat_events(_main(workspace, message=message), Event())))
    assert "Format pane" in reply
    assert service.workspace(workspace.id) == workspace


@pytest.mark.parametrize(
    "message",
    [
        "Create a DAX measure for sales",
        "Create a dashboard from Python generated CSV data",
        "Add a Python visual",
        "Add an R visual",
        "Update the dashboard using an API endpoint",
        "Build it",
    ],
)
def test_report_artifacts_keep_existing_action_routing(message):
    assert is_build_request(message)


@pytest.mark.parametrize(
    "message",
    [
        "Explain DAX row context versus filter context",
        "Explain SQL joins compared with Power BI relationships",
        "Why do SQL joins duplicate rows in this model?",
        "Explain the API source in this Power BI report",
        "Explain the relationships in this workspace",
    ],
)
def test_conceptual_bi_questions_keep_workspace_routing(message):
    assert not is_developer_request(message)


@pytest.mark.parametrize(
    "message,expected_reference_query",
    [
        ("Create a SQL query that returns title", ""),
        ("Create an API route named /titles", ""),
        ("Explain Python decorators", ""),
        ("Explain SQL joins", ""),
        ("What is a REST API?", ""),
        ("Create a DAX query using COUNTROWS for Power BI", None),
        ("Create a Power Query script that removes null rows", None),
    ],
)
def test_developer_reference_retrieval_stays_in_its_requested_domain(
    studio,
    monkeypatch,
    message,
    expected_reference_query,
):
    service, workspace = studio
    queries = []
    original = service.knowledge.retrieve

    def retrieve(query, **kwargs):
        queries.append(query)
        return original(query, **kwargs)

    monkeypatch.setattr(service.knowledge, "retrieve", retrieve)
    list(service.chat_events(_main(workspace, message=message), Event()))
    assert queries == [message if expected_reference_query is None else expected_reference_query]


@pytest.mark.parametrize(
    "followup", ["Show an example", "Add type hints", "Explain it", "What are the tradeoffs?"]
)
def test_developer_followups_use_recent_user_intent_not_earlier_bi_context(
    studio, monkeypatch, followup
):
    service, workspace = studio
    service.store.add_message(workspace.id, "user", "Explain the relationships in this workspace")
    service.store.add_message(workspace.id, "assistant", "Unrelated model advice about Trucks.")
    service.store.add_message(workspace.id, "user", "Explain Python decorators")
    service.store.add_message(workspace.id, "assistant", "A decorator wraps a function.")
    captured = {}

    def answer(message, **options):
        captured.update(message=message, **options)
        yield "A Python example."

    def no_context(*args, **kwargs):
        pytest.fail("A general developer follow-up must not load unrelated BI context")

    monkeypatch.setattr(service.model, "stream_chat", answer)
    monkeypatch.setattr(service, "_chat_context", no_context)
    assert _tokens(list(service.chat_events(_main(workspace, message=followup), Event()))) == (
        "A Python example."
    )
    assert captured["max_output_tokens"] == 768
    assert captured["history"] == [
        {"role": "user", "content": "Explain Python decorators"},
        {"role": "assistant", "content": "A decorator wraps a function."},
    ]
    assert "general technical knowledge" in captured["system_prompt"]


@pytest.mark.parametrize("chained", [False, True])
@pytest.mark.parametrize("pressure", [False, True])
def test_long_developer_replies_preserve_the_user_topic_in_actual_model_history(
    studio, monkeypatch, chained, pressure
):
    service, workspace = studio
    topic = "Explain Python decorators and when to use them. Keep it concise."
    long_answer = "A Python decorator wraps a function. " + "Additional explanation. " * 150
    original = [("user", topic), ("assistant", long_answer)]
    if chained:
        original += [
            ("user", "Show an example"),
            ("assistant", "def decorator(fn): " + long_answer),
        ]
    for role, content in original:
        service.store.add_message(workspace.id, role, content)
    captured = {}

    def answer(message, **options):
        captured.update(message=message, **options)
        yield "A Python decorator example."

    monkeypatch.setattr(service.model, "stream_chat", answer)
    if pressure:
        monkeypatch.setattr(service.model, "prompt_bytes_per_token", lambda: 0.6, raising=False)
    followup = "Add type hints" if chained else "Show an example"
    list(service.chat_events(_main(workspace, message=followup), Event()))
    history = captured["history"]
    assert {"role": "user", "content": topic} in history
    assert history[0]["role"] == "user"
    assert any(item["role"] == "assistant" for item in history)
    if chained:
        assert {"role": "user", "content": "Show an example"} in history
    assert sum(len(item["content"]) for item in history) <= 1600
    if pressure:
        total = len(captured["system_prompt"].encode()) + len(followup.encode())
        total += sum(len(item["content"].encode()) for item in history)
        assert total / 0.6 + 768 + max(128, 64 * (len(history) + 2)) <= 4096
        assert sum(len(item["content"]) for item in history) < 1600
    stored = service.messages(workspace.id)
    assert [(item["role"], item["content"]) for item in stored[: len(original)]] == original


@pytest.mark.parametrize(
    "earlier",
    [
        "Add a C# class called Measure",
        "Create an R script to chart sales",
        "Explain the Python data model",
    ],
)
def test_code_names_that_resemble_bi_artifacts_remain_in_developer_followups(
    studio, monkeypatch, earlier
):
    service, workspace = studio
    service.store.add_message(workspace.id, "user", earlier)
    service.store.add_message(workspace.id, "assistant", "A technical example.")
    captured = {}

    def answer(message, **options):
        captured.update(options)
        yield "More explanation."

    monkeypatch.setattr(service.model, "stream_chat", answer)
    list(service.chat_events(_main(workspace, message="Show an example"), Event()))
    assert captured["max_output_tokens"] == 768
    assert captured["history"] == [
        {"role": "user", "content": earlier},
        {"role": "assistant", "content": "A technical example."},
    ]


@pytest.mark.parametrize("boundary", ["report-switch", "archive", "assistant-only"])
def test_developer_followups_do_not_infer_from_other_topics_or_archives(
    studio, monkeypatch, boundary
):
    service, workspace = studio
    service.store.add_message(workspace.id, "user", "Explain Python decorators")
    service.store.add_message(workspace.id, "assistant", "A decorator wraps a function.")
    if boundary == "report-switch":
        service.store.add_message(
            workspace.id, "user", "Explain the relationships in this workspace"
        )
        service.store.add_message(workspace.id, "assistant", "A report example.")
    else:
        workspace = service.clear_chat(
            workspace.id,
            ClearChatRequest(
                workspace_id=workspace.id,
                expected_base_hash=workspace.base_hash,
                idempotency_key="archive-developer",
            ),
        )
        if boundary == "assistant-only":
            service.store.add_message(workspace.id, "assistant", "Explain Python decorators")
    captured = {}

    def answer(message, **options):
        captured.update(options)
        yield "A grounded answer."

    monkeypatch.setattr(service.model, "stream_chat", answer)
    list(service.chat_events(_main(workspace, message="Show an example"), Event()))
    assert "max_output_tokens" not in captured
    assert "general technical knowledge" not in captured["system_prompt"]


def test_general_developer_concept_omits_dataset_schema_but_explicit_workspace_request_keeps_it(
    studio, monkeypatch, tmp_path
):
    service, workspace = studio
    source = tmp_path / "source.csv"
    source.write_text("Date,Amount\n2026-01-01,3\n", encoding="utf-8")
    service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="developer-data",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    captured = []

    def answer(message, **options):
        captured.append(options["system_prompt"])
        yield "A technical answer."

    monkeypatch.setattr(service.model, "stream_chat", answer)
    monkeypatch.setattr(service, "_chat_context", lambda *args, **kwargs: "Reviewed dataset Amount")
    list(service.chat_events(_main(workspace, message="Explain Python decorators"), Event()))
    assert "Reviewed dataset Amount" not in captured[0]
    assert "No workspace facts are supplied" in captured[0]
    list(
        service.chat_events(
            _main(
                workspace,
                key="grounded-python",
                message="Create a Python function using this dataset",
            ),
            Event(),
        )
    )
    assert "Reviewed dataset Amount" in captured[1]
    assert "Modelcraft" in captured[1]
    assert "never invent dataset fields or values" in captured[1]
    list(
        service.chat(
            workspace.id,
            "Create a Python function using this field",
            selected_context="Selected field Amount",
        )
    )
    assert "Reviewed dataset Amount" in captured[2]


def test_dax_completed_reply_is_durable_and_replays_without_main_chat_pollution(
    studio,
    monkeypatch,
):
    service, workspace = studio
    request = _dax(workspace)
    first = _tokens(list(dax_assistant_events(service, request, Event())))
    service.store = StudioStateStore(service.store.path)

    def no_inference(*args, **kwargs):
        pytest.fail("A completed DAX reply must replay without another model request")

    monkeypatch.setattr(service.model, "stream_chat", no_inference)
    assert _tokens(list(dax_assistant_events(service, request, Event()))) == first
    assert service.messages(workspace.id) == []
    events = service.events(workspace.id)
    assert len([event for event in events if event["type"] == "dax.assistant_requested"]) == 1
    assert len([event for event in events if event["type"] == "dax.assistant_answered"]) == 1
    with pytest.raises(StateConflictError, match="different input"):
        changed = request.model_copy(update={"goal": "Explain CALCULATE"})
        list(dax_assistant_events(service, changed, Event()))


def test_dax_preparation_failure_is_audited_and_same_request_can_retry(studio, monkeypatch):
    service, workspace = studio
    original = service._record_retrieval
    attempts = []

    def retrieve(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("Simulated retrieval failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_record_retrieval", retrieve)
    request = _dax(workspace)
    with pytest.raises(RuntimeError, match="retrieval"):
        list(dax_assistant_events(service, request, Event()))
    assert any(event["type"] == "dax.assistant_requested" for event in service.events(workspace.id))
    assert _tokens(list(dax_assistant_events(service, request, Event())))
    answers = [e for e in service.events(workspace.id) if e["type"] == "dax.assistant_answered"]
    assert [event["payload"]["status"] for event in answers] == ["interrupted", "completed"]


def test_dax_archive_boundary_is_scoped_durable_and_idempotent(studio):
    service, workspace = studio
    list(dax_assistant_events(service, _dax(workspace), Event()))
    prior = service.events(workspace.id)[-1]["sequence"]
    service.store.event(
        workspace.id,
        "dax.assistant_answered",
        {
            "snapshot_id": "snapshot-other",
            "request_id": "other",
            "response": "Other data",
        },
    )
    request = ClearDaxChatRequest(
        workspace_id=workspace.id,
        snapshot_id=None,
        idempotency_key="dax-clear",
        expected_base_hash=workspace.base_hash,
    )
    assert service.clear_dax_chat(workspace.id, request) == workspace
    service.store = StudioStateStore(service.store.path)
    boundary = [e for e in service.events(workspace.id) if e["type"] == "dax.chat.cleared"]
    assert len(boundary) == 1
    assert boundary[0]["payload"] == {"snapshot_id": None, "through_sequence": prior}
    list(dax_assistant_events(service, _dax(workspace, "dax-2"), Event()))
    service.clear_dax_chat(workspace.id, request)
    assert len([e for e in service.events(workspace.id) if e["type"] == "dax.chat.cleared"]) == 1
    assert service.messages(workspace.id) == []
    assert service.workspace(workspace.id) == workspace
    with pytest.raises(StateConflictError):
        changed = request.model_copy(update={"snapshot_id": "snapshot-other"})
        service.clear_dax_chat(workspace.id, changed)


def test_workspace_deletion_erases_discussion_and_action_response_receipts(studio):
    service, workspace = studio
    list(service.chat_events(_main(workspace), Event()))
    identity = _main(workspace, key="identity", message="What model are you?")
    list(service.chat_events(identity, Event()))
    list(dax_assistant_events(service, _dax(workspace), Event()))
    service.delete_workspace(
        workspace.id,
        DeleteWorkspaceRequest(
            workspace_id=workspace.id,
            idempotency_key="delete-history",
            expected_base_hash=workspace.base_hash,
            confirm_name=workspace.name,
        ),
    )
    with service.store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM assistant_requests").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM chat_actions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


def test_deleted_workspace_rejects_late_requests_and_does_not_recreate_text(studio):
    service, workspace = studio
    request = _main(workspace)
    stream = service.chat_events(request, Event())
    assert next(stream)["event"] == "token"
    service.delete_workspace(
        workspace.id,
        DeleteWorkspaceRequest(
            workspace_id=workspace.id,
            idempotency_key="delete-while-replying",
            expected_base_hash=workspace.base_hash,
            confirm_name=workspace.name,
        ),
    )
    stream.close()  # Finishing a deleted request cannot reinsert its partial answer.
    with pytest.raises(KeyError, match="Unknown workspace"):
        service.store.start_assistant_request("main", "late", request, workspace.id, message="late")
    with pytest.raises(KeyError, match="Unknown workspace"):
        service.store.add_message(workspace.id, "assistant", "Late private response")
    with pytest.raises(KeyError, match="Unknown workspace"):
        service.store.record_chat_action(
            "late-action", request, workspace.id, "late", {"text": "late"}
        )
    with service.store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM assistant_requests").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM chat_actions").fetchone()[0] == 0
