"""SQLite job and progress journal resilient to Streamlit reruns."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import psutil


def _process_owner_id() -> str:
    process = psutil.Process(os.getpid())
    return f"pid:{process.pid}:{process.create_time():.6f}"


_PROCESS_OWNER_ID = _process_owner_id()


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class IdempotencyConflict(ValueError):
    """An idempotency key was reused for a different operation."""


class InvalidJobTransition(RuntimeError):
    """The requested job transition is not legal."""


class ProgressRegression(ValueError):
    """A progress update moved backwards."""


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    kind: str
    idempotency_key: str
    status: JobStatus
    request_json: str
    result_json: str | None
    error: str | None
    created_at: str
    updated_at: str
    owner_id: str | None = None

    @property
    def request(self) -> dict[str, Any]:
        value = json.loads(self.request_json)
        if not isinstance(value, dict):
            raise TypeError("job request must be a JSON object")
        return value


@dataclass(frozen=True, slots=True)
class EventRecord:
    sequence: int
    job_id: str
    phase: str
    message: str
    progress: float
    created_at: str


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class JobJournal:
    def __init__(
        self,
        database_path: str | Path,
        *,
        owner_id: str | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.owner_id = owner_id or _PROCESS_OWNER_ID
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    owner_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    phase TEXT NOT NULL,
                    message TEXT NOT NULL,
                    progress REAL NOT NULL CHECK(progress >= 0 AND progress <= 1),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_job_sequence
                    ON events(job_id, sequence);
                CREATE TABLE IF NOT EXISTS workspace_context (
                    project_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "owner_id" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN owner_id TEXT")

    def create_or_get(
        self, kind: str, idempotency_key: str, request: Mapping[str, Any]
    ) -> JobRecord:
        # A prior crashed process is recoverable immediately from its dead PID
        # lease. Legacy ownerless rows retain the conservative age threshold.
        self.recover_abandoned(older_than_seconds=300)
        payload = _canonical_json(request)
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if row is not None:
                record = self._job(row)
                if record.kind != kind or record.request_json != payload:
                    raise IdempotencyConflict(
                        "idempotency key already belongs to a different request"
                    )
                if record.status is not JobStatus.FAILED:
                    return record
                # A failed, safely terminated transaction may be retried by an
                # explicit later submission. Preserve the failed attempt under
                # an immutable historical key, then issue a fresh job for the
                # same logical idempotency key.
                historical_key = f"{idempotency_key}#failed#{record.id}"
                connection.execute(
                    "UPDATE jobs SET idempotency_key = ? WHERE id = ?",
                    (historical_key, record.id),
                )
            job_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO jobs
                   (id, kind, idempotency_key, status, request_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (job_id, kind, idempotency_key, JobStatus.PENDING.value, payload, now, now),
            )
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert row is not None
            return self._job(row)

    def get(self, job_id: str) -> JobRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown job: {job_id}")
        return self._job(row)

    def claim(self, job_id: str) -> bool:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE jobs SET status = ?, owner_id = ?, updated_at = ?
                   WHERE id = ? AND status = ?""",
                (
                    JobStatus.RUNNING.value,
                    self.owner_id,
                    now,
                    job_id,
                    JobStatus.PENDING.value,
                ),
            ).rowcount
            if changed:
                connection.execute(
                    """INSERT INTO events(job_id, phase, message, progress, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (job_id, "start", "Started", 0.0, now),
                )
        return bool(changed)

    def append_event(
        self, job_id: str, phase: str, message: str, *, progress: float
    ) -> EventRecord:
        if not 0.0 <= progress <= 1.0:
            raise ValueError("progress must be between 0 and 1")
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(f"unknown job: {job_id}")
            if JobStatus(job["status"]) is not JobStatus.RUNNING:
                raise InvalidJobTransition("progress can be appended only to a running job")
            previous = connection.execute(
                "SELECT progress FROM events WHERE job_id = ? ORDER BY sequence DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if previous is not None and progress < float(previous["progress"]):
                raise ProgressRegression("progress cannot move backwards")
            cursor = connection.execute(
                """INSERT INTO events(job_id, phase, message, progress, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (job_id, phase, message, progress, now),
            )
            connection.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (now, job_id))
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return an event sequence")
            sequence = cursor.lastrowid
        return EventRecord(sequence, job_id, phase, message, progress, now)

    def succeed(self, job_id: str, result: Mapping[str, Any]) -> JobRecord:
        return self._finish(job_id, JobStatus.SUCCEEDED, result=result, error=None)

    def fail(self, job_id: str, error: str) -> JobRecord:
        return self._finish(job_id, JobStatus.FAILED, result=None, error=error)

    def _finish(
        self,
        job_id: str,
        status: JobStatus,
        *,
        result: Mapping[str, Any] | None,
        error: str | None,
    ) -> JobRecord:
        now = _now()
        result_json = _canonical_json(result) if result is not None else None
        phase = "complete" if status is JobStatus.SUCCEEDED else "failed"
        message = "Completed" if status is JobStatus.SUCCEEDED else (error or "Failed")
        progress = 1.0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE jobs SET status = ?, result_json = ?, error = ?, updated_at = ?
                   WHERE id = ? AND status = ?""",
                (status.value, result_json, error, now, job_id, JobStatus.RUNNING.value),
            ).rowcount
            if not changed:
                current = connection.execute(
                    "SELECT status FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if current is None:
                    raise KeyError(f"unknown job: {job_id}")
                raise InvalidJobTransition(
                    f"cannot finish {current['status']} job as {status.value}"
                )
            connection.execute(
                """INSERT INTO events(job_id, phase, message, progress, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (job_id, phase, message[:500], progress, now),
            )
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert row is not None
            return self._job(row)

    def events(self, job_id: str) -> list[EventRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE job_id = ? ORDER BY sequence", (job_id,)
            ).fetchall()
        return [self._event(row) for row in rows]

    def save_context(self, project_id: str, state: Mapping[str, Any]) -> None:
        if not project_id.strip():
            raise ValueError("project_id cannot be empty")
        payload = _canonical_json(state)
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO workspace_context(project_id, state_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(project_id) DO UPDATE SET
                       state_json = excluded.state_json,
                       updated_at = excluded.updated_at""",
                (project_id, payload, now),
            )

    def load_context(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM workspace_context WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(str(row["state_json"]))
        if not isinstance(value, dict):
            raise TypeError("workspace context must be a JSON object")
        return value

    def recover_abandoned(self, *, older_than_seconds: int = 300) -> list[JobRecord]:
        """Fail stale RUNNING jobs closed after their owning process disappeared.

        Recovery never replays a write. The report transaction layer separately
        restores an interrupted directory swap before a user can submit a new plan.
        """

        if older_than_seconds < 0:
            raise ValueError("older_than_seconds cannot be negative")
        now = datetime.now(UTC)
        cutoff = (now - timedelta(seconds=older_than_seconds)).isoformat(timespec="milliseconds")
        recovered_ids: list[str] = []
        error = "Operation abandoned after its worker stopped"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT id, owner_id, updated_at FROM jobs
                   WHERE status = ? ORDER BY created_at""",
                (JobStatus.RUNNING.value,),
            ).fetchall()
            for row in rows:
                job_id = str(row["id"])
                owner_id = str(row["owner_id"]) if row["owner_id"] is not None else None
                if owner_id == self.owner_id or _owner_process_is_alive(owner_id):
                    continue
                if owner_id is None and str(row["updated_at"]) > cutoff:
                    continue
                changed = connection.execute(
                    """UPDATE jobs SET status = ?, error = ?, updated_at = ?
                       WHERE id = ? AND status = ?""",
                    (
                        JobStatus.FAILED.value,
                        error,
                        now.isoformat(timespec="milliseconds"),
                        job_id,
                        JobStatus.RUNNING.value,
                    ),
                ).rowcount
                if not changed:
                    continue
                connection.execute(
                    """INSERT INTO events(job_id, phase, message, progress, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        job_id,
                        "failed",
                        error,
                        1.0,
                        now.isoformat(timespec="milliseconds"),
                    ),
                )
                recovered_ids.append(job_id)
            recovered_rows = [
                connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                for job_id in recovered_ids
            ]
        return [self._job(row) for row in recovered_rows if row is not None]

    @staticmethod
    def _job(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=str(row["id"]),
            kind=str(row["kind"]),
            idempotency_key=str(row["idempotency_key"]),
            status=JobStatus(row["status"]),
            request_json=str(row["request_json"]),
            result_json=str(row["result_json"]) if row["result_json"] is not None else None,
            error=str(row["error"]) if row["error"] is not None else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            owner_id=str(row["owner_id"]) if row["owner_id"] is not None else None,
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> EventRecord:
        return EventRecord(
            sequence=int(row["sequence"]),
            job_id=str(row["job_id"]),
            phase=str(row["phase"]),
            message=str(row["message"]),
            progress=float(row["progress"]),
            created_at=str(row["created_at"]),
        )


def _owner_process_is_alive(owner_id: str | None) -> bool:
    if owner_id is None:
        return False
    match = re.fullmatch(r"pid:(\d+):(\d+(?:\.\d+)?)", owner_id)
    if match is None:
        return False
    pid = int(match.group(1))
    expected_start = float(match.group(2))
    try:
        process = psutil.Process(pid)
        return abs(process.create_time() - expected_start) < 0.01
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
