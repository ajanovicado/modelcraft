from __future__ import annotations

import json
import threading
import time

import pytest

from ai_powerbi_analyst.jobs.journal import (
    IdempotencyConflict,
    JobJournal,
    JobStatus,
    ProgressRegression,
)
from ai_powerbi_analyst.jobs.runner import JobRunner


def test_create_job_is_idempotent_across_journal_instances(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    first = JobJournal(database)

    created = first.create_or_get("apply_report", "request-123", {"plan": "abc"})
    reopened = JobJournal(database).create_or_get("apply_report", "request-123", {"plan": "abc"})

    assert reopened.id == created.id
    assert reopened.status is JobStatus.PENDING


def test_reusing_idempotency_key_with_different_payload_is_rejected(tmp_path) -> None:
    journal = JobJournal(tmp_path / "jobs.sqlite3")
    journal.create_or_get("apply_report", "same-key", {"plan": "one"})

    with pytest.raises(IdempotencyConflict):
        journal.create_or_get("apply_report", "same-key", {"plan": "two"})


def test_claim_is_atomic_and_only_one_runner_can_execute(tmp_path) -> None:
    journal = JobJournal(tmp_path / "jobs.sqlite3")
    job = journal.create_or_get("apply_report", "request-1", {"plan": "abc"})

    assert journal.claim(job.id) is True
    assert journal.claim(job.id) is False


def test_progress_events_must_be_monotonic(tmp_path) -> None:
    journal = JobJournal(tmp_path / "jobs.sqlite3")
    job = journal.create_or_get("apply_report", "request-1", {"plan": "abc"})
    assert journal.claim(job.id)
    journal.append_event(job.id, "stage", "Staging report", progress=0.5)

    with pytest.raises(ProgressRegression):
        journal.append_event(job.id, "validate", "Validating", progress=0.4)


def test_runner_records_progress_and_terminal_result_once(tmp_path) -> None:
    journal = JobJournal(tmp_path / "jobs.sqlite3")
    runner = JobRunner(journal)
    job = journal.create_or_get("apply_report", "request-1", {"plan": "abc"})
    calls: list[dict[str, str]] = []

    def handler(payload: dict[str, str], report) -> dict[str, bool]:  # type: ignore[no-untyped-def]
        calls.append(payload)
        report("stage", "Staged a safe copy", 0.25)
        report("validate", "Validated report", 0.9)
        return {"validated": True}

    completed = runner.run(job.id, handler)
    duplicate = runner.run(job.id, handler)

    assert completed.status is JobStatus.SUCCEEDED
    assert duplicate.status is JobStatus.SUCCEEDED
    assert calls == [{"plan": "abc"}]
    assert json.loads(completed.result_json or "{}") == {"validated": True}
    assert [event.progress for event in journal.events(job.id)] == [0.0, 0.25, 0.9, 1.0]


def test_runner_marks_job_failed_before_propagating_system_exit(tmp_path) -> None:
    journal = JobJournal(tmp_path / "jobs.sqlite3")
    runner = JobRunner(journal, duplicate_wait_seconds=0.01)
    job = journal.create_or_get("apply_report", "request-system-exit", {"plan": "abc"})

    def terminate(_payload, _report):  # type: ignore[no-untyped-def]
        raise SystemExit(17)

    with pytest.raises(SystemExit) as error:
        runner.run(job.id, terminate)

    assert error.value.code == 17
    failed = journal.get(job.id)
    assert failed.status is JobStatus.FAILED
    assert failed.error == "SystemExit: 17"
    assert journal.recover_abandoned(older_than_seconds=0) == []
    assert runner.run(job.id, terminate).status is JobStatus.FAILED


def test_stale_running_job_can_be_failed_closed_after_process_loss(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    journal = JobJournal(database, owner_id="dead-process")
    job = journal.create_or_get("apply_report", "request-1", {"plan": "abc"})
    assert journal.claim(job.id)

    replacement = JobJournal(database, owner_id="replacement-process")
    recovered = replacement.recover_abandoned(older_than_seconds=300)

    assert [item.id for item in recovered] == [job.id]
    assert replacement.get(job.id).status is JobStatus.FAILED
    assert replacement.get(job.id).error == "Operation abandoned after its worker stopped"


def test_refresh_in_the_same_process_never_recovers_an_active_job(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    owner = JobJournal(database)
    job = owner.create_or_get("apply_report", "request-1", {"plan": "abc"})
    assert owner.claim(job.id)

    refreshed = JobJournal(database)

    assert refreshed.recover_abandoned(older_than_seconds=0) == []
    assert refreshed.get(job.id).status is JobStatus.RUNNING


def test_dead_owner_is_recovered_before_same_key_is_resubmitted(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    crashed = JobJournal(database, owner_id="crashed-process")
    first = crashed.create_or_get("apply_report", "request-1", {"plan": "abc"})
    assert crashed.claim(first.id)

    restarted = JobJournal(database, owner_id="restarted-process")
    retry = restarted.create_or_get("apply_report", "request-1", {"plan": "abc"})

    assert restarted.get(first.id).status is JobStatus.FAILED
    assert retry.id != first.id
    assert retry.status is JobStatus.PENDING


def test_failed_logical_request_gets_a_fresh_retry_job(tmp_path) -> None:
    journal = JobJournal(tmp_path / "jobs.sqlite3")
    first = journal.create_or_get("apply_report", "request-1", {"plan": "abc"})
    assert journal.claim(first.id)
    journal.fail(first.id, "validator failed")

    retry = journal.create_or_get("apply_report", "request-1", {"plan": "abc"})

    assert retry.id != first.id
    assert retry.status is JobStatus.PENDING
    assert journal.get(first.id).status is JobStatus.FAILED


def test_workspace_context_survives_journal_reopen(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    JobJournal(database).save_context("demo", {"active_page_key": "margin_drivers"})

    restored = JobJournal(database).load_context("demo")

    assert restored == {"active_page_key": "margin_drivers"}


def test_duplicate_runner_waits_for_the_claimed_job_result(tmp_path) -> None:
    journal = JobJournal(tmp_path / "jobs.sqlite3")
    first_runner = JobRunner(journal, duplicate_wait_seconds=2, poll_interval_seconds=0.01)
    duplicate_runner = JobRunner(
        JobJournal(journal.database_path),
        duplicate_wait_seconds=2,
        poll_interval_seconds=0.01,
    )
    job = journal.create_or_get("apply_report", "request-1", {"plan": "abc"})
    started = threading.Event()

    def handler(_payload, report):  # type: ignore[no-untyped-def]
        started.set()
        report("compose", "Writing staged report", 0.4)
        time.sleep(0.05)
        return {"validated": True}

    thread = threading.Thread(target=lambda: first_runner.run(job.id, handler))
    thread.start()
    assert started.wait(timeout=1)

    replayed: list[tuple[str, str, float]] = []
    duplicate = duplicate_runner.run(
        job.id,
        handler,
        progress_listener=lambda phase, message, progress: replayed.append(
            (phase, message, progress)
        ),
    )
    thread.join(timeout=1)

    assert duplicate.status is JobStatus.SUCCEEDED
    assert json.loads(duplicate.result_json or "{}") == {"validated": True}
    assert ("compose", "Writing staged report", 0.4) in replayed
