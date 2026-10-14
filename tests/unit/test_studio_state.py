from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_powerbi_analyst.studio.contracts import LocalWorkspace
from ai_powerbi_analyst.studio.state import EMPTY_HASH, StateConflictError, StudioStateStore


def test_state_connections_close_without_losing_commit_or_rollback(tmp_path: Path) -> None:
    store = StudioStateStore(tmp_path / "state.sqlite3")
    with store._connect() as committed:
        committed.execute("CREATE TABLE lifecycle(value INTEGER)")
        committed.execute("INSERT INTO lifecycle VALUES (1)")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        committed.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="interrupted"), store._connect() as failed:
        failed.execute("INSERT INTO lifecycle VALUES (2)")
        raise RuntimeError("interrupted")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        failed.execute("SELECT 1")
    with store._connect() as reopened:
        assert [row[0] for row in reopened.execute("SELECT value FROM lifecycle")] == [1]


def test_state_store_replays_same_idempotent_mutation(tmp_path: Path) -> None:
    store = StudioStateStore(tmp_path / "state.sqlite3")
    now = datetime.now(UTC)
    workspace = LocalWorkspace(
        id="workspace-test",
        name="Test",
        base_hash="1" * 64,
        created_at=now,
        updated_at=now,
    )

    first = store.put(
        "workspace",
        workspace,
        workspace_id=workspace.id,
        expected_base_hash=EMPTY_HASH,
        idempotency_key="same-request-key",
        mutation_payload={"name": "Test"},
    )
    replay = store.put(
        "workspace",
        workspace,
        workspace_id=workspace.id,
        expected_base_hash=EMPTY_HASH,
        idempotency_key="same-request-key",
        mutation_payload={"name": "Test"},
    )

    assert replay == first
    with pytest.raises(StateConflictError, match="another request"):
        store.put(
            "workspace",
            workspace,
            workspace_id=workspace.id,
            expected_base_hash=workspace.base_hash,
            idempotency_key="same-request-key",
            mutation_payload={"name": "Different"},
        )
