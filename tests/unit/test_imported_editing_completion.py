"""Completion includes bookkeeping; fake native edits need no Desktop or model service."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from editing_support import patch_diff
from test_imported_editing_service import MOVE, WORKSPACE, Died, Rig, make_rig

from ai_powerbi_analyst.studio import report_revisions
from ai_powerbi_analyst.studio.imported_editing import BatchHandle, ImportedEditError


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    patch_diff(monkeypatch)
    monkeypatch.setattr(report_revisions, "RETRY_DELAY", 0.01)
    found = make_rig(tmp_path)
    try:
        yield found
    finally:
        found.service.close()


def _hold_bookkeeping(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, stage: str, *, kind: str | None = None
) -> tuple[threading.Event, threading.Event]:
    entered, release = threading.Event(), threading.Event()
    original = getattr(rig.service, stage)

    def held(handle: BatchHandle) -> bool:
        state, _result, _error = handle._completion()  # noqa: SLF001
        if state in {"done", "failed"} and (kind is None or handle.kind == kind):
            entered.set()
            assert release.wait(10), "The test must release completion bookkeeping"
        return original(handle)

    monkeypatch.setattr(rig.service, stage, held)
    return entered, release


def _assert_pending(rig: Rig, handle: BatchHandle, state: str = "running") -> None:
    assert handle.state == state
    assert handle.result is None and handle.error is None and handle.finished is None
    started = time.monotonic()
    assert not handle.wait(0.01)
    assert time.monotonic() - started < 0.5
    assert rig.service.outcome(handle, wait=0).status == 202
    status = rig.service.status(WORKSPACE, handle.id)
    assert status["state"] == state
    assert {"receipt", "error", "completion_warning"}.isdisjoint(status)
    stream = handle.events(heartbeat=0.01)
    if state == "running":
        assert next(stream) == {"stage": "planning", "label": "Getting ready"}
    else:
        assert next(stream) is None
    # Progress may contain native steps, but the stream cannot end on a staged outcome.
    assert any(item is None for item in stream)


def _capture_expected_worker_death(
    monkeypatch: pytest.MonkeyPatch, workers: list[threading.Thread]
) -> tuple[list[threading.ExceptHookArgs], threading.Event]:
    captured: list[threading.ExceptHookArgs] = []
    reported = threading.Event()
    original = threading.excepthook

    def capture(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is Died and args.thread in workers:
            captured.append(args)
            reported.set()
        else:
            original(args)

    monkeypatch.setattr(threading, "excepthook", capture)
    return captured, reported


@pytest.mark.parametrize("stage", ["_record", "_mirror"])
def test_a_committed_edit_stays_pending_until_its_completion_records_are_finished(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    entered, release = _hold_bookkeeping(rig, monkeypatch, stage)
    callback_done = threading.Event()
    callbacks: list[str] = []

    def listener(handle: BatchHandle) -> None:
        assert handle.wait(0) and handle.state == "done"
        assert rig.service.status(WORKSPACE, handle.id)["receipt"]["head"]["id"] == "r0001"
        applied = [e for e in rig.state.events(WORKSPACE) if e["type"] == "report.edit_applied"]
        assert applied[-1]["payload"]["revision"] == "r0001"
        callbacks.append("finished")
        callback_done.set()

    try:
        handle = rig.service.submit_edit(
            WORKSPACE, [MOVE], expected_head="r0000", idempotency_key="completion-edit-001"
        )
        assert entered.wait(5)
        handle.on_complete(listener)  # Registered during bookkeeping, never stranded.
        _assert_pending(rig, handle)
        assert rig.head == "r0001"  # The real fake-native commit is already durable.
        rig.service.close(timeout=0.01)
        assert not handle.wait(0)
        release.set()
        assert handle.wait(5) and callback_done.wait(5)
        assert callbacks == ["finished"]
        handle.on_complete(listener)
        assert callbacks == ["finished", "finished"]
        result = rig.service.outcome(handle, wait=0)
        assert result.status == 200 and result.body["revision"]["id"] == "r0001"
        assert "completion_warning" not in handle.status()
        assert rig.service._mirror_record("report-1", handle.id)["state"] == "done"  # noqa: SLF001
    finally:
        release.set()


@pytest.mark.parametrize("fatal", [False, True])
def test_a_failed_batch_publishes_its_error_only_after_the_failure_event_is_recorded(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, fatal: bool
) -> None:
    entered, release = _hold_bookkeeping(rig, monkeypatch, "_record")
    workers: list[threading.Thread] = []
    captured, reported = _capture_expected_worker_death(monkeypatch, workers)

    def fail(_progress: Callable[[str], None]) -> dict[str, Any]:
        workers.append(threading.current_thread())
        if fatal:
            raise Died
        raise ImportedEditError("edit_rejected", "The requested field is missing.", 422)

    try:
        handle = rig.service.submit(rig.report(), "custom", "completion-fail-001", "fail", fail)
        assert entered.wait(5)
        _assert_pending(rig, handle)
        assert rig.head == "r0000"
        release.set()
        assert handle.wait(5) and handle.state == "failed"
        assert handle.error is not None
        assert handle.error.code == ("edit_failed" if fatal else "edit_rejected")
        failed = [e for e in rig.state.events(WORKSPACE) if e["type"] == "report.edit_failed"]
        assert failed[-1]["payload"]["batch_id"] == handle.id
        assert failed[-1]["payload"]["code"] == handle.error.code
        with pytest.raises(ImportedEditError) as raised:
            rig.service.outcome(handle, wait=0)
        assert raised.value is handle.error
        assert rig.head == "r0000"
    finally:
        release.set()
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive(), "The worker must finish before test teardown"
    assert reported.is_set() is fatal
    assert len(captured) == int(fatal)
    if fatal:
        assert captured[0].thread is workers[0]
        assert isinstance(captured[0].exc_value, Died)


def test_queued_cancellation_waits_for_its_record_and_never_runs_the_job(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_entered, first_release, cancel_done = (threading.Event() for _ in range(3))
    entered, release = _hold_bookkeeping(rig, monkeypatch, "_record", kind="cancel-me")
    ran: list[str] = []
    cancelled: list[dict[str, Any]] = []

    def first(_progress: Callable[[str], None]) -> dict[str, Any]:
        first_entered.set()
        assert first_release.wait(10)
        return {"first": True}

    def forbidden(_progress: Callable[[str], None]) -> dict[str, Any]:
        ran.append("cancelled job ran")
        return {}

    rig.service.submit(rig.report(), "custom", "completion-first-001", "first", first)
    assert first_entered.wait(5)
    handle = rig.service.submit(rig.report(), "cancel-me", "completion-cancel-001", "c", forbidden)

    def cancel() -> None:
        cancelled.append(rig.service.cancel(WORKSPACE, handle.id))
        cancel_done.set()

    thread = threading.Thread(target=cancel)
    thread.start()
    try:
        assert entered.wait(5)
        _assert_pending(rig, handle, "queued")
        assert not cancel_done.is_set() and not ran
        release.set()
        assert cancel_done.wait(5) and handle.wait(0)
        assert cancelled[0]["cancelled"] is True
        assert cancelled[0]["error"]["code"] == "cancelled"
        failed = [e for e in rig.state.events(WORKSPACE) if e["type"] == "report.edit_failed"]
        assert failed[-1]["payload"]["batch_id"] == handle.id
        assert not ran and rig.head == "r0000"
    finally:
        release.set()
        first_release.set()
        thread.join(5)
    assert not thread.is_alive()


@pytest.mark.parametrize("stage", ["_record", "_mirror"])
@pytest.mark.parametrize("raises", [False, True])
def test_bookkeeping_failure_keeps_the_committed_receipt_and_reports_a_warning(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, stage: str, raises: bool
) -> None:
    original = getattr(rig.service, stage)

    def broken(handle: BatchHandle) -> bool:
        state, _result, _error = handle._completion()  # noqa: SLF001
        if state != "done":
            return original(handle)
        if raises:
            raise OSError("private bookkeeping failure")
        return False

    monkeypatch.setattr(rig.service, stage, broken)
    handle = rig.service.submit_edit(
        WORKSPACE, [MOVE], expected_head="r0000", idempotency_key="completion-warning-001"
    )
    assert handle.wait(5)
    result = rig.service.outcome(handle, wait=0)
    assert handle.state == "done" and handle.error is None and rig.head == "r0001"
    assert result.status == 200 and result.body["revision"]["id"] == "r0001"
    warning = handle.status()["completion_warning"]
    assert warning == (
        "Studio could not save all batch completion details. Check the report history."
    )
    assert warning in result.body["warnings"]
    assert "private bookkeeping failure" not in str(handle.status())
    if stage == "_record":
        assert not [e for e in rig.state.events(WORKSPACE) if e["type"] == "report.edit_applied"]
        mirror = rig.service._mirror_record("report-1", handle.id)  # noqa: SLF001
        assert mirror["state"] == "done" and mirror["completion_warning"] == warning
    else:
        applied = [e for e in rig.state.events(WORKSPACE) if e["type"] == "report.edit_applied"]
        assert applied[-1]["payload"]["revision"] == "r0001"


def test_an_event_store_failure_keeps_the_edit_and_persists_the_warning_in_the_mirror(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = rig.state.event

    def broken(workspace: str, event_type: str, *args: Any, **kwargs: Any) -> Any:
        if event_type == "report.edit_applied":
            raise OSError("private event-store failure")
        return original(workspace, event_type, *args, **kwargs)

    monkeypatch.setattr(rig.state, "event", broken)
    result = rig.edit(key="completion-real-log-001")
    assert result.status == 200 and rig.head == "r0001"
    warning = "Studio could not save all batch completion details. Check the report history."
    assert warning in result.body["warnings"]
    status = rig.service.status(WORKSPACE, result.body["batch_id"])
    assert status["state"] == "done" and status["completion_warning"] == warning
    mirror = rig.service._mirror_record("report-1", result.body["batch_id"])  # noqa: SLF001
    assert mirror["state"] == "done" and mirror["completion_warning"] == warning
    assert "private event-store failure" not in str(status)


def test_mirror_serialization_failure_cannot_skip_the_event_or_leave_the_batch_pending(
    rig: Rig,
) -> None:
    opaque = object()
    handle = rig.service.submit(
        rig.report(), "custom", "completion-serialize-001", "serialize", lambda _p: {"x": opaque}
    )
    assert handle.wait(5) and handle.state == "done" and handle.error is None
    assert handle.result == {"x": opaque}
    assert "completion_warning" in handle.status()
    applied = [e for e in rig.state.events(WORKSPACE) if e["type"] == "report.edit_applied"]
    assert applied[-1]["payload"]["batch_id"] == handle.id


@pytest.mark.parametrize("stage", ["_record", "_mirror"])
def test_bookkeeping_interruption_publishes_the_commit_before_the_lane_restarts(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    original = getattr(rig.service, stage)
    workers: list[threading.Thread] = []
    captured, reported = _capture_expected_worker_death(monkeypatch, workers)

    def interrupted(handle: BatchHandle) -> bool:
        state, _result, _error = handle._completion()  # noqa: SLF001
        if state == "done" and handle.kind == "edit":
            workers.append(threading.current_thread())
            raise Died
        return original(handle)

    monkeypatch.setattr(rig.service, stage, interrupted)
    handle = rig.service.submit_edit(
        WORKSPACE, [MOVE], expected_head="r0000", idempotency_key="completion-interrupt-001"
    )
    try:
        assert handle.wait(5)
        assert reported.wait(5), "The expected worker exception must be reported here"
    finally:
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive(), "The worker must finish before test teardown"
    assert len(captured) == 1 and captured[0].thread is workers[0]
    assert isinstance(captured[0].exc_value, Died)
    assert handle.state == "done" and handle.error is None and rig.head == "r0001"
    result = rig.service.outcome(handle, wait=0)
    assert result.status == 200 and result.body["head"]["id"] == "r0001"
    assert handle.status()["completion_warning"] in result.body["warnings"]
    later = rig.service.submit(
        rig.report(), "custom", "completion-interrupt-002", "later", lambda _p: {"later": True}
    )
    assert later.wait(5) and later.result == {"later": True}
    if stage == "_mirror":
        applied = [e for e in rig.state.events(WORKSPACE) if e["type"] == "report.edit_applied"]
        assert any(e["payload"]["batch_id"] == handle.id for e in applied)


@pytest.mark.parametrize("mirror_failure", ["returns_false", "raises"])
@pytest.mark.parametrize("job_fails", [False, True], ids=["success", "failure"])
def test_unsaved_terminal_outcomes_survive_burst_pruning_and_later_submissions(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    mirror_failure: str,
    job_fails: bool,
) -> None:
    from ai_powerbi_analyst.studio import imported_editing

    monkeypatch.setattr(imported_editing, "MAX_BATCHES_IN_MEMORY", 3)
    releases = [threading.Event() for _ in range(7)]
    original_mirror = rig.service._mirror  # noqa: SLF001
    callback_done = threading.Event()
    callback_statuses: list[dict[str, Any]] = []

    def mirror(handle: BatchHandle) -> bool:
        state, _result, _error = handle._completion()  # noqa: SLF001
        if handle.key == "unsaved-burst-00000" and state in {"done", "failed"}:
            if mirror_failure == "raises":
                raise OSError("Test terminal mirror failure")
            return False
        return original_mirror(handle)

    monkeypatch.setattr(rig.service, "_mirror", mirror)

    def job(index: int) -> Callable[[Callable[[str], None]], dict[str, Any]]:
        def held(_progress: Callable[[str], None]) -> dict[str, Any]:
            assert releases[index].wait(10), "The test must release every queued job"
            if index == 0 and job_fails:
                raise ImportedEditError(
                    "needs_confirmation", "Test job did not apply an edit.", 409
                )
            return {"index": index}

        return held

    def listener(handle: BatchHandle) -> None:
        assert handle.wait(0)
        callback_statuses.append(rig.service.status(WORKSPACE, handle.id))
        callback_done.set()

    handles = [
        rig.service.submit(
            rig.report(), "custom", f"unsaved-burst-{index:05d}", str(index), job(index)
        )
        for index in range(6)
    ]
    first = handles[0]
    first.on_complete(listener)
    try:
        assert list(rig.service._batches) == [handle.id for handle in handles]  # noqa: SLF001
        releases[0].set()
        assert first.wait(10)
        assert callback_done.wait(5), "Completion listeners may reenter polling without deadlock"
        # The failed terminal write must not discard the only accurate live outcome.
        assert list(rig.service._batches) == [handle.id for handle in handles]  # noqa: SLF001
        status = rig.service.status(WORKSPACE, first.id)
        assert status["state"] == ("failed" if job_fails else "done")
        assert status["completion_warning"]
        if job_fails:
            assert status["error"]["code"] == "needs_confirmation"
            assert "receipt" not in status
        else:
            assert status["receipt"] == {"index": 0}
            assert "error" not in status
        assert callback_statuses == [status]
        mirror_record = rig.service._mirror_record(first.report_id, first.id)  # noqa: SLF001
        assert mirror_record is not None and mirror_record["state"] == "running"

        # Submit-time pruning must also preserve the unsaved terminal result.
        later = rig.service.submit(rig.report(), "custom", "unsaved-burst-00006", "6", job(6))
        handles.append(later)
        assert list(rig.service._batches) == [handle.id for handle in handles]  # noqa: SLF001
        assert rig.service.status(WORKSPACE, first.id) == status
    finally:
        for release in releases:
            release.set()
    for handle in handles[1:]:
        assert handle.wait(10)
    # Saved outcomes are still pruned down to the cap, retaining the unsaved first one.
    assert list(rig.service._batches) == [  # noqa: SLF001
        first.id,
        handles[-2].id,
        handles[-1].id,
    ]
    assert rig.service.status(WORKSPACE, first.id) == status
    # An evicted saved outcome remains pollable from its terminal disk mirror.
    assert rig.service.status(WORKSPACE, handles[1].id)["receipt"] == {"index": 1}
