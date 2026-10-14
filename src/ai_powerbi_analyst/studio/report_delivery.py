"""Deliver the edited report to the user: a new copy, or over their own file (P10).

Studio never touches the user's source file unless they ask for it. The edited report is the
head revision, a whole PBIX kept under reports/<id>/revisions; delivery writes it out.

Two deliveries, with the same checks:
- ``save_copy`` creates a new file. It is created exclusively, so an earlier "(edited)" copy
  is never replaced: a later one gets " (2)", " (3)" instead.
- ``replace_original`` writes over the user's own file, only while that file is still what
  Studio imported (or last wrote). If the user kept working on it in Power BI Desktop, their
  changes would be overwritten, so it refuses and points to "Save a copy". It first makes a
  backup beside the file, writes the new content to a temporary file in the same folder, and
  swaps it in with an atomic replace, retrying the brief Windows sharing violations.

Both adopt a Power BI Desktop save first (so the copy is the user's latest work), refuse while
Desktop shows unsaved changes, and check the head before writing: it is a complete ZIP, it
parses as a package, and its sha256 is the revision's. The written file is read back and
checked the same way.

A head Studio wrote has no SecurityBindings part: Studio drops it because it signs the
previous content with the author's DPAPI key. Power BI Desktop opens the file without it and
writes a fresh one the first time the user saves. The user is told so in the UI.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal, Protocol

from ai_powerbi_analyst.integrity import sha256_file

from .desktop_session import DesktopInstance, DesktopSessionError
from .pbix_package import PackageError, PbixPackage
from .report_revisions import (
    ORIGINAL_ID,
    Equivalent,
    Revision,
    RevisionError,
    RevisionStore,
    WorkingCopyBusy,
    _readable_zip,
    _retry,
)

DeliveryState = Literal["in_sync", "edited", "source_changed"]
_CHUNK = 1024 * 1024
_MAX_COPIES = 999
_FORBIDDEN_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class DeliveryError(ValueError):
    """The report was not delivered; nothing the user owns was changed."""


class DeliveryPort(Protocol):
    """The two things delivery asks Power BI Desktop."""

    def find(self, path: Path) -> DesktopInstance | None: ...

    def status(self, instance: DesktopInstance) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ReplaceResult:
    path: Path
    backup: Path
    hash: str
    warnings: list[str] = field(default_factory=list)


def _same_file(a: str | Path, b: str | Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def _try_hash(path: Path) -> str | None:
    try:
        return sha256_file(path)
    except OSError:
        return None


def _copy_to(source: Path, target: BinaryIO) -> str:
    """Copy ``source`` into an open file, flush it to disk and return the sha256 written."""
    digest = hashlib.sha256()
    with source.open("rb") as reader:
        for block in iter(lambda: reader.read(_CHUNK), b""):
            digest.update(block)
            target.write(block)
    target.flush()
    os.fsync(target.fileno())
    return digest.hexdigest()


def _holder(port: DeliveryPort, path: Path) -> DesktopInstance | None:
    """The Desktop instance that has ``path`` open. When Studio cannot tell (the bridge
    failed, or two windows hold the file) delivery is refused: writing over a file Desktop
    may have open could lose the user's work."""
    try:
        return port.find(path)
    except DesktopSessionError as error:
        raise DeliveryError(str(error)) from error


def _adopt_first(
    store: RevisionStore, port: DeliveryPort | None, equivalent: Equivalent | None
) -> None:
    """Record a Desktop save first, so the delivered file holds the user's latest work."""
    if port is not None:
        instance = _holder(port, store.working_copy)
        try:
            dirty = instance is not None and bool(port.status(instance).get("hasUnsavedChanges"))
        except DesktopSessionError as error:
            raise DeliveryError(str(error)) from error
        if dirty:
            raise DeliveryError(
                "Power BI Desktop has unsaved changes to this report. Save them in Power BI "
                "Desktop (Studio then keeps them in the history), then try again."
            )
    try:
        store.adopt_working_copy(equivalent=equivalent)
    except WorkingCopyBusy as error:
        raise DeliveryError(str(error)) from error


def _check_head(store: RevisionStore, head: Revision) -> Path:
    """The head's file, checked before anything is written from it."""
    path = store.path(head)
    if not path.is_file():
        raise DeliveryError(
            f"The file of the current version ({head.id}) is missing from Studio's history."
        )
    if not _readable_zip(path):
        raise DeliveryError(
            "Studio's copy of the edited report is damaged, so it was not delivered."
        )
    try:
        PbixPackage(path)
    except PackageError as error:
        raise DeliveryError(
            f"Studio's copy of the edited report cannot be read: {error}"
        ) from error
    return path


def _check_written(path: Path, expected: str) -> None:
    """Read the delivered file back: same bytes, complete, parses."""
    if _try_hash(path) != expected or not _readable_zip(path):
        raise DeliveryError(
            "The file that was written does not match Studio's edited report, so it was removed."
        )
    try:
        PbixPackage(path)
    except PackageError as error:
        raise DeliveryError(f"The file that was written cannot be read: {error}") from error


def _clean_name(name: str) -> str:
    cleaned = _FORBIDDEN_NAME.sub("_", name).strip(" .")
    return cleaned or "Report"


def save_copy(
    store: RevisionStore,
    destination: Path,
    *,
    name: str = "Report",
    port: DeliveryPort | None = None,
    equivalent: Equivalent | None = None,
) -> Path:
    """Write the head to a new .pbix and return its path.

    ``destination`` is a folder (the file is "<name> (edited).pbix") or a file name ending in
    .pbix. An existing file is never replaced: the new file gets " (2)", " (3)" and so on.
    """
    destination = Path(destination)
    if destination.is_dir():
        folder, filename = destination, f"{_clean_name(name)} (edited).pbix"
    else:
        folder, filename = destination.parent, destination.name
        if destination.suffix.casefold() != ".pbix":
            raise DeliveryError("Save the report as a .pbix file")
    if not folder.is_dir():
        raise DeliveryError(f"The folder {folder} does not exist")
    with store.lock():
        _adopt_first(store, port, equivalent)
        head = store.head()
        source = _check_head(store, head)
        stem, suffix = Path(filename).stem, Path(filename).suffix
        for number in range(1, _MAX_COPIES + 1):
            candidate = folder / (filename if number == 1 else f"{stem} ({number}){suffix}")
            try:
                handle = candidate.open("xb")
            except FileExistsError:
                continue
            except OSError as error:
                raise DeliveryError(f"Studio cannot write {candidate.name}: {error}") from error
            break
        else:
            raise DeliveryError(f"There are already {_MAX_COPIES} copies named {filename}")
        try:
            with handle:
                digest = _copy_to(source, handle)
            if digest != head.blob:
                raise DeliveryError(
                    "Studio's copy of the edited report changed while it was being written, so "
                    "it was not delivered."
                )
            _check_written(candidate, head.blob)
        except BaseException:
            candidate.unlink(missing_ok=True)
            raise
        return candidate


def replace_original(
    store: RevisionStore,
    source_path: Path,
    source_hash: str,
    *,
    port: DeliveryPort | None = None,
    equivalent: Equivalent | None = None,
    now: Callable[[], datetime] | None = None,
) -> ReplaceResult:
    """Write the head over ``source_path``, the file that was imported.

    Refused unless the file on disk still hashes to ``source_hash`` or to what Studio last
    wrote there, and refused while Power BI Desktop has it open. A backup named
    "<name>.pbix.studio-backup-<UTC time>" is made first.
    """
    source = Path(source_path)
    clock = now or (lambda: datetime.now(UTC))
    with store.lock():
        if port is not None and _holder(port, source) is not None:
            raise DeliveryError(
                f"Power BI Desktop has {source.name} open. Close it there first, or use Save a "
                "copy so nothing of yours is overwritten."
            )
        _adopt_first(store, port, equivalent)
        if not source.is_file():
            raise DeliveryError(
                f"{source.name} is no longer at {source.parent}. Use Save a copy instead."
            )
        try:
            current = sha256_file(source)
        except OSError as error:
            raise DeliveryError(
                f"{source.name} is in use by another program. Close it, then try again."
            ) from error
        delivered = store.delivered()
        last_written = (
            delivered.hash if delivered is not None and _same_file(delivered.path, source) else None
        )
        if current != source_hash and current != last_written:
            raise DeliveryError(
                f"{source.name} was changed outside Studio after it was imported, and replacing "
                "it would overwrite those changes. Use Save a copy instead."
            )
        head = store.head()
        head_file = _check_head(store, head)
        stamp = clock().strftime("%Y%m%dT%H%M%SZ")
        backup = source.with_name(f"{source.name}.studio-backup-{stamp}")
        temporary = source.with_name(f"{source.name}.studio-{uuid.uuid4().hex[:12]}.tmp")
        try:
            backup = _write_new(source, backup, current, "the backup")
            _write_new(head_file, temporary, head.blob, "the new file")
            try:
                _retry(lambda: os.replace(temporary, source))
            except PermissionError as error:
                raise DeliveryError(
                    f"{source.name} is in use by another program, so it was not replaced. "
                    f"Your file is unchanged; a backup is at {backup.name}."
                ) from error
        finally:
            temporary.unlink(missing_ok=True)
        if _try_hash(source) != head.blob:
            raise DeliveryError(
                f"{source.name} does not hold the edited report after writing it. Your original "
                f"is saved as {backup.name}."
            )
        store.record_delivery(source, head.blob)
        return ReplaceResult(
            path=source,
            backup=backup,
            hash=head.blob,
            warnings=[
                "The edited file has no Power BI Desktop signature yet. It opens normally, and "
                "Power BI Desktop adds one the first time you save it there."
            ],
        )


def _write_new(source: Path, target: Path, expected: str, what: str) -> Path:
    """Copy ``source`` to a file that must not exist yet, and check it holds ``expected``.

    A name that is taken (two backups within one second) gets a numbered suffix.
    """
    for number in range(0, _MAX_COPIES):
        candidate = target if number == 0 else target.with_name(f"{target.name}-{number}")
        try:
            handle = candidate.open("xb")
        except FileExistsError:
            continue
        except OSError as error:
            raise DeliveryError(f"Studio cannot write {what} next to your file: {error}") from error
        try:
            with handle:
                digest = _copy_to(source, handle)
            if digest != expected:
                raise DeliveryError(
                    f"Your file changed while Studio was preparing {what}, so nothing was replaced."
                )
        except BaseException:
            candidate.unlink(missing_ok=True)
            raise
        return candidate
    raise DeliveryError(f"Studio could not find a free name for {what}")


def delivery_state(store: RevisionStore, source_path: Path) -> DeliveryState:
    """How the user's own file relates to the edited report.

    "in_sync": the file holds the head. "edited": the file is what Studio imported or last
    wrote, and the head has changes it lacks (the toolbar says "Edited in Studio").
    "source_changed": the file was changed by someone else, or is gone.
    """
    current = _try_hash(Path(source_path))
    if current is None:
        return "source_changed"
    if current == store.head().blob:
        return "in_sync"
    delivered = store.delivered()
    known = {store.original_hash}
    if delivered is not None and _same_file(delivered.path, source_path):
        known.add(delivered.hash)
    return "edited" if current in known else "source_changed"


def pending_changes(store: RevisionStore, source_path: Path) -> int:
    """How many changes in the head the user's file does not have ("N changes not in your file").

    Counted from the newest version whose content the file holds; a file that matches no
    version (changed elsewhere) counts every change since the import.
    """
    current = _try_hash(Path(source_path))
    reference = ORIGINAL_ID
    if current is not None:
        for row in store.history():  # newest first
            if row.blob == current:
                reference = row.id
                break
    try:
        return len(store.later_changes(reference))
    except RevisionError:
        return 0
