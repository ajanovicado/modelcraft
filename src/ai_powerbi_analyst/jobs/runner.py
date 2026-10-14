"""Synchronous job runner with durable at-most-once claiming."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from .journal import JobJournal, JobRecord, JobStatus

ProgressReporter = Callable[[str, str, float], None]
JobHandler = Callable[[dict[str, Any], ProgressReporter], Mapping[str, Any]]


class JobRunner:
    def __init__(
        self,
        journal: JobJournal,
        *,
        duplicate_wait_seconds: float = 120.0,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        self.journal = journal
        self.duplicate_wait_seconds = duplicate_wait_seconds
        self.poll_interval_seconds = poll_interval_seconds

    def run(
        self,
        job_id: str,
        handler: JobHandler,
        *,
        progress_listener: ProgressReporter | None = None,
    ) -> JobRecord:
        current = self.journal.get(job_id)
        if current.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
            return current
        if current.status is JobStatus.RUNNING:
            return self._wait_for_terminal(job_id, progress_listener=progress_listener)
        if not self.journal.claim(job_id):
            return self._wait_for_terminal(job_id, progress_listener=progress_listener)

        try:
            if progress_listener is not None:
                progress_listener("start", "Started", 0.0)

            def report(phase: str, message: str, progress: float) -> None:
                self.journal.append_event(job_id, phase, message, progress=progress)
                if progress_listener is not None:
                    progress_listener(phase, message, progress)

            result = handler(current.request, report)
        except BaseException as exc:
            detail = str(exc).strip()
            message = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
            failed = self.journal.fail(job_id, message)
            if not isinstance(exc, Exception):
                # Preserve control-flow semantics, but never strand a durable
                # RUNNING job in the still-live Streamlit process.
                raise
            return failed
        return self.journal.succeed(job_id, result)

    def _wait_for_terminal(
        self,
        job_id: str,
        *,
        progress_listener: ProgressReporter | None = None,
    ) -> JobRecord:
        deadline = time.monotonic() + self.duplicate_wait_seconds
        last_sequence = 0
        while time.monotonic() < deadline:
            if progress_listener is not None:
                for event in self.journal.events(job_id):
                    if event.sequence <= last_sequence:
                        continue
                    progress_listener(event.phase, event.message, event.progress)
                    last_sequence = event.sequence
            current = self.journal.get(job_id)
            if current.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
                return current
            time.sleep(self.poll_interval_seconds)
        return self.journal.get(job_id)
