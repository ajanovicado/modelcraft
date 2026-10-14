"""Whole-file revision history of one imported report, and Desktop's working copy of it.

Why whole files: a Power BI Desktop save rewrites the entire PBIX. In the 2026-09-28
experiments on the Agents Performance report (Desktop 2.157), a save changed the DataModel
bytes with no model change and deleted a resource nothing referenced (exp1's background
PNG), so no part-level diff survives a round trip through Desktop. Every version is a
complete PBIX stored once under its sha256, which makes undo back to an earlier state free.

Layout under reports/<report-id>/:
    original.pbix          the imported file, never written; it is revision r0000
    revisions/<sha>.pbix   every later version, written once and never modified
    revisions/index.json   the history: rows oldest first, the head, the placement
    staging/               files being prepared; commit consumes them
    desktop/Report.pbix    the copy Power BI Desktop opens
    edit.lock              serializes history writes across threads and processes

Why the working copy is tracked ("placed"): Desktop locks the PBIX for as long as it is
open (open r+b and os.replace both failed with access denied), and the user may save
there at any time. Studio records which revision it last wrote into desktop/Report.pbix.
A working copy with any other content is a Desktop save, and it is adopted as a revision
before Studio writes over it; otherwise the user's work would be lost. Desktop also
normalizes a Studio-written file about 10 s after opening it (hasUnsavedChanges turns
true), so a Desktop save after every open is expected and adopted like any other.

Why the retries: on Windows, os.replace fails with ERROR_ACCESS_DENIED (5) while any
handle has the destination open without FILE_SHARE_DELETE, which is how Python opens
files, so a Studio reader of index.json, an antivirus scan or the search indexer can
each block it for a moment. ERROR_SHARING_VIOLATION (32) comes from the same holders.
Writes retry 20 x 250 ms. Any other PermissionError is raised at once, and so is one
that outlasts the retries, such as Desktop's lock on its open file.

Crash safety: a version file is flushed to disk and renamed into revisions/ before its
row is appended, and index.json is replaced atomically. A crash therefore leaves at worst
an unreferenced file, which prune removes. A damaged index.json is reported and never
reset, because a reset would drop the user's history. The batch that surrounds a commit
(Desktop, the working copy, Studio's view) is journaled by edit_journal, which also tells
prune which revisions an unfinished batch still needs.

Normalization: the placement holds a revision id and the hash of the file Desktop actually
wrote, which differs from the revision's blob once Desktop has opened and saved it. A copy
that a caller-supplied ``equivalent`` check calls the same report as the placed revision is
recorded as a new placement, never as a revision: otherwise every open in Desktop would add
a "Desktop" row to History.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import tempfile
import time
import zipfile
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from filelock import FileLock, Timeout
from pydantic import Field, ValidationError

from ai_powerbi_analyst.contracts import ClosedModel, Sha256
from ai_powerbi_analyst.integrity import sha256_file

from .edit_journal import JOURNAL_NAME, EditJournal
from .state import StateConflictError

ORIGINAL_ID = "r0000"
LOCK_TIMEOUT = 120.0
# Windows transient holders: 20 x 250 ms outlasts an antivirus or indexer scan.
RETRY_ATTEMPTS = 20
RETRY_DELAY = 0.25
# Desktop keeps its lock while the file is open, so reading the working copy waits only
# briefly: waiting the full 5 s would only delay the "close Power BI Desktop" message.
WORKING_COPY_READ_ATTEMPTS = 4
STAGING_MAX_AGE = 24 * 3600.0
MAX_INDEX_BYTES = 32 * 1024**2
DEFAULT_KEEP = 40
# A locked version file is normally an antivirus scan; prune is best effort, so it does not
# wait the full 5 s per file that a history write does.
PRUNE_ATTEMPTS = 4
_TRANSIENT_WINERRORS = frozenset({5, 32})  # ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION
_BLOB_NAME = re.compile(r"([0-9a-f]{64})\.pbix")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION_ID = re.compile(r"r(\d{4,})")
_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,16}")
_CHUNK = 1024 * 1024
_DESKTOP_BUSY = (
    "Power BI Desktop has this report open, so Studio cannot read or replace its copy. "
    "Save and close the report in Power BI Desktop, then try again."
)

RevisionAuthor = Literal["import", "studio", "desktop", "undo", "restore"]
CommitAuthor = Literal["studio", "desktop"]


# --- errors ---------------------------------------------------------------------------


class RevisionError(ValueError):
    """The history cannot do what was asked: an unknown or pruned version, nothing to undo."""


class RevisionIndexError(RevisionError):
    """revisions/index.json is unreadable or inconsistent. It is never reset silently."""


class RevisionConflict(StateConflictError):
    """The head moved after the caller read it: another edit or a Desktop save came first."""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(
            f"This report changed while the edit was being prepared (expected version "
            f"{expected}, now {actual}). Review the latest version and try again."
        )
        self.expected = expected
        self.actual = actual


class RevisionBusy(StateConflictError):
    """Another thread or process held edit.lock for longer than the timeout."""


class UserChangesPending(StateConflictError):
    """desktop/Report.pbix holds a Desktop save that Studio has not recorded yet."""


class WorkingCopyBusy(StateConflictError):
    """desktop/Report.pbix is locked, normally because Power BI Desktop has it open."""


# --- records --------------------------------------------------------------------------


class Revision(ClosedModel):
    """One complete version of the report; ``blob`` is the sha256 of its PBIX file."""

    id: str = Field(pattern=r"^r\d{4,}$")
    blob: Sha256
    parent: str | None = None
    author: RevisionAuthor
    summary: str
    operations: list[dict[str, Any]] = Field(default_factory=list)
    created: str
    size: int = Field(ge=0)
    # A Desktop save made on top of a revision older than the head: the save replaced
    # Studio's later edits, and the service tells the user so.
    conflict: bool = False
    # For undo and restore rows: the revision whose content the row brought back.
    restores: str | None = None
    # Desktop's product version when it took part in the change (a model edit, or a
    # Desktop save). Desktop updates monthly, and a schema or automation change in a new
    # version explains a batch that stops working, so the version is kept with the row.
    desktop_version: str | None = None


class Placement(ClosedModel):
    """The revision Studio last wrote into desktop/Report.pbix, with that file's hash.

    After Desktop normalizes a Studio-written file the hash is Desktop's copy of the same
    content, so ``hash`` need not equal ``revision``'s blob.
    """

    revision: str
    hash: Sha256


class Delivered(ClosedModel):
    """The last head Studio wrote over the user's own file ("Replace original")."""

    path: str
    hash: Sha256


class RevisionIndex(ClosedModel):
    """revisions/index.json. ``revisions`` is oldest first and ids are r0000, r0001, ..."""

    version: Literal[1] = 1
    original: Sha256
    head: str
    placed: Placement | None = None
    # Optional so that indexes written before delivery existed still load.
    delivered: Delivered | None = None
    revisions: list[Revision] = Field(min_length=1)


class HistoryEntry(ClosedModel):
    """A revision with what the History pane needs to know about it."""

    revision: Revision
    available: bool  # its file is still on disk (pruning removes old ones)
    is_head: bool
    is_placed: bool  # the working copy was last written from this revision
    can_undo: bool  # only the newest change can be undone; older ones are restored


WorkingCopyState = Literal["absent", "placed", "placed_stale", "user_changes"]
Equivalent = Callable[[Path, Path], bool]


# --- file helpers ---------------------------------------------------------------------


def _transient(error: PermissionError) -> bool:
    return getattr(error, "winerror", None) in _TRANSIENT_WINERRORS


def _retry[T](action: Callable[[], T], attempts: int | None = None) -> T:
    """Run ``action``, retrying the Windows access-denied and sharing violations."""
    total = RETRY_ATTEMPTS if attempts is None else max(1, attempts)
    for attempt in range(total):
        try:
            return action()
        except PermissionError as error:
            if not _transient(error) or attempt == total - 1:
                raise
            time.sleep(RETRY_DELAY)
    raise AssertionError("unreachable")


def _flush_to_disk(path: Path) -> None:
    # FlushFileBuffers needs a handle with write access, hence r+b.
    with _retry(lambda: path.open("r+b")) as handle:
        os.fsync(handle.fileno())


def _write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to a temporary file beside ``path``, flush it, then replace ``path``."""
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        _retry(lambda: os.replace(temporary, path))
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _copy_hashing(source: Path, target: Path, *, attempts: int | None = None) -> str:
    """Copy ``source`` over ``target``, flush the copy to disk and return its sha256.

    The hash is of the bytes written, so it is the copy's identity even if ``source``
    changed while it was read.
    """
    digest = hashlib.sha256()
    with _retry(lambda: source.open("rb"), attempts) as reader, target.open("wb") as out:
        for block in iter(lambda: reader.read(_CHUNK), b""):
            digest.update(block)
            out.write(block)
        out.flush()
        os.fsync(out.fileno())
    return digest.hexdigest()


def _readable_zip(path: Path) -> bool:
    """True when every member reads back with a matching CRC: a complete Desktop save."""
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.testzip() is None
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError, NotImplementedError, RuntimeError):
        return False


def _pbix_diff() -> Any:
    """The pbix_diff module, or None until it exists (another workstream adds it)."""
    try:
        return importlib.import_module("ai_powerbi_analyst.studio.pbix_diff")
    except ImportError:
        return None


def semantic_equal(a: Path, b: Path) -> bool:
    """True when two PBIX files hold the same report, ignoring what a Desktop save changes.

    Desktop rewrites the whole file on every save (key order, 17-digit floats, the DataModel
    bytes, unreferenced resources), so a byte comparison would call every save a change.
    Without pbix_diff the answer is "different", which errs towards recording a Desktop
    revision rather than losing the user's work. Tests replace this function.

    pbix_diff leaves DataModel bytes out of "empty" (they change on every save), so this
    compares the report layer only. A measure the user added in Desktop is therefore not
    seen here; ReportEditor adds the model check (see ``ReportEditor._equivalent``).
    """
    module = _pbix_diff()
    if module is None:
        return False
    return bool(module.semantic_diff(a, b).empty)


def survival(written: Path, saved: Path, baseline: Path | None = None) -> list[str]:
    """What Studio wrote in ``written`` that Desktop's save in ``saved`` dropped.

    ``baseline`` is the file Studio started from: what it shares with ``written`` is not
    blamed, because Desktop also rewrites things Studio never touched (it deletes resources
    nothing references).

    Empty when pbix_diff is not available: the check cannot run, and it is not a reason
    to refuse an edit.
    """
    module = _pbix_diff()
    if module is None:
        return []
    if baseline is None:
        found = module.survival(written, saved)
    else:
        found = module.survival(written, saved, baseline)
    return [str(item) for item in found]


def _plain_operations(operations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    try:
        text = json.dumps([dict(op) for op in operations], ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise RevisionError("Edit operations must be plain JSON values") from error
    plain: list[dict[str, Any]] = json.loads(text)
    return plain


def _revision_id(number: int) -> str:
    return f"r{number:04d}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _row(index: RevisionIndex, revision_id: str) -> Revision:
    match = _REVISION_ID.fullmatch(revision_id)
    number = int(match.group(1)) if match else -1
    if 0 <= number < len(index.revisions) and index.revisions[number].id == revision_id:
        return index.revisions[number]
    raise RevisionError(f"This report has no version {revision_id!r}")


def _expect(index: RevisionIndex, expected_head: str) -> None:
    if expected_head != index.head:
        raise RevisionConflict(expected_head, index.head)


def _with_row(
    index: RevisionIndex,
    *,
    blob: str,
    size: int,
    author: RevisionAuthor,
    summary: str,
    operations: list[dict[str, Any]],
    parent: str,
    conflict: bool = False,
    restores: str | None = None,
    desktop_version: str | None = None,
) -> tuple[RevisionIndex, Revision]:
    row = Revision(
        id=_revision_id(len(index.revisions)),
        blob=blob,
        parent=parent,
        author=author,
        summary=summary,
        operations=operations,
        created=_now(),
        size=size,
        conflict=conflict,
        restores=restores,
        desktop_version=desktop_version,
    )
    updated = index.model_copy(update={"revisions": [*index.revisions, row], "head": row.id})
    return updated, row


def _undo_step(index: RevisionIndex) -> tuple[Revision, Revision]:
    """The change an undo reverts and the revision it goes back to.

    Undo rows are followed to the revision they brought back, then the step goes to the
    row before that one, which was the head before it. Taking ``head.parent`` alone would
    make a second undo redo the first (an undo row's parent is the head it undid).
    Restore rows are ordinary changes here: undoing one returns to the head before it.
    """
    current = _row(index, index.head)
    while current.author == "undo" and current.restores is not None:
        current = _row(index, current.restores)
    if current.id == ORIGINAL_ID:
        raise RevisionError("There is nothing to undo")
    # Ids are positions (checked on load), and a restored target is always older.
    return current, index.revisions[int(current.id[1:]) - 1]


def _effective_chain(index: RevisionIndex, start: str) -> list[str]:
    """Ids of the changes that make up ``start``'s content, newest first.

    An undo or restore row has the content of the revision it brought back, so the walk
    continues from that revision. What is left is the run of Studio and Desktop changes
    that are in effect, ending at r0000 (which is not a change, so it is not listed).
    """
    chain: list[str] = []
    current = _row(index, start)
    while True:
        while current.restores is not None and current.author in ("undo", "restore"):
            current = _row(index, current.restores)
        if current.parent is None:
            return chain
        chain.append(current.id)
        current = _row(index, current.parent)


# --- the store ------------------------------------------------------------------------


class RevisionStore:
    """The revision chain of one imported report, stored under ``root``.

    Every history write takes edit.lock. The lock is reentrant per thread and shared by
    every store for the same folder in this process, so callers may hold ``lock()``
    around a whole read-edit-commit sequence while the methods take it again inside.
    Reads (``head``, ``history``, ``placed``) need no lock: index.json is only ever
    replaced whole.
    """

    def __init__(self, root: Path, original: Path, original_hash: str) -> None:
        if not _SHA256.fullmatch(original_hash):
            raise ValueError("original_hash must be a lowercase sha256")
        self.root = Path(root)
        self.original = Path(original)
        self.original_hash = original_hash
        self.revisions_dir = self.root / "revisions"
        self.index_path = self.revisions_dir / "index.json"
        self.staging_dir = self.root / "staging"
        # A process-wide instance per path: nested use from two stores cannot deadlock.
        self._lock = FileLock(str(self.root / "edit.lock"), is_singleton=True)

    @property
    def working_copy(self) -> Path:
        return self.root / "desktop" / "Report.pbix"

    @property
    def journal_path(self) -> Path:
        return self.root / JOURNAL_NAME

    # --- locking -----------------------------------------------------------------------

    @contextmanager
    def lock(self, timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
        """Hold edit.lock; raises RevisionBusy after ``timeout`` seconds of waiting."""
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self._lock.acquire(timeout=timeout)
        except Timeout as error:
            raise RevisionBusy(
                "Another change to this report is still being saved. Try again in a moment."
            ) from error
        try:
            yield
        finally:
            self._lock.release()

    # --- reading -----------------------------------------------------------------------

    def head(self) -> Revision:
        index = self._read_index()
        return _row(index, index.head)

    def history(self) -> list[Revision]:
        """Every revision, newest first."""
        return list(reversed(self._read_index().revisions))

    def revision(self, revision_id: str) -> Revision:
        return _row(self._read_index(), revision_id)

    def path(self, revision: Revision) -> Path:
        """The file holding ``revision``: original.pbix for the original's content."""
        if revision.blob == self.original_hash:
            return self.original
        return self.revisions_dir / f"{revision.blob}.pbix"

    def available(self, revision: Revision) -> bool:
        """False once prune has removed the revision's file."""
        return self.path(revision).is_file()

    def placed(self) -> tuple[str, str] | None:
        """(revision id, file hash) last written into the working copy, if any."""
        placement = self._read_index().placed
        return None if placement is None else (placement.revision, placement.hash)

    def delivered(self) -> Delivered | None:
        """The head Studio last wrote over the user's own file, if it ever did."""
        return self._read_index().delivered

    def history_with_flags(self) -> list[HistoryEntry]:
        """Every revision, newest first, with the flags the History pane shows."""
        index = self._read_index()
        head = _row(index, index.head)
        undoable = False
        try:
            _, target = _undo_step(index)
            undoable = self.available(target)
        except RevisionError:
            undoable = False
        placed = index.placed.revision if index.placed is not None else None
        return [
            HistoryEntry(
                revision=row,
                available=self.available(row),
                is_head=row.id == head.id,
                is_placed=row.id == placed,
                can_undo=undoable and row.id == head.id,
            )
            for row in reversed(index.revisions)
        ]

    def later_changes(self, target_id: str) -> list[Revision]:
        """The changes in the head that restoring ``target_id`` would leave out, newest first.

        Undo and restore rows are not changes of their own; what counts is the Studio and
        Desktop steps in effect (see ``_effective_chain``), so a Desktop save made after the
        target is listed and the UI can ask before discarding it.
        """
        index = self._read_index()
        _row(index, target_id)
        kept = set(_effective_chain(index, target_id))
        return [
            _row(index, revision_id)
            for revision_id in _effective_chain(index, index.head)
            if revision_id not in kept
        ]

    def working_copy_state(self, equivalent: Equivalent | None = None) -> WorkingCopyState:
        """How the working copy relates to the history, without changing anything.

        "absent": no file. "placed": it holds the head (Studio's bytes, or Desktop's
        normalization of them). "placed_stale": it holds an older revision, and the next
        materialize refreshes it. "user_changes": a save Studio has not recorded.
        ``equivalent(placed_revision_file, working_copy)`` recognizes a normalization.
        """
        if not self.working_copy.is_file():
            return "absent"
        index = self._read_index()
        head = _row(index, index.head)
        current = self._working_hash()
        if index.placed is not None and index.placed.hash == current:
            placed_row = _row(index, index.placed.revision)
            return "placed" if placed_row.blob == head.blob else "placed_stale"
        known = self._known(index, current)
        if known is not None:
            return "placed" if known.blob == head.blob else "placed_stale"
        base = self._normalization_base(index)
        if equivalent is not None and self.available(base):
            try:
                same = equivalent(self.path(base), self.working_copy)
            except OSError:
                same = False
            if same:
                return "placed" if base.blob == head.blob else "placed_stale"
        return "user_changes"

    # --- writing -----------------------------------------------------------------------

    def staging_path(self, suffix: str = ".pbix") -> Path:
        """A new, empty file under staging/ for a version being prepared."""
        if not _SUFFIX.fullmatch(suffix):
            raise ValueError(f"Unsupported staging suffix {suffix!r}")
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix="stage-", suffix=suffix, dir=self.staging_dir)
        os.close(handle)
        return Path(name)

    def commit(
        self,
        staged: Path,
        *,
        author: CommitAuthor,
        summary: str,
        operations: Sequence[Mapping[str, Any]],
        expected_head: str,
        desktop_version: str | None = None,
    ) -> Revision:
        """Record ``staged`` (a file under staging/) as the new head and consume it.

        ``expected_head`` is the head the edit was computed from. A different head means
        another edit landed first, and RevisionConflict leaves ``staged`` in place.
        """
        if author not in ("studio", "desktop"):
            raise RevisionError(f"Commits are made by Studio or Desktop, not {author!r}")
        plain = _plain_operations(operations)
        with self.lock():
            index = self._read_index()
            _expect(index, expected_head)
            blob, size = self._store_blob(Path(staged))
            index, row = _with_row(
                index,
                blob=blob,
                size=size,
                author=author,
                summary=summary,
                operations=plain,
                parent=index.head,
                desktop_version=desktop_version,
            )
            self._write_index(index)
        return row

    def restore(
        self,
        target_id: str,
        *,
        expected_head: str,
        author: Literal["restore", "undo"] = "restore",
        desktop_version: str | None = None,
    ) -> Revision:
        """Make ``target_id``'s content the head again, as a new row (history is kept)."""
        with self.lock():
            index = self._read_index()
            _expect(index, expected_head)
            target = self._restore_target(index, target_id)
            return self._restore(
                index,
                target,
                author=author,
                summary=f"Restored to {target.id}: {target.summary}",
                desktop_version=desktop_version,
            )

    def undo(self, expected_head: str, *, desktop_version: str | None = None) -> Revision:
        """Revert the latest change that is still in effect; see ``_undo_step``."""
        with self.lock():
            index = self._read_index()
            _expect(index, expected_head)
            undone, target = _undo_step(index)
            return self._restore(
                index,
                target,
                author="undo",
                summary=f"Undid {undone.id}: {undone.summary}",
                desktop_version=desktop_version,
            )

    def check_restore(self, target_id: str) -> None:
        """Raise RevisionError when ``restore(target_id)`` cannot work (unknown, already the
        head, or its file was pruned), so a caller can refuse before it disturbs Desktop."""
        self._restore_target(self._read_index(), target_id)

    def check_undo(self) -> None:
        """Raise RevisionError when ``undo`` cannot work (nothing to undo, file pruned)."""
        _, target = _undo_step(self._read_index())
        self._require_available(target)

    def _restore_target(self, index: RevisionIndex, target_id: str) -> Revision:
        target = _row(index, target_id)
        if target.id == index.head:
            raise RevisionError(f"{target.id} is already the current version")
        self._require_available(target)
        return target

    def _require_available(self, target: Revision) -> None:
        if not self.available(target):
            raise RevisionError(
                f"Version {target.id} is no longer kept on disk; older versions are removed "
                "to save space"
            )

    def mark_placed(self, revision_id: str, digest: str) -> None:
        """Record that the working copy now holds ``revision_id`` with hash ``digest``."""
        with self.lock():
            index = self._read_index()
            _row(index, revision_id)
            try:
                placement = Placement(revision=revision_id, hash=digest)
            except ValidationError as error:
                raise RevisionError("A placement needs the working copy's sha256") from error
            self._write_index(index.model_copy(update={"placed": placement}))

    def replace_placed_copy(self) -> str | None:
        """Record the working copy's current bytes as the placed revision's copy; returns the
        new hash, or None when nothing is placed.

        Only for a save Studio itself made: opening a file makes Desktop mark it changed, and
        Studio saves that at once (``normalize``). The file then differs from what Studio wrote
        but holds the same revision; without this the next batch would record Desktop's own
        rewrite as the user's work (seen live on 2026-09-29: a "Changes saved in Power BI
        Desktop" step after every reopen, which also broke Undo)."""
        with self.lock():
            index = self._read_index()
            if index.placed is None:
                return None
            digest = self._working_hash()
            if digest != index.placed.hash:
                placement = Placement(revision=index.placed.revision, hash=digest)
                self._write_index(index.model_copy(update={"placed": placement}))
            return digest

    def record_delivery(self, path: Path, digest: str) -> None:
        """Remember that ``path`` (the user's own file) now holds ``digest``."""
        with self.lock():
            index = self._read_index()
            try:
                delivered = Delivered(path=str(path), hash=digest)
            except ValidationError as error:
                raise RevisionError("A delivery needs the file's sha256") from error
            self._write_index(index.model_copy(update={"delivered": delivered}))

    def materialize(self, *, force: bool = False) -> bool:
        """Write the head into desktop/Report.pbix; False when it already held the head.

        Refuses with UserChangesPending when the working copy holds content Studio never
        wrote there: a Desktop save, which ``adopt_working_copy`` must record first. A copy
        matching any recorded revision is Studio's own (Desktop's save always changes the
        bytes), which also covers a crash between writing the copy and recording it.

        ``force`` skips that refusal. Only a caller that knows the content is a by-product
        of its own batch may use it: a model change Studio rolled back, or a Desktop save
        of a failed transaction, with Desktop closed. It never applies to a user's save.
        """
        with self.lock():
            index = self._read_index()
            head = _row(index, index.head)
            working = self.working_copy
            if working.exists():
                current = self._working_hash()
                if current == head.blob:
                    self._place(index, head.id, current)
                    return False
                if (
                    index.placed is not None
                    and index.placed.hash == current
                    and index.placed.revision == head.id
                ):
                    # Desktop's normalization of the head: the same report, so the copy
                    # Desktop wrote stays, and opening it does not normalize it again.
                    return False
                if not force and not self._accounted(index, current):
                    raise UserChangesPending(
                        "Power BI Desktop saved changes to this report that Studio has not "
                        "recorded yet. Studio records them as a version before writing its own."
                    )
            source = self.path(head)
            if not source.is_file():
                raise RevisionError(f"The file of the current version {head.id} is missing")
            working.parent.mkdir(parents=True, exist_ok=True)
            handle, name = tempfile.mkstemp(
                prefix=working.name + ".", suffix=".tmp", dir=working.parent
            )
            os.close(handle)
            temporary = Path(name)
            try:
                digest = _copy_hashing(source, temporary)
                if digest != head.blob:
                    raise RevisionError(
                        f"The stored file of version {head.id} is damaged: its content no "
                        "longer matches its fingerprint"
                    )
                try:
                    _retry(lambda: os.replace(temporary, working))
                except PermissionError as error:
                    raise WorkingCopyBusy(_DESKTOP_BUSY) from error
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            self._place(index, head.id, digest)
            return True

    def stage_working_copy(self) -> tuple[Path, str]:
        """Copy the working copy into staging/ and return (that file, its sha256).

        The copy is a complete package or nothing: a Desktop save that was cut short raises
        RevisionError. It is the way to look at what Desktop saved without recording it, for
        example to check a model change before it becomes a revision.
        """
        staged = self.staging_path()
        try:
            try:
                digest = _copy_hashing(
                    self.working_copy, staged, attempts=WORKING_COPY_READ_ATTEMPTS
                )
            except PermissionError as error:
                raise WorkingCopyBusy(_DESKTOP_BUSY) from error
            if not _readable_zip(staged):
                raise RevisionError(
                    "The copy saved in Power BI Desktop is not a complete report. Save "
                    "it again in Power BI Desktop, then retry."
                )
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        return staged, digest

    def adopt_working_copy(
        self,
        summary: str = "Changes saved in Power BI Desktop",
        *,
        desktop_version: str | None = None,
        equivalent: Equivalent | None = None,
    ) -> Revision | None:
        """Record a Desktop save in the working copy as the new head; None when unchanged.

        The row's parent is the revision Studio placed there (r0000 for installs that
        copied the original before placements were recorded, which is also the migration
        for a working copy that predates placements: it is adopted, never overwritten).
        When the head moved on after that placement, the save does not contain Studio's
        later edits: the row is still recorded, because it is the user's work, with
        ``conflict`` set so the service can say what the save replaced.

        ``equivalent(base_file, saved_file)`` tells a real change from Desktop's own
        normalization of the file Studio placed: when it says they are the same report no
        revision is made and the saved bytes become the placement.
        """
        with self.lock():
            index = self._read_index()
            working = self.working_copy
            if not working.is_file():
                return None
            current = self._working_hash()
            if index.placed is not None and index.placed.hash == current:
                return None
            known = self._known(index, current)
            if known is not None:
                self._place(index, known.id, current)
                return None
            staged, digest = self.stage_working_copy()
            try:
                base = self._normalization_base(index)
                if (
                    equivalent is not None
                    and self.available(base)
                    and equivalent(self.path(base), staged)
                ):
                    staged.unlink(missing_ok=True)
                    self._write_index(
                        index.model_copy(
                            update={"placed": Placement(revision=base.id, hash=digest)}
                        )
                    )
                    return None
                blob, size = self._store_blob(staged, digest=digest)
            except BaseException:
                staged.unlink(missing_ok=True)
                raise
            parent = index.placed.revision if index.placed is not None else ORIGINAL_ID
            index, row = _with_row(
                index,
                blob=blob,
                size=size,
                author="desktop",
                summary=summary,
                operations=[],
                parent=parent,
                conflict=index.head != parent,
                desktop_version=desktop_version,
            )
            self._write_index(
                index.model_copy(update={"placed": Placement(revision=row.id, hash=blob)})
            )
            return row

    # --- housekeeping ------------------------------------------------------------------

    def prune(
        self,
        keep: int = DEFAULT_KEEP,
        *,
        pinned: Iterable[str] = (),
        max_bytes: int | None = None,
    ) -> list[str]:
        """Delete version files outside the newest ``keep`` distinct contents.

        Kept whatever the limits: the head's, the placed revision's and the original's
        files, the parents of batches still in flight (the journal says which), and any
        revision id or blob hash in ``pinned``. Retention counts distinct contents, not
        rows: undo ping-pong adds rows that point at the same file, and would otherwise
        push real history out. ``max_bytes`` optionally caps the unpinned files' total size,
        newest first. original.pbix is never touched, and staging/ is not pruned: a file
        there belongs to a batch that is still running.

        Best effort: a file another process holds open (antivirus, a reader) is retried
        briefly and otherwise stays for the next prune.
        """
        if keep < 1:
            raise ValueError("keep must be at least 1")
        with self.lock():
            index = self._read_index()
            must = {self.original_hash, _row(index, index.head).blob}
            if index.placed is not None:
                must.add(index.placed.hash)
                must.add(_row(index, index.placed.revision).blob)
            for item in {*pinned, *EditJournal(self.journal_path).pinned_revisions()}:
                if _SHA256.fullmatch(item):
                    must.add(item)
                    continue
                try:
                    must.add(_row(index, item).blob)
                except RevisionError:
                    continue
            wanted = set(must)
            size_of = {row.blob: row.size for row in index.revisions}
            total = sum(size_of.get(blob, 0) for blob in must)
            seen: set[str] = set()
            for row in reversed(index.revisions):
                if row.blob in seen:
                    continue
                seen.add(row.blob)
                if row.blob in wanted:
                    continue
                if len(seen) > keep or (max_bytes is not None and total + row.size > max_bytes):
                    break
                wanted.add(row.blob)
                total += row.size
            if not self.revisions_dir.is_dir():
                return []
            original = self.original.resolve()
            removed: list[str] = []
            for entry in self.revisions_dir.iterdir():
                match = _BLOB_NAME.fullmatch(entry.name)
                if match is None or match.group(1) in wanted or entry.resolve() == original:
                    continue
                try:
                    _retry(entry.unlink, PRUNE_ATTEMPTS)
                except (PermissionError, FileNotFoundError):
                    continue
                removed.append(match.group(1))
            return sorted(removed)

    def cleanup(self, max_age: float = STAGING_MAX_AGE) -> int:
        """Remove files older than ``max_age`` seconds that an interrupted write left.

        That is everything in staging/, plus the temporary files beside index.json and the
        working copy. The age keeps a write that is still running safe without the lock.
        """
        cutoff = time.time() - max_age
        leftovers: list[Path] = []
        if self.staging_dir.is_dir():
            leftovers.extend(self.staging_dir.iterdir())
        if self.revisions_dir.is_dir():
            leftovers.extend(self.revisions_dir.glob(self.index_path.name + ".*.tmp"))
        if self.working_copy.parent.is_dir():
            leftovers.extend(self.working_copy.parent.glob(self.working_copy.name + ".*.tmp"))
        removed = 0
        for entry in leftovers:
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except (PermissionError, FileNotFoundError):
                continue
        return removed

    # --- internals ---------------------------------------------------------------------

    def _damaged(self, detail: str) -> RevisionIndexError:
        return RevisionIndexError(
            f"The edit history of this report ({self.index_path}) is damaged: {detail}. "
            "Studio does not reset it, because that would lose earlier versions; the "
            f"version files themselves are kept in {self.revisions_dir}."
        )

    def _fresh_index(self) -> RevisionIndex:
        try:
            stat = self.original.stat()
        except FileNotFoundError as error:
            raise RevisionError("The preserved original of this report is missing") from error
        first = Revision(
            id=ORIGINAL_ID,
            blob=self.original_hash,
            parent=None,
            author="import",
            summary="Imported report",
            operations=[],
            created=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(timespec="seconds"),
            size=stat.st_size,
        )
        return RevisionIndex(original=self.original_hash, head=ORIGINAL_ID, revisions=[first])

    def _read_index(self) -> RevisionIndex:
        path = self.index_path
        try:
            if path.stat().st_size > MAX_INDEX_BYTES:
                raise self._damaged("it is larger than any real history")
            raw = _retry(path.read_bytes)
        except FileNotFoundError:
            return self._fresh_index()
        try:
            index = RevisionIndex.model_validate(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
            raise self._damaged(
                f"it is not a complete history file ({type(error).__name__})"
            ) from error
        self._check_index(index)
        return index

    def _check_index(self, index: RevisionIndex) -> None:
        if index.original != self.original_hash:
            raise self._damaged("it belongs to a different original file")
        rows = index.revisions
        first = rows[0]
        if first.id != ORIGINAL_ID or first.blob != self.original_hash or first.parent:
            raise self._damaged("its first version is not the imported original")
        seen: set[str] = set()
        for number, row in enumerate(rows):
            if row.id != _revision_id(number):
                raise self._damaged(f"version {row.id} is out of order")
            if number and (row.parent not in seen or (row.restores and row.restores not in seen)):
                raise self._damaged(f"version {row.id} refers to a version recorded after it")
            seen.add(row.id)
        if index.head not in seen:
            raise self._damaged(f"its current version {index.head} is not in the history")
        if index.placed is not None and index.placed.revision not in seen:
            raise self._damaged(f"the working copy's version {index.placed.revision} is unknown")

    def _write_index(self, index: RevisionIndex) -> None:
        assert self._lock.is_locked, "index writes happen under edit.lock"
        self.revisions_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(index.model_dump(mode="json"), ensure_ascii=False, indent=1)
        _write_atomic(self.index_path, payload.encode("utf-8"))

    def _store_blob(self, staged: Path, *, digest: str | None = None) -> tuple[str, int]:
        """Move a staged file into revisions/<sha>.pbix; returns (sha256, size).

        The file is flushed to disk before the rename, so no row ever names a file a
        power loss could leave incomplete. ``digest`` is passed only by callers that
        hashed the bytes while writing them.
        """
        try:
            resolved = staged.resolve(strict=True)
        except OSError as error:
            raise RevisionError("The prepared version of the report is missing") from error
        if not resolved.is_file() or not resolved.is_relative_to(self.staging_dir.resolve()):
            raise RevisionError("Only files prepared in the report's staging folder are recorded")
        if not zipfile.is_zipfile(resolved):
            raise RevisionError("The prepared version is not a PBIX package")
        _flush_to_disk(resolved)
        if digest is None:
            digest = _retry(lambda: sha256_file(resolved))
        size = resolved.stat().st_size
        if digest == self.original_hash:
            _retry(lambda: resolved.unlink(missing_ok=True))
            return digest, size
        target = self.revisions_dir / f"{digest}.pbix"
        self.revisions_dir.mkdir(parents=True, exist_ok=True)
        if (
            target.is_file()
            and target.stat().st_size == size
            and _retry(lambda: sha256_file(target)) == digest
        ):
            _retry(lambda: resolved.unlink(missing_ok=True))
        else:
            _retry(lambda: os.replace(resolved, target))
            # The row names this file, so what reached the disk must be what was hashed.
            if _retry(lambda: sha256_file(target)) != digest:
                target.unlink(missing_ok=True)
                raise RevisionError(
                    "The new version could not be stored intact. Check the free space on "
                    "the drive that holds Studio's data, then try again."
                )
        return digest, size

    def _restore(
        self,
        index: RevisionIndex,
        target: Revision,
        *,
        author: Literal["restore", "undo"],
        summary: str,
        desktop_version: str | None = None,
    ) -> Revision:
        self._require_available(target)
        index, row = _with_row(
            index,
            blob=target.blob,
            size=target.size,
            author=author,
            summary=summary,
            operations=[{"op": author, "revision": target.id}],
            parent=index.head,
            restores=target.id,
            desktop_version=desktop_version,
        )
        self._write_index(index)
        return row

    @staticmethod
    def _normalization_base(index: RevisionIndex) -> Revision:
        """The revision Desktop's copy is compared to: what Studio last placed there (the
        original for a working copy from before placements were recorded)."""
        return _row(index, index.placed.revision if index.placed is not None else ORIGINAL_ID)

    def _working_hash(self) -> str:
        try:
            return _retry(lambda: sha256_file(self.working_copy), WORKING_COPY_READ_ATTEMPTS)
        except PermissionError as error:
            raise WorkingCopyBusy(_DESKTOP_BUSY) from error

    @staticmethod
    def _known(index: RevisionIndex, digest: str) -> Revision | None:
        for row in reversed(index.revisions):
            if row.blob == digest:
                return row
        return None

    def _accounted(self, index: RevisionIndex, digest: str) -> bool:
        placed = index.placed is not None and index.placed.hash == digest
        return placed or self._known(index, digest) is not None

    def _place(self, index: RevisionIndex, revision_id: str, digest: str) -> None:
        placement = Placement(revision=revision_id, hash=digest)
        if index.placed != placement:
            self._write_index(index.model_copy(update={"placed": placement}))


def history_with_flags(store: RevisionStore) -> list[HistoryEntry]:
    """Every revision, newest first, with ``available``, ``is_head`` and ``can_undo``."""
    return store.history_with_flags()
