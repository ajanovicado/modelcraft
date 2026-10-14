"""Apply edits to an imported report: one locked batch per request, one revision per batch.

The order follows the 2026-09-28 design review (P2, P3, P5, P8, P9, P10, P20, P21) and the
Desktop contract measured on 2.157:

1. Take the report's edit lock (chat, the Format pane, "Open in Power BI" and delivery all
   wait on it) and check there is room for the copies a batch makes.
2. A repeated idempotency key gets the recorded receipt back; a batch that never finished
   is recovered first.
3. If Power BI Desktop has the working copy open: refuse while a dialog is showing; ask
   before touching unsaved work (``UnsavedDesktopWork``), and save it only when the caller
   said so (``desktop_work="save"``); then close Desktop and wait for it to exit. Studio
   never saves the user's work silently and never kills Desktop.
4. Adopt what Desktop saved as a revision of its own, after the save and the close, so the
   bytes adopted are the ones Desktop wrote. A copy that differs from the placed revision
   only by Desktop's normalization is not a change and makes no revision.
5. Check the caller's ``expected_head`` against the head.
6. Build the new document from the head, dry-run the operations on a fresh model view,
   validate, and write a staging file. Nothing has moved yet.
7. Commit ONE revision, put the head into the working copy, reopen Desktop if it was open.
8. Re-derive Studio's view of the report and prune old versions.

A batch with model operations is one transaction across both layers: Desktop applies them to
the current head and saves, the saved file is checked (Desktop can silently drop what Studio
wrote), the report-layer half of the operations runs on that saved file, and only then does the
head move, once. Any failure before that leaves the head where it was. Measure-only batches go
through ``pbi measure``; a batch with any other model operation (columns, tables,
relationships, hierarchies, field parameters, calculation groups, roles, Power Query, model
settings) goes through Studio's TOM runner (``DesktopController.apply_model_operations``), and
the saved file is then held to a model facts diff (``report_edit_model.saved_model_problems``)
besides the measure check. A data refresh that fails undoes the batch and, when a new query asked
for it, comes back as a question (keep the new query without reloading the data).

What the edit layer checks an edit against (design review P5, P13, P22):

- The model is the report's tables and measures plus the model's own metadata when a
  ``model_facts`` feed is given: the data types (a filter on a whole-number column writes
  ``2019L``, not ``'2019'``) and the DAX of calculated columns and tables (a delete or rename
  sees the field parameter or calculated column that uses the measure). Without the facts the
  edit goes ahead on the tables and measures alone and the receipt says so.
- The result is held only to the problems the edit introduced: ``capture_baseline`` of the head
  is taken before anything is applied (twice for a model batch: on the head before Desktop is
  opened, and again on the file Desktop saved), so a defect the source report already had never
  blocks an edit while a new one does; a visual or page the edit copied is held to its source's.
- The receipt carries what the edit layer reported besides its summaries (what it protected,
  what a whole-visual write replaced, where each change shows, the model changes it only
  suggests). The journal records them with the batch, so a replay returns them.
- Power BI Desktop calls that can hang or vanish (the ``pbi`` tool times out, a dialog opens)
  come back as ``EditRejected``, or as ``DesktopDialog`` with the dialog's title.

Every step is journaled (edit_journal) before its side effect, so a crash is recovered at the
next start. After the head has moved nothing may turn the answer into a failure, because a
retry would apply the edit twice: later problems become warnings and a "partial" status.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import shutil
import subprocess
import uuid
import zipfile
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from ai_powerbi_analyst.integrity import sha256_file

from . import report_delivery, report_edit_model, report_import, report_revisions
from .desktop_session import (
    DesktopInstance,
    DesktopRefreshFailed,
    DesktopSessionError,
    UnsavedDesktopWork,
    unsupported_measure_change,
)
from .edit_journal import BatchRecord, EditJournal, RecoveryReport, recover
from .pbir_document import DocumentError, PbirDocument
from .pbix_package import PackageError, PbixPackage
from .report_edit_context import Choice
from .report_edit_model import (
    MODEL_FAMILY_OPERATIONS,
    DeleteColumn,
    DeleteTable,
    RenameColumn,
    RenameTable,
    SetTableQuery,
)
from .report_edits import (
    Baseline,
    CreateMeasure,
    DeleteMeasure,
    EditOutcome,
    ModelInfo,
    NeedsChoice,
    NeedsConfirmation,
    Protected,
    RenameMeasure,
    UpdateMeasure,
    VisibleIn,
    apply_operations,
    apply_report_operations,
    capture_baseline,
    validate_document,
)
from .report_import import ImportedReport, extract_argv
from .report_revisions import (
    CommitAuthor,
    HistoryEntry,
    Revision,
    RevisionConflict,
    RevisionError,
    RevisionStore,
    WorkingCopyState,
    _readable_zip,
)
from .state import StateConflictError

LOGGER = logging.getLogger(__name__)

DesktopWork = Literal["ask", "save"]
Rederive = Callable[[ImportedReport, Path, str, bool], ImportedReport]
ReadMeasures = Callable[[Path], list[dict[str, Any]]]
Progress = Callable[[str], None]
DiskFree = Callable[[Path], int]
# What the model's metadata adds to the report's own tables and measures: the types payload
# (``pbix_extract.types``: ``{"columns": [...], "measures": [...]}``, None when it could not be
# read) and the DAX of calculated columns and tables (``"Table[Column]"`` or ``"Table"`` -> DAX,
# None when unknown). ``ModelInfo.from_report`` takes both.
ModelFacts = tuple[Mapping[str, Any] | None, Mapping[str, str] | None]
ReadModelFacts = Callable[[ImportedReport], ModelFacts | None]
# The whole model's metadata as a saved file has it (``pbix_extract --facts``): tables, columns,
# relationships, hierarchies, roles, calculation groups, field parameters, partitions, named
# expressions and settings, each section optional. A model batch's saved file is held to it.
ReadSavedFacts = Callable[[Path], Mapping[str, Any]]

# Working files a batch makes at once: the staged file, Desktop's saved copy and the revision
# blob, each about the size of the PBIX.
SPACE_FACTOR_STATE = 3
# A model change lets Desktop load the data model, which needs memory-mapped scratch space
# on the user's profile drive.
SPACE_FACTOR_PROFILE = 2
# What a call into Power BI Desktop can raise besides its own DesktopSessionError: the real
# port shells out to pbi.exe with a timeout, and a hung or vanished bridge is not a
# Desktop error. Each becomes a plain refusal instead of a raw failure.
_PORT_FAILURES = (DesktopSessionError, OSError, subprocess.SubprocessError, KeyError)
# The receipt's words when the model's data types or calculated DAX could not be read.
NO_TYPES_WARNING = "Studio could not read the model's data types; values were treated as text."
NO_CALCULATED_WARNING = (
    "Studio could not read the model's calculated columns and tables, so it cannot tell whether "
    "they use this measure. Check them in Power BI Desktop."
)


class EditRejected(ValueError):
    """The edit cannot be applied as asked; the report is as it was.

    ``status`` is what the reply says instead of "updated".
    """

    status = "failed"


class DesktopDialog(EditRejected):
    """The edit was refused because a dialog is open in Power BI Desktop.

    ``dialog`` is its title, so a caller need not parse the message (the service falls back to
    reading the message for an editor that does not set it).
    """

    def __init__(self, message: str, dialog: str) -> None:
        super().__init__(message)
        self.dialog = dialog


class RestoreNeedsConfirmation(EditRejected):
    """Restoring would leave later changes out of the report; the user must agree first."""

    def __init__(self, later: list[Revision]) -> None:
        super().__init__(
            f"Restoring this version removes {len(later)} later change"
            f"{'' if len(later) == 1 else 's'} from the report (they stay in the history)."
        )
        self.later = later


class DesktopPort(Protocol):
    """Power BI Desktop as this module uses it; ``desktop_session.DesktopController`` is the
    real one, and tests use an in-memory fake."""

    def find(self, path: Path) -> DesktopInstance | None: ...

    def status(self, instance: DesktopInstance) -> dict[str, Any]: ...

    def open(self, path: Path, *, normalize: bool = True) -> DesktopInstance: ...

    # The real controller returns a SaveResult; the pipeline only needs the save to have
    # happened (it re-reads the flag and the file), so any return value is accepted.
    def save(self, instance: DesktopInstance) -> object: ...

    def close(self, instance: DesktopInstance) -> None: ...

    def apply_measure_operations(
        self, instance: DesktopInstance, operations: list[Any], tables: dict[str, str]
    ) -> list[str]: ...

    def screenshot(self, instance: DesktopInstance, page_id: str, output: Path) -> Path: ...

    def blocking_windows(self, instance: DesktopInstance) -> list[str]:
        """Titles of modal dialogs, or the main window when it is disabled; [] if none."""
        ...

    def version(self) -> str | None:
        """Desktop's product version."""
        ...


class TestsCheck(Protocol):
    """One batch's report-tests check (docs/imported-report-tests.md, section 8): begun before
    Power BI Desktop is touched, finished on the model Desktop saved when the batch changes
    more than measures (``needs_saved``). Either step raises ``report_tests.ReportTestsBroken``
    (a ``NeedsConfirmation``) unless the user confirmed; nothing is written then."""

    needs_saved: bool

    def after_save(self, saved: ImportedReport) -> None: ...


class TestsGate(Protocol):
    """Where a batch's report-tests check comes from (the service builds it: it knows the
    tests, their last run and the data to run them on)."""

    def begin(
        self, report: ImportedReport, head: Revision, operations: list[Any], *, confirmed: bool
    ) -> TestsCheck | None:
        """The check of a batch whose model ``operations`` may change numbers (None: the
        report has no tests). Runs step 1 at once and raises when it breaks a test."""
        ...


# The batch-wide confirmation that lets a model edit go ahead although it breaks report tests.
TESTS_CONFIRMATION = "report_tests_broken"


@dataclass
class EditReceipt:
    revision: Revision
    report: ImportedReport
    summaries: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    adopted: Revision | None = None
    desktop: str = "closed"  # "closed" | "reopened" | "open"
    # "updated", or "partial" when the edit is recorded but something after it (the working
    # copy, Studio's refreshed view) did not complete. A failure raises EditRejected.
    status: Literal["updated", "partial"] = "updated"
    replayed: bool = False
    batch_id: str = ""
    model_changed: bool = False
    # What the edit layer reported beyond its summaries: formatting it left alone, what a
    # whole-visual write replaced, where each change shows, and model changes it only
    # suggests (colors inside a measure's DAX): those are shown to the user, never applied.
    protected: list[Protected] = field(default_factory=list[Protected])
    replaced: list[str] = field(default_factory=list[str])
    visible_in: list[VisibleIn] = field(default_factory=list[VisibleIn])
    pending_model_operations: list[Any] = field(default_factory=list[Any])
    # The report-tests check this batch ran (None: no model change, or no tests); the service
    # records its results and names the tests the user let it break (after the commit).
    tests: TestsCheck | None = None
    # The model operations the batch sent to Power BI (the edit layer's, dependents included).
    model_operations: list[Any] = field(default_factory=list[Any])


@dataclass
class _Batch:
    id: str
    store: RevisionStore
    journal: EditJournal
    kind: str
    report: ImportedReport
    progress: Progress | None
    warnings: list[str] = field(default_factory=list)
    was_open: bool = False
    desktop_closed: bool = False  # Studio closed Desktop, so a failure must put it back
    model_touched: bool = False  # Desktop was opened on the head for measure operations
    adopted: Revision | None = None
    version: str | None = None
    committed: bool = False
    journal_failed: bool = False  # a journal write failed after the head moved
    staged: list[Path] = field(default_factory=list)
    outcome: EditOutcome | None = None  # what the edit layer reported for this batch
    facts: ModelFacts | None = None  # the model facts of the head this batch works on
    # The model view the dry run left behind: the head's model with the batch's operations applied
    # by the edit layer (new, renamed and deleted columns and tables included).
    planned_model: ModelInfo | None = None
    # The report-tests check of a model batch, and whether the user let it break tests.
    tests: TestsCheck | None = None
    tests_confirmed: bool = False
    tests_pending: bool = False  # the first dry run could not run: begin after adoption
    # The saved file's model facts as ``_check_saved_facts`` read them (the tests' second step
    # runs on the same model instead of reading it again).
    saved_facts: Mapping[str, Any] | None = None


def _port_message(error: BaseException) -> str:
    """Words for a failed call into Power BI Desktop: the port's own, or a generic one for
    a timeout or a crash that has none."""
    if isinstance(error, DesktopSessionError) and str(error):
        return str(error)
    return (
        "Studio could not get an answer from Power BI Desktop. Close the report in Power BI "
        "Desktop (choose Don't save if it asks), then try again."
    )


def _refusal(error: BaseException) -> EditRejected:
    """The refusal for a failed call into Power BI Desktop, typed (``DesktopDialog``) when the
    port says a dialog is open."""
    dialog = getattr(error, "dialog", None)
    if isinstance(dialog, str) and dialog:
        return DesktopDialog(_port_message(error), dialog)
    return EditRejected(_port_message(error))


def _refusal_saying(message: str, *causes: BaseException) -> EditRejected:
    """A refusal with the editor's own words that stays typed when one of ``causes`` (the port's
    failures behind it) was a dialog, so the caller still tells the user to finish that dialog
    instead of showing a generic failure."""
    for cause in causes:
        dialog = getattr(cause, "dialog", None)
        if isinstance(dialog, str) and dialog:
            return DesktopDialog(message, dialog)
    return EditRejected(message)


def _plain(value: Any) -> Any:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _has_data_model(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return "DataModel" in archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def _model_fingerprint(path: Path) -> tuple[int, int] | None:
    """(size, CRC) of the DataModel part, straight from the zip directory; None if absent."""
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("DataModel")
            return info.file_size, info.CRC
    except (KeyError, zipfile.BadZipFile, OSError):
        return None


def _data_model_size(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        try:
            return archive.getinfo("DataModel").file_size
        except KeyError:
            return 0


def read_measures_subprocess(path: Path) -> list[dict[str, Any]]:
    """The model's measures as saved in ``path``, read in a child process like the importer
    does (decoding the model needs a lot of memory and must not take the sidecar down)."""
    try:
        process = subprocess.run(  # noqa: S603 - fixed local script, Studio's own file, argv only
            extract_argv(path, "--measures"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            check=False,
        )
        found = json.loads(process.stdout) if process.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        found = None
    if not isinstance(found, list):
        raise EditRejected(
            "Studio could not read the measures back from the file Power BI saved, so the "
            "change was not kept."
        )
    return [item for item in found if isinstance(item, dict)]


def read_model_facts_subprocess(path: Path) -> dict[str, Any]:
    """The saved file's model facts (``pbix_extract --facts``), read in a child process like the
    measures. {} when they cannot be read: every section is then unknown, and the transaction
    says which checks it left to Power BI Desktop instead of refusing."""
    try:
        process = subprocess.run(  # noqa: S603 - fixed local script, Studio's own file, argv only
            extract_argv(path, "--facts"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            check=False,
        )
        found = json.loads(process.stdout) if process.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        found = None
    return found if isinstance(found, dict) else {}


# Operations whose result depends on the data types of the model's fields: the filters write
# literals typed by the column, fields pick an aggregation from the type and the default
# summarization. Add an operation here when its handler starts to read the types.
_TYPED_OPERATIONS = frozenset(
    {
        "set_visual_filter",
        "set_page_filter",
        "set_report_filter",
        "set_fields",
        "add_visual",
        "change_visual_type",
    }
)


def _needs_types(op: Any) -> bool:
    return getattr(op, "op", "") in _TYPED_OPERATIONS


# Operations that never look at the model beyond the report's own tables and measures: no type,
# no summarization, no calculated DAX. A batch made only of these does not decode the model
# (a child process that can take a minute on a large one) just to move a visual. Everything
# else, including an operation added later, reads the facts.
_FACT_FREE_OPERATIONS = frozenset(
    {
        "set_position",
        "arrange",
        "align_visuals",
        "distribute_visuals",
        "swap_visuals",
        "add_shape",
        "add_textbox",
        "set_visual_hidden",
        "delete_visual",
        "duplicate_visual",
        "rename_page",
        "add_page",
        "duplicate_page",
        "delete_page",
        "move_page",
        "set_page_size",
        "set_page_hidden",
    }
)


def _needs_facts(op: Any) -> bool:
    if _needs_types(op):
        return True
    return getattr(op, "op", "") not in _FACT_FREE_OPERATIONS


def _measure_map(model: ModelInfo) -> dict[str, str]:
    return {name.casefold(): table.casefold() for name, table in model.measures.items()}


def _expected_measures(pre: ModelInfo, operations: list[Any]) -> dict[str, str]:
    """The measure map (name -> home table, lower-cased) the operations must leave behind,
    worked out from the operations alone. It is what Desktop's saved file is held to, so it
    must not come from the report-layer handlers' own bookkeeping."""
    homes = {name.casefold(): table.casefold() for name, table in pre.measures.items()}
    for op in operations:
        if isinstance(op, CreateMeasure):
            homes[op.name.casefold()] = (pre.table(op.table) or op.table).casefold()
        elif isinstance(op, RenameMeasure):
            home = homes.pop(op.name.casefold(), None)
            if home is not None:
                homes[op.new_name.casefold()] = home
        elif isinstance(op, DeleteMeasure):
            homes.pop(op.name.casefold(), None)
        elif isinstance(op, RenameTable):
            # A table's measures move with it.
            homes = {
                m: op.new_name.casefold() if t == op.name.casefold() else t
                for m, t in homes.items()
            }
        elif isinstance(op, DeleteTable):
            homes = {m: t for m, t in homes.items() if t != op.name.casefold()}
    return homes


def _model_before(pre: ModelInfo, saved: ModelInfo, operations: list[Any]) -> ModelInfo:
    """The model as the report-layer handlers expect it before the measure operations ran.

    The handlers in report_edits check each measure operation against the model and then
    apply it to their model view, so they need the state *before* the change: a rename needs
    the old name to exist, a create needs the new name free. Desktop's saved file has the
    state *after*, so it is wound back by the batch's own operations. The wound-back model
    is never trusted: after the handlers ran, the model they arrive at must equal the saved
    file's (checked by the caller), which is what proves Desktop kept every operation.
    """
    homes = dict(pre.measures)
    for op in operations:
        if isinstance(op, CreateMeasure):
            homes[op.name] = pre.table(op.table) or op.table
    measures = dict(saved.measures)

    def drop(name: str) -> None:
        for existing in [m for m in measures if m.casefold() == name.casefold()]:
            del measures[existing]

    def home(name: str) -> str:
        return next((t for m, t in homes.items() if m.casefold() == name.casefold()), "")

    for op in reversed(operations):
        if isinstance(op, CreateMeasure):
            drop(op.name)
        elif isinstance(op, RenameMeasure):
            table = home(op.name)
            drop(op.new_name)
            measures[op.name] = table
        elif isinstance(op, DeleteMeasure):
            measures[op.name] = home(op.name)
        elif isinstance(op, RenameTable):
            for name, table in list(measures.items()):
                if table.casefold() == op.new_name.casefold():
                    measures[name] = op.name
        elif isinstance(op, DeleteTable):
            for name, table in pre.measures.items():
                if table.casefold() == op.name.casefold():
                    measures[name] = table
    # ``pre`` carries what the report's own tables and measures do not (data types, the DAX of
    # measures and calculated columns and tables); only the measure map is wound back.
    return pre.with_measures(measures)


def _model_for_report_half(before: ModelInfo, after: ModelInfo) -> ModelInfo:
    """The model the report-layer half of a batch resolves names against.

    ``apply_report_operations`` skips the measure operations, so nothing in that pass renames,
    creates or deletes a measure in its model view: it is given the measures of both sides. An
    operation that stage one accepted may name a measure under the name it had when that step
    ran (a card of [Sales] added before [Sales] is renamed in the same batch), and the saved
    file only has the new name. The check that decides whether the result is valid runs against
    ``after`` alone (``_check_model_layer``). Columns are merged the same way, for a column or
    table the batch renames."""
    merged = after.with_measures({**before.measures, **after.measures})
    for table, columns in before.columns.items():
        merged.columns.setdefault(table, {})
        for column, dtype in columns.items():
            merged.columns[table].setdefault(column, dtype)
    return merged


def _renamed_away(document: PbirDocument, family: list[Any], planned: ModelInfo | None) -> set[str]:
    """Report fields that still name a column or table the batch renamed or deleted (a rewrite
    that missed one spelling), unless the batch's own model has that name again."""
    gone_tables = {op.name.casefold() for op in family if isinstance(op, RenameTable | DeleteTable)}
    gone_columns = {
        (op.table.casefold(), op.name.casefold())
        for op in family
        if isinstance(op, RenameColumn | DeleteColumn)
    }
    if not gone_tables and not gone_columns:
        return set()

    def exists(table: str, column: str | None) -> bool:
        if planned is None:
            return False
        home = planned.table(table)
        if home is None:
            return False
        return (
            column is None
            or any(c.casefold() == column.casefold() for c in planned.columns.get(home, {}))
            or (planned.measure_table(column) or "").casefold() == home.casefold()
        )

    found: set[str] = set()
    for kind, table, name in report_edit_model.field_references(document):
        key = (table.casefold(), name.casefold())
        table_gone = table.casefold() in gone_tables and not exists(table, None)
        column_gone = kind == "Column" and key in gone_columns and not exists(table, name)
        if table_gone or column_gone:
            found.add(f"{table}[{name}]")
    return found


def _refresh_answer(operations: list[Any], error: DesktopRefreshFailed) -> Exception:
    """What the caller gets when the data refresh of a model batch failed and the batch was undone:
    the question "keep the new query without reloading the data?" when a new query asked for one,
    else a plain refusal."""
    reason = " ".join(error.reason.split())[:300] or "no details"
    if report_edit_model.refresh_policy(operations) != "ask":
        return EditRejected(
            f"Power BI could not reload the data ({reason}), so the change was not kept. The "
            "report was not changed."
        )
    step = next(
        index
        for index, op in enumerate(operations, start=1)
        if isinstance(op, SetTableQuery) and op.if_refresh_fails == "ask"
    )
    question = NeedsChoice(
        f"Power BI could not reload the data with the new query ({reason}). Keep the new query "
        "anyway? The table keeps the rows it had until a refresh succeeds. Nothing was changed "
        "yet.",
        [Choice("Keep the new query without reloading the data", {"if_refresh_fails": "keep"})],
    )
    question.step = step
    return question


def _viewed_at(report: ImportedReport, store: RevisionStore, head: Revision) -> ImportedReport:
    """``report`` pointing at ``head``'s file, so anything derived from "the report's model"
    (the types sidecar is keyed by the file's DataModel) reads the file the batch works on and
    not the one the view was last built from (they differ after Desktop's save was adopted)."""
    if head.blob == report.source_hash:
        return report.model_copy(update={"revision_path": None, "revision_hash": None})
    update = {"revision_path": str(store.path(head)), "revision_hash": head.blob}
    return report.model_copy(update=update)


def _receipt_record(reported: EditOutcome) -> dict[str, Any]:
    """What the journal keeps of the edit layer's answer, so a replay returns it."""
    return {
        "protected": [dataclasses.asdict(item) for item in reported.protected],
        "replaced": [str(item) for item in reported.replaced],
        "visible_in": [dataclasses.asdict(item) for item in reported.visible_in],
        "pending_model_operations": [_plain(op) for op in reported.pending_model_operations],
    }


def _recorded_items(recorded: Mapping[str, Any]) -> dict[str, Any]:
    """The reverse of ``_receipt_record``. A journal line from before those fields existed, or
    one with an entry of another shape, yields fewer items and never fails."""

    def rows(key: str) -> list[dict[str, Any]]:
        found = recorded.get(key)
        return [item for item in found if isinstance(item, dict)] if isinstance(found, list) else []

    def where(value: object) -> Literal["studio", "powerbi_only", "covered_by_background"]:
        if value == "powerbi_only":
            return "powerbi_only"
        if value == "covered_by_background":
            return "covered_by_background"
        return "studio"

    replaced = recorded.get("replaced")
    return {
        "protected": [
            Protected(str(item.get("visual", "")), str(item.get("what", "")))
            for item in rows("protected")
        ],
        "replaced": [str(item) for item in replaced] if isinstance(replaced, list) else [],
        "visible_in": [
            VisibleIn(
                str(item.get("op", "")), where(item.get("where")), str(item.get("detail", ""))
            )
            for item in rows("visible_in")
        ],
        "pending_model_operations": rows("pending_model_operations"),
    }


def _measure_references(document: PbirDocument) -> set[tuple[str, str]]:
    """(table, measure) pairs the visuals refer to by table name, lower-cased."""
    found: set[tuple[str, str]] = set()

    def walk(node: Any, aliases: dict[str, str]) -> None:
        if isinstance(node, dict):
            # Filters name a table through a From alias ({Source: "m"}), not only through
            # {Entity: "# Measures"}; a query's From list scopes the aliases below it.
            sources = node.get("From")
            if isinstance(sources, list):
                aliases = {
                    **aliases,
                    **{
                        str(item["Name"]): str(item["Entity"])
                        for item in sources
                        if isinstance(item, dict)
                        and isinstance(item.get("Name"), str)
                        and isinstance(item.get("Entity"), str)
                    },
                }
            measure = node.get("Measure")
            if isinstance(measure, dict):
                expression = measure.get("Expression")
                source = expression.get("SourceRef", {}) if isinstance(expression, dict) else {}
                entity = source.get("Entity") if isinstance(source, dict) else None
                alias = source.get("Source") if isinstance(source, dict) else None
                if not isinstance(entity, str) and isinstance(alias, str):
                    entity = aliases.get(alias)
                name = measure.get("Property")
                if isinstance(entity, str) and isinstance(name, str):
                    found.add((entity.casefold(), name.casefold()))
            for child in node.values():
                walk(child, aliases)
        elif isinstance(node, list):
            for child in node:
                walk(child, aliases)

    for _, _, visual in document.all_visuals():
        walk(visual, {})
    return found


class ReportEditor:
    def __init__(
        self,
        root: Path,
        rederive: Rederive,
        controller: Callable[[], DesktopPort] | None = None,
        *,
        read_measures: ReadMeasures | None = None,
        disk_free: DiskFree | None = None,
        profile_dir: Path | None = None,
        reopen_desktop: bool = True,
        checkpoint: Callable[[str], None] | None = None,
        model_facts: ReadModelFacts | None = None,
        read_model_facts: ReadSavedFacts | None = None,
        tests_gate: TestsGate | None = None,
    ) -> None:
        self.root = root
        # Report tests hold model and measure edits to the numbers the user pinned (None: none).
        self.tests_gate = tests_gate
        self.rederive = rederive
        self._controller = controller
        self._read_measures = read_measures or read_measures_subprocess
        # What a model batch's saved file is held to beyond its measures (tolerates missing keys).
        self._read_saved_facts = read_model_facts or read_model_facts_subprocess
        # Where the data types and the calculated DAX of the report's model come from (the
        # service's types sidecar). Without one a model is the report's own tables and measures.
        self._model_facts = model_facts
        self._disk_free = disk_free or (lambda path: shutil.disk_usage(path).free)
        self._profile_dir = profile_dir
        self._reopen = reopen_desktop
        # Called after every journal line; tests raise from it to simulate a crash there.
        self._checkpoint = checkpoint
        # Measures read for the normalization check, by (file, size, mtime): a revision file
        # never changes, and reading a model costs seconds.
        self._model_cache: dict[tuple[str, int, int], list[dict[str, Any]] | None] = {}

    # --- plumbing ------------------------------------------------------------------------

    def _equivalent(self, a: Path, b: Path) -> bool:
        """True when ``b`` is only Desktop's normalization of ``a``: the same report layer
        (report_revisions.semantic_equal) and, when the DataModel bytes differ, the same
        measures. pbix_diff leaves model bytes out of its answer because Desktop rewrites
        them on every save, but a measure the user added or edited in Desktop changes
        nothing else in the file, and calling that normalization would overwrite it."""
        if not report_revisions.semantic_equal(a, b):
            return False
        if _model_fingerprint(a) == _model_fingerprint(b):
            return True
        first, second = self._measures_of(a), self._measures_of(b)
        # Unreadable is "different": a revision too many is safe, a lost measure is not.
        return first is not None and second is not None and first == second

    def _measures_of(self, path: Path) -> list[dict[str, Any]] | None:
        try:
            stat = path.stat()
            key = (str(path), stat.st_size, stat.st_mtime_ns)
        except OSError:
            return None
        if key not in self._model_cache:
            try:
                rows = self._read_measures(path)
            except (EditRejected, OSError, ValueError):
                self._model_cache[key] = None
            else:
                self._model_cache[key] = sorted(
                    (
                        {
                            "table": str(row.get("TableName", "")),
                            "name": str(row.get("Name", "")),
                            "expression": " ".join(str(row.get("Expression", "")).split()),
                            "format": str(row.get("FormatString") or ""),
                        }
                        for row in rows
                    ),
                    key=lambda row: (row["table"], row["name"]),
                )
        return self._model_cache[key]

    # --- the model the edit layer checks against ---------------------------------------------

    def _read_facts(self, report: ImportedReport, operations: list[Any]) -> ModelFacts | None:
        """The model facts of ``report``'s own file, None when there is no source for them, when
        none of ``operations`` reads them, or when reading fails: an edit goes ahead with the
        report's tables and measures alone (and warns when an operation needed them)."""
        if self._model_facts is None or not any(_needs_facts(op) for op in operations):
            return None
        try:
            return self._model_facts(report)
        except Exception as error:  # noqa: BLE001 - facts sharpen the checks; they never block
            LOGGER.warning("Could not read the model facts of report %s: %s", report.id, error)
            return None

    @staticmethod
    def _model_info(
        report: ImportedReport, measures: list[dict[str, Any]], facts: ModelFacts | None
    ) -> ModelInfo:
        """The report's tables and ``measures`` plus the data types and the calculated DAX, and the
        model's structure (relationships, hierarchies, roles...) when the types payload has it."""
        types, calculated = facts if facts is not None else (None, None)
        info = ModelInfo.from_report(report.tables, measures, types, calculated=calculated)
        report_edit_model.attach_structure(info, types)
        return info

    def _facts_warnings(
        self, facts: ModelFacts | None, operations: list[Any], *, has_model: bool
    ) -> list[str]:
        """What the receipt says when the model facts could not be read. Silent without a
        source for them (nothing was asked) and for a report with no model of its own."""
        if self._model_facts is None or not has_model:
            return []
        types, calculated = facts if facts is not None else (None, None)
        found: list[str] = []
        if not types and any(_needs_types(op) for op in operations):
            found.append(NO_TYPES_WARNING)
        if calculated is None and any(
            isinstance(op, (UpdateMeasure, RenameMeasure, DeleteMeasure)) for op in operations
        ):
            found.append(NO_CALCULATED_WARNING)
        return found

    def store(self, report: ImportedReport) -> RevisionStore:
        original = Path(report.archived_path)
        return RevisionStore(original.parent, original, report.source_hash)

    def controller(self) -> DesktopPort:
        if self._controller is None:
            raise EditRejected("Power BI Desktop is needed for this change but is not set up")
        return self._controller()

    def _find(self, store: RevisionStore) -> DesktopInstance | None:
        """The Desktop instance that has the working copy open; None when there is none.

        "Cannot tell" (the bridge timed out, or two windows hold the file) is not "not
        open": treating it as closed would skip the dialog and unsaved-work checks and
        write under a Desktop that may hold the user's work, so it is refused instead.
        """
        if self._controller is None:
            return None
        try:
            return self.controller().find(store.working_copy)
        except _PORT_FAILURES as error:
            raise _refusal(error) from error

    def _version(self) -> str | None:
        # Only recorded with the row, so a failure here is not worth refusing an edit for.
        if self._controller is None:
            return None
        try:
            return self.controller().version()
        except _PORT_FAILURES:
            return None

    @staticmethod
    def _say(batch: _Batch, text: str) -> None:
        if batch.progress is not None:
            batch.progress(text)

    def _note(self, batch: _Batch, state: str, **data: Any) -> None:
        """Journal a step. Before the head moves a journal failure stops the batch (nothing
        has happened yet); afterwards it only costs the record, because raising would make
        the caller retry an edit that is already in the history."""
        try:
            batch.journal.append(batch.id, state, **data)
        except OSError:
            if not batch.committed:
                raise
            batch.journal_failed = True
        if self._checkpoint is not None:
            self._checkpoint(state)

    def _check_space(self, store: RevisionStore) -> None:
        """P20: room for the staged file, Desktop's save and the blob, before Desktop is touched."""
        size = store.path(store.head()).stat().st_size
        needed = size * SPACE_FACTOR_STATE
        if self._disk_free(store.root) < needed:
            raise EditRejected(
                "There is not enough free disk space to change this report safely. Free about "
                f"{needed // 1024**2 + 1} MB on the drive that holds Studio's data, then try again."
            )
        model_size = _data_model_size(store.path(store.head()))
        profile = self._profile_dir or Path(os.environ.get("USERPROFILE") or Path.home())
        if model_size and self._disk_free(profile) < model_size * SPACE_FACTOR_PROFILE:
            raise EditRejected(
                "There is not enough free disk space for Power BI Desktop to open this report. "
                f"Free about {model_size * SPACE_FACTOR_PROFILE // 1024**2 + 1} MB on the "
                "drive that holds your user profile, then try again."
            )

    # --- the Desktop steps shared by every batch ---------------------------------------------

    def _begin(
        self,
        report: ImportedReport,
        store: RevisionStore,
        kind: str,
        key: str,
        operations: list[str],
        progress: Progress | None,
    ) -> _Batch:
        journal = EditJournal(store.journal_path)
        batch = _Batch(uuid.uuid4().hex, store, journal, kind, report, progress)
        self._note(
            batch,
            "planned",
            key=key,
            kind=kind,
            parent=store.head().id,
            ops=operations,
            desktop_open=self._find(store) is not None,
        )
        return batch

    def _gate(self, batch: _Batch, desktop_work: DesktopWork) -> None:
        """Steps 3(a)-(c): dialogs, unsaved work, then close Desktop and wait for it to exit."""
        store = batch.store
        instance = self._find(store)
        if instance is None:
            return
        port = self.controller()
        batch.was_open = True
        batch.version = self._version()
        try:
            blocking = port.blocking_windows(instance)
            if blocking:
                raise DesktopDialog(
                    f"Power BI Desktop is showing a dialog ({blocking[0]}). Finish it in Power "
                    "BI Desktop, then try again. Studio has not changed anything.",
                    blocking[0],
                )
            dirty = bool(port.status(instance).get("hasUnsavedChanges"))
            before: str | None = None
            verified = True
            if dirty:
                if desktop_work != "save":
                    raise UnsavedDesktopWork(
                        "Power BI Desktop has unsaved changes to this report. Save them into "
                        "the history and continue, or cancel and finish in Power BI Desktop."
                    )
                self._say(batch, "Saving your Power BI Desktop work")
                before = self._working_hash(store)
                self._note(batch, "desktop_saved")
                port.save(instance)
                if port.status(instance).get("hasUnsavedChanges"):
                    raise EditRejected(
                        "Power BI Desktop still shows unsaved changes after saving. Save the "
                        "report in Power BI Desktop, then try again."
                    )
                verified = self._verify_saved(store, before, final=False)
            self._say(batch, "Closing Power BI Desktop")
            port.close(instance)
            batch.desktop_closed = True
            self._note(batch, "desktop_closed")
            if dirty and not verified:
                self._verify_saved(store, before, final=True)
        except UnsavedDesktopWork:
            raise
        except _PORT_FAILURES as error:
            raise _refusal(error) from error

    @staticmethod
    def _working_hash(store: RevisionStore) -> str | None:
        try:
            return sha256_file(store.working_copy)
        except OSError:
            placed = store.placed()
            return placed[1] if placed else None

    @staticmethod
    def _verify_saved(store: RevisionStore, before: str | None, *, final: bool) -> bool:
        """The Desktop save produced a new, complete report. False means not readable yet
        (Desktop still holds the file); ``final`` after the close makes that an error."""
        working = store.working_copy
        try:
            digest = sha256_file(working)
        except PermissionError as error:
            if final:
                raise EditRejected(
                    "Studio could not read the report Power BI Desktop saved. Close it in "
                    "Power BI Desktop and try again."
                ) from error
            return False
        if before is not None and digest == before:
            raise EditRejected(
                "Power BI Desktop reported that it saved the report, but the file did not "
                "change. Save it again in Power BI Desktop, then try again."
            )
        if not _readable_zip(working):
            raise EditRejected(
                "The copy saved in Power BI Desktop is not a complete report. Save it again in "
                "Power BI Desktop, then try again."
            )
        try:
            PbixPackage(working)
        except PackageError as error:
            raise EditRejected(
                f"Power BI Desktop's saved report cannot be read: {error}"
            ) from error
        return True

    def _adopt(self, batch: _Batch) -> Revision | None:
        """Step 4: record what Desktop saved, unless it is only Desktop's normalization."""
        try:
            adopted = batch.store.adopt_working_copy(
                desktop_version=batch.version, equivalent=self._equivalent
            )
        except report_revisions.WorkingCopyBusy as error:
            raise EditRejected(str(error)) from error
        if adopted is not None:
            batch.adopted = adopted
            later_edits = (
                " (they were made on an earlier version, so Studio's later edits are not in them)."
                if adopted.conflict
                else "."
            )
            batch.warnings.append(
                "Changes saved in Power BI Desktop were kept as their own step in the history"
                + later_edits
            )
            self._note(batch, "adopted", parent=adopted.id, revision=adopted.id)
        return adopted

    def _abandon(self, batch: _Batch) -> None:
        """Put the working copy back to the head and reopen Desktop, after a failed batch.

        Best effort: the failure being reported matters more than a problem here. Once
        Desktop has been opened for a model change, whatever it saved is Studio's own
        by-product and the head replaces it; otherwise a working copy that cannot be reset
        (Desktop still has it, a save Studio has not recorded) is left for the next
        batch's adoption. Nothing runs when Desktop was never touched.
        """
        if batch.committed or not (batch.desktop_closed or batch.model_touched):
            return
        store = batch.store
        try:
            store.materialize(force=batch.model_touched)
        except (OSError, RevisionError, StateConflictError):
            return
        if batch.was_open and self._reopen and self._controller is not None:
            try:
                self._open_placed(store)
            except _PORT_FAILURES:
                return

    def _fail(self, batch: _Batch, error: BaseException) -> None:
        for path in batch.staged:
            path.unlink(missing_ok=True)
        state = "rolled_back" if batch.model_touched else "failed"
        # The journal is a safety net; the original error is the one to report.
        with contextlib.suppress(OSError):
            self._note(batch, state, reason=str(error)[:500])

    def _finish(
        self,
        batch: _Batch,
        revision: Revision,
        summaries: list[str],
        *,
        model_changed: bool,
        force_place: bool = False,
    ) -> EditReceipt:
        """Steps 7-8 after the head moved: nothing here may turn the answer into a failure."""
        store = batch.store
        warnings = list(batch.warnings)
        status: Literal["updated", "partial"] = "updated"
        desktop = "closed"
        self._note(batch, "committed", revision=revision.id)
        try:
            store.materialize(force=force_place)
            placed = store.placed()
            self._note(batch, "placed", hash=placed[1] if placed else "")
        except (OSError, RevisionError, StateConflictError) as error:
            status = "partial"
            warnings.append(
                "The change is saved in Studio, but the copy for Power BI Desktop could not be "
                f"refreshed yet ({error}). It is refreshed when you open the report in Power BI."
            )
        else:
            if batch.was_open and self._reopen and self._controller is not None:
                self._say(batch, "Opening Power BI Desktop")
                try:
                    self._open_placed(store)
                    desktop = "reopened"
                    self._note(batch, "reopened")
                except _PORT_FAILURES as error:
                    warnings.append(
                        f"Power BI Desktop could not be reopened ({_port_message(error)}). "
                        "Use Open in Power BI."
                    )
        report = batch.report
        try:
            report = self.rederive(batch.report, store.path(revision), revision.blob, model_changed)
        except Exception as error:  # noqa: BLE001 - the head moved; a retry would apply it twice
            status = "partial"
            warnings.append(
                f"The change is saved, but Studio could not refresh its view of the report "
                f"({error}). Reopen the report to see it."
            )
        try:
            store.prune()
            batch.journal.trim()
        except (OSError, RevisionError):
            pass  # housekeeping only; the next batch prunes again
        journal_note = (
            "The change is saved, but Studio could not write its activity log (the disk may "
            "be full or the file locked). Check the free space on the drive that holds "
            "Studio's data."
        )
        if batch.journal_failed:
            status = "partial"
            warnings.append(journal_note)
        reported = batch.outcome or EditOutcome()
        warnings.extend(w for w in reported.warnings if w not in warnings)
        receipt = EditReceipt(
            revision=revision,
            report=report,
            summaries=summaries,
            warnings=warnings,
            adopted=batch.adopted,
            desktop=desktop,
            status=status,
            batch_id=batch.id,
            model_changed=model_changed,
            protected=list(reported.protected),
            replaced=list(reported.replaced),
            visible_in=list(reported.visible_in),
            pending_model_operations=list(reported.pending_model_operations),
        )
        recorded_before = batch.journal_failed
        self._note(
            batch,
            "done",
            receipt={
                "revision": revision.id,
                "summaries": summaries,
                "warnings": list(warnings),
                "adopted": batch.adopted.id if batch.adopted else None,
                "desktop": desktop,
                "status": status,
                "model_changed": model_changed,
                **_receipt_record(reported),
            },
        )
        if batch.journal_failed and not recorded_before:
            receipt.status = "partial"
            receipt.warnings.append(journal_note)
        return receipt

    def _replay(
        self, store: RevisionStore, journal: EditJournal, report: ImportedReport, key: str
    ) -> EditReceipt | None:
        """P8: a repeated key returns what the first attempt did instead of doing it again."""
        prior = journal.find_key(key)
        if prior is None:
            return None
        if not prior.terminal:
            # Nothing runs the batch any more (this call holds the lock): finish or undo it.
            recover(
                store.root,
                store,
                self._controller() if self._controller else None,
                self._equivalent,
            )
            prior = journal.find_key(key)
        if prior is None or prior.state != "done" or prior.receipt is None:
            return None  # failed or undone: the same request may run again
        return self._receipt_from(store, report, prior)

    def _receipt_from(
        self, store: RevisionStore, report: ImportedReport, batch: BatchRecord
    ) -> EditReceipt:
        recorded = batch.receipt or {}
        revision = store.revision(str(recorded.get("revision", store.head().id)))
        adopted_id = recorded.get("adopted")
        head = store.head()
        model_changed = bool(recorded.get("model_changed", True))
        warnings = [str(item) for item in recorded.get("warnings", [])]
        current = report
        try:
            current = self.rederive(report, store.path(head), head.blob, model_changed)
        except Exception as error:  # noqa: BLE001 - a replay must still answer
            warnings.append(f"Studio could not refresh its view of the report ({error}).")
        return EditReceipt(
            revision=revision,
            report=current,
            summaries=[str(item) for item in recorded.get("summaries", batch.ops)],
            warnings=warnings,
            adopted=store.revision(str(adopted_id)) if adopted_id else None,
            desktop=str(recorded.get("desktop", "closed")),
            status="partial" if recorded.get("status") == "partial" else "updated",
            replayed=True,
            batch_id=batch.id,
            model_changed=model_changed,
            **_recorded_items(recorded),
        )

    # --- edits ------------------------------------------------------------------------------

    def apply(
        self,
        report: ImportedReport,
        operations: list[Any],
        *,
        expected_head: str,
        summary: str | None = None,
        author: CommitAuthor = "studio",
        idempotency_key: str | None = None,
        desktop_work: DesktopWork = "ask",
        progress: Progress | None = None,
        confirmed: Collection[str] = (),
    ) -> EditReceipt:
        """Apply a batch of typed operations as one revision.

        Raises ``UnsavedDesktopWork`` when Desktop has unsaved work and ``desktop_work`` is
        "ask" (the caller offers "save into history and continue" or cancel),
        ``RevisionConflict`` when ``expected_head`` is stale, and ``EditRejected`` when the
        edit is invalid or Power BI did not keep it. In every raise the report is as it was.
        ``confirmed`` holds the batch-wide yeses (``report_tests_broken``): a model batch that
        breaks report tests raises ``ReportTestsBroken`` without it, before Desktop is opened
        when its measures alone decide it, else before the revision is committed.
        """
        if not operations:
            raise EditRejected("There is nothing to change")
        store = self.store(report)
        key = idempotency_key or uuid.uuid4().hex
        with store.lock():
            self._check_space(store)
            journal = EditJournal(store.journal_path)
            replay = self._replay(store, journal, report, key)
            if replay is not None:
                return replay
            head = store.head()
            if head.id != expected_head:
                # Adopting a Desktop save only moves the head forward from where it is now,
                # so a stale caller is stale for certain: do not close Desktop to find out.
                raise RevisionConflict(expected_head, head.id)
            model = self._model_info(
                report,
                report.measures,
                self._read_facts(_viewed_at(report, store, head), operations),
            )
            # Refusals that need no Desktop come first, before Desktop is closed or opened:
            # Studio's model tool must be able to run, and a change outside Microsoft's list
            # needs a live probe receipt for this Desktop version.
            notes = self._model_family_gate(operations, model)
            # Fail on a bad edit before Desktop is disturbed; re-checked on the final head.
            # With work in Desktop the edit may name something only that work has (a measure
            # the user just added), so the refusal waits for the check after adoption.
            first: EditOutcome | None = None
            try:
                first = self._dry_run(store.path(head), operations, model, None)[2]
            except EditRejected:
                if not self._desktop_may_hold_work(store):
                    raise
            tests_confirmed = TESTS_CONFIRMATION in confirmed
            # Report tests, step 1: the batch's measures projected onto the head's, run before
            # Desktop is touched. A broken test raises here and nothing has happened yet.
            tests = (
                self._begin_tests(report, head, first, tests_confirmed)
                if first is not None
                else None
            )
            names = [str(op.op) for op in operations]
            batch = self._begin(report, store, "edit", key, names, progress)
            batch.warnings.extend(n for n in notes if n not in batch.warnings)
            batch.tests, batch.tests_confirmed = tests, tests_confirmed
            # A first dry run that was left to the check after adoption begins the tests there.
            batch.tests_pending = first is None
            try:
                return self._run_edit(
                    batch, operations, expected_head, summary, author, desktop_work
                )
            except Exception as error:
                self._abandon(batch)
                self._fail(batch, error)
                raise

    def _begin_tests(
        self, report: ImportedReport, head: Revision, outcome: EditOutcome, confirmed: bool
    ) -> TestsCheck | None:
        """Step 1 of the report-tests gate for a batch that changes the model (a report-layer
        edit changes no number and is never gated)."""
        if self.tests_gate is None or not outcome.model_operations:
            return None
        return self.tests_gate.begin(
            report, head, list(outcome.model_operations), confirmed=confirmed
        )

    def _model_family_gate(self, operations: list[Any], model: ModelInfo) -> list[str]:
        """Refuse a batch of model changes (beyond measures) that cannot run, before Desktop is
        touched: no Desktop port that drives the model, Studio's model tool unavailable
        (bug review #10), or a change outside Microsoft's documented list that no passing model
        probe receipt for the installed Desktop version covers (the refusal gives Desktop's own
        steps). Returns the receipt notes of the covered changes outside the list."""
        family = [op for op in operations if isinstance(op, MODEL_FAMILY_OPERATIONS)]
        if not family:
            return []
        port = self.controller()
        if not callable(getattr(port, "apply_model_operations", None)):
            raise EditRejected(
                "Power BI Desktop is needed for this change but Studio cannot drive its model "
                "here. Make the change in Power BI Desktop."
            )
        runner_problem = getattr(port, "model_runner_problem", None)
        problem = runner_problem() if callable(runner_problem) else None
        if isinstance(problem, str) and problem:
            raise EditRejected(f"{problem}. Nothing was changed.")
        keys = report_edit_model.outside_support(family, model)
        if not keys:
            return []
        coverage = getattr(port, "model_probe_coverage", None)
        try:
            version, verified = coverage() if callable(coverage) else (self._version(), frozenset())
        except _PORT_FAILURES:
            version, verified = self._version(), frozenset()
        missing = [key for key in keys if key not in verified]
        if missing:
            raise EditRejected(report_edit_model.support_refusal(missing[0], version))
        return [report_edit_model.support_note(key, version) for key in keys]

    def _desktop_may_hold_work(self, store: RevisionStore) -> bool:
        """True when Desktop, or a save it left behind, may hold changes the head lacks."""
        instance = self._find(store)
        if instance is not None:
            try:
                return bool(self.controller().status(instance).get("hasUnsavedChanges"))
            except _PORT_FAILURES:
                return True
        try:
            return store.working_copy_state(self._equivalent) == "user_changes"
        except StateConflictError:
            return True

    def _dry_run(
        self,
        path: Path,
        operations: list[Any],
        model: ModelInfo,
        conflict: tuple[str, str] | None,
    ) -> tuple[PbirDocument, PbixPackage, EditOutcome]:
        """Build the document and apply the operations to it; nothing is written.

        The result is held only to the problems the edit introduced: what the source report
        already had (``capture_baseline`` of the head, taken before anything is applied) never
        blocks, and a visual or page the edit copied is held to its source's baseline."""
        try:
            package = PbixPackage(path)
            document = PbirDocument(package)
            baseline = capture_baseline(PbirDocument(package), model)
            outcome = apply_operations(document, operations, model)
            problems = validate_document(
                document, model, baseline=baseline, lineage=outcome.lineage
            )
        except (NeedsConfirmation, NeedsChoice) as error:
            # The caller acts on the kind of answer needed (confirm, ask), so it is not
            # turned into a plain refusal.
            if conflict is not None:
                raise RevisionConflict(*conflict) from error
            raise
        except (DocumentError, PackageError) as error:
            if conflict is not None:
                raise RevisionConflict(*conflict) from error
            raise EditRejected(str(error)) from error
        if problems:
            if conflict is not None:
                raise RevisionConflict(*conflict)
            raise EditRejected(
                "The edit would leave the report invalid: " + "; ".join(problems[:3])
            )
        # A model operation the edit layer did not hand over as one (a facade that lists the
        # family's handlers but not its classes among MODEL_OPERATIONS) would be reported as done
        # and never reach the model.
        if any(isinstance(op, MODEL_FAMILY_OPERATIONS) for op in operations) and not any(
            isinstance(op, MODEL_FAMILY_OPERATIONS) for op in outcome.model_operations
        ):
            raise EditRejected(
                "Studio is not set up to send this change to Power BI Desktop, so nothing was "
                "changed. Make the change in Power BI Desktop."
            )
        # Checked here, before Desktop is opened for it, and again by the port itself.
        unsupported = unsupported_measure_change(outcome.model_operations)
        if unsupported is not None:
            if conflict is not None:
                raise RevisionConflict(*conflict)
            raise EditRejected(unsupported)
        if outcome.model_operations and not _has_data_model(path):
            if any(isinstance(op, MODEL_FAMILY_OPERATIONS) for op in outcome.model_operations):
                raise EditRejected(
                    "This report reads its data from a shared Power BI model, so Studio cannot "
                    "change that model here. Change it in the report that owns the model."
                )
            raise EditRejected(
                "This report reads its data from a shared Power BI model, so Studio cannot "
                "change measures here. Change them in Power BI Desktop, or in the report that "
                "owns the model."
            )
        return document, package, outcome

    def _run_edit(
        self,
        batch: _Batch,
        operations: list[Any],
        expected_head: str,
        summary: str | None,
        author: CommitAuthor,
        desktop_work: DesktopWork,
    ) -> EditReceipt:
        store, report = batch.store, batch.report
        self._gate(batch, desktop_work)
        before_adoption = store.head().id
        self._adopt(batch)
        head = store.head()
        if head.id != expected_head and not (
            batch.adopted is not None and expected_head == before_adoption
        ):
            raise RevisionConflict(expected_head, head.id)
        # Ops are absolute values, so on a head that only gained a Desktop save the same
        # edit still applies when its targets exist; otherwise it is a conflict.
        conflict = (expected_head, head.id) if head.id != expected_head else None
        if batch.adopted is not None and _has_data_model(store.path(head)):
            # The model in Desktop's save may differ from the one Studio knows (a measure the
            # user added), and the edit is checked against the head, not against a stale view.
            batch.report = report = report.model_copy(
                update={"measures": self._read_measures(store.path(head))}
            )
        # Facts of the head's own model: after an adopted save that is not the file the view
        # was built from. The types sidecar is keyed by the DataModel, so this costs nothing
        # while the model bytes are the ones it already read.
        head_has_model = _has_data_model(store.path(head))
        batch.facts = self._read_facts(_viewed_at(report, store, head), operations)
        batch.warnings.extend(
            self._facts_warnings(batch.facts, operations, has_model=head_has_model)
        )
        working_model = self._model_info(report, report.measures, batch.facts)
        document, package, outcome = self._dry_run(
            store.path(head), operations, working_model, conflict
        )
        batch.outcome = outcome
        batch.planned_model = working_model
        if batch.tests_pending and batch.tests is None:
            batch.tests_pending = False
            batch.tests = self._begin_tests(report, head, outcome, batch.tests_confirmed)
        text = summary or " ".join(outcome.summaries)
        serialized = [_plain(op) for op in operations]
        if not _has_data_model(store.path(head)):
            batch.warnings.append(
                "This report has no model of its own, so Studio cannot check these changes "
                "against the data. Check them in Power BI Desktop."
            )
        if outcome.model_operations:
            batch.version = batch.version or self._version()
            return self._model_transaction(batch, head, operations, outcome, text, serialized)
        self._say(batch, "Writing the change")
        document.commit()
        staged = store.staging_path()
        batch.staged.append(staged)
        digest = package.write(staged)
        self._note(batch, "staged", path=str(staged), hash=digest)
        revision = store.commit(
            staged,
            author=author,
            summary=text,
            operations=serialized,
            expected_head=head.id,
            desktop_version=batch.version,
        )
        batch.committed = True
        # An adopted Desktop save may have changed the model, so Studio's view is rebuilt.
        return self._finish(
            batch, revision, outcome.summaries, model_changed=batch.adopted is not None
        )

    # --- batches with measure operations: one transaction across both layers -----------------

    def _model_transaction(
        self,
        batch: _Batch,
        head: Revision,
        operations: list[Any],
        outcome: EditOutcome,
        text: str,
        serialized: list[dict[str, Any]],
    ) -> EditReceipt:
        store, report = batch.store, batch.report
        port = self.controller()
        # Anything beyond measures goes through the TOM runner; a port without one (Desktop not
        # set up here) refuses before Desktop is touched.
        runner_needed = any(
            isinstance(op, MODEL_FAMILY_OPERATIONS) for op in outcome.model_operations
        )
        apply_model = getattr(port, "apply_model_operations", None)
        if runner_needed and not callable(apply_model):
            raise EditRejected(
                "Power BI Desktop is needed for this change but Studio cannot drive its model "
                "here. Make the change in Power BI Desktop."
            )
        runner_problem = getattr(port, "model_runner_problem", None)
        problem = runner_problem() if runner_needed and callable(runner_problem) else None
        if isinstance(problem, str) and problem:
            raise EditRejected(f"{problem}. Nothing was changed.")
        # What the saved file is held to must be the head's own measures, not Studio's view
        # of them: a caller can hold an older view, and Desktop's save is judged against
        # "the head plus these operations".
        rows = self._read_measures(store.path(head))
        pre_model = self._model_info(report, rows, batch.facts)
        store.materialize()
        self._say(batch, "Opening Power BI Desktop")
        batch.model_touched = True
        try:
            instance = port.open(store.working_copy, normalize=True)
        except _PORT_FAILURES as error:
            raise _refusal(error) from error
        tables = {str(m.get("Name")): str(m.get("TableName")) for m in rows}
        self._say(batch, "Applying the change in Power BI")
        try:
            if runner_needed and callable(apply_model):
                applied = apply_model(instance, outcome.model_operations)
                batch.warnings.extend(
                    w for w in getattr(applied, "warnings", []) if w not in batch.warnings
                )
            else:
                port.apply_measure_operations(instance, outcome.model_operations, tables)
        except DesktopRefreshFailed as error:
            # The runner undid the batch in Desktop; persist that, then ask or refuse.
            self._close_after_rollback(port, instance, error)
            raise _refresh_answer(operations, error) from error
        except DesktopSessionError as error:
            self._close_after_rollback(port, instance, error)
            raise _refusal_saying(
                f"Power BI did not accept the change ({error}). The report was not changed.",
                error,
            ) from error
        except Exception as error:  # noqa: BLE001 - a timeout or crash leaves Desktop half-changed
            # Nobody knows which measures Desktop applied, so it is closed the same way (its
            # state is Studio's by-product and the head replaces it), never left dirty for
            # the next batch to mistake for the user's own work.
            self._close_after_rollback(port, instance, error)
            raise EditRejected(
                "Power BI Desktop did not finish applying the change in time, so it was not "
                "kept. The report was not changed."
            ) from error
        self._note(batch, "model_applied")
        self._say(batch, "Saving")
        try:
            port.save(instance)
            self._note(batch, "model_saved")
            port.close(instance)
        except _PORT_FAILURES as error:
            raise _refusal_saying(
                "Power BI Desktop could not finish saving and closing the report "
                f"({_port_message(error)}). "
                "The history is unchanged. Close the report in Power BI Desktop (choose Don't "
                "save if it asks), then try again.",
                error,
            ) from error
        batch.desktop_closed = True
        provisional, digest = store.stage_working_copy()
        batch.staged.append(provisional)
        self._note(batch, "adopted", provisional=digest)
        self._note(batch, "staged", path=str(provisional), hash=digest)
        staged, summaries = self._check_model_layer(batch, head, provisional, operations, pre_model)
        if batch.tests is not None and batch.tests.needs_saved:
            # Report tests, step 2: what only Power BI's saved model can say (columns, tables,
            # relationships changed). A broken test raises here: the head is put back over
            # Desktop's copy by ``apply`` and nothing is committed.
            saved = report.model_copy(update={"measures": self._read_measures(provisional)})
            if batch.saved_facts:
                with contextlib.suppress(Exception):
                    saved = report_import.with_model_facts(saved, dict(batch.saved_facts))
            batch.tests.after_save(saved)
        revision = store.commit(
            staged,
            author="studio",
            summary=text,
            operations=serialized,
            expected_head=head.id,
            desktop_version=batch.version,
        )
        batch.committed = True
        if staged != provisional:
            provisional.unlink(missing_ok=True)
        receipt = self._finish(
            batch, revision, summaries or outcome.summaries, model_changed=True, force_place=True
        )
        receipt.tests = batch.tests
        receipt.model_operations = list(outcome.model_operations)
        return receipt

    @staticmethod
    def _close_after_rollback(
        port: DesktopPort, instance: DesktopInstance, failure: Exception
    ) -> None:
        """The port undid the measures inside Desktop; persist that so Desktop is clean and
        can be closed. The caller then puts the head back over the working copy. If Desktop
        cannot be closed cleanly it is left open and the working copy is left to it."""
        try:
            port.save(instance)
            port.close(instance)
        except _PORT_FAILURES as error:
            raise _refusal_saying(
                f"Power BI did not accept the change ({_port_message(failure)}), and Power BI "
                f"Desktop could not be closed cleanly ({_port_message(error)}). Close the "
                "report in Power BI Desktop (choose Don't save if it asks), then try again. "
                "The history is unchanged.",
                error,
                failure,
            ) from error

    def _check_model_layer(
        self,
        batch: _Batch,
        head: Revision,
        provisional: Path,
        operations: list[Any],
        pre_model: ModelInfo,
    ) -> tuple[Path, list[str]]:
        """Steps 3-4 of the transaction: survival of what Studio wrote, then the report-layer
        half of the operations on Desktop's saved file. Returns (file to commit, summaries)."""
        store, report = batch.store, batch.report
        # What Studio wrote is the head's difference from the file that edit started from;
        # a head Studio did not write (the import, a Desktop save) has nothing to blame.
        started_from = store.path(head)
        if head.author == "studio" and head.parent is not None:
            parent = store.revision(head.parent)
            if store.available(parent):
                started_from = store.path(parent)
        dropped = report_revisions.survival(store.path(head), provisional, started_from)
        if dropped:
            raise EditRejected(
                "Power BI Desktop dropped part of the report while saving, so the change was "
                "not kept: " + "; ".join(dropped[:3]) + ". The report was not changed."
            )
        saved_rows = self._read_measures(provisional)
        saved = ModelInfo.from_report(report.tables, saved_rows)
        # Before anything is applied to the report: the measures the file has are the ones the
        # operations alone must leave. Anything else means Desktop did not keep the change.
        if _expected_measures(pre_model, operations) != _measure_map(saved):
            raise EditRejected(
                "Power BI saved a different set of measures than the change asked for, so it was "
                "not kept. The report was not changed."
            )
        # The model as the saved file has it, with the head's types and calculated DAX: what the
        # result is validated against. The report-layer half resolves names against the measures
        # of both sides (``_model_for_report_half``).
        after = self._model_info(report, saved_rows, batch.facts)
        family = [op for op in operations if isinstance(op, MODEL_FAMILY_OPERATIONS)]
        if family:
            # The whole batch: a measure rename in it is carried into a field parameter's fields.
            self._check_saved_facts(batch, provisional, operations)
            if batch.planned_model is not None:
                # The facts diff above proved the saved model has the batch's columns and tables;
                # the dry run's view of them is what the report is validated against.
                after.columns = {t: dict(c) for t, c in batch.planned_model.columns.items()}
        model = _model_for_report_half(_model_before(pre_model, saved, operations), after)
        try:
            # What the head already had wrong stays out of the way, the same as before Desktop
            # was opened (``_dry_run``); Desktop's own rewrite of the file is not held against it.
            baseline: Baseline = capture_baseline(
                PbirDocument(PbixPackage(store.path(head))), pre_model
            )
            package = PbixPackage(provisional)
            document = PbirDocument(package)
            # A reference the head already had keeps counting as the head's own after the batch
            # renames the table it names.
            moved = {
                op.name.casefold(): op.new_name.casefold()
                for op in family
                if isinstance(op, RenameTable)
            }
            references_before = {
                (moved.get(table, table), name) for table, name in _measure_references(document)
            }
            outcome = apply_report_operations(document, operations, model)
            problems = validate_document(
                document, after, baseline=baseline, lineage=outcome.lineage
            )
        except (DocumentError, PackageError) as error:
            raise EditRejected(
                f"The change could not be applied to the report Power BI saved ({error}). "
                "The report was not changed."
            ) from error
        if problems:
            raise EditRejected(
                "The change would leave the report invalid: " + "; ".join(problems[:3])
            )
        known = _measure_map(saved)
        # A reference the operations left pointing at a name they renamed or deleted is
        # dangling even though it was there before (a rewrite that missed one spelling).
        gone = {
            op.name.casefold()
            for op in operations
            if isinstance(op, (RenameMeasure, DeleteMeasure))
        }
        dangling = {
            f"{table}[{name}]"
            for table, name in _measure_references(document)
            if known.get(name) != table and ((table, name) not in references_before or name in gone)
        }
        if dangling:
            raise EditRejected(
                "The change would leave visuals that use measures the model does not have ("
                + ", ".join(sorted(dangling)[:3])
                + "). The report was not changed."
            )
        stale = _renamed_away(document, family, batch.planned_model)
        if stale:
            raise EditRejected(
                "The change would leave visuals that use fields the model no longer has ("
                + ", ".join(sorted(stale)[:3])
                + "). The report was not changed."
            )
        document.commit()
        # The receipt keeps what stage one reported (summaries, warnings and suggestions of the
        # model operations too): this pass only ran the report-layer half, so its own summary
        # of a rename has no dependents and it has nothing to say about the other measure
        # operations.
        summaries = batch.outcome.summaries if batch.outcome is not None else outcome.summaries
        if batch.outcome is None:
            batch.outcome = outcome
        if not package.changed:
            # Only the model changed: Desktop's own file (with its fresh SecurityBindings)
            # is the revision.
            return provisional, summaries
        staged = store.staging_path()
        batch.staged.append(staged)
        digest = package.write(staged)
        self._note(batch, "staged", path=str(staged), hash=digest)
        return staged, summaries

    def _check_saved_facts(self, batch: _Batch, provisional: Path, family: list[Any]) -> None:
        """Hold Desktop's saved model to what the batch's model operations asked for. A section
        the facts do not have is not a failure: the receipt says the check was Desktop's own."""
        try:
            facts = self._read_saved_facts(provisional)
        except Exception as error:  # noqa: BLE001 - unreadable facts leave the checks to Desktop
            LOGGER.warning("Could not read the saved model facts: %s", error)
            facts = {}
        batch.saved_facts = facts
        problems, unchecked = report_edit_model.saved_model_problems(family, facts)
        if problems:
            raise EditRejected(
                "Power BI Desktop did not keep the change as asked ("
                + "; ".join(problems[:3])
                + "). The report was not changed."
            )
        if unchecked:
            note = (
                "Studio could not read back the saved model's "
                + ", ".join(unchecked)
                + ", so it relied on Power BI Desktop's own checks for them."
            )
            if note not in batch.warnings:
                batch.warnings.append(note)

    # --- undo, restore -----------------------------------------------------------------------

    def undo(
        self,
        report: ImportedReport,
        *,
        expected_head: str,
        idempotency_key: str | None = None,
        desktop_work: DesktopWork = "ask",
        progress: Progress | None = None,
    ) -> EditReceipt:
        """Revert the newest change. Only that change: older ones are restored (``restore``)."""
        store = self.store(report)
        key = idempotency_key or uuid.uuid4().hex
        with store.lock():
            self._check_space(store)
            replay = self._replay(store, EditJournal(store.journal_path), report, key)
            if replay is not None:
                return replay
            if store.head().id != expected_head:
                raise RevisionConflict(expected_head, store.head().id)
            store.check_undo()
            batch = self._begin(report, store, "undo", key, ["undo"], progress)
            try:
                self._gate(batch, desktop_work)
                if self._adopt(batch) is not None:
                    raise RevisionConflict(expected_head, store.head().id)
                revision = store.undo(expected_head, desktop_version=batch.version)
                batch.committed = True
                return self._finish(batch, revision, [revision.summary], model_changed=True)
            except Exception as error:
                self._abandon(batch)
                self._fail(batch, error)
                raise

    def restore_preview(self, report: ImportedReport, revision_id: str) -> list[Revision]:
        """The later changes (Studio's and Desktop saves) a restore would leave out."""
        return self.store(report).later_changes(revision_id)

    def restore(
        self,
        report: ImportedReport,
        revision_id: str,
        *,
        expected_head: str,
        confirmed_discard: bool = False,
        idempotency_key: str | None = None,
        desktop_work: DesktopWork = "ask",
        progress: Progress | None = None,
    ) -> EditReceipt:
        """Make an earlier version the head again (as a new row; nothing is deleted).

        Raises ``RestoreNeedsConfirmation`` when later changes would be left out and
        ``confirmed_discard`` is not set, including a Desktop save found while preparing.
        """
        store = self.store(report)
        key = idempotency_key or uuid.uuid4().hex
        with store.lock():
            self._check_space(store)
            replay = self._replay(store, EditJournal(store.journal_path), report, key)
            if replay is not None:
                return replay
            if store.head().id != expected_head:
                raise RevisionConflict(expected_head, store.head().id)
            # A no-op or pruned target is refused before Desktop is closed for it.
            store.check_restore(revision_id)
            later = store.later_changes(revision_id)
            if later and not confirmed_discard:
                raise RestoreNeedsConfirmation(later)
            confirmed = {row.id for row in later}
            batch = self._begin(report, store, "restore", key, [f"restore {revision_id}"], progress)
            try:
                self._gate(batch, desktop_work)
                if self._adopt(batch) is not None:
                    # The caller agreed to leave out what its preview listed. A save adopted
                    # just now was not in that preview, so it needs its own confirmation.
                    now = store.later_changes(revision_id)
                    if any(row.id not in confirmed for row in now):
                        raise RestoreNeedsConfirmation(now)
                    expected_head = store.head().id
                revision = store.restore(
                    revision_id, expected_head=expected_head, desktop_version=batch.version
                )
                batch.committed = True
                return self._finish(batch, revision, [revision.summary], model_changed=True)
            except Exception as error:
                self._abandon(batch)
                self._fail(batch, error)
                raise

    # --- the working copy --------------------------------------------------------------------

    def sync_desktop(self, report: ImportedReport) -> ImportedReport | None:
        """Adopt a save the user made in Power BI Desktop (when Desktop is closed)."""
        store = self.store(report)
        with store.lock():
            if self._find(store) is not None:
                return None
            adopted = store.adopt_working_copy(
                desktop_version=self._version(), equivalent=self._equivalent
            )
            if adopted is None:
                return None
            return self.rederive(report, store.path(adopted), adopted.blob, True)

    def _open_placed(self, store: RevisionStore) -> DesktopInstance:
        """Open the working copy in Desktop; when the open saved Desktop's own rewrite of the
        file (normalize), that is recorded as the placed revision, not as user work."""
        controller = self.controller()
        instance = controller.open(store.working_copy, normalize=True)
        normalized = getattr(controller, "is_normalized", None)
        if callable(normalized) and normalized(instance):
            with contextlib.suppress(OSError, RevisionError, StateConflictError):
                store.replace_placed_copy()
        return instance

    def open_in_desktop(self, report: ImportedReport) -> DesktopInstance:
        """Open the head in Power BI Desktop: adopt a save, put the head in the working copy
        (P3), then open it. Replaces copying the original only when the file was missing."""
        store = self.store(report)
        with store.lock():
            existing = self._find(store)
            if existing is not None:
                return existing
            adopted = store.adopt_working_copy(
                desktop_version=self._version(), equivalent=self._equivalent
            )
            if adopted is not None:
                self.rederive(report, store.path(adopted), adopted.blob, True)
            store.materialize()
            return self._open_placed(store)

    def desktop_screenshot(self, report: ImportedReport, page_id: str, output: Path) -> Path:
        """A Desktop screenshot of the head, only when Desktop shows exactly that (P18):
        the working copy is the head and Desktop has no unsaved changes."""
        store = self.store(report)
        with store.lock():
            instance = self._find(store)
            if instance is None:
                raise EditRejected("Open the report in Power BI first to preview it there")
            port = self.controller()
            if port.status(instance).get("hasUnsavedChanges"):
                raise EditRejected(
                    "Power BI Desktop has unsaved changes, so its preview would not match "
                    "Studio. Save the report in Power BI Desktop, then try again."
                )
            if store.working_copy_state(self._equivalent) != "placed":
                raise EditRejected(
                    "Power BI Desktop is showing a different version of the report than "
                    "Studio has. Close it and use Open in Power BI to preview the current one."
                )
            return port.screenshot(instance, page_id, output)

    def working_copy_state(self, report: ImportedReport) -> WorkingCopyState:
        return self.store(report).working_copy_state(self._equivalent)

    def recover(self, report: ImportedReport) -> RecoveryReport:
        """Finish or undo batches a crash left in flight (run at start-up for each report)."""
        store = self.store(report)
        port = self.controller() if self._controller is not None else None
        return recover(store.root, store, port, self._equivalent)

    # --- history and delivery ------------------------------------------------------------------

    def history(self, report: ImportedReport) -> list[Revision]:
        return self.store(report).history()

    def history_with_flags(self, report: ImportedReport) -> list[HistoryEntry]:
        return self.store(report).history_with_flags()

    def _delivery_port(self) -> report_delivery.DeliveryPort | None:
        return self.controller() if self._controller is not None else None

    def save_copy(self, report: ImportedReport, destination: Path) -> Path:
        """Write the current head to a new .pbix (never overwrites: adds " (2)" and so on).

        ``destination`` is a file name or a folder (then "<name> (edited).pbix").
        """
        store = self.store(report)
        with store.lock():
            before = store.head().id
            try:
                written = report_delivery.save_copy(
                    store,
                    Path(destination),
                    name=report.name,
                    port=self._delivery_port(),
                    equivalent=self._equivalent,
                )
            except report_delivery.DeliveryError as error:
                self._refresh_after_adoption(store, report, before)
                raise EditRejected(str(error)) from error
            self._refresh_after_adoption(store, report, before)
            return written

    def replace_original(self, report: ImportedReport) -> report_delivery.ReplaceResult:
        """Write the head over the user's own file, after a backup and only if that file is
        still the one Studio imported (or last wrote)."""
        store = self.store(report)
        with store.lock():
            before = store.head().id
            try:
                result = report_delivery.replace_original(
                    store,
                    Path(report.source_path),
                    report.source_hash,
                    port=self._delivery_port(),
                    equivalent=self._equivalent,
                )
            except report_delivery.DeliveryError as error:
                self._refresh_after_adoption(store, report, before)
                raise EditRejected(str(error)) from error
            self._refresh_after_adoption(store, report, before)
            return result

    def _refresh_after_adoption(
        self, store: RevisionStore, report: ImportedReport, before: str
    ) -> None:
        """Delivery adopts a Desktop save first; Studio's view must follow the new head."""
        head = store.head()
        if head.id != before:
            self.rederive(report, store.path(head), head.blob, True)

    def delivery_state(self, report: ImportedReport) -> report_delivery.DeliveryState:
        return report_delivery.delivery_state(self.store(report), Path(report.source_path))

    def pending_changes(self, report: ImportedReport) -> int:
        """How many changes the user's own file does not have yet."""
        return report_delivery.pending_changes(self.store(report), Path(report.source_path))
