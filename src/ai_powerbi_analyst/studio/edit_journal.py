"""Write-ahead journal of edit batches, and recovery after a crash or a restart.

One batch changes several things that cannot be committed together: Power BI Desktop's
process, desktop/Report.pbix, the revision index and Studio's own view of the report. A
batch therefore leaves a trail in reports/<id>/journal.jsonl, one JSON object per line,
appended and flushed to disk before the step it announces. A restart reads the trail and
finishes or undoes whatever was in flight.

States, in the order a batch passes through them (a report-only batch skips the model
ones):

    planned -> desktop_saved -> desktop_closed -> adopted -> staged -> committed -> placed
    -> [model_applied -> model_saved -> adopted -> staged ->] ... -> reopened -> done

    failed / rolled_back end a batch that changed nothing (or was undone).

Why recovery inspects the store instead of trusting the last line: every step is written
before its side effect, so the last line says what was about to happen, not what did. A
crash between "revision recorded" and its journal line leaves the row in index.json and the
old state in the journal. ``recover`` therefore asks the store whether a revision with the
staged file's hash exists, and rolls forward when it does. All steps are idempotent, so
running one twice is harmless.

Rules recovery never breaks:
- It never kills or closes Power BI Desktop. An instance that holds the working copy is
  reported back to the caller and its file is left alone, because Desktop locks it and may
  hold work Studio cannot see.
- It never discards a save the user made in Desktop: before rolling back, a working copy
  that Studio did not write is adopted as a revision.
- A torn last line (a crash in the middle of an append) is ignored, and the next append
  starts on a new line.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .desktop_session import DesktopInstance, DesktopSessionError

if TYPE_CHECKING:
    from .report_revisions import RevisionStore

JOURNAL_NAME = "journal.jsonl"
STATES = (
    "planned",
    "desktop_saved",
    "desktop_closed",
    "adopted",
    "staged",
    "committed",
    "placed",
    "model_applied",
    "model_saved",
    "reopened",
    "done",
    "failed",
    "rolled_back",
)
TERMINAL = frozenset({"done", "failed", "rolled_back"})
# Once Desktop has been opened on the head for a model change, whatever it saved into the
# working copy is Studio's own by-product, never the user's work.
_MODEL_PHASE = frozenset({"model_applied", "model_saved"})
MAX_JOURNAL_BYTES = 1024 * 1024
KEEP_BATCHES = 200
_RETRIES = 20


class RecoveryPort(Protocol):
    """What recovery needs from Desktop: which instance holds a file. Nothing else."""

    def find(self, path: Path) -> DesktopInstance | None: ...


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass
class BatchRecord:
    """One batch as the journal describes it."""

    id: str
    state: str = "planned"
    key: str | None = None
    kind: str = "edit"  # edit | model_edit | undo | restore
    parent: str | None = None  # the head the batch builds on; ``adopted`` moves it
    ops: list[str] = field(default_factory=list)
    desktop_open: bool = False
    started: str = ""
    updated: str = ""
    states: list[str] = field(default_factory=list)
    staged: list[dict[str, str]] = field(default_factory=list)
    revision: str | None = None
    target: str | None = None
    reason: str | None = None
    receipt: dict[str, Any] | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def staged_hash(self) -> str | None:
        """The hash of the file that becomes the revision: the last one staged."""
        return self.staged[-1]["hash"] if self.staged else None

    @property
    def model_phase(self) -> bool:
        return any(state in _MODEL_PHASE for state in self.states)


def _fold(records: list[dict[str, Any]]) -> dict[str, BatchRecord]:
    batches: dict[str, BatchRecord] = {}
    for record in records:
        batch_id = record.get("batch")
        state = record.get("state")
        if not isinstance(batch_id, str) or state not in STATES:
            continue
        batch = batches.setdefault(batch_id, BatchRecord(id=batch_id, started=record.get("ts", "")))
        batch.state = str(state)
        batch.states.append(str(state))
        batch.updated = str(record.get("ts", ""))
        for name in ("key", "kind", "parent", "revision", "target", "reason"):
            value = record.get(name)
            if isinstance(value, str):
                setattr(batch, name, value)
        ops = record.get("ops")
        if isinstance(ops, list):
            batch.ops = [str(item) for item in ops]
        if isinstance(record.get("desktop_open"), bool):
            batch.desktop_open = record["desktop_open"]
        if state == "staged" and isinstance(record.get("hash"), str):
            batch.staged.append({"path": str(record.get("path", "")), "hash": record["hash"]})
        if isinstance(record.get("receipt"), dict):
            batch.receipt = record["receipt"]
    return batches


class EditJournal:
    """reports/<id>/journal.jsonl. Callers hold the report's edit.lock while appending."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # --- writing -----------------------------------------------------------------------

    def append(self, batch: str, state: str, **data: Any) -> None:
        """Add one line and flush it to disk before returning."""
        if state not in STATES:
            raise ValueError(f"Unknown batch state {state!r}")
        record = {"v": 1, "batch": batch, "state": state, "ts": _now(), **data}
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = (line + "\n").encode("utf-8")
        if self._torn():
            payload = b"\n" + payload
        for attempt in range(_RETRIES):
            try:
                with self.path.open("ab") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                return
            except PermissionError as error:
                # An antivirus or indexer scan holds the file for a moment (WinError 5, 32).
                if getattr(error, "winerror", None) not in (5, 32) or attempt == _RETRIES - 1:
                    raise
                time.sleep(0.25)

    def _torn(self) -> bool:
        try:
            with self.path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    return False
                handle.seek(-1, os.SEEK_END)
                return handle.read(1) != b"\n"
        except FileNotFoundError:
            return False

    # --- reading -----------------------------------------------------------------------

    def records(self) -> list[dict[str, Any]]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return []
        found: list[dict[str, Any]] = []
        for line in raw.split(b"\n"):
            if not line.strip():
                continue
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue  # a torn line: the append it belonged to never completed
            if isinstance(value, dict):
                found.append(value)
        return found

    def batches(self) -> dict[str, BatchRecord]:
        return _fold(self.records())

    def open_batches(self) -> list[BatchRecord]:
        return [batch for batch in self.batches().values() if not batch.terminal]

    def find_key(self, key: str) -> BatchRecord | None:
        """The newest batch that used this idempotency key."""
        found: BatchRecord | None = None
        for batch in self.batches().values():
            if batch.key == key:
                found = batch
        return found

    def pinned_revisions(self) -> set[str]:
        """Revision ids that pruning must keep: the parents of batches still in flight."""
        pinned: set[str] = set()
        for batch in self.open_batches():
            if batch.parent:
                pinned.add(batch.parent)
        return pinned

    # --- housekeeping ------------------------------------------------------------------

    def trim(self, keep: int = KEEP_BATCHES) -> None:
        """Drop the oldest finished batches once the file grows large; open ones stay."""
        try:
            if self.path.stat().st_size <= MAX_JOURNAL_BYTES:
                return
        except FileNotFoundError:
            return
        records = self.records()
        batches = _fold(records)
        finished = [b.id for b in batches.values() if b.terminal]
        dropped = set(finished[:-keep]) if len(finished) > keep else set()
        if not dropped:
            return
        from .report_revisions import _write_atomic  # noqa: PLC0415 - avoids an import cycle

        kept = [r for r in records if r.get("batch") not in dropped]
        payload = "".join(
            json.dumps(r, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
            for r in kept
        )
        _write_atomic(self.path, payload.encode("utf-8"))


# --- recovery -------------------------------------------------------------------------


@dataclass
class BatchRecovery:
    batch: str
    from_state: str
    action: str  # rolled_forward | rolled_back | left_open
    detail: str = ""


@dataclass
class RecoveryReport:
    batches: list[BatchRecovery] = field(default_factory=list)
    # The Desktop instance that has the working copy open, if one does. Recovery never
    # closes it; the caller starts tracking it again.
    desktop: DesktopInstance | None = None
    removed_staging: int = 0


def _number(revision_id: str | None) -> int:
    if revision_id and revision_id[1:].isdigit():
        return int(revision_id[1:])
    return -1


def _committed_row(store: RevisionStore, batch: BatchRecord) -> str | None:
    """The id of the revision this batch recorded, found in the store; None if there is none."""
    floor = _number(batch.parent)
    for row in store.history():  # newest first
        if _number(row.id) <= floor:
            break
        if batch.kind in ("undo", "restore"):
            if row.author == batch.kind and row.parent == batch.parent:
                return row.id
        elif row.author == "studio" and row.blob == batch.staged_hash:
            return row.id
    return None


def _holder(port: RecoveryPort | None, store: RevisionStore) -> DesktopInstance | None:
    if port is None:
        return None
    try:
        return port.find(store.working_copy)
    except DesktopSessionError:
        return None


def _roll_forward(
    journal: EditJournal,
    store: RevisionStore,
    batch: BatchRecord,
    revision: str,
    instance: DesktopInstance | None,
) -> BatchRecovery:
    detail = f"recorded as {revision}"
    if instance is None:
        try:
            # After a model change the working copy holds Desktop's saved file of this same
            # batch, which is not the head (the report layer was rewritten on top of it) and
            # is not a user save: it must be replaced, never adopted.
            store.materialize(force=batch.model_phase)
        except (ValueError, RuntimeError, OSError) as error:
            # The revision is recorded, so the batch is complete; the working copy is
            # refreshed by "Open in Power BI", which materializes under the lock.
            detail += f"; the working copy will be refreshed when Power BI is opened ({error})"
        else:
            journal.append(batch.id, "placed", recovered=True)
    else:
        detail += "; Power BI Desktop has the working copy open, so it is left as it is"
    journal.append(
        batch.id,
        "done",
        recovered=True,
        receipt={"revision": revision, "recovered": True, "summaries": list(batch.ops)},
    )
    return BatchRecovery(batch.id, batch.state, "rolled_forward", detail)


def _roll_back(
    journal: EditJournal,
    store: RevisionStore,
    batch: BatchRecord,
    instance: DesktopInstance | None,
    equivalent: Callable[[Path, Path], bool] | None = None,
) -> BatchRecovery:
    if instance is not None:
        journal.append(
            batch.id,
            "rolled_back",
            reason="Power BI Desktop still has the report open",
            desktop_open=True,
        )
        return BatchRecovery(
            batch.id,
            batch.state,
            "left_open",
            "Power BI Desktop still has this report open; the revision history is unchanged",
        )
    detail = "the report was left as it was before the change"
    try:
        if batch.model_phase:
            store.materialize(force=True)
        else:
            # Desktop may have saved the user's work at their request just before the crash;
            # its own normalization of a Studio-written file is not work.
            from . import report_revisions  # noqa: PLC0415 - avoids an import cycle

            store.adopt_working_copy(equivalent=equivalent or report_revisions.semantic_equal)
            store.materialize()
    except (ValueError, RuntimeError, OSError) as error:
        detail = f"the working copy could not be reset ({error})"
    journal.append(batch.id, "rolled_back", recovered=True, reason=detail)
    return BatchRecovery(batch.id, batch.state, "rolled_back", detail)


def recover(
    report_root: Path,
    store: RevisionStore,
    port: RecoveryPort | None = None,
    equivalent: Callable[[Path, Path], bool] | None = None,
) -> RecoveryReport:
    """Finish or undo every batch that was in flight when the sidecar stopped.

    Runs under the report's edit lock. Committed batches roll forward, the rest roll back
    (see the module docstring). Orphan files in staging/ are deleted afterwards, and a
    Desktop instance that has the working copy open is reported, never touched.
    ``equivalent`` tells Desktop's own normalization from a user's save (default: the
    report-layer check in report_revisions).
    """
    journal = EditJournal(Path(report_root) / JOURNAL_NAME)
    report = RecoveryReport()
    with store.lock():
        instance = _holder(port, store)
        report.desktop = instance
        for batch in journal.open_batches():
            revision = _committed_row(store, batch)
            if revision is not None:
                report.batches.append(_roll_forward(journal, store, batch, revision, instance))
            else:
                report.batches.append(_roll_back(journal, store, batch, instance, equivalent))
        report.removed_staging = store.cleanup(max_age=-1)
    return report
