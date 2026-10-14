"""The edit pipeline with an in-memory Power BI Desktop: both batch shapes, undo, recovery."""

from __future__ import annotations

import json
import subprocess
import threading
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from editing_support import (
    DESKTOP_VERSION,
    INITIAL_MEASURES,
    MEASURE_TABLE,
    MODEL_MEASURES,
    OTHER_VISUAL_PART,
    SURVIVAL_CALLS,
    TWIN,
    VISUAL,
    VISUAL_PART,
    Crash,
    Env,
    FakeDesktop,
    make_env,
    make_zip,
    model_state,
    part_names,
    patch_diff,
    read_measures,
    read_model_facts,
    read_part,
    sha,
    tiny_pbix,
    visual_x,
)

from ai_powerbi_analyst.studio import (
    report_delivery,
    report_edit_model,
    report_editor,
    report_edits,
    report_revisions,
)
from ai_powerbi_analyst.studio.desktop_session import (
    DesktopController,
    DesktopDialogOpen,
    DesktopSessionError,
    UnsavedDesktopWork,
)
from ai_powerbi_analyst.studio.edit_journal import EditJournal, RecoveryPort
from ai_powerbi_analyst.studio.pbir_document import PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage
from ai_powerbi_analyst.studio.report_edit_conditional import SetConditionalFormat
from ai_powerbi_analyst.studio.report_edit_context import Choice, Protected, VisibleIn
from ai_powerbi_analyst.studio.report_editor import (
    DesktopDialog,
    EditRejected,
    ReportEditor,
    RestoreNeedsConfirmation,
)
from ai_powerbi_analyst.studio.report_edits import (
    AddVisual,
    CreateMeasure,
    DeleteMeasure,
    FieldRef,
    ModelInfo,
    NeedsChoice,
    NeedsConfirmation,
    RenameMeasure,
    SetPosition,
    SetReportFilter,
    UpdateMeasure,
    validate_document,
)
from ai_powerbi_analyst.studio.report_filters import CategoricalFilter
from ai_powerbi_analyst.studio.report_filters import FieldRef as FilterField
from ai_powerbi_analyst.studio.report_import import ImportedReport, report_archive
from ai_powerbi_analyst.studio.report_revisions import RevisionConflict, RevisionError


@pytest.fixture(autouse=True)
def _doubles(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    patch_diff(monkeypatch)
    monkeypatch.setattr(report_revisions, "RETRY_DELAY", 0.01)
    desktops: list[FakeDesktop] = []
    desktop_init = FakeDesktop.__init__

    def track_desktop(desktop: FakeDesktop) -> None:
        desktop_init(desktop)
        desktops.append(desktop)

    monkeypatch.setattr(FakeDesktop, "__init__", track_desktop)
    try:
        yield
    finally:
        # Assertions preserve the user's open Desktop; only these synthetic
        # sessions own Python file handles that need explicit test teardown.
        for desktop in desktops:
            for path in desktop.open_paths():
                desktop.user_closes(path)


MOVE = SetPosition(visual=VISUAL, x=50)
OTHER = "953a59a3cad98b0b5a39"
CREATE = CreateMeasure(
    table=MEASURE_TABLE, name="Avg Discount", expression="1+1", format_string="0.0"
)
RENAME = RenameMeasure(name="Rank", new_name="Agent Rank")


def journal(env: Env) -> EditJournal:
    return EditJournal(env.store.journal_path)


def last_states(env: Env) -> list[str]:
    return list(journal(env).batches().values())[-1].states


def open_desktop(env: Env, *, normalize: bool = True) -> Any:
    """Power BI Desktop has the working copy open (as after "Open in Power BI")."""
    env.store.materialize()
    return env.desktop.open(env.working, normalize=normalize)


def head_path(env: Env) -> Path:
    return env.store.path(env.store.head())


def measure_refs(path: Path, name: str) -> int:
    """How many visual fields refer to the measure [name] of the measures table."""
    found = 0

    def walk(node: Any) -> None:
        nonlocal found
        if isinstance(node, dict):
            measure = node.get("Measure")
            if (
                isinstance(measure, dict)
                and measure.get("Property") == name
                and measure["Expression"]["SourceRef"].get("Entity") == MEASURE_TABLE
            ):
                found += 1
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    for part in part_names(path):
        if part.endswith("/visual.json"):
            walk(read_part(path, part))
    return found


def measure_names(path: Path) -> list[str]:
    return [m["Name"] for m in read_measures(path)]


# --- report-only batches -----------------------------------------------------------------------


def test_a_report_edit_with_desktop_closed_makes_one_revision_and_refreshes_the_working_copy(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)
    receipt = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    revision = env.store.head()
    assert receipt.revision == revision
    assert (revision.id, revision.author, revision.parent) == ("r0001", "studio", "r0000")
    assert receipt.status == "updated" and receipt.desktop == "closed" and not receipt.replayed
    assert receipt.summaries == ["Moved the 'Country' slicer to (50, 40)."]
    assert revision.summary == "Moved the 'Country' slicer to (50, 40)."
    assert revision.operations == [
        {
            "op": "set_position",
            "visual": VISUAL,
            "x": 50.0,
            "y": None,
            "width": None,
            "height": None,
        }
    ]
    assert visual_x(head_path(env)) == 50.0
    assert visual_x(env.store.original) != 50.0
    assert sha(env.store.original) == env.report.source_hash
    assert sha(env.working) == revision.blob
    assert env.store.placed() == ("r0001", revision.blob)
    assert env.rederived == [(revision.blob, False)]
    assert receipt.report.revision_hash == revision.blob
    assert last_states(env) == ["planned", "staged", "committed", "placed", "done"]
    assert env.desktop.events == []
    assert revision.desktop_version is None


def test_only_the_edited_part_changes_and_studio_drops_securitybindings(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.editor.apply(env.report, [MOVE], expected_head="r0000")
    before, after = env.store.original, head_path(env)
    with zipfile.ZipFile(before) as a, zipfile.ZipFile(after) as b:
        same = [n for n in a.namelist() if n in b.namelist() and a.read(n) == b.read(n)]
        changed = [n for n in a.namelist() if n in b.namelist() and a.read(n) != b.read(n)]
        assert sorted(set(a.namelist()) - set(b.namelist())) == ["SecurityBindings"]
    assert VISUAL_PART in changed
    assert "DataModel" in same and "Report/definition/report.json" in same
    assert sorted(set(changed) - {"[Content_Types].xml", VISUAL_PART}) == []


def test_a_clean_desktop_is_closed_for_the_edit_and_reopened_on_the_new_head(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)
    open_desktop(env)
    env.desktop.events.clear()
    receipt = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert env.desktop.events == ["close", "open normalize=True"]
    assert receipt.desktop == "reopened"
    instance = env.desktop.find(env.working)
    assert instance is not None
    assert env.desktop.status(instance)["hasUnsavedChanges"] is False
    assert env.editor.working_copy_state(receipt.report) == "placed"
    assert receipt.revision.desktop_version == DESKTOP_VERSION
    assert last_states(env) == [
        "planned",
        "desktop_closed",
        "staged",
        "committed",
        "placed",
        "reopened",
        "done",
    ]
    assert env.rederived == [(receipt.revision.blob, False)]
    assert len(env.store.history()) == 2  # Desktop's normalization is not a revision


def test_reopening_can_be_switched_off(tmp_path: Path) -> None:
    env = make_env(tmp_path, reopen=False)
    open_desktop(env)
    receipt = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert receipt.desktop == "closed"
    assert env.desktop.find(env.working) is None
    assert sha(env.working) == receipt.revision.blob


def test_unsaved_desktop_work_needs_consent_and_nothing_is_touched_without_it(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)
    instance = open_desktop(env, normalize=False)
    before = sha(env.working)
    with pytest.raises(UnsavedDesktopWork, match="Save them into the history"):
        env.editor.apply(env.report, [MOVE], expected_head="r0000", idempotency_key="key-1")
    assert env.desktop.events == ["open normalize=False"]
    assert env.desktop.status(instance)["hasUnsavedChanges"] is True
    assert env.store.head().id == "r0000"
    assert sha(env.working) == before
    assert last_states(env) == ["planned", "failed"]
    assert env.rederived == []


def test_saving_the_users_work_into_history_then_applying_on_top_of_it(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env, normalize=False)
    env.desktop.user_edit(
        env.working, OTHER_VISUAL_PART, lambda v: v["position"].__setitem__("x", 999.0)
    )
    with pytest.raises(UnsavedDesktopWork):
        env.editor.apply(env.report, [MOVE], expected_head="r0000", idempotency_key="key-1")
    receipt = env.editor.apply(
        env.report,
        [MOVE],
        expected_head="r0000",
        idempotency_key="key-1",  # the retry after the user chose "Save into history"
        desktop_work="save",
    )
    rows = env.store.history()
    assert [(r.id, r.author, r.parent) for r in rows] == [
        ("r0002", "studio", "r0001"),
        ("r0001", "desktop", "r0000"),
        ("r0000", "import", None),
    ]
    assert receipt.adopted == rows[1]
    assert rows[1].desktop_version == DESKTOP_VERSION and not rows[1].conflict
    assert receipt.warnings[0].startswith("Changes saved in Power BI Desktop were kept")
    assert visual_x(head_path(env), OTHER_VISUAL_PART) == 999.0  # the user's work is in the head
    assert visual_x(head_path(env)) == 50.0
    assert env.desktop.events == [
        "open normalize=False",
        "save",
        "close",
        "open normalize=True",
    ]
    assert receipt.desktop == "reopened"
    assert last_states(env) == [
        "planned",
        "desktop_saved",
        "desktop_closed",
        "adopted",
        "staged",
        "committed",
        "placed",
        "reopened",
        "done",
    ]


def test_a_dirty_flag_with_no_real_change_saves_without_making_a_desktop_revision(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)
    open_desktop(env, normalize=False)  # dirty only because Desktop normalized the file
    receipt = env.editor.apply(env.report, [MOVE], expected_head="r0000", desktop_work="save")
    assert receipt.adopted is None
    assert [r.id for r in env.store.history()] == ["r0001", "r0000"]
    assert receipt.warnings == []


def test_a_dialog_in_desktop_refuses_the_edit_and_leaves_desktop_alone(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env)
    env.desktop.events.clear()
    env.desktop.blocking = ["Save changes?"]
    with pytest.raises(EditRejected, match=r"showing a dialog \(Save changes\?\)"):
        env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert env.desktop.events == []
    assert env.desktop.find(env.working) is not None
    assert env.store.head().id == "r0000"


def test_desktop_that_cannot_be_closed_is_reported_and_never_killed(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env)
    env.desktop.fail_close = DesktopSessionError("Power BI Desktop did not close.")
    with pytest.raises(EditRejected, match="did not close"):
        env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert env.desktop.find(env.working) is not None
    assert env.store.head().id == "r0000"
    assert last_states(env) == ["planned", "failed"]


def test_desktops_normalization_of_the_placed_file_is_not_a_revision(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    env.desktop.open(env.working, normalize=True)  # opened and normalized, then closed by the user
    env.desktop.user_closes(env.working)
    assert sha(env.working) != first.revision.blob
    second = env.editor.apply(
        first.report, [SetPosition(visual=VISUAL, x=60)], expected_head="r0001"
    )
    assert second.adopted is None and second.warnings == []
    assert [r.id for r in env.store.history()] == ["r0002", "r0001", "r0000"]
    assert visual_x(head_path(env)) == 60.0


def test_a_save_the_user_made_while_desktop_was_closed_is_adopted_by_sync(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env, normalize=False)
    env.desktop.user_edit(
        env.working, OTHER_VISUAL_PART, lambda v: v["position"].__setitem__("x", 777.0)
    )
    env.desktop.user_saves(env.working)
    env.desktop.user_closes(env.working)
    updated = env.editor.sync_desktop(env.report)
    assert updated is not None
    head = env.store.head()
    assert (head.id, head.author, head.parent) == ("r0001", "desktop", "r0000")
    assert visual_x(head_path(env), OTHER_VISUAL_PART) == 777.0
    assert env.rederived == [(head.blob, True)]
    assert env.editor.sync_desktop(env.report) is None


def test_sync_does_nothing_while_desktop_is_open(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env)
    assert env.editor.sync_desktop(env.report) is None
    assert env.rederived == []


def test_a_stale_expected_head_is_a_conflict_and_changes_nothing(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    open_desktop(env)
    env.desktop.events.clear()
    records = journal(env).records()
    with pytest.raises(RevisionConflict) as caught:
        env.editor.apply(first.report, [SetPosition(visual=VISUAL, x=70)], expected_head="r0000")
    assert (caught.value.expected, caught.value.actual) == ("r0000", "r0001")
    assert env.store.head().id == "r0001"
    assert visual_x(head_path(env)) == 50.0
    # A stale caller is turned away before Desktop is closed for nothing.
    assert env.desktop.events == []
    assert journal(env).records() == records


def test_two_edits_from_the_same_head_produce_one_conflict(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    results: list[Any] = []
    barrier = threading.Barrier(2)

    def worker(x: float) -> None:
        barrier.wait()
        try:
            results.append(
                env.editor.apply(
                    env.report, [SetPosition(visual=VISUAL, x=x)], expected_head="r0000"
                )
            )
        except RevisionConflict as error:
            results.append(error)

    threads = [threading.Thread(target=worker, args=(x,)) for x in (10.0, 20.0)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(type(r).__name__ for r in results) == ["EditReceipt", "RevisionConflict"]
    assert env.store.head().id == "r0001"
    assert len(env.store.history()) == 2


def test_a_desktop_save_that_removes_the_edits_target_makes_the_edit_a_conflict(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)
    open_desktop(env, normalize=False)
    env.desktop.user_removes(
        env.working, "Report/definition/pages/ReportSection8454c812b60b8bb83b99/visuals/" + VISUAL
    )
    with pytest.raises(RevisionConflict) as caught:
        env.editor.apply(env.report, [MOVE], expected_head="r0000", desktop_work="save")
    assert (caught.value.expected, caught.value.actual) == ("r0000", "r0001")
    assert env.store.head().author == "desktop"  # the user's work is kept in the history
    assert VISUAL_PART not in part_names(head_path(env))
    assert env.desktop.find(env.working) is not None  # Desktop is back, on the adopted head


def test_a_bad_edit_is_refused_before_desktop_is_disturbed(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env)
    env.desktop.events.clear()
    with pytest.raises(EditRejected, match="There is no visual 'nope'"):
        env.editor.apply(env.report, [MOVE, SetPosition(visual="nope", x=1)], expected_head="r0000")
    assert env.desktop.events == []
    assert env.store.head().id == "r0000"
    assert journal(env).records() == []
    assert list(env.store.staging_dir.glob("*")) == []


@pytest.mark.parametrize(
    "raised",
    [
        NeedsConfirmation("replace_conditional", "This replaces the conditional color."),
        NeedsChoice("Which chart?", [Choice("Sales by channel"), Choice("MTD Sales by Group")]),
    ],
)
def test_a_question_from_an_operation_reaches_the_caller_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raised: Exception
) -> None:
    """The service answers "confirm" or "choose" from the type, so the editor must not turn
    it into a plain refusal, and Desktop is left alone."""
    env = make_env(tmp_path)
    open_desktop(env)
    env.desktop.events.clear()

    def refuse(*_: Any, **__: Any) -> Any:
        raise raised

    monkeypatch.setattr(report_editor, "apply_operations", refuse)
    with pytest.raises(type(raised)) as caught:
        env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert caught.value is raised
    assert env.desktop.events == []
    assert env.store.head().id == "r0000"
    assert list(env.store.staging_dir.glob("*")) == []


def test_an_empty_batch_is_refused(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    with pytest.raises(EditRejected, match="nothing to change"):
        env.editor.apply(env.report, [], expected_head="r0000")


# --- idempotency -------------------------------------------------------------------------------


def test_a_repeated_key_returns_the_recorded_receipt_and_does_nothing_again(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first = env.editor.apply(
        env.report, [MOVE], expected_head="r0000", idempotency_key="client-retry-1"
    )
    again = env.editor.apply(
        env.report, [MOVE], expected_head="r0000", idempotency_key="client-retry-1"
    )
    assert again.replayed is True and first.replayed is False
    assert again.revision == first.revision
    assert again.summaries == first.summaries
    assert again.batch_id == first.batch_id
    assert [r.id for r in env.store.history()] == ["r0001", "r0000"]
    assert len(env.rederived) == 2  # the replay re-derives so the caller's view is current
    assert env.desktop.events == []


def test_a_new_key_with_a_stale_head_is_a_conflict_not_a_replay(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.editor.apply(env.report, [MOVE], expected_head="r0000", idempotency_key="one")
    with pytest.raises(RevisionConflict):
        env.editor.apply(env.report, [MOVE], expected_head="r0000", idempotency_key="two")


def test_a_failed_attempt_may_be_retried_with_the_same_key(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env, normalize=False)
    with pytest.raises(UnsavedDesktopWork):
        env.editor.apply(env.report, [MOVE], expected_head="r0000", idempotency_key="same")
    receipt = env.editor.apply(
        env.report, [MOVE], expected_head="r0000", idempotency_key="same", desktop_work="save"
    )
    assert receipt.replayed is False and receipt.revision.id == "r0001"


# --- disk space (P20) --------------------------------------------------------------------------


def test_too_little_space_on_the_state_drive_is_refused_before_anything_starts(
    tmp_path: Path,
) -> None:
    size = 0

    def free(path: Path) -> int:
        return size

    env = make_env(tmp_path, disk_free=free)
    open_desktop(env)
    env.desktop.events.clear()
    with pytest.raises(EditRejected, match="not enough free disk space to change this report"):
        env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert env.desktop.events == []
    assert journal(env).records() == []


def test_the_profile_drive_needs_twice_the_data_models_size(tmp_path: Path) -> None:
    with zipfile.ZipFile(TWIN) as twin:
        model_size = twin.getinfo("DataModel").file_size
    state_root = tmp_path / "state"

    def free(path: Path) -> int:
        return 10**12 if Path(path).is_relative_to(state_root) else 2 * model_size - 1

    env = make_env(tmp_path, disk_free=free)
    with pytest.raises(EditRejected, match="user profile"):
        env.editor.apply(env.report, [MOVE], expected_head="r0000")

    ok = ReportEditor(
        state_root,
        lambda r, p, b, m: r,
        lambda: env.desktop,
        disk_free=lambda path: 10**12 if Path(path).is_relative_to(state_root) else 2 * model_size,
        profile_dir=tmp_path,
    )
    assert ok.apply(env.report, [MOVE], expected_head="r0000").revision.id == "r0001"


# --- post-commit problems never turn the answer into a failure ---------------------------------


def test_a_view_that_cannot_refresh_after_the_head_moved_is_partial_not_failed(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)

    def broken(report: Any, path: Path, blob: str, model_changed: bool) -> Any:
        raise RuntimeError("the snapshot is locked")

    editor = ReportEditor(
        tmp_path / "state", broken, lambda: env.desktop, disk_free=lambda p: 10**12
    )
    receipt = editor.apply(env.report, [MOVE], expected_head="r0000")
    assert receipt.status == "partial"
    assert receipt.report is env.report
    assert (
        "could not refresh its view of the report (the snapshot is locked)" in receipt.warnings[0]
    )
    assert env.store.head().id == "r0001"
    assert journal(env).batches()[receipt.batch_id].state == "done"


def test_a_working_copy_that_cannot_be_refreshed_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path)

    def busy(self: Any, *, force: bool = False) -> bool:
        raise report_revisions.WorkingCopyBusy("Desktop has this report open")

    monkeypatch.setattr(report_revisions.RevisionStore, "materialize", busy)
    receipt = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert receipt.status == "partial"
    assert "could not be refreshed yet (Desktop has this report open)" in receipt.warnings[0]
    assert env.store.head().id == "r0001"


def test_desktop_that_will_not_reopen_leaves_a_warning_not_a_failure(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env)
    env.desktop.fail_open = DesktopSessionError("Power BI Desktop did not open the report in time")
    receipt = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert receipt.status == "updated" and receipt.desktop == "closed"
    assert "could not be reopened" in receipt.warnings[0]
    assert env.store.head().id == "r0001"


def test_pruning_runs_after_every_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path)
    calls: list[int] = []
    real = report_revisions.RevisionStore.prune

    def spy(self: Any, *args: Any, **kwargs: Any) -> list[str]:
        calls.append(1)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(report_revisions.RevisionStore, "prune", spy)
    env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert calls == [1]


def test_thin_reports_take_report_edits_with_a_warning(tmp_path: Path) -> None:
    env = make_env(tmp_path, thin=True)
    receipt = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert receipt.status == "updated"
    assert receipt.warnings == [
        "This report has no model of its own, so Studio cannot check these changes against "
        "the data. Check them in Power BI Desktop."
    ]
    assert visual_x(head_path(env)) == 50.0


def test_thin_reports_refuse_measure_operations_before_touching_desktop(tmp_path: Path) -> None:
    env = make_env(tmp_path, thin=True)
    env.report = env.report.model_copy(
        update={"tables": [{"TableName": "T", "ColumnName": "c", "PandasDataType": "int"}]}
    )
    open_desktop(env)
    env.desktop.events.clear()
    with pytest.raises(EditRejected, match="reads its data from a shared Power BI model"):
        env.editor.apply(
            env.report,
            [CreateMeasure(table="T", name="M", expression="1")],
            expected_head="r0000",
        )
    assert env.desktop.events == []
    assert env.store.head().id == "r0000"


# --- batches with measure operations -----------------------------------------------------------


def test_creating_a_measure_is_one_revision_holding_desktops_saved_file(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    receipt = env.editor.apply(env.report, [CREATE], expected_head="r0000")
    head = env.store.head()
    assert (head.id, head.author, receipt.status, receipt.model_changed) == (
        "r0001",
        "studio",
        "updated",
        True,
    )
    assert measure_names(head_path(env)) == [
        "Rank",
        "Employees Avg MTD Sales",
        "dummy",
        "Avg Discount",
    ]
    assert receipt.summaries == ["Created measure [Avg Discount] in # Measures."]
    assert head.operations[0]["op"] == "create_measure" and head.desktop_version == DESKTOP_VERSION
    # Only the model changed, so Desktop's own file is the revision (it keeps its signature).
    assert "SecurityBindings" in part_names(head_path(env))
    assert env.desktop.events == [
        "open normalize=True",
        "ops applied",
        "save",
        "close",
    ]
    assert env.desktop.find(env.working) is None and receipt.desktop == "closed"
    assert sha(env.working) == head.blob
    assert env.store.placed() == ("r0001", head.blob)
    assert env.rederived == [(head.blob, True)]
    assert [m["Name"] for m in receipt.report.measures][-1] == "Avg Discount"
    assert last_states(env) == [
        "planned",
        "model_applied",
        "model_saved",
        "adopted",
        "staged",
        "committed",
        "placed",
        "done",
    ]


def test_a_rename_rewrites_the_reports_references_on_the_saved_model(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env)
    env.desktop.events.clear()
    assert measure_refs(env.store.original, "Rank") == 2
    receipt = env.editor.apply(env.report, [RENAME], expected_head="r0000")
    head = env.store.head()
    assert measure_refs(head_path(env), "Rank") == 0
    assert measure_refs(head_path(env), "Agent Rank") == 2
    assert measure_names(head_path(env)) == ["Agent Rank", "Employees Avg MTD Sales", "dummy"]
    assert receipt.summaries == [
        "Renamed measure [Rank] to [Agent Rank] and updated the visuals that use it."
    ]
    assert (head.id, len(env.store.history())) == ("r0001", 2)  # ONE head move for both layers
    assert env.desktop.events == [
        "close",
        "open normalize=True",
        "ops applied",
        "save",
        "close",
        "open normalize=True",
    ]
    assert receipt.desktop == "reopened"
    assert "SecurityBindings" not in part_names(head_path(env))


def test_a_new_measure_and_a_card_that_shows_it_land_in_one_revision(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    card = AddVisual(
        page="1", visual_type="card", fields=[FieldRef(table=MEASURE_TABLE, name="Avg Discount")]
    )
    receipt = env.editor.apply(env.report, [CREATE, card], expected_head="r0000")
    assert receipt.status == "updated"
    assert len(env.store.history()) == 2
    assert "Avg Discount" in measure_names(head_path(env))
    assert measure_refs(head_path(env), "Avg Discount") >= 1
    assert receipt.summaries == [
        "Created measure [Avg Discount] in # Measures.",
        "Added a card of Avg Discount to page '1'.",
    ]


def test_updating_and_deleting_measures_persist(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    receipt = env.editor.apply(env.report, [CREATE], expected_head="r0000")
    update = UpdateMeasure(name="Avg Discount", expression="2")
    receipt = env.editor.apply(receipt.report, [update], expected_head="r0001")
    expressions = {m["Name"]: m["Expression"] for m in read_measures(head_path(env))}
    assert expressions["Avg Discount"] == "2"
    receipt = env.editor.apply(
        receipt.report, [DeleteMeasure(name="Avg Discount")], expected_head="r0002"
    )
    assert measure_names(head_path(env)) == ["Rank", "Employees Avg MTD Sales", "dummy"]
    assert receipt.report.measures == read_measures(head_path(env))
    assert [r.author for r in env.store.history()] == ["studio"] * 3 + ["import"]


def test_a_measure_desktop_rejects_rolls_back_and_leaves_the_head_alone(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.desktop.fail_ops = {"create_measure"}
    with pytest.raises(EditRejected, match=r"did not accept the change.*has an error") as caught:
        env.editor.apply(env.report, [CREATE], expected_head="r0000")
    assert caught.value.status == "failed"
    assert env.store.head().id == "r0000"
    assert env.desktop.events == ["open normalize=True", "ops rolled back", "save", "close"]
    assert env.desktop.find(env.working) is None
    assert sha(env.working) == env.store.head().blob  # the head is back in the working copy
    assert list(env.store.staging_dir.glob("*")) == []
    assert last_states(env) == ["planned", "rolled_back"]
    assert env.rederived == []


def test_a_rejected_measure_reopens_desktop_when_it_was_open(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env)
    env.desktop.fail_ops = {"create_measure"}
    with pytest.raises(EditRejected):
        env.editor.apply(env.report, [CREATE], expected_head="r0000")
    instance = env.desktop.find(env.working)
    assert instance is not None and env.desktop.status(instance)["hasUnsavedChanges"] is False
    assert env.store.head().id == "r0000"
    assert env.editor.working_copy_state(env.report) == "placed"


def test_desktop_that_cannot_close_after_a_rollback_is_left_open_and_reported(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)
    env.desktop.fail_ops = {"create_measure"}
    env.desktop.fail_close = DesktopSessionError("Power BI Desktop did not close.")
    with pytest.raises(EditRejected, match="could not be closed cleanly.*Don't save"):
        env.editor.apply(env.report, [CREATE], expected_head="r0000")
    assert env.desktop.find(env.working) is not None  # never killed
    assert env.store.head().id == "r0000"
    assert last_states(env)[-1] == "rolled_back"


def test_a_dialog_while_a_model_batch_closes_desktop_stays_a_typed_refusal(tmp_path: Path) -> None:
    ask = DesktopDialogOpen(
        "Power BI Desktop is asking a question before it closes (Save changes?).", "Save changes?"
    )
    # while saving and closing after the measures were applied
    env = make_env(tmp_path / "closing")
    env.desktop.fail_close = ask
    with pytest.raises(DesktopDialog, match="could not finish saving and closing") as closing:
        env.editor.apply(env.report, [CREATE], expected_head="r0000")
    assert closing.value.dialog == "Save changes?"
    assert env.store.head().id == "r0000"
    # while closing after Desktop rejected a measure
    env = make_env(tmp_path / "rollback")
    env.desktop.fail_ops = {"create_measure"}
    env.desktop.fail_close = ask
    with pytest.raises(DesktopDialog, match="could not be closed cleanly") as rolled:
        env.editor.apply(env.report, [CREATE], expected_head="r0000")
    assert rolled.value.dialog == "Save changes?"
    # while the measures are applied
    env = make_env(tmp_path / "applying")

    def blocked(*_args: Any, **_kwargs: Any) -> list[str]:
        raise DesktopDialogOpen("Power BI Desktop is showing a dialog (Sign in).", "Sign in")

    env.desktop.apply_measure_operations = blocked  # type: ignore[method-assign]
    with pytest.raises(DesktopDialog, match="did not accept the change") as applying:
        env.editor.apply(env.report, [CREATE], expected_head="r0000")
    assert applying.value.dialog == "Sign in"
    assert env.store.head().id == "r0000"


def test_a_desktop_that_will_not_open_leaves_the_history_alone(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.desktop.fail_open = DesktopSessionError("Power BI Desktop did not open the report in time")
    with pytest.raises(EditRejected, match="did not open the report"):
        env.editor.apply(env.report, [CREATE], expected_head="r0000")
    assert env.store.head().id == "r0000"
    assert sha(env.working) == env.store.head().blob


def test_a_property_desktop_drops_while_saving_fails_the_batch_and_names_it(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.desktop.drop_on_save = [(VISUAL_PART, "position")]
    with pytest.raises(
        EditRejected, match="dropped part of the report.*visuals/54be5cf0783e66471540"
    ):
        env.editor.apply(env.report, [CREATE], expected_head="r0000")
    assert env.store.head().id == "r0000"
    assert env.desktop.find(env.working) is None
    assert sha(env.working) == env.store.head().blob
    assert list(env.store.staging_dir.glob("*")) == []
    assert last_states(env)[-1] == "rolled_back"


def test_a_model_change_desktop_did_not_persist_fails_the_batch(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.desktop.keep_measures = False
    with pytest.raises(EditRejected, match="saved a different set of measures"):
        env.editor.apply(env.report, [CREATE], expected_head="r0000")
    assert env.store.head().id == "r0000"
    assert sha(env.working) == env.store.head().blob


def test_measures_that_cannot_be_read_back_fail_the_batch_and_restore_the_head(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)

    reads: list[Path] = []

    def unreadable(path: Path) -> list[dict[str, Any]]:
        reads.append(path)
        if len(reads) == 1:  # the head's own measures, read before Desktop is opened
            return read_measures(path)
        raise EditRejected("Studio could not read the measures back from the file Power BI saved")

    editor = ReportEditor(
        tmp_path / "state",
        lambda r, p, b, m: r,
        lambda: env.desktop,
        read_measures=unreadable,
        disk_free=lambda p: 10**12,
    )
    with pytest.raises(EditRejected, match="could not read the measures back"):
        editor.apply(env.report, [CREATE], expected_head="r0000")
    assert env.store.head().id == "r0000"
    assert sha(env.working) == env.store.head().blob


def test_a_rename_that_would_leave_a_visual_without_its_measure_is_refused(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)
    # Desktop "renames" to a different name than the one the report layer was told.
    original = env.desktop.apply_measure_operations

    def wrong_name(instance: Any, operations: Any, tables: Any) -> list[str]:
        done = original(instance, operations, tables)
        for inst in env.desktop.instances.values():
            for row in inst.measures:
                if row["Name"] == "Agent Rank":
                    row["Name"] = "Some Other Name"
        return done

    env.desktop.apply_measure_operations = wrong_name  # type: ignore[method-assign]
    with pytest.raises(EditRejected, match="saved a different set of measures"):
        env.editor.apply(env.report, [RENAME], expected_head="r0000")
    assert env.store.head().id == "r0000"
    assert measure_refs(head_path(env), "Rank") == 2


def test_a_delete_of_a_measure_a_visual_uses_is_refused_by_the_dry_run(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env)
    env.desktop.events.clear()
    with pytest.raises(EditRejected, match=r"\[Rank\] is used by"):
        env.editor.apply(env.report, [DeleteMeasure(name="Rank")], expected_head="r0000")
    assert env.desktop.events == []


def test_a_model_batch_asks_before_touching_unsaved_desktop_work(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env, normalize=False)
    with pytest.raises(UnsavedDesktopWork):
        env.editor.apply(env.report, [CREATE], expected_head="r0000")
    assert env.desktop.events == ["open normalize=False"]
    assert env.store.head().id == "r0000"


def test_a_measure_batch_can_be_replayed_by_its_key(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first = env.editor.apply(env.report, [CREATE], expected_head="r0000", idempotency_key="m-1")
    events = list(env.desktop.events)
    again = env.editor.apply(env.report, [CREATE], expected_head="r0000", idempotency_key="m-1")
    assert again.replayed and again.revision == first.revision and again.model_changed
    assert env.desktop.events == events
    assert len(env.store.history()) == 2


def test_the_model_view_is_wound_back_by_the_batchs_own_operations() -> None:
    pre = ModelInfo.from_report(
        [{"TableName": "T", "ColumnName": "c", "PandasDataType": "int"}],
        [
            {"Name": "Keep", "TableName": "T"},
            {"Name": "Old", "TableName": "T"},
            {"Name": "Gone", "TableName": "T"},
        ],
    )
    ops = [
        CreateMeasure(table="T", name="Fresh", expression="1"),
        RenameMeasure(name="Old", new_name="New"),
        DeleteMeasure(name="Gone"),
        RenameMeasure(name="Fresh", new_name="Fresher"),
    ]
    saved = ModelInfo.from_report(
        pre.columns and [{"TableName": "T", "ColumnName": "c", "PandasDataType": "int"}] or [],
        [
            {"Name": "Keep", "TableName": "T"},
            {"Name": "New", "TableName": "T"},
            {"Name": "Fresher", "TableName": "T"},
        ],
    )
    wound = report_editor._model_before(pre, saved, ops)
    assert wound.measures == {"Keep": "T", "Old": "T", "Gone": "T"}
    assert pre.measures == {"Keep": "T", "Old": "T", "Gone": "T"}  # the input is untouched
    assert report_editor._measure_map(saved) == {"keep": "t", "new": "t", "fresher": "t"}


def test_measures_are_read_in_a_child_process_and_failures_are_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(INITIAL_MEASURES), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert report_editor.read_measures_subprocess(tmp_path / "x.pbix") == INITIAL_MEASURES
    assert calls[0][-2:] == [str(tmp_path / "x.pbix"), "--measures"]
    assert calls[0][1].endswith("pbix_extract.py")

    def failing(argv: list[str], **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", failing)
    with pytest.raises(EditRejected, match="could not read the measures back"):
        report_editor.read_measures_subprocess(tmp_path / "x.pbix")


# --- undo and restore --------------------------------------------------------------------------


def test_undo_reverts_only_the_newest_change_through_the_pipeline(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    second = env.editor.apply(
        first.report, [SetPosition(visual=VISUAL, x=70)], expected_head="r0001"
    )
    assert visual_x(head_path(env)) == 70.0
    receipt = env.editor.undo(second.report, expected_head="r0002")
    head = env.store.head()
    assert (head.id, head.author, head.restores, head.blob) == (
        "r0003",
        "undo",
        "r0001",
        first.revision.blob,
    )
    assert visual_x(head_path(env)) == 50.0
    assert sha(env.working) == head.blob
    assert receipt.summaries == [head.summary]
    assert env.rederived[-1] == (head.blob, True)
    assert last_states(env) == ["planned", "committed", "placed", "done"]
    with pytest.raises(RevisionConflict):
        env.editor.undo(receipt.report, expected_head="r0002")  # only the newest change


def test_undo_with_nothing_to_undo(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    with pytest.raises(RevisionError, match="nothing to undo"):
        env.editor.undo(env.report, expected_head="r0000")
    # Refused before a batch started: no journal line, Desktop never disturbed.
    assert journal(env).batches() == {}


def test_undo_asks_before_touching_unsaved_desktop_work(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    open_desktop(env, normalize=False)
    with pytest.raises(UnsavedDesktopWork):
        env.editor.undo(first.report, expected_head="r0001")
    assert env.store.head().id == "r0001"


def test_undo_after_a_real_desktop_save_reports_the_new_head_instead_of_undoing_it(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)
    first = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    open_desktop(env, normalize=False)
    env.desktop.user_edit(
        env.working, OTHER_VISUAL_PART, lambda v: v["position"].__setitem__("x", 11.0)
    )
    with pytest.raises(RevisionConflict) as caught:
        env.editor.undo(first.report, expected_head="r0001", desktop_work="save")
    assert (caught.value.expected, caught.value.actual) == ("r0001", "r0002")
    assert env.store.head().author == "desktop"
    assert env.desktop.find(env.working) is not None


def test_restore_lists_what_it_leaves_out_and_needs_confirmation(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    open_desktop(env, normalize=False)
    env.desktop.user_edit(
        env.working, OTHER_VISUAL_PART, lambda v: v["position"].__setitem__("x", 11.0)
    )
    env.desktop.user_saves(env.working)
    env.desktop.user_closes(env.working)
    env.editor.sync_desktop(first.report)  # r0002 is the user's Desktop save
    third = env.editor.apply(
        first.report, [SetPosition(visual=VISUAL, x=90)], expected_head="r0002"
    )
    preview = env.editor.restore_preview(third.report, "r0001")
    assert [(r.id, r.author) for r in preview] == [("r0003", "studio"), ("r0002", "desktop")]
    with pytest.raises(RestoreNeedsConfirmation) as caught:
        env.editor.restore(third.report, "r0001", expected_head="r0003")
    assert [r.id for r in caught.value.later] == ["r0003", "r0002"]
    assert "removes 2 later changes" in str(caught.value)
    assert env.store.head().id == "r0003"
    receipt = env.editor.restore(
        third.report, "r0001", expected_head="r0003", confirmed_discard=True
    )
    head = env.store.head()
    assert (head.id, head.author, head.blob) == ("r0004", "restore", first.revision.blob)
    assert visual_x(head_path(env)) == 50.0 and visual_x(head_path(env), OTHER_VISUAL_PART) != 11.0
    assert [r.id for r in env.store.history()][-4:] == ["r0003", "r0002", "r0001", "r0000"]
    assert receipt.revision == head and sha(env.working) == head.blob


def test_restoring_a_version_with_nothing_after_it_needs_no_confirmation(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    receipt = env.editor.restore(
        first.report, "r0000", expected_head="r0001", confirmed_discard=True
    )
    assert receipt.revision.blob == env.store.original_hash
    third = env.editor.apply(receipt.report, [MOVE], expected_head="r0002")
    assert env.editor.restore_preview(third.report, "r0002") == [env.store.head()]


def test_a_desktop_save_found_while_restoring_needs_confirmation_too(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    undone = env.editor.undo(first.report, expected_head="r0001")
    # The change is undone, so from the history alone restoring r0000 leaves nothing out.
    assert env.editor.restore_preview(undone.report, "r0000") == []
    open_desktop(env, normalize=False)
    env.desktop.user_edit(
        env.working, OTHER_VISUAL_PART, lambda v: v["position"].__setitem__("x", 12.0)
    )
    with pytest.raises(RestoreNeedsConfirmation) as caught:
        env.editor.restore(undone.report, "r0000", expected_head="r0002", desktop_work="save")
    assert [(r.id, r.author) for r in caught.value.later] == [("r0003", "desktop")]
    assert env.store.head().author == "desktop"  # the user's work was kept, not discarded
    assert env.desktop.find(env.working) is not None  # and Desktop is back


def test_restore_of_an_unknown_or_current_version_is_an_error(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.editor.apply(env.report, [MOVE], expected_head="r0000")
    with pytest.raises(RevisionError, match="no version 'r0009'"):
        env.editor.restore_preview(env.report, "r0009")
    with pytest.raises(RevisionError, match="already the current version"):
        env.editor.restore(env.report, "r0001", expected_head="r0001", confirmed_discard=True)


def test_undo_can_be_replayed_by_its_key(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    one = env.editor.undo(first.report, expected_head="r0001", idempotency_key="undo-1")
    two = env.editor.undo(first.report, expected_head="r0001", idempotency_key="undo-1")
    assert two.replayed and two.revision == one.revision
    assert len(env.store.history()) == 3


# --- the working copy --------------------------------------------------------------------------


def test_open_in_desktop_opens_the_head_not_a_stale_copy(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.editor.apply(env.report, [MOVE], expected_head="r0000")
    staged = env.store.staging_path()
    tiny_pbix(staged, "a head the working copy has not seen")
    row = env.store.commit(
        staged, author="studio", summary="x", operations=[], expected_head="r0001"
    )
    assert env.store.working_copy_state() == "placed_stale"
    instance = env.editor.open_in_desktop(env.report)
    assert instance.path == env.working.resolve()
    assert env.desktop.events == ["open normalize=True"]
    placed = env.store.placed()
    assert placed is not None and placed[0] == row.id


def test_open_in_desktop_creates_the_working_copy_when_it_is_missing(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    assert not env.working.exists()
    env.editor.open_in_desktop(env.report)
    assert env.desktop.find(env.working) is not None
    assert env.editor.working_copy_state(env.report) == "placed"


def test_open_in_desktop_adopts_a_save_made_while_desktop_was_closed(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env, normalize=False)
    env.desktop.user_edit(
        env.working, OTHER_VISUAL_PART, lambda v: v["position"].__setitem__("x", 31.0)
    )
    env.desktop.user_saves(env.working)
    env.desktop.user_closes(env.working)
    env.editor.open_in_desktop(env.report)
    assert env.store.head().author == "desktop"
    assert env.rederived and env.rederived[-1] == (env.store.head().blob, True)
    assert env.desktop.find(env.working) is not None


def test_open_in_desktop_reuses_an_instance_that_is_already_open(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first = open_desktop(env)
    assert env.editor.open_in_desktop(env.report) == first
    assert env.desktop.events == ["open normalize=True"]


def test_a_desktop_screenshot_only_shows_exactly_the_head(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    output = tmp_path / "page.png"
    with pytest.raises(EditRejected, match="Open the report in Power BI first"):
        env.editor.desktop_screenshot(env.report, "p1", output)
    open_desktop(env)
    assert env.editor.desktop_screenshot(env.report, "p1", output) == output
    assert output.is_file()
    env.desktop.user_edit(env.working, VISUAL_PART, lambda v: v["position"].__setitem__("x", 1.0))
    with pytest.raises(EditRejected, match="unsaved changes"):
        env.editor.desktop_screenshot(env.report, "p1", output)
    env.desktop.user_saves(env.working)
    # Saved but not recorded in the history: Desktop shows something Studio does not have.
    with pytest.raises(EditRejected, match="different version"):
        env.editor.desktop_screenshot(env.report, "p1", output)


def test_a_screenshot_of_an_older_head_is_refused(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env)
    staged = env.store.staging_path()
    tiny_pbix(staged, "the head moved on while Desktop showed r0000")
    env.store.commit(staged, author="studio", summary="x", operations=[], expected_head="r0000")
    with pytest.raises(EditRejected, match="different version"):
        env.editor.desktop_screenshot(env.report, "p1", tmp_path / "page.png")


# --- delivery through the editor ---------------------------------------------------------------


def test_save_copy_and_replace_original_through_the_editor(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    exports = tmp_path / "Exports"
    exports.mkdir()
    copy = env.editor.save_copy(first.report, exports)
    assert copy == exports / "Agents (edited).pbix" and sha(copy) == first.revision.blob
    assert env.editor.save_copy(first.report, exports).name == "Agents (edited) (2).pbix"
    assert env.editor.delivery_state(first.report) == "edited"
    assert env.editor.pending_changes(first.report) == 1
    result = env.editor.replace_original(first.report)
    assert sha(env.source) == first.revision.blob and result.backup.is_file()
    assert env.editor.delivery_state(first.report) == "in_sync"
    with pytest.raises(EditRejected, match="changed outside Studio"):
        make_zip(env.source, {"x": b"the user's newer work"})
        env.editor.replace_original(first.report)


def test_delivery_adopts_a_desktop_save_and_refreshes_studios_view(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env, normalize=False)
    env.desktop.user_edit(
        env.working, OTHER_VISUAL_PART, lambda v: v["position"].__setitem__("x", 41.0)
    )
    env.desktop.user_saves(env.working)
    env.desktop.user_closes(env.working)
    exports = tmp_path / "Exports"
    exports.mkdir()
    copy = env.editor.save_copy(env.report, exports)
    assert env.store.head().author == "desktop"
    assert sha(copy) == env.store.head().blob
    assert env.rederived == [(env.store.head().blob, True)]


def test_delivery_refuses_while_desktop_is_dirty_and_says_so_in_plain_words(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env, normalize=False)
    with pytest.raises(EditRejected, match="unsaved changes"):
        env.editor.save_copy(env.report, tmp_path)


# --- crash recovery ----------------------------------------------------------------------------


REPORT_ONLY_CRASHES = [
    # (state the process died at, what recovery does, head afterwards)
    ("planned", "left_open", "r0000"),
    ("desktop_closed", "rolled_back", "r0000"),
    ("staged", "rolled_back", "r0000"),
    ("committed", "rolled_forward", "r0001"),
    ("placed", "rolled_forward", "r0001"),
    ("reopened", "rolled_forward", "r0001"),
]


@pytest.mark.parametrize(("state", "action", "head"), REPORT_ONLY_CRASHES)
def test_a_crash_between_any_two_steps_of_a_report_batch_recovers(
    tmp_path: Path, state: str, action: str, head: str
) -> None:
    env = make_env(tmp_path)
    open_desktop(env)
    env.crash_at.append(state)
    with pytest.raises(Crash):
        env.editor.apply(env.report, [MOVE], expected_head="r0000", idempotency_key="crash-key")
    env.crash_at.clear()

    result = env.editor.recover(env.report)  # the sidecar starts again

    assert [b.action for b in result.batches] == [action]
    assert env.store.head().id == head
    assert journal(env).open_batches() == []
    assert list(env.store.staging_dir.glob("*")) == []
    holder = env.desktop.find(env.working)
    assert result.desktop == holder  # a running Desktop is found again, never closed
    if holder is None:
        assert env.editor.working_copy_state(env.report) == "placed"
        if action == "rolled_forward":
            assert sha(env.working) == env.store.head().blob
    expected_x = 50.0 if head == "r0001" else None
    if expected_x is not None:
        assert visual_x(head_path(env)) == expected_x
    # The client retries with the same key: it never applies twice.
    receipt = env.editor.apply(
        env.report, [MOVE], expected_head="r0000", idempotency_key="crash-key"
    )
    assert env.store.head().id == "r0001" and len(env.store.history()) == 2
    assert receipt.replayed is (head == "r0001")
    assert visual_x(head_path(env)) == 50.0


def test_an_unfinished_batch_is_recovered_when_the_same_key_comes_back(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.crash_at.append("committed")
    with pytest.raises(Crash):
        env.editor.apply(env.report, [MOVE], expected_head="r0000", idempotency_key="k")
    env.crash_at.clear()
    receipt = env.editor.apply(env.report, [MOVE], expected_head="r0000", idempotency_key="k")
    assert receipt.replayed and receipt.revision.id == "r0001"
    assert sha(env.working) == receipt.revision.blob
    assert len(env.store.history()) == 2


MODEL_CRASHES = [
    ("model_applied", "left_open", "r0000"),
    ("model_saved", "left_open", "r0000"),
    ("adopted", "rolled_back", "r0000"),
    ("staged", "rolled_back", "r0000"),
    ("committed", "rolled_forward", "r0001"),
]


@pytest.mark.parametrize(("state", "action", "head"), MODEL_CRASHES)
def test_a_crash_between_any_two_steps_of_a_measure_batch_recovers(
    tmp_path: Path, state: str, action: str, head: str
) -> None:
    env = make_env(tmp_path)
    env.crash_at.append(state)
    with pytest.raises(Crash):
        env.editor.apply(env.report, [RENAME], expected_head="r0000", idempotency_key="model-key")
    env.crash_at.clear()

    result = env.editor.recover(env.report)

    assert [b.action for b in result.batches] == [action]
    assert env.store.head().id == head
    assert journal(env).open_batches() == []
    assert list(env.store.staging_dir.glob("*")) == []
    holder = env.desktop.find(env.working)
    assert result.desktop == holder
    if holder is None:
        assert sha(env.working) == env.store.head().blob  # the by-product never becomes a revision
    if head == "r0001":
        assert measure_refs(head_path(env), "Agent Rank") == 2
        assert measure_refs(head_path(env), "Rank") == 0
    else:
        assert measure_refs(head_path(env), "Rank") == 2


def test_the_next_batch_after_a_crash_that_left_desktop_dirty_asks_first(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.crash_at.append("model_applied")
    with pytest.raises(Crash):
        env.editor.apply(env.report, [CREATE], expected_head="r0000")
    env.crash_at.clear()
    env.editor.recover(env.report)
    with pytest.raises(UnsavedDesktopWork):
        env.editor.apply(env.report, [MOVE], expected_head="r0000")
    # The user chooses "Save into history": the half-applied change is kept as their save.
    receipt = env.editor.apply(env.report, [MOVE], expected_head="r0000", desktop_work="save")
    assert receipt.adopted is not None and receipt.adopted.author == "desktop"
    assert "Avg Discount" in measure_names(head_path(env))


def test_an_undo_that_crashed_after_the_row_was_recorded_is_finished(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    env.crash_at.append("committed")
    with pytest.raises(Crash):
        env.editor.undo(first.report, expected_head="r0001", idempotency_key="u")
    env.crash_at.clear()
    result = env.editor.recover(env.report)
    assert [b.action for b in result.batches] == ["rolled_forward"]
    assert env.store.head().author == "undo"
    assert sha(env.working) == env.store.head().blob


def test_sidecar_restart_keeps_a_running_desktop_alive(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    instance = open_desktop(env)
    env.crash_at.append("committed")
    with pytest.raises(Crash):
        env.editor.apply(env.report, [MOVE], expected_head="r0000")
    # The crash happened after Studio had closed Desktop; the user reopens it themselves
    # before the sidecar comes back.
    assert env.desktop.find(env.working) is None
    reopened = env.desktop.open(env.working, normalize=False)
    env.crash_at.clear()
    result = env.editor.recover(env.report)
    assert result.desktop == reopened != instance
    assert env.desktop.find(env.working) == reopened
    assert [b.action for b in result.batches] == ["rolled_forward"]


def test_survival_blames_only_what_the_last_studio_edit_wrote(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    SURVIVAL_CALLS.clear()
    first = env.editor.apply(env.report, [CREATE], expected_head="r0000")
    written, saved, baseline = SURVIVAL_CALLS[-1]
    # The head was the import, which Studio wrote nothing in: nothing to blame.
    assert written == baseline == env.store.original
    assert saved.parent == env.store.staging_dir
    second_head = env.editor.apply(first.report, [MOVE], expected_head="r0001")
    SURVIVAL_CALLS.clear()
    env.editor.apply(
        second_head.report,
        [CreateMeasure(table=MEASURE_TABLE, name="Another", expression="1")],
        expected_head="r0002",
    )
    written, saved, baseline = SURVIVAL_CALLS[-1]
    # A Studio-written head is compared with the file that edit started from.
    assert written == env.store.path(env.store.revision("r0002"))
    assert baseline == env.store.path(env.store.revision("r0001"))


def test_progress_is_reported_in_the_order_the_user_waits_for_it(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env, normalize=False)
    seen: list[str] = []
    env.editor.apply(
        env.report,
        [CREATE],
        expected_head="r0000",
        desktop_work="save",
        progress=seen.append,
    )
    assert seen == [
        "Saving your Power BI Desktop work",
        "Closing Power BI Desktop",
        "Opening Power BI Desktop",
        "Applying the change in Power BI",
        "Saving",
        "Opening Power BI Desktop",
    ]


def test_history_flags_are_available_through_the_editor(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    receipt = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert [r.id for r in env.editor.history(receipt.report)] == ["r0001", "r0000"]
    assert [
        (e.revision.id, e.is_head, e.can_undo)
        for e in env.editor.history_with_flags(receipt.report)
    ] == [("r0001", True, True), ("r0000", False, False)]


# --- the user's real dashboard (read-only; skipped where it does not exist) ----

REAL_DASHBOARD = Path.home() / "Downloads" / "Agents Performance - Dashboard.pbix"


@pytest.mark.skipif(
    not REAL_DASHBOARD.is_file(), reason="the user's dashboard is not on this machine"
)
def test_a_real_dashboard_round_trips_an_edit_and_a_copy_without_touching_the_model(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path, source_file=REAL_DASHBOARD)
    receipt = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    with zipfile.ZipFile(env.store.original) as before, zipfile.ZipFile(head_path(env)) as after:
        assert after.read("DataModel") == before.read("DataModel")
        assert [i.compress_type for i in after.infolist() if i.filename == "DataModel"] == [0]
        changed = [
            n
            for n in after.namelist()
            if n in before.namelist() and after.read(n) != before.read(n)
        ]
    assert sorted(set(changed) - {"[Content_Types].xml"}) == [VISUAL_PART]
    assert visual_x(head_path(env)) == 50.0
    exports = tmp_path / "Exports"
    exports.mkdir()
    copy = env.editor.save_copy(receipt.report, exports)
    assert sha(copy) == receipt.revision.blob
    assert sha(REAL_DASHBOARD) == env.report.source_hash  # the user's own file is untouched


# --- the second review round -------------------------------------------------------------------

# Captured at import, before any fixture swaps them for the doubles in editing_support.
REAL_SEMANTIC_EQUAL = report_revisions.semantic_equal
REAL_SURVIVAL = report_revisions.survival


@pytest.fixture
def real_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real pbix_diff behind the store's bridge, as in production (no doubles)."""
    monkeypatch.setattr(report_revisions, "semantic_equal", REAL_SEMANTIC_EQUAL)
    monkeypatch.setattr(report_revisions, "survival", REAL_SURVIVAL)


def add_user_measure(env: Env, name: str = "UserMeasure") -> None:
    """The user adds a measure in the open Desktop (unsaved until they save)."""
    inst = env.desktop.instances[env.working.resolve()]
    inst.measures.append({"Name": name, "TableName": MEASURE_TABLE, "Expression": "3"})
    inst.dirty = True


def test_with_the_real_diff_desktops_rewrite_of_the_data_model_is_not_a_change(
    tmp_path: Path, real_diff: None
) -> None:
    env = make_env(tmp_path)
    env.editor.open_in_desktop(env.report)
    # Desktop rewrote the DataModel bytes on opening, and that is still the same report.
    with zipfile.ZipFile(env.store.original) as a, zipfile.ZipFile(env.working) as b:
        assert a.read("DataModel") != b.read("DataModel")
    assert env.editor.working_copy_state(env.report) == "placed"
    shot = env.editor.desktop_screenshot(env.report, "p", tmp_path / "shot.png")
    assert shot == tmp_path / "shot.png"
    receipt = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert receipt.adopted is None and receipt.warnings == [] and receipt.status == "updated"
    assert [r.id for r in env.store.history()] == ["r0001", "r0000"]
    # Closing and syncing the reopened, untouched Desktop makes no revision either.
    env.desktop.user_closes(env.working)
    assert env.editor.sync_desktop(receipt.report) is None
    assert [r.id for r in env.store.history()] == ["r0001", "r0000"]


def test_with_the_real_diff_a_measure_added_in_desktop_is_kept_not_overwritten(
    tmp_path: Path, real_diff: None
) -> None:
    env = make_env(tmp_path)
    env.editor.open_in_desktop(env.report)
    add_user_measure(env)
    env.desktop.user_saves(env.working)
    env.desktop.user_closes(env.working)
    # Only the DataModel differs from the placed revision, and that must not read as
    # "normalization": the report layer is identical but the user's measure is new.
    assert env.editor.working_copy_state(env.report) == "user_changes"
    synced = env.editor.sync_desktop(env.report)
    assert synced is not None
    assert [m["Name"] for m in synced.measures] == [
        "Rank",
        "Employees Avg MTD Sales",
        "dummy",
        "UserMeasure",
    ]
    head = env.store.head()
    assert (head.id, head.author, head.desktop_version) == ("r0001", "desktop", DESKTOP_VERSION)
    assert measure_names(head_path(env))[-1] == "UserMeasure"


def test_a_desktop_save_that_changed_only_the_model_is_adopted_before_an_edit(
    tmp_path: Path, real_diff: None
) -> None:
    env = make_env(tmp_path)
    env.editor.open_in_desktop(env.report)
    add_user_measure(env)
    receipt = env.editor.apply(env.report, [MOVE], expected_head="r0000", desktop_work="save")
    assert receipt.adopted is not None and receipt.adopted.id == "r0001"
    assert env.store.head().id == "r0002"
    assert "UserMeasure" in measure_names(env.store.path(env.store.revision("r0001")))
    assert "UserMeasure" in measure_names(head_path(env))


def test_an_adopted_desktop_save_rebuilds_studios_model_view(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env)
    add_user_measure(env)
    card = AddVisual(
        page="1", visual_type="card", fields=[FieldRef(table=MEASURE_TABLE, name="UserMeasure")]
    )
    # The caller's view still lacks UserMeasure; the head after adoption has it.
    receipt = env.editor.apply(env.report, [card], expected_head="r0000", desktop_work="save")
    assert receipt.adopted is not None and receipt.status == "updated"
    assert receipt.model_changed is True
    assert env.rederived[-1] == (receipt.revision.blob, True)
    assert [m["Name"] for m in receipt.report.measures][-1] == "UserMeasure"
    assert measure_refs(head_path(env), "UserMeasure") >= 1


def test_a_bad_edit_is_still_refused_at_once_when_desktop_holds_no_work(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env)
    env.desktop.events.clear()
    card = AddVisual(
        page="1", visual_type="card", fields=[FieldRef(table=MEASURE_TABLE, name="Nope")]
    )
    with pytest.raises(EditRejected):
        env.editor.apply(env.report, [card], expected_head="r0000")
    assert env.desktop.events == []


def test_a_model_batch_is_judged_against_the_heads_measures_not_a_stale_view(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)
    first = env.editor.apply(env.report, [RENAME], expected_head="r0000")
    assert [m["Name"] for m in first.report.measures][0] == "Agent Rank"
    # A caller that still holds the report from before the rename.
    later = env.editor.apply(env.report, [CREATE], expected_head="r0001")
    assert later.status == "updated"
    assert measure_names(head_path(env)) == [
        "Agent Rank",
        "Employees Avg MTD Sales",
        "dummy",
        "Avg Discount",
    ]


# --- the journal never fails a batch whose head has moved ----------------------------------------


@pytest.mark.parametrize("failing", [("committed",), ("placed",), ("done",), ("committed", "done")])
def test_a_journal_that_cannot_be_written_after_the_head_moved_is_partial_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: tuple[str, ...]
) -> None:
    env = make_env(tmp_path)
    real = EditJournal.append

    def full_disk(self: EditJournal, batch: str, state: str, **data: Any) -> Any:
        if state in failing:
            raise OSError(28, "No space left on device")
        return real(self, batch, state, **data)

    monkeypatch.setattr(EditJournal, "append", full_disk)
    receipt = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert env.store.head().id == "r0001" and receipt.revision.id == "r0001"
    assert receipt.status == "partial"
    assert sum("activity log" in w for w in receipt.warnings) == 1
    assert visual_x(head_path(env)) == 50.0


def test_a_journal_that_fails_before_the_head_moves_stops_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path)
    real = EditJournal.append

    def full_disk(self: EditJournal, batch: str, state: str, **data: Any) -> Any:
        if state == "staged":
            raise OSError(28, "No space left on device")
        return real(self, batch, state, **data)

    monkeypatch.setattr(EditJournal, "append", full_disk)
    with pytest.raises(OSError, match="No space left"):
        env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert env.store.head().id == "r0000"
    assert list(env.store.staging_dir.glob("*")) == []


# --- Desktop calls that fail without a DesktopSessionError ---------------------------------------


def test_a_timeout_while_desktop_applies_measures_is_rolled_back_and_never_left_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path)
    real = env.desktop.apply_measure_operations

    def hangs(instance: Any, operations: Any, tables: Any) -> list[str]:
        real(instance, operations, tables)  # applied inside Desktop, then the pbi call times out
        raise subprocess.TimeoutExpired("pbi", 120)

    monkeypatch.setattr(env.desktop, "apply_measure_operations", hangs)
    with pytest.raises(EditRejected, match="did not finish applying the change in time"):
        env.editor.apply(env.report, [CREATE], expected_head="r0000")
    assert env.store.head().id == "r0000"
    assert env.desktop.find(env.working) is None  # saved and closed, not left dirty
    assert sha(env.working) == env.store.head().blob
    assert last_states(env)[-1] == "rolled_back"
    monkeypatch.setattr(env.desktop, "apply_measure_operations", real)
    # The next batch is not asked about "unsaved work" and adopts nothing.
    receipt = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert receipt.adopted is None
    assert "Avg Discount" not in measure_names(head_path(env))


def test_a_timeout_that_also_stops_desktop_closing_is_reported_with_what_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path)

    def hangs(instance: Any, operations: Any, tables: Any) -> list[str]:
        raise subprocess.TimeoutExpired("pbi", 120)

    def stuck(instance: Any) -> None:
        raise subprocess.TimeoutExpired("uia", 90)

    monkeypatch.setattr(env.desktop, "apply_measure_operations", hangs)
    monkeypatch.setattr(env.desktop, "save", stuck)
    with pytest.raises(EditRejected, match="could not be closed cleanly.*Don't save"):
        env.editor.apply(env.report, [CREATE], expected_head="r0000")
    assert env.desktop.find(env.working) is not None  # never killed
    assert env.store.head().id == "r0000"


def test_a_save_that_times_out_for_the_users_work_refuses_and_leaves_desktop_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path)
    open_desktop(env, normalize=False)
    env.desktop.user_edit(
        env.working, OTHER_VISUAL_PART, lambda v: v["position"].__setitem__("x", 999.0)
    )

    def stuck(instance: Any) -> None:
        raise subprocess.TimeoutExpired("uia", 90)

    monkeypatch.setattr(env.desktop, "save", stuck)
    with pytest.raises(EditRejected, match="could not get an answer from Power BI Desktop"):
        env.editor.apply(env.report, [MOVE], expected_head="r0000", desktop_work="save")
    assert env.desktop.find(env.working) is not None
    assert env.store.head().id == "r0000"


def test_a_desktop_that_cannot_be_asked_is_never_taken_for_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path)
    open_desktop(env, normalize=False)
    env.desktop.user_edit(
        env.working, OTHER_VISUAL_PART, lambda v: v["position"].__setitem__("x", 999.0)
    )

    def unsure(path: Path) -> Any:
        raise DesktopSessionError("This report is open in more than one Power BI Desktop window.")

    monkeypatch.setattr(env.desktop, "find", unsure)
    attempts: list[Callable[[], object]] = [
        lambda: env.editor.apply(env.report, [MOVE], expected_head="r0000"),
        lambda: env.editor.apply(env.report, [CREATE], expected_head="r0000"),
        lambda: env.editor.sync_desktop(env.report),
        lambda: env.editor.open_in_desktop(env.report),
        lambda: env.editor.desktop_screenshot(env.report, "p", tmp_path / "s.png"),
    ]
    for attempt in attempts:
        with pytest.raises(EditRejected, match="more than one Power BI Desktop window"):
            attempt()
    assert env.store.head().id == "r0000"
    assert journal(env).batches() == {}  # no batch even started
    assert env.desktop.events == ["open normalize=False"]  # Desktop was not touched


# --- measure references through From aliases -----------------------------------------------------


class _Doc:
    def __init__(self, visual: dict[str, Any]) -> None:
        self.visual = visual

    def all_visuals(self) -> list[tuple[Any, str, dict[str, Any]]]:
        return [(None, "v", self.visual)]


def _measure(source: dict[str, str], name: str) -> dict[str, Any]:
    return {"Measure": {"Expression": {"SourceRef": source}, "Property": name}}


def _references(visual: dict[str, Any]) -> set[tuple[str, str]]:
    # A stub with all_visuals() stands in for a PbirDocument.
    return report_editor._measure_references(_Doc(visual))  # type: ignore[arg-type]


def test_measure_references_resolve_from_aliases_as_well_as_entities() -> None:
    visual = {
        "query": {"Select": [_measure({"Entity": "# Measures"}, "Rank")]},
        "filter": {
            "From": [{"Name": "m", "Entity": "# Measures", "Type": 0}],
            "Where": [
                {
                    "Condition": {
                        "Comparison": {
                            "Left": _measure({"Source": "m"}, "Rank Filtering Dynamics"),
                            "Right": {"Literal": {"Value": "1L"}},
                        }
                    }
                }
            ],
        },
        # An alias this scope does not define, and a measure with no table at all.
        "elsewhere": [_measure({"Source": "zz"}, "Lost"), _measure({}, "NoTable")],
    }
    assert _references(visual) == {
        ("# measures", "rank"),
        ("# measures", "rank filtering dynamics"),
    }


def test_an_alias_scope_does_not_leak_to_a_sibling_query() -> None:
    visual = {
        "a": {"From": [{"Name": "m", "Entity": "T1"}], "x": _measure({"Source": "m"}, "One")},
        "b": {"From": [{"Name": "m", "Entity": "T2"}], "x": _measure({"Source": "m"}, "Two")},
    }
    assert _references(visual) == {("t1", "one"), ("t2", "two")}


def _twin_with_alias_filter(path: Path) -> Path:
    """The structure twin with a measure filter written through a From alias on the slicer."""
    with zipfile.ZipFile(TWIN) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    visual = json.loads(members[VISUAL_PART].decode("utf-8-sig"))
    field = _measure({"Entity": MEASURE_TABLE}, "Rank")
    visual.setdefault("filterConfig", {}).setdefault("filters", []).append(
        {
            "name": "aliased",
            "type": "Advanced",
            "field": field,
            "filter": {
                "Version": 2,
                "From": [{"Name": "m", "Entity": MEASURE_TABLE, "Type": 0}],
                "Where": [
                    {
                        "Condition": {
                            "Comparison": {
                                "ComparisonKind": 1,
                                "Left": _measure({"Source": "m"}, "Rank"),
                                "Right": {"Literal": {"Value": "1L"}},
                            }
                        }
                    }
                ],
            },
        }
    )
    members[VISUAL_PART] = json.dumps(visual).encode()
    make_zip(path, members)
    return path


@pytest.mark.parametrize("stage", ["apply_operations", "apply_report_operations"])
def test_a_rename_that_misses_an_aliased_reference_is_refused_as_dangling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """The edit layer is made to miss the alias in the dry run (before Desktop is touched) or in
    the report-layer half run on Desktop's saved file: either way nothing is kept."""
    variant = _twin_with_alias_filter(tmp_path / "variant.pbix")
    env = make_env(tmp_path, source_file=variant)
    real = getattr(report_edits, stage)

    def revert(node: Any) -> None:
        if isinstance(node, dict):
            measure = node.get("Measure")
            if isinstance(measure, dict) and "Source" in measure["Expression"]["SourceRef"]:
                measure["Property"] = "Rank"  # the rewrite that only knew about Entity
            for child in node.values():
                revert(child)
        elif isinstance(node, list):
            for child in node:
                revert(child)

    def misses_the_alias(document: Any, operations: Any, model: Any) -> Any:
        outcome = real(document, operations, model)
        for _, _, visual in document.all_visuals():
            revert(visual)
        return outcome

    monkeypatch.setattr(report_editor, stage, misses_the_alias)
    with pytest.raises(EditRejected, match=r"# Measures\[Rank\]"):
        env.editor.apply(env.report, [RENAME], expected_head="r0000")
    assert env.store.head().id == "r0000"
    if stage == "apply_operations":
        assert env.desktop.events == []  # refused before Desktop was opened
        assert not env.working.exists()
    else:
        assert sha(env.working) == env.store.head().blob
    assert list(env.store.staging_dir.glob("*")) == []


def test_the_expected_measures_come_from_the_operations_alone() -> None:
    pre = ModelInfo.from_report(
        [],
        [
            {"Name": "Keep", "TableName": "T"},
            {"Name": "Old", "TableName": "T"},
            {"Name": "Gone", "TableName": "T"},
        ],
    )
    ops = [
        CreateMeasure(table="t", name="Fresh", expression="1"),
        RenameMeasure(name="Old", new_name="New"),
        DeleteMeasure(name="Gone"),
        RenameMeasure(name="Fresh", new_name="Fresher"),
        UpdateMeasure(name="Keep", expression="2"),
    ]
    assert report_editor._expected_measures(pre, ops) == {
        "keep": "t",
        "new": "t",
        "fresher": "t",
    }
    assert pre.measures == {"Keep": "T", "Old": "T", "Gone": "T"}


# --- undo and restore ----------------------------------------------------------------------------


def test_restoring_the_current_version_is_refused_before_desktop_is_closed(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    open_desktop(env)
    env.desktop.events.clear()
    with pytest.raises(RevisionError, match="r0001 is already the current version"):
        env.editor.restore(first.report, "r0001", expected_head="r0001")
    with pytest.raises(RevisionError, match="no version 'r0099'"):
        env.editor.restore(first.report, "r0099", expected_head="r0001")
    assert env.desktop.events == []  # Desktop was neither closed nor reopened
    assert set(journal(env).batches()) == {first.batch_id}


def test_undo_of_a_version_whose_file_was_pruned_is_refused_before_desktop_is_closed(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)
    first = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    second = env.editor.apply(
        first.report, [SetPosition(visual=OTHER, x=12)], expected_head="r0001"
    )
    open_desktop(env)
    env.desktop.events.clear()
    (env.store.revisions_dir / f"{first.revision.blob}.pbix").unlink()
    with pytest.raises(RevisionError, match="Version r0001 is no longer kept on disk"):
        env.editor.undo(second.report, expected_head="r0002")
    assert env.desktop.events == []


def test_a_desktop_save_found_after_the_users_confirmation_needs_its_own_confirmation(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)
    first = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    second = env.editor.apply(
        first.report, [SetPosition(visual=OTHER, x=12)], expected_head="r0001"
    )
    assert [r.id for r in env.editor.restore_preview(second.report, "r0000")] == ["r0002", "r0001"]
    open_desktop(env, normalize=False)
    env.desktop.user_edit(
        env.working, OTHER_VISUAL_PART, lambda v: v["position"].__setitem__("x", 777.0)
    )
    # The UI confirmed leaving out r0002 and r0001; the save made meanwhile was not shown.
    with pytest.raises(RestoreNeedsConfirmation) as caught:
        env.editor.restore(
            second.report,
            "r0000",
            expected_head="r0002",
            confirmed_discard=True,
            desktop_work="save",
        )
    assert [(r.id, r.author) for r in caught.value.later] == [
        ("r0003", "desktop"),
        ("r0002", "studio"),
        ("r0001", "studio"),
    ]
    assert env.store.head().id == "r0003"  # the user's work is safe in the history
    assert visual_x(head_path(env), OTHER_VISUAL_PART) == 777.0
    # Confirming the new list goes through.
    receipt = env.editor.restore(
        second.report, "r0000", expected_head="r0003", confirmed_discard=True
    )
    assert (receipt.revision.id, receipt.revision.author) == ("r0004", "restore")
    assert env.store.head().blob == env.store.revision("r0000").blob


def test_undo_and_restore_rows_record_the_desktop_version_when_desktop_took_part(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)
    first = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    undone = env.editor.undo(first.report, expected_head="r0001")
    assert undone.revision.desktop_version is None  # Desktop was not involved
    open_desktop(env)
    restored = env.editor.restore(undone.report, "r0001", expected_head="r0002")
    assert (restored.revision.author, restored.revision.desktop_version) == (
        "restore",
        DESKTOP_VERSION,
    )
    undone_again = env.editor.undo(restored.report, expected_head="r0003")
    assert undone_again.revision.desktop_version == DESKTOP_VERSION


def test_a_save_adopted_by_sync_or_open_records_the_desktop_version(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env, normalize=False)
    env.desktop.user_edit(
        env.working, OTHER_VISUAL_PART, lambda v: v["position"].__setitem__("x", 5.0)
    )
    env.desktop.user_saves(env.working)
    env.desktop.user_closes(env.working)
    assert env.editor.sync_desktop(env.report) is not None
    assert env.store.head().desktop_version == DESKTOP_VERSION
    env.store.materialize()
    open_desktop(env, normalize=False)
    env.desktop.user_edit(
        env.working, OTHER_VISUAL_PART, lambda v: v["position"].__setitem__("x", 6.0)
    )
    env.desktop.user_saves(env.working)
    env.desktop.user_closes(env.working)
    env.editor.open_in_desktop(env.report)
    assert (env.store.head().id, env.store.head().desktop_version) == ("r0002", DESKTOP_VERSION)


# --- the real controller satisfies the ports ---------------------------------------------------


def _controller_is_a_port(controller: DesktopController) -> report_editor.DesktopPort:
    """Checked by mypy: the real class must fit the protocol the editor is written against
    (its save returns a SaveResult and takes an optional timeout)."""
    return controller


def _controller_fits_the_other_ports(
    controller: DesktopController,
) -> tuple[report_delivery.DeliveryPort, RecoveryPort]:
    return controller, controller


def test_the_real_controller_has_every_method_of_the_port() -> None:
    for name in (
        "find",
        "status",
        "open",
        "save",
        "close",
        "apply_measure_operations",
        "screenshot",
        "blocking_windows",
        "version",
    ):
        assert callable(getattr(DesktopController, name)), name


# --- what the edit layer is given: model facts, baselines, and what it reports back ------------

INT_CHANNEL: dict[str, Any] = {
    "columns": [{"table": "DimChannel", "name": "ChannelName", "dataType": 6}],
    "measures": [],
}
NO_TYPES = "Studio could not read the model's data types; values were treated as text."
FILTER_2019 = SetReportFilter(
    filter=CategoricalFilter(
        field=FilterField(table="DimChannel", name="ChannelName"), values=[2019]
    )
)


def with_facts(env: Env, facts: Callable[[ImportedReport], Any]) -> None:
    """Give the env's editor a source for the model facts (as the service's types sidecar is)."""
    env.editor = ReportEditor(
        env.editor.root,
        env.editor.rederive,
        lambda: env.desktop,
        read_measures=read_measures,
        disk_free=lambda _path: 10**12,
        profile_dir=env.root,
        model_facts=facts,
    )


def report_json(path: Path) -> str:
    return json.dumps(read_part(path, "Report/definition/report.json"))


def test_the_model_facts_type_a_filter_literal_from_the_column(tmp_path: Path) -> None:
    plain = make_env(tmp_path / "plain")
    plain.editor.apply(plain.report, [FILTER_2019], expected_head="r0000")
    # the import's pandas dtype says object, so 2019 is text
    assert '"Value": "\'2019\'"' in report_json(head_path(plain))
    typed = make_env(tmp_path / "typed")
    seen: list[ImportedReport] = []

    def facts(report: ImportedReport) -> Any:
        seen.append(report)
        return INT_CHANNEL, {}

    with_facts(typed, facts)
    receipt = typed.editor.apply(typed.report, [FILTER_2019], expected_head="r0000")
    assert '"Value": "2019L"' in report_json(head_path(typed))  # the model says whole number
    assert receipt.warnings == [] and seen


def test_a_calculated_column_that_uses_a_measure_blocks_deleting_it(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    unused = {"Name": "Unused", "TableName": MEASURE_TABLE, "Expression": "1"}
    report = env.report.model_copy(update={"measures": [*INITIAL_MEASURES, unused]})
    with_facts(env, lambda _report: (INT_CHANNEL, {"FactSales[Disc]": "[Unused] * 2"}))
    open_desktop(env)
    env.desktop.events.clear()
    with pytest.raises(
        EditRejected, match=r"\[Unused\] is used by the calculated column 'FactSales\[Disc\]'"
    ):
        env.editor.apply(report, [DeleteMeasure(name="Unused")], expected_head="r0000")
    assert env.desktop.events == [] and env.store.head().id == "r0000"


def test_a_field_parameter_is_seen_through_its_calculated_table(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    unused = {"Name": "Unused", "TableName": MEASURE_TABLE, "Expression": "1"}
    report = env.report.model_copy(update={"measures": [*INITIAL_MEASURES, unused]})
    picker = {"Metric Picker": '{ ("Unused", NAMEOF([Unused]), 0) }'}
    with_facts(env, lambda _report: (INT_CHANNEL, picker))
    with pytest.raises(EditRejected, match=r"\[Unused\] is used by .*Metric Picker"):
        env.editor.apply(report, [DeleteMeasure(name="Unused")], expected_head="r0000")


def test_a_defect_the_source_report_already_has_never_blocks_but_a_new_one_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path)
    model = ModelInfo.from_report(env.report.tables, env.report.measures)
    document = PbirDocument(PbixPackage(head_path(env)))
    # The twin uses fields the fixture's small model does not list: a strict check would refuse
    # every edit to this report.
    assert any("that the model does not have" in p for p in validate_document(document, model))
    receipt = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert receipt.status == "updated"
    # A defect the edit itself introduces is still refused, and by name.
    real = report_edits._HANDLERS["set_position"]

    def breaks_the_visual(doc: Any, op: Any, model: Any) -> Any:
        result = real(doc, op, model)
        page, visual = doc.visual(op.visual)
        visual["filterConfig"] = {
            "filters": [
                {
                    "name": "F1",
                    "type": "Categorical",
                    "field": {
                        "Column": {
                            "Expression": {"SourceRef": {"Entity": "Ghost"}},
                            "Property": "Nope",
                        }
                    },
                }
            ]
        }
        doc.touch_visual(page, op.visual)
        return result

    monkeypatch.setitem(report_edits._HANDLERS, "set_position", breaks_the_visual)
    with pytest.raises(EditRejected, match=r"uses a filter, Ghost\[Nope\], that the model does"):
        env.editor.apply(receipt.report, [SetPosition(visual=VISUAL, x=70)], expected_head="r0001")
    assert env.store.head().id == "r0001"


def test_both_stages_of_a_model_batch_hold_the_result_to_the_heads_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path)
    baselines: list[tuple[Any, Any]] = []
    validations: list[dict[str, Any]] = []
    real_baseline, real_validate = report_editor.capture_baseline, report_editor.validate_document

    def spy_baseline(document: Any, model: Any = None) -> Any:
        found = real_baseline(document, model)
        baselines.append((model, found))
        return found

    def spy_validate(document: Any, model: Any = None, **kwargs: Any) -> Any:
        validations.append({"model": model, **kwargs})
        return real_validate(document, model, **kwargs)

    monkeypatch.setattr(report_editor, "capture_baseline", spy_baseline)
    monkeypatch.setattr(report_editor, "validate_document", spy_validate)
    card = AddVisual(
        page="1", visual_type="card", fields=[FieldRef(table=MEASURE_TABLE, name="Avg Discount")]
    )
    env.editor.apply(env.report, [CREATE, card], expected_head="r0000")
    # the pre-check and the check on the head, then the report-layer half on Desktop's file
    assert len(baselines) == len(validations) == 3
    for (before, found), check in zip(baselines, validations, strict=True):
        assert found, "the twin has problems of its own for the baseline to hold"
        assert check["baseline"] == found and isinstance(check["lineage"], dict)
        assert isinstance(check["model"], ModelInfo) and isinstance(before, ModelInfo)
    # the source model has no Avg Discount; what the result is checked against does
    assert "Avg Discount" not in baselines[2][0].measures
    assert "Avg Discount" in validations[2]["model"].measures


def test_a_step_that_names_a_measure_by_its_old_name_before_it_is_renamed_still_applies(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)
    card = AddVisual(
        page="1", visual_type="card", fields=[FieldRef(table=MEASURE_TABLE, name="Rank")]
    )
    receipt = env.editor.apply(env.report, [card, RENAME], expected_head="r0000")
    assert receipt.status == "updated"
    assert receipt.summaries[0] == "Added a card of Rank to page '1'."
    # two visuals used [Rank] before, and the new card: all follow the rename
    assert measure_refs(head_path(env), "Agent Rank") == 3
    assert measure_refs(head_path(env), "Rank") == 0
    assert "Agent Rank" in measure_names(head_path(env))


def test_the_model_before_keeps_the_types_and_the_formulas_of_the_head() -> None:
    pre = ModelInfo.from_report(
        [{"TableName": "FactSales", "ColumnName": "Amount", "PandasDataType": "float64"}],
        [
            {"Name": "Keep", "TableName": "T", "Expression": "1"},
            {"Name": "Old", "TableName": "T", "Expression": "[Keep] + 1"},
        ],
        {
            "columns": [{"table": "FactSales", "name": "Amount", "dataType": 10}],
            "measures": [{"table": "T", "name": "Old", "dataType": 6}],
        },
        calculated={"FactSales[Disc]": "[Old] * 2"},
    )
    saved = ModelInfo.from_report(
        [],
        [
            {"Name": "Keep", "TableName": "T"},
            {"Name": "New", "TableName": "T"},
            {"Name": "Fresh", "TableName": "T"},
        ],
    )
    ops = [
        RenameMeasure(name="Old", new_name="New"),
        CreateMeasure(table="T", name="Fresh", expression="1"),
    ]
    before = report_editor._model_before(pre, saved, ops)
    assert before.measures == {"Keep": "T", "Old": "T"}
    assert before.measure_expressions == {"Keep": "1", "Old": "[Keep] + 1"}
    assert before.data_type("FactSales", "Amount") == 10  # the model's id, not pandas' float64
    assert before.data_type("T", "Old") == 6
    assert before.calculated == {"FactSales[Disc]": "[Old] * 2"}
    # the report-layer half resolves names on both sides of the rename
    half = report_editor._model_for_report_half(before, saved)
    assert set(half.measures) == {"Keep", "Old", "New", "Fresh"}


def test_facts_are_read_from_the_head_after_a_desktop_save_was_adopted(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    seen: list[tuple[Path, str]] = []

    def facts(report: ImportedReport) -> Any:
        seen.append(report_archive(report))
        return INT_CHANNEL, {}

    with_facts(env, facts)
    open_desktop(env, normalize=False)
    env.desktop.user_edit(
        env.working, OTHER_VISUAL_PART, lambda v: v["position"].__setitem__("x", 999.0)
    )
    # a filter, because a batch that reads no type never decodes the model
    receipt = env.editor.apply(
        env.report, [FILTER_2019], expected_head="r0000", desktop_work="save"
    )
    assert receipt.adopted is not None
    assert seen[0] == (env.store.original, env.report.source_hash)
    adopted = receipt.adopted
    assert seen[-1] == (env.store.path(adopted), adopted.blob)  # not the file the view was built on


@pytest.mark.parametrize("failure", ["nothing", "raises", "no_types"])
def test_facts_that_cannot_be_read_warn_only_when_the_edit_depends_on_them(
    tmp_path: Path, failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    def facts(report: ImportedReport) -> Any:
        if failure == "raises":
            raise RuntimeError("the sidecar is unreadable")
        return None if failure == "nothing" else (None, {})

    env = make_env(tmp_path / "typed")
    with_facts(env, facts)
    receipt = env.editor.apply(env.report, [FILTER_2019], expected_head="r0000")
    assert receipt.warnings == [NO_TYPES]
    assert receipt.status == "updated"
    # a move writes no value that a type decides
    moved = env.editor.apply(receipt.report, [MOVE], expected_head="r0001")
    assert NO_TYPES not in moved.warnings
    if failure == "raises":
        assert "sidecar is unreadable" in caplog.text
    # no source for the facts at all (the editor used on its own): nothing was asked, no warning
    bare = make_env(tmp_path / "bare")
    assert bare.editor.apply(bare.report, [FILTER_2019], expected_head="r0000").warnings == []


def test_the_receipt_warns_when_calculated_dax_is_unknown_for_a_measure_change(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)
    with_facts(env, lambda report: (INT_CHANNEL, None))
    editor = env.editor
    words = editor._facts_warnings((INT_CHANNEL, None), [RENAME], has_model=True)
    assert words == [report_editor.NO_CALCULATED_WARNING]
    assert "calculated columns and tables" in words[0]
    assert editor._facts_warnings((INT_CHANNEL, None), [CREATE], has_model=True) == []
    assert editor._facts_warnings((INT_CHANNEL, {}), [RENAME], has_model=True) == []
    # a report with no model of its own has nothing to read: that has its own warning
    assert editor._facts_warnings(None, [RENAME, FILTER_2019], has_model=False) == []


def test_a_measure_can_be_hidden_neither_by_the_edit_layer_nor_the_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path)
    hide = UpdateMeasure(name="Rank", hidden=True)
    with pytest.raises(EditRejected, match=r"cannot hide or show a measure yet.*\[Rank\].*Hide"):
        env.editor.apply(env.report, [hide], expected_head="r0000")
    # Lift the edit layer's own refusal: the editor still turns it away before it opens Desktop,
    # because ``pbi measure update`` has no way to set the flag and a no-op would say "done".
    monkeypatch.setitem(report_edits._HANDLERS, "update_measure", lambda doc, op, model: "Hid it.")
    open_desktop(env)
    env.desktop.events.clear()
    with pytest.raises(EditRejected, match=r"right-click \[Rank\] in the Data pane.*choose Hide"):
        env.editor.apply(env.report, [hide], expected_head="r0000")
    show = UpdateMeasure(name="Rank", hidden=False)
    with pytest.raises(EditRejected, match="choose Unhide"):
        env.editor.apply(env.report, [show], expected_head="r0000")
    assert env.desktop.events == [] and env.store.head().id == "r0000"


def test_the_model_is_only_decoded_for_operations_that_read_it(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    seen: list[ImportedReport] = []

    def facts(report: ImportedReport) -> Any:
        seen.append(report)
        return INT_CHANNEL, {}

    with_facts(env, facts)
    # moving a visual never looks at a type: no decode, no warning
    receipt = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert seen == [] and receipt.warnings == []
    # a filter is typed by its column, so the facts are read (once per phase that needs them)
    env.editor.apply(env.report, [FILTER_2019], expected_head="r0001")
    assert seen
    assert '"Value": "2019L"' in report_json(head_path(env))


def test_a_dialog_in_desktop_is_a_typed_refusal_with_the_dialogs_title(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    open_desktop(env)
    env.desktop.blocking = ["Save changes?"]
    with pytest.raises(DesktopDialog) as caught:
        env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert caught.value.dialog == "Save changes?"
    assert isinstance(caught.value, EditRejected) and caught.value.status == "failed"
    # the words are the ones the service used to read the title from
    assert str(caught.value) == (
        "Power BI Desktop is showing a dialog (Save changes?). Finish it in Power BI Desktop, "
        "then try again. Studio has not changed anything."
    )
    # a dialog the port itself reports (a prompt on close, a dialog while saving) is typed too
    env.desktop.blocking = []
    env.desktop.fail_close = DesktopDialogOpen(
        "Power BI Desktop is asking a question before it closes (Save changes?).", "Save changes?"
    )
    with pytest.raises(DesktopDialog) as again:
        env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert again.value.dialog == "Save changes?"
    assert "asking a question before it closes" in str(again.value)
    # any other port failure stays a plain refusal
    env.desktop.fail_close = DesktopSessionError("Power BI Desktop did not close.")
    with pytest.raises(EditRejected) as plain:
        env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert not isinstance(plain.value, DesktopDialog)


# --- receipts carry what the edit layer reported, and a replay returns it ----------------------

TABLE_VISUAL = "e82e02f93055d6b0b544"  # a tableEx with four conditional font colors
REPLACE_CONDITIONAL = SetConditionalFormat(
    visual=TABLE_VISUAL,
    style="gradient",
    target="font_color",
    field="Rank",
    replace_conditional=True,
)


def test_the_receipt_carries_what_the_edit_layer_reported_and_a_replay_returns_it(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)
    receipt = env.editor.apply(
        env.report, [REPLACE_CONDITIONAL], expected_head="r0000", idempotency_key="replay-key-1"
    )
    assert len(receipt.replaced) == 1
    assert "measure" in receipt.replaced[0] and "Rank column" in receipt.replaced[0]
    assert receipt.visible_in == [
        VisibleIn(op="set_conditional_format", where="studio", detail="values.fontColor")
    ]
    assert receipt.protected == [] and receipt.pending_model_operations == []
    # the journal keeps it beside the summaries ...
    recorded = list(journal(env).batches().values())[-1].receipt
    assert recorded is not None
    assert recorded["replaced"] == receipt.replaced
    assert recorded["visible_in"] == [
        {"op": "set_conditional_format", "where": "studio", "detail": "values.fontColor"}
    ]
    assert recorded["protected"] == [] and recorded["pending_model_operations"] == []
    # ... so the same key gives it back without applying anything twice
    replay = env.editor.apply(
        receipt.report, [REPLACE_CONDITIONAL], expected_head="r0000", idempotency_key="replay-key-1"
    )
    assert replay.replayed and replay.revision == receipt.revision
    assert replay.replaced == receipt.replaced and len(replay.replaced) == 1
    assert replay.visible_in == receipt.visible_in
    assert len(env.store.history()) == 2


def test_a_recorded_receipt_round_trips_through_the_journal_line() -> None:
    outcome = report_edits.EditOutcome(
        protected=[Protected("v1", "conditional font color on Agent ID, kept")],
        replaced=["TopN blue, BottomN red"],
        visible_in=[
            VisibleIn("set_conditional_format", "powerbi_only", "the canvas does not draw this"),
            VisibleIn("set_position", "covered_by_background"),
        ],
        pending_model_operations=[UpdateMeasure(name="Rank", expression="1")],
    )
    line = json.loads(json.dumps(report_editor._receipt_record(outcome)))
    items = report_editor._recorded_items(line)
    assert items["protected"] == outcome.protected
    assert items["replaced"] == ["TopN blue, BottomN red"]
    assert items["visible_in"] == outcome.visible_in
    assert items["pending_model_operations"] == [
        UpdateMeasure(name="Rank", expression="1").model_dump(mode="json")
    ]
    # a line written before these fields existed, or with entries of another shape, still reads
    assert report_editor._recorded_items({"summaries": ["x"]}) == {
        "protected": [],
        "replaced": [],
        "visible_in": [],
        "pending_model_operations": [],
    }
    odd = report_editor._recorded_items(
        {"protected": ["nope", {"visual": "v"}], "visible_in": [{"op": "x", "where": "elsewhere"}]}
    )
    assert odd["protected"] == [Protected("v", "")]
    assert odd["visible_in"] == [VisibleIn("x", "studio", "")]


def test_studios_own_save_after_a_reopen_is_not_recorded_as_the_users_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seen live on 2026-09-29: every reopen made Desktop rewrite the file, and the next edit
    recorded that as "Changes saved in Power BI Desktop", which also broke Undo. The rewrite
    is not treated as the same content here (as with the real Desktop), so only recording it
    as the placed copy keeps the history clean."""
    monkeypatch.setattr(report_revisions, "semantic_equal", lambda a, b: False)
    env = make_env(tmp_path)
    env.editor.open_in_desktop(env.report)  # "Open in Power BI"
    first = env.editor.apply(env.report, [MOVE], expected_head="r0000")
    assert first.desktop == "reopened"
    second = env.editor.apply(
        env.report,
        [SetPosition(visual=VISUAL, x=60)],
        expected_head=first.revision.id,
    )
    assert second.adopted is None
    assert [r.author for r in env.store.history()] == ["studio", "studio", "import"]
    undone = env.editor.undo(env.report, expected_head=second.revision.id)
    assert undone.revision.restores == first.revision.id


# --- batches with other model operations (the TOM runner) -----------------------------------------
# The model family is registered in the facade (report_edits); these tests only keep Power Query
# text away from the real M checker.


@pytest.fixture
def offline_m_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report_edit_model, "M_CHECKER", lambda text: [])


def model_ops(*raw: dict[str, Any]) -> list[Any]:
    return [report_edit_model.parse_model_operation(item) for item in raw]


def saved_facts(env: Env) -> dict[str, Any]:
    return read_model_facts(head_path(env))


def facts_table(facts: dict[str, Any], name: str) -> dict[str, Any]:
    return next(t for t in facts["tables"] if t["name"] == name)


STORE_VISUAL_PART = VISUAL_PART.replace(VISUAL, "5b0dbccd61c6c4adb415")
TABLE_VISUAL_PART = VISUAL_PART.replace(VISUAL, "e82e02f93055d6b0b544")
YEARS = {"op": "create_calculated_table", "name": "Years", "expression": "DISTINCT('Date'[Year])"}


@pytest.mark.usefixtures("offline_m_checker")
def test_a_calculated_column_is_one_revision_through_the_runner(tmp_path: Path) -> None:
    env = make_env(tmp_path, model=model_state())
    column = {
        "op": "create_calculated_column",
        "table": "FactSales",
        "name": "Margin",
        "expression": "FactSales[SalesAmount] - FactSales[TotalCost]",
    }
    receipt = env.editor.apply(env.report, model_ops(column), expected_head="r0000")
    head = env.store.head()
    assert (head.id, receipt.status, receipt.model_changed) == ("r0001", "updated", True)
    assert receipt.summaries == ["Created the calculated column FactSales[Margin]."]
    assert env.desktop.events == ["open normalize=True", "model applied", "save", "close"]
    columns = facts_table(saved_facts(env), "FactSales")["columns"]
    margin = [c for c in columns if c["name"] == "Margin"]
    assert margin[0]["expression"] == "FactSales[SalesAmount] - FactSales[TotalCost]"
    assert last_states(env) == [
        "planned",
        "model_applied",
        "model_saved",
        "adopted",
        "staged",
        "committed",
        "placed",
        "done",
    ]
    apply_call = env.desktop.runner_calls[-1]
    assert [c["kind"] for c in apply_call["changes"]] == ["calculatedColumn"]
    assert apply_call["probes"][0]["label"] == "The check of FactSales[Margin]"
    assert receipt.warnings == []


@pytest.mark.usefixtures("offline_m_checker")
def test_a_column_rename_rewrites_the_report_and_the_formulas_in_one_revision(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path, model=model_state())
    open_desktop(env)
    env.desktop.events.clear()
    rename = {"op": "rename_column", "table": "DimStore", "name": "StoreName", "new_name": "Store"}
    receipt = env.editor.apply(env.report, model_ops(rename), expected_head="r0000")
    assert len(env.store.history()) == 2  # ONE head move for both layers
    field = read_part(head_path(env), STORE_VISUAL_PART)["visual"]["query"]["queryState"]["Values"][
        "projections"
    ][0]
    assert field["field"]["Column"]["Property"] == "Store"
    assert field["queryRef"] == "DimStore.StoreName"
    names = [c["name"] for c in facts_table(saved_facts(env), "DimStore")["columns"]]
    assert "Store" in names and "StoreName" not in names
    expressions = {m["Name"]: m["Expression"] for m in read_measures(head_path(env))}
    assert expressions["Stores"] == "DISTINCTCOUNT(DimStore[Store])"
    assert receipt.warnings == [
        "Renaming a column is outside Microsoft's supported list; Studio tested it with Power BI "
        "Desktop 2.157.1354.0.",
        "Studio also updated the formulas that used the old name: the measure [Stores].",
    ]
    assert receipt.desktop == "reopened"
    assert env.desktop.events == [
        "close",
        "open normalize=True",
        "model applied",
        "save",
        "close",
        "open normalize=True",
    ]


@pytest.mark.usefixtures("offline_m_checker")
def test_a_table_rename_moves_its_measures_and_rewrites_every_entity(tmp_path: Path) -> None:
    env = make_env(tmp_path, model=model_state())
    before = measure_refs(env.store.original, "Rank")
    rename = {"op": "rename_table", "name": "# Measures", "new_name": "Measures"}
    receipt = env.editor.apply(env.report, model_ops(rename), expected_head="r0000")
    assert receipt.status == "updated"
    homes = {m["Name"]: m["TableName"] for m in read_measures(head_path(env))}
    assert set(homes.values()) == {"Measures"}
    text = json.dumps(read_part(head_path(env), TABLE_VISUAL_PART))
    assert '"Entity": "# Measures"' not in text and '"Entity": "Measures"' in text
    assert before == 2


@pytest.mark.usefixtures("offline_m_checker")
def test_a_model_change_mixed_with_measures_goes_through_the_runner_only(tmp_path: Path) -> None:
    env = make_env(tmp_path, model=model_state())
    batch = [CREATE, *model_ops(YEARS)]
    receipt = env.editor.apply(env.report, batch, expected_head="r0000")
    assert "ops applied" not in env.desktop.events and "model applied" in env.desktop.events
    assert receipt.summaries == [
        "Created measure [Avg Discount] in # Measures.",
        "Created the calculated table 'Years'.",
    ]
    assert "Avg Discount" in measure_names(head_path(env))
    assert facts_table(saved_facts(env), "Years")["kind"] == "calculated"


@pytest.mark.usefixtures("offline_m_checker")
def test_a_change_the_engine_refuses_rolls_back_and_leaves_the_head(tmp_path: Path) -> None:
    env = make_env(tmp_path, model=model_state())
    env.desktop.fail_model_saves = ["The name 'Years' is already used"]
    with pytest.raises(
        EditRejected,
        match=r"Power BI did not accept the change \(Power BI rejected the change: The name "
        r"'Years'",
    ):
        env.editor.apply(
            env.report,
            model_ops({"op": "create_calculated_table", "name": "Years", "expression": "1"}),
            expected_head="r0000",
        )
    assert env.store.head().id == "r0000"
    assert env.desktop.events == ["open normalize=True", "model rolled back", "save", "close"]
    assert sha(env.working) == env.store.head().blob
    assert last_states(env) == ["planned", "rolled_back"]


@pytest.mark.usefixtures("offline_m_checker")
def test_a_query_whose_source_cannot_be_reached_asks_to_keep_it_without_data(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path, model=model_state())
    query = 'let Source = Csv.Document(File.Contents("UNREACHABLE")) in Source'
    batch = model_ops({"op": "set_table_query", "table": "FactSales", "expression": query})
    with pytest.raises(NeedsChoice) as asked:
        env.editor.apply(env.report, batch, expected_head="r0000")
    assert asked.value.step == 1
    assert asked.value.question.startswith(
        "Power BI could not reload the data with the new query (The data source of 'FactSales' "
        "could not be reached"
    )
    assert asked.value.options == [
        Choice("Keep the new query without reloading the data", {"if_refresh_fails": "keep"})
    ]
    assert env.store.head().id == "r0000"
    assert env.desktop.find(env.working) is None and sha(env.working) == env.store.head().blob
    kept = model_ops(
        {
            "op": "set_table_query",
            "table": "FactSales",
            "expression": query,
            "if_refresh_fails": "keep",
        }
    )
    receipt = env.editor.apply(env.report, kept, expected_head="r0000")
    assert receipt.status == "updated" and env.store.head().id == "r0001"
    note = "The new query is saved and takes effect at the next successful refresh"
    assert any(note in w for w in receipt.warnings)
    partition = facts_table(saved_facts(env), "FactSales")["partitions"][0]
    assert partition["expression"] == query


@pytest.mark.usefixtures("offline_m_checker")
def test_a_plain_refresh_that_fails_is_refused(tmp_path: Path) -> None:
    state = model_state()
    state["tables"][0]["partitions"][0]["expression"] = "let Source = UNREACHABLE in Source"
    env = make_env(tmp_path, model=state)
    with pytest.raises(
        EditRejected,
        match=r"^Power BI could not reload the data \(The data source of 'FactSales' could not be",
    ):
        env.editor.apply(
            env.report,
            model_ops({"op": "refresh_data", "table": "FactSales"}),
            expected_head="r0000",
        )
    assert env.store.head().id == "r0000"


@pytest.mark.usefixtures("offline_m_checker")
def test_a_save_that_lost_the_model_change_fails_the_facts_diff(tmp_path: Path) -> None:
    env = make_env(tmp_path, model=model_state())
    env.desktop.keep_model = False
    with pytest.raises(
        EditRejected, match=r"did not keep the change as asked \(the table 'Years' is missing\)"
    ):
        env.editor.apply(env.report, model_ops(YEARS), expected_head="r0000")
    assert env.store.head().id == "r0000"
    assert sha(env.working) == env.store.head().blob


@pytest.mark.usefixtures("offline_m_checker")
def test_unreadable_saved_facts_leave_the_checks_to_desktop_and_say_so(tmp_path: Path) -> None:
    env = make_env(tmp_path, model=model_state())
    env.editor._read_saved_facts = lambda path: {}
    receipt = env.editor.apply(env.report, model_ops(YEARS), expected_head="r0000")
    assert receipt.status == "updated"
    assert receipt.warnings == [
        "Studio could not read back the saved model's tables, formulas, so it relied on Power BI "
        "Desktop's own checks for them."
    ]


@pytest.mark.usefixtures("offline_m_checker")
def test_a_port_without_the_runner_refuses_before_desktop_is_opened(tmp_path: Path) -> None:
    env = make_env(tmp_path, model=model_state())
    env.desktop.apply_model_operations = None  # type: ignore[assignment,method-assign]
    with pytest.raises(EditRejected, match="cannot drive its model here"):
        env.editor.apply(
            env.report,
            model_ops({"op": "create_calculated_table", "name": "Years", "expression": "1"}),
            expected_head="r0000",
        )
    assert env.desktop.events == []


@pytest.mark.usefixtures("offline_m_checker")
def test_a_runner_that_cannot_run_is_said_before_desktop_is_opened(tmp_path: Path) -> None:
    env = make_env(tmp_path, model=model_state())
    env.desktop.model_runner_problem = lambda: (  # type: ignore[attr-defined]
        "The Power BI command-line tool is version 3.10.0; Studio was checked with 3.11.1"
    )
    with pytest.raises(
        EditRejected,
        match=r"^The Power BI command-line tool is version 3\.10\.0; Studio was checked with "
        r"3\.11\.1\. Nothing was changed\.$",
    ):
        env.editor.apply(env.report, model_ops(YEARS), expected_head="r0000")
    assert env.desktop.events == []


@pytest.mark.usefixtures("offline_m_checker")
def test_a_runner_that_cannot_run_leaves_an_open_desktop_alone(tmp_path: Path) -> None:
    """Bug review #10: the refusal came after Desktop had been saved, closed and adopted."""
    env = make_env(tmp_path, model=model_state())
    open_desktop(env)
    env.desktop.events.clear()
    env.desktop.model_runner_problem = lambda: "Studio's model tool is missing"  # type: ignore[attr-defined]
    with pytest.raises(EditRejected, match="Nothing was changed"):
        env.editor.apply(env.report, model_ops(YEARS), expected_head="r0000")
    assert env.desktop.events == [] and env.desktop.find(env.working) is not None
    assert env.store.head().id == "r0000"


@pytest.mark.usefixtures("offline_m_checker")
def test_a_change_outside_microsofts_list_needs_a_probe_receipt_for_this_desktop(
    tmp_path: Path,
) -> None:
    """Decision 1: ops outside the documented external-tool list run only when a passing live
    probe receipt for the running Desktop version covers their kind; else Desktop's steps."""
    env = make_env(tmp_path, model=model_state())
    open_desktop(env)
    env.desktop.events.clear()
    env.desktop.probe_verified = frozenset({"rename_table"})
    rename = {"op": "rename_column", "table": "DimStore", "name": "StoreName", "new_name": "Store"}
    with pytest.raises(
        EditRejected,
        match=r"^Renaming a column is outside the changes Microsoft supports for tools like Studio "
        r"in Power BI Desktop, and Studio has not tested it with Power BI Desktop 2\.157\.1354\.0 "
        r"yet, so nothing was changed\. In Power BI Desktop, right-click the column",
    ):
        env.editor.apply(env.report, model_ops(rename), expected_head="r0000")
    assert env.desktop.events == [] and env.store.head().id == "r0000"
    # A documented change (a calculated table) needs no receipt.
    env.desktop.probe_verified = frozenset()
    receipt = env.editor.apply(env.report, model_ops(YEARS), expected_head="r0000")
    assert receipt.status == "updated"
    assert not any("outside Microsoft" in w for w in receipt.warnings)


def test_the_support_keys_follow_the_object_a_step_changes() -> None:
    model = report_edit_model.example_model()
    parse = report_edit_model.parse_model_operation
    keys = report_edit_model.outside_support(
        [
            parse(
                {
                    "op": "create_calculated_column",
                    "table": "FactSales",
                    "name": "M",
                    "expression": "1",
                }
            ),
            parse({"op": "delete_column", "table": "FactSales", "name": "M"}),
            parse({"op": "delete_table", "name": "Metric"}),
            parse({"op": "delete_column", "table": "DimStore", "name": "Status"}),
            parse({"op": "set_table_properties", "table": "DimStore", "hidden": True}),
            parse(
                {
                    "op": "create_hierarchy",
                    "table": "Date",
                    "name": "H",
                    "levels": [{"column": "Year"}],
                }
            ),
            parse({"op": "create_role", "name": "R"}),
        ],
        model,
    )
    assert keys == ["delete_data_column", "data_table_properties", "hierarchies"]
    assert set(report_edit_model.MICROSOFT_SUPPORT) == set(report_edit_model.MODEL_HANDLERS)


def test_a_model_operation_the_edit_layer_did_not_hand_over_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Handlers registered but the classes missing from MODEL_OPERATIONS: the batch would say
    "updated" and never reach the model, so it is refused instead."""
    monkeypatch.setattr(report_edits, "MODEL_OPERATIONS", report_edits.MEASURE_OPERATIONS)
    env = make_env(tmp_path, model=model_state())
    with pytest.raises(EditRejected, match="not set up to send this change to Power BI Desktop"):
        env.editor.apply(env.report, model_ops(YEARS), expected_head="r0000")
    assert env.desktop.events == [] and env.store.head().id == "r0000"


@pytest.mark.usefixtures("offline_m_checker")
def test_the_dry_run_refuses_a_used_column_before_desktop_is_touched(tmp_path: Path) -> None:
    env = make_env(tmp_path, model=model_state())
    with pytest.raises(
        EditRejected,
        match=r"DimStore\[StoreName\] is used by .*used in the formula of the measure \[Stores\]",
    ):
        env.editor.apply(
            env.report,
            model_ops({"op": "delete_column", "table": "DimStore", "name": "StoreName"}),
            expected_head="r0000",
        )
    assert env.desktop.events == [] and env.desktop.runner_calls == []


@pytest.mark.usefixtures("offline_m_checker")
def test_the_model_view_knows_the_structure_the_facts_describe(tmp_path: Path) -> None:
    env = make_env(tmp_path, model=model_state())
    with pytest.raises(
        EditRejected,
        match=r"already connected through active relationships \(FactSales - DimStore\)",
    ):
        env.editor.apply(
            env.report,
            model_ops(
                {
                    "op": "create_relationship",
                    "from_table": "FactSales",
                    "from_column": "SalesKey",
                    "to_table": "DimStore",
                    "to_column": "StoreKey",
                }
            ),
            expected_head="r0000",
        )


@pytest.mark.usefixtures("offline_m_checker")
def test_thin_reports_refuse_model_changes_in_plain_words(tmp_path: Path) -> None:
    env = make_env(tmp_path, thin=True)
    env.report = env.report.model_copy(
        update={"tables": [{"TableName": "T", "ColumnName": "c", "PandasDataType": "int"}]}
    )
    with pytest.raises(EditRejected, match="cannot change that model here"):
        env.editor.apply(
            env.report,
            model_ops({"op": "create_calculated_table", "name": "Years", "expression": "1"}),
            expected_head="r0000",
        )


def test_the_expected_measures_follow_a_table_rename_and_delete() -> None:
    pre = ModelInfo(measures={"Rank": "# Measures", "Old": "Scratch"})
    ops = [
        report_edit_model.RenameTable(name="# Measures", new_name="Measures"),
        report_edit_model.DeleteTable(name="Scratch"),
    ]
    assert report_editor._expected_measures(pre, ops) == {"rank": "measures"}
    saved = ModelInfo(measures={"Rank": "Measures"})
    before = report_editor._model_before(pre, saved, ops)
    assert before.measures == {"Rank": "# Measures", "Old": "Scratch"}


def test_the_saved_facts_are_read_in_a_child_process_and_failures_mean_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='{"tables": []}', stderr="")

    monkeypatch.setattr(report_editor.subprocess, "run", run)
    assert report_editor.read_model_facts_subprocess(tmp_path / "r.pbix") == {"tables": []}
    assert calls[0][-1] == "--facts" and calls[0][-2] == str(tmp_path / "r.pbix")
    monkeypatch.setattr(
        report_editor.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, stdout="", stderr="usage"),
    )
    assert report_editor.read_model_facts_subprocess(tmp_path / "r.pbix") == {}


@pytest.mark.usefixtures("offline_m_checker")
def test_a_model_batch_starts_from_the_heads_own_measures(tmp_path: Path) -> None:
    env = make_env(tmp_path, model=model_state())
    assert [m["Name"] for m in env.report.measures][-2:] == [m["Name"] for m in MODEL_MEASURES]
    receipt = env.editor.apply(
        env.report,
        model_ops(
            {
                "op": "rename_column",
                "table": "FactSales",
                "name": "SalesAmount",
                "new_name": "Amount",
            }
        ),
        expected_head="r0000",
    )
    expressions = {m["Name"]: m["Expression"] for m in read_measures(head_path(env))}
    assert expressions["Total Sales"] == "SUM(FactSales[Amount])"
    assert receipt.summaries == [
        "Renamed the column FactSales[SalesAmount] to [Amount] and updated the visuals, filters "
        "and formulas that use it."
    ]
