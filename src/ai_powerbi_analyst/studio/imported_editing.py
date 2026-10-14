"""Everything stateful about editing an imported report, behind one service.

``StudioService`` builds one ``ImportedEditingService`` and delegates to it; the HTTP routes
and the chat both call the same methods, so a Format-pane edit, a chat message, an undo and a
"Save a copy" all take the same road (docs/imported-report-editing-api.md).

The road, and why it is shaped this way (design review P6, P8, P9, P10, P19, P21, P28, P37):

- A batch is not tied to the request that asked for it (P8). Each report has one serial lane:
  a worker thread that runs its batches one after another, while different reports run side by
  side. ``submit`` queues a batch and returns a handle; the HTTP route waits up to 25 s for it
  and answers ``202`` with the batch id when it is still running, and the client polls. A
  client that goes away (a closed chat stream, a reloaded page) does not stop the batch.
- The batch registry lives in memory and is mirrored in ``reports/<id>/batches.jsonl``, so
  after a restart a poll for an old batch answers with what happened (the edit journal says
  whether the interrupted batch was rolled forward) instead of "unknown".
- ``ReportEditor`` (report_editor.py) does the work under the report's edit lock; this
  module supplies what it needs: a Power BI Desktop port that never launches anything by
  itself, a bounded reader of the model's measures, and ``_rederive``, which moves Studio's
  own view of the report (pages, measures) to the new head and stores it.
- Start-up recovery (P9) finishes or undoes batches a crash left half done, once per report,
  in the background; it never closes Desktop and never blocks the app from starting.

Desktop is only ever touched for a model change, a Desktop that was already open when a batch
starts, "Open in Power BI" and delivery; a report-only edit with Desktop closed (or not
installed) never needs it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import psutil
from pydantic import Field, TypeAdapter, ValidationError

from ai_powerbi_analyst.contracts import ClosedModel
from ai_powerbi_analyst.integrity import sha256_file

from . import (
    model_health,
    pbir_expr,
    report_delivery,
    report_edit_model,
    report_edits,
    report_import,
    report_review,
    report_tests,
)
from .contracts import DatasetSnapshot, EditIdempotencyKey
from .desktop_session import (
    DesktopController,
    DesktopInstance,
    DesktopReadiness,
    DesktopSessionError,
    UnsavedDesktopWork,
    default_receipts_dir,
    read_receipt,
)
from .edit_context import EditContext, LastEdit
from .edit_journal import EditJournal, RecoveryReport
from .imported_format_catalog import format_catalog as build_format_catalog
from .model_facts import data_status as facts_data_status
from .model_facts import known as model_facts_known
from .model_facts import payload as facts_payload
from .native_report import STALE_DESKTOP, STALE_VIEW, NativeReportPreview
from .pbir_document import DocumentError, PbirDocument
from .pbix_package import PackageError, PbixPackage
from .report_editor import (
    TESTS_CONFIRMATION,
    DesktopPort,
    DesktopWork,
    EditReceipt,
    EditRejected,
    ModelFacts,
    ReportEditor,
    RestoreNeedsConfirmation,
    TestsCheck,
)
from .report_edits import (
    CreateMeasure,
    DeleteMeasure,
    EditOutcome,
    RenameMeasure,
    UpdateMeasure,
)
from .report_engine import ENGINE_LABEL
from .report_import import ImportedReport
from .report_revisions import (
    ORIGINAL_ID,
    HistoryEntry,
    Revision,
    RevisionBusy,
    RevisionConflict,
    RevisionError,
    RevisionStore,
    UserChangesPending,
    WorkingCopyBusy,
)
from .report_tokens import family_of
from .state import EMPTY_HASH, StateConflictError, StudioStateStore

LOGGER = logging.getLogger(__name__)

# How long an HTTP request waits for its batch before answering 202 (docs/...-api.md).
WAIT_SECONDS = 25.0
BatchState = Literal["queued", "running", "done", "failed"]
_TERMINAL = frozenset({"done", "failed"})
Progress = Callable[[str], None]
Job = Callable[[Progress], dict[str, Any]]
ReadMeasures = Callable[[Path], list[dict[str, Any]]]
ReadTypes = Callable[[Path], dict[str, Any]]
ReadFacts = Callable[[Path], dict[str, Any]]
# The rows a report's numbers are computed from (the workspace's active snapshot), None when it
# has none; and what runs report tests on Studio's engine for a report (tests inject their own).
SnapshotOf = Callable[[ImportedReport], DatasetSnapshot | None]
TestsEvaluator = Callable[[ImportedReport], report_tests.Evaluate]
# Renders one page of a report with Studio's engine (the render payload), for suggestions.
PageRenderer = Callable[[ImportedReport, str], Mapping[str, Any]]

MAX_BATCHES_IN_MEMORY = 300
# Batches whose result is an EditReceipt (the others answer {path, hash}, {launch_requested...}).
RECEIPT_KINDS = frozenset({"edit", "undo", "restore"})
MIRROR_NAME = "batches.jsonl"
MIRROR_MAX_BYTES = 512 * 1024
MIRROR_KEEP = 200
TYPES_NAME = "model-types.json"
# 2: the sidecar also holds the DAX of calculated columns and tables. A file without it is read
# again (its model key can match while it lacks what the edit layer now asks for).
TYPES_VERSION = 2
# A model whose types could not be read is not tried again for this long (each try is a child
# process that can run for minutes), so a failing model does not stall every edit.
TYPES_BACKOFF = 60.0
# The same for the model's facts, and the facts kept in memory (a few models per report: the
# head's, the snapshot's, the one an undo returns to).
FACTS_BACKOFF = 60.0
FACTS_IN_MEMORY = 8
# Model health results kept per (revision, model, rows, statistics): the pane, the chat and the
# receipt of the next edit ask for the same head (~0.1 s on the Agents model, 3 s with stats).
HEALTH_IN_MEMORY = 8
# Pages reviewed after one edit: a change to the whole report can touch every page, and each
# review is ~30 ms.
REVIEW_MAX_PAGES = 12
# Findings a receipt carries (the Review pane lists the rest on "review this page").
REVIEW_MAX_FINDINGS = 20
# The operations that change the model (model health after an edit looks at their objects).
_MEASURE_OPS = frozenset({"create_measure", "update_measure", "rename_measure", "delete_measure"})
_COLUMN_OPS = frozenset(
    {"create_calculated_column", "set_column_properties", "rename_column", "delete_column"}
)
_TABLE_OPS = frozenset(
    {
        "create_calculated_table",
        "set_table_properties",
        "rename_table",
        "delete_table",
        "mark_date_table",
        "set_table_query",
        "refresh_data",
    }
)
_RELATIONSHIP_OPS = frozenset({"create_relationship", "update_relationship", "delete_relationship"})
_RENAME_OPS = frozenset({"rename_measure", "rename_column", "rename_table"})
# Desktop's state and the head's delivery status are read on every payload; a short cache keeps
# a burst of requests (a page load asks several times) from asking the bridge each time.
STATE_TTL = 3.0
# A view that could not be brought to its head is not retried for this long (same head).
HEAL_BACKOFF = 45.0
# A failed look for Desktop is repeated after this long, so installing it needs no restart;
# a found Desktop is trusted longer.
FOUND_TTL = 300.0
MISSING_TTL = 30.0
_EXTRACT_TIMEOUT = 180.0
_EXTRACT_MAX_BYTES = 16 * 1024**2
_MIN_FREE_MEMORY = 512 * 1024**2
# The script that reads a model's metadata in a child process (a module constant so tests can
# point it at a stand-in).
_EXTRACT_SCRIPT = Path(__file__).with_name("pbix_extract.py")

_STILL_LOOKING = "Still looking for Power BI Desktop"
_NEEDS_DESKTOP = (
    "Power BI Desktop is needed for this change but is not set up on this computer, so Studio "
    "left the report as it was."
)
_STAGES = {
    "Saving your Power BI Desktop work": "saving",
    "Closing Power BI Desktop": "closing_desktop",
    "Writing the change": "writing",
    "Opening Power BI Desktop": "opening_desktop",
    "Applying the change in Power BI": "applying",
    "Saving": "saving",
}
# The operation fields the request-level ``confirm_replace`` sets where an operation has them.
_CONFIRM_REPLACE_FLAGS = ("confirm_replace", "replace_conditional", "replace_dynamic")
_DIALOG = re.compile(r"showing a dialog \((?P<dialog>[^)]*)\)")
# A dialog that opened in the middle of a save: desktop_session words it without a title.
_DIALOG_WHILE_SAVING = re.compile(r"opened a dialog while saving")
# The edit layer writes "Step 2: ..." or "Step 2 (set_position): ...".
_STEP_ANYWHERE = re.compile(r"^Step (?P<step>\d+)\b")
_OP_NAMES = {
    "set_position": "Moved or resized a visual",
    "arrange": "Changed the stacking order",
    "set_visual_hidden": "Showed or hid a visual",
    "delete_visual": "Deleted a visual",
    "duplicate_visual": "Duplicated a visual",
    "add_visual": "Added a visual",
    "change_visual_type": "Changed a visual's type",
    "set_fields": "Changed a visual's fields",
    "set_sort": "Changed the sort order",
    "rename_page": "Renamed a page",
    "add_page": "Added a page",
    "duplicate_page": "Duplicated a page",
    "delete_page": "Deleted a page",
    "move_page": "Moved a page",
    "set_page_size": "Changed a page's size",
    "set_page_hidden": "Showed or hid a page",
    "create_measure": "Created a measure",
    "update_measure": "Changed a measure",
    "rename_measure": "Renamed a measure",
    "delete_measure": "Deleted a measure",
    "undo": "Went back one change",
    "restore": "Restored an earlier version",
}
# Measure operations change the model, which only Power BI Desktop can do.
_MODEL_OPERATIONS: tuple[type, ...] = tuple(
    getattr(
        report_edits,
        "MODEL_OPERATIONS",
        (CreateMeasure, UpdateMeasure, RenameMeasure, DeleteMeasure),
    )
)
# Operations that only change the layout of the page or the report's structure: Studio's canvas
# draws them whatever the visual is. Property operations are judged by what the visual draws.
_ALWAYS_DRAWN = frozenset(
    {
        "set_position",
        "arrange",
        "set_visual_hidden",
        "align_visuals",
        "distribute_visuals",
        "swap_visuals",
        "place_visual",
        "add_textbox",
        "add_shape",
        "delete_visual",
        "duplicate_visual",
        "add_visual",
        "change_visual_type",
        "set_fields",
        "set_sort",
        "rename_page",
        "add_page",
        "duplicate_page",
        "delete_page",
        "move_page",
        "set_page_size",
        "set_page_hidden",
        "create_measure",
        "update_measure",
        "rename_measure",
        "delete_measure",
        "undo",
        "restore",
    }
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ImportedRereadRequest(ClosedModel):
    """Read the report's data again from its head (after a refresh in Power BI Desktop)."""

    idempotency_key: EditIdempotencyKey


class ModelQueryCheckRequest(ClosedModel):
    """Check a Power Query formula the way ``set_table_query`` (with ``table``) or
    ``set_named_expression`` (without) would, without making the change."""

    expression: str = Field(min_length=1, max_length=50000)
    table: str | None = Field(default=None, min_length=1, max_length=200)


# --- errors ---------------------------------------------------------------------------------


class ImportedEditError(Exception):
    """An error with the code, HTTP status and fields the API contract defines.

    ``payload`` is what the route returns: ``code`` and ``message`` as documented, plus
    ``detail`` (the same text) because every other route of the sidecar reports its message
    that way and the client reads it there.
    """

    def __init__(self, code: str, message: str, status: int = 422, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra

    def payload(self) -> dict[str, Any]:
        # A needs_confirmation names what would be lost in its own ``detail``; every other
        # error repeats its message there.
        return {"detail": self.message, "code": self.code, "message": self.message, **self.extra}


def _revision_row(row: Revision) -> dict[str, Any]:
    return {
        "id": row.id,
        "parent": row.parent,
        "author": row.author,
        "summary": row.summary,
        "created": row.created,
    }


def _operation_line(op: Any) -> str:
    name = str(op.get("op", "")) if isinstance(op, dict) else ""
    return _OP_NAMES.get(name) or (name.replace("_", " ").capitalize() if name else "A change")


def history_row(entry: HistoryEntry) -> dict[str, Any]:
    """One History pane row (docs/imported-report-editing-api.md)."""
    row = entry.revision
    found: dict[str, Any] = {
        **_revision_row(row),
        "size": row.size,
        "available": entry.available,
        "is_head": entry.is_head,
        "is_placed": entry.is_placed,
        "can_undo": entry.can_undo,
        "conflict": row.conflict,
        "desktop_version": row.desktop_version,
        "operations": [
            {"op": str(op.get("op", "")), "summary": _operation_line(op)} for op in row.operations
        ],
    }
    if row.restores:
        found["restores"] = row.restores
    return found


def _named(error: BaseException, name: str) -> bool:
    return any(cls.__name__ == name for cls in type(error).__mro__)


def _asks_the_user(error: BaseException) -> BaseException | None:
    """The NeedsConfirmation or NeedsChoice behind ``error``, if it is one or was raised from
    one: an edit layer that wraps a refusal in its own exception must not hide the question."""
    seen: BaseException | None = error
    for _ in range(4):
        if seen is None:
            return None
        if _named(seen, "NeedsConfirmation") or _named(seen, "NeedsChoice"):
            return seen
        seen = seen.__cause__ or seen.__context__
    return None


def _choice_row(index: int, option: Any) -> dict[str, Any]:
    if isinstance(option, dict):
        return {"index": index, **option}
    row: dict[str, Any] = {"index": index, "label": str(getattr(option, "label", option))}
    fill = getattr(option, "fill", None)
    if isinstance(fill, dict) and fill:
        row["fill"] = fill
    return row


def classify(error: BaseException) -> ImportedEditError:
    """The API error for anything a batch can raise (the table in the API contract).

    NeedsConfirmation and NeedsChoice are defined by the edit layer (report_edit_context); they
    are recognised by name so this module does not depend on where that layer keeps them.
    """
    if isinstance(error, ImportedEditError):
        return error
    asking = _asks_the_user(error)
    if asking is not None:
        # The step (counted from 1) the question is about: an answer names it, so the server
        # writes the answer's ``fill`` over that operation (``merge_answers``).
        step = getattr(asking, "step", None)
        where: dict[str, Any] = {"step": step} if isinstance(step, int) else {}
        if _named(asking, "NeedsChoice"):
            options = getattr(asking, "options", None) or getattr(asking, "choices", None) or []
            question = str(getattr(asking, "question", "") or str(asking))
            return ImportedEditError(
                "needs_choice",
                question,
                409,
                question=question,
                choices=[_choice_row(index, option) for index, option in enumerate(options)],
                **where,
            )
        detail = str(getattr(asking, "detail", "") or str(asking))
        kind = str(getattr(asking, "kind", "replace_conditional"))
        if kind == TESTS_CONFIRMATION:
            # The whole batch is asked about (not a step): the broken tests, and the
            # request field that answers yes (docs/imported-report-tests.md, section 8).
            results = getattr(asking, "results", None) or []
            where = {
                "tests": [
                    r.model_dump(mode="json") if hasattr(r, "model_dump") else dict(r)
                    for r in results
                ],
                "confirm": {"confirmed": [TESTS_CONFIRMATION]},
            }
        return ImportedEditError(
            "needs_confirmation",
            detail,
            409,
            kind=kind,
            detail=detail,
            **where,
        )
    if isinstance(error, RevisionConflict):
        return ImportedEditError(
            "head_moved",
            str(error),
            409,
            expected=error.expected,
            actual=error.actual,
        )
    if isinstance(error, UnsavedDesktopWork):
        return ImportedEditError(
            "desktop_unsaved",
            str(error),
            409,
            choices=["save_and_continue", "cancel"],
        )
    if isinstance(error, RevisionBusy):
        return ImportedEditError("edit_busy", str(error), 409)
    if isinstance(error, RestoreNeedsConfirmation):
        return ImportedEditError(
            "needs_confirmation",
            str(error),
            409,
            kind="restore_discard",
            detail=str(error),
            would_discard=[_revision_row(row) | {"summary": row.summary} for row in error.later],
        )
    if isinstance(error, EditRejected):
        dialog = getattr(error, "dialog", None)
        if dialog is None:
            found = _DIALOG.search(str(error))
            dialog = found.group("dialog") if found else None
        if dialog is None and _DIALOG_WHILE_SAVING.search(str(error)):
            dialog = "a dialog in Power BI Desktop"
        if dialog is not None:
            return ImportedEditError("desktop_dialog", str(error), 409, dialog=str(dialog))
        step = _STEP_ANYWHERE.match(str(error))
        return ImportedEditError(
            "edit_rejected",
            str(error),
            422,
            step=int(step.group("step")) if step else None,
        )
    if isinstance(error, WorkingCopyBusy):
        return ImportedEditError("desktop_busy", str(error), 409)
    if isinstance(error, UserChangesPending | StateConflictError):
        return ImportedEditError("conflict", str(error), 409)
    if isinstance(error, DesktopSessionError):
        dialog = getattr(error, "dialog", None)
        if isinstance(dialog, str) and dialog:
            return ImportedEditError("desktop_dialog", str(error), 409, dialog=dialog)
        return ImportedEditError("desktop_unavailable", str(error) or _NEEDS_DESKTOP, 503)
    if isinstance(error, ValueError | KeyError | FileNotFoundError):
        text = str(error).strip("'\"") or "This change cannot be applied."
        return ImportedEditError("edit_rejected", text, 422, step=None)
    if isinstance(error, PermissionError | FileExistsError):
        return ImportedEditError(
            "conflict",
            f"A file could not be written ({error}). Choose another place, or close the "
            "program that has it open.",
            409,
        )
    return ImportedEditError(
        "edit_failed",
        "Studio could not finish this change. Check the report and try again.",
        500,
    )


# --- Power BI Desktop as this module reaches it ---------------------------------------------


@dataclass(frozen=True)
class DesktopTools:
    """The pinned tools Desktop control needs. Empty fields mean "not found"; ``problems`` says
    why in words (a tool of the wrong version, for example)."""

    bridge: list[str]
    pbi: str
    desktop_exe: str
    problems: tuple[str, ...] = ()


def bridge_argv(path: str) -> list[str]:
    """The command that runs the Desktop bridge. An npm ``.cmd`` shim is run as node plus the
    bridge's own script, so a report's file name never goes through cmd.exe."""
    if Path(path).suffix.casefold() == ".cmd":
        folder = Path(path).parent
        node = folder / "node.exe"
        entry = folder / "node_modules/@microsoft/powerbi-desktop-bridge-cli/dist/index.js"
        if node.is_file() and entry.is_file():
            return [str(node), str(entry)]
        return []
    return [path] if path else []


class NoDesktop:
    """The Desktop port when Power BI Desktop cannot be controlled from here.

    It reports "not open" for every file, so an edit to the report layer goes ahead exactly as
    it would with Desktop closed; anything that needs Desktop says so in plain words.
    """

    def find(self, path: Path) -> DesktopInstance | None:
        return None

    def status(self, instance: DesktopInstance) -> dict[str, Any]:
        raise DesktopSessionError(_NEEDS_DESKTOP)

    def open(self, path: Path, *, normalize: bool = True) -> DesktopInstance:
        raise DesktopSessionError(_NEEDS_DESKTOP)

    def save(self, instance: DesktopInstance) -> object:
        raise DesktopSessionError(_NEEDS_DESKTOP)

    def close(self, instance: DesktopInstance) -> None:
        raise DesktopSessionError(_NEEDS_DESKTOP)

    def apply_measure_operations(
        self, instance: DesktopInstance, operations: list[Any], tables: dict[str, str]
    ) -> list[str]:
        raise DesktopSessionError(_NEEDS_DESKTOP)

    def screenshot(self, instance: DesktopInstance, page_id: str, output: Path) -> Path:
        raise DesktopSessionError(_NEEDS_DESKTOP)

    def blocking_windows(self, instance: DesktopInstance) -> list[str]:
        raise DesktopSessionError(_NEEDS_DESKTOP)

    def version(self) -> str | None:
        return None


@dataclass
class _Desktop:
    port: DesktopPort
    readiness: DesktopReadiness
    checked: float = field(default_factory=time.monotonic)

    @property
    def usable(self) -> bool:
        return not isinstance(self.port, NoDesktop)


def _receipts_folder(state_root: Path, version: str | None) -> Path:
    """Where the contract receipt of ``version`` is read from: the per-user folder when it has
    one (a check run on this computer), else the receipts that ship with Studio."""
    own = state_root / "desktop-receipts"
    if version and read_receipt(version, own) is not None:
        return own
    bundled = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundled:
        return Path(str(bundled)) / "evals" / "imported_edit" / "evidence"
    return default_receipts_dir()


def discover_desktop(state_root: Path, tools: DesktopTools) -> _Desktop:
    """A controller for the tools found, or the null port with the reasons why not."""
    exe_ok = bool(tools.desktop_exe) and Path(tools.desktop_exe).is_file()
    if not tools.bridge or not exe_ok:
        reasons = list(tools.problems)
        if not exe_ok and "Power BI Desktop is not installed" not in reasons:
            reasons.append("Power BI Desktop is not installed")
        if not tools.bridge and not any("bridge" in reason for reason in reasons):
            reasons.append("The Power BI Desktop bridge is not installed")
        return _Desktop(NoDesktop(), DesktopReadiness(False, False, exe_ok, False, None, reasons))
    version = DesktopController(tools.bridge, tools.pbi, tools.desktop_exe).version()
    controller = DesktopController(
        tools.bridge,
        tools.pbi,
        tools.desktop_exe,
        receipts_dir=_receipts_folder(state_root, version),
    )
    readiness = controller.readiness()
    if tools.problems:
        readiness = DesktopReadiness(
            readiness.bridge_ok,
            readiness.pbi_ok,
            readiness.desktop_ok,
            readiness.contract_passed,
            readiness.version,
            [*tools.problems, *(r for r in readiness.reasons if r not in tools.problems)],
        )
    return _Desktop(controller, readiness)


def _alt_text(definition: object) -> str | None:
    """A visual's alt text (``visualContainerObjects.general`` ``altText``, where Desktop and
    ``set_alt_text`` write it), None when it has none."""
    if not isinstance(definition, dict):
        return None
    body = definition.get("visual")
    objects = body.get("visualContainerObjects") if isinstance(body, dict) else None
    entries = objects.get("general") if isinstance(objects, dict) else None
    found: str | None = None
    for entry in entries if isinstance(entries, list) else []:
        props = entry.get("properties") if isinstance(entry, dict) else None
        if isinstance(props, dict) and "altText" in props:
            value = pbir_expr.decode(props["altText"])
            found = value if isinstance(value, str) and value.strip() else None
    return found


def model_support_payload(version: str | None, verified: Iterable[str]) -> dict[str, Any]:
    """``capabilities.model_support``: which model operations Microsoft documents for tools like
    Studio, which are outside that list, and which of those the installed Desktop passed.

    ``verified`` are the probe keys of the model probe receipt for exactly ``version`` (what the
    editor's gate reads, ``DesktopController.model_probe_coverage``). An operation outside the
    list, or a "depends" one on a table or column loaded from the data source, is refused before
    Desktop opens unless its probe key is verified; a "depends" one on a calculated object is
    documented and always allowed."""
    tags = report_edit_model.MICROSOFT_SUPPORT
    keys = report_edit_model.support_keys()
    passed = {str(key) for key in verified} & set(report_edit_model.OUTSIDE_SUPPORT)
    gated = [op for op, tag in tags.items() if tag in ("outside", "depends")]
    return {
        "desktop_version": version,
        "documented": [op for op, tag in tags.items() if tag == "documented"],
        "outside": [op for op, tag in tags.items() if tag == "outside"],
        "depends": [op for op, tag in tags.items() if tag == "depends"],
        "verified": [op for op in gated if keys[op] in passed],
        "keys": {op: keys[op] for op in gated},
        "verified_keys": sorted(passed),
    }


# --- reading a model in a child process -----------------------------------------------------


def extract_command(path: Path, flag: str) -> list[str]:
    """The command that runs pbix_extract on ``path``.

    From source or the sealed runtime ``sys.executable`` is a Python and runs the script. In
    the PyInstaller build it is the app itself, which has no script to run: the module form is
    what report_import.extract_model uses there too, so the one dispatch the build needs (the
    entry point running ``-m <module>``) serves both.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "-m", "ai_powerbi_analyst.studio.pbix_extract", str(path), flag]
    return [sys.executable, str(_EXTRACT_SCRIPT), str(path), flag]


def run_extract(path: Path, flag: str, *, timeout: float = _EXTRACT_TIMEOUT) -> Any:
    """Run pbix_extract on ``path`` with ``--measures`` or ``--types`` and return its JSON.

    Decoding a model needs a lot of memory and must not take the sidecar down, so it runs in
    a child process that is stopped when it outgrows its share of free memory or its time,
    the way report_import.extract_model runs the full decoder (no console window). Raises
    EditRejected, in words a user can act on, when it fails.
    """
    available = psutil.virtual_memory().available
    if available < _MIN_FREE_MEMORY:
        raise EditRejected(
            "Studio needs about 512 MB of free memory to read the report's model. Close some "
            "apps, then try again."
        )
    budget = min(2 * 1024**3, int(available * 0.55))
    failure = EditRejected(
        "Studio could not read the model back from the report, so the change was not kept."
    )
    with tempfile.TemporaryFile() as out:
        process = subprocess.Popen(  # noqa: S603 - Studio's own script, argv only, no shell
            extract_command(path, flag),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.DEVNULL,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                try:
                    used = psutil.Process(process.pid).memory_info().rss
                except psutil.Error:
                    break
                if used > budget or time.monotonic() > deadline:
                    raise failure
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        if process.returncode != 0:
            raise failure
        out.seek(0)
        data = out.read(_EXTRACT_MAX_BYTES + 1)
    if len(data) > _EXTRACT_MAX_BYTES:
        raise failure
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise failure from error


def read_measures_bounded(path: Path) -> list[dict[str, Any]]:
    """The model's measures as saved in ``path`` (see ``run_extract``)."""
    found = run_extract(path, "--measures")
    if not isinstance(found, list):
        raise EditRejected(
            "Studio could not read the measures back from the report, so the change was not kept."
        )
    return [item for item in found if isinstance(item, dict)]


def read_types_bounded(path: Path) -> dict[str, Any]:
    """Column and measure types of the model in ``path`` (see ``run_extract``)."""
    found = run_extract(path, "--types")
    if not isinstance(found, dict):
        raise EditRejected("Studio could not read the model's data types.")
    return found


_STREAM_STORAGE = report_import.STREAM_STORAGE


def readable_model(path: Path) -> bool:
    """Whether ``path`` holds a DataModel Power BI wrote (``report_import.readable_model``)."""
    return report_import.readable_model(path)


def read_model_facts_bounded(path: Path) -> dict[str, Any]:
    """``pbix_extract.read_model_facts`` of the model in ``path``, read in a bounded child
    process (see ``run_extract``); the schema is in that function's docstring. Raises
    EditRejected, in plain words, when the file has no model Studio can read or the read fails.
    """
    if not readable_model(path):
        raise EditRejected("Studio could not read the report's model.")
    found = run_extract(path, "--facts")
    if not isinstance(found, dict) or not isinstance(found.get("version"), int):
        raise EditRejected("Studio could not read the report's model.")
    return found


@dataclass(frozen=True)
class ColumnFacts:
    """What the model says about one column."""

    data_type: str | None  # text, integer, double, decimal, datetime, boolean; None if unknown
    summarize_by: str | None  # the model's default aggregation, None when it says "none"
    hidden: bool = False
    key: bool = False


@dataclass(frozen=True)
class ModelTypes:
    """Data types of the model's columns and measures, for typing filter and color literals.

    Read from the head file's model metadata, once per model (the DataModel's CRC and size),
    so report-layer edits reuse it and a Desktop save that changed the model reads it again.
    """

    model_key: str
    columns: dict[tuple[str, str], ColumnFacts]
    measures: dict[tuple[str, str], str | None]
    # The DAX of calculated columns and tables (``"Table[Column]"`` or ``"Table"``), which the
    # edit layer reads to see what a rename or delete of a measure would break. None: the
    # extraction could not tell (an empty dict means the model has none).
    calculated: dict[str, str] | None = None

    @property
    def known(self) -> bool:
        """False when the model's metadata gave no types at all (the read worked, the
        metadata query behind it did not): the maps are then empty because nothing is known."""
        return bool(self.columns or self.measures)

    def payload(self) -> dict[str, Any]:
        """The ``{"columns": [...], "measures": [...]}`` that ``ModelInfo.from_report`` takes."""
        return self._rows()

    @classmethod
    def from_extract(cls, model_key: str, raw: dict[str, Any]) -> ModelTypes:
        columns: dict[tuple[str, str], ColumnFacts] = {}
        for item in raw.get("columns", []) if isinstance(raw.get("columns"), list) else []:
            if not isinstance(item, dict) or not item.get("table") or not item.get("name"):
                continue
            summarize = item.get("summarizeBy")
            columns[(str(item["table"]), str(item["name"]))] = ColumnFacts(
                data_type=pbir_expr.data_type_name(item.get("dataType")),
                summarize_by=str(summarize) if isinstance(summarize, str) and summarize else None,
                hidden=bool(item.get("hidden")),
                key=bool(item.get("key")),
            )
        measures: dict[tuple[str, str], str | None] = {}
        for item in raw.get("measures", []) if isinstance(raw.get("measures"), list) else []:
            if isinstance(item, dict) and item.get("table") and item.get("name"):
                measures[(str(item["table"]), str(item["name"]))] = pbir_expr.data_type_name(
                    item.get("dataType")
                )
        raw_calculated = raw.get("calculated")
        calculated = (
            {str(k): v for k, v in raw_calculated.items() if isinstance(v, str)}
            if isinstance(raw_calculated, dict)
            else None
        )
        return cls(model_key, columns, measures, calculated)

    def to_json(self) -> dict[str, Any]:
        found: dict[str, Any] = self._rows()
        if self.calculated is not None:
            found["calculated"] = dict(self.calculated)
        return {"v": TYPES_VERSION, "model_key": self.model_key, **found}

    def _rows(self) -> dict[str, Any]:
        return {
            "columns": [
                {
                    "table": t,
                    "name": n,
                    "dataType": f.data_type,
                    "summarizeBy": f.summarize_by,
                    "hidden": f.hidden,
                    "key": f.key,
                }
                for (t, n), f in self.columns.items()
            ],
            "measures": [
                {"table": t, "name": n, "dataType": d} for (t, n), d in self.measures.items()
            ],
        }

    def column(self, table: str, name: str) -> ColumnFacts | None:
        wanted = (table.casefold(), name.casefold())
        return next(
            (f for (t, n), f in self.columns.items() if (t.casefold(), n.casefold()) == wanted),
            None,
        )

    def data_type(self, table: str, name: str) -> str | None:
        """The type of a column or measure by its (case-insensitive) name, None if unknown."""
        facts = self.column(table, name)
        if facts is not None:
            return facts.data_type
        wanted = (table.casefold(), name.casefold())
        return next(
            (d for (t, n), d in self.measures.items() if (t.casefold(), n.casefold()) == wanted),
            None,
        )


def _has_data_model(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return "DataModel" in archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


# --- batches --------------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchOutcome:
    """What an HTTP route answers: 200 with the result, or 202 while the batch still runs."""

    status: int
    body: dict[str, Any]


class BatchHandle:
    """One batch as callers see it: its state, its progress, and how it ended.

    Safe to read from any thread. ``events`` replays the progress from the start, so a client
    that attaches late (a chat stream after a reload) still sees every step.
    """

    def __init__(
        self,
        *,
        batch_id: str,
        report_id: str,
        workspace_id: str,
        kind: str,
        key: str,
        fingerprint: str,
        summary: str = "",
    ) -> None:
        self.id = batch_id
        self.report_id = report_id
        self.workspace_id = workspace_id
        self.kind = kind
        self.key = key
        self.fingerprint = fingerprint
        self.summary = summary
        self.created = _now()
        self.finished: str | None = None
        self.result: dict[str, Any] | None = None
        self.error: ImportedEditError | None = None
        self._state: BatchState = "queued"
        self._pending: tuple[BatchState, dict[str, Any] | None, ImportedEditError | None] | None = (
            None
        )
        self._completion_warning: str | None = None
        self._terminal_mirrored = False
        self._events: list[dict[str, Any]] = []
        self._cond = threading.Condition()
        self._callbacks: list[Callable[[BatchHandle], None]] = []

    @property
    def state(self) -> BatchState:
        with self._cond:
            return self._state

    def _start(self) -> None:
        with self._cond:
            self._state = "running"
            self._events.append({"stage": "planning", "label": "Getting ready"})
            self._cond.notify_all()

    def progress(self, text: str) -> None:
        """Record a step the editor announced (its words become the label)."""
        with self._cond:
            self._events.append({"stage": _STAGES.get(text, "applying"), "label": text})
            self._cond.notify_all()

    def _complete(self, result: dict[str, Any]) -> None:
        with self._cond:
            self._pending = ("done", result, None)

    def _fail(self, error: ImportedEditError) -> None:
        with self._cond:
            self._pending = ("failed", None, error)

    def _completion(self) -> tuple[BatchState, dict[str, Any] | None, ImportedEditError | None]:
        """The staged outcome for bookkeeping, before callers may observe completion."""
        with self._cond:
            return self._pending or (self._state, self.result, self.error)

    def _warn_completion(self) -> None:
        with self._cond:
            self._completion_warning = (
                "Studio could not save all batch completion details. Check the report history."
            )
            if self._pending is not None:
                state, result, error = self._pending
                if result is not None and self.kind in RECEIPT_KINDS:
                    warnings = list(result.get("warnings") or [])
                    if self._completion_warning not in warnings:
                        warnings.append(self._completion_warning)
                    self._pending = (state, {**result, "warnings": warnings}, error)

    def _mark_terminal_mirrored(self) -> None:
        """Only a successfully saved terminal mirror permits registry eviction."""
        with self._cond:
            if self._pending is not None and self._pending[0] in _TERMINAL:
                self._terminal_mirrored = True

    def _can_forget(self, *, completing: bool = False) -> bool:
        with self._cond:
            state = self._pending[0] if completing and self._pending is not None else self._state
            return self._terminal_mirrored and state in _TERMINAL

    def _publish(self) -> list[Callable[[BatchHandle], None]]:
        """Publish only after bookkeeping; drain listeners in the same atomic step."""
        with self._cond:
            assert self._pending is not None
            self._state, self.result, self.error = self._pending
            self._pending = None
            self.finished = _now()
            callbacks, self._callbacks = self._callbacks, []
            self._cond.notify_all()
            return callbacks

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the batch has finished or ``timeout`` passes; True when finished."""
        with self._cond:
            return self._cond.wait_for(lambda: self._state in _TERMINAL, timeout)

    def events(self, heartbeat: float | None = None) -> Iterator[dict[str, Any] | None]:
        """Yield each progress event as it happens, then stop once the batch has finished.

        With ``heartbeat`` seconds set, yields None whenever that long passes with nothing
        new, so a streaming caller can keep its connection alive.
        """
        index = 0

        def news() -> bool:
            return index < len(self._events) or self._state in _TERMINAL

        while True:
            with self._cond:
                ready = self._cond.wait_for(news, heartbeat)
                fresh = self._events[index:]
                index = len(self._events)
                finished = self._state in _TERMINAL and not fresh
            if not ready:
                yield None
                continue
            yield from fresh
            if finished:
                return

    def on_complete(self, callback: Callable[[BatchHandle], None]) -> None:
        """Call ``callback(handle)`` once the batch has finished (at once if it has)."""
        with self._cond:
            if self._state not in _TERMINAL:
                self._callbacks.append(callback)
                return
        callback(self)

    def status(self) -> dict[str, Any]:
        """``{batch_id, state, kind, stage?, label?, receipt?, error?}``: the body of the poll
        route. ``stage`` and ``label`` are the latest progress of the batch (what a chat stream
        gets as ``edit_progress``), so a client that polls can show the same steps; absent
        while the batch is still queued."""
        with self._cond:
            state = self._state
            latest = self._events[-1] if self._events else None
            result, error = self.result, self.error
            warning = self._completion_warning if state in _TERMINAL else None
        body: dict[str, Any] = {"batch_id": self.id, "state": state, "kind": self.kind}
        if latest is not None:
            body["stage"] = latest["stage"]
            body["label"] = latest["label"]
        if result is not None:
            body["receipt"] = result
        if error is not None:
            body["error"] = error.payload()
        if warning is not None:
            body["completion_warning"] = warning
        return body


class _Lane:
    """The serial worker of one report: batches run one after another on one thread that
    exists only while there is work."""

    def __init__(self, service: ImportedEditingService, report_id: str) -> None:
        self._service = service
        self.report_id = report_id
        self._lock = threading.Lock()
        self._queue: deque[tuple[BatchHandle, Job]] = deque()
        self._thread: threading.Thread | None = None
        self.current: BatchHandle | None = None

    def submit(self, handle: BatchHandle, job: Job) -> None:
        with self._lock:
            self._queue.append((handle, job))
            if self._thread is None:
                # Daemon: a batch that a restart interrupts is finished or undone by the
                # journal at the next start (P9), so nothing waits on this thread.
                self._thread = threading.Thread(
                    target=self._run, name=f"edit-lane-{self.report_id}", daemon=True
                )
                self._thread.start()

    def cancel(self, handle: BatchHandle) -> bool:
        """Remove a batch that has not started; False once it is running or finished."""
        with self._lock:
            for item in self._queue:
                if item[0] is handle:
                    self._queue.remove(item)
                    return True
        return False

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._queue) or self.current is not None

    def _run(self) -> None:
        try:
            while True:
                with self._lock:
                    if not self._queue:
                        self._thread = None
                        self.current = None
                        return
                    handle, job = self._queue.popleft()
                    self.current = handle
                self._service._execute(handle, job)  # noqa: SLF001 - the lane is the service's own
        except BaseException:
            # Only something that is not an Exception gets here (``_execute`` turns every
            # Exception into the batch's outcome). The batch was failed; the lane must not
            # stay marked as running, or nothing queued behind it would ever start.
            with self._lock:
                self.current = None
                self._thread = None
                if self._queue:
                    self._thread = threading.Thread(
                        target=self._run, name=f"edit-lane-{self.report_id}", daemon=True
                    )
                    self._thread.start()
            raise


# --- the service ----------------------------------------------------------------------------


class EditPort(Protocol):
    """What chat (and any other in-process caller) uses to edit an imported report.

    Every method is synchronous and takes the report's lane like an HTTP request does, so a
    chat edit and a Format-pane edit are serialized and journaled the same way. A method that
    changes the report returns the receipt as the API shows it (``EditReceipt`` in
    docs/imported-report-editing-api.md, a plain dict) or raises ``ImportedEditError`` whose
    ``code`` is one of the API's (head_moved, desktop_unsaved, needs_confirmation, ...).

    ``progress`` is called on the caller's thread with ``{"stage", "label"}`` while the batch
    runs (planning, closing_desktop, writing, opening_desktop, applying, saving).
    """

    def head(self, workspace_id: str) -> dict[str, Any]:
        """``{id, hash}`` of the report's head revision."""
        ...

    def edit_context(
        self, workspace_id: str, selection: str | None, active_page: str | None
    ) -> EditContext:
        """The request-scoped index the edit parser reads (edit_context.EditContext): the
        report as it is now, the head id, the theme's palette and the newest change."""
        ...

    def apply(
        self,
        workspace_id: str,
        ops: Sequence[dict[str, Any]],
        *,
        summary: str | None,
        idempotency_key: str,
        expected_head: str,
        progress: Callable[[dict[str, Any]], None] | None = None,
        desktop_work: DesktopWork = "ask",
        confirm_model: bool = False,
        confirm_replace: bool = False,
        answers: Sequence[Any] | None = None,
        confirmed: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Apply ``ops`` as one revision. Model operations need ``confirm_model``. ``answers``
        are ``[{step, fill}]`` replies to an earlier ``needs_choice``, written over the operations
        by the service (``merge_answers``). ``confirmed`` are batch-wide yeses
        (``report_tests_broken``)."""
        ...

    def undo(
        self,
        workspace_id: str,
        *,
        expected_head: str,
        idempotency_key: str,
        progress: Callable[[dict[str, Any]], None] | None = None,
        desktop_work: DesktopWork = "ask",
    ) -> dict[str, Any]:
        """Revert the newest change (only that one)."""
        ...

    def restore(
        self,
        workspace_id: str,
        revision_id: str,
        *,
        expected_head: str,
        idempotency_key: str,
        confirmed_discard: bool = False,
        progress: Callable[[dict[str, Any]], None] | None = None,
        desktop_work: DesktopWork = "ask",
    ) -> dict[str, Any]:
        """Make an earlier revision the head again; later changes need ``confirmed_discard``."""
        ...

    def history(self, workspace_id: str) -> list[dict[str, Any]]:
        """History rows, newest first (``history_row``)."""
        ...

    def what_changed(self, workspace_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """The newest changes with the operations each made, for "what did you change"."""
        ...

    def remember_last_edit(
        self, workspace_id: str, revision_id: str, ops: Sequence[dict[str, Any]], summary: str
    ) -> None:
        """Note what the newest revision changed, for "undo the color change"; used by
        ``edit_context`` while that revision is the head."""
        ...


class ImportedEditingService:
    """Editing of imported reports: lanes, the batch registry, rederivation, recovery."""

    def __init__(
        self,
        state_root: Path,
        store: StudioStateStore,
        *,
        load_report: Callable[[str], ImportedReport],
        tools: Callable[[], DesktopTools] | None = None,
        port_factory: Callable[[], DesktopPort] | None = None,
        readiness_factory: Callable[[], DesktopReadiness] | None = None,
        read_measures: ReadMeasures | None = None,
        read_types: ReadTypes | None = None,
        before_launch: Callable[[], None] | None = None,
        wait_seconds: float = WAIT_SECONDS,
        disk_free: Callable[[Path], int] | None = None,
        profile_dir: Path | None = None,
        read_facts: ReadFacts | None = None,
        snapshot_of: SnapshotOf | None = None,
        tests_evaluator: TestsEvaluator | None = None,
    ) -> None:
        self.state_root = Path(state_root)
        self._snapshot_of = snapshot_of
        self._tests_evaluator = tests_evaluator
        # Report tests per report (reports/<id>/tests.json), and the health checks of recent heads.
        self.tests = report_tests.TestStore(self.state_root)
        self._health_lock = threading.Lock()
        self._health: OrderedDict[tuple[str, ...], model_health.Health] = OrderedDict()
        self.store = store
        self._load_report = load_report
        self._tools = tools
        self._port_factory = port_factory
        self._readiness_factory = readiness_factory
        self._read_measures = read_measures or read_measures_bounded
        self._read_types = read_types or read_types_bounded
        self._read_facts = read_facts or read_model_facts_bounded
        self._before_launch = before_launch
        self.wait_seconds = wait_seconds
        self._registry = threading.Lock()
        self._mirror_lock = threading.Lock()
        self._lanes: dict[str, _Lane] = {}
        self._batches: OrderedDict[str, BatchHandle] = OrderedDict()
        self._by_key: dict[tuple[str, str], BatchHandle] = {}
        self._desktop: _Desktop | None = None
        self._desktop_lock = threading.Lock()
        self._discover_lock = threading.Lock()
        self._discovering = False
        self._discovery: threading.Thread | None = None
        self._closing = False
        self._state_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._heal_failed: dict[str, tuple[str, float]] = {}
        self._hash_lock = threading.Lock()
        self._hashes: OrderedDict[tuple[str, int, int], str] = OrderedDict()
        self._types: dict[str, ModelTypes] = {}
        self._types_failed: dict[str, tuple[str, float]] = {}
        self._facts_lock = threading.Lock()
        self._facts: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._facts_failed: dict[tuple[str, str], float] = {}
        self._filling: set[str] = set()
        self._last_edits: dict[str, LastEdit] = {}
        self._recovery: threading.Thread | None = None
        self.recovered_desktops: dict[str, DesktopInstance] = {}
        self.on_batch_finished: list[Callable[[BatchHandle], None]] = []
        self.editor = ReportEditor(
            self.state_root / "reports",
            self._rederive,
            self._port,
            read_measures=self._read_measures,
            disk_free=disk_free,
            profile_dir=profile_dir,
            model_facts=self.model_facts,
            tests_gate=_ReportTestsGate(self),
        )

    # --- Power BI Desktop ---------------------------------------------------------------------

    def _fresh_desktop(self) -> _Desktop | None:
        with self._desktop_lock:
            cached = self._desktop
        if cached is not None:
            age = time.monotonic() - cached.checked
            if age < (FOUND_TTL if cached.usable else MISSING_TTL):
                return cached
        return None

    def _discover(self) -> _Desktop:
        if self._port_factory is not None:
            port = self._port_factory()
            readiness = (
                self._readiness_factory()
                if self._readiness_factory is not None
                else DesktopReadiness(True, True, True, True, port.version(), [])
            )
            return _Desktop(port, readiness)
        if self._tools is None:
            return _Desktop(
                NoDesktop(), DesktopReadiness(False, False, False, False, None, [_NEEDS_DESKTOP])
            )
        try:
            return discover_desktop(self.state_root, self._tools())
        except Exception as error:  # noqa: BLE001 - a broken probe must not break editing
            LOGGER.warning("Looking for Power BI Desktop failed: %s", error)
            reasons = ["Studio could not check for Power BI Desktop"]
            return _Desktop(
                NoDesktop(), DesktopReadiness(False, False, False, False, None, reasons)
            )

    def _desktop_now(self) -> _Desktop:
        """The Desktop controller, looked for on first use (the search runs the tools' version
        checks, which take seconds) and again after a while. The search is serialized on its
        own lock so that reading the last answer never waits for one."""
        found = self._fresh_desktop()
        if found is not None:
            return found
        with self._discover_lock:
            found = self._fresh_desktop()
            if found is None:
                found = self._discover()
                with self._desktop_lock:
                    self._desktop = found
            return found

    def _port(self) -> DesktopPort:
        return self._desktop_now().port

    def forget_desktop(self) -> None:
        """Look for Desktop again on next use (after the tools were installed or changed)."""
        with self._desktop_lock:
            self._desktop = None

    def _desktop_peek(self) -> _Desktop | None:
        """The Desktop found so far, without looking. The first ask, and the first after the
        answer has gone stale (FOUND_TTL / MISSING_TTL), starts the search in the background so
        a payload never waits for the tools' version checks, and returns the last answer
        meanwhile (an injected port needs no search and is used at once)."""
        with self._desktop_lock:
            if self._closing:
                return self._desktop
        if self._port_factory is not None or self._tools is None:
            return self._desktop_now()  # nothing to search for: the answer is immediate
        with self._desktop_lock:
            cached = self._desktop
            if self._closing:
                return cached
            stale = cached is None or time.monotonic() - cached.checked >= (
                FOUND_TTL if cached.usable else MISSING_TTL
            )
            starting = stale and not self._discovering
            if starting:
                self._discovering = True

                def search() -> None:
                    try:
                        with self._desktop_lock:
                            if self._closing:
                                return
                        self._desktop_now()
                    finally:
                        with self._desktop_lock:
                            self._discovering = False

                self._discovery = threading.Thread(
                    target=search, name="desktop-discovery", daemon=True
                )
                # Starting under the same lock lets close capture only a started thread.
                self._discovery.start()
        return cached

    def readiness(self, *, wait: bool = False) -> DesktopReadiness | None:
        """Whether model edits can run; None while the first look is still under way."""
        found = self._desktop_now() if wait else self._desktop_peek()
        return None if found is None else found.readiness

    # --- reports, heads, capabilities -------------------------------------------------------

    def report(self, workspace_id: str, *, in_lane: bool = False) -> ImportedReport:
        """The workspace's imported report, moved to the head first if a batch left Studio's
        view behind it (a refresh that failed after the head moved). ``in_lane`` is for a job
        running on the report's own lane: it may refresh a view that is behind even though the
        lane is busy (with itself)."""
        return self._heal(self._load_report(workspace_id), in_lane=in_lane)

    def _store_of(self, report: ImportedReport) -> RevisionStore:
        return self.editor.store(report)

    def head(self, workspace_id: str) -> dict[str, Any]:
        row = self._store_of(self._load_report(workspace_id)).head()
        return {"id": row.id, "hash": row.blob}

    def require_head(self, report: ImportedReport, revision: str | None) -> Revision:
        """The head revision, after checking that the caller's ``revision`` is it (a render or
        preview of an older head would show an old picture as if it were current)."""
        head = self._store_of(report).head()
        if revision is not None and revision != head.id:
            raise ImportedEditError(
                "head_moved",
                "The report changed while this view was loading. Studio is showing the "
                "latest version.",
                409,
                expected=revision,
                actual=head.id,
            )
        return head

    def preview_page(
        self,
        engine: NativeReportPreview,
        report: ImportedReport,
        page_id: str,
        refresh: bool,
        revision: str | None,
    ) -> dict[str, Any]:
        """A Power BI Desktop screenshot of one page of the head (P18).

        A capture is taken only while Desktop shows exactly the head, saved: the working copy
        is the head (NativeReportPreview checks that) and Desktop has no unsaved changes, or its
        picture would not match what Studio draws. A picture kept for this head is served
        without Desktop. An older head, or a Desktop showing an older version, is a 409 in
        plain words rather than a picture of the wrong report.
        """
        head = self.require_head(report, revision)
        store = self._store_of(report)
        try:
            kept = engine.preview_path(report, page_id).is_file() and not refresh
        except ValueError:
            kept = False
        if not kept:
            try:
                desktop = self._desktop_now()
                instance = desktop.port.find(store.working_copy) if desktop.usable else None
                dirty = instance is not None and bool(
                    desktop.port.status(instance).get("hasUnsavedChanges")
                )
            except (DesktopSessionError, OSError, subprocess.SubprocessError) as error:
                raise ImportedEditError(
                    "desktop_unavailable", str(error) or _NEEDS_DESKTOP, 503
                ) from error
            if dirty:
                raise ImportedEditError(
                    "desktop_unsaved",
                    "Power BI Desktop has unsaved changes, so its preview would not match "
                    "Studio. Save the report in Power BI Desktop, then try again.",
                    409,
                    choices=["cancel"],
                )
        try:
            return engine.page(report, page_id, refresh, head_hash=head.blob)
        except ValueError as error:
            if str(error) == STALE_VIEW:
                raise ImportedEditError(
                    "head_moved", STALE_VIEW, 409, expected=revision, actual=head.id
                ) from error
            if str(error) == STALE_DESKTOP:
                raise ImportedEditError("desktop_stale", STALE_DESKTOP, 409) from error
            raise

    def _heal(self, report: ImportedReport, *, in_lane: bool = False) -> ImportedReport:
        store = self._store_of(report)
        if not store.index_path.is_file():
            return report
        try:
            head = store.head()
            _, current = report_import.report_archive(report)
        except (RevisionError, ValueError, OSError):
            return report
        if current == head.blob or not store.available(head):
            return report
        if not in_lane and self._lane_busy(report.id):
            return report  # the running batch refreshes the view itself when it finishes
        failed = self._heal_failed.get(report.id)
        if (
            not in_lane
            and failed is not None
            and failed[0] == head.blob
            and time.monotonic() - failed[1] < HEAL_BACKOFF
        ):
            # A refresh reads the model in a child process (seconds, up to 2 GB) under the edit
            # lock; a head it just failed to read is not tried again on every GET.
            return report
        try:
            with store.lock(timeout=2.0):
                fresh = self._load_report(report.workspace_id)
                head = store.head()
                _, current = report_import.report_archive(fresh)
                if current == head.blob:
                    return fresh
                healed = self._rederive(fresh, store.path(head), head.blob, True)
                self._heal_failed.pop(report.id, None)
                return healed
        except RevisionBusy:
            return report  # the lock is only busy; nothing failed
        except (RevisionError, StateConflictError, EditRejected, OSError, ValueError):
            LOGGER.warning("Could not bring the view of report %s up to its head", report.id)
            self._heal_failed[report.id] = (head.blob, time.monotonic())
            return report

    def _lane_busy(self, report_id: str) -> bool:
        with self._registry:
            lane = self._lanes.get(report_id)
        return lane is not None and lane.busy

    def capabilities(self, report: ImportedReport, *, wait: bool = False) -> dict[str, Any]:
        """What can be edited: the report layer, and (with Desktop set up and checked) the
        model. ``reasons`` says in words what stops a model edit."""
        store = self._store_of(report)
        reasons: list[str] = []
        edit_report = report.format == "PBIR"
        if not edit_report:
            reasons.append(
                "This report uses an older file layout that Studio cannot edit. Open it in "
                "Power BI Desktop and save it there once to upgrade it."
            )
        head_file = store.path(store.head())
        thin = not _has_data_model(head_file) if head_file.is_file() else False
        if thin:
            reasons.append(
                "This report reads its data from a shared model, so measures cannot be "
                "changed here."
            )
        readiness = self.readiness(wait=wait)
        checking = readiness is None
        ready = readiness is not None and readiness.can_edit_model
        if readiness is None:
            reasons.append("Studio is still checking Power BI Desktop.")
        elif not readiness.can_edit_model:
            reasons.extend(readiness.reasons or ["Power BI Desktop is not ready for this."])
        edit_model = edit_report and not thin and ready
        found: dict[str, Any] = {
            "edit_report": edit_report,
            "edit_model": edit_model,
            "reasons": [] if edit_model else reasons,
            "thin_report": thin,
            "model_support": self.model_support(),
        }
        if checking:
            found["checking"] = True
        return found

    def model_support(self) -> dict[str, Any]:
        """``model_support_payload`` for the Desktop found so far (never waits for the search and
        never starts Desktop: the receipt is one file read). Without Desktop, or while it is still
        being looked for, the version is None and nothing outside Microsoft's list is verified."""
        found = self._desktop_peek()
        version: str | None = None
        verified: frozenset[str] = frozenset()
        coverage = getattr(found.port, "model_probe_coverage", None) if found else None
        if callable(coverage):
            try:
                version, verified = coverage()
            except (DesktopSessionError, OSError, subprocess.SubprocessError, ValueError):
                LOGGER.warning("Could not read the model probe receipt", exc_info=True)
        return model_support_payload(version, verified)

    def model_step_support(
        self, workspace_id: str, operations: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """For each operation (dicts as chat plans them): ``{outside_supported}``, and when it is
        outside Microsoft's documented list, ``verified_on`` (the installed Desktop version when
        its model probe receipt covers it, else None: the editor will refuse it). Decided the way
        the editor's gate decides it (``report_edit_model.outside_support`` against the model
        Studio already knows, so a delete of a calculated column is documented). An operation
        that does not parse is reported as not outside; the edit layer refuses it anyway."""
        report = self.report(workspace_id)
        model = report_edits.ModelInfo.from_report(report.tables, report.measures)
        archive, _ = report_import.report_archive(report)
        facts = self._kept_facts(report.id, archive) if _has_data_model(archive) else None
        report_edit_model.attach_structure(model, facts or report.model)
        support = self.model_support()
        passed = set(support["verified_keys"])
        creates = (
            report_edit_model.CreateCalculatedColumn,
            report_edit_model.CreateCalculatedTable,
            report_edit_model.CreateFieldParameter,
            report_edit_model.CreateCalculationGroup,
        )
        created: list[Any] = []
        found: list[dict[str, Any]] = []
        for raw in operations:
            try:
                (parsed,) = report_edits.parse_operations([raw])
            except DocumentError:
                found.append({"outside_supported": False})
                continue
            if not isinstance(parsed, report_edit_model.MODEL_FAMILY_OPERATIONS):
                found.append({"outside_supported": False})
                continue
            # Objects an earlier step creates are calculated ones (documented to change).
            keys = report_edit_model.outside_support([*created, parsed], model)
            if isinstance(parsed, creates):
                created.append(parsed)
            if not keys:
                found.append({"outside_supported": False})
                continue
            verified = all(key in passed for key in keys)
            found.append(
                {
                    "outside_supported": True,
                    "verified_on": support["desktop_version"] if verified else None,
                }
            )
        return found

    def _file_hash(self, path: Path) -> str | None:
        """sha256 of a file, remembered by (path, size, mtime): the report's status is asked
        for often and the source file can be large."""
        try:
            info = path.stat()
            key = (str(path), info.st_size, info.st_mtime_ns)
        except OSError:
            return None
        with self._hash_lock:
            cached = self._hashes.get(key)
            if cached is not None:
                self._hashes.move_to_end(key)
                return cached
        try:
            digest = sha256_file(path)
        except OSError:
            return None
        with self._hash_lock:
            self._hashes[key] = digest
            while len(self._hashes) > 16:
                self._hashes.popitem(last=False)
        return digest

    def delivery(self, report: ImportedReport, store: RevisionStore) -> dict[str, Any]:
        """How the user's own file relates to the edited report (report_delivery's rules, with
        the source file's hash remembered instead of recomputed on every ask)."""
        source = Path(report.source_path)
        current = self._file_hash(source)
        head = store.head()
        delivered = store.delivered()
        known = {store.original_hash}
        if delivered is not None and _same_file(delivered.path, source):
            known.add(delivered.hash)
        state: report_delivery.DeliveryState
        if current is None:
            state = "source_changed"
        elif current == head.blob:
            state = "in_sync"
        else:
            state = "edited" if current in known else "source_changed"
        reference = ORIGINAL_ID
        if current is not None:
            newest = (row.id for row in store.history() if row.blob == current)
            reference = next(newest, ORIGINAL_ID)
        try:
            pending = len(store.later_changes(reference))
        except RevisionError:
            pending = 0
        return {
            "state": state,
            "source_path": str(source),
            "changes_not_in_source": pending,
        }

    def desktop_state(self, report: ImportedReport) -> dict[str, Any]:
        """Whether Desktop has the working copy open, and whether it is safe to change the
        report: ``{state: closed|open|open_unsaved|dialog|unknown, detail, working_copy}``.
        Only looks; never launches or touches Desktop."""
        cached = self._state_cache.get(report.id)
        if cached is not None and time.monotonic() - cached[0] < STATE_TTL:
            return dict(cached[1])
        found = self._look_at_desktop(report)
        if found["detail"] != _STILL_LOOKING:  # an answer that is about to change is not kept
            self._state_cache[report.id] = (time.monotonic(), found)
        return dict(found)

    def _look_at_desktop(self, report: ImportedReport) -> dict[str, Any]:
        store = self._store_of(report)
        desktop = self._desktop_peek()
        state, detail = "closed", ""
        if desktop is None:
            # The first search for Desktop is still running: "closed" would be a guess.
            state, detail = "unknown", _STILL_LOOKING
        # Not there: nothing to ask (and nothing is launched to find out).
        elif desktop.usable:
            port = desktop.port
            try:
                instance = port.find(store.working_copy)
                if instance is not None:
                    blocking = port.blocking_windows(instance)
                    if blocking:
                        state, detail = "dialog", blocking[0]
                    elif port.status(instance).get("hasUnsavedChanges"):
                        state = "open_unsaved"
                    else:
                        state = "open"
            except (DesktopSessionError, OSError, subprocess.SubprocessError, KeyError) as error:
                state, detail = "unknown", str(error)
        working: str
        try:
            working = self.editor.working_copy_state(report)
        except (StateConflictError, RevisionError, OSError):
            # The file is locked (Desktop has it open) or the history cannot be read.
            working = "locked"
        return {"state": state, "detail": detail, "working_copy": working}

    def report_payload(self, workspace_id: str) -> dict[str, Any]:
        """The imported report plus what editing needs: head, delivery, capabilities, Desktop."""
        report = self.report(workspace_id)
        store = self._store_of(report)
        head = store.head()
        payload = report.model_dump(mode="json")
        payload["head"] = {
            "id": head.id,
            "hash": head.blob,
            "edited": head.blob != store.original_hash,
            "desktop_version": head.desktop_version,
        }
        # The alt text a visual has, for the Format pane's editor (the renderer's formatting
        # tokens do not carry it; the visual's own JSON, ``definition``, does).
        for page in payload.get("pages") or []:
            for visual in page.get("visuals") or [] if isinstance(page, dict) else []:
                if isinstance(visual, dict):
                    visual["alt_text"] = _alt_text(visual.get("definition"))
        payload["delivery"] = self.delivery(report, store)
        payload["capabilities"] = self.capabilities(report)
        payload["desktop"] = self.desktop_state(report)
        # The snapshot's digest is Studio's own bookkeeping; what it says is in model.studio.
        payload.pop("snapshot_model", None)
        payload["model"] = self.model_payload(report)
        return payload

    # --- the batch registry -----------------------------------------------------------------

    def _lane(self, report_id: str) -> _Lane:
        with self._registry:
            lane = self._lanes.get(report_id)
            if lane is None:
                lane = self._lanes[report_id] = _Lane(self, report_id)
            return lane

    def submit(
        self,
        report: ImportedReport,
        kind: str,
        key: str,
        fingerprint: str,
        job: Job,
        *,
        summary: str = "",
    ) -> BatchHandle:
        """Queue a batch on the report's lane and return its handle.

        The same key with the same request gives the same batch back (its outcome once it has
        finished, the running batch while it runs); the same key with a different request is a
        conflict. A batch that failed may be asked for again under its key: the edit layer
        treats a failed attempt as never having happened.
        """
        with self._registry:
            prior = self._by_key.get((report.id, key))
            if prior is not None:
                if prior.fingerprint != fingerprint:
                    raise ImportedEditError(
                        "idempotency_conflict",
                        "This request key was already used for a different change. Start the "
                        "change again.",
                        409,
                    )
                if prior.state != "failed":
                    return prior
            handle = BatchHandle(
                batch_id=uuid.uuid4().hex,
                report_id=report.id,
                workspace_id=report.workspace_id,
                kind=kind,
                key=key,
                fingerprint=fingerprint,
                summary=summary,
            )
            self._batches[handle.id] = handle
            self._by_key[(report.id, key)] = handle
            self._trim_registry()
            lane = self._lanes.get(report.id)
            if lane is None:
                lane = self._lanes[report.id] = _Lane(self, report.id)
        self._mirror(handle)
        lane.submit(handle, job)
        return handle

    def _trim_registry(self, *, completing: BatchHandle | None = None) -> None:
        while len(self._batches) > MAX_BATCHES_IN_MEMORY:
            for old_id, old in self._batches.items():
                if old._can_forget(completing=old is completing):  # noqa: SLF001
                    del self._batches[old_id]
                    if self._by_key.get((old.report_id, old.key)) is old:
                        del self._by_key[(old.report_id, old.key)]
                    break
            else:
                return

    def wait(self, handle: BatchHandle, timeout: float | None = None) -> bool:
        """Block until the batch has finished or ``timeout`` passes; True when finished."""
        return handle.wait(timeout)

    def outcome(self, handle: BatchHandle, wait: float | None = None) -> BatchOutcome:
        """Wait for the batch up to ``wait`` seconds (default ``wait_seconds``): 200 with its
        result, its error raised, or 202 with the batch id while it still runs."""
        handle.wait(self.wait_seconds if wait is None else wait)
        if handle.state == "done" and handle.result is not None:
            return BatchOutcome(200, handle.result)
        if handle.state == "failed" and handle.error is not None:
            raise handle.error
        body = {"batch_id": handle.id, "state": handle.state, "kind": handle.kind}
        return BatchOutcome(202, body)

    def _execute(self, handle: BatchHandle, job: Job) -> None:
        handle._start()  # noqa: SLF001
        self._mirror(handle)
        try:
            result = job(handle.progress)
            if handle.kind in RECEIPT_KINDS:
                # The id to poll with: the edit layer's own batch id names its journal entry.
                result = {**result, "journal_batch": result.get("batch_id"), "batch_id": handle.id}
            handle._complete(result)  # noqa: SLF001
        except Exception as error:  # noqa: BLE001 - every failure becomes the batch's outcome
            classified = classify(error)
            if classified.code == "edit_failed":
                LOGGER.exception("Edit batch %s (%s) failed unexpectedly", handle.id, handle.kind)
            handle._fail(classified)  # noqa: SLF001
        except BaseException:
            handle._fail(  # noqa: SLF001
                ImportedEditError("edit_failed", "Studio stopped before this change finished.", 500)
            )
            self._after(handle)
            raise
        self._after(handle)

    def _after(self, handle: BatchHandle) -> None:
        self._state_cache.pop(handle.report_id, None)
        # A native commit must keep its receipt even when Studio's bookkeeping fails. The
        # public state stays pending throughout this work, so wait/poll/chat see completion
        # only after the event and mirror have been attempted, without holding a handle lock
        # across I/O. Listeners run after publication and may safely reenter wait/status.
        try:
            for bookkeeping, is_mirror in ((self._record, False), (self._mirror, True)):
                try:
                    saved = bookkeeping(handle)
                except Exception:  # noqa: BLE001 - bookkeeping cannot undo a committed edit
                    LOGGER.warning(
                        "Could not finish bookkeeping for batch %s", handle.id, exc_info=True
                    )
                    saved = False
                if saved is not True:
                    handle._warn_completion()  # noqa: SLF001
                elif is_mirror:
                    handle._mark_terminal_mirrored()  # noqa: SLF001
        except BaseException:
            handle._warn_completion()  # noqa: SLF001
            raise
        finally:
            # A queued burst may exceed the cap while every batch is still active. Trim
            # before waking waiters, but only forget outcomes saved in the terminal mirror.
            # Failed bookkeeping keeps its result and warning available to live polling.
            with self._registry:
                self._trim_registry(completing=handle)
                completed_callbacks = handle._publish()  # noqa: SLF001
        callbacks = [*self.on_batch_finished, *completed_callbacks]
        for callback in callbacks:
            try:
                callback(handle)
            except Exception:  # noqa: BLE001 - a listener must not disturb the lane
                LOGGER.exception("A batch listener failed")

    def _record(self, handle: BatchHandle) -> bool:
        """The outcome in the workspace's event log, so a reload can show it (P8)."""
        if handle.kind == "open":
            return True
        try:
            state, result, error = handle._completion()  # noqa: SLF001
            if state == "done" and result is not None:
                head = result.get("head") or {}
                self.store.event(
                    handle.workspace_id,
                    "report.edit_applied",
                    {
                        "batch_id": handle.id,
                        "kind": handle.kind,
                        "revision": (result.get("revision") or {}).get("id"),
                        "head": head.get("id"),
                        "summaries": result.get("summaries", []),
                        "outcome": result.get("outcome"),
                    },
                )
            elif error is not None:
                self.store.event(
                    handle.workspace_id,
                    "report.edit_failed",
                    {
                        "batch_id": handle.id,
                        "kind": handle.kind,
                        "code": error.code,
                        "message": error.message,
                    },
                )
        except Exception:  # noqa: BLE001 - the log is a convenience, never a reason to fail
            LOGGER.warning("Could not record the outcome of batch %s", handle.id, exc_info=True)
            return False
        return True

    # --- the mirror of the registry in the report's folder ----------------------------------

    def _mirror_path(self, report_id: str) -> Path:
        return self.state_root / "reports" / report_id / MIRROR_NAME

    def _mirror(self, handle: BatchHandle) -> bool:
        """Append the handle's current state to the report's batch log.

        Under one lock, with the line built inside it: the request thread (queued) and the
        lane (running, done) both write, and a line written from an older reading of the state
        after a newer one would leave the log saying a finished batch is still queued.
        """
        path = self._mirror_path(handle.report_id)
        with self._mirror_lock:
            state, result, error = handle._completion()  # noqa: SLF001
            line: dict[str, Any] = {
                "batch": handle.id,
                "key": handle.key,
                "kind": handle.kind,
                "state": state,
                "ts": _now(),
                "workspace": handle.workspace_id,
                "summary": handle.summary,
            }
            if result is not None:
                line["receipt"] = result
            if error is not None:
                line["error"] = error.payload()
            if handle._completion_warning is not None:  # noqa: SLF001
                line["completion_warning"] = handle._completion_warning  # noqa: SLF001
            data = json.dumps(line, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("ab") as out:
                    out.write(data)
                if path.stat().st_size > MIRROR_MAX_BYTES:
                    self._trim_mirror(path)
            except OSError:
                LOGGER.warning("Could not write the batch log of report %s", handle.report_id)
                return False
        return True

    @staticmethod
    def _trim_mirror(path: Path) -> None:
        lines = path.read_bytes().splitlines()
        seen: list[str] = []
        for raw in reversed(lines):
            with contextlib.suppress(ValueError):
                batch = json.loads(raw).get("batch")
                if isinstance(batch, str) and batch not in seen:
                    seen.append(batch)
        keep = set(seen[:MIRROR_KEEP])
        kept: list[bytes] = []
        for raw in lines:
            with contextlib.suppress(ValueError):
                if json.loads(raw).get("batch") in keep:
                    kept.append(raw)
        path.write_bytes(b"\n".join(kept) + b"\n")

    def _mirror_record(self, report_id: str, batch_id: str) -> dict[str, Any] | None:
        """The last line the mirror has for a batch (None if it never heard of it)."""
        try:
            with self._mirror_lock:
                lines = self._mirror_path(report_id).read_bytes().splitlines()
        except OSError:
            return None
        found: dict[str, Any] | None = None
        for raw in lines:
            try:
                value = json.loads(raw)
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("batch") == batch_id:
                found = value
        return found

    def status(self, workspace_id: str, batch_id: str) -> dict[str, Any]:
        """The poll route's body. After a restart a batch that was still queued or running is
        answered from the edit journal: done when recovery found its revision recorded,
        otherwise failed (the report is as it was)."""
        report = self._load_report(workspace_id)
        with self._registry:
            handle = self._batches.get(batch_id)
        if handle is not None:
            if handle.workspace_id != workspace_id:
                raise KeyError(f"Unknown batch: {batch_id}")
            return handle.status()
        record = self._mirror_record(report.id, batch_id)
        if record is None:
            raise KeyError(f"Unknown batch: {batch_id}")
        body: dict[str, Any] = {
            "batch_id": batch_id,
            "state": record.get("state"),
            "kind": record.get("kind"),
        }
        if record.get("state") == "done":
            body["receipt"] = record.get("receipt")
        elif record.get("state") == "failed":
            body["error"] = record.get("error")
        else:
            return self._after_restart(report, record, body)
        if record.get("completion_warning"):
            body["completion_warning"] = record["completion_warning"]
        return body

    def _after_restart(
        self, report: ImportedReport, record: dict[str, Any], body: dict[str, Any]
    ) -> dict[str, Any]:
        store = self._store_of(report)
        prior = EditJournal(store.journal_path).find_key(str(record.get("key", "")))
        if prior is not None and prior.state == "done" and prior.receipt is not None:
            body["state"] = "done"
            body["receipt"] = self._receipt_from_journal(store, prior.receipt)
            return body
        body["state"] = "failed"
        body["error"] = ImportedEditError(
            "interrupted",
            "Studio was closed before this change finished. Check the history to see whether "
            "it was kept, then ask again if it was not.",
            409,
        ).payload()
        return body

    @staticmethod
    def _receipt_from_journal(store: RevisionStore, recorded: dict[str, Any]) -> dict[str, Any]:
        head = store.head()
        try:
            row = store.revision(str(recorded.get("revision", head.id)))
        except RevisionError:
            row = head
        adopted: Revision | None = None
        adopted_id = recorded.get("adopted")
        if isinstance(adopted_id, str) and adopted_id:
            # The Desktop work this batch kept as its own step: a poll after a restart still
            # tells the user it was kept.
            try:
                adopted = store.revision(adopted_id)
            except RevisionError:
                adopted = None
        return {
            "revision": _revision_row(row),
            "head": {"id": head.id, "hash": head.blob},
            "summaries": [str(item) for item in recorded.get("summaries", [])],
            "warnings": [str(item) for item in recorded.get("warnings", [])],
            "adopted": _revision_row(adopted) if adopted is not None else None,
            # What the edit layer reported is recorded with the batch (a line from before it
            # was has none of it).
            "protected": _plain_dicts(recorded.get("protected")),
            "replaced": [str(item) for item in _as_list(recorded.get("replaced"))],
            "visible_in": _plain_dicts(recorded.get("visible_in")),
            "pending_model_operations": _plain_dicts(recorded.get("pending_model_operations")),
            "outcome": "partial" if recorded.get("status") == "partial" else "updated",
            "desktop": str(recorded.get("desktop", "closed")),
            "replayed": True,
        }

    def cancel(self, workspace_id: str, batch_id: str) -> dict[str, Any]:
        """Cancel a batch that has not started. Once it is running the edit layer is already
        working with the report (and maybe Desktop), so it is left to finish."""
        with self._registry:
            handle = self._batches.get(batch_id)
        if handle is None or handle.workspace_id != workspace_id:
            raise KeyError(f"Unknown batch: {batch_id}")
        cancelled = self._lane(handle.report_id).cancel(handle)
        if cancelled:
            handle._fail(  # noqa: SLF001
                ImportedEditError("cancelled", "This change was cancelled before it started.", 409)
            )
            self._after(handle)
        return {**handle.status(), "cancelled": cancelled}

    # --- receipts ---------------------------------------------------------------------------

    def _visible_in(
        self, report: ImportedReport, ops: Sequence[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Where each operation shows up: ``studio`` (the canvas draws it), ``powerbi_only``
        (saved, but only Power BI draws it: a property Studio has no token for, or a visual
        type it only frames)."""
        types = {v.id: v.visual_type for page in report.pages for v in page.visuals}
        found: list[dict[str, Any]] = []
        for op in ops:
            name = str(op.get("op", ""))
            visual_type = types.get(str(op.get("visual", "")))
            where = "studio"
            if name not in _ALWAYS_DRAWN and visual_type is not None:
                drawn = family_of(visual_type) != "unsupported"
                where = "studio" if drawn else "powerbi_only"
            found.append({"op": name, "where": where})
        return found

    def receipt_payload(
        self,
        receipt: EditReceipt,
        ops: Sequence[dict[str, Any]],
        store: RevisionStore,
        outcome: EditOutcome | None = None,
    ) -> dict[str, Any]:
        """``EditReceipt`` of the API contract.

        The editor's receipt carries what the edit layer reported (what it left alone, what a
        whole-visual write replaced, where each change shows, the model changes it only
        suggests), also when the receipt is a replay of a recorded batch. ``outcome`` is an
        ``EditOutcome`` a caller worked out itself, used only for what the receipt lacks. A
        receipt with no ``visible_in`` (an undo, a restore) gets the per-operation reading of
        ``_visible_in``."""
        head = store.head()
        protected = _plain_items(receipt.protected) or _plain_items(
            outcome.protected if outcome is not None else None
        )
        visible = _plain_items(receipt.visible_in) or _plain_items(
            outcome.visible_in if outcome is not None else None
        )
        replaced = list(receipt.replaced) or list(outcome.replaced if outcome is not None else [])
        pending = receipt.pending_model_operations or (
            outcome.pending_model_operations if outcome is not None else []
        )
        adopted = receipt.adopted
        return {
            "revision": _revision_row(receipt.revision),
            "head": {"id": head.id, "hash": head.blob},
            "summaries": list(receipt.summaries),
            "warnings": list(receipt.warnings),
            "adopted": (_revision_row(adopted) if adopted is not None else None),
            "protected": protected,
            "replaced": replaced,
            "visible_in": visible or self._visible_in(receipt.report, ops),
            "pending_model_operations": [_plain_operation(op) for op in pending],
            "outcome": receipt.status,
            "desktop": receipt.desktop,
            "replayed": receipt.replayed,
            "batch_id": receipt.batch_id,
        }

    # --- rederivation -----------------------------------------------------------------------

    def _model_differs(self, before: Path, after: Path) -> bool:
        try:
            return report_import.model_key(before) != report_import.model_key(after)
        except (OSError, zipfile.BadZipFile):
            return True

    def _rederive(
        self, report: ImportedReport, path: Path, blob: str, model_changed: bool
    ) -> ImportedReport:
        """Move Studio's view of the report to the revision in ``path`` and store it (P19).

        Pages come from the new file. Measures are read again only when the model may have
        changed *and* its bytes are not the ones the view was built from: report-layer
        revisions copy the DataModel unchanged, so an undo of a recolor costs no model read.
        When the model did change, its column types and calculated DAX are read again with the
        measures (``model_facts`` feeds them to every edit, keyed by the DataModel's CRC and
        size, so an edit that leaves the model alone never reads them); a failure there only
        costs the facts, never the refresh. Runs on the report's lane, under the edit lock.

        The model's facts are read again too (``read_model_facts_bounded``, or the copy kept for
        that model), and ``tables``, ``relationships`` and ``model`` then describe the new
        model: a renamed or deleted column or table disappears from every reader, and the local
        engine (keyed on them) never keeps an old join. A failed read leaves ``model`` unknown
        (None) rather than describing the old model.
        """
        refreshed = report_import.refresh_from_revision(report, path, blob)
        old_archive, _ = report_import.report_archive(report)
        if model_changed and _has_data_model(path) and self._model_differs(old_archive, path):
            refreshed = refreshed.model_copy(update={"measures": self._read_measures(path)})
            self._refresh_types(report.id, path)
            refreshed = self._with_facts(refreshed, path)
        return self._persist(report, refreshed)

    # --- model facts ------------------------------------------------------------------------

    def _facts_root(self, report_id: str) -> Path:
        return self.state_root / "reports" / report_id

    def _facts_for(self, report_id: str, path: Path) -> dict[str, Any]:
        """The full facts of the model in ``path``: from memory, from the copy kept beside the
        history (``report_import.store_facts``, keyed by the DataModel's CRC and size), or read
        now in a child process and kept. A model that failed to read is not tried again for
        ``FACTS_BACKOFF`` seconds. Raises EditRejected when they cannot be had."""
        key = report_import.model_key(path)
        memo = (report_id, key)
        with self._facts_lock:
            found = self._facts.get(memo)
            if found is not None:
                self._facts.move_to_end(memo)
                return found
            failed = self._facts_failed.get(memo)
        found = report_import.cached_facts(self._facts_root(report_id), key)
        if found is None:
            if failed is not None and time.monotonic() - failed < FACTS_BACKOFF:
                raise EditRejected("Studio could not read the report's model.")
            try:
                found = self._read_facts(path)
            except (EditRejected, OSError, ValueError, zipfile.BadZipFile):
                with self._facts_lock:
                    self._facts_failed[memo] = time.monotonic()
                raise
            report_import.store_facts(self._facts_root(report_id), key, found)
        with self._facts_lock:
            self._facts_failed.pop(memo, None)
            self._facts[memo] = found
            while len(self._facts) > FACTS_IN_MEMORY:
                self._facts.popitem(last=False)
        return found

    def _with_facts(self, report: ImportedReport, path: Path) -> ImportedReport:
        """``report`` describing the model in ``path`` (``report_import.with_model_facts``),
        with the snapshot's digest filled for a report imported before digests were kept."""
        try:
            facts = self._facts_for(report.id, path)
        except (EditRejected, OSError, ValueError, zipfile.BadZipFile) as error:
            LOGGER.warning("Could not read the model facts of report %s: %s", report.id, error)
            return report_import.with_model_facts(report, None)
        return report_import.with_model_facts(
            report, facts, snapshot_facts=self._snapshot_source(report)
        )

    def _snapshot_source(self, report: ImportedReport) -> tuple[str, dict[str, Any]] | None:
        """(model key, facts) of the model the snapshot's rows came from, for a report that has
        rows but no digest of them yet: its import, the original file (re-read data always
        keeps a digest). None when the report needs none or they cannot be read."""
        if report.snapshot_model is not None or not report.snapshot_id:
            return None
        original = Path(report.archived_path)
        try:
            return report_import.model_key(original), self._facts_for(report.id, original)
        except (EditRejected, OSError, ValueError, zipfile.BadZipFile):
            return None

    def read_model_facts(self, report: ImportedReport, path: Path) -> dict[str, Any]:
        """The full facts of the model in ``path`` (a file of ``report``: a revision, the working
        copy Desktop saved), through the view's own cache: memory, the copy kept beside the
        history, else a bounded read that is then kept. The schema is
        ``pbix_extract.read_model_facts``'s. Raises EditRejected in plain words."""
        return self._facts_for(report.id, path)

    def head_model_facts(self, report: ImportedReport) -> dict[str, Any] | None:
        """The full facts of the head's model (``pbix_extract.read_model_facts``, unbounded:
        Power Query and measure DAX in full), read now if Studio has not kept them; None for a
        report without a model or when they cannot be read. For the model editor and the
        executor of model changes; the report payload carries the bounded ``model``."""
        archive, _ = report_import.report_archive(report)
        if not _has_data_model(archive):
            return None
        try:
            return self._facts_for(report.id, archive)
        except (EditRejected, OSError, ValueError, zipfile.BadZipFile):
            return None

    def full_model_facts(self, workspace_id: str, table: str | None = None) -> dict[str, Any]:
        """The head's full facts for the model workbench: Power Query and measure DAX in full,
        never cut to the payload's 4,000 characters (``truncated`` at the top still lists any
        section the read itself had to cut, and such an item says ``"truncated": true``: the
        workbench must not edit it). With ``table``, only what belongs to that table: its entry,
        columns, measures, partitions, hierarchies, variations, its calculation group or
        parameter, the relationships and role filters that touch it. Never chat context: no
        prompt builder reads it."""
        report = self.report(workspace_id)
        archive, _ = report_import.report_archive(report)
        if not _has_data_model(archive):
            raise ImportedEditError(
                "no_model",
                "This report has no embedded local model. Confirm its connection in Power BI.",
                404,
            )
        facts = self.head_model_facts(report)
        if facts is None:
            raise ImportedEditError(
                "model_unreadable",
                "Studio could not read this report's model. Open it in Power BI Desktop.",
                503,
            )
        status = "current" if model_facts_known(facts) else "unknown"
        found = facts_payload(
            facts, report.snapshot_model, status=status, rows=bool(report.snapshot_id), reread=True
        )
        if table is None:
            return found
        wanted = table.casefold()
        entry = next(
            (
                t
                for t in facts.get("tables") or []
                if isinstance(t, dict) and str(t.get("name")).casefold() == wanted
            ),
            None,
        )
        if entry is None:
            raise ImportedEditError("unknown_table", f"The model has no table '{table}'.", 404)

        def own(section: str, *keys: str) -> list[Any]:
            return [
                item
                for item in facts.get(section) or []
                if isinstance(item, dict)
                and any(str(item.get(key)).casefold() == wanted for key in keys)
            ]

        roles = []
        for role in facts.get("roles") or []:
            filters = [
                f
                for f in (role.get("tables") or [] if isinstance(role, dict) else [])
                if isinstance(f, dict) and str(f.get("table")).casefold() == wanted
            ]
            if filters:
                roles.append({**role, "tables": filters})
        cut = [s for s in facts.get("truncated") or [] if isinstance(s, str)]
        return {
            "version": facts.get("version"),
            "status": status,
            "storage": found["storage"],
            **({"truncated": cut} if cut else {}),
            "model": facts.get("model"),
            "table": entry,
            "columns": own("columns", "table"),
            "measures": own("measures", "table"),
            "partitions": own("partitions", "table"),
            "hierarchies": own("hierarchies", "table"),
            "variations": own("variations", "table", "dateTable"),
            "relationships": own("relationships", "fromTable", "toTable"),
            "calculationGroups": own("calculationGroups", "table"),
            "fieldParameters": own("fieldParameters", "table"),
            "whatIfParameters": own("whatIfParameters", "table"),
            "roles": roles,
        }

    def validate_model_query(
        self, workspace_id: str, request: ModelQueryCheckRequest
    ) -> dict[str, Any]:
        """What an edit with this Power Query formula would be told, without queueing it or
        touching Desktop: ``{valid, checked, message, problems, warnings, table}``.

        ``message`` is the refusal the edit would get (None when it would go ahead), in the
        model family's own words; ``problems`` everything Microsoft's Power Query parser found
        (``report_edit_model.check_m``); ``checked`` whether that parser ran; ``warnings`` what
        the receipt would add. With ``table`` the table's own checks come first (a calculated
        table has no query), as in ``set_table_query``, and ``table`` is its own spelling. The
        formula goes to the local parser only: it is never logged, stored or sent."""
        report = self.report(workspace_id)
        archive, _ = report_import.report_archive(report)
        if not _has_data_model(archive):
            raise ImportedEditError(
                "no_model", "This report reads its data from a shared model.", 404
            )
        table: str | None = None
        table_warnings: list[str] = []
        if request.table is not None:
            model = report_edits.ModelInfo.from_report(report.tables, report.measures)
            # Facts Studio already has (never a new read of the model for a check): without
            # them the query's own checks are left to Power BI Desktop, as in an edit.
            facts = self._kept_facts(report.id, archive) or report.model
            report_edit_model.attach_structure(model, facts)
            try:
                table, table_warnings = report_edit_model.table_query_target(model, request.table)
            except DocumentError as error:
                return {
                    "valid": False,
                    "checked": False,
                    "message": str(error),
                    "problems": [],
                    "warnings": [],
                    "table": None,
                }
        found = report_edit_model.check_m(request.expression)
        valid = found.refusal is None
        return {
            "valid": valid,
            "checked": found.checked,
            "message": found.refusal,
            "problems": list(found.problems),
            "warnings": [*found.warnings, *table_warnings] if valid else [],
            "table": table,
        }

    def submit_reread(
        self,
        workspace_id: str,
        *,
        idempotency_key: str,
        reread: Callable[[ImportedReport], ImportedReport],
    ) -> BatchHandle:
        """Read the head's data again (after Power BI Desktop refreshed it) on the report's
        lane, under the edit lock, so no edit moves the head meanwhile. ``reread`` decodes the
        head into a new snapshot, makes it the workspace's and returns the report updated for it
        (``report_import.reread_data`` plus the service's snapshot bookkeeping); this stores
        the report and answers ``{snapshot_id, model}`` (the payload's ``model``)."""
        report = self.report(workspace_id)
        if not _has_data_model(report_import.report_archive(report)[0]):
            raise ImportedEditError(
                "no_model", "This report reads its data from a shared model.", 404
            )
        fingerprint = _fingerprint("reread_data", report.id)

        def job(progress: Progress) -> dict[str, Any]:
            progress("Reading the data again")
            store = self._store_of(report)
            with store.lock(timeout=30.0):
                fresh = self.report(workspace_id, in_lane=True)
                stored = self._persist(fresh, reread(fresh))
            return {"snapshot_id": stored.snapshot_id, "model": self.model_payload(stored)}

        return self.submit(
            report, "reread_data", idempotency_key, fingerprint, job, summary="Read the data again"
        )

    def model_payload(self, report: ImportedReport) -> dict[str, Any]:
        """The report payload's ``model`` (``model_facts.payload``). A report whose facts are
        unknown (imported before they were read) has them read in the background, once per
        head: the payload says ``"reading"`` meanwhile, and ``"unknown"`` when that failed."""
        archive, digest = report_import.report_archive(report)
        if not _has_data_model(archive):
            return facts_payload(None, None, status="none")
        if report.model is not None:
            if report.snapshot_model is None and report.snapshot_id:
                # The rows' own model is still unknown (its read failed): try again, so their
                # calculated values need not be distrusted for good.
                self._fill_in_background(report, archive, digest)
            return facts_payload(
                report.model,
                report.snapshot_model,
                status="current",
                rows=bool(report.snapshot_id),
                reread=True,
            )
        started = self._fill_in_background(report, archive, digest)
        return facts_payload(None, None, status="reading" if started else "unknown")

    def _fill_in_background(self, report: ImportedReport, archive: Path, digest: str) -> bool:
        """Read what the view lacks in a background thread: the head's facts, or, with those
        known, the facts of the model the rows came from (the import's)."""
        target = archive if report.model is None else Path(report.archived_path)
        with self._facts_lock:
            if report.id in self._filling:
                return True
            try:
                failed = self._facts_failed.get((report.id, report_import.model_key(target)))
            except (OSError, zipfile.BadZipFile):
                return False
            if failed is not None and time.monotonic() - failed < FACTS_BACKOFF:
                return False
            if not readable_model(target):
                return False
            self._filling.add(report.id)
        threading.Thread(
            target=self._fill,
            args=(report.workspace_id, report.id, digest),
            name="model-facts",
            daemon=True,
        ).start()
        return True

    def _fill(self, workspace_id: str, report_id: str, digest: str) -> None:
        """Read the facts a view lacks (the head's, or the rows' model's) and store them with
        the view, under the edit lock (like ``_heal``), unless the head moved or a batch filled
        them first."""

        def lacking(view: ImportedReport) -> bool:
            return view.model is None or (view.snapshot_model is None and bool(view.snapshot_id))

        try:
            report = self._load_report(workspace_id)
            archive, current = report_import.report_archive(report)
            if current != digest or not lacking(report):
                return
            try:
                self._facts_for(report_id, archive)  # the slow part, outside the lock
            except (EditRejected, OSError, ValueError, zipfile.BadZipFile):
                # A failed current read must not remain indistinguishable from a legacy
                # import after restart. Never replace facts that another worker filled, or
                # mark a different head whose read this worker did not attempt.
                with self._store_of(report).lock(timeout=2.0):
                    fresh = self._load_report(workspace_id)
                    _, now = report_import.report_archive(fresh)
                    if now == digest and fresh.model is None and fresh.model_read_version == 0:
                        self._persist(fresh, report_import.with_model_facts(fresh, None))
                raise
            source = self._snapshot_source(report)
            if report.model is not None and source is None:
                return  # still no digest: nothing new to store
            with self._store_of(report).lock(timeout=2.0):
                fresh = self._load_report(workspace_id)
                _, now = report_import.report_archive(fresh)
                if now != digest or not lacking(fresh):
                    return
                facts = self._facts_for(report_id, archive)
                self._persist(
                    fresh, report_import.with_model_facts(fresh, facts, snapshot_facts=source)
                )
        except (
            EditRejected,
            RevisionError,
            StateConflictError,
            OSError,
            ValueError,
            KeyError,
            zipfile.BadZipFile,
        ):
            LOGGER.info("The model facts of report %s were not read this time", report_id)
        finally:
            with self._facts_lock:
                self._filling.discard(report_id)

    def _persist(self, before: ImportedReport, refreshed: ImportedReport) -> ImportedReport:
        """Store the refreshed report: optimistic on the stored base hash (which is the head's
        hash for an edited report), with a key no other write shares."""
        last: StateConflictError | None = None
        for _ in range(3):
            current = self.store.get("imported_report", before.id, ImportedReport)
            expected = current.base_hash if current is not None else EMPTY_HASH
            try:
                return self.store.put(
                    "imported_report",
                    refreshed,
                    workspace_id=refreshed.workspace_id,
                    expected_base_hash=expected,
                    idempotency_key=(
                        f"imported-revision-{refreshed.id}-{refreshed.base_hash[:16]}-"
                        f"{uuid.uuid4().hex[:12]}"
                    ),
                    mutation_payload={"report": refreshed.id, "head": refreshed.base_hash},
                )
            except StateConflictError as error:
                last = error
        assert last is not None
        raise last

    # --- model types ------------------------------------------------------------------------

    def _types_path(self, report_id: str) -> Path:
        return self.state_root / "reports" / report_id / TYPES_NAME

    def _read_and_keep_types(self, report_id: str, path: Path) -> ModelTypes:
        key = report_import.model_key(path)
        types = ModelTypes.from_extract(key, self._read_types(path))
        target = self._types_path(report_id)
        with contextlib.suppress(OSError):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(types.to_json()), encoding="utf-8")
        return types

    def _refresh_types(self, report_id: str, path: Path) -> None:
        """Read the types of the model in ``path`` now, as part of moving the view to a head
        whose DataModel changed. Best effort: without them edits go ahead on the tables and
        measures alone and say so."""
        with contextlib.suppress(OSError, zipfile.BadZipFile):
            cached = self._types.get(report_id)
            if cached is not None and cached.model_key == report_import.model_key(path):
                return  # an edit on this model (an adopted Desktop save) already read them
        self._types.pop(report_id, None)
        try:
            found = self._read_and_keep_types(report_id, path)
        except (EditRejected, OSError, ValueError, zipfile.BadZipFile) as error:
            LOGGER.warning("Could not read the model types of report %s: %s", report_id, error)
            with contextlib.suppress(OSError, zipfile.BadZipFile):
                self._types_failed[report_id] = (
                    report_import.model_key(path),
                    time.monotonic(),
                )
            return
        self._types[report_id] = found
        self._types_failed.pop(report_id, None)

    def model_types(self, report: ImportedReport) -> ModelTypes | None:
        """Column and measure types (and the DAX of calculated columns and tables) of the
        head's model, None for a report without a model or when they cannot be read.

        Read once per model: the answer is kept in memory and in ``model-types.json`` beside the
        history, keyed by the DataModel's CRC and size, so an edit that leaves the model alone
        starts no child process. A model that failed to read is not tried again for
        ``TYPES_BACKOFF`` seconds."""
        archive, _ = report_import.report_archive(report)
        try:
            if not _has_data_model(archive):
                return None
            key = report_import.model_key(archive)
        except (OSError, zipfile.BadZipFile):
            return None
        cached = self._types.get(report.id)
        if cached is not None and cached.model_key == key:
            return cached
        try:
            stored = json.loads(self._types_path(report.id).read_text(encoding="utf-8"))
            if (
                isinstance(stored, dict)
                and stored.get("model_key") == key
                and stored.get("v") == TYPES_VERSION
            ):
                found = ModelTypes.from_extract(key, stored)
                self._types[report.id] = found
                return found
        except (OSError, ValueError):
            pass
        failed = self._types_failed.get(report.id)
        if failed is not None and failed[0] == key and time.monotonic() - failed[1] < TYPES_BACKOFF:
            return None
        try:
            found = self._read_and_keep_types(report.id, archive)
        except (EditRejected, OSError, ValueError):
            self._types_failed[report.id] = (key, time.monotonic())
            return None
        self._types[report.id] = found
        self._types_failed.pop(report.id, None)
        return found

    def model_facts(self, report: ImportedReport) -> ModelFacts | None:
        """What the edit layer is given besides the report's tables and measures: the types
        payload (None when the metadata gave none) and the calculated DAX (None when unknown);
        None altogether for a report with no model or when it could not be read. This is the
        ``model_facts`` feed of ``ReportEditor``."""
        found = self.model_types(report)
        if found is None:
            return None
        return (found.payload() if found.known else None, found.calculated)

    # --- parsing and gating a request -------------------------------------------------------

    @staticmethod
    def parse_operations(
        raw: Sequence[dict[str, Any]], *, confirm_replace: bool = False
    ) -> list[Any]:
        """The typed operations for request dicts, or ``ImportedEditError`` in plain words.

        Uses report_edits.parse_operations when the edit layer has one, else validates with
        EditBatch. An operation name Studio does not know is ``edit_unsupported``; a value that
        does not fit is ``edit_rejected`` naming the step. ``confirm_replace`` is the request-level
        "yes, replace it": it sets ``replace_conditional`` and ``replace_dynamic`` (replacing
        rule-driven formatting or a calculated title asks first) on every operation that takes
        them, and ``confirm_replace`` itself on one that has that field. It does not set
        ``cleanup_references`` of ``delete_page``: switching off a page's buttons and links is a
        different question that the operation's own field answers.
        """
        parser = getattr(report_edits, "parse_operations", None)

        def parse(items: Sequence[dict[str, Any]]) -> list[Any]:
            if callable(parser):
                return list(parser(list(items)))
            batch = report_edits.EditBatch.model_validate({"operations": list(items)})
            return list(batch.operations)

        try:
            found = parse(raw)
            if confirm_replace and len(found) == len(raw):
                marked = [
                    {
                        **dict(item),
                        **{
                            name: True
                            for name in _CONFIRM_REPLACE_FLAGS
                            if name in getattr(type(op), "model_fields", {})
                        },
                    }
                    for item, op in zip(raw, found, strict=True)
                ]
                found = parse(marked)
        except ValidationError as error:
            raise _validation_error(error, raw) from error
        except ValueError as error:
            if _asks_the_user(error) is not None:
                raise classify(error) from error
            raise _parse_failure(error, raw) from error
        return found

    @staticmethod
    def merge_answers(
        operations: Sequence[dict[str, Any]], answers: Sequence[Any] | None
    ) -> list[dict[str, Any]]:
        """The operations with the user's answers written in: each answer is ``{step, fill}``
        (a mapping or an object with those attributes) and ``fill`` (the option values a
        ``needs_choice`` offered, verbatim: ``{"overrides": "clear"}``, ``{"fields": [...]}``,
        ``{"on": "overlay"}``) is set over the fields of the operation at ``step``, counted from 1.

        Writing a value over a field is repeatable, so a client that already merged the same
        fill into its operations (the front end does for the choices it knows) changes nothing
        by also sending it. An answer cannot change which operation a step is, and a step that
        does not exist is refused in plain words: a wrong answer must not land on another step.
        """
        merged = [dict(op) for op in operations]
        for answer in answers or ():
            step = _answer_field(answer, "step")
            fill = _answer_field(answer, "fill")
            if isinstance(step, bool) or not isinstance(step, int) or not 1 <= step <= len(merged):
                raise ImportedEditError(
                    "edit_rejected",
                    f"The answer is for step {step}, but this change has {len(merged)} "
                    f"step{'' if len(merged) == 1 else 's'}. Ask again to get the question.",
                    422,
                    step=None,
                )
            if (
                not isinstance(fill, Mapping)
                or not fill
                or not all(isinstance(key, str) for key in fill)
            ):
                raise ImportedEditError(
                    "edit_rejected",
                    f"The answer for step {step} says nothing to change.",
                    422,
                    step=step,
                )
            if "op" in fill:
                raise ImportedEditError(
                    "edit_rejected",
                    f"An answer cannot change what step {step} does.",
                    422,
                    step=step,
                )
            merged[step - 1].update(fill)
        return merged

    def _require_editable(self, report: ImportedReport) -> None:
        if report.format != "PBIR":
            raise ImportedEditError(
                "edit_unsupported",
                "This report uses an older file layout that Studio cannot edit.",
                422,
                capability="legacy_layout",
                reason="The report is stored in Power BI's older layout.",
                desktop_steps=[
                    "Open the report in Power BI Desktop.",
                    "Choose File > Save, so Power BI Desktop upgrades it.",
                    "Import the saved file into Studio again.",
                ],
            )

    def _gate_model_operations(
        self, report: ImportedReport, ops: Sequence[Any], confirmed: bool
    ) -> None:
        model = [op for op in ops if isinstance(op, _MODEL_OPERATIONS)]
        if not model:
            return
        caps = self.capabilities(report, wait=True)
        if not caps["edit_model"]:
            raise ImportedEditError(
                "desktop_unavailable",
                " ".join(caps["reasons"]) or _NEEDS_DESKTOP,
                503,
                reasons=caps["reasons"],
            )
        family = tuple(getattr(report_edits, "MODEL_FAMILY_OPERATIONS", ()))
        readiness = self.readiness(wait=True)
        if (
            family
            and any(isinstance(op, family) for op in model)
            and readiness is not None
            and not readiness.can_edit_model_objects
        ):
            # Columns, tables, relationships and the rest go through Studio's TOM runner, which
            # measures do not need: say what stops it before anything is queued.
            reason = readiness.runner_reason or (
                "Studio's model tool is not available, so only measures can be changed here"
            )
            raise ImportedEditError("desktop_unavailable", reason, 503, reasons=[reason])
        if not confirmed:
            only_measures = all(
                isinstance(op, CreateMeasure | UpdateMeasure | RenameMeasure | DeleteMeasure)
                for op in model
            )
            what = "a measure" if only_measures else "the model"
            raise ImportedEditError(
                "needs_confirmation",
                f"Changing {what} needs Power BI Desktop and is never done without your "
                "confirmation.",
                409,
                kind="model_edit",
                detail=" ".join(_model_line(op) for op in model),
            )

    # --- requests ---------------------------------------------------------------------------

    def submit_edit(
        self,
        workspace_id: str,
        operations: Sequence[dict[str, Any]],
        *,
        expected_head: str,
        idempotency_key: str,
        summary: str | None = None,
        desktop_work: DesktopWork = "ask",
        confirm_replace: bool = False,
        confirm_model: bool = False,
        answers: Sequence[Any] | None = None,
        confirmed: Sequence[str] = (),
    ) -> BatchHandle:
        """Queue one edit batch. ``answers`` are the user's replies to the questions an earlier
        attempt raised (``needs_choice``): each is ``{step, fill}`` and its ``fill`` is written
        over the fields of the operation at ``step`` (1-based) before anything is parsed
        (``merge_answers``). ``confirmed`` are batch-wide yeses (``report_tests_broken``).

        The receipt also says what Studio noticed (never blocking, never failing the edit):
        ``review``, the page findings the edit introduced; ``model_health.new``, the model
        findings a model or measure edit introduced on the objects it touched; ``tests``, the
        report tests a model edit was checked against."""
        report = self.report(workspace_id)
        self._require_editable(report)
        operations = self.merge_answers(operations, answers)
        ops = self.parse_operations(operations, confirm_replace=confirm_replace)
        # The consent flags (desktop_work, confirm_*) are not part of what the request is: the
        # answer to "Desktop has unsaved work" retries the same edit under the same key.
        fingerprint = _fingerprint("edit", list(operations), expected_head, summary)
        if not self._is_replay(report, idempotency_key):
            self._gate_model_operations(report, ops, confirm_model)
            self._require_head_id(report, expected_head)

        def job(progress: Progress) -> dict[str, Any]:
            fresh = self.report(workspace_id, in_lane=True)
            store = self._store_of(fresh)
            receipt = self.editor.apply(
                fresh,
                ops,
                expected_head=expected_head,
                summary=summary,
                idempotency_key=idempotency_key,
                desktop_work=desktop_work,
                progress=progress,
                confirmed=frozenset(confirmed),
            )
            body = self.receipt_payload(receipt, operations, store)
            self._judge_edit(fresh, store, receipt, operations, body)
            return body

        return self.submit(report, "edit", idempotency_key, fingerprint, job, summary=summary or "")

    def submit_undo(
        self,
        workspace_id: str,
        *,
        expected_head: str,
        idempotency_key: str,
        desktop_work: DesktopWork = "ask",
    ) -> BatchHandle:
        report = self.report(workspace_id)
        if not self._is_replay(report, idempotency_key):
            self._require_head_id(report, expected_head)
        fingerprint = _fingerprint("undo", expected_head)

        def job(progress: Progress) -> dict[str, Any]:
            fresh = self.report(workspace_id, in_lane=True)
            store = self._store_of(fresh)
            receipt = self.editor.undo(
                fresh,
                expected_head=expected_head,
                idempotency_key=idempotency_key,
                desktop_work=desktop_work,
                progress=progress,
            )
            return self.receipt_payload(receipt, [{"op": "undo"}], store)

        return self.submit(report, "undo", idempotency_key, fingerprint, job)

    def discard_preview(self, workspace_id: str, revision_id: str) -> dict[str, Any]:
        """The later changes a restore of ``revision_id`` would leave out."""
        report = self.report(workspace_id)
        store = self._store_of(report)
        try:
            later = {row.id for row in self.editor.restore_preview(report, revision_id)}
        except RevisionError as error:
            raise classify(error) from error
        rows = [entry for entry in store.history_with_flags() if entry.revision.id in later]
        return {"would_discard": [history_row(entry) for entry in rows]}

    def submit_restore(
        self,
        workspace_id: str,
        revision_id: str,
        *,
        expected_head: str,
        idempotency_key: str,
        confirmed_discard: bool = False,
        desktop_work: DesktopWork = "ask",
    ) -> BatchHandle:
        report = self.report(workspace_id)
        if not self._is_replay(report, idempotency_key):
            self._require_head_id(report, expected_head)
            # Refused before anything is queued: an unknown, pruned or current revision, and a
            # restore that would leave changes out without the user's say-so.
            try:
                self._store_of(report).check_restore(revision_id)
            except RevisionError as error:
                raise classify(error) from error
            later = self.editor.restore_preview(report, revision_id)
            if later and not confirmed_discard:
                raise classify(RestoreNeedsConfirmation(later))
        fingerprint = _fingerprint("restore", revision_id, expected_head)

        def job(progress: Progress) -> dict[str, Any]:
            fresh = self.report(workspace_id, in_lane=True)
            receipt = self.editor.restore(
                fresh,
                revision_id,
                expected_head=expected_head,
                confirmed_discard=confirmed_discard,
                idempotency_key=idempotency_key,
                desktop_work=desktop_work,
                progress=progress,
            )
            return self.receipt_payload(
                receipt, [{"op": "restore", "revision": revision_id}], self._store_of(fresh)
            )

        return self.submit(report, "restore", idempotency_key, fingerprint, job)

    def _is_replay(self, report: ImportedReport, key: str) -> bool:
        """Whether this key already has a batch that is running or finished, here or (after a
        restart) in the edit journal. A repeated request must get that batch's outcome, so it is
        not judged against the head, which the first attempt may have moved."""
        with self._registry:
            prior = self._by_key.get((report.id, key))
        if prior is not None:
            return prior.state != "failed"
        recorded = EditJournal(self._store_of(report).journal_path).find_key(key)
        return recorded is not None and recorded.state not in ("failed", "rolled_back")

    def _require_head_id(self, report: ImportedReport, expected_head: str) -> None:
        """Fail at once, before queueing, when the caller's head is not the head. (The edit
        layer checks again under the lock: another batch may land in between.)"""
        head = self._store_of(report).head()
        if head.id != expected_head:
            raise classify(RevisionConflict(expected_head, head.id))

    def history(self, workspace_id: str) -> list[dict[str, Any]]:
        report = self.report(workspace_id)
        return [history_row(entry) for entry in self.editor.history_with_flags(report)]

    def what_changed(self, workspace_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """The newest changes (Studio's and Desktop's), each with its operations."""
        rows = [
            row
            for row in self.history(workspace_id)
            if row["author"] in ("studio", "desktop", "undo", "restore")
        ]
        return rows[: max(1, limit)]

    def format_catalog(self, workspace_id: str, visual_type: str) -> dict[str, Any]:
        """The Format pane's controls for a visual type, with the report's theme defaults."""
        from .report_formatting import theme  # noqa: PLC0415 - pulls in the whole renderer

        report = self.report(workspace_id)
        archive, blob = report_import.report_archive(report)
        try:
            merged = theme(str(archive), blob)
        except (OSError, ValueError, zipfile.BadZipFile):
            merged = None
        return build_format_catalog(visual_type, merged)

    # --- delivery and Desktop ---------------------------------------------------------------

    def _destination(self, destination: str) -> Path:
        """A place for "Save a copy" that Studio may write: an absolute .pbix path or an
        existing folder, never inside Studio's own state and never through a link."""
        text = destination.strip()
        refusal = ImportedEditError
        if not text or "\x00" in text:
            raise refusal("edit_rejected", "Choose where to save the copy.", 422, step=None)
        path = Path(text).expanduser()
        if not path.is_absolute():
            raise refusal("edit_rejected", "Choose a full path for the copy.", 422, step=None)
        is_folder = path.is_dir()
        if not is_folder and path.suffix.casefold() != ".pbix":
            raise refusal("edit_rejected", "Save the report as a .pbix file.", 422, step=None)
        folder = path if is_folder else path.parent
        if not folder.is_dir():
            raise refusal("edit_rejected", f"The folder {folder} does not exist.", 422, step=None)
        try:
            resolved = folder.resolve(strict=True)
            keep = self.state_root.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise refusal(
                "edit_rejected", f"Studio cannot use the folder {folder}.", 422, step=None
            ) from error
        if resolved == keep or resolved.is_relative_to(keep):
            raise refusal(
                "edit_rejected",
                "That folder is where Studio keeps its own files. Choose another one, such as "
                "Documents.",
                422,
                step=None,
            )
        if not is_folder and path.is_symlink():
            raise refusal(
                "edit_rejected",
                "That name is a shortcut to another file. Choose a new file name.",
                422,
                step=None,
            )
        return resolved if is_folder else resolved / path.name

    def submit_save_copy(
        self, workspace_id: str, destination: str, *, idempotency_key: str
    ) -> BatchHandle:
        report = self.report(workspace_id)
        target = self._destination(destination)
        fingerprint = _fingerprint("save_copy", str(target))

        def job(progress: Progress) -> dict[str, Any]:
            fresh = self.report(workspace_id, in_lane=True)
            written = self.editor.save_copy(fresh, target)
            return {"path": str(written), "hash": self._store_of(fresh).head().blob}

        return self.submit(report, "save_copy", idempotency_key, fingerprint, job)

    def submit_replace_original(self, workspace_id: str, *, idempotency_key: str) -> BatchHandle:
        report = self.report(workspace_id)
        fingerprint = _fingerprint("replace_original", report.source_path)

        def job(progress: Progress) -> dict[str, Any]:
            fresh = self.report(workspace_id, in_lane=True)
            result = self.editor.replace_original(fresh)
            return {
                "path": str(result.path),
                "hash": result.hash,
                "backup": str(result.backup),
                "warnings": list(result.warnings),
            }

        return self.submit(report, "replace_original", idempotency_key, fingerprint, job)

    def submit_open(self, workspace_id: str) -> BatchHandle:
        """Open the head in Power BI Desktop: record a save the user made there, put the head in
        the working copy, then open it (P3). Replaces copying the original when missing."""
        report = self.report(workspace_id)
        key = f"open-{uuid.uuid4().hex}"

        def job(progress: Progress) -> dict[str, Any]:
            fresh = self.report(workspace_id, in_lane=True)
            original = Path(fresh.archived_path).resolve(strict=True)
            if (
                not original.is_relative_to(self.state_root / "reports")
                or sha256_file(original) != fresh.source_hash
            ):
                raise ValueError("The preserved report failed its integrity check")
            desktop = self._desktop_now()
            if not desktop.usable:
                raise ValueError(
                    "Install Power BI Desktop to open the native report"
                    if not desktop.readiness.desktop_ok
                    else " ".join(desktop.readiness.reasons) or _NEEDS_DESKTOP
                )
            store = self._store_of(fresh)
            already = desktop.port.find(store.working_copy) is not None
            if not already and self._before_launch is not None:
                # Desktop next to the 4B model would starve both of memory.
                self._before_launch()
            progress("Opening Power BI Desktop")
            self.editor.open_in_desktop(fresh)
            self.store.event(
                workspace_id, "report.desktop_launch_requested", {"report_id": fresh.id}
            )
            return {"launch_requested": True, "already_open": already}

        return self.submit(report, "open", key, _fingerprint("open", key), job)

    def open_body(self, handle: BatchHandle) -> BatchOutcome:
        """``outcome`` for an "open" batch: while Desktop is still starting, the answer is
        the launch request itself (the old route's answer), with the batch id to poll."""
        outcome = self.outcome(handle)
        if outcome.status == 202:
            body = {**outcome.body, "launch_requested": True, "already_open": False}
            return BatchOutcome(202, body)
        return outcome

    # --- start-up recovery (P9) -------------------------------------------------------------

    def start_recovery(self) -> threading.Thread:
        """Finish or undo interrupted batches of every imported report, in the background."""
        with self._registry:
            if self._recovery is not None and self._recovery.is_alive():
                return self._recovery
            self._recovery = threading.Thread(
                target=self.recover_all, name="edit-recovery", daemon=True
            )
            self._recovery.start()
            return self._recovery

    def recover_all(self) -> dict[str, RecoveryReport]:
        """Run recovery for every stored imported report; never raises."""
        results: dict[str, RecoveryReport] = {}
        try:
            reports = self.store.list_objects("imported_report", ImportedReport)
        except Exception:  # noqa: BLE001 - recovery must never stop the app from starting
            LOGGER.exception("Could not list the imported reports for recovery")
            return results
        for report in reports:
            try:
                if not self._needs_recovery(report):
                    self._heal(report)
                    continue
                result = self.editor.recover(report)
                self._heal(report)
            except Exception as error:  # noqa: BLE001
                LOGGER.warning("Recovery of report %s failed: %s", report.id, error)
                self._log_recovery(report, {"error": str(error)[:300]})
                continue
            results[report.id] = result
            if result.desktop is not None:
                self.recovered_desktops[report.id] = result.desktop
            if result.batches or result.removed_staging or result.desktop is not None:
                self._log_recovery(
                    report,
                    {
                        "batches": [
                            {
                                "batch": item.batch,
                                "from_state": item.from_state,
                                "action": item.action,
                                "detail": item.detail,
                            }
                            for item in result.batches
                        ],
                        "removed_staging": result.removed_staging,
                        "desktop_open": result.desktop is not None,
                        "desktop_pid": result.desktop.pid if result.desktop else None,
                    },
                )
        return results

    def _needs_recovery(self, report: ImportedReport) -> bool:
        """Whether there is anything to recover or re-attach to: a batch the journal left open,
        files a batch left in staging, or a working copy (a Desktop may have it open). A report
        that was never edited or opened costs nothing, in particular no search for Desktop."""
        store = self._store_of(report)
        if store.working_copy.is_file():
            return True
        with contextlib.suppress(OSError):
            if store.staging_dir.is_dir() and any(store.staging_dir.iterdir()):
                return True
        return bool(EditJournal(store.journal_path).open_batches())

    def _log_recovery(self, report: ImportedReport, payload: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            self.store.event(
                report.workspace_id, "report.edit_recovered", {"report_id": report.id, **payload}
            )

    def close(self, timeout: float = 4.0) -> None:
        """Give running batches a moment to finish on a graceful shutdown; whatever is still
        running is finished or undone by recovery at the next start. Stop starting Desktop
        searches and wait for the owned discovery within the same shutdown deadline."""
        deadline = time.monotonic() + timeout
        with self._desktop_lock:
            self._closing = True
            discovery = self._discovery
        with self._registry:
            handles = [h for h in self._batches.values() if h.state not in _TERMINAL]
        for handle in handles:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            handle.wait(remaining)
        if discovery is not None and discovery is not threading.current_thread():
            remaining = deadline - time.monotonic()
            if remaining > 0:
                discovery.join(remaining)

    # --- judging the report: review, model health, report tests ----------------------------
    # (docs/imported-report-review.md, imported-model-health.md, imported-report-tests.md.)
    # Deterministic code only: no finding, number or result comes from the language model, and
    # none of it ever blocks or fails an edit that was committed.

    @staticmethod
    def _document(path: Path) -> PbirDocument:
        return PbirDocument(PbixPackage(path))

    def _judge_edit(
        self,
        report: ImportedReport,
        store: RevisionStore,
        receipt: EditReceipt,
        operations: Sequence[dict[str, Any]],
        body: dict[str, Any],
    ) -> None:
        """What Studio noticed about a committed edit, added to its receipt ``body``: the page
        review, the model findings it introduced, and the report tests it was checked against
        (their run is recorded for the new head; the tests the user let it break are named in
        the warnings)."""
        body["review"] = self._edit_review(store, receipt.revision)
        if receipt.model_operations:
            body["model_health"] = {"new": self._health_news(report, store, receipt, operations)}
        tests = self._tests_after_commit(receipt, store)
        if tests is not None:
            body["tests"] = tests
            if tests["broken"]:
                body["warnings"] = [*body.get("warnings", []), tests["text_broken"]]

    # -- page review --------------------------------------------------------------------------

    def review_page(
        self,
        workspace_id: str,
        page_id: str,
        *,
        revision: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``report_review.review_page`` of one page of the head (by id, name or position);
        ``payload`` is the page as Studio drew it (``render_page``), so the rules that read rows
        run too. A stale ``revision`` is ``head_moved``; an unknown page ``unknown_page``."""
        report = self.report(workspace_id)
        head = self.require_head(report, revision)
        store = self._store_of(report)
        try:
            document = self._document(store.path(head))
        except (DocumentError, PackageError) as error:
            raise ImportedEditError("report_unreadable", str(error), 422) from error
        try:
            page = document.resolve_page(page_id)
        except DocumentError as error:
            raise ImportedEditError("unknown_page", str(error), 422) from error
        found = report_review.review_page(document, page.name, payload=payload)
        return {**found.to_dict(), "head": {"id": head.id, "hash": head.blob}}

    def review_after_edit(
        self,
        before: PbirDocument,
        after: PbirDocument,
        page_ids: Sequence[str],
        touched_ids: Sequence[str],
        after_payload: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """The findings an edit introduced or made worse on ``page_ids`` (``review_edit``), the
        visuals it touched first. A page the edit added has only new findings. A rule the
        review before the edit could not run (it had no payload) is not reported: it would
        only look new."""
        found: list[dict[str, Any]] = []
        for page_id in list(page_ids)[:REVIEW_MAX_PAGES]:
            page = after.pages.get(page_id)
            if page is None:
                continue
            focus = [v for v in touched_ids if v in page.visuals]
            payload = after_payload if _payload_of(after_payload, page_id) else None
            now = report_review.review_page(after, page_id, payload=payload, focus=focus)
            if page_id in before.pages:
                earlier = report_review.review_page(before, page_id, focus=focus)
                fresh = [
                    f
                    for f in report_review.review_edit(earlier, now)
                    if f.rule not in earlier.skipped
                ]
            else:
                fresh = list(now.findings)
            found.extend(f.to_dict() for f in fresh)
        return found[:REVIEW_MAX_FINDINGS]

    def _edit_review(self, store: RevisionStore, revision: Revision) -> list[dict[str, Any]]:
        """``review_after_edit`` of a committed revision against its parent; [] when anything
        stops it (a review is a courtesy, never a reason to fail an edit)."""
        if revision.parent is None:
            return []
        try:
            parent = store.revision(revision.parent)
            if not (store.available(parent) and store.available(revision)):
                return []
            before = self._document(store.path(parent))
            after = self._document(store.path(revision))
            pages, touched = _touched(before, after)
            return self.review_after_edit(before, after, pages, touched)
        except Exception:  # noqa: BLE001 - the edit is committed; the review only adds to it
            LOGGER.warning("Could not review revision %s", revision.id, exc_info=True)
            return []

    # -- model health -------------------------------------------------------------------------

    def _kept_facts(self, report_id: str, path: Path) -> dict[str, Any] | None:
        """The facts of the model in ``path`` only if Studio already has them (memory, or the
        copy beside the history): never a read in a child process."""
        try:
            key = report_import.model_key(path)
        except (OSError, zipfile.BadZipFile):
            return None
        with self._facts_lock:
            found = self._facts.get((report_id, key))
        if found is not None:
            return found
        return report_import.cached_facts(self._facts_root(report_id), key)

    def _health_of(
        self,
        report: ImportedReport,
        path: Path,
        blob: str,
        *,
        stats: bool = False,
        read: bool = True,
    ) -> model_health.Health:
        """The health check of the model in ``path`` (a revision of ``report``), with the
        report's use of it read from the same file. Kept per (revision, model, rows, stats).
        ``read`` False uses only facts Studio already has (unknown otherwise): the check after
        an edit must not start a model read that can take minutes."""
        try:
            model = report_import.model_key(path)
        except (OSError, zipfile.BadZipFile):
            model = "none"
        snapshot = self._snapshot_of(report) if stats and self._snapshot_of is not None else None
        key = (report.id, blob, model, snapshot.id if snapshot is not None else "", str(stats))
        with self._health_lock:
            cached = self._health.get(key)
            if cached is not None:
                self._health.move_to_end(key)
                return cached
        facts: dict[str, Any] | None = None
        if not read:
            facts = self._kept_facts(report.id, path)
            if facts is None:
                return model_health.check_model(None, usage=None)  # unknown, and not kept
        elif _has_data_model(path):
            try:
                facts = self._facts_for(report.id, path)
            except (EditRejected, OSError, ValueError, zipfile.BadZipFile):
                facts = None
        usage: model_health.Usage | None
        try:
            usage = model_health.report_usage(self._document(path))
        except (DocumentError, PackageError, OSError, ValueError):
            usage = None
        column_stats = self._health_stats(report, facts, snapshot) if stats else None
        found = model_health.check_model(facts, usage=usage, stats=column_stats)
        with self._health_lock:
            self._health[key] = found
            while len(self._health) > HEALTH_IN_MEMORY:
                self._health.popitem(last=False)
        return found

    @staticmethod
    def _health_stats(
        report: ImportedReport, facts: Mapping[str, Any] | None, snapshot: DatasetSnapshot | None
    ) -> dict[tuple[str, str], model_health.ColumnStats] | None:
        """Column statistics from the rows Studio decoded, when they are the model's current
        data (rows Power BI has refreshed since would describe other data)."""
        if facts is None or snapshot is None:
            return None
        if facts_data_status(report.model, report.snapshot_model)["stale"]:
            return None
        files = {t.table.name: t.normalized_parquet_path for t in snapshot.tables}
        try:
            return model_health.stats_from_parquet(files, wanted=model_health.stats_wanted(facts))
        except (OSError, ValueError, KeyError, ImportError) as error:
            LOGGER.warning("Could not read column statistics of report %s: %s", report.id, error)
            return None

    def health_check(self, workspace_id: str, *, stats: bool = False) -> model_health.Health:
        """The head model's health check (``model_health.check_model``)."""
        report = self.report(workspace_id)
        head = self._store_of(report).head()
        return self._health_of(report, self._store_of(report).path(head), head.blob, stats=stats)

    def model_health(self, workspace_id: str, *, stats: bool = False) -> dict[str, Any]:
        """The head model's health check (``Health.as_dict``) plus the ``head`` it was read on
        (a fix names its hash). Unknown facts give ``known: false``, never an error."""
        report = self.report(workspace_id)
        head = self._store_of(report).head()
        path = self._store_of(report).path(head)
        found = self._health_of(report, path, head.blob, stats=stats)
        return {**found.as_dict(), "head": {"id": head.id, "hash": head.blob}}

    @staticmethod
    def model_health_rules() -> list[dict[str, str]]:
        """Every rule the check runs: ``[{id, category, severity, title}]``."""
        return [
            {"id": r.id, "category": r.category, "severity": r.severity, "title": r.title}
            for r in model_health.RULES.values()
        ]

    def model_health_fix(
        self,
        workspace_id: str,
        finding_id: str,
        *,
        base_hash: str,
        confirmed: bool,
        idempotency_key: str,
        desktop_work: DesktopWork = "ask",
    ) -> BatchHandle:
        """Apply one finding's fix as one edit through the normal pipeline (Power BI Desktop,
        the model gate, the report-tests gate). It always asks first: without ``confirmed`` it
        is ``needs_confirmation`` (kind ``model_health_fix``). A finding that is gone (fixed,
        or the model changed) is ``finding_gone``; a head other than ``base_hash`` is
        ``head_moved``."""
        report = self.report(workspace_id)
        with self._registry:
            prior = self._by_key.get((report.id, idempotency_key))
        if prior is not None and prior.state != "failed":
            return prior  # a repeated request gets its batch, though the finding is gone now
        store = self._store_of(report)
        head = store.head()
        if base_hash != head.blob:
            raise ImportedEditError(
                "head_moved",
                "The report changed since the model was checked. Check the model again.",
                409,
                expected=base_hash,
                actual=head.blob,
            )
        health = self._health_of(report, store.path(head), head.blob)
        finding = next((f for f in health.findings if f.id == finding_id), None)
        if finding is None:
            raise ImportedEditError(
                "finding_gone", "This was already fixed or changed. Check the model again.", 409
            )
        if finding.fix is None:
            raise ImportedEditError(
                "no_fix",
                f"Studio has no automatic fix for this. {finding.fix_summary}".strip(),
                422,
            )
        if not confirmed:
            raise ImportedEditError(
                "needs_confirmation",
                finding.fix_summary,
                409,
                kind="model_health_fix",
                detail=finding.fix_summary,
                outside_supported=finding.fix_outside_supported,
                needs_desktop=finding.fix_needs_desktop,
                finding=finding.id,
            )
        return self.submit_edit(
            workspace_id,
            [dict(op) for op in finding.fix],
            expected_head=head.id,
            idempotency_key=idempotency_key,
            summary=finding.fix_summary[:300] or None,
            desktop_work=desktop_work,
            confirm_model=True,
        )

    def _health_news(
        self,
        report: ImportedReport,
        store: RevisionStore,
        receipt: EditReceipt,
        operations: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """The model findings a model or measure edit introduced on the objects it touched:
        a finding the model did not have before, or one that now names an object the edit
        touched and did not name it before. [] when either side is unknown or on any error."""
        revision = receipt.revision
        if revision.parent is None:
            return []
        try:
            parent = store.revision(revision.parent)
            # Only models Studio has read already (the view of the old head, and the new head's,
            # which moving the view to it read): no model is read again for this.
            before = self._health_of(report, store.path(parent), parent.blob, read=False)
            after = self._health_of(receipt.report, store.path(revision), revision.blob, read=False)
            if not (before.known and after.known):
                return []
            facts = self._kept_facts(report.id, store.path(revision))
            ops = [*operations, *(_plain_operation(op) for op in receipt.model_operations)]
            touched = _touched_objects(ops, facts)
            earlier = {f.id: set(f.objects) for f in before.findings}
            return [
                f.as_dict()
                for f in model_health.findings_for(after, touched)
                if f.id not in earlier
                or any(o in touched and o not in earlier[f.id] for o in f.objects)
            ]
        except Exception:  # noqa: BLE001 - the edit is committed; the check only adds to it
            LOGGER.warning(
                "Could not check the model after revision %s", revision.id, exc_info=True
            )
            return []

    # -- report tests -------------------------------------------------------------------------

    def _evaluator(self, report: ImportedReport) -> report_tests.Evaluate:
        """Studio's engine over the report's rows (``LocalEvaluator``)."""
        if self._tests_evaluator is not None:
            return self._tests_evaluator(report)
        snapshot = self._snapshot_of(report) if self._snapshot_of is not None else None
        if snapshot is None:
            return _NoRows()
        return report_tests.LocalEvaluator(report, snapshot)

    def _desktop_evaluator(self, report: ImportedReport) -> report_tests.QueryEvaluator | None:
        """Power BI Desktop's own model, only when it already has exactly the head open and
        saved (never opened for this) and Studio's model tool can run queries on it."""
        desktop = self._desktop_peek()
        if desktop is None or not desktop.usable:
            return None
        port = desktop.port
        query = getattr(port, "query_model", None)
        if not callable(query):
            return None
        problem = getattr(port, "model_runner_problem", None)
        try:
            if callable(problem) and problem():
                return None
            store = self._store_of(report)
            placed = store.placed()
            if placed is None or placed[0] != store.head().id:
                return None
            instance = port.find(store.working_copy)
            if instance is None or port.status(instance).get("hasUnsavedChanges"):
                return None
        except (DesktopSessionError, OSError, subprocess.SubprocessError, KeyError, RevisionError):
            return None

        def run(queries: Mapping[str, str]) -> Mapping[str, Any]:
            try:
                found: Mapping[str, Any] = query(instance, queries)
            except DesktopSessionError as error:
                raise RuntimeError(str(error)) from error
            return found

        return report_tests.QueryEvaluator(run)

    def _stored_tests(self, report: ImportedReport) -> list[report_tests.TestDefinition]:
        try:
            return self.tests.tests(report.id)
        except report_tests.TestStoreError as error:
            raise ImportedEditError("tests_unreadable", str(error), 422) from error

    def _record_run(
        self,
        report_id: str,
        revision: Revision,
        results: Sequence[report_tests.TestResult],
        engine: str,
    ) -> report_tests.TestRun:
        run = report_tests.TestRun(
            revision=revision.id,
            hash=revision.blob,
            engine=engine,
            ran_at=report_tests.now_text(),
            results=list(results),
        )
        try:
            self.tests.record_run(report_id, run)
        except report_tests.TestStoreError as error:
            LOGGER.warning("Could not record the test run of report %s: %s", report_id, error)
        return run

    def report_tests(self, workspace_id: str) -> dict[str, Any]:
        """``{tests, last_run, stale}``; ``stale`` when the last run was for another head."""
        report = self.report(workspace_id)
        head = self._store_of(report).head()
        try:
            stored = self.tests.read(report.id)
        except report_tests.TestStoreError as error:
            raise ImportedEditError("tests_unreadable", str(error), 422) from error
        last = stored.last_run
        return {
            "tests": [t.model_dump(mode="json") for t in stored.tests],
            "last_run": last.model_dump(mode="json") if last is not None else None,
            "stale": last is not None and last.hash != head.blob,
        }

    def pin_report_test(
        self,
        workspace_id: str,
        page_id: str,
        visual_id: str,
        point: Mapping[str, Any] | None,
        value: Any,
        *,
        measure: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Pin the number a visual shows (``pin_from_visual``), check it on Studio's engine and
        keep it whatever the result: ``{test, result}``, so the UI can say "Studio calculates
        850 here; the card showed 851" when it did not pass. Refusals are ``pin_refused``."""
        report = self.report(workspace_id)
        store = self._store_of(report)
        try:
            document: PbirDocument | None = self._document(store.path(store.head()))
        except (DocumentError, PackageError, OSError):
            document = None
        try:
            test = report_tests.pin_from_visual(
                document,
                page_id,
                visual_id,
                dict(point or {}),
                value=value,
                report=report,
                measure=measure,
                name=name,
            )
        except report_tests.PinError as error:
            raise ImportedEditError("pin_refused", str(error), 422) from error
        result = report_tests.run_tests([test], self._evaluator(report))[0]
        try:
            self.tests.add(report.id, test)
        except report_tests.TestStoreError as error:
            raise ImportedEditError("pin_refused", str(error), 422) from error
        return {"test": test.model_dump(mode="json"), "result": result.model_dump(mode="json")}

    def add_report_test(self, workspace_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """A test the user wrote (``new_user_test``): ``{name, measure?, expression?, filters,
        expect, format?}``. An invalid one is ``invalid_test`` with the first problem."""
        report = self.report(workspace_id)
        try:
            measure = body.get("measure")
            test = report_tests.new_user_test(
                str(body.get("name") or ""),
                expect=_EXPECTATION.validate_python(body.get("expect")),
                measure=report_tests.MeasureRef.model_validate(measure) if measure else None,
                expression=body.get("expression"),
                filters=[
                    report_tests.TestFilter.model_validate(f) for f in body.get("filters") or []
                ],
                fmt=body.get("format"),
            )
            self.tests.add(report.id, test)
        except ValidationError as error:
            raise ImportedEditError("invalid_test", _first_problem(error), 422) from error
        except (report_tests.TestStoreError, ValueError) as error:
            raise ImportedEditError("invalid_test", str(error), 422) from error
        return test.model_dump(mode="json")

    def update_report_test(
        self, workspace_id: str, test_id: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        """A test's ``name``, ``expect`` or ``filters`` changed; its id stays."""
        report = self.report(workspace_id)
        current = next((t for t in self._stored_tests(report) if t.id == test_id), None)
        if current is None:
            raise ImportedEditError("unknown_test", "This report has no such test.", 404)
        changes = {k: body[k] for k in ("name", "expect", "filters") if body.get(k) is not None}
        try:
            updated = report_tests.TestDefinition.model_validate(
                {**current.model_dump(mode="json"), **changes}
            )
            self.tests.replace(report.id, updated)
        except ValidationError as error:
            raise ImportedEditError("invalid_test", _first_problem(error), 422) from error
        except (report_tests.TestStoreError, ValueError) as error:
            raise ImportedEditError("invalid_test", str(error), 422) from error
        return updated.model_dump(mode="json")

    def delete_report_test(self, workspace_id: str, test_id: str) -> dict[str, Any]:
        report = self.report(workspace_id)
        try:
            removed = self.tests.remove(report.id, test_id)
        except report_tests.TestStoreError as error:
            raise ImportedEditError("tests_unreadable", str(error), 422) from error
        return {"removed": removed}

    def run_report_tests(
        self, workspace_id: str, engine: Literal["local", "desktop", "auto"] = "auto"
    ) -> dict[str, Any]:
        """Run every test and record the run for the head: ``TestRun`` plus ``text``
        (``describe_results``). ``auto`` runs Studio's engine first and asks Power BI Desktop
        (only when it already has the head open) for the tests the engine left unchecked."""
        report = self.report(workspace_id)
        head = self._store_of(report).head()
        tests = self._stored_tests(report)
        desktop = self._desktop_evaluator(report) if engine != "local" else None
        engines: list[str] = []
        if engine == "desktop":
            evaluate: report_tests.Evaluate = desktop if desktop is not None else _NoDesktop()
            results = report_tests.run_tests(tests, evaluate)
            engines.append(report_tests.DESKTOP_LABEL)
        else:
            local = self._evaluator(report)
            results = report_tests.run_tests(tests, local)
            engines.append(str(getattr(local, "label", ENGINE_LABEL)))
            unchecked = [
                t for t, r in zip(tests, results, strict=True) if r.status == "not_checked"
            ]
            if engine == "auto" and unchecked and desktop is not None:
                again = {r.id: r for r in report_tests.run_tests(unchecked, desktop)}
                results = [
                    again[r.id] if r.id in again and again[r.id].status != "not_checked" else r
                    for r in results
                ]
                engines.append(report_tests.DESKTOP_LABEL)
        run = self._record_run(report.id, head, results, " and ".join(engines))
        return {**run.model_dump(mode="json"), "text": report_tests.describe_results(results)}

    def suggest_report_tests(
        self,
        workspace_id: str,
        page_ids: Sequence[str] | None = None,
        *,
        render: PageRenderer | None = None,
    ) -> dict[str, Any]:
        """Tests worth pinning (``suggest_tests``), proposed and never stored: ``{tests, text}``
        (``text``: one sentence per test). ``render`` draws a page with Studio's engine."""
        report = self.report(workspace_id)
        draw = render or self._render_local
        pages = list(page_ids) if page_ids else [p.id for p in report.pages]
        payloads: list[Mapping[str, Any]] = []
        for page_id in pages:
            try:
                payloads.append(draw(report, page_id))
            except Exception as error:  # noqa: BLE001 - a page Studio cannot draw offers nothing
                LOGGER.info("No suggestions from page %s: %s", page_id, error)
        found = report_tests.suggest_tests(None, payloads, report=report)
        return {
            "tests": [t.model_dump(mode="json") for t in found],
            "text": [report_tests.describe(t) for t in found],
        }

    def _render_local(self, report: ImportedReport, page_id: str) -> Mapping[str, Any]:
        from .live_report import LiveReportRequest, render_page  # noqa: PLC0415 - the renderer
        from .report_engine import LocalReportEngine, report_model  # noqa: PLC0415

        snapshot = self._snapshot_of(report) if self._snapshot_of is not None else None
        if snapshot is None:
            raise ValueError("This report has no rows in Studio")
        engine = LocalReportEngine(report_model(report, snapshot))
        return render_page(engine, report, page_id, LiveReportRequest())

    def _begin_tests(
        self,
        report: ImportedReport,
        head: Revision,
        operations: list[Any],
        confirmed: bool,
    ) -> _TestsCheck | None:
        """Step 1 of the report-tests gate (see ``_TestsCheck``); None when the report has no
        tests. A damaged tests file never blocks an edit: the Tests pane reports it."""
        try:
            tests = self.tests.tests(report.id)
        except report_tests.TestStoreError as error:
            LOGGER.warning("Report %s: tests not checked (%s)", report.id, error)
            return None
        if not tests:
            return None
        ids = {t.id for t in tests}
        last = None
        with contextlib.suppress(report_tests.TestStoreError):
            last = self.tests.last_run(report.id)
        if last is not None and last.hash == head.blob and ids <= {r.id for r in last.results}:
            before = [r for r in last.results if r.id in ids]
        else:
            evaluate = self._evaluator(report)
            before = report_tests.run_tests(tests, evaluate)
            label = str(getattr(evaluate, "label", ENGINE_LABEL))
            self._record_run(report.id, head, before, label)
        check = _TestsCheck(
            self,
            report,
            list(operations),
            report_tests.follow_renames(tests, operations),
            before,
            confirmed,
        )
        projected = report_tests.projected_report(report, operations)
        if projected is None:
            check.needs_saved = True
        else:
            check.judge(projected)
        return check

    def _tests_after_commit(
        self, receipt: EditReceipt, store: RevisionStore
    ) -> dict[str, Any] | None:
        """Step 3: the tests follow the batch's renames, the run is recorded for the new head,
        and the receipt names the tests the user let the edit break."""
        check = receipt.tests
        if not isinstance(check, _TestsCheck) or check.after is None:
            return None
        report_id = check.report.id
        try:
            if any(_op_name(op) in _RENAME_OPS for op in check.operations):
                self.tests.replace_all(report_id, check.renamed)
        except report_tests.TestStoreError as error:
            LOGGER.warning("Could not rename the tests of report %s: %s", report_id, error)
        self._record_run(report_id, receipt.revision, check.after, check.engine)
        return {
            "results": [r.model_dump(mode="json") for r in check.after],
            "broken": [r.model_dump(mode="json") for r in check.broken],
            "text": report_tests.describe_results(check.after),
            "text_broken": (
                "You applied this change although it breaks report tests. "
                + report_tests.describe_results(check.broken)
                if check.broken
                else ""
            ),
        }

    # --- the chat port ----------------------------------------------------------------------

    @property
    def port(self) -> EditPort:
        return _ChatPort(self)

    def remember_last_edit(
        self, workspace_id: str, revision_id: str, ops: Sequence[dict[str, Any]], summary: str
    ) -> None:
        self._last_edits[workspace_id] = LastEdit(
            revision_id=revision_id, ops=tuple(dict(op) for op in ops), summary=summary
        )

    def _last_edit(self, workspace_id: str, store: RevisionStore) -> LastEdit | None:
        head = store.head()
        noted = self._last_edits.get(workspace_id)
        if noted is not None and noted.revision_id == head.id:
            return noted
        for row in store.history():  # newest first
            if row.author in ("studio", "desktop"):
                return LastEdit(
                    revision_id=row.id,
                    ops=tuple(row.operations),
                    summary=row.summary,
                    author=row.author,
                )
        return None

    def edit_context(
        self, workspace_id: str, selection: str | None, active_page: str | None
    ) -> EditContext:
        from .edit_context import hue_family  # noqa: PLC0415
        from .report_formatting import theme  # noqa: PLC0415

        report = self.report(workspace_id)
        store = self._store_of(report)
        archive, blob = report_import.report_archive(report)
        palette: dict[str, str] = {}
        try:
            colors = theme(str(archive), blob).get("dataColors", [])
        except (OSError, ValueError, zipfile.BadZipFile):
            colors = []
        # A color word in a request ("red below zero", "a navy rectangle") is the theme's color
        # of that hue when the theme has one; the first entry is the primary data color.
        for color in colors if isinstance(colors, list) else []:
            if isinstance(color, str):
                hue = hue_family(color)
                if hue and hue not in palette:
                    palette[hue] = color
        return EditContext.from_report(
            report,
            selection=selection,
            active_page=active_page,
            head_revision=store.head().id,
            last_edit=self._last_edit(workspace_id, store),
            palette=palette,
        )


class _ChatPort:
    """``EditPort`` over the service: every call queues on the report's lane and waits."""

    def __init__(self, service: ImportedEditingService) -> None:
        self._service = service

    def _finish(
        self, handle: BatchHandle, progress: Callable[[dict[str, Any]], None] | None
    ) -> dict[str, Any]:
        for event in handle.events(heartbeat=None):
            if progress is not None and event is not None:
                progress(event)
        if handle.error is not None:
            raise handle.error
        assert handle.result is not None
        return handle.result

    def head(self, workspace_id: str) -> dict[str, Any]:
        return self._service.head(workspace_id)

    def edit_context(
        self, workspace_id: str, selection: str | None, active_page: str | None
    ) -> EditContext:
        return self._service.edit_context(workspace_id, selection, active_page)

    def model_step_support(
        self, workspace_id: str, operations: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        return self._service.model_step_support(workspace_id, operations)

    def apply(
        self,
        workspace_id: str,
        ops: Sequence[dict[str, Any]],
        *,
        summary: str | None,
        idempotency_key: str,
        expected_head: str,
        progress: Callable[[dict[str, Any]], None] | None = None,
        desktop_work: DesktopWork = "ask",
        confirm_model: bool = False,
        confirm_replace: bool = False,
        answers: Sequence[Any] | None = None,
        confirmed: Sequence[str] = (),
    ) -> dict[str, Any]:
        handle = self._service.submit_edit(
            workspace_id,
            ops,
            expected_head=expected_head,
            idempotency_key=idempotency_key,
            summary=summary,
            desktop_work=desktop_work,
            confirm_model=confirm_model,
            confirm_replace=confirm_replace,
            answers=answers,
            confirmed=confirmed,
        )
        return self._finish(handle, progress)

    def undo(
        self,
        workspace_id: str,
        *,
        expected_head: str,
        idempotency_key: str,
        progress: Callable[[dict[str, Any]], None] | None = None,
        desktop_work: DesktopWork = "ask",
    ) -> dict[str, Any]:
        handle = self._service.submit_undo(
            workspace_id,
            expected_head=expected_head,
            idempotency_key=idempotency_key,
            desktop_work=desktop_work,
        )
        return self._finish(handle, progress)

    def restore(
        self,
        workspace_id: str,
        revision_id: str,
        *,
        expected_head: str,
        idempotency_key: str,
        confirmed_discard: bool = False,
        progress: Callable[[dict[str, Any]], None] | None = None,
        desktop_work: DesktopWork = "ask",
    ) -> dict[str, Any]:
        handle = self._service.submit_restore(
            workspace_id,
            revision_id,
            expected_head=expected_head,
            idempotency_key=idempotency_key,
            confirmed_discard=confirmed_discard,
            desktop_work=desktop_work,
        )
        return self._finish(handle, progress)

    def history(self, workspace_id: str) -> list[dict[str, Any]]:
        return self._service.history(workspace_id)

    def what_changed(self, workspace_id: str, limit: int = 10) -> list[dict[str, Any]]:
        return self._service.what_changed(workspace_id, limit)

    def remember_last_edit(
        self, workspace_id: str, revision_id: str, ops: Sequence[dict[str, Any]], summary: str
    ) -> None:
        self._service.remember_last_edit(workspace_id, revision_id, ops, summary)


# --- the report-tests gate (docs/imported-report-tests.md, section 8) -------------------------


class _TestsCheck:
    """One model batch's report-tests check (``report_editor.TestsCheck``): the results before
    the edit, the tests with the batch's renames followed, and the results after it (on the
    projected measures before Desktop is opened, else on the model Desktop saved)."""

    def __init__(
        self,
        service: ImportedEditingService,
        report: ImportedReport,
        operations: list[Any],
        renamed: list[report_tests.TestDefinition],
        before: list[report_tests.TestResult],
        confirmed: bool,
    ) -> None:
        self.service = service
        self.report = report
        self.operations = operations
        self.renamed = renamed
        self.before = before
        self.confirmed = confirmed
        self.needs_saved = False
        self.after: list[report_tests.TestResult] | None = None
        self.broken: list[report_tests.TestResult] = []
        self.engine = ENGINE_LABEL

    def judge(self, view: ImportedReport) -> None:
        """Run the tests on ``view`` (the report as the edit leaves it); raises
        ``ReportTestsBroken`` for tests that passed before and fail now, unless confirmed."""
        evaluate = self.service._evaluator(view)  # noqa: SLF001 - the service's own gate
        self.engine = str(getattr(evaluate, "label", ENGINE_LABEL))
        after = report_tests.run_tests(self.renamed, evaluate)
        self.broken = report_tests.check_gate(self.before, after, confirmed=self.confirmed)
        self.after = after

    def after_save(self, saved: ImportedReport) -> None:
        self.judge(saved)


class _ReportTestsGate:
    """``report_editor.TestsGate`` over the service's test store and evaluators."""

    def __init__(self, service: ImportedEditingService) -> None:
        self._service = service

    def begin(
        self, report: ImportedReport, head: Revision, operations: list[Any], *, confirmed: bool
    ) -> TestsCheck | None:
        return self._service._begin_tests(  # noqa: SLF001 - the service's own gate
            report, head, operations, confirmed
        )


class _NoRows:
    """The evaluator of a report Studio has no rows for: every test is "not checked"."""

    label = ENGINE_LABEL

    def __call__(self, test: report_tests.TestDefinition) -> report_tests.Value:
        raise report_tests.NotComputable("Studio has no rows of this report's data")


class _NoDesktop:
    """The evaluator when Power BI Desktop does not have this report open (it is never opened
    to run tests)."""

    label = report_tests.DESKTOP_LABEL

    def __call__(self, test: report_tests.TestDefinition) -> report_tests.Value:
        raise report_tests.NotComputable(
            "Power BI Desktop does not have this version of the report open"
        )


_EXPECTATION: TypeAdapter[report_tests.Expectation] = TypeAdapter(report_tests.Expectation)


def _first_problem(error: ValidationError) -> str:
    errors = error.errors()
    if not errors:
        return "This test is not valid."
    message = str(errors[0].get("msg") or "This test is not valid.")
    return message.removeprefix("Value error, ")


# --- judgement helpers ------------------------------------------------------------------------


def _op_name(op: Any) -> str:
    return str(op.get("op", "")) if isinstance(op, Mapping) else str(getattr(op, "op", ""))


def _payload_of(payload: Mapping[str, Any] | None, page_id: str) -> bool:
    page = payload.get("page") if isinstance(payload, Mapping) else None
    return isinstance(page, Mapping) and page.get("id") == page_id


def _report_json(document: PbirDocument) -> Any:
    try:
        return json.loads(document.package.read("Report/definition/report.json"))
    except (KeyError, PackageError, ValueError):
        return None


def _touched(before: PbirDocument, after: PbirDocument) -> tuple[list[str], list[str]]:
    """(pages, visuals) an edit changed, compared as JSON (a Desktop save rewrites every part's
    bytes without changing what it says). A change to report.json touches every page."""
    everywhere = _report_json(before) != _report_json(after)
    pages: list[str] = []
    visuals: list[str] = []
    for name, page in after.pages.items():
        old = before.pages.get(name)
        changed = [
            v for v, body in page.visuals.items() if old is None or old.visuals.get(v) != body
        ]
        removed = old is not None and any(v not in page.visuals for v in old.visuals)
        if old is None or changed or removed or old.data != page.data or everywhere:
            pages.append(name)
            visuals.extend(changed)
    return pages, visuals


def _touched_objects(
    ops: Sequence[Mapping[str, Any]], facts: Mapping[str, Any] | None
) -> list[model_health.ObjectRef]:
    """The model objects a batch touched, as model health names them (a measure by its home
    table, read from the new model's facts)."""
    homes: dict[str, str] = {}
    columns: dict[str, list[str]] = {}
    for item in (facts or {}).get("measures") or []:
        if isinstance(item, Mapping) and item.get("name"):
            homes[str(item["name"]).casefold()] = str(item.get("table") or "")
    for item in (facts or {}).get("columns") or []:
        if isinstance(item, Mapping) and item.get("table") and item.get("name"):
            columns.setdefault(str(item["table"]).casefold(), []).append(str(item["name"]))
    found: list[model_health.ObjectRef] = []

    def names(op: Mapping[str, Any], *keys: str) -> list[str]:
        return [str(op[k]) for k in keys if isinstance(op.get(k), str) and op[k]]

    for op in ops:
        name = _op_name(op)
        if name in _MEASURE_OPS:
            for measure in names(op, "name", "new_name"):
                home = homes.get(measure.casefold(), str(op.get("table") or ""))
                found.append(model_health.ObjectRef("measure", home, measure))
        elif name in _COLUMN_OPS:
            table = str(op.get("table") or "")
            found += [
                model_health.ObjectRef("column", table, c) for c in names(op, "name", "new_name")
            ]
        elif name in _TABLE_OPS:
            for table in names(op, "table", "name", "new_name"):
                found.append(model_health.ObjectRef("table", table, table))
                found += [
                    model_health.ObjectRef("column", table, c)
                    for c in columns.get(table.casefold(), [])
                ]
        elif name in _RELATIONSHIP_OPS:
            found.append(
                model_health.ObjectRef(
                    "relationship",
                    str(op.get("from_table") or ""),
                    str(op.get("from_column") or ""),
                    str(op.get("to_table") or ""),
                    str(op.get("to_column") or ""),
                )
            )
        elif name in {"set_calculation_item", "delete_calculation_item"}:
            found.append(
                model_health.ObjectRef(
                    "calculation_item", str(op.get("group") or ""), str(op.get("name") or "")
                )
            )
    return found


# --- helpers --------------------------------------------------------------------------------


def _plain_items(items: Any) -> list[dict[str, Any]]:
    """Dataclass or mapping items (Protected, VisibleIn) as plain dicts for the JSON body."""
    found: list[dict[str, Any]] = []
    for item in items or ():
        if isinstance(item, dict):
            found.append(dict(item))
        elif dataclasses.is_dataclass(item) and not isinstance(item, type):
            found.append(dataclasses.asdict(item))
    return found


def _answer_field(answer: Any, name: str) -> Any:
    return answer.get(name) if isinstance(answer, Mapping) else getattr(answer, name, None)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _plain_dicts(value: Any) -> list[dict[str, Any]]:
    """The entries of a recorded list that are objects, as copies (a journal line may hold
    anything)."""
    return [dict(item) for item in _as_list(value) if isinstance(item, dict)]


def _plain_operation(op: Any) -> dict[str, Any]:
    """A typed operation (or one already a dict) as a plain dict for the JSON body."""
    if hasattr(op, "model_dump"):
        return dict(op.model_dump(mode="json"))
    return dict(op) if isinstance(op, dict) else {"op": str(op)}


def _fingerprint(*parts: Any) -> str:
    text = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _same_file(a: str | Path, b: str | Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def _model_line(op: Any) -> str:
    """The line the confirmation before Desktop says for one model operation, worded from the
    operation that will be sent ("Hide DimStore[StoreKey] in the model"); never a formula or a
    query's text."""
    if isinstance(op, CreateMeasure):
        return f"Create the measure {op.name} in {op.table}."
    if isinstance(op, UpdateMeasure):
        return f"Change the measure {op.name}."
    if isinstance(op, RenameMeasure):
        return f"Rename the measure {op.name} to {op.new_name}."
    if isinstance(op, DeleteMeasure):
        return f"Delete the measure {op.name}."
    line = _model_family_line(op)
    return (line[0].upper() + line[1:] + ".") if line else "Change the model."


def _ref(table: str, column: str) -> str:
    return f"{table}[{column}]"


def _listed(items: Sequence[str], limit: int = 5) -> str:
    shown = list(items[:limit])
    more = len(items) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def _clauses(first: str, parts: list[str]) -> str:
    """``first`` then its settings: "hide X in the model, and format it as “0.0”"."""
    return ", and ".join([first, *parts]) if first else ", and ".join(parts)


_DATA_TYPE_WORDS = {
    "text": "text",
    "whole_number": "a whole number",
    "decimal_number": "a decimal number",
    "fixed_decimal": "a fixed decimal number",
    "date_time": "a date/time",
    "true_false": "true/false",
}


def _column_settings(op: Any, target: str) -> list[str]:
    """What ``set_column_properties`` or ``create_calculated_column`` sets, one clause each; the
    first names ``target``, the rest say "it"."""
    said: list[str] = []

    def it() -> str:
        return target if not said else "it"

    def value(name: str) -> Any:
        return getattr(op, name, None)

    hidden = value("hidden")
    if hidden is not None:
        said.append(f"{'hide' if hidden else 'show'} {it()} in the model")
    if value("format_string") is not None:
        said.append(f"format {it()} as “{op.format_string}”")
    if value("data_type") is not None:
        said.append(f"make {it()} {_DATA_TYPE_WORDS.get(op.data_type, op.data_type)}")
    sort_by = value("sort_by")
    if sort_by is not None:
        said.append(
            f"sort {it()} by {sort_by}" if sort_by else f"stop sorting {it()} by another column"
        )
    if value("data_category") is not None:
        said.append(f"categorize {it()} as {op.data_category.replace('_', ' ')}")
    summary = value("summarize_by")
    if summary is not None:
        words = summary.replace("_", " ")
        said.append(
            f"summarize {it()} by {words}" if words != "none" else f"do not summarize {it()}"
        )
    folder = value("folder")
    if folder is not None:
        said.append(
            f"put {it()} in the folder “{folder}”" if folder else f"take {it()} out of its folder"
        )
    key = value("key")
    if key is not None:
        said.append(f"make {it()} the table's key" if key else f"stop using {it()} as the key")
    description = value("description")
    if description is not None:
        said.append(
            f"describe {it()} as “{description}”"
            if description
            else f"remove the description of {it()}"
        )
    if isinstance(op, report_edit_model.SetColumnProperties) and op.expression is not None:
        said.append(f"change the formula of {it()}")
    return said


def _relationship(op: Any) -> str:
    return f"{_ref(op.from_table, op.from_column)} → {_ref(op.to_table, op.to_column)}"


def _relationship_settings(op: Any) -> list[str]:
    said: list[str] = []
    if op.cardinality is not None:
        said.append(op.cardinality.replace("_", "-"))
    if op.cross_filter is not None:
        said.append("filtering both ways" if op.cross_filter == "both" else "filtering one way")
    if op.active is not None:
        said.append("active" if op.active else "inactive")
    if op.assume_referential_integrity:
        said.append("assuming referential integrity")
    if op.security_filter == "both":
        said.append("security filter both ways")
    return said


def _model_family_line(op: Any) -> str:
    """The clause for one ``model`` family operation (no capital, no full stop), "" for an
    operation this does not know (then the generic line is said)."""
    m = report_edit_model
    if isinstance(op, m.CreateCalculatedColumn):
        target = _ref(op.table, op.name)
        return _clauses(f"create the calculated column {target}", _column_settings(op, "it"))
    if isinstance(op, m.SetColumnProperties):
        return _clauses("", _column_settings(op, _ref(op.table, op.name))) or (
            f"change the column {_ref(op.table, op.name)}"
        )
    if isinstance(op, m.RenameColumn):
        return f"rename the column {_ref(op.table, op.name)} to {op.new_name}"
    if isinstance(op, m.DeleteColumn):
        return f"delete the column {_ref(op.table, op.name)} from the model"
    if isinstance(op, m.CreateCalculatedTable):
        return f"create the calculated table {op.name}" + (", hidden" if op.hidden else "")
    if isinstance(op, m.SetTableProperties):
        said: list[str] = []
        if op.hidden is not None:
            said.append(f"{'hide' if op.hidden else 'show'} the table {op.table} in the model")
        if op.description is not None:
            said.append(
                f"describe the table {op.table} as “{op.description}”"
                if op.description
                else f"remove the description of the table {op.table}"
            )
        if op.expression is not None:
            said.append(f"change the formula of the table {op.table}")
        return ", and ".join(said) or f"change the table {op.table}"
    if isinstance(op, m.RenameTable):
        return f"rename the table {op.name} to {op.new_name}"
    if isinstance(op, m.DeleteTable):
        return f"delete the table {op.name} from the model"
    if isinstance(op, m.MarkDateTable):
        if op.column is None:
            return f"stop using {op.table} as the date table"
        return f"mark {op.table} as the date table on {_ref(op.table, op.column)}"
    if isinstance(op, m.CreateRelationship):
        details = []
        if op.cardinality != "many_to_one":
            details.append(op.cardinality.replace("_", "-"))
        if op.cross_filter == "both":
            details.append("filtering both ways")
        if not op.active:
            details.append("inactive")
        if op.assume_referential_integrity:
            details.append("assuming referential integrity")
        tail = f" ({', '.join(details)})" if details else ""
        return f"create the relationship {_relationship(op)}{tail}"
    if isinstance(op, m.UpdateRelationship):
        details = _relationship_settings(op)
        tail = f": {', '.join(details)}" if details else ""
        return f"change the relationship {_relationship(op)}{tail}"
    if isinstance(op, m.DeleteRelationship):
        return f"delete the relationship {_relationship(op)}"
    if isinstance(op, m.CreateHierarchy):
        levels = _listed([level.name or level.column for level in op.levels])
        return f"create the hierarchy {op.name} in {op.table} ({levels})"
    if isinstance(op, m.UpdateHierarchy):
        said = []
        if op.levels is not None:
            said.append("levels " + _listed([lvl.name or lvl.column for lvl in op.levels]))
        if op.new_name is not None:
            said.append(f"rename it to {op.new_name}")
        tail = f": {', and '.join(said)}" if said else ""
        return f"change the hierarchy {op.name} in {op.table}{tail}"
    if isinstance(op, m.DeleteHierarchy):
        return f"delete the hierarchy {op.name} from {op.table}"
    if isinstance(op, m.CreateFieldParameter | m.UpdateFieldParameter):
        fields = _listed([f.label or f.name for f in op.fields])
        verb = (
            "create the field parameter"
            if isinstance(op, m.CreateFieldParameter)
            else ("set the fields of the field parameter")
        )
        joiner = "with" if isinstance(op, m.CreateFieldParameter) else "to"
        return f"{verb} {op.name} {joiner} {fields}"
    if isinstance(op, m.CreateCalculationGroup):
        return f"create the calculation group {op.name} with {_listed([i.name for i in op.items])}"
    if isinstance(op, m.SetCalculationItem):
        said = []
        if op.expression is not None:
            said.append("its formula")
        if op.format_string_expression is not None:
            said.append("its format string")
        if op.ordinal is not None:
            said.append(f"its place ({op.ordinal})")
        line = f"set the calculation item {op.name} of {op.group}"
        if said:
            line += f": {', '.join(said)}"
        if op.new_name is not None:
            line += f", and rename it to {op.new_name}"
        return line
    if isinstance(op, m.DeleteCalculationItem):
        return f"delete the calculation item {op.name} from {op.group}"
    if isinstance(op, m.UpdateCalculationGroup):
        said = []
        if op.precedence is not None:
            said.append(f"precedence {op.precedence}")
        if op.description is not None:
            said.append("its description")
        tail = f": {', '.join(said)}" if said else ""
        return f"change the calculation group {op.name}{tail}"
    if isinstance(op, m.CreateRole):
        tables = _listed([f.table for f in op.filters])
        return f"create the role {op.name}" + (f" (filters on {tables})" if op.filters else "")
    if isinstance(op, m.UpdateRole):
        said = []
        if op.filters is not None:
            tables = _listed([f.table for f in op.filters])
            said.append(f"filters on {tables}" if op.filters else "no filters")
        if op.description is not None:
            said.append("its description")
        if op.new_name is not None:
            said.append(f"rename it to {op.new_name}")
        tail = f": {', '.join(said)}" if said else ""
        return f"change the role {op.name}{tail}"
    if isinstance(op, m.DeleteRole):
        return f"delete the role {op.name}"
    if isinstance(op, m.SetTableQuery):
        return f"change the Power Query query of the table {op.table} and reload its data"
    if isinstance(op, m.SetNamedExpression):
        return f"set the Power Query query {op.name}"
    if isinstance(op, m.DeleteNamedExpression):
        return f"delete the Power Query query {op.name}"
    if isinstance(op, m.SetQueryParameter):
        shown = f"“{op.value}”" if isinstance(op.value, str) else str(op.value).lower()
        return f"set the Power Query parameter {op.name} to {shown}"
    if isinstance(op, m.RefreshData):
        return f"refresh the data of {op.table}" if op.table else "refresh the data of every table"
    if isinstance(op, m.SetModelOptions):
        said = []
        if op.discourage_implicit_measures is not None:
            said.append(
                "discourage implicit measures"
                if op.discourage_implicit_measures
                else "allow implicit measures"
            )
        if op.culture is not None:
            said.append(f"set the model's culture to {op.culture}")
        if op.auto_date_time is not None:
            said.append(f"turn auto date/time {'on' if op.auto_date_time else 'off'}")
        return ", and ".join(said) or "change the model's settings"
    return ""


def _known_operations() -> set[str] | None:
    """The operation names the edit layer accepts, None when it does not list them."""
    classes = getattr(report_edits, "REPORT_OPERATIONS", None)
    if not classes:
        return None
    return {str(cls.model_fields["op"].default) for cls in classes}


def _parse_failure(error: ValueError, raw: Sequence[dict[str, Any]]) -> ImportedEditError:
    """The edit layer's plain-English refusal of a step, with the code the API gives it: an
    operation Studio does not have is ``edit_unsupported``, anything else ``edit_rejected``
    naming the step."""
    text = str(error)
    step = _STEP_ANYWHERE.match(text)
    number = int(step.group("step")) if step else None
    known = _known_operations()
    if known is not None:
        for item in raw:
            name = item.get("op") if isinstance(item, dict) else None
            if not isinstance(name, str) or name not in known:
                return ImportedEditError(
                    "edit_unsupported",
                    text,
                    422,
                    capability=name if isinstance(name, str) and name else "unknown",
                    reason="Studio does not have this kind of change.",
                    desktop_steps=[],
                )
    return ImportedEditError("edit_rejected", text, 422, step=number)


def _validation_error(error: ValidationError, raw: Sequence[dict[str, Any]]) -> ImportedEditError:
    """Plain words for the first problem pydantic found in an operation."""
    first = error.errors()[0]
    location = [part for part in first.get("loc", ()) if part != "operations"]
    position = next((i for i, part in enumerate(location) if isinstance(part, int)), None)
    step = None if position is None else int(location[position]) + 1
    if first.get("type") in ("union_tag_invalid", "union_tag_not_found"):
        index = (step or 1) - 1
        name = str(raw[index].get("op", "")) if index < len(raw) else ""
        return ImportedEditError(
            "edit_unsupported",
            (
                f"Studio cannot make this kind of change yet ({name})."
                if name
                else "Each change needs an 'op' that names what to do."
            ),
            422,
            capability=name or "unknown",
            reason="Studio does not have this kind of change.",
            desktop_steps=[],
        )
    # After the step number comes the operation's own name (the union's tag), then the field.
    after = [str(part) for part in location[(position or 0) + 1 :] if isinstance(part, str)]
    field_name = after[1] if len(after) > 1 else (after[0] if after else "")
    where = f"Step {step}: " if step is not None and len(raw) > 1 else ""
    what = f"the value for '{field_name}'" if field_name else "this change"
    return ImportedEditError(
        "edit_rejected",
        f"{where}{what} is not valid ({first.get('msg', 'it does not fit')}).",
        422,
        step=step,
    )
