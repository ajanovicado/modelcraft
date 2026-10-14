"""The imported-editing service: lanes, batches, rederivation, guards, recovery, chat port.

Power BI Desktop is the in-memory fake of the edit-pipeline tests (editing_support); the report
is the structure twin. Nothing here starts Desktop, a browser or the model.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from editing_support import (
    INITIAL_MEASURES,
    MEASURE_TABLE,
    OTHER_VISUAL_PART,
    PAGE,
    TABLES,
    TWIN,
    VISUAL,
    Crash,
    FakeDesktop,
    make_report,
    patch_diff,
    read_measures,
    sha,
    visual_x,
)

from ai_powerbi_analyst.studio import (
    imported_editing,
    report_delivery,
    report_edit_model,
    report_revisions,
)
from ai_powerbi_analyst.studio import report_edits as report_edits_module
from ai_powerbi_analyst.studio.desktop_session import (
    DesktopController,
    DesktopReadiness,
    DesktopSessionError,
    UnsavedDesktopWork,
)
from ai_powerbi_analyst.studio.edit_context import EditContext
from ai_powerbi_analyst.studio.edit_journal import EditJournal
from ai_powerbi_analyst.studio.imported_editing import (
    BatchOutcome,
    DesktopTools,
    ImportedEditError,
    ImportedEditingService,
    ModelTypes,
    NoDesktop,
    bridge_argv,
    classify,
    discover_desktop,
    extract_command,
    read_measures_bounded,
    run_extract,
)
from ai_powerbi_analyst.studio.report_editor import EditRejected, RestoreNeedsConfirmation
from ai_powerbi_analyst.studio.report_import import ImportedReport, read_report, report_archive
from ai_powerbi_analyst.studio.report_revisions import (
    Revision,
    RevisionBusy,
    RevisionConflict,
    RevisionError,
    UserChangesPending,
    WorkingCopyBusy,
)
from ai_powerbi_analyst.studio.state import EMPTY_HASH, StateConflictError, StudioStateStore

WORKSPACE = "workspace-1"
READY = DesktopReadiness(True, True, True, True, "2.157.1354.0", [])
MOVE = {"op": "set_position", "visual": VISUAL, "x": 50}
CREATE = {
    "op": "create_measure",
    "table": MEASURE_TABLE,
    "name": "Avg Discount",
    "expression": "1+1",
    "format_string": "0.0",
}
TYPES = {
    "columns": [
        {"table": "DimChannel", "name": "ChannelName", "dataType": 2, "summarizeBy": "none"},
        {"table": "FactSales", "name": "Amount", "dataType": 8, "summarizeBy": "sum", "key": True},
    ],
    "measures": [{"table": MEASURE_TABLE, "name": "Rank", "dataType": 6}],
    "calculated": {},
}


@pytest.fixture(autouse=True)
def _doubles(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    patch_diff(monkeypatch)
    monkeypatch.setattr(report_revisions, "RETRY_DELAY", 0.01)
    desktops: list[FakeDesktop] = []
    services: list[ImportedEditingService] = []
    desktop_init = FakeDesktop.__init__
    service_init = ImportedEditingService.__init__

    def track_desktop(desktop: FakeDesktop) -> None:
        desktop_init(desktop)
        desktops.append(desktop)

    def track_service(service: ImportedEditingService, *args: Any, **kwargs: Any) -> None:
        service_init(service, *args, **kwargs)
        services.append(service)

    monkeypatch.setattr(FakeDesktop, "__init__", track_desktop)
    monkeypatch.setattr(ImportedEditingService, "__init__", track_service)
    try:
        yield
    finally:
        for service in services:
            service.close(timeout=10)
        # A real user's Desktop remains open on shutdown. Only these test-owned
        # fake sessions hold Python file handles, which need explicit teardown.
        for desktop in desktops:
            for path in desktop.open_paths():
                desktop.user_closes(path)


@dataclass
class Rig:
    root: Path
    state: StudioStateStore
    service: ImportedEditingService
    desktop: FakeDesktop
    measure_reads: list[Path] = field(default_factory=list)
    type_reads: list[Path] = field(default_factory=list)
    launches: list[str] = field(default_factory=list)

    def report(self, workspace: str = WORKSPACE) -> ImportedReport:
        return load(self.state, workspace)

    def store(self, workspace: str = WORKSPACE) -> report_revisions.RevisionStore:
        return self.service.editor.store(self.report(workspace))

    @property
    def head(self) -> str:
        return self.store().head().id

    def edit(
        self,
        ops: list[dict[str, Any]] | None = None,
        *,
        key: str,
        expected_head: str | None = None,
        workspace: str = WORKSPACE,
        **kwargs: Any,
    ) -> BatchOutcome:
        handle = self.service.submit_edit(
            workspace,
            ops or [MOVE],
            expected_head=expected_head or self.store(workspace).head().id,
            idempotency_key=key,
            **kwargs,
        )
        return self.service.outcome(handle)

    def working(self, workspace: str = WORKSPACE) -> Path:
        return self.store(workspace).working_copy

    def open_desktop(self, *, normalize: bool = True, workspace: str = WORKSPACE) -> Any:
        self.store(workspace).materialize()
        return self.desktop.open(self.working(workspace), normalize=normalize)


def load(state: StudioStateStore, workspace: str) -> ImportedReport:
    found = state.list_objects("imported_report", ImportedReport, workspace)
    if not found:
        raise KeyError(f"Unknown workspace: {workspace}")
    return found[0]


def seed(state: StudioStateStore, report: ImportedReport) -> ImportedReport:
    pages, layout, _ = read_report(Path(report.archived_path))
    report = report.model_copy(update={"pages": pages, "format": layout})
    return state.put(
        "imported_report",
        report,
        workspace_id=report.workspace_id,
        expected_base_hash=EMPTY_HASH,
        idempotency_key=f"seed-{report.id}",
        mutation_payload={"seed": report.id},
    )


def make_service(
    root: Path,
    state: StudioStateStore,
    desktop: FakeDesktop | None,
    rig: Rig | None = None,
    *,
    readiness: DesktopReadiness = READY,
    wait: float = 10.0,
) -> ImportedEditingService:
    def measures(path: Path) -> list[dict[str, Any]]:
        if rig is not None:
            rig.measure_reads.append(path)
        return read_measures(path)

    def types(path: Path) -> dict[str, Any]:
        if rig is not None:
            rig.type_reads.append(path)
        return copy.deepcopy(TYPES)

    def launched() -> None:
        if rig is not None:
            rig.launches.append("launch")

    return ImportedEditingService(
        root / "state",
        state,
        load_report=lambda workspace: load(state, workspace),
        port_factory=(lambda: desktop) if desktop is not None else None,
        readiness_factory=lambda: readiness,
        read_measures=measures,
        read_types=types,
        before_launch=launched,
        wait_seconds=wait,
        disk_free=lambda _path: 10**12,
        profile_dir=root,
    )


def make_rig(
    tmp_path: Path, *, thin: bool = False, readiness: DesktopReadiness = READY, wait: float = 10.0
) -> Rig:
    report, _store, _source = make_report(tmp_path, thin=thin)
    state = StudioStateStore(tmp_path / "state" / "studio.sqlite3")
    seed(state, report)
    desktop = FakeDesktop()
    rig = Rig(tmp_path, state, None, desktop)  # type: ignore[arg-type]
    rig.service = make_service(tmp_path, state, desktop, rig, readiness=readiness, wait=wait)
    return rig


def add_report(rig: Rig, number: int) -> str:
    """A second imported report (its own folder, history and lane) in the same state."""
    report_id, workspace = f"report-{number}", f"workspace-{number}"
    root = rig.root / "state" / "reports" / report_id
    root.mkdir(parents=True)
    source = rig.root / "Downloads" / f"Second {number}.pbix"
    shutil.copyfile(TWIN, source)
    original = root / "original.pbix"
    shutil.copyfile(source, original)
    digest = sha(original)
    seed(
        rig.state,
        ImportedReport(
            id=report_id,
            workspace_id=workspace,
            name=f"Second {number}",
            source_path=str(source),
            archived_path=str(original),
            source_hash=digest,
            base_hash=digest,
            format="PBIR",
            pages=[],
            tables=copy.deepcopy(TABLES),
            measures=copy.deepcopy(INITIAL_MEASURES),
        ),
    )
    return workspace


def block_edits(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, report_id: str = "report-1"
) -> tuple[threading.Event, threading.Event]:
    """Hold every edit of one report inside the edit layer until the test lets it go."""
    started, release = threading.Event(), threading.Event()
    real = rig.service.editor.apply

    def slow(report: ImportedReport, ops: Any, **kwargs: Any) -> Any:
        if report.id == report_id:
            started.set()
            assert release.wait(20)
        return real(report, ops, **kwargs)

    monkeypatch.setattr(rig.service.editor, "apply", slow)
    return started, release


def wait_until(condition: Callable[[], bool], seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "the condition did not come true"
        time.sleep(0.01)


def raises_edit_error(code: str, status: int) -> Any:
    return pytest.raises(ImportedEditError, check=lambda e: (e.code, e.status) == (code, status))


# --- the lane and the batch registry --------------------------------------------------------------


def test_an_edit_runs_on_the_lane_and_moves_studios_view_to_the_new_head(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    before = rig.report()
    outcome = rig.edit(key="edit-key-0001")
    assert outcome.status == 200
    body = outcome.body
    head = rig.store().head()
    assert body["revision"]["id"] == "r0001" and body["revision"]["parent"] == "r0000"
    assert body["revision"]["author"] == "studio"
    assert body["head"] == {"id": "r0001", "hash": head.blob}
    # The words are the edit layer's; the revision row carries them joined.
    assert body["summaries"] and body["revision"]["summary"] == " ".join(body["summaries"])
    assert body["outcome"] == "updated" and body["desktop"] == "closed"
    assert body["adopted"] is None and body["protected"] == [] and body["replayed"] is False
    assert [(i["op"], i["where"]) for i in body["visible_in"]] == [("set_position", "studio")]
    # The stored report now points at the head file, with the head's pages.
    after = rig.report()
    assert report_archive(after) == (rig.store().path(head), head.blob)
    assert after.base_hash == head.blob and after.revision_hash == head.blob
    assert after.source_hash == before.source_hash
    moved = next(v for p in after.pages for v in p.visuals if v.id == VISUAL)
    assert moved.x == 50.0
    assert next(v for p in before.pages for v in p.visuals if v.id == VISUAL).x != 50.0
    # The outcome is in the workspace's event log for a reload to show.
    events = [e for e in rig.state.events(WORKSPACE) if e["type"] == "report.edit_applied"]
    assert events[-1]["payload"]["revision"] == "r0001"
    assert events[-1]["payload"]["summaries"] == body["summaries"]


def test_batches_of_one_report_run_one_after_the_other_and_reports_run_side_by_side(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    second = add_report(rig, 2)
    service = rig.service
    order: list[str] = []
    first_started, release_first = threading.Event(), threading.Event()
    other_done = threading.Event()

    def first(progress: Callable[[str], None]) -> dict[str, Any]:
        order.append("first start")
        first_started.set()
        assert other_done.wait(10), "a second report's batch must not wait for this one"
        assert release_first.wait(10)
        order.append("first end")
        return {"n": 1}

    def queued(progress: Callable[[str], None]) -> dict[str, Any]:
        order.append("queued run")
        return {"n": 2}

    def other(progress: Callable[[str], None]) -> dict[str, Any]:
        order.append("other run")
        other_done.set()
        return {"n": 3}

    one, two = rig.report(), rig.report(second)
    a = service.submit(one, "custom", "lane-key-0001", "a", first)
    assert first_started.wait(10)
    b = service.submit(one, "custom", "lane-key-0002", "b", queued)
    c = service.submit(two, "custom", "lane-key-0003", "c", other)
    assert c.wait(10) and c.result == {"n": 3}
    assert (a.state, b.state) == ("running", "queued")
    assert order == ["first start", "other run"]
    release_first.set()
    assert b.wait(10)
    assert a.result == {"n": 1} and b.result == {"n": 2}
    assert order == ["first start", "other run", "first end", "queued run"]


def test_two_edits_from_the_same_head_the_second_finds_the_first_has_moved_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path)
    started, release = block_edits(rig, monkeypatch)
    a = rig.service.submit_edit(
        WORKSPACE, [MOVE], expected_head="r0000", idempotency_key="race-key-0001"
    )
    assert started.wait(10)
    b = rig.service.submit_edit(
        WORKSPACE,
        [{"op": "set_position", "visual": VISUAL, "x": 70}],
        expected_head="r0000",  # the head both callers read
        idempotency_key="race-key-0002",
    )
    assert b.state == "queued"
    release.set()
    assert a.wait(10) and b.wait(10)
    assert a.state == "done" and b.state == "failed"
    assert b.error is not None and (b.error.code, b.error.status) == ("head_moved", 409)
    assert b.error.extra == {"expected": "r0000", "actual": "r0001"}
    assert [r.id for r in rig.store().history()] == ["r0001", "r0000"]
    assert visual_x(rig.store().path(rig.store().head())) == 50.0


def test_the_same_key_gives_the_recorded_outcome_and_a_running_batch_is_not_repeated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path)
    started, release = block_edits(rig, monkeypatch)
    handle = rig.service.submit_edit(
        WORKSPACE, [MOVE], expected_head="r0000", idempotency_key="same-key-0001"
    )
    assert started.wait(10)
    again = rig.service.submit_edit(
        WORKSPACE, [MOVE], expected_head="r0000", idempotency_key="same-key-0001"
    )
    assert again is handle  # a running key is the running batch
    release.set()
    first = rig.service.outcome(handle)
    replay = rig.service.outcome(
        rig.service.submit_edit(
            WORKSPACE, [MOVE], expected_head="r0000", idempotency_key="same-key-0001"
        )
    )
    assert replay.status == 200 and replay.body == first.body
    assert [r.id for r in rig.store().history()] == ["r0001", "r0000"]
    with raises_edit_error("idempotency_conflict", 409):
        rig.service.submit_edit(
            WORKSPACE,
            [{"op": "set_position", "visual": VISUAL, "x": 99}],
            expected_head="r0000",
            idempotency_key="same-key-0001",
        )


def test_a_failed_batch_may_be_asked_for_again_under_its_key(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.open_desktop(normalize=False)  # Desktop has unsaved changes: the batch asks first
    with raises_edit_error("desktop_unsaved", 409) as raised:
        rig.edit(key="retry-key-0001")
    assert raised.value.extra["choices"] == ["save_and_continue", "cancel"]
    rig.desktop.user_closes(rig.working())
    rig.store().adopt_working_copy(equivalent=report_revisions.semantic_equal)
    retried = rig.edit(key="retry-key-0001")  # the same request, once Desktop is closed
    assert retried.status == 200 and retried.body["revision"]["id"] == "r0001"


def test_a_long_batch_answers_202_and_is_polled_until_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path, wait=0.05)
    started, release = block_edits(rig, monkeypatch)
    handle = rig.service.submit_edit(
        WORKSPACE, [MOVE], expected_head="r0000", idempotency_key="slow-key-0001"
    )
    assert started.wait(10)
    outcome = rig.service.outcome(handle)
    assert outcome.status == 202
    assert outcome.body == {"batch_id": handle.id, "state": "running", "kind": "edit"}
    # A poll also carries the latest step, so a client that polls shows the stages a chat stream
    # would (the 202 answer itself is unchanged).
    assert rig.service.status(WORKSPACE, handle.id) == {
        "batch_id": handle.id,
        "state": "running",
        "kind": "edit",
        "stage": "planning",
        "label": "Getting ready",
    }
    handle.progress("Writing the change")
    assert rig.service.status(WORKSPACE, handle.id)["stage"] == "writing"
    assert rig.service.status(WORKSPACE, handle.id)["label"] == "Writing the change"
    assert rig.service.wait(handle, 0.01) is False  # still running
    release.set()
    assert rig.service.wait(handle, 10) is True
    polled = rig.service.status(WORKSPACE, handle.id)
    assert polled["state"] == "done" and polled["receipt"]["revision"]["id"] == "r0001"
    assert rig.service.outcome(handle).status == 200
    with pytest.raises(KeyError, match="Unknown batch"):
        rig.service.status(WORKSPACE, "no-such-batch")
    second = add_report(rig, 2)
    with pytest.raises(KeyError, match="Unknown batch"):
        rig.service.status(second, handle.id)  # another report's batch is not readable here


def test_a_batch_keeps_running_when_nobody_waits_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path, wait=0.0)
    started, release = block_edits(rig, monkeypatch)
    handle = rig.service.submit_edit(
        WORKSPACE, [MOVE], expected_head="r0000", idempotency_key="gone-key-0001"
    )
    assert started.wait(10)
    del handle  # the client went away
    release.set()
    wait_until(lambda: rig.head == "r0001")


def test_cancel_works_only_before_the_batch_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path)
    started, release = block_edits(rig, monkeypatch)
    a = rig.service.submit_edit(
        WORKSPACE, [MOVE], expected_head="r0000", idempotency_key="cancel-key-0001"
    )
    assert started.wait(10)
    b = rig.service.submit_edit(
        WORKSPACE,
        [{"op": "set_position", "visual": VISUAL, "x": 70}],
        expected_head="r0000",
        idempotency_key="cancel-key-0002",
    )
    running = rig.service.cancel(WORKSPACE, a.id)
    assert running["cancelled"] is False and running["state"] == "running"
    queued = rig.service.cancel(WORKSPACE, b.id)
    assert queued["cancelled"] is True and queued["state"] == "failed"
    assert queued["error"]["code"] == "cancelled"
    assert b.error is not None and (b.error.code, b.error.status) == ("cancelled", 409)
    release.set()
    assert a.wait(10)
    assert [r.id for r in rig.store().history()] == ["r0001", "r0000"]
    assert visual_x(rig.store().path(rig.store().head())) == 50.0  # b never ran
    # A cancelled batch is over: cancelling it again does nothing.
    assert rig.service.cancel(WORKSPACE, b.id)["cancelled"] is False
    with pytest.raises(KeyError):
        rig.service.cancel(WORKSPACE, "no-such-batch")


def test_progress_is_replayed_to_a_late_reader_and_beats_when_idle(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    release = threading.Event()
    at_closing = threading.Event()

    def job(progress: Callable[[str], None]) -> dict[str, Any]:
        progress("Closing Power BI Desktop")
        at_closing.set()
        assert release.wait(10)
        progress("Writing the change")
        progress("Opening Power BI Desktop")
        progress("A step nobody named")
        return {"ok": True}

    handle = rig.service.submit(rig.report(), "custom", "events-key-0001", "f", job)
    assert at_closing.wait(10)
    seen: list[Any] = []
    stream = handle.events(heartbeat=0.02)
    for event in stream:
        seen.append(event)
        if event is None:
            break  # nothing new for a moment: a streaming caller sends its heartbeat
    assert seen == [
        {"stage": "planning", "label": "Getting ready"},
        {"stage": "closing_desktop", "label": "Closing Power BI Desktop"},
        None,
    ]
    release.set()
    rest = [event for event in stream if event is not None]
    assert [e["stage"] for e in rest] == ["writing", "opening_desktop", "applying"]
    replay = [e for e in handle.events() if e is not None]  # a reader that attaches late
    assert [e["stage"] for e in replay] == [
        "planning",
        "closing_desktop",
        "writing",
        "opening_desktop",
        "applying",
    ]


def test_finished_listeners_and_the_finished_hook_are_called_once(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    finished: list[str] = []
    rig.service.on_batch_finished.append(lambda handle: finished.append(f"hook {handle.state}"))
    handle = rig.service.submit(rig.report(), "custom", "hook-key-00001", "f", lambda p: {"x": 1})
    handle.on_complete(lambda h: finished.append(f"listener {h.state}"))
    assert handle.wait(10)
    wait_until(lambda: len(finished) == 2)
    handle.on_complete(lambda h: finished.append("late"))
    assert sorted(finished) == ["hook done", "late", "listener done"]

    def broken(_handle: Any) -> None:
        raise RuntimeError("a listener bug")

    rig.service.on_batch_finished.append(broken)
    after = rig.service.submit(rig.report(), "custom", "hook-key-00002", "g", lambda p: {"x": 2})
    assert after.wait(10) and after.result == {"x": 2}  # a listener cannot disturb the lane


def test_the_registry_forgets_old_finished_batches_but_never_a_running_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path)
    monkeypatch.setattr(imported_editing, "MAX_BATCHES_IN_MEMORY", 3)
    releases = [threading.Event() for _ in range(6)]

    def job(index: int) -> Callable[[Callable[[str], None]], dict[str, Any]]:
        def held(_progress: Callable[[str], None]) -> dict[str, Any]:
            assert releases[index].wait(10)
            return {}

        return held

    handles = [
        rig.service.submit(rig.report(), "custom", f"trim-key-{n:05d}", str(n), job(n))
        for n in range(6)
    ]
    try:
        # Every submitted batch is still active: none may be forgotten to meet the cap.
        assert list(rig.service._batches) == [h.id for h in handles]  # noqa: SLF001
        releases[0].set()
        assert handles[0].wait(10)
        assert list(rig.service._batches) == [h.id for h in handles[1:]]  # noqa: SLF001
    finally:
        for release in releases:
            release.set()
    for handle in handles[1:]:
        assert handle.wait(10)
    kept = list(rig.service._batches)  # noqa: SLF001
    assert kept == [handle.id for handle in handles[-3:]]
    # The old one is still known to the mirror, so a poll answers from disk.
    assert rig.service.status(WORKSPACE, handles[0].id)["state"] == "done"


class Died(BaseException):
    """Something that is not an Exception: it ends the lane's thread."""


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_batch_that_kills_its_thread_fails_and_the_lane_still_serves_the_next(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)

    def dies(progress: Callable[[str], None]) -> dict[str, Any]:
        raise Died

    doomed = rig.service.submit(rig.report(), "custom", "died-key-00001", "a", dies)
    assert doomed.wait(10)
    assert doomed.state == "failed" and doomed.error is not None
    assert doomed.error.code == "edit_failed" and doomed.error.status == 500
    wait_until(lambda: rig.service._lane("report-1").current is None)  # noqa: SLF001
    later = rig.service.submit(rig.report(), "custom", "died-key-00002", "b", lambda p: {"ok": 1})
    assert later.wait(10) and later.result == {"ok": 1}
    # ...also when one was already waiting behind it.
    release = threading.Event()

    def held(progress: Callable[[str], None]) -> dict[str, Any]:
        assert release.wait(10)
        raise Died

    first = rig.service.submit(rig.report(), "custom", "died-key-00003", "c", held)
    behind = rig.service.submit(rig.report(), "custom", "died-key-00004", "d", lambda p: {"ok": 2})
    release.set()
    assert first.wait(10) and behind.wait(10)
    assert first.state == "failed" and behind.result == {"ok": 2}


def test_the_mirror_stays_whole_and_ends_on_the_true_state_when_many_batches_write_at_once(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    handles: list[Any] = []
    guard = threading.Lock()

    def submit(number: int) -> None:
        handle = rig.service.submit(
            rig.report(), "custom", f"burst-key-{number:04d}", str(number), lambda p: {"n": 1}
        )
        with guard:
            handles.append(handle)

    threads = [threading.Thread(target=submit, args=(n,)) for n in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(handles) == 40
    for handle in handles:
        assert handle.wait(10)
    mirror = tmp_path / "state" / "reports" / "report-1" / imported_editing.MIRROR_NAME
    last: dict[str, str] = {}
    for line in mirror.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)  # every line is one whole record
        last[record["batch"]] = record["state"]
    assert set(last) == {h.id for h in handles}
    assert set(last.values()) == {"done"}  # never a stale "queued" written after "done"


def test_the_mirror_keeps_only_the_newest_batches_when_it_grows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "batches.jsonl"
    lines = [
        json.dumps({"batch": f"b{n}", "state": state})
        for n in range(10)
        for state in ("queued", "running", "done")
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(imported_editing, "MIRROR_KEEP", 3)
    ImportedEditingService._trim_mirror(path)  # noqa: SLF001
    kept = {json.loads(line)["batch"] for line in path.read_text().splitlines()}
    assert kept == {"b7", "b8", "b9"}
    assert len(path.read_text().splitlines()) == 9


# --- the error table ------------------------------------------------------------------------------


class NeedsConfirmation(Exception):
    kind = "replace_conditional"
    detail = "This replaces the rules that color the table."


class NeedsChoice(Exception):
    question = "Which chart?"
    choices = [{"index": 0, "label": "The left one"}]


def _revision(number: int) -> Revision:
    return Revision(
        id=f"r{number:04d}",
        blob="a" * 64,
        parent=f"r{number - 1:04d}",
        author="studio",
        summary=f"Change {number}",
        created="2026-09-28T00:00:00+00:00",
        size=10,
    )


@pytest.mark.parametrize(
    ("error", "code", "status", "extra"),
    [
        (
            RevisionConflict("r0001", "r0002"),
            "head_moved",
            409,
            {"expected": "r0001", "actual": "r0002"},
        ),
        (
            UnsavedDesktopWork("Power BI Desktop has unsaved changes."),
            "desktop_unsaved",
            409,
            {"choices": ["save_and_continue", "cancel"]},
        ),
        (RevisionBusy("Another change is still being saved."), "edit_busy", 409, {}),
        (
            EditRejected(
                "Power BI Desktop is showing a dialog (Save changes?). Finish it in Power BI "
                "Desktop, then try again."
            ),
            "desktop_dialog",
            409,
            {"dialog": "Save changes?"},
        ),
        (
            EditRejected(
                "Power BI Desktop opened a dialog while saving. Finish it in Power BI Desktop; "
                "Studio did not change anything."
            ),
            "desktop_dialog",
            409,
            {"dialog": "a dialog in Power BI Desktop"},
        ),
        (EditRejected("Step 2: The report has no visual 'x'."), "edit_rejected", 422, {"step": 2}),
        (EditRejected("Plain refusal."), "edit_rejected", 422, {"step": None}),
        (WorkingCopyBusy("Desktop has the file open."), "desktop_busy", 409, {}),
        (UserChangesPending("A save is waiting."), "conflict", 409, {}),
        (StateConflictError("Stale."), "conflict", 409, {}),
        (DesktopSessionError("Power BI Desktop was closed"), "desktop_unavailable", 503, {}),
        (report_delivery.DeliveryError("Not delivered."), "edit_rejected", 422, {}),
        (RevisionError("There is nothing to undo"), "edit_rejected", 422, {}),
        (KeyError("Unknown workspace: w"), "edit_rejected", 422, {}),
        (PermissionError("denied"), "conflict", 409, {}),
        (
            NeedsConfirmation("Replace?"),
            "needs_confirmation",
            409,
            {
                "kind": "replace_conditional",
                "detail": "This replaces the rules that color the table.",
            },
        ),
        (
            NeedsChoice("Which?"),
            "needs_choice",
            409,
            {"question": "Which chart?", "choices": [{"index": 0, "label": "The left one"}]},
        ),
    ],
)
def test_every_error_maps_to_its_documented_code_status_and_fields(
    error: BaseException, code: str, status: int, extra: dict[str, Any]
) -> None:
    mapped = classify(error)
    assert (mapped.code, mapped.status) == (code, status)
    for key, value in extra.items():
        assert mapped.extra[key] == value
    payload = mapped.payload()
    assert payload["code"] == code and payload["message"] == payload["detail"] == mapped.message
    assert mapped.message  # always words a person can read


def test_the_edit_layers_questions_map_to_confirmation_and_choice_even_when_wrapped() -> None:
    from ai_powerbi_analyst.studio.report_edit_context import Choice
    from ai_powerbi_analyst.studio.report_edit_context import NeedsChoice as RealChoice
    from ai_powerbi_analyst.studio.report_edit_context import NeedsConfirmation as RealConfirm

    confirm = classify(
        RealConfirm("replace_conditional", "This replaces the rules that color the table.")
    )
    assert (confirm.code, confirm.status) == ("needs_confirmation", 409)
    assert confirm.extra == {
        "kind": "replace_conditional",
        "detail": "This replaces the rules that color the table.",
    }
    assert confirm.message == confirm.payload()["detail"] == confirm.extra["detail"]
    choice = classify(
        RealChoice(
            "Which chart do you mean?", [Choice("The left one"), Choice("All", {"scope": "all"})]
        )
    )
    assert (choice.code, choice.status) == ("needs_choice", 409)
    assert choice.extra["question"] == "Which chart do you mean?"
    assert choice.extra["choices"] == [
        {"index": 0, "label": "The left one"},
        {"index": 1, "label": "All", "fill": {"scope": "all"}},
    ]
    # An edit layer that wraps the refusal in its own exception does not hide the question.
    try:
        try:
            raise RealConfirm("remove_page_references", "Buttons still point at this page.")
        except RealConfirm as inner:
            raise EditRejected("The edit was refused.") from inner
    except EditRejected as wrapped:
        mapped = classify(wrapped)
    assert (mapped.code, mapped.extra["kind"]) == ("needs_confirmation", "remove_page_references")


def test_a_restore_that_leaves_changes_out_lists_them_and_an_unknown_error_hides_its_detail() -> (
    None
):
    mapped = classify(RestoreNeedsConfirmation([_revision(3), _revision(2)]))
    assert (mapped.code, mapped.status) == ("needs_confirmation", 409)
    assert mapped.extra["kind"] == "restore_discard"
    assert [row["id"] for row in mapped.extra["would_discard"]] == ["r0003", "r0002"]
    assert mapped.extra["would_discard"][0]["summary"] == "Change 3"
    assert "2 later changes" in mapped.message
    unexpected = classify(RuntimeError("boom at line 7 of secret.py"))
    assert (unexpected.code, unexpected.status) == ("edit_failed", 500)
    assert "boom" not in unexpected.message and "secret" not in unexpected.message
    already = ImportedEditError("head_moved", "Moved.", 409, expected="r1")
    assert classify(already) is already


def test_a_stale_head_is_refused_at_once_before_anything_is_queued(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.edit(key="edit-key-0001")
    with raises_edit_error("head_moved", 409) as raised:
        rig.service.submit_edit(
            WORKSPACE, [MOVE], expected_head="r0000", idempotency_key="edit-key-0002"
        )
    assert raised.value.extra == {"expected": "r0000", "actual": "r0001"}
    assert "changed while" in raised.value.message
    assert not rig.service._by_key.get(("report-1", "edit-key-0002"))  # noqa: SLF001


@pytest.mark.parametrize(
    ("ops", "code", "text", "extra"),
    [
        (
            [{"op": "set_sparkles", "visual": "x"}],
            "edit_unsupported",
            "set_sparkles",
            {"capability": "set_sparkles", "desktop_steps": []},
        ),
        (
            [{"visual": "x"}],
            "edit_unsupported",
            "Step 1",
            {"capability": "unknown", "desktop_steps": []},
        ),
        ([{"op": "set_position", "visual": VISUAL, "x": -5}], "edit_rejected", "'x'", {"step": 1}),
        (
            [MOVE, {"op": "add_shape", "page": "1", "fill": "not a color"}],
            "edit_rejected",
            "Step 2",
            {"step": 2},
        ),
        (
            [{"op": "delete_visual", "visual": "nonexistent"}],
            "edit_rejected",
            "nonexistent",
            {"step": None},
        ),
    ],
)
def test_operations_that_do_not_fit_are_refused_in_plain_words(
    tmp_path: Path, ops: list[dict[str, Any]], code: str, text: str, extra: dict[str, Any]
) -> None:
    rig = make_rig(tmp_path)
    with pytest.raises(ImportedEditError) as raised:
        rig.edit(ops, key="bad-ops-key-1")
    assert raised.value.code == code and raised.value.status == 422
    assert text in raised.value.message
    for name, value in extra.items():
        assert raised.value.extra[name] == value
    assert "pydantic" not in raised.value.message.casefold()
    assert rig.head == "r0000"


def test_a_missing_or_unknown_operation_name_is_unsupported_even_without_the_edit_layers_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The older layout validates with EditBatch (no parse_operations): the union's tag error
    is the same answer."""
    monkeypatch.delattr(imported_editing.report_edits, "parse_operations")
    for ops, capability in (
        ([{"op": "set_sparkles", "visual": "x"}], "set_sparkles"),
        ([{"visual": "x"}], "unknown"),
    ):
        with pytest.raises(ImportedEditError) as raised:
            ImportedEditingService.parse_operations(ops)
        assert (raised.value.code, raised.value.extra["capability"]) == (
            "edit_unsupported",
            capability,
        )
    with pytest.raises(ImportedEditError) as raised:
        ImportedEditingService.parse_operations(
            [MOVE, {"op": "add_shape", "page": "1", "fill": "not a color"}]
        )
    assert raised.value.code == "edit_rejected" and raised.value.extra["step"] == 2
    assert "Step 2" in raised.value.message and "'fill'" in raised.value.message


def test_desktop_states_that_stop_an_edit_map_to_their_codes(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.open_desktop(normalize=False)
    with raises_edit_error("desktop_unsaved", 409):
        rig.edit(key="state-key-0001")
    rig.desktop.save(rig.desktop.find(rig.working()))  # clean now
    rig.desktop.blocking = ["Save changes?"]
    with raises_edit_error("desktop_dialog", 409) as raised:
        rig.edit(key="state-key-0002")
    assert raised.value.extra == {"dialog": "Save changes?"}
    assert "dialog" in raised.value.message
    assert rig.head == "r0000" and rig.desktop.find(rig.working()) is not None


def test_saving_the_users_desktop_work_first_keeps_it_as_its_own_revision(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.open_desktop(normalize=False)
    rig.desktop.user_edit(
        rig.working(), OTHER_VISUAL_PART, lambda v: v["position"].__setitem__("x", 999.0)
    )
    with raises_edit_error("desktop_unsaved", 409):
        rig.edit(key="save-key-0001")
    outcome = rig.edit(key="save-key-0001", desktop_work="save")
    body = outcome.body
    assert body["adopted"]["author"] == "desktop" and body["adopted"]["id"] == "r0001"
    assert body["revision"]["id"] == "r0002" and body["desktop"] == "reopened"
    assert "saved in Power BI Desktop" in body["warnings"][0]
    head_file = rig.store().path(rig.store().head())
    assert visual_x(head_file, OTHER_VISUAL_PART) == 999.0 and visual_x(head_file) == 50.0
    # Studio's view follows the head, Desktop's save included.
    assert (
        next(
            v
            for p in rig.report().pages
            for v in p.visuals
            if v.id == OTHER_VISUAL_PART.split("/")[-2]
        ).x
        == 999.0
    )


# --- rederivation, the model and Desktop ----------------------------------------------------------


def test_undo_and_restore_move_the_view_back_and_point_at_the_original_again(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    rig.edit(key="edit-key-0001")
    rig.edit([{"op": "set_position", "visual": VISUAL, "x": 70}], key="edit-key-0002")
    assert rig.head == "r0002"
    undone = rig.service.outcome(
        rig.service.submit_undo(WORKSPACE, expected_head="r0002", idempotency_key="undo-key-0001")
    )
    assert undone.status == 200
    assert undone.body["revision"]["id"] == "r0003" and undone.body["revision"]["author"] == "undo"
    assert undone.body["summaries"] == [f"Undid r0002: {rig.store().revision('r0002').summary}"]
    moved = next(v for p in rig.report().pages for v in p.visuals if v.id == VISUAL)
    assert moved.x == 50.0
    # Restoring the original: later changes are listed first, then confirmed.
    with raises_edit_error("needs_confirmation", 409) as raised:
        rig.service.submit_restore(
            WORKSPACE,
            "r0000",
            expected_head="r0003",
            idempotency_key="restore-key-1",
        )
    assert raised.value.extra["kind"] == "restore_discard"
    assert [row["id"] for row in raised.value.extra["would_discard"]] == ["r0001"]
    preview = rig.service.discard_preview(WORKSPACE, "r0000")
    assert [row["id"] for row in preview["would_discard"]] == ["r0001"]
    assert preview["would_discard"][0]["operations"] == [
        {"op": "set_position", "summary": "Moved or resized a visual"}
    ]
    restored = rig.service.outcome(
        rig.service.submit_restore(
            WORKSPACE,
            "r0000",
            expected_head="r0003",
            idempotency_key="restore-key-1",
            confirmed_discard=True,
        )
    )
    assert restored.body["revision"]["author"] == "restore"
    after = rig.report()
    assert after.revision_path is None and after.revision_hash is None  # the original again
    assert report_archive(after) == (Path(after.archived_path), after.source_hash)
    assert after.base_hash == after.source_hash
    # Nothing to restore to itself; nothing before the original to undo.
    with raises_edit_error("edit_rejected", 422):
        rig.service.submit_restore(
            WORKSPACE, "r0004", expected_head="r0004", idempotency_key="restore-key-2"
        )


def test_history_rows_carry_what_the_history_pane_shows(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.edit(key="edit-key-0001", summary="Nudged the slicer")
    rig.service.outcome(
        rig.service.submit_undo(WORKSPACE, expected_head="r0001", idempotency_key="undo-key-0001")
    )
    rows = rig.service.history(WORKSPACE)
    assert [r["id"] for r in rows] == ["r0002", "r0001", "r0000"]
    newest, change, original = rows
    assert newest["author"] == "undo" and newest["restores"] == "r0000" and newest["is_head"]
    assert newest["can_undo"] is False  # nothing left to undo: the report is the import again
    assert change["author"] == "studio" and change["summary"] == "Nudged the slicer"
    assert change["operations"] == [{"op": "set_position", "summary": "Moved or resized a visual"}]
    assert change["available"] is True and change["conflict"] is False
    assert change["parent"] == "r0000" and change["size"] > 0
    assert original["author"] == "import" and "restores" not in original
    assert original["operations"] == []
    assert set(newest) >= {
        "id",
        "parent",
        "author",
        "summary",
        "created",
        "size",
        "available",
        "is_head",
        "can_undo",
        "conflict",
        "operations",
    }
    changed = rig.service.what_changed(WORKSPACE, 2)
    assert [r["id"] for r in changed] == ["r0002", "r0001"]


def test_a_model_change_other_than_a_measure_says_what_it_changes(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    hide = {"op": "set_column_properties", "table": "DimChannel", "name": "ChannelName"}
    steps = [
        {**hide, "hidden": True},
        {"op": "rename_table", "name": "DimChannel", "new_name": "Channel"},
    ]
    with raises_edit_error("needs_confirmation", 409) as raised:
        rig.edit(steps, key="model-key-0001")
    assert raised.value.message.startswith("Changing the model needs Power BI Desktop")
    assert raised.value.extra["detail"] == (
        "Hide DimChannel[ChannelName] in the model. Rename the table DimChannel to Channel."
    )
    assert rig.head == "r0000" and rig.desktop.events == []


@pytest.mark.parametrize(
    ("step", "line"),
    [
        (
            {"op": "set_column_properties", "table": "DimStore", "name": "StoreKey", "hidden": 1},
            "Hide DimStore[StoreKey] in the model.",
        ),
        (
            {"op": "rename_table", "name": "DimChannel", "new_name": "Channel"},
            "Rename the table DimChannel to Channel.",
        ),
        (
            {
                "op": "create_relationship",
                "from_table": "FactSales",
                "from_column": "StoreKey",
                "to_table": "DimStore",
                "to_column": "StoreKey",
            },
            "Create the relationship FactSales[StoreKey] → DimStore[StoreKey].",
        ),
        (
            {
                "op": "update_relationship",
                "from_table": "FactSales",
                "from_column": "StoreKey",
                "to_table": "DimStore",
                "to_column": "StoreKey",
                "cross_filter": "both",
                "active": False,
            },
            "Change the relationship FactSales[StoreKey] → DimStore[StoreKey]: filtering both "
            "ways, inactive.",
        ),
        (
            {
                "op": "set_column_properties",
                "table": "Date",
                "name": "Month",
                "sort_by": "Month Number",
                "format_string": "mmm",
            },
            "Format Date[Month] as “mmm”, and sort it by Month Number.",
        ),
        (
            {
                "op": "create_calculated_column",
                "table": "FactSales",
                "name": "Margin",
                "expression": "FactSales[SalesAmount] - FactSales[TotalCost]",
                "hidden": True,
            },
            "Create the calculated column FactSales[Margin], and hide it in the model.",
        ),
        (
            {"op": "mark_date_table", "table": "Date", "column": "Date"},
            "Mark Date as the date table on Date[Date].",
        ),
        (
            {
                "op": "create_hierarchy",
                "table": "Date",
                "name": "Calendar",
                "levels": [{"column": "Year"}, {"column": "Month", "name": "Mon"}],
            },
            "Create the hierarchy Calendar in Date (Year, Mon).",
        ),
        (
            {"op": "set_query_parameter", "name": "Region", "value": "East"},
            "Set the Power Query parameter Region to “East”.",
        ),
        ({"op": "refresh_data"}, "Refresh the data of every table."),
        ({"op": "delete_role", "name": "Auditors"}, "Delete the role Auditors."),
        (
            {"op": "set_model_options", "discourage_implicit_measures": True},
            "Discourage implicit measures.",
        ),
    ],
)
def test_the_model_confirmation_line_is_worded_from_the_operation(
    step: dict[str, Any], line: str
) -> None:
    (parsed,) = report_edits_module.parse_operations([step])
    assert imported_editing._model_line(parsed) == line  # noqa: SLF001


def test_every_model_card_example_gets_its_own_line_and_never_its_formula() -> None:
    for card in report_edit_model.TOOL_CARDS:
        for _request, example in card.examples:
            (parsed,) = report_edits_module.parse_operations([dict(example)])
            line = imported_editing._model_line(parsed)  # noqa: SLF001
            assert line != "Change the model." and line.endswith("."), card.op
            assert line[0].isupper(), (card.op, line)
            for secret in ("expression", "format_string_expression"):
                text = example.get(secret)
                if isinstance(text, str) and len(text) > 12:
                    assert text not in line, (card.op, line)
            for item in example.get("items") or example.get("filters") or []:
                formula = str(item.get("expression") or "")
                assert not formula or formula not in line, (card.op, line)


def test_a_model_change_is_gated_confirmed_and_read_back_into_the_view(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    with raises_edit_error("needs_confirmation", 409) as raised:
        rig.edit([CREATE], key="model-key-0001")
    assert raised.value.extra["kind"] == "model_edit"
    assert raised.value.extra["detail"] == "Create the measure Avg Discount in # Measures."
    assert rig.head == "r0000" and rig.desktop.events == []
    reads_before = len(rig.measure_reads)
    outcome = rig.edit([CREATE], key="model-key-0002", confirm_model=True)
    assert outcome.body["outcome"] == "updated" and outcome.body["desktop"] == "closed"
    report = rig.report()
    assert "Avg Discount" in {m["Name"] for m in report.measures}
    # The types are read for the edit itself (the original's model) and again with the measures
    # when the head's model changed: once each, in that order.
    assert [path.name for path in rig.type_reads][0] == "original.pbix"
    assert len(rig.type_reads) == 2
    # A report-only edit and its undo leave the model as it was: no model is read again.
    reads, type_reads = len(rig.measure_reads), len(rig.type_reads)
    assert reads > reads_before
    rig.edit(key="edit-key-0003")
    rig.service.outcome(
        rig.service.submit_undo(WORKSPACE, expected_head="r0002", idempotency_key="undo-key-0001")
    )
    assert len(rig.measure_reads) == reads and len(rig.type_reads) == type_reads
    assert "Avg Discount" in {m["Name"] for m in rig.report().measures}


def test_a_model_change_without_a_ready_desktop_is_refused_with_the_reasons(
    tmp_path: Path,
) -> None:
    readiness = DesktopReadiness(
        True, False, True, False, "2.157", ["The Power BI command-line tool is not installed"]
    )
    rig = make_rig(tmp_path, readiness=readiness)
    with raises_edit_error("desktop_unavailable", 503) as raised:
        rig.edit([CREATE], key="model-key-0001", confirm_model=True)
    assert raised.value.extra["reasons"] == ["The Power BI command-line tool is not installed"]
    assert "command-line tool" in raised.value.message
    assert rig.head == "r0000"
    # A report-only edit does not need any of it.
    assert rig.edit(key="edit-key-0001").status == 200


def test_without_power_bi_desktop_a_report_edit_still_works_and_a_model_edit_says_why(
    tmp_path: Path,
) -> None:
    report, _store, _source = make_report(tmp_path)
    state = StudioStateStore(tmp_path / "state" / "studio.sqlite3")
    seed(state, report)
    service = ImportedEditingService(
        tmp_path / "state",
        state,
        load_report=lambda workspace: load(state, workspace),
        read_measures=read_measures,
        disk_free=lambda _path: 10**12,
        profile_dir=tmp_path,
    )
    done = service.outcome(
        service.submit_edit(
            WORKSPACE, [MOVE], expected_head="r0000", idempotency_key="nodesk-key-1"
        )
    )
    assert done.status == 200 and done.body["desktop"] == "closed"
    with pytest.raises(ImportedEditError) as raised:
        service.submit_edit(
            WORKSPACE,
            [CREATE],
            expected_head="r0001",
            idempotency_key="nodesk-key-2",
            confirm_model=True,
        )
    assert (raised.value.code, raised.value.status) == ("desktop_unavailable", 503)
    payload = service.report_payload(WORKSPACE)
    assert payload["capabilities"]["edit_model"] is False
    assert payload["capabilities"]["edit_report"] is True
    assert payload["desktop"]["state"] == "closed"
    with pytest.raises(ImportedEditError, match="Install Power BI Desktop"):
        service.outcome(service.submit_open(WORKSPACE))


def test_a_failed_refresh_leaves_the_view_behind_until_the_next_read_heals_it(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    before = rig.report()
    rig.edit(key="edit-key-0001")
    head = rig.store().head()
    # Put the stored view back as it was before the edit (as if the refresh had failed).
    current = rig.report()
    rig.state.put(
        "imported_report",
        before,
        workspace_id=WORKSPACE,
        expected_base_hash=current.base_hash,
        idempotency_key="stale-view-0001",
        mutation_payload={"stale": True},
    )
    assert rig.report().revision_hash is None  # the stored view is behind the head
    healed = rig.service.report(WORKSPACE)
    assert healed.revision_hash == head.blob and healed.base_hash == head.blob
    assert next(v for p in healed.pages for v in p.visuals if v.id == VISUAL).x == 50.0
    assert rig.report().revision_hash == head.blob  # stored, not just returned
    with pytest.raises(KeyError):
        rig.service.report("workspace-nope")


def test_a_batch_that_starts_on_a_stale_view_refreshes_it_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path)
    before = rig.report()
    rig.edit(key="edit-key-0001")
    head = rig.store().head()
    current = rig.report()
    rig.state.put(
        "imported_report",
        before,
        workspace_id=WORKSPACE,
        expected_base_hash=current.base_hash,
        idempotency_key="stale-view-0001",
        mutation_payload={"stale": True},
    )
    seen: list[str | None] = []
    real = rig.service.editor.apply

    def spy(report: ImportedReport, ops: Any, **kwargs: Any) -> Any:
        seen.append(report.revision_hash)  # what the edit layer is handed
        return real(report, ops, **kwargs)

    monkeypatch.setattr(rig.service.editor, "apply", spy)
    outcome = rig.edit(
        [{"op": "set_position", "visual": VISUAL, "x": 70}],
        key="edit-key-0002",
        expected_head="r0001",
    )
    assert outcome.status == 200 and seen == [head.blob]


def test_a_view_is_not_healed_underneath_a_running_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path)
    started, release = block_edits(rig, monkeypatch)
    handle = rig.service.submit_edit(
        WORKSPACE, [MOVE], expected_head="r0000", idempotency_key="busy-key-0001"
    )
    assert started.wait(10)
    stale = rig.report()
    assert rig.service.report(WORKSPACE) == stale  # the running batch refreshes it itself
    release.set()
    assert handle.wait(10)


def test_the_source_file_is_hashed_once_and_desktop_is_not_asked_twice_in_a_burst(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path)
    hashed: list[Path] = []
    real = imported_editing.sha256_file
    monkeypatch.setattr(imported_editing, "sha256_file", lambda p: (hashed.append(p), real(p))[1])
    for _ in range(3):
        rig.service.report_payload(WORKSPACE)
    assert len(hashed) == 1
    source = Path(rig.report().source_path)
    source.write_bytes(source.read_bytes() + b"x")  # the user edits their own file
    assert rig.service.report_payload(WORKSPACE)["delivery"]["state"] == "source_changed"
    assert len(hashed) == 2


def test_the_report_payload_says_head_delivery_capabilities_and_desktop(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    fresh = rig.service.report_payload(WORKSPACE)
    assert fresh["id"] == "report-1" and fresh["pages"]  # the existing fields are all there
    head = rig.store().head()
    assert fresh["head"] == {
        "id": "r0000",
        "hash": head.blob,
        "edited": False,
        "desktop_version": None,
    }
    assert fresh["delivery"] == {
        "state": "in_sync",
        "source_path": rig.report().source_path,
        "changes_not_in_source": 0,
    }
    support = fresh["capabilities"].pop("model_support")
    assert fresh["capabilities"] == {
        "edit_report": True,
        "edit_model": True,
        "reasons": [],
        "thin_report": False,
    }
    # the fake Desktop's probe receipt covers every kind outside Microsoft's list
    assert support == imported_editing.model_support_payload(
        "2.157.1354.0", report_edit_model.OUTSIDE_SUPPORT
    )
    assert fresh["desktop"] == {"state": "closed", "detail": "", "working_copy": "absent"}
    rig.edit(key="edit-key-0001")
    edited = rig.service.report_payload(WORKSPACE)
    assert edited["head"]["id"] == "r0001" and edited["head"]["edited"] is True
    assert edited["delivery"]["state"] == "edited"
    assert edited["delivery"]["changes_not_in_source"] == 1
    assert edited["desktop"]["working_copy"] == "placed"
    rig.edit([{"op": "set_position", "visual": VISUAL, "x": 70}], key="edit-key-0002")
    assert rig.service.report_payload(WORKSPACE)["delivery"]["changes_not_in_source"] == 2


def test_capabilities_name_what_stops_a_model_edit(tmp_path: Path) -> None:
    thin = make_rig(tmp_path / "thin", thin=True)
    caps = thin.service.report_payload(WORKSPACE)["capabilities"]
    assert caps["thin_report"] is True and caps["edit_model"] is False
    assert caps["edit_report"] is True
    assert "shared model" in " ".join(caps["reasons"])
    # A thin report's report-layer edit works, with a warning that it cannot be checked.
    outcome = thin.edit(key="edit-key-0001")
    assert outcome.status == 200
    assert "no model" in outcome.body["warnings"][0]

    missing = DesktopReadiness(
        False, False, False, False, None, ["Power BI Desktop is not installed"]
    )
    rig = make_rig(tmp_path / "missing", readiness=missing)
    caps = rig.service.report_payload(WORKSPACE)["capabilities"]
    caps.pop("model_support")  # test_model_support_names_each_gated_operation_...
    assert caps == {
        "edit_report": True,
        "edit_model": False,
        "reasons": ["Power BI Desktop is not installed"],
        "thin_report": False,
    }
    # An older file layout cannot be edited at all, with the way out.
    legacy = make_rig(tmp_path / "legacy")
    current = legacy.report()
    legacy.state.put(
        "imported_report",
        current.model_copy(update={"format": "Legacy PBIX"}),
        workspace_id=WORKSPACE,
        expected_base_hash=current.base_hash,
        idempotency_key="legacy-layout-1",
        mutation_payload={"legacy": True},
    )
    caps = legacy.service.report_payload(WORKSPACE)["capabilities"]
    assert caps["edit_report"] is False and "older file layout" in caps["reasons"][0]
    with pytest.raises(ImportedEditError) as raised:
        legacy.edit(key="edit-key-0001")
    assert (raised.value.code, raised.value.status) == ("edit_unsupported", 422)
    assert raised.value.extra["capability"] == "legacy_layout"
    assert raised.value.extra["desktop_steps"]


def test_desktop_state_reports_open_unsaved_dialog_and_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(imported_editing, "STATE_TTL", 0.0)
    rig = make_rig(tmp_path)
    assert rig.service.desktop_state(rig.report())["state"] == "closed"
    rig.open_desktop(normalize=False)  # Desktop's own change: the flag is set
    state = rig.service.desktop_state(rig.report())
    assert state["state"] == "open_unsaved" and state["working_copy"] == "placed"
    rig.desktop.save(rig.desktop.find(rig.working()))
    assert rig.service.desktop_state(rig.report())["state"] == "open"
    rig.desktop.blocking = ["Save changes?"]
    assert rig.service.desktop_state(rig.report())["state"] == "dialog"
    assert rig.service.desktop_state(rig.report())["detail"] == "Save changes?"
    rig.desktop.blocking = []

    def broken(path: Path) -> Any:
        raise DesktopSessionError("The Power BI Desktop bridge could not be started")

    monkeypatch.setattr(rig.desktop, "find", broken)
    unknown = rig.service.desktop_state(rig.report())
    assert unknown["state"] == "unknown" and "bridge could not be started" in unknown["detail"]


def test_desktop_state_is_cached_for_a_moment_and_forgotten_when_a_batch_finishes(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    assert rig.service.desktop_state(rig.report())["state"] == "closed"
    rig.open_desktop()
    assert rig.service.desktop_state(rig.report())["state"] == "closed"  # cached
    rig.desktop.user_closes(rig.working())
    handle = rig.service.submit(rig.report(), "custom", "cache-key-0001", "c", lambda p: {})
    assert handle.wait(10)
    wait_until(lambda: "report-1" not in rig.service._state_cache)  # noqa: SLF001


def test_a_missing_desktop_is_looked_for_again_later_and_a_found_one_is_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    searches: list[int] = []
    found = DesktopTools(["bridge"], "pbi", "PBIDesktop.exe")

    def tools() -> DesktopTools:
        searches.append(1)
        return found

    def discover(_root: Path, tool_set: DesktopTools) -> Any:
        return imported_editing._Desktop(  # noqa: SLF001
            NoDesktop(), DesktopReadiness(False, False, False, False, None, ["x"])
        )

    monkeypatch.setattr(imported_editing, "discover_desktop", discover)
    state = StudioStateStore(tmp_path / "s.sqlite3")
    service = ImportedEditingService(
        tmp_path, state, load_report=lambda w: load(state, w), tools=tools
    )
    service._desktop_now()  # noqa: SLF001
    service._desktop_now()  # noqa: SLF001
    assert len(searches) == 1
    monkeypatch.setattr(imported_editing, "MISSING_TTL", 0.0)
    service._desktop_now()  # noqa: SLF001
    assert len(searches) == 2
    service.forget_desktop()
    # The first look after that starts in the background: a payload never waits for it.
    assert service.readiness() is None
    wait_until(lambda: len(searches) >= 3)


# --- delivery -------------------------------------------------------------------------------------


def test_save_a_copy_never_overwrites_and_stays_out_of_studios_own_folder(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.edit(key="edit-key-0001")
    target = tmp_path / "Out"
    target.mkdir()
    saved = rig.service.outcome(
        rig.service.submit_save_copy(
            WORKSPACE, str(target / "Agents (edited).pbix"), idempotency_key="copy-key-00001"
        )
    )
    assert saved.status == 200
    path = Path(saved.body["path"])
    head = rig.store().head()
    assert path == (target / "Agents (edited).pbix").resolve()
    assert saved.body["hash"] == head.blob == sha(path)
    # The same request again is the same copy; a new request never replaces it.
    replay = rig.service.outcome(
        rig.service.submit_save_copy(
            WORKSPACE, str(target / "Agents (edited).pbix"), idempotency_key="copy-key-00001"
        )
    )
    assert replay.body == saved.body
    second = rig.service.outcome(
        rig.service.submit_save_copy(
            WORKSPACE, str(target / "Agents (edited).pbix"), idempotency_key="copy-key-00002"
        )
    )
    assert Path(second.body["path"]).name == "Agents (edited) (2).pbix"
    assert sorted(p.name for p in target.iterdir()) == [
        "Agents (edited) (2).pbix",
        "Agents (edited).pbix",
    ]
    # A folder gets the default name ("<name> (edited)"), numbered past the copies already there.
    named = rig.service.outcome(
        rig.service.submit_save_copy(WORKSPACE, str(target), idempotency_key="copy-key-00003")
    )
    assert Path(named.body["path"]).name == "Agents (edited) (3).pbix"
    fresh_folder = tmp_path / "Empty"
    fresh_folder.mkdir()
    default = rig.service.outcome(
        rig.service.submit_save_copy(WORKSPACE, str(fresh_folder), idempotency_key="copy-key-00004")
    )
    assert Path(default.body["path"]).name == "Agents (edited).pbix"
    # The original file is untouched and delivery says the copy is not the user's file.
    assert sha(Path(rig.report().source_path)) == rig.report().source_hash
    assert rig.service.report_payload(WORKSPACE)["delivery"]["state"] == "edited"


@pytest.mark.parametrize(
    ("where", "message"),
    [
        ("state", "where Studio keeps its own files"),
        ("state/reports/report-1/sub/x.pbix", "where Studio keeps its own files"),
        ("notes.txt", "Save the report as a .pbix file"),
        ("relative.pbix", "Choose a full path"),
        ("missing/x.pbix", "does not exist"),
        ("", "Choose where to save"),
    ],
)
def test_save_a_copy_refuses_places_it_may_not_write(
    tmp_path: Path, where: str, message: str
) -> None:
    rig = make_rig(tmp_path)
    (tmp_path / "state" / "reports" / "report-1" / "sub").mkdir()
    if where == "state":
        destination = str(tmp_path / "state" / "Copy.pbix")
    elif where.startswith("state/"):
        destination = str(tmp_path / where)
    elif where in {"relative.pbix", ""}:
        destination = where
    elif where.startswith("missing"):
        destination = str(tmp_path / where)
    else:
        destination = str(tmp_path / where)
    if where == "notes.txt":
        (tmp_path / "notes.txt").write_text("x")
    with pytest.raises(ImportedEditError) as raised:
        rig.service.submit_save_copy(WORKSPACE, destination, idempotency_key="copy-key-00001")
    assert (raised.value.code, raised.value.status) == ("edit_rejected", 422)
    assert message in raised.value.message
    assert not list((tmp_path / "state").rglob("Copy.pbix"))


def test_save_a_copy_refuses_a_name_that_is_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path)
    target = tmp_path / "Out"
    target.mkdir()
    real = target / "real.pbix"
    real.write_bytes(b"mine")
    link = target / "Link.pbix"
    real_is_symlink = Path.is_symlink
    # Making a real link needs a privilege on Windows, so the name is reported as one.
    monkeypatch.setattr(
        Path, "is_symlink", lambda self: self.name == "Link.pbix" or real_is_symlink(self)
    )
    with pytest.raises(ImportedEditError, match="shortcut") as raised:
        rig.service.submit_save_copy(WORKSPACE, str(link), idempotency_key="copy-key-00001")
    assert raised.value.code == "edit_rejected"
    assert real.read_bytes() == b"mine" and not link.exists()
    # A real link, where the machine allows one, is refused the same way.
    monkeypatch.undo()
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        return
    with pytest.raises(ImportedEditError, match="shortcut"):
        rig.service.submit_save_copy(WORKSPACE, str(link), idempotency_key="copy-key-00002")
    assert real.read_bytes() == b"mine"


def test_replace_original_backs_up_writes_and_records_the_delivery(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.edit(key="edit-key-0001")
    source = Path(rig.report().source_path)
    original_bytes = source.read_bytes()
    outcome = rig.service.outcome(
        rig.service.submit_replace_original(WORKSPACE, idempotency_key="replace-key-001")
    )
    assert outcome.status == 200
    head = rig.store().head()
    assert Path(outcome.body["path"]) == source and outcome.body["hash"] == head.blob
    assert sha(source) == head.blob
    backup = Path(outcome.body["backup"])
    assert backup.read_bytes() == original_bytes and ".studio-backup-" in backup.name
    assert "Desktop signature" in outcome.body["warnings"][0]
    assert rig.service.report_payload(WORKSPACE)["delivery"]["state"] == "in_sync"
    # Asked again with the same key it does not write again.
    again = rig.service.outcome(
        rig.service.submit_replace_original(WORKSPACE, idempotency_key="replace-key-001")
    )
    assert again.body == outcome.body
    assert len(list(source.parent.glob("*.studio-backup-*"))) == 1


def test_replace_original_is_refused_when_the_users_file_changed_or_is_open(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    rig.edit(key="edit-key-0001")
    source = Path(rig.report().source_path)
    source.write_bytes(source.read_bytes() + b"the user kept working here")
    with raises_edit_error("edit_rejected", 422) as raised:
        rig.service.outcome(
            rig.service.submit_replace_original(WORKSPACE, idempotency_key="replace-key-001")
        )
    assert "changed outside Studio" in raised.value.message
    assert source.read_bytes().endswith(b"the user kept working here")
    assert not list(source.parent.glob("*.studio-backup-*"))
    shutil.copyfile(rig.store().original, source)  # back to what was imported
    rig.desktop.open(source, normalize=False)  # ...but the user has it open in Desktop
    with raises_edit_error("edit_rejected", 422) as raised:
        rig.service.outcome(
            rig.service.submit_replace_original(WORKSPACE, idempotency_key="replace-key-002")
        )
    assert "has Agents.pbix open" in raised.value.message


def test_a_delivery_first_records_a_save_the_user_made_in_desktop(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.open_desktop(normalize=False)
    rig.desktop.user_edit(
        rig.working(), OTHER_VISUAL_PART, lambda v: v["position"].__setitem__("x", 555.0)
    )
    rig.desktop.user_saves(rig.working())
    rig.desktop.user_closes(rig.working())
    target = tmp_path / "Out"
    target.mkdir()
    saved = rig.service.outcome(
        rig.service.submit_save_copy(
            WORKSPACE, str(target / "Copy.pbix"), idempotency_key="copy-key-00001"
        )
    )
    assert rig.head == "r0001"
    assert visual_x(Path(saved.body["path"]), OTHER_VISUAL_PART) == 555.0
    # The delivery moved the head, so Studio's own view moved with it.
    assert rig.report().revision_hash == rig.store().head().blob


# --- Open in Power BI -----------------------------------------------------------------------------


def test_open_in_power_bi_puts_the_head_in_the_working_copy_and_opens_it_once(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    rig.edit(key="edit-key-0001")
    opened = rig.service.open_body(rig.service.submit_open(WORKSPACE))
    assert opened.status == 200
    assert opened.body == {"launch_requested": True, "already_open": False}
    instance = rig.desktop.find(rig.working())
    assert instance is not None
    assert rig.launches == ["launch"]  # the model was unloaded before Desktop started
    assert visual_x(rig.working()) == 50.0  # the edited copy, not the imported one
    again = rig.service.open_body(rig.service.submit_open(WORKSPACE))
    assert again.body == {"launch_requested": True, "already_open": True}
    assert rig.launches == ["launch"]
    events = [e["type"] for e in rig.state.events(WORKSPACE)]
    assert events.count("report.desktop_launch_requested") == 2


def test_open_in_power_bi_first_adopts_a_save_made_while_studio_was_not_looking(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    rig.open_desktop(normalize=False)
    rig.desktop.user_edit(
        rig.working(), OTHER_VISUAL_PART, lambda v: v["position"].__setitem__("x", 321.0)
    )
    rig.desktop.user_saves(rig.working())
    rig.desktop.user_closes(rig.working())
    assert rig.service.open_body(rig.service.submit_open(WORKSPACE)).status == 200
    rows = rig.store().history()
    assert [(r.id, r.author) for r in rows] == [("r0001", "desktop"), ("r0000", "import")]
    assert visual_x(rig.working(), OTHER_VISUAL_PART) == 321.0
    assert rig.report().revision_hash == rows[0].blob


def test_open_in_power_bi_checks_the_preserved_original_and_desktop(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    Path(rig.report().archived_path).write_bytes(b"tampered archive")
    with pytest.raises(ImportedEditError, match="integrity check"):
        rig.service.outcome(rig.service.submit_open(WORKSPACE))
    assert rig.launches == [] and rig.desktop.instances == {}


def test_an_open_that_outlasts_the_wait_answers_with_the_launch_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path, wait=0.05)
    release = threading.Event()
    real = rig.service.editor.open_in_desktop

    def slow(report: ImportedReport) -> Any:
        assert release.wait(10)
        return real(report)

    monkeypatch.setattr(rig.service.editor, "open_in_desktop", slow)
    handle = rig.service.submit_open(WORKSPACE)
    body = rig.service.open_body(handle)
    assert body.status == 202
    assert body.body == {
        "batch_id": handle.id,
        "state": "running",
        "kind": "open",
        "launch_requested": True,
        "already_open": False,
    }
    release.set()
    assert handle.wait(10) and handle.result == {"launch_requested": True, "already_open": False}


# --- recovery -------------------------------------------------------------------------------------


def test_startup_recovery_rolls_a_committed_batch_forward_and_brings_the_view_to_the_head(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    report = rig.report()

    def crash(state: str) -> None:
        if state == "committed":
            raise Crash(state)

    rig.service.editor._checkpoint = crash  # noqa: SLF001
    with pytest.raises(Crash):
        rig.service.editor.apply(
            report,
            [imported_editing.report_edits.SetPosition(visual=VISUAL, x=50)],
            expected_head="r0000",
            idempotency_key="crash-key-0001",
        )
    rig.service.editor._checkpoint = None  # noqa: SLF001
    assert rig.head == "r0001"
    assert rig.report().revision_hash is None  # the crash came before the view was refreshed
    journal = EditJournal(rig.store().journal_path)
    assert [b.state for b in journal.open_batches()] == ["committed"]
    results = rig.service.recover_all()
    assert [b.action for b in results["report-1"].batches] == ["rolled_forward"]
    assert journal.open_batches() == []
    assert rig.report().revision_hash == rig.store().head().blob
    assert next(v for p in rig.report().pages for v in p.visuals if v.id == VISUAL).x == 50.0
    logged = [e for e in rig.state.events(WORKSPACE) if e["type"] == "report.edit_recovered"]
    assert logged[-1]["payload"]["batches"][0]["action"] == "rolled_forward"
    assert logged[-1]["payload"]["report_id"] == "report-1"


def test_startup_recovery_rolls_back_a_batch_that_never_committed(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    store = rig.store()
    EditJournal(store.journal_path).append(
        "orphan-batch",
        "planned",
        key="orphan-key-01",
        kind="edit",
        parent="r0000",
        ops=["set_position"],
    )
    (store.staging_dir).mkdir(parents=True, exist_ok=True)
    (store.staging_dir / "stage-left-behind.pbix").write_bytes(b"half a file")
    results = rig.service.recover_all()
    assert [b.action for b in results["report-1"].batches] == ["rolled_back"]
    assert results["report-1"].removed_staging == 1
    assert rig.head == "r0000"
    assert EditJournal(store.journal_path).open_batches() == []


def test_startup_recovery_reports_the_desktop_that_has_the_report_open_and_never_closes_it(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    instance = rig.open_desktop()
    events_before = list(rig.desktop.events)
    results = rig.service.recover_all()
    assert results["report-1"].desktop == instance
    assert rig.service.recovered_desktops == {"report-1": instance}
    assert rig.desktop.events == events_before  # nothing closed, saved or reopened
    logged = [e for e in rig.state.events(WORKSPACE) if e["type"] == "report.edit_recovered"]
    assert logged[-1]["payload"]["desktop_open"] is True
    assert logged[-1]["payload"]["desktop_pid"] == instance.pid


def test_recovery_never_raises_and_does_not_block_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path)
    second = add_report(rig, 2)
    rig.store().materialize()  # both were opened in Desktop once: there is a working copy
    rig.store(second).materialize()
    real = rig.service.editor.recover

    def flaky(report: ImportedReport) -> Any:
        if report.id == "report-1":
            raise RuntimeError("the disk went away")
        return real(report)

    monkeypatch.setattr(rig.service.editor, "recover", flaky)
    results = rig.service.recover_all()
    assert list(results) == ["report-2"]
    failed = [e for e in rig.state.events(WORKSPACE) if e["type"] == "report.edit_recovered"]
    assert failed[-1]["payload"]["error"] == "the disk went away"
    assert not [e for e in rig.state.events(second) if e["type"] == "report.edit_recovered"]

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("database is locked")

    monkeypatch.setattr(rig.state, "list_objects", broken)
    assert rig.service.recover_all() == {}
    # The background thread returns at once and does the same work.
    monkeypatch.undo()
    thread = rig.service.start_recovery()
    assert thread.daemon is True
    thread.join(10)
    assert not thread.is_alive()
    assert rig.service.start_recovery() is not None


def test_a_report_that_was_never_edited_or_opened_costs_recovery_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path)
    looked: list[Path] = []
    monkeypatch.setattr(rig.desktop, "find", lambda path: looked.append(path))
    assert rig.service.recover_all() == {}
    assert looked == []  # Desktop was not even asked
    assert not [e for e in rig.state.events(WORKSPACE) if e["type"] == "report.edit_recovered"]
    rig.store().materialize()  # opened once: a Desktop may have the working copy
    assert list(rig.service.recover_all()) == ["report-1"]
    assert looked == [rig.working()]


def test_startup_brings_a_view_that_fell_behind_its_head_up_to_date(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    before = rig.report()
    rig.edit(key="edit-key-0001")
    current = rig.report()
    rig.state.put(
        "imported_report",
        before,
        workspace_id=WORKSPACE,
        expected_base_hash=current.base_hash,
        idempotency_key="stale-view-0001",
        mutation_payload={"stale": True},
    )
    assert rig.report().revision_hash is None
    rig.service.recover_all()
    assert rig.report().revision_hash == rig.store().head().blob


def test_a_poll_after_a_restart_answers_from_the_mirror_and_the_journal(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    handle = rig.service.submit_edit(
        WORKSPACE, [MOVE], expected_head="r0000", idempotency_key="restart-key-01"
    )
    assert handle.wait(10)
    restarted = make_service(tmp_path, rig.state, rig.desktop, rig)
    # A finished batch: what it returned.
    again = restarted.status(WORKSPACE, handle.id)
    assert again["state"] == "done" and again["receipt"] == handle.result
    failed = rig.service.submit_edit(
        WORKSPACE, [MOVE], expected_head="r0001", idempotency_key="restart-key-02"
    )
    assert failed.wait(10)  # ...it moves the same visual to the same place: fine
    mirror = tmp_path / "state" / "reports" / "report-1" / imported_editing.MIRROR_NAME
    lines = [json.loads(line) for line in mirror.read_text().splitlines()]
    assert {line["state"] for line in lines if line["batch"] == handle.id} == {
        "queued",
        "running",
        "done",
    }
    # A batch the process died in: nothing in the journal, so it is reported as interrupted.
    dead = {
        "batch": "dead-batch",
        "key": "never-journaled",
        "kind": "edit",
        "state": "running",
        "ts": "2026-09-28T00:00:00+00:00",
        "workspace": WORKSPACE,
        "summary": "",
    }
    with mirror.open("a", encoding="utf-8") as out:
        out.write(json.dumps(dead) + "\n")
    interrupted = restarted.status(WORKSPACE, "dead-batch")
    assert interrupted["state"] == "failed"
    assert interrupted["error"]["code"] == "interrupted"
    assert "closed before this change finished" in interrupted["error"]["message"]
    # A batch the journal says finished (recovery rolled it forward) is answered as done.
    store = rig.store()
    EditJournal(store.journal_path).append(
        "journal-batch",
        "planned",
        key="journaled-key-1",
        kind="edit",
        parent="r0001",
        ops=["set_position"],
    )
    EditJournal(store.journal_path).append(
        "journal-batch",
        "done",
        recovered=True,
        receipt={"revision": "r0002", "summaries": ["Moved it."], "status": "partial"},
    )
    dead["batch"], dead["key"] = "rolled-batch", "journaled-key-1"
    with mirror.open("a", encoding="utf-8") as out:
        out.write(json.dumps(dead) + "\n")
    rolled = restarted.status(WORKSPACE, "rolled-batch")
    assert rolled["state"] == "done"
    assert rolled["receipt"]["summaries"] == ["Moved it."]
    assert rolled["receipt"]["outcome"] == "partial" and rolled["receipt"]["replayed"] is True
    assert rolled["receipt"]["head"]["id"] == rig.head == "r0002"
    with pytest.raises(KeyError, match="Unknown batch"):
        restarted.status(WORKSPACE, "never-heard-of")


def test_a_receipt_read_back_from_the_journal_names_the_desktop_work_it_kept(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    rig.edit(key="adopt-key-0001")
    store = rig.store()
    kept = store.revision("r0001")
    recorded = {"revision": "r0001", "summaries": ["Moved it."], "adopted": "r0001"}
    receipt = imported_editing.ImportedEditingService._receipt_from_journal(store, recorded)
    assert receipt["adopted"] == {
        "id": "r0001",
        "parent": kept.parent,
        "author": kept.author,
        "summary": kept.summary,
        "created": kept.created,
    }
    # an id the history no longer has, or none recorded, is "nothing adopted", never an error
    gone = imported_editing.ImportedEditingService._receipt_from_journal(
        store, {**recorded, "adopted": "r0099"}
    )
    none = imported_editing.ImportedEditingService._receipt_from_journal(
        store, {"revision": "r0001"}
    )
    assert gone["adopted"] is None and none["adopted"] is None


def test_a_repeated_key_after_a_restart_is_answered_by_the_journal_not_applied_twice(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    first = rig.edit(key="twice-key-00001")
    restarted = make_service(tmp_path, rig.state, rig.desktop, rig)
    replay = restarted.outcome(
        restarted.submit_edit(
            WORKSPACE, [MOVE], expected_head="r0001", idempotency_key="twice-key-00001"
        )
    )
    assert replay.body["replayed"] is True
    assert replay.body["revision"]["id"] == first.body["revision"]["id"] == "r0001"
    assert rig.head == "r0001"  # one revision, however many times it was asked for


def test_close_gives_a_running_batch_a_moment_then_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path)
    started, release = block_edits(rig, monkeypatch)
    handle = rig.service.submit_edit(
        WORKSPACE, [MOVE], expected_head="r0000", idempotency_key="close-key-0001"
    )
    assert started.wait(10)
    began = time.monotonic()
    rig.service.close(timeout=0.2)
    assert 0.15 <= time.monotonic() - began < 5
    assert handle.state == "running"
    release.set()
    rig.service.close(timeout=10)
    assert handle.state == "done"


# --- model types ----------------------------------------------------------------------------------


def test_model_types_are_read_once_per_model_and_kept_beside_the_history(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    report = rig.report()
    types = rig.service.model_types(report)
    assert isinstance(types, ModelTypes)
    assert types.data_type("DimChannel", "ChannelName") == "text"
    assert types.data_type("dimchannel", "channelname") == "text"  # names are not case-sensitive
    assert types.data_type("FactSales", "Amount") == "double"
    assert types.data_type(MEASURE_TABLE, "Rank") == "integer"
    assert types.data_type("FactSales", "Nope") is None
    amount = types.column("FactSales", "Amount")
    assert amount is not None and amount.summarize_by == "sum" and amount.key is True
    channel = types.column("DimChannel", "ChannelName")
    assert channel is not None and channel.summarize_by == "none"
    assert len(rig.type_reads) == 1
    assert rig.service.model_types(report) is types  # memory
    restarted = make_service(tmp_path, rig.state, rig.desktop, rig)
    again = restarted.model_types(report)
    assert again is not None and again.columns == types.columns
    assert len(rig.type_reads) == 1  # the sidecar file, not the model
    # A report-layer edit copies the model bytes, so the types stay valid.
    rig.edit(key="edit-key-0001")
    assert rig.service.model_types(rig.report()) is types
    # A model change writes a new model: it is read again, as part of moving the view to the new
    # head (not lazily by whoever asks next).
    rig.edit([CREATE], key="model-key-0001", confirm_model=True)
    assert len(rig.type_reads) == 2
    assert rig.service.model_types(rig.report()) is not types
    assert len(rig.type_reads) == 2


def test_model_types_are_none_without_a_model_or_when_the_read_fails(tmp_path: Path) -> None:
    thin = make_rig(tmp_path / "thin", thin=True)
    assert thin.service.model_types(thin.report()) is None
    rig = make_rig(tmp_path / "rig")

    def failing(path: Path) -> dict[str, Any]:
        raise EditRejected("cannot read")

    rig.service._read_types = failing  # noqa: SLF001
    assert rig.service.model_types(rig.report()) is None


def test_a_model_change_does_not_depend_on_reading_the_types(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    tries: list[Path] = []

    def failing(path: Path) -> dict[str, Any]:
        tries.append(path)
        raise EditRejected("cannot read")

    rig.service._read_types = failing  # noqa: SLF001
    outcome = rig.edit([CREATE], key="model-key-0001", confirm_model=True)
    assert outcome.body["outcome"] == "updated"
    assert "Avg Discount" in {m["Name"] for m in rig.report().measures}
    # Nothing in a create needs the types or the calculated DAX, so the receipt says nothing.
    assert outcome.body["warnings"] == []
    tried = len(tries)
    # The receipt says what was missing when the edit depends on it, and a failing model is not
    # read again on every edit.
    no_types = "Studio could not read the model's data types; values were treated as text."
    card = {
        "op": "add_visual",
        "page": "1",
        "visual_type": "card",
        "fields": [{"table": MEASURE_TABLE, "name": "Rank"}],
    }
    added = rig.edit([card], key="edit-key-0002")
    assert added.body["warnings"] == [no_types]
    moved = rig.edit(key="edit-key-0003")  # a move needs no types (its own overlap note stays)
    assert no_types not in moved.body["warnings"]
    assert len(tries) == tried
    # Renaming, changing or deleting a measure needs the calculated DAX, and says so.
    renamed = rig.edit(
        [{"op": "rename_measure", "name": "Avg Discount", "new_name": "Avg Disc"}],
        key="model-key-0002",
        confirm_model=True,
    )
    assert len(renamed.body["warnings"]) == 1
    assert renamed.body["warnings"][0].startswith(
        "Studio could not read the model's calculated columns and tables"
    )


def test_a_model_whose_types_cannot_be_read_is_tried_again_after_the_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path)
    tries: list[Path] = []
    healthy = rig.service._read_types  # noqa: SLF001

    def flaky(path: Path) -> dict[str, Any]:
        tries.append(path)
        if len(tries) == 1:
            raise EditRejected("cannot read")
        return healthy(path)

    rig.service._read_types = flaky  # noqa: SLF001
    report = rig.report()
    assert rig.service.model_facts(report) is None
    assert rig.service.model_facts(report) is None and len(tries) == 1  # within the backoff
    monkeypatch.setattr(imported_editing, "TYPES_BACKOFF", 0.0)
    found = rig.service.model_facts(report)
    assert len(tries) == 2 and found is not None
    types, calculated = found
    assert types is not None and calculated == {}
    assert [c["name"] for c in types["columns"]] == ["ChannelName", "Amount"]


def test_model_facts_give_the_edit_layer_types_and_calculated_dax(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    calculated = {"FactSales[Margin %]": "[Rank] / 2", "Metric Picker": "{ NAMEOF([Rank]) }"}
    rig.service._read_types = lambda path: {**TYPES, "calculated": calculated}  # noqa: SLF001
    types, found = rig.service.model_facts(rig.report()) or (None, None)
    assert found == calculated
    assert types is not None
    assert {(c["table"], c["name"], c["dataType"]) for c in types["columns"]} == {
        ("DimChannel", "ChannelName", "text"),
        ("FactSales", "Amount", "double"),
    }
    # the payload is what ModelInfo.from_report reads: types survive the round trip
    model = imported_editing.report_edits.ModelInfo.from_report(
        rig.report().tables, rig.report().measures, types, calculated=found
    )
    assert model.data_type("FactSales", "Amount") == "double"
    assert model.field_parameter_tables() == {"Metric Picker"}
    # a model whose metadata gave no types at all has none to offer, but its DAX is still known
    rig.service._types.clear()  # noqa: SLF001
    rig.service._types_path(rig.report().id).unlink()  # noqa: SLF001
    rig.service._read_types = lambda path: {  # noqa: SLF001
        "columns": [],
        "measures": [],
        "calculated": calculated,
    }
    assert rig.service.model_facts(rig.report()) == (None, calculated)


def test_an_old_sidecar_without_the_calculated_dax_is_read_again(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    report = rig.report()
    key = imported_editing.report_import.model_key(report)
    sidecar = rig.service._types_path(report.id)  # noqa: SLF001
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps({"model_key": key, "columns": [], "measures": []}), encoding="utf-8"
    )
    found = rig.service.model_types(report)
    assert found is not None and found.known and found.calculated == {}
    assert len(rig.type_reads) == 1
    assert json.loads(sidecar.read_text(encoding="utf-8"))["v"] == imported_editing.TYPES_VERSION


# --- reading a model in a child process -----------------------------------------------------------


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "extract_stand_in.py"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_child_process_reader_returns_the_json_it_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script(
        tmp_path,
        "import json, sys\n"
        "print(json.dumps([{'Name': 'M', 'TableName': 'T', 'flag': sys.argv[2],"
        " 'file': sys.argv[1]}]))\n",
    )
    monkeypatch.setattr(imported_editing, "_EXTRACT_SCRIPT", script)
    found = read_measures_bounded(tmp_path / "some.pbix")
    assert found == [
        {"Name": "M", "TableName": "T", "flag": "--measures", "file": str(tmp_path / "some.pbix")}
    ]
    assert run_extract(tmp_path / "x.pbix", "--types")[0]["flag"] == "--types"


@pytest.mark.parametrize(
    ("body", "limit"),
    [
        ("import sys\nsys.exit(3)\n", 30.0),
        ("print('not json at all')\n", 30.0),
        ("import time\ntime.sleep(60)\n", 0.4),
        ("print('x' * (17 * 1024 * 1024))\n", 30.0),
    ],
)
def test_the_child_process_reader_gives_up_in_plain_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, limit: float
) -> None:
    monkeypatch.setattr(imported_editing, "_EXTRACT_SCRIPT", _script(tmp_path, body))
    began = time.monotonic()
    with pytest.raises(EditRejected, match="could not read the model back"):
        run_extract(tmp_path / "x.pbix", "--measures", timeout=limit)
    assert time.monotonic() - began < 20  # the stuck child was stopped, not waited for


def test_the_child_process_reader_refuses_to_start_without_memory_and_wants_a_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Memory:
        available = 100 * 1024**2

    monkeypatch.setattr(imported_editing.psutil, "virtual_memory", lambda: Memory())
    with pytest.raises(EditRejected, match="512 MB of free memory"):
        run_extract(tmp_path / "x.pbix", "--measures")
    monkeypatch.undo()
    monkeypatch.setattr(
        imported_editing, "_EXTRACT_SCRIPT", _script(tmp_path, "print('{\"a\": 1}')\n")
    )
    with pytest.raises(EditRejected, match="measures back"):
        read_measures_bounded(tmp_path / "x.pbix")
    monkeypatch.setattr(imported_editing, "_EXTRACT_SCRIPT", _script(tmp_path, "print('[1]')\n"))
    with pytest.raises(EditRejected, match="data types"):
        imported_editing.read_types_bounded(tmp_path / "x.pbix")  # a list, not the types dict


def test_the_extract_command_runs_the_script_from_source_and_the_module_in_the_packaged_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "x.pbix"
    assert extract_command(target, "--types") == [
        sys.executable,
        str(imported_editing._EXTRACT_SCRIPT),  # noqa: SLF001
        str(target),
        "--types",
    ]
    assert Path(extract_command(target, "--measures")[1]).name == "pbix_extract.py"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert extract_command(target, "--types") == [
        sys.executable,
        "-m",
        "ai_powerbi_analyst.studio.pbix_extract",
        str(target),
        "--types",
    ]


def test_the_child_process_is_run_without_a_console_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    real = imported_editing.subprocess.Popen

    def spy(argv: list[str], **kwargs: Any) -> Any:
        seen.update(kwargs)
        seen["argv"] = argv
        return real(argv, **kwargs)

    monkeypatch.setattr(imported_editing.subprocess, "Popen", spy)
    monkeypatch.setattr(imported_editing, "_EXTRACT_SCRIPT", _script(tmp_path, "print('[]')\n"))
    run_extract(tmp_path / "x.pbix", "--measures")
    assert seen["argv"][0] == sys.executable and seen["argv"][-1] == "--measures"
    assert seen["shell"] is False
    assert seen["creationflags"] == getattr(imported_editing.subprocess, "CREATE_NO_WINDOW", 0)


# --- finding Power BI Desktop ---------------------------------------------------------------------


def test_the_bridge_command_runs_node_directly_and_never_through_cmd(tmp_path: Path) -> None:
    folder = tmp_path / "node"
    entry = folder / "node_modules/@microsoft/powerbi-desktop-bridge-cli/dist/index.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("//")
    (folder / "node.exe").write_text("")
    shim = folder / "powerbi-desktop.cmd"
    shim.write_text("")
    assert bridge_argv(str(shim)) == [str(folder / "node.exe"), str(entry)]
    (folder / "node.exe").unlink()
    assert bridge_argv(str(shim)) == []  # an incomplete install is not guessed at
    assert bridge_argv(str(tmp_path / "bridge.exe")) == [str(tmp_path / "bridge.exe")]
    assert bridge_argv("") == []


def test_without_the_tools_desktop_is_the_null_port_with_the_reasons(tmp_path: Path) -> None:
    found = discover_desktop(
        tmp_path, DesktopTools([], "", "", ("Microsoft bridge 0.1.2 is required",))
    )
    assert isinstance(found.port, NoDesktop) and found.usable is False
    assert found.readiness.can_edit_model is False
    assert "Microsoft bridge 0.1.2 is required" in found.readiness.reasons
    assert "Power BI Desktop is not installed" in found.readiness.reasons
    # No reason is added twice: the bridge's own is already named.
    assert not any("bridge is not installed" in r for r in found.readiness.reasons)
    port = found.port
    assert port.find(tmp_path / "x.pbix") is None and port.version() is None
    for call in (
        lambda: port.status(None),  # type: ignore[arg-type]
        lambda: port.open(tmp_path / "x.pbix"),
        lambda: port.save(None),  # type: ignore[arg-type]
        lambda: port.close(None),  # type: ignore[arg-type]
        lambda: port.apply_measure_operations(None, [], {}),  # type: ignore[arg-type]
        lambda: port.screenshot(None, "p", tmp_path),  # type: ignore[arg-type]
        lambda: port.blocking_windows(None),  # type: ignore[arg-type]
    ):
        with pytest.raises(DesktopSessionError, match="Power BI Desktop is needed"):
            call()


def test_with_the_tools_present_readiness_needs_a_passing_receipt_for_the_installed_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "PBIDesktop.exe"
    exe.write_text("")
    bridge = tmp_path / "bridge.exe"
    bridge.write_text("")
    pbi = tmp_path / "pbi.exe"
    pbi.write_text("")
    monkeypatch.setattr(DesktopController, "version", lambda self: "2.157.1354.0")
    tools = DesktopTools([str(bridge)], str(pbi), str(exe))
    state = tmp_path / "state"
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setattr(imported_editing, "default_receipts_dir", lambda: bundled)
    found = discover_desktop(state, tools)
    assert found.usable and isinstance(found.port, DesktopController)
    assert found.readiness.can_edit_model is False
    assert any("has not been checked" in reason for reason in found.readiness.reasons)
    # A check run on this computer counts; so does one that ships with Studio.
    receipts = state / "desktop-receipts"
    receipts.mkdir(parents=True)
    (receipts / "desktop-2.157.1354.0.json").write_text(
        json.dumps({"desktop_version": "2.157.1354.0", "passed": True})
    )
    assert discover_desktop(state, tools).readiness.can_edit_model is True
    (receipts / "desktop-2.157.1354.0.json").unlink()
    (bundled / "desktop-2.157.1354.0.json").write_text(
        json.dumps({"desktop_version": "2.157.1354.0", "passed": False})
    )
    failed = discover_desktop(state, tools)
    assert failed.readiness.can_edit_model is False
    assert any("failed Studio's compatibility check" in r for r in failed.readiness.reasons)
    # A tool that is present but the wrong version keeps its own reason first.
    wrong = discover_desktop(
        state, DesktopTools([str(bridge)], str(pbi), str(exe), ("pbi-cli 3.11.1 is required",))
    )
    assert wrong.readiness.reasons[0] == "pbi-cli 3.11.1 is required"


# --- the chat port --------------------------------------------------------------------------------


def test_the_chat_port_edits_on_the_lane_and_reports_progress_to_the_caller(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    port = rig.service.port
    assert port.head(WORKSPACE) == {"id": "r0000", "hash": rig.store().head().blob}
    stages: list[str] = []
    receipt = port.apply(
        WORKSPACE,
        [MOVE],
        summary="Moved Country to x=50",
        idempotency_key="chat-key-00001",
        expected_head="r0000",
        progress=lambda event: stages.append(event["stage"]),
    )
    assert receipt["revision"]["summary"] == "Moved Country to x=50"
    assert receipt["revision"]["id"] == "r0001" and receipt["outcome"] == "updated"
    assert stages == ["planning", "writing"]
    context = port.edit_context(WORKSPACE, VISUAL, PAGE)
    assert isinstance(context, EditContext)
    assert context.head_revision == "r0001"
    assert context.selection == VISUAL and context.active_page == PAGE
    assert context.last_edit is not None
    assert context.last_edit.revision_id == "r0001"
    assert context.last_edit.ops[0]["op"] == "set_position"
    assert context.last_edit.summary == "Moved Country to x=50"
    assert context.visual(VISUAL) is not None and context.page(PAGE) is not None
    assert {m.name for m in context.measures} == {m["Name"] for m in INITIAL_MEASURES}
    port.remember_last_edit(WORKSPACE, "r0001", [MOVE], "Moved Country")
    noted = port.edit_context(WORKSPACE, None, None).last_edit
    assert noted is not None and noted.summary == "Moved Country" and noted.ops == (MOVE,)
    # ...but only while that revision is the head.
    rig.edit([{"op": "set_position", "visual": VISUAL, "x": 70}], key="edit-key-0002")
    newest = port.edit_context(WORKSPACE, None, None).last_edit
    assert newest is not None and newest.revision_id == "r0002"
    undone = port.undo(WORKSPACE, expected_head="r0002", idempotency_key="chat-undo-001")
    assert undone["revision"]["author"] == "undo"
    assert [r["id"] for r in port.history(WORKSPACE)][:2] == ["r0003", "r0002"]
    assert [r["id"] for r in port.what_changed(WORKSPACE, 1)] == ["r0003"]
    with pytest.raises(ImportedEditError) as raised:
        port.restore(WORKSPACE, "r0000", expected_head="r0003", idempotency_key="chat-restore-1")
    assert raised.value.code == "needs_confirmation"
    restored = port.restore(
        WORKSPACE,
        "r0000",
        expected_head="r0003",
        idempotency_key="chat-restore-1",
        confirmed_discard=True,
    )
    assert restored["revision"]["author"] == "restore"


def test_the_chat_port_raises_the_apis_errors(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    port = rig.service.port
    with pytest.raises(ImportedEditError) as raised:
        port.apply(
            WORKSPACE,
            [MOVE],
            summary=None,
            idempotency_key="chat-key-00001",
            expected_head="r0009",
        )
    assert raised.value.code == "head_moved"
    with pytest.raises(ImportedEditError) as raised:
        port.apply(
            WORKSPACE,
            [CREATE],
            summary=None,
            idempotency_key="chat-key-00002",
            expected_head="r0000",
        )
    assert raised.value.code == "needs_confirmation"
    applied = port.apply(
        WORKSPACE,
        [CREATE],
        summary=None,
        idempotency_key="chat-key-00003",
        expected_head="r0000",
        confirm_model=True,
    )
    assert applied["summaries"] and applied["revision"]["id"] == "r0001"


def test_the_palette_maps_the_themes_colors_to_the_words_people_use(tmp_path: Path) -> None:
    from ai_powerbi_analyst.studio.edit_context import hue_family
    from ai_powerbi_analyst.studio.report_formatting import theme

    rig = make_rig(tmp_path)
    report = rig.report()
    archive, blob = report_archive(report)
    colors = [c for c in theme(str(archive), blob).get("dataColors", []) if isinstance(c, str)]
    expected: dict[str, str] = {}
    for color in colors:
        hue = hue_family(color)
        if hue and hue not in expected:
            expected[hue] = color
    context = rig.service.edit_context(WORKSPACE, None, None)
    assert expected  # the twin's theme has colors
    assert context.palette == expected
    # The first entry is the theme's primary data color (edit_ops.data_color reads it).
    assert list(context.palette) == list(expected)


def test_operations_that_ask_before_replacing_get_the_confirmation_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_powerbi_analyst.contracts import ClosedModel

    class Guarded(ClosedModel):
        op: str = "guarded"
        visual: str
        confirm_replace: bool = False

    seen: list[list[dict[str, Any]]] = []

    def parse(raw: list[dict[str, Any]]) -> list[Any]:
        seen.append(raw)
        return [Guarded(**item) for item in raw]

    monkeypatch.setattr(imported_editing.report_edits, "parse_operations", parse, raising=False)
    ops = ImportedEditingService.parse_operations(
        [{"op": "guarded", "visual": "a"}], confirm_replace=True
    )
    assert ops[0].confirm_replace is True
    assert seen[0] == [{"op": "guarded", "visual": "a"}]
    assert seen[1] == [{"op": "guarded", "visual": "a", "confirm_replace": True}]
    plain = ImportedEditingService.parse_operations([{"op": "guarded", "visual": "a"}])
    assert plain[0].confirm_replace is False


def test_the_editor_reads_measures_with_the_services_bounded_reader(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    assert rig.service.editor._read_measures is rig.service._read_measures  # noqa: SLF001
    default = ImportedEditingService(
        tmp_path / "other", rig.state, load_report=lambda w: load(rig.state, w)
    )
    assert default.editor._read_measures is read_measures_bounded  # noqa: SLF001


def test_a_receipt_says_what_conditional_formatting_the_edit_replaced(tmp_path: Path) -> None:
    """The receipt names the conditional rule the user chose to replace."""
    rig = make_rig(tmp_path)
    table = "e82e02f93055d6b0b544"  # tableEx with four conditional font colors
    ops = [
        {
            "op": "set_conditional_format",
            "visual": table,
            "style": "gradient",
            "target": "font_color",
            "field": "Rank",
            "replace_conditional": True,
        }
    ]
    outcome = rig.edit(ops, key="edit-key-0001")
    assert outcome.status == 200
    replaced = outcome.body["replaced"]
    assert len(replaced) == 1
    assert "measure" in replaced[0] and "Rank column" in replaced[0]
    assert [(v["op"], v["where"]) for v in outcome.body["visible_in"]] == [
        ("set_conditional_format", "studio")
    ]
    # A plain edit protects nothing.
    plain = rig.edit(key="edit-key-0002")
    assert plain.body["protected"] == []


def test_a_layout_change_is_drawn_by_studio_even_for_a_visual_type_it_only_frames(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    report = rig.report()
    page = report.pages[0]
    gauge = page.visuals[0].model_copy(update={"id": "gauge-1", "visual_type": "gauge"})
    framed = report.model_copy(
        update={"pages": [page.model_copy(update={"visuals": [*page.visuals, gauge]})]}
    )
    ops: list[dict[str, Any]] = [
        {"op": "place_visual", "visual": "gauge-1", "relative_to": VISUAL, "side": "below"},
        {"op": "swap_visuals", "visual": "gauge-1", "with": VISUAL},
        {"op": "align_visuals", "visuals": ["gauge-1", VISUAL], "edge": "left"},
        {"op": "set_analytics_line", "visual": "gauge-1", "kind": "constant", "value": 1},
    ]
    found = [(row["op"], row["where"]) for row in rig.service._visible_in(framed, ops)]
    assert found == [
        ("place_visual", "studio"),
        ("swap_visuals", "studio"),
        ("align_visuals", "studio"),
        ("set_analytics_line", "powerbi_only"),
    ]


def test_reading_the_payload_refreshes_a_stale_desktop_answer_in_the_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user who installs Power BI Desktop after Studio started sees it without a restart, and
    the payload that notices the stale answer still returns at once with the old one."""
    searches: list[int] = []
    installed = threading.Event()

    def discover(_root: Path, _tools: DesktopTools) -> Any:
        searches.append(1)
        if installed.is_set():
            return imported_editing._Desktop(FakeDesktop(), READY)  # noqa: SLF001
        return imported_editing._Desktop(  # noqa: SLF001
            NoDesktop(), DesktopReadiness(False, False, False, False, None, ["not installed"])
        )

    monkeypatch.setattr(imported_editing, "discover_desktop", discover)
    monkeypatch.setattr(imported_editing, "MISSING_TTL", 0.05)
    state = StudioStateStore(tmp_path / "s.sqlite3")
    service = ImportedEditingService(
        tmp_path,
        state,
        load_report=lambda w: load(state, w),
        tools=lambda: DesktopTools(["bridge"], "pbi", "PBIDesktop.exe"),
    )
    assert service.readiness() is None  # the first look starts in the background
    wait_until(lambda: len(searches) == 1 and not service._discovering)  # noqa: SLF001
    first = service.readiness()
    assert first is not None and first.can_edit_model is False
    installed.set()
    time.sleep(0.06)
    stale = service.readiness()  # returns the old answer at once and starts a new search
    assert stale is not None and stale.can_edit_model is False
    wait_until(lambda: len(searches) == 2)
    wait_until(lambda: (r := service.readiness()) is not None and r.can_edit_model is True)


def test_desktop_state_is_unknown_not_closed_while_the_first_search_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    def discover(_root: Path, _tools: DesktopTools) -> Any:
        assert release.wait(20)
        return imported_editing._Desktop(  # noqa: SLF001
            NoDesktop(), DesktopReadiness(False, False, False, False, None, ["not installed"])
        )

    monkeypatch.setattr(imported_editing, "discover_desktop", discover)
    rig = make_rig(tmp_path)
    service = ImportedEditingService(
        tmp_path / "other",
        rig.state,
        load_report=lambda w: load(rig.state, w),
        tools=lambda: DesktopTools(["bridge"], "pbi", "PBIDesktop.exe"),
    )
    report = rig.report()
    looking = service.desktop_state(report)
    assert looking["state"] == "unknown"
    assert looking["detail"] == "Still looking for Power BI Desktop"
    # ... and that answer is not remembered for the next few seconds.
    release.set()
    wait_until(lambda: service.desktop_state(report)["state"] == "closed")


def test_a_view_that_cannot_be_healed_is_not_retried_on_every_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path)
    before = rig.report()
    rig.edit(key="edit-key-0001")
    head = rig.store().head()
    current = rig.report()
    rig.state.put(
        "imported_report",
        before,
        workspace_id=WORKSPACE,
        expected_base_hash=current.base_hash,
        idempotency_key="stale-view-0001",
        mutation_payload={"stale": True},
    )
    attempts: list[str] = []
    real = rig.service._rederive  # noqa: SLF001

    def failing(report: ImportedReport, path: Path, blob: str, changed: bool) -> ImportedReport:
        attempts.append(blob)
        raise EditRejected("The model could not be read")

    monkeypatch.setattr(rig.service, "_rederive", failing)
    for _ in range(5):
        assert rig.service.report(WORKSPACE).revision_hash is None
    assert attempts == [head.blob]  # one costly attempt, then the same head is left alone
    # After the backoff the read tries again, and a heal that works clears the memory.
    monkeypatch.setattr(imported_editing, "HEAL_BACKOFF", 0.0)
    assert rig.service.report(WORKSPACE).revision_hash is None
    assert attempts == [head.blob, head.blob]
    monkeypatch.setattr(rig.service, "_rederive", real)
    assert rig.service.report(WORKSPACE).revision_hash == head.blob
    assert "report-1" not in rig.service._heal_failed  # noqa: SLF001


def test_the_sidecar_build_bundles_the_receipts_the_packaged_service_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the receipts in the onefile, no packaged Studio could ever turn model edits on."""
    build = Path(__file__).resolve().parents[2] / "scripts" / "build-desktop-sidecar.ps1"
    script = build.read_text(encoding="utf-8")
    destination = r"evals\imported_edit\evidence"
    assert f"$receiptEvidence = Join-Path $repository '{destination}'" in script
    # Added only when the folder exists, so a build before the first contract check still works,
    # and passed to PyInstaller with the other data.
    assert "if (Test-Path -LiteralPath $receiptEvidence -PathType Container)" in script
    assert f'"$receiptEvidence;{destination}"' in script
    assert "@receiptData" in script
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    folder = imported_editing._receipts_folder(tmp_path / "state", "2.157.1354.0")  # noqa: SLF001
    assert folder == tmp_path / "bundle" / Path(*destination.split("\\"))


# --- answers to the edit layer's questions ------------------------------------------------------

CONDITIONAL = {
    "op": "set_conditional_format",
    "visual": "e82e02f93055d6b0b544",
    "style": "gradient",
    "field": "Rank",
}


def test_a_question_names_its_step_and_offers_each_answer_as_the_fill_that_makes_it(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    with raises_edit_error("needs_choice", 409) as raised:
        rig.edit([MOVE, CONDITIONAL], key="choice-key-0001")
    extra = raised.value.extra
    assert extra["step"] == 2  # counted from 1: the conditional format after the move
    assert [c["fill"] for c in extra["choices"]] == [
        {"target": "font_color"},
        {"target": "background"},
    ]
    assert [c["index"] for c in extra["choices"]] == [0, 1]
    assert rig.head == "r0000"


def test_the_server_writes_an_answer_over_its_step_and_the_edit_then_goes_through(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    with raises_edit_error("needs_choice", 409):
        rig.edit([MOVE, CONDITIONAL], key="choice-key-0001")
    answers = [{"step": 2, "fill": {"target": "background"}}]
    outcome = rig.edit([MOVE, CONDITIONAL], key="choice-key-0002", answers=answers)
    assert outcome.status == 200
    assert outcome.body["summaries"][1].startswith("Set the background of the Rank column")
    row = rig.store().head()
    conditional = next(op for op in row.operations if op["op"] == "set_conditional_format")
    assert conditional["target"] == "background" and row.operations[0]["op"] == "set_position"
    # A client that already wrote the fill into its operations (the front end does for the
    # choices it knows) and sends it again changes nothing: the fill overwrites, it never adds.
    both = rig.edit(
        [MOVE, {**CONDITIONAL, "target": "background", "min_color": "#F0F0F0"}],
        key="choice-key-0003",
        answers=answers,
        expected_head=rig.head,
    )
    assert both.status == 200
    assert rig.store().head().operations[1]["target"] == "background"


def test_the_chat_port_carries_the_answers_to_the_service(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    port = rig.service.port
    request: dict[str, Any] = {"summary": None, "expected_head": "r0000"}
    with pytest.raises(ImportedEditError) as raised:
        port.apply(WORKSPACE, [MOVE, CONDITIONAL], idempotency_key="chat-choice-key1", **request)
    assert raised.value.code == "needs_choice"
    receipt = port.apply(
        WORKSPACE,
        [MOVE, CONDITIONAL],
        idempotency_key="chat-choice-key2",
        answers=[{"step": 2, "fill": {"target": "background"}}],
        **request,
    )
    assert receipt["revision"]["id"] == "r0001"
    conditional = next(
        op for op in rig.store().head().operations if op["op"] == "set_conditional_format"
    )
    assert conditional["target"] == "background"


def test_merged_answers_are_part_of_what_the_request_is(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    answer = [{"step": 2, "fill": {"target": "background"}}]
    request: dict[str, Any] = {"expected_head": "r0000", "idempotency_key": "choice-key-0001"}
    first = rig.service.submit_edit(WORKSPACE, [MOVE, CONDITIONAL], answers=answer, **request)
    assert rig.service.outcome(first).status == 200
    # the same key with the same answers is the same batch (and is not judged against the head)
    again = rig.service.submit_edit(WORKSPACE, [MOVE, CONDITIONAL], answers=answer, **request)
    assert again is first
    # ... and with another answer it is another request under a key that is taken
    with raises_edit_error("idempotency_conflict", 409):
        rig.service.submit_edit(
            WORKSPACE,
            [MOVE, CONDITIONAL],
            expected_head="r0000",
            idempotency_key="choice-key-0001",
            answers=[{"step": 2, "fill": {"target": "font_color"}}],
        )


@pytest.mark.parametrize(
    ("answers", "text"),
    [
        (
            [{"step": 3, "fill": {"target": "background"}}],
            "for step 3, but this change has 2 steps",
        ),
        ([{"step": 0, "fill": {"target": "background"}}], "for step 0"),
        ([{"step": True, "fill": {"target": "background"}}], "for step True"),
        ([{"step": "2", "fill": {"target": "background"}}], "for step 2"),
        ([{"step": 2, "fill": {}}], "says nothing to change"),
        ([{"step": 2, "fill": "clear"}], "says nothing to change"),
        ([{"step": 2, "fill": {"op": "set_position"}}], "cannot change what step 2 does"),
        ([{"step": 2}], "says nothing to change"),
    ],
)
def test_an_answer_that_does_not_fit_is_refused_and_nothing_is_queued(
    tmp_path: Path, answers: list[dict[str, Any]], text: str
) -> None:
    rig = make_rig(tmp_path)
    with raises_edit_error("edit_rejected", 422) as raised:
        rig.edit([MOVE, CONDITIONAL], key="choice-key-0001", answers=answers)
    assert text in raised.value.message
    assert not rig.service._by_key.get(("report-1", "choice-key-0001"))  # noqa: SLF001


def test_answers_can_be_given_as_objects_and_leave_the_request_untouched() -> None:
    from types import SimpleNamespace

    original = [dict(CONDITIONAL)]
    merged = ImportedEditingService.merge_answers(
        original, [SimpleNamespace(step=1, fill={"target": "background"})]
    )
    assert merged == [{**CONDITIONAL, "target": "background"}]
    assert original == [CONDITIONAL]  # a copy is changed
    assert ImportedEditingService.merge_answers(original, None) == original
    assert ImportedEditingService.merge_answers(original, []) == original
    # the other fills a question offers reach the operation verbatim, several answers at once
    operations = [{"op": "set_fields", "visual": "v"}, {"op": "place_visual", "visual": "w"}]
    merged = ImportedEditingService.merge_answers(
        operations,
        [
            {"step": 1, "fill": {"fields": [{"table": "T", "name": "A"}]}},
            {"step": 2, "fill": {"on": "overlay"}},
        ],
    )
    assert merged[0]["fields"] == [{"table": "T", "name": "A"}] and merged[1]["on"] == "overlay"


def test_a_confirmation_names_its_step_too() -> None:
    from ai_powerbi_analyst.studio.report_edit_context import (
        NeedsChoice as RealChoice,
    )
    from ai_powerbi_analyst.studio.report_edit_context import (
        NeedsConfirmation as RealConfirm,
    )

    confirm = RealConfirm("replace_conditional", "This replaces the rules.")
    confirm.step = 3
    assert classify(confirm).extra == {
        "kind": "replace_conditional",
        "detail": "This replaces the rules.",
        "step": 3,
    }
    ask = RealChoice("Which?", [])
    ask.step = 1
    assert classify(ask).extra["step"] == 1


def test_the_request_level_yes_sets_the_flags_the_operations_really_have() -> None:
    parse = ImportedEditingService.parse_operations
    raw = [
        {"op": "set_conditional_format", "visual": "v", "style": "gradient"},
        {"op": "set_number_format", "visual": "v", "preset": "percent"},
        {"op": "delete_page", "page": "1"},
        {"op": "set_position", "visual": "v", "x": 1},
    ]
    plain = parse(raw)
    assert [getattr(op, "replace_conditional", None) for op in plain] == [False, False, None, None]
    flagged = parse(raw, confirm_replace=True)
    assert flagged[0].replace_conditional is True
    assert flagged[1].replace_conditional is True
    # Switching off a page's buttons and links is another question: only its own field answers it.
    assert flagged[2].cleanup_references is False
    assert not hasattr(flagged[3], "replace_conditional")


# --- what the receipt says ------------------------------------------------------------------


def test_the_receipt_of_an_edit_and_of_its_replay_carry_what_the_edit_layer_reported(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    table = "e82e02f93055d6b0b544"
    ops = [
        {
            "op": "set_conditional_format",
            "visual": table,
            "style": "gradient",
            "target": "font_color",
            "field": "Rank",
            "replace_conditional": True,
        }
    ]
    first = rig.edit(ops, key="edit-key-0001")
    assert len(first.body["replaced"]) == 1 and first.body["replayed"] is False
    assert "measure" in first.body["replaced"][0] and "Rank column" in first.body["replaced"][0]
    assert first.body["protected"] == [] and first.body["pending_model_operations"] == []
    handle = rig.service.submit_edit(
        WORKSPACE, ops, expected_head="r0000", idempotency_key="edit-key-0001"
    )
    assert handle is not None and rig.service.outcome(handle).body == first.body  # in memory
    # After a restart only the journal remembers the batch: a new service (no registry) still
    # gives back the rule that was replaced.
    restarted = make_service(tmp_path, rig.state, rig.desktop, rig)
    replay = restarted.outcome(
        restarted.submit_edit(
            WORKSPACE, ops, expected_head="r0000", idempotency_key="edit-key-0001"
        )
    )
    assert replay.status == 200 and replay.body["replayed"] is True
    assert replay.body["replaced"] == first.body["replaced"]
    assert replay.body["visible_in"] == first.body["visible_in"]
    assert replay.body["protected"] == [] and replay.body["pending_model_operations"] == []
    # ... and so does a poll for the finished batch that a restart forgot
    polled = restarted.status(WORKSPACE, first.body["batch_id"])
    assert polled["state"] == "done"
    assert polled["receipt"]["replaced"] == first.body["replaced"]
    assert polled["receipt"]["visible_in"] == first.body["visible_in"]


def test_the_service_no_longer_works_the_edit_layers_answer_out_again() -> None:
    assert not hasattr(ImportedEditingService, "_replayed_outcome")


def test_a_batch_a_restart_interrupted_answers_from_the_journal_with_its_reported_items(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    rig.edit(key="edit-key-0001")
    recorded = {
        "revision": "r0001",
        "summaries": ["Done."],
        "warnings": [],
        "status": "updated",
        "protected": [{"visual": "v1", "what": "kept"}, "garbage"],
        "replaced": ["TopN blue"],
        "visible_in": [{"op": "set_conditional_format", "where": "powerbi_only", "detail": ""}],
        "pending_model_operations": [{"op": "update_measure", "name": "Rank", "expression": "1"}],
    }
    body = ImportedEditingService._receipt_from_journal(rig.store(), recorded)  # noqa: SLF001
    assert body["protected"] == [{"visual": "v1", "what": "kept"}]
    assert body["replaced"] == ["TopN blue"]
    assert body["visible_in"] == [
        {"op": "set_conditional_format", "where": "powerbi_only", "detail": ""}
    ]
    assert body["pending_model_operations"] == recorded["pending_model_operations"]
    assert body["replayed"] is True and body["outcome"] == "updated"
    # a journal line from before these fields were recorded has none of them
    old = ImportedEditingService._receipt_from_journal(  # noqa: SLF001
        rig.store(), {"revision": "r0001", "summaries": ["Done."]}
    )
    assert old["protected"] == old["replaced"] == old["visible_in"] == []
    assert old["pending_model_operations"] == []


def test_a_receipt_payload_keeps_what_the_editor_gave_and_a_callers_outcome_fills_gaps(
    tmp_path: Path,
) -> None:
    from ai_powerbi_analyst.studio.report_edit_context import Protected, VisibleIn
    from ai_powerbi_analyst.studio.report_editor import EditReceipt
    from ai_powerbi_analyst.studio.report_edits import EditOutcome, UpdateMeasure

    rig = make_rig(tmp_path)
    rig.edit(key="edit-key-0001")
    store, report = rig.store(), rig.report()
    revision = store.head()
    receipt = EditReceipt(
        revision=revision,
        report=report,
        summaries=["Done."],
        protected=[Protected("v1", "conditional font color on Agent ID, kept")],
        replaced=["TopN blue"],
        visible_in=[VisibleIn("set_conditional_format", "powerbi_only", "not drawn")],
        pending_model_operations=[UpdateMeasure(name="Rank", expression="1")],
    )
    payload = rig.service.receipt_payload(receipt, [{"op": "set_conditional_format"}], store)
    assert payload["protected"] == [
        {"visual": "v1", "what": "conditional font color on Agent ID, kept"}
    ]
    assert payload["replaced"] == ["TopN blue"]
    assert payload["visible_in"] == [
        {"op": "set_conditional_format", "where": "powerbi_only", "detail": "not drawn"}
    ]
    assert payload["pending_model_operations"] == [
        UpdateMeasure(name="Rank", expression="1").model_dump(mode="json")
    ]
    # An undo or restore reports nothing of its own; a caller may pass what it worked out.
    bare = EditReceipt(revision=revision, report=report)
    empty = rig.service.receipt_payload(bare, [{"op": "undo"}], store)
    assert empty["protected"] == empty["replaced"] == empty["pending_model_operations"] == []
    assert [(v["op"], v["where"]) for v in empty["visible_in"]] == [("undo", "studio")]
    worked_out = EditOutcome(protected=[Protected("v9", "kept")], replaced=["x"])
    filled = rig.service.receipt_payload(bare, [{"op": "undo"}], store, worked_out)
    assert filled["protected"] == [{"visual": "v9", "what": "kept"}] and filled["replaced"] == ["x"]


def test_a_dialog_the_port_or_the_editor_names_maps_without_reading_the_message() -> None:
    from ai_powerbi_analyst.studio.desktop_session import DesktopDialogOpen
    from ai_powerbi_analyst.studio.report_editor import DesktopDialog

    typed = classify(DesktopDialog("A dialog is open.", "Export data"))
    assert (typed.code, typed.status) == ("desktop_dialog", 409)
    assert typed.extra["dialog"] == "Export data"
    assert typed.message == "A dialog is open."  # no title in the words: only the attribute has it
    raw = classify(DesktopDialogOpen("Answer it in Power BI Desktop.", "Save changes?"))
    assert (raw.code, raw.status) == ("desktop_dialog", 409)
    assert raw.extra["dialog"] == "Save changes?"
    # a port error with no dialog is still "Desktop is unavailable"
    unavailable = classify(DesktopSessionError("Power BI Desktop was closed"))
    assert unavailable.code == "desktop_unavailable"


def test_a_repeated_key_with_other_consent_flags_is_still_the_same_batch(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    first = rig.service.submit_edit(
        WORKSPACE, [MOVE], expected_head="r0000", idempotency_key="edit-key-0001"
    )
    assert rig.service.outcome(first).status == 200
    for flags in (
        {"desktop_work": "save"},
        {"confirm_model": True},
        {"confirm_replace": True},
        {"desktop_work": "save", "confirm_model": True, "confirm_replace": True},
    ):
        again = rig.service.submit_edit(
            WORKSPACE, [MOVE], expected_head="r0000", idempotency_key="edit-key-0001", **flags
        )
        assert again is first
    assert len(rig.store().history()) == 2


# --- the model's facts follow the head ---------------------------------------------------------

FACTS_V1: dict[str, Any] = {
    "version": 1,
    "tables": [
        {"name": "FactSales", "source": "m", "calculated": False, "lineageTag": "t-sales"},
        {"name": "DimChannel", "source": "m", "calculated": False, "lineageTag": "t-channel"},
        {"name": MEASURE_TABLE, "source": "m", "calculated": False, "lineageTag": "t-measures"},
    ],
    "columns": [
        {"table": "FactSales", "name": "ChannelKey", "kind": "data", "dataType": 6,
         "sourceColumn": "ChannelKey", "lineageTag": "c-fkey"},
        {"table": "FactSales", "name": "Amount", "kind": "data", "dataType": 8,
         "sourceColumn": "Amount", "lineageTag": "c-amount"},
        {"table": "DimChannel", "name": "ChannelKey", "kind": "data", "dataType": 6,
         "sourceColumn": "ChannelKey", "lineageTag": "c-key"},
        {"table": "DimChannel", "name": "ChannelName", "kind": "data", "dataType": 2,
         "sourceColumn": "ChannelName", "lineageTag": "c-name"},
    ],
    "measures": [{"table": MEASURE_TABLE, "name": "Rank", "expression": "1"}],
    "relationships": [],
    "partitions": [
        {"table": name, "source": "m", "mode": "import"}
        for name in ("FactSales", "DimChannel", MEASURE_TABLE)
    ],
}  # fmt: skip
# What Desktop saved after the change: ChannelName renamed Channel (same lineage tag) and a join.
FACTS_V2: dict[str, Any] = {
    **FACTS_V1,
    "columns": [
        {**c, "name": "Channel"} if c["name"] == "ChannelName" else c for c in FACTS_V1["columns"]
    ],
    "relationships": [
        {"fromTable": "FactSales", "fromColumn": "ChannelKey", "toTable": "DimChannel",
         "toColumn": "ChannelKey", "cardinality": "M:1", "crossFilter": "single", "active": True},
    ],
    "roles": [{"name": "EU", "tables": [{"table": "FactSales", "filter": "1 = 1"}]}],
}  # fmt: skip


def facts_by_model(reads: list[str]) -> Callable[[Path], dict[str, Any]]:
    """Reads the facts of the model the fake Desktop saved: V2 once it holds the new measure."""

    def read(path: Path) -> dict[str, Any]:
        reads.append(path.name)
        changed = "Avg Discount" in {m["Name"] for m in read_measures(path)}
        return copy.deepcopy(FACTS_V2 if changed else FACTS_V1)

    return read


def restore_view(rig: Rig, **update: Any) -> ImportedReport:
    current = rig.report()
    return rig.state.put(
        "imported_report",
        current.model_copy(update=update),
        workspace_id=WORKSPACE,
        expected_base_hash=current.base_hash,
        idempotency_key=f"facts-view-{len(update)}-{sorted(update)[0]}",
        mutation_payload={"facts": sorted(update)},
    )


def channel_snapshot(tmp_path: Path) -> Any:
    from types import SimpleNamespace

    import pandas as pd

    frames = {
        "FactSales": pd.DataFrame({"ChannelKey": [1, 1, 2], "Amount": [10.0, 20.0, 5.0]}),
        "DimChannel": pd.DataFrame({"ChannelKey": [1, 2], "ChannelName": ["Store", "Online"]}),
    }
    items = []
    for index, (name, frame) in enumerate(frames.items()):
        path = tmp_path / f"rows-{index}.parquet"
        frame.to_parquet(path, index=False)
        table = SimpleNamespace(name=name, row_count=len(frame), columns=[])
        item = SimpleNamespace(id=f"t{index}", table=table, normalized_parquet_path=str(path))
        items.append(item)
    return SimpleNamespace(id="snap-1", tables=items)


def test_a_model_change_moves_tables_relationships_and_facts_to_the_head(tmp_path: Path) -> None:
    from ai_powerbi_analyst.studio import model_facts, report_engine
    from ai_powerbi_analyst.studio.dax_engine import run_query

    rig = make_rig(tmp_path)
    original_key = imported_editing.report_import.model_key(rig.report())
    restore_view(
        rig,
        tables=model_facts.schema_rows(FACTS_V1),
        relationships=[],
        relationships_version=2,
        snapshot_id="snap-1",
        model=model_facts.stored(FACTS_V1),
        snapshot_model=model_facts.digest(FACTS_V1, original_key),
    )
    reads: list[str] = []
    rig.service._read_facts = facts_by_model(reads)  # noqa: SLF001
    snapshot = channel_snapshot(tmp_path)
    query = 'EVALUATE SUMMARIZECOLUMNS(DimChannel[{}], "S", SUM(FactSales[Amount]))'
    before = report_engine.report_model(rig.report(), snapshot)
    # no join yet: every channel sees every sale
    unjoined = run_query(before, query.format("ChannelName")).rows
    assert sorted(unjoined) == [["Online", 35.0], ["Store", 35.0]]
    rig.edit([CREATE], key="model-key-0001", confirm_model=True)
    after = rig.report()
    assert len(reads) == 1  # the new head's model, read once
    assert after.tables == model_facts.schema_rows(FACTS_V2)
    names = {(t["TableName"], t["ColumnName"]) for t in after.tables}
    assert ("DimChannel", "Channel") in names and ("DimChannel", "ChannelName") not in names
    assert after.relationships == model_facts.relationship_rows(FACTS_V2)
    assert after.relationships_version == 2
    assert after.model == model_facts.stored(FACTS_V2)
    # the rows still come from the import: their digest is kept
    assert after.snapshot_model == model_facts.digest(FACTS_V1, original_key)
    # The engine follows the head: the new join, the new name, the old name gone.
    engine = report_engine.report_model(after, snapshot)
    assert sorted(run_query(engine, query.format("Channel")).rows) == [
        ["Online", 5.0],
        ["Store", 30.0],
    ]
    assert not engine.has_column("DimChannel", "ChannelName")
    payload = rig.service.report_payload(WORKSPACE)
    assert payload["model"]["status"] == "current" and "snapshot_model" not in payload
    assert payload["model"]["studio"]["rowLevelSecurity"] == model_facts.RLS_NOTE
    # Undo returns to the imported model: its facts are read once and then kept by model.
    rig.service.outcome(
        rig.service.submit_undo(WORKSPACE, expected_head="r0001", idempotency_key="undo-key-0001")
    )
    assert rig.report().model == model_facts.stored(FACTS_V1)
    assert rig.report().tables == model_facts.schema_rows(FACTS_V1)
    assert len(reads) == 2 and reads[1] == "original.pbix"
    # the executor of model changes reads a revision's facts through the same cache
    changed_file = rig.store().path(rig.store().revision("r0001"))
    assert rig.service.read_model_facts(rig.report(), changed_file) == FACTS_V2
    assert len(reads) == 2
    kept = tmp_path / "state" / "reports" / "report-1" / "model-facts"
    assert sorted(p.name for p in kept.glob("*.json")) == sorted(
        f"{k}.json"
        for k in {
            original_key,
            imported_editing.report_import.model_key(
                rig.store().path(rig.store().revision("r0001"))
            ),
        }
    )
    # A restarted Studio reads them from beside the history, not from the model.
    restarted = make_service(tmp_path, rig.state, rig.desktop, rig)
    restarted._read_facts = facts_by_model(reads)  # noqa: SLF001
    assert restarted.head_model_facts(rig.report()) == FACTS_V1
    assert len(reads) == 2


def test_a_model_whose_facts_cannot_be_read_is_unknown_not_stale(tmp_path: Path) -> None:
    from ai_powerbi_analyst.studio import model_facts

    rig = make_rig(tmp_path)
    restore_view(
        rig,
        tables=model_facts.schema_rows(FACTS_V1),
        model=model_facts.stored(FACTS_V1),
    )

    def failing(path: Path) -> dict[str, Any]:
        raise EditRejected("Studio could not read the report's model.")

    rig.service._read_facts = failing  # noqa: SLF001
    outcome = rig.edit([CREATE], key="model-key-0001", confirm_model=True)
    assert outcome.body["outcome"] == "updated"  # the facts never cost the change
    after = rig.report()
    assert after.model is None  # not the old model's facts
    assert after.tables == model_facts.schema_rows(FACTS_V1)  # the last rows known
    payload = rig.service.report_payload(WORKSPACE)["model"]
    assert payload["status"] == "unknown" and payload["storage"]["kind"] == "unknown"
    assert after.model_read_version == 1
    assert rig.service.head_model_facts(after) is None
    # The joins are the old model's: they are read again from the head (and fail here, the
    # fake model is no Power BI file), so the engine refuses instead of filtering with them.
    assert after.relationships_version == 0


def test_a_failed_read_after_desktop_added_a_join_never_answers_with_the_old_joins(
    tmp_path: Path,
) -> None:
    """Desktop saved a model with a new FactSales -> DimChannel join and the read of its facts
    failed: the engine must refuse, not answer Online 35 / Store 35 (Desktop: 5 / 30)."""
    from ai_powerbi_analyst.studio import model_facts, report_engine
    from ai_powerbi_analyst.studio.report_engine import DaxUnavailable

    rig = make_rig(tmp_path)
    key = imported_editing.report_import.model_key(rig.report())
    restore_view(
        rig,
        tables=model_facts.schema_rows(FACTS_V1),
        relationships=[],
        relationships_version=2,
        snapshot_id="snap-1",
        model=model_facts.stored(FACTS_V1),
        snapshot_model=model_facts.digest(FACTS_V1, key),
    )

    def read(path: Path) -> dict[str, Any]:
        if "Avg Discount" in {m["Name"] for m in read_measures(path)}:
            raise EditRejected("Studio could not read the report's model.")
        return copy.deepcopy(FACTS_V1)

    rig.service._read_facts = read  # noqa: SLF001
    rig.edit([CREATE], key="model-key-0001", confirm_model=True)
    after = rig.report()
    assert after.model is None and after.snapshot_model is not None
    with pytest.raises(DaxUnavailable, match="current model's metadata"):
        report_engine.report_model(after, channel_snapshot(tmp_path))


def test_a_failed_legacy_background_read_is_persisted_and_cannot_return_saved_values(
    tmp_path: Path,
) -> None:
    from ai_powerbi_analyst.studio import report_engine

    rig = make_rig(tmp_path)
    before = restore_view(rig, snapshot_id="snap-1", relationships_version=2)
    assert before.model is None and before.model_read_version == 0

    def failing(path: Path) -> dict[str, Any]:
        raise EditRejected("Studio could not read the report's model.")

    rig.service._read_facts = failing  # noqa: SLF001
    _, digest = report_archive(before)
    rig.service._fill(WORKSPACE, before.id, digest)  # noqa: SLF001
    restarted = make_service(tmp_path, rig.state, rig.desktop, rig)
    after = restarted.report(WORKSPACE)
    assert after.model is None and after.model_read_version == 1
    with pytest.raises(report_engine.DaxUnavailable, match="current model's metadata"):
        report_engine.report_model(after, channel_snapshot(tmp_path))


def test_a_failed_background_read_does_not_mark_a_head_that_moved(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    before = rig.report()

    def move_then_fail(path: Path) -> dict[str, Any]:
        rig.edit([MOVE], key="move-during-facts", confirm_model=False)
        raise EditRejected("Studio could not read the report's model.")

    rig.service._read_facts = move_then_fail  # noqa: SLF001
    _, digest = report_archive(before)
    rig.service._fill(WORKSPACE, before.id, digest)  # noqa: SLF001
    after = rig.report()
    assert after.base_hash != before.base_hash and after.model_read_version == 0


def test_a_failed_background_read_does_not_erase_facts_filled_by_another_worker(
    tmp_path: Path,
) -> None:
    from ai_powerbi_analyst.studio import model_facts

    rig = make_rig(tmp_path)
    before = rig.report()

    def fill_then_fail(path: Path) -> dict[str, Any]:
        restore_view(rig, model=model_facts.stored(FACTS_V1), model_read_version=1)
        raise EditRejected("Studio could not read the report's model.")

    rig.service._read_facts = fill_then_fail  # noqa: SLF001
    _, digest = report_archive(before)
    rig.service._fill(WORKSPACE, before.id, digest)  # noqa: SLF001
    assert rig.report().model == model_facts.stored(FACTS_V1)


def test_the_rows_own_model_is_read_again_until_it_is_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older import whose original model could not be read when the head's facts were: the
    payload tries again (after the backoff), and the digest fills once the read works."""
    from ai_powerbi_analyst.studio import model_facts

    rig = make_rig(tmp_path)
    monkeypatch.setattr(imported_editing, "readable_model", lambda path: True)
    restore_view(rig, snapshot_id="snap-1", model=model_facts.stored(FACTS_V1))
    tries: list[str] = []

    def flaky(path: Path) -> dict[str, Any]:
        tries.append(path.name)
        if len(tries) == 1:
            raise EditRejected("Studio could not read the report's model.")
        return copy.deepcopy(FACTS_V1)

    rig.service._read_facts = flaky  # noqa: SLF001
    notes = rig.service.report_payload(WORKSPACE)["model"]["studio"]
    # meanwhile every calculated object is distrusted and reading the data again is offered
    assert notes["actions"] == ["reread_data"]
    wait_until(lambda: tries == ["original.pbix"] and not rig.service._filling)  # noqa: SLF001
    assert rig.report().snapshot_model is None
    rig.service.report_payload(WORKSPACE)  # within the backoff: not tried again
    assert tries == ["original.pbix"]
    monkeypatch.setattr(imported_editing, "FACTS_BACKOFF", 0.0)
    rig.service.report_payload(WORKSPACE)
    wait_until(lambda: rig.report().snapshot_model is not None)
    key = imported_editing.report_import.model_key(rig.report())
    assert rig.report().snapshot_model == model_facts.digest(FACTS_V1, key)
    assert rig.service.report_payload(WORKSPACE)["model"]["studio"]["actions"] == []


def test_the_payload_says_what_studio_knows_of_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_powerbi_analyst.studio import model_facts

    thin = make_rig(tmp_path / "thin", thin=True)
    thin_model = thin.service.report_payload(WORKSPACE)["model"]
    assert thin_model["status"] == "none" and thin_model["storage"]["kind"] == "no_local_model"
    rig = make_rig(tmp_path / "rig")
    reads: list[str] = []
    rig.service._read_facts = facts_by_model(reads)  # noqa: SLF001
    # The twin's model is not one Power BI wrote: nothing is started for it.
    unknown = rig.service.report_payload(WORKSPACE)["model"]
    assert unknown["status"] == "unknown" and unknown["storage"]["kind"] == "unknown"
    assert reads == []
    # A report imported before facts were read has them read in the background, once.
    monkeypatch.setattr(imported_editing, "readable_model", lambda path: True)
    restore_view(rig, snapshot_id="snap-1")
    reading = rig.service.report_payload(WORKSPACE)["model"]
    assert reading["status"] == "reading" and reading["storage"]["kind"] == "unknown"
    wait_until(lambda: rig.report().model is not None)
    report = rig.report()
    assert report.model == model_facts.stored(FACTS_V1)
    assert report.tables == model_facts.schema_rows(FACTS_V1)
    key = imported_editing.report_import.model_key(report)
    # the rows came from the same (imported) model: its digest is filled from the same read
    assert report.snapshot_model == model_facts.digest(FACTS_V1, key)
    assert reads == ["original.pbix"]
    payload = rig.service.report_payload(WORKSPACE)["model"]
    assert payload["status"] == "current"
    assert payload["tables"] == report.model["tables"]
    assert payload["studio"]["data"]["stale"] is False
    wait_until(lambda: not rig.service._filling)  # noqa: SLF001
    assert reads == ["original.pbix"]


def test_the_bounded_facts_reader_runs_only_on_a_model_power_bi_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from editing_support import make_zip

    marker = tmp_path / "ran.txt"
    script = _script(
        tmp_path,
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text('x')\n"
        "print(json.dumps({'version': 1, 'flag': sys.argv[2]}))\n",
    )
    monkeypatch.setattr(imported_editing, "_EXTRACT_SCRIPT", script)
    xpress = "This backup was created using XPress9 compression.".encode("utf-16-le")
    stream = imported_editing._STREAM_STORAGE  # noqa: SLF001
    written = tmp_path / "written.pbix"
    make_zip(written, {"DataModel": xpress + b"\x00" * 40})
    make_zip(tmp_path / "stream.pbix", {"DataModel": stream + b"\x00" * 40})
    make_zip(tmp_path / "fake.pbix", {"DataModel": b'{"measures": []}'})
    make_zip(tmp_path / "thin.pbix", {"Report/definition/report.json": b"{}"})
    assert imported_editing.readable_model(written)
    assert imported_editing.readable_model(tmp_path / "stream.pbix")
    assert not imported_editing.readable_model(tmp_path / "fake.pbix")
    assert not imported_editing.readable_model(tmp_path / "thin.pbix")
    assert not imported_editing.readable_model(tmp_path / "missing.pbix")
    with pytest.raises(EditRejected, match="could not read the report's model"):
        imported_editing.read_model_facts_bounded(tmp_path / "fake.pbix")
    assert not marker.exists()  # refused without starting a child process
    assert imported_editing.read_model_facts_bounded(written) == {"version": 1, "flag": "--facts"}
    assert marker.exists()
    monkeypatch.setattr(imported_editing, "_EXTRACT_SCRIPT", _script(tmp_path, "print('[1]')\n"))
    with pytest.raises(EditRejected, match="could not read the report's model"):
        imported_editing.read_model_facts_bounded(written)


# --- what the model workbench asks before it edits ------------------------------------------------


def test_model_support_names_each_gated_operation_and_what_the_installed_desktop_passed(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    rig.desktop.probe_verified = frozenset({"hierarchies", "delete_data_table", "not_a_kind"})
    support = rig.service.report_payload(WORKSPACE)["capabilities"]["model_support"]
    tags = report_edit_model.MICROSOFT_SUPPORT
    assert support["desktop_version"] == "2.157.1354.0"
    assert set(support["documented"]) | set(support["outside"]) | set(support["depends"]) == set(
        tags
    )
    assert support["depends"] == ["delete_column", "set_table_properties", "delete_table"]
    assert "rename_column" in support["outside"] and "create_relationship" in support["documented"]
    assert support["verified_keys"] == ["delete_data_table", "hierarchies"]  # unknown keys dropped
    assert support["verified"] == [
        "delete_table",
        "create_hierarchy",
        "update_hierarchy",
        "delete_hierarchy",
    ]
    assert support["keys"]["delete_table"] == "delete_data_table"
    assert set(support["keys"]) == set(support["outside"]) | set(support["depends"])
    # nothing passed: every gated operation is refused before Desktop opens
    rig.desktop.probe_verified = frozenset()
    none = rig.service.model_support()
    assert none["verified"] == [] and none["verified_keys"] == []
    # no Desktop at all
    assert imported_editing.model_support_payload(None, ())["desktop_version"] is None


def test_the_payload_keys_agree_with_the_gate_the_editor_applies() -> None:
    """``support_keys`` (what the UI is told) and ``outside_support`` (what the editor refuses)
    name the same probe key for every gated operation on a table loaded from the data source."""
    model = report_edit_model.example_model()
    data_table = next(
        name
        for name, entry in report_edit_model.model_structure(model).tables.items()
        if entry.get("kind") == "data" and model.columns.get(entry["name"])
    )
    table = report_edit_model.model_structure(model).tables[data_table]["name"]
    column = next(iter(model.columns[table]))
    steps = {
        "delete_column": {"op": "delete_column", "table": table, "name": column},
        "delete_table": {"op": "delete_table", "name": table},
        "set_table_properties": {"op": "set_table_properties", "table": table, "hidden": True},
    }
    keys = report_edit_model.support_keys()
    for op, step in steps.items():
        parsed = report_edit_model.parse_model_operation(step)
        assert report_edit_model.outside_support([parsed], model) == [keys[op]], op
    tagged = {op for op, tag in report_edit_model.MICROSOFT_SUPPORT.items() if tag != "documented"}
    assert set(keys) == tagged
    assert set(keys.values()) == set(report_edit_model.OUTSIDE_SUPPORT)


QUERY_FACTS: dict[str, Any] = {
    "tables": [
        {"name": "DimChannel"},
        {"name": "Years", "calculated": True, "expression": "DISTINCT('Date'[Year])"},
        {"name": "Mixed"},
    ],
    "partitions": [
        {"table": "DimChannel", "name": "DimChannel", "source": "m", "expression": "let in 1"},
        {"table": "Mixed", "name": "a", "source": "m", "expression": "let in 1"},
        {"table": "Mixed", "name": "b", "source": "m", "expression": "let in 2"},
    ],
}


def test_a_power_query_formula_is_checked_like_an_edit_without_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = make_rig(tmp_path)
    tables = [
        *copy.deepcopy(TABLES),
        {"TableName": "Years", "ColumnName": "Year", "PandasDataType": "int64"},
        {"TableName": "Mixed", "ColumnName": "Value", "PandasDataType": "int64"},
    ]
    restore_view(rig, tables=tables, model=copy.deepcopy(QUERY_FACTS))
    seen: list[str] = []

    def parser(text: str) -> list[str] | None:
        seen.append(text)
        if "BROKEN" in text:
            return ["Expected 'in'", "Unexpected token ','", "Missing ')'", "Fourth"]
        if "UNCHECKED" in text:
            return None
        return []

    monkeypatch.setattr(report_edit_model, "M_CHECKER", parser)
    check = imported_editing.ModelQueryCheckRequest
    heads = rig.head, len(rig.desktop.events)
    good = 'let Source = Csv.Document(File.Contents("F:/private/a.csv")) in Source'
    assert rig.service.validate_model_query(WORKSPACE, check(expression=good)) == {
        "valid": True,
        "checked": True,
        "message": None,
        "problems": [],
        "warnings": [],
        "table": None,
    }
    broken = rig.service.validate_model_query(
        WORKSPACE, check(expression="let x = BROKEN", table="dimchannel")
    )
    assert broken == {
        "valid": False,
        "checked": True,
        "message": "The Power Query formula is not valid: Expected 'in'; Unexpected token ','; "
        "Missing ')'",
        "problems": ["Expected 'in'", "Unexpected token ','", "Missing ')'", "Fourth"],
        "warnings": [],
        "table": "DimChannel",
    }
    unchecked = rig.service.validate_model_query(
        WORKSPACE, check(expression="UNCHECKED", table="DimChannel")
    )
    assert (unchecked["valid"], unchecked["checked"]) == (True, False)
    assert unchecked["warnings"] == [report_edit_model.M_UNCHECKED]
    # the table's own checks come first, in the family's words, and the parser is not run
    before = len(seen)
    for table, message in (
        ("Years", "'Years' is calculated with DAX, so it has no Power Query query"),
        (
            "Mixed",
            "'Mixed' does not load from a single Power Query query, so Studio cannot change it",
        ),
        ("Nope", "The model has no table 'Nope'"),
    ):
        refused = rig.service.validate_model_query(WORKSPACE, check(expression=good, table=table))
        assert (refused["valid"], refused["message"], refused["table"]) == (False, message, None)
    assert len(seen) == before
    control = rig.service.validate_model_query(WORKSPACE, check(expression="let\x07 in 1"))
    assert control["message"] == "The query cannot contain control characters"
    # nothing was queued, Desktop was not touched, and the formula was never logged
    assert (rig.head, len(rig.desktop.events)) == heads
    assert rig.service.editor.store(rig.report()).head().id == "r0000"
    assert not any("private/a.csv" in record.getMessage() for record in caplog.records)


def test_a_thin_report_has_no_query_to_check(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, thin=True)
    with pytest.raises(ImportedEditError) as refused:
        rig.service.validate_model_query(
            WORKSPACE, imported_editing.ModelQueryCheckRequest(expression="1")
        )
    assert (refused.value.code, refused.value.status) == ("no_model", 404)


def test_each_planned_step_says_whether_microsoft_supports_it(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.desktop.probe_verified = frozenset({"rename_column"})
    created = {
        "op": "create_calculated_column",
        "table": "DimChannel",
        "name": "Short",
        "expression": "LEFT(DimChannel[ChannelName], 3)",
    }
    steps = [
        {"op": "rename_column", "table": "DimChannel", "name": "ChannelName", "new_name": "X"},
        created,
        {"op": "delete_column", "table": "DimChannel", "name": "Short"},  # created above
        {"op": "delete_column", "table": "DimChannel", "name": "ChannelName"},  # from the source
        {"op": "set_alt_text", "visual": "v", "text": "Sales"},
        {"op": "not_an_operation"},
    ]
    assert rig.service.port.model_step_support(WORKSPACE, steps) == [  # type: ignore[attr-defined]
        {"outside_supported": True, "verified_on": "2.157.1354.0"},
        {"outside_supported": False},
        {"outside_supported": False},
        {"outside_supported": True, "verified_on": None},
        {"outside_supported": False},
        {"outside_supported": False},
    ]


def test_the_payload_says_each_visuals_alt_text_and_each_pages_type(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)

    def visual(payload: dict[str, Any]) -> dict[str, Any]:
        return next(v for p in payload["pages"] for v in p["visuals"] if v["id"] == VISUAL)

    before = rig.service.report_payload(WORKSPACE)
    assert visual(before)["alt_text"] is None
    assert {p["page_type"] for p in before["pages"]} == {"default"}
    steps = [
        {"op": "set_alt_text", "visual": VISUAL, "text": "Sales by channel"},
        {"op": "add_page", "name": "Agent tooltip"},
        {"op": "set_tooltip_page", "page": "Agent tooltip"},
    ]
    assert rig.edit(steps, key="edit-key-0001").status == 200
    after = rig.service.report_payload(WORKSPACE)
    assert visual(after)["alt_text"] == "Sales by channel"
    types = {p["title"]: p["page_type"] for p in after["pages"]}
    assert types["Agent tooltip"] == "tooltip"
    assert (
        rig.edit(
            [{"op": "set_alt_text", "visual": VISUAL, "text": None}], key="edit-key-0002"
        ).status
        == 200
    )
    assert visual(rig.service.report_payload(WORKSPACE))["alt_text"] is None
