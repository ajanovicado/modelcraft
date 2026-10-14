"""SQLite state store with optimistic concurrency and idempotent mutations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, cast

from pydantic import BaseModel

from .contracts import LocalWorkspace

EMPTY_HASH = hashlib.sha256(b"").hexdigest()
T = TypeVar("T", bound=BaseModel)
U = TypeVar("U", bound=BaseModel)
_CHAT_OPERATION_OWNER: ContextVar[tuple[str, str] | None] = ContextVar(
    "studio_chat_operation_owner", default=None
)


class StateConflictError(RuntimeError):
    """A mutation was replayed with different input or against stale state."""


class StudioStateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._active_assistants: set[tuple[str, str]] = set()
        self._active_chat_operations: dict[str, tuple[str, str]] = {}
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                yield connection
        finally:
            # sqlite3.Connection's own context manager commits or rolls back,
            # but leaves the file handle open until garbage collection.
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS objects (
                    kind TEXT NOT NULL,
                    id TEXT NOT NULL,
                    workspace_id TEXT,
                    base_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(kind, id)
                );
                CREATE INDEX IF NOT EXISTS objects_workspace
                    ON objects(workspace_id, kind, updated_at DESC);
                CREATE TABLE IF NOT EXISTS idempotency (
                    key TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    response_kind TEXT NOT NULL,
                    response_id TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
                    content TEXT NOT NULL,
                    citations TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS chat_actions (
                    request_id TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    chat_boundary INTEGER
                );
                CREATE TABLE IF NOT EXISTS assistant_requests (
                    kind TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    user_id INTEGER,
                    assistant_id INTEGER,
                    payload TEXT,
                    PRIMARY KEY(kind, request_id)
                );
                CREATE TABLE IF NOT EXISTS draft_versions (
                    draft_id TEXT NOT NULL,
                    base_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(draft_id, base_hash)
                );
                """
            )
            if "chat_boundary" not in {
                row["name"] for row in connection.execute("PRAGMA table_info(chat_actions)")
            }:
                connection.execute("ALTER TABLE chat_actions ADD COLUMN chat_boundary INTEGER")

    @staticmethod
    def content_hash(value: object) -> str:
        payload: object = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _request_hash(cls, request: object, *, omit_initial_boundary: bool = False) -> str:
        data = request.model_dump(mode="json") if isinstance(request, BaseModel) else request
        if isinstance(data, dict) and "chat_boundary" in data:
            data = dict(data)
            if (
                data["chat_boundary"] is None
                or omit_initial_boundary
                and data["chat_boundary"] == 0
            ):
                data.pop("chat_boundary")
        return cls.content_hash(data)

    @classmethod
    def _matches_request(cls, stored_hash: str, request: object) -> bool:
        # Scope validation runs before this fallback: explicit zero is equivalent
        # to a legacy omitted marker only while the current conversation is initial.
        return stored_hash in {
            cls._request_hash(request),
            cls._request_hash(request, omit_initial_boundary=True),
        }

    def get(self, kind: str, object_id: str, model: type[T]) -> T | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM objects WHERE kind = ? AND id = ?",
                (kind, object_id),
            ).fetchone()
        return None if row is None else model.model_validate_json(str(row["payload"]))

    def list_objects(
        self,
        kind: str,
        model: type[T],
        workspace_id: str | None = None,
    ) -> list[T]:
        query = "SELECT payload FROM objects WHERE kind = ?"
        values: list[str] = [kind]
        if workspace_id is not None:
            query += " AND workspace_id = ?"
            values.append(workspace_id)
        query += " ORDER BY updated_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [model.model_validate_json(str(row["payload"])) for row in rows]

    def put(
        self,
        kind: str,
        value: T,
        *,
        workspace_id: str | None,
        expected_base_hash: str,
        idempotency_key: str,
        mutation_payload: object,
    ) -> T:
        request_hash = self.content_hash(mutation_payload)
        raw_id = value.model_dump().get("id")
        if raw_id is None:
            raise TypeError("Stored models require an id field")
        value_id = str(raw_id)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT request_hash, response_kind, response_id FROM idempotency WHERE key = ?",
                (idempotency_key,),
            ).fetchone()
            if prior is not None:
                if str(prior["request_hash"]) != request_hash:
                    raise StateConflictError("Idempotency key was already used for another request")
                stored = connection.execute(
                    "SELECT payload FROM objects WHERE kind = ? AND id = ?",
                    (str(prior["response_kind"]), str(prior["response_id"])),
                ).fetchone()
                if stored is None:
                    raise StateConflictError("Idempotent response is no longer available")
                return type(value).model_validate_json(str(stored["payload"]))
            current = connection.execute(
                "SELECT base_hash FROM objects WHERE kind = ? AND id = ?",
                (kind, value_id),
            ).fetchone()
            current_hash = EMPTY_HASH if current is None else str(current["base_hash"])
            if current_hash != expected_base_hash:
                raise StateConflictError(
                    f"Stale {kind} base hash; expected {current_hash}, "
                    f"received {expected_base_hash}"
                )
            self._guard_conversation_change(connection, kind, value, value_id)
            payload = value.model_dump_json()
            if kind == "draft" and current is not None:
                connection.execute(
                    "INSERT OR IGNORE INTO draft_versions(draft_id, base_hash, payload) "
                    "SELECT id, base_hash, payload FROM objects WHERE kind='draft' AND id=?",
                    (value_id,),
                )
            base_hash = str(getattr(value, "base_hash", self.content_hash(value)))
            connection.execute(
                """
                INSERT INTO objects(kind, id, workspace_id, base_hash, payload)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(kind, id) DO UPDATE SET
                    workspace_id = excluded.workspace_id,
                    base_hash = excluded.base_hash,
                    payload = excluded.payload,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (kind, value_id, workspace_id, base_hash, payload),
            )
            connection.execute(
                """
                INSERT INTO idempotency(key, request_hash, response_kind, response_id)
                VALUES (?, ?, ?, ?)
                """,
                (idempotency_key, request_hash, kind, value_id),
            )
            return value

    def draft_version(self, draft_id: str, base_hash: str, model: type[T]) -> T:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM draft_versions WHERE draft_id=? AND base_hash=?",
                (draft_id, base_hash),
            ).fetchone()
        if row is None:
            raise ValueError("This previous draft is not available for restoration")
        return model.model_validate_json(str(row["payload"]))

    def idempotent_response(
        self,
        idempotency_key: str,
        mutation_payload: object,
        model: type[T],
    ) -> T | None:
        """Return an already-recorded response, rejecting key reuse with new input."""
        request_hash = self.content_hash(mutation_payload)
        with self._lock, self._connect() as connection:
            prior = connection.execute(
                "SELECT request_hash, response_kind, response_id FROM idempotency WHERE key = ?",
                (idempotency_key,),
            ).fetchone()
            if prior is None:
                return None
            if str(prior["request_hash"]) != request_hash:
                raise StateConflictError("Idempotency key was already used for another request")
            stored = connection.execute(
                "SELECT payload FROM objects WHERE kind = ? AND id = ?",
                (str(prior["response_kind"]), str(prior["response_id"])),
            ).fetchone()
        if stored is None:
            raise StateConflictError("Idempotent response is no longer available")
        try:
            return model.model_validate_json(str(stored["payload"]))
        except (TypeError, ValueError) as error:
            raise StateConflictError("Idempotent response has an unexpected type") from error

    def put_pair(
        self,
        primary_kind: str,
        primary_value: T,
        *,
        primary_expected_base_hash: str,
        primary_idempotency_key: str,
        secondary_kind: str,
        secondary_value: U,
        secondary_expected_base_hash: str,
        secondary_idempotency_key: str,
        workspace_id: str | None,
        mutation_payload: object,
    ) -> tuple[T, U]:
        """Atomically store a mutation and its separately addressable evidence object."""
        request_hash = self.content_hash(mutation_payload)
        primary_id = _model_id(primary_value)
        secondary_id = _model_id(secondary_value)
        pairs = (
            (
                primary_kind,
                primary_value,
                primary_id,
                primary_expected_base_hash,
                primary_idempotency_key,
            ),
            (
                secondary_kind,
                secondary_value,
                secondary_id,
                secondary_expected_base_hash,
                secondary_idempotency_key,
            ),
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior_rows = [
                connection.execute(
                    "SELECT request_hash, response_kind, response_id FROM idempotency "
                    "WHERE key = ?",
                    (idempotency_key,),
                ).fetchone()
                for *_head, idempotency_key in pairs
            ]
            if any(row is not None for row in prior_rows):
                if not all(row is not None for row in prior_rows):
                    raise StateConflictError("Atomic evidence mutation is incomplete")
                loaded: list[BaseModel] = []
                for row, (_kind, value, _id, _expected, _key) in zip(
                    prior_rows,
                    pairs,
                    strict=True,
                ):
                    assert row is not None
                    if str(row["request_hash"]) != request_hash:
                        raise StateConflictError(
                            "Idempotency key was already used for another request"
                        )
                    stored = connection.execute(
                        "SELECT payload FROM objects WHERE kind = ? AND id = ?",
                        (str(row["response_kind"]), str(row["response_id"])),
                    ).fetchone()
                    if stored is None:
                        raise StateConflictError("Idempotent response is no longer available")
                    loaded.append(type(value).model_validate_json(str(stored["payload"])))
                return loaded[0], loaded[1]  # type: ignore[return-value]

            for kind, _value, value_id, expected, _key in pairs:
                current = connection.execute(
                    "SELECT base_hash FROM objects WHERE kind = ? AND id = ?",
                    (kind, value_id),
                ).fetchone()
                current_hash = EMPTY_HASH if current is None else str(current["base_hash"])
                if current_hash != expected:
                    raise StateConflictError(
                        f"Stale {kind} base hash; expected {current_hash}, received {expected}"
                    )

            for kind, value, value_id, _expected, idempotency_key in pairs:
                self._guard_conversation_change(connection, kind, value, value_id)
                payload = value.model_dump_json()
                base_hash = str(getattr(value, "base_hash", self.content_hash(value)))
                connection.execute(
                    """
                    INSERT INTO objects(kind, id, workspace_id, base_hash, payload)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(kind, id) DO UPDATE SET
                        workspace_id = excluded.workspace_id,
                        base_hash = excluded.base_hash,
                        payload = excluded.payload,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (kind, value_id, workspace_id, base_hash, payload),
                )
                connection.execute(
                    """
                    INSERT INTO idempotency(key, request_hash, response_kind, response_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    (idempotency_key, request_hash, kind, value_id),
                )
        return primary_value, secondary_value

    def mutate(
        self,
        kind: str,
        object_id: str,
        model: type[T],
        *,
        workspace_id: str | None,
        expected_base_hash: str,
        idempotency_key: str,
        mutation_payload: object,
        change: Callable[[T], T],
    ) -> T:
        current = self.get(kind, object_id, model)
        if current is None:
            raise KeyError(f"Unknown {kind}: {object_id}")
        updated = change(current)
        return self.put(
            kind,
            updated,
            workspace_id=workspace_id,
            expected_base_hash=expected_base_hash,
            idempotency_key=idempotency_key,
            mutation_payload=mutation_payload,
        )

    def remove_workspace(
        self,
        workspace_id: str,
        *,
        receipt_payload: dict[str, Any],
        expected_base_hash: str,
        idempotency_key: str,
        mutation_payload: object,
    ) -> dict[str, Any]:
        """Delete a workspace and every row the app stored for it.

        The check-and-delete runs in one transaction: the base hash is re-read,
        then the workspace row, its snapshots, drafts, builds, saved draft
        versions, chat turns and events are removed. The receipt is written with
        the idempotency key in that same transaction, so a retried delete
        replays the receipt instead of failing on the missing workspace. Only
        rows are deleted here; no file is touched.
        """
        request_hash = self.content_hash(mutation_payload)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT request_hash, response_kind, response_id FROM idempotency WHERE key = ?",
                (idempotency_key,),
            ).fetchone()
            if prior is not None:
                if str(prior["request_hash"]) != request_hash:
                    raise StateConflictError("Idempotency key was already used for another request")
                stored = connection.execute(
                    "SELECT payload FROM objects WHERE kind = ? AND id = ?",
                    (str(prior["response_kind"]), str(prior["response_id"])),
                ).fetchone()
                if stored is None:
                    raise StateConflictError("Idempotent response is no longer available")
                return cast(dict[str, Any], json.loads(str(stored["payload"])))
            current = connection.execute(
                "SELECT base_hash FROM objects WHERE kind = 'workspace' AND id = ?",
                (workspace_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"Unknown workspace: {workspace_id}")
            current_hash = str(current["base_hash"])
            if current_hash != expected_base_hash:
                raise StateConflictError(
                    f"Stale workspace base hash; expected {current_hash}, "
                    f"received {expected_base_hash}"
                )
            removed = {
                str(row["kind"]): int(row["total"])
                for row in connection.execute(
                    "SELECT kind, COUNT(*) AS total FROM objects WHERE workspace_id = ? "
                    "GROUP BY kind",
                    (workspace_id,),
                ).fetchall()
            }
            messages = connection.execute(
                "SELECT COUNT(*) AS total FROM conversations WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            events = connection.execute(
                "SELECT COUNT(*) AS total FROM events WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            receipt = {
                **receipt_payload,
                "removed_snapshots": removed.get("snapshot", 0),
                "removed_drafts": removed.get("draft", 0),
                "removed_builds": removed.get("build", 0),
                "removed_messages": int(messages["total"]) if messages is not None else 0,
                "removed_events": int(events["total"]) if events is not None else 0,
            }
            connection.execute(
                "DELETE FROM draft_versions WHERE draft_id IN "
                "(SELECT id FROM objects WHERE kind = 'draft' AND workspace_id = ?)",
                (workspace_id,),
            )
            connection.execute("DELETE FROM objects WHERE workspace_id = ?", (workspace_id,))
            connection.execute("DELETE FROM conversations WHERE workspace_id = ?", (workspace_id,))
            # Action receipts contain response text too; delete them with their audit rows.
            action_ids = [
                json.loads(str(row["payload"])).get("request_id")
                for row in connection.execute(
                    "SELECT payload FROM events WHERE workspace_id=? AND event_type='chat.action'",
                    (workspace_id,),
                )
            ]
            connection.executemany(
                "DELETE FROM chat_actions WHERE request_id=?", [(key,) for key in action_ids if key]
            )
            connection.execute(
                "DELETE FROM assistant_requests WHERE workspace_id=?", (workspace_id,)
            )
            connection.execute("DELETE FROM events WHERE workspace_id = ?", (workspace_id,))
            connection.execute(
                "INSERT INTO objects(kind,id,workspace_id,base_hash,payload) VALUES (?,?,?,?,?)",
                (
                    "workspace_removal",
                    workspace_id,
                    workspace_id,
                    EMPTY_HASH,
                    json.dumps(receipt, default=str, sort_keys=True),
                ),
            )
            connection.execute(
                "INSERT INTO idempotency(key,request_hash,response_kind,response_id) "
                "VALUES (?,?,?,?)",
                (idempotency_key, request_hash, "workspace_removal", workspace_id),
            )
            return receipt

    def event(self, workspace_id: str, event_type: str, payload: object) -> int:
        value = json.dumps(payload, default=str, sort_keys=True)
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO events(workspace_id, event_type, payload) VALUES (?, ?, ?)",
                (workspace_id, event_type, value),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return an event sequence")
            return int(cursor.lastrowid)

    def events(self, workspace_id: str, after: int = 0) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sequence, event_type, payload, created_at FROM events
                WHERE workspace_id = ? AND sequence > ? ORDER BY sequence LIMIT 250
                """,
                (workspace_id, after),
            ).fetchall()
        return [
            {
                "sequence": int(row["sequence"]),
                "type": str(row["event_type"]),
                "payload": json.loads(str(row["payload"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def add_message(
        self,
        workspace_id: str,
        role: str,
        content: str,
        citations: list[str] | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_workspace(connection, workspace_id)
            connection.execute(
                """
                INSERT INTO conversations(workspace_id, role, content, citations)
                VALUES (?, ?, ?, ?)
                """,
                (workspace_id, role, content, json.dumps(citations or [])),
            )

    def chat_action(self, request_id: str, request: object) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_hash,payload,chat_boundary FROM chat_actions WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            data = request.model_dump() if isinstance(request, BaseModel) else request
            if isinstance(data, dict) and data.get("workspace_id"):
                boundary = self._validate_chat_scope(
                    connection, "main", str(data["workspace_id"]), data, check_base_hash=False
                )
                self._validate_action_boundary(row, boundary)
        if not self._matches_request(row["request_hash"], request):
            raise StateConflictError("Chat request ID was reused with different input")
        return cast(dict[str, Any], json.loads(row["payload"]))

    def record_chat_action(
        self,
        request_id: str,
        request: object,
        workspace_id: str,
        message: str,
        result: dict[str, Any],
    ) -> None:
        """Persist the receipt, both turns and evidence as one SQLite transaction."""
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_workspace(connection, workspace_id)
            prior = connection.execute(
                "SELECT request_hash,chat_boundary FROM chat_actions WHERE request_id=?",
                (request_id,),
            ).fetchone()
            request_hash = self._request_hash(request)
            if prior is not None:
                data = request.model_dump() if isinstance(request, BaseModel) else request
                if isinstance(data, dict):
                    boundary = self._validate_chat_scope(
                        connection, "main", workspace_id, data, check_base_hash=False
                    )
                    self._validate_action_boundary(prior, boundary)
                if not self._matches_request(prior["request_hash"], request):
                    raise StateConflictError("Chat request ID was reused with different input")
                return
            data = request.model_dump() if isinstance(request, BaseModel) else request
            boundary = 0
            if isinstance(data, dict):
                boundary = self._validate_chat_scope(
                    connection, "main", workspace_id, data, check_base_hash=False
                )
            operation = self._active_chat_operations.get(workspace_id)
            if operation is not None and operation != (request_id, request_hash):
                raise StateConflictError(
                    "Wait for the assistant response or stop it before continuing"
                )
            self._require_idle_assistants(
                connection,
                workspace_id,
                kind="main",
                operation_owner=(request_id, request_hash),
            )
            connection.execute(
                "INSERT INTO chat_actions(request_id,request_hash,payload,chat_boundary) "
                "VALUES (?,?,?,?)",
                (request_id, request_hash, json.dumps(result), boundary),
            )
            connection.executemany(
                "INSERT INTO conversations(workspace_id,role,content) VALUES (?,?,?)",
                [(workspace_id, "user", message), (workspace_id, "assistant", result["text"])],
            )
            connection.execute(
                "INSERT INTO events(workspace_id,event_type,payload) VALUES (?,?,?)",
                (workspace_id, "chat.action", json.dumps({"request_id": request_id, **result})),
            )

    def latest_message_id(self, workspace_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) AS latest FROM conversations WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        return int(row["latest"])

    @staticmethod
    def _require_workspace(connection: sqlite3.Connection, workspace_id: str) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM objects WHERE kind='workspace' AND id=?", (workspace_id,)
            ).fetchone()
            is None
        ):
            raise KeyError(f"Unknown workspace: {workspace_id}")

    @staticmethod
    def _validate_action_boundary(row: sqlite3.Row, boundary: int) -> None:
        # A legacy receipt has no original boundary. It can safely replay only in
        # the initial chat; never reintroduce its action after an archive or import.
        if (row["chat_boundary"] or 0) != boundary:
            raise StateConflictError(
                "This request belongs to an archived chat. Send a new message."
            )

    def _validate_chat_scope(
        self,
        connection: sqlite3.Connection,
        kind: str,
        workspace_id: str,
        data: dict[str, Any],
        *,
        check_base_hash: bool = True,
    ) -> int:
        if data.get("workspace_id") not in {None, workspace_id}:
            raise ValueError("Assistant request does not match this workspace")
        stored = connection.execute(
            "SELECT payload,base_hash FROM objects WHERE kind='workspace' AND id=?",
            (workspace_id,),
        ).fetchone()
        if stored is None:
            raise KeyError(f"Unknown workspace: {workspace_id}")
        workspace = json.loads(stored["payload"])
        if kind == "main":
            boundaries = workspace.get("chat_boundaries", [])
            boundary = boundaries[-1] if boundaries else 0
        else:
            snapshot_id = data.get("snapshot_id")
            if snapshot_id is not None and workspace.get("active_snapshot_id") != snapshot_id:
                raise StateConflictError("Data changed. Reopen the DAX lab before continuing.")
            boundary = max(
                (
                    int(item.get("through_sequence", 0))
                    for item in (
                        json.loads(row["payload"])
                        for row in connection.execute(
                            "SELECT payload FROM events WHERE workspace_id=? "
                            "AND event_type='dax.chat.cleared'",
                            (workspace_id,),
                        )
                    )
                    if item.get("snapshot_id") == snapshot_id
                ),
                default=0,
            )
        if data.get("chat_boundary") is not None and data["chat_boundary"] != boundary:
            raise StateConflictError(
                "This request belongs to an archived chat. Send a new message."
            )
        if kind == "main" and check_base_hash and data.get("expected_base_hash") is not None:
            hashes = {stored["base_hash"]}
            if workspace.get("active_draft_id"):
                draft = connection.execute(
                    "SELECT base_hash FROM objects WHERE kind='draft' AND id=? AND workspace_id=?",
                    (workspace["active_draft_id"], workspace_id),
                ).fetchone()
                if draft is not None:
                    hashes.add(draft["base_hash"])
            if data["expected_base_hash"] not in hashes:
                raise StateConflictError("Workspace changed. Reload before sending a new message.")
        return int(boundary)

    def _require_idle_assistants(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        *,
        kind: str | None = None,
        allow_owner: bool = True,
        operation_owner: tuple[str, str] | None = None,
    ) -> None:
        operation = self._active_chat_operations.get(workspace_id)
        owner = _CHAT_OPERATION_OWNER.get() if allow_owner else None
        if (
            operation is not None
            and kind in {None, "main"}
            and owner != (workspace_id, operation[0])
            # Imported-edit receipts finish on a background worker. They may
            # commit only for the exact ID and envelope reserved by this chat.
            and not (allow_owner and operation_owner == operation)
        ):
            raise StateConflictError("Wait for the assistant response or stop it before continuing")
        rows = connection.execute(
            "SELECT kind,request_id FROM assistant_requests WHERE workspace_id=?",
            (workspace_id,),
        )
        if any(
            (row["kind"], row["request_id"]) in self._active_assistants
            and (kind is None or row["kind"] == kind)
            for row in rows
        ):
            raise StateConflictError("Wait for the assistant response or stop it before continuing")

    @contextmanager
    def main_chat_operation(
        self, request: BaseModel, *, check_base_hash: bool = True
    ) -> Iterator[None]:
        data = request.model_dump()
        workspace_id, request_id = str(data["workspace_id"]), str(data["idempotency_key"])
        operation = (request_id, self._request_hash(request))
        with self._lock, self._connect() as connection:
            self._validate_chat_scope(
                connection, "main", workspace_id, data, check_base_hash=check_base_hash
            )
            active = self._active_chat_operations.get(workspace_id)
            if active is not None and active[0] == request_id:
                raise StateConflictError("This assistant request is already running")
            self._require_idle_assistants(connection, workspace_id, kind="main")
            self._active_chat_operations[workspace_id] = operation
        try:
            yield
        finally:
            with self._lock:
                if self._active_chat_operations.get(workspace_id) == operation:
                    self._active_chat_operations.pop(workspace_id)

    @contextmanager
    def main_chat_call(self, workspace_id: str, request_id: str) -> Iterator[None]:
        # SSE resumes a generator on pool workers. Own only this advance/close,
        # never a suspended generator's worker-thread context.
        token = _CHAT_OPERATION_OWNER.set((workspace_id, request_id))
        try:
            yield
        finally:
            _CHAT_OPERATION_OWNER.reset(token)

    def _guard_conversation_change(
        self, connection: sqlite3.Connection, kind: str, value: BaseModel, value_id: str
    ) -> None:
        if kind != "workspace":
            return
        stored = connection.execute(
            "SELECT payload FROM objects WHERE kind='workspace' AND id=?", (value_id,)
        ).fetchone()
        if stored is None:
            return
        previous, updated = json.loads(stored["payload"]), value.model_dump()
        boundaries = tuple(previous.get("chat_boundaries", ()))
        if tuple(updated.get("chat_boundaries", ()))[: len(boundaries)] != boundaries:
            raise StateConflictError("Chat history changed. Reload before saving this workspace.")
        if any(
            previous.get(name) != updated.get(name)
            for name in ("active_snapshot_id", "active_draft_id")
        ):
            self._require_idle_assistants(connection, value_id)

    def archive_main_chat(
        self,
        workspace_id: str,
        request: BaseModel,
        *,
        expected_base_hash: str,
        idempotency_key: str,
    ) -> LocalWorkspace:
        """Reserve the current boundary and persist its receipt in the same transaction."""
        request_hash = self.content_hash(request)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT request_hash FROM idempotency WHERE key=?", (idempotency_key,)
            ).fetchone()
            if prior is not None and prior["request_hash"] != request_hash:
                raise StateConflictError("Idempotency key was already used for another request")
            row = connection.execute(
                "SELECT payload,base_hash FROM objects WHERE kind='workspace' AND id=?",
                (workspace_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown workspace: {workspace_id}")
            workspace = LocalWorkspace.model_validate_json(row["payload"])
            if prior is not None:
                return workspace
            if row["base_hash"] != expected_base_hash:
                raise StateConflictError(
                    "Stale workspace base hash; reload before starting a new chat"
                )
            self._require_idle_assistants(connection, workspace_id, kind="main", allow_owner=False)
            latest = int(
                connection.execute(
                    "SELECT COALESCE(MAX(id),0) FROM conversations WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone()[0]
            )
            boundary = workspace.chat_boundaries[-1] if workspace.chat_boundaries else 0
            if latest > boundary:
                workspace = workspace.model_copy(
                    update={
                        "chat_boundaries": (*workspace.chat_boundaries, latest),
                        "updated_at": datetime.now(UTC),
                    }
                )
                connection.execute(
                    "UPDATE objects SET payload=?,updated_at=CURRENT_TIMESTAMP "
                    "WHERE kind='workspace' AND id=?",
                    (workspace.model_dump_json(), workspace_id),
                )
                connection.execute(
                    "INSERT INTO events(workspace_id,event_type,payload) VALUES (?,?,?)",
                    (workspace_id, "chat.cleared", json.dumps({"through_message_id": latest})),
                )
            connection.execute(
                "INSERT INTO idempotency(key,request_hash,response_kind,response_id) "
                "VALUES (?,?,?,?)",
                (idempotency_key, request_hash, "workspace", workspace_id),
            )
            return workspace

    def start_assistant_request(
        self,
        kind: str,
        request_id: str,
        request: object,
        workspace_id: str,
        *,
        message: str = "",
    ) -> dict[str, Any]:
        """Reserve one durable attempt; a failed retry reuses its original turns.

        Only completed responses replay. A process restart releases the in-memory
        reservation, so a persisted interrupted attempt can safely run again.
        """
        request_hash = self._request_hash(request)
        key = (kind, request_id)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_workspace(connection, workspace_id)
            row = connection.execute(
                "SELECT * FROM assistant_requests WHERE kind=? AND request_id=?", key
            ).fetchone()
            payload = json.loads(row["payload"]) if row is not None and row["payload"] else None
            data = request.model_dump() if isinstance(request, BaseModel) else request
            if isinstance(data, dict):
                self._validate_chat_scope(
                    connection,
                    kind,
                    workspace_id,
                    data,
                    check_base_hash=not (
                        payload is not None and payload.get("status") == "completed"
                    ),
                )
            if row is not None and not self._matches_request(row["request_hash"], request):
                raise StateConflictError("Assistant request ID was reused with different input")
            if row is not None and row["user_id"] is not None:
                if kind == "main":
                    stored = connection.execute(
                        "SELECT payload FROM objects WHERE kind='workspace' AND id=?",
                        (workspace_id,),
                    ).fetchone()
                    boundaries = (
                        json.loads(stored["payload"]).get("chat_boundaries", []) if stored else []
                    )
                    archived = bool(boundaries and row["user_id"] <= boundaries[-1])
                else:
                    snapshot_id = data.get("snapshot_id") if isinstance(data, dict) else None
                    archived = any(
                        boundary.get("snapshot_id") == snapshot_id
                        and row["user_id"] <= boundary.get("through_sequence", 0)
                        for boundary in (
                            json.loads(item["payload"])
                            for item in connection.execute(
                                "SELECT payload FROM events WHERE workspace_id=? "
                                "AND event_type='dax.chat.cleared'",
                                (workspace_id,),
                            )
                        )
                    )
                if archived:
                    raise StateConflictError(
                        "This request belongs to an archived chat. Send a new message."
                    )
            if payload is not None and payload.get("status") == "completed":
                return {"replay": payload, "exclude_ids": []}
            if key in self._active_assistants:
                raise StateConflictError("This assistant request is already running")
            self._require_idle_assistants(connection, workspace_id, kind=kind)
            if row is None:
                user_id = None
                if kind == "main":
                    cursor = connection.execute(
                        "INSERT INTO conversations(workspace_id,role,content) VALUES (?,'user',?)",
                        (workspace_id, message),
                    )
                    user_id = cursor.lastrowid
                elif kind == "dax":
                    cursor = connection.execute(
                        "INSERT INTO events(workspace_id,event_type,payload) VALUES (?,?,?)",
                        (
                            workspace_id,
                            "dax.assistant_requested",
                            json.dumps(
                                request.model_dump(mode="json")
                                if isinstance(request, BaseModel)
                                else request
                            ),
                        ),
                    )
                    user_id = cursor.lastrowid
                connection.execute(
                    "INSERT INTO assistant_requests"
                    "(kind,request_id,workspace_id,request_hash,user_id) "
                    "VALUES (?,?,?,?,?)",
                    (kind, request_id, workspace_id, request_hash, user_id),
                )
                exclude_ids = [user_id] if user_id is not None else []
            else:
                exclude_ids = [row[name] for name in ("user_id", "assistant_id") if row[name]]
            self._active_assistants.add(key)
            return {"replay": None, "exclude_ids": exclude_ids}

    def validate_assistant_request(self, kind: str, request_id: str, request: object) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_hash,payload,user_id FROM assistant_requests "
                "WHERE kind=? AND request_id=?",
                (kind, request_id),
            ).fetchone()
            data = request.model_dump() if isinstance(request, BaseModel) else request
            if isinstance(data, dict):
                self._validate_chat_scope(
                    connection, kind, str(data["workspace_id"]), data, check_base_hash=False
                )
                if row is not None and row["user_id"] is not None and kind == "main":
                    stored = connection.execute(
                        "SELECT payload FROM objects WHERE kind='workspace' AND id=?",
                        (str(data["workspace_id"]),),
                    ).fetchone()
                    boundaries = json.loads(stored["payload"]).get("chat_boundaries", [])
                    if boundaries and row["user_id"] <= boundaries[-1]:
                        raise StateConflictError(
                            "This request belongs to an archived chat. Send a new message."
                        )
        if row is not None and not self._matches_request(row["request_hash"], request):
            raise StateConflictError("Assistant request ID was reused with different input")
        return bool(
            row is not None
            and row["payload"]
            and json.loads(row["payload"]).get("status") == "completed"
        )

    def require_idle_assistants(self, workspace_id: str, *, kind: str | None = None) -> None:
        with self._lock, self._connect() as connection:
            self._require_idle_assistants(connection, workspace_id, kind=kind)

    def finish_assistant_request(self, kind: str, request_id: str, result: dict[str, Any]) -> None:
        """Commit a reply and its replay receipt together; replace a retry's partial row."""
        key = (kind, request_id)
        try:
            with self._lock, self._connect() as connection:
                if key not in self._active_assistants:
                    return
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM assistant_requests WHERE kind=? AND request_id=?", key
                ).fetchone()
                if row is None:
                    return  # The workspace was deleted while this request was running.
                assistant_id = row["assistant_id"]
                if kind == "main" and result.get("response"):
                    values = (result["response"], json.dumps(result.get("citations", [])))
                    if assistant_id is None:
                        cursor = connection.execute(
                            "INSERT INTO conversations(workspace_id,role,content,citations) "
                            "VALUES (?,'assistant',?,?)",
                            (row["workspace_id"], *values),
                        )
                        assistant_id = cursor.lastrowid
                    else:
                        connection.execute(
                            "UPDATE conversations SET content=?,citations=? WHERE id=?",
                            (*values, assistant_id),
                        )
                elif kind == "dax":
                    connection.execute(
                        "INSERT INTO events(workspace_id,event_type,payload) VALUES (?,?,?)",
                        (row["workspace_id"], "dax.assistant_answered", json.dumps(result)),
                    )
                connection.execute(
                    "UPDATE assistant_requests SET assistant_id=?,payload=? "
                    "WHERE kind=? AND request_id=?",
                    (assistant_id, json.dumps(result), *key),
                )
        finally:
            with self._lock:
                self._active_assistants.discard(key)

    def archive_dax_chat(
        self,
        workspace_id: str,
        snapshot_id: str | None,
        request: BaseModel,
        *,
        expected_base_hash: str,
        idempotency_key: str,
    ) -> None:
        """Atomically persist a scoped archive boundary and even a no-op clear key."""
        request_hash = self.content_hash(request)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT request_hash FROM idempotency WHERE key=?", (idempotency_key,)
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != request_hash:
                    raise StateConflictError("Idempotency key was already used for another request")
                return
            self._require_idle_assistants(connection, workspace_id, kind="dax")
            current = connection.execute(
                "SELECT base_hash FROM objects WHERE kind='workspace' AND id=?", (workspace_id,)
            ).fetchone()
            if current is None or current["base_hash"] != expected_base_hash:
                raise StateConflictError(
                    "Stale workspace base hash; reload before starting a new chat"
                )
            through = previous = 0
            for row in connection.execute(
                "SELECT sequence,event_type,payload FROM events WHERE workspace_id=? AND "
                "event_type IN "
                "('dax.assistant_requested','dax.assistant_answered','dax.chat.cleared')",
                (workspace_id,),
            ):
                payload = json.loads(row["payload"])
                if payload.get("snapshot_id") != snapshot_id:
                    continue
                if row["event_type"] == "dax.chat.cleared":
                    previous = max(previous, int(payload.get("through_sequence", 0)))
                else:
                    through = max(through, int(row["sequence"]))
            if through > previous:
                connection.execute(
                    "INSERT INTO events(workspace_id,event_type,payload) VALUES (?,?,?)",
                    (
                        workspace_id,
                        "dax.chat.cleared",
                        json.dumps(
                            {
                                "snapshot_id": snapshot_id,
                                "through_sequence": through,
                            }
                        ),
                    ),
                )
            connection.execute(
                "INSERT INTO idempotency(key,request_hash,response_kind,response_id) "
                "VALUES (?,?,?,?)",
                (idempotency_key, request_hash, "workspace", workspace_id),
            )

    def messages(
        self,
        workspace_id: str,
        limit: int = 100,
        after_id: int = 0,
        oldest_first: bool = False,
    ) -> list[dict[str, object]]:
        query = (
            "SELECT c.id,c.role,c.content,c.citations,c.created_at,a.request_id,a.payload "
            "FROM conversations c LEFT JOIN assistant_requests a ON a.kind='main' "
            "AND a.workspace_id=c.workspace_id AND "
            "((c.role='user' AND a.user_id=c.id) OR (c.role='assistant' AND a.assistant_id=c.id)) "
            "WHERE c.workspace_id = ? AND c.id > ? ORDER BY c.id ASC LIMIT ?"
            if oldest_first
            else "SELECT c.id,c.role,c.content,c.citations,c.created_at,a.request_id,a.payload "
            "FROM conversations c LEFT JOIN assistant_requests a ON a.kind='main' "
            "AND a.workspace_id=c.workspace_id AND "
            "((c.role='user' AND a.user_id=c.id) OR (c.role='assistant' AND a.assistant_id=c.id)) "
            "WHERE c.workspace_id = ? AND c.id > ? ORDER BY c.id DESC LIMIT ?"
        )
        with self._connect() as connection:
            rows = connection.execute(
                query,
                (workspace_id, after_id, min(max(limit, 1), 200)),
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows if oldest_first else reversed(rows):
            item: dict[str, object] = {
                "id": int(row["id"]),
                "role": str(row["role"]),
                "content": str(row["content"]),
                "citations": json.loads(str(row["citations"])),
                "created_at": str(row["created_at"]),
            }
            if row["request_id"]:
                item["request_id"] = str(row["request_id"])
                status = (
                    json.loads(row["payload"]).get("status") if row["payload"] else "interrupted"
                )
                if status in {"completed", "interrupted", "cancelled", "failed"}:
                    item["status"] = status
            result.append(item)
        return result


def _model_id(value: BaseModel) -> str:
    raw_id = value.model_dump().get("id")
    if raw_id is None:
        raise TypeError("Stored models require an id field")
    return str(raw_id)
