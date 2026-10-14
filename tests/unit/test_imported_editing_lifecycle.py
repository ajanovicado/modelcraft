"""Desktop discovery follows the service shutdown deadline without touching Desktop."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.studio import imported_editing
from ai_powerbi_analyst.studio.desktop_session import DesktopReadiness
from ai_powerbi_analyst.studio.imported_editing import (
    DesktopTools,
    ImportedEditingService,
    NoDesktop,
)
from ai_powerbi_analyst.studio.report_import import ImportedReport
from ai_powerbi_analyst.studio.state import StudioStateStore


def _service(tmp_path: Path) -> ImportedEditingService:
    def unused_report(_workspace: str) -> ImportedReport:
        raise AssertionError("Discovery must not load a report")

    return ImportedEditingService(
        tmp_path,
        StudioStateStore(tmp_path / "state.sqlite3"),
        load_report=unused_report,
        tools=lambda: DesktopTools(["bridge"], "pbi", "PBIDesktop.exe"),
    )


def _desktop() -> Any:
    return imported_editing._Desktop(  # noqa: SLF001
        NoDesktop(), DesktopReadiness(False, False, False, False, None, ["not installed"])
    )


def test_close_waits_for_an_inflight_discovery_before_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release, finished, closing, closed = (threading.Event() for _ in range(5))

    def discover(_root: Path, _tools: DesktopTools) -> Any:
        entered.set()
        assert release.wait(3)
        finished.set()
        return _desktop()

    monkeypatch.setattr(imported_editing, "discover_desktop", discover)
    service = _service(tmp_path)
    assert service.readiness() is None
    assert entered.wait(2)

    def shutdown() -> None:
        closing.set()
        service.close(timeout=2)
        closed.set()

    closer = threading.Thread(target=shutdown)
    closer.start()
    try:
        assert closing.wait(2)
        assert not closed.wait(0.05)
        release.set()
        assert closed.wait(2)
        assert finished.is_set()
    finally:
        release.set()
        closer.join(3)
        service.close(timeout=2)
    assert not closer.is_alive()


def test_repeated_payload_lookups_share_discovery_and_do_not_refresh_after_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def discover(_root: Path, _tools: DesktopTools) -> Any:
        calls.append("probe")
        entered.set()
        assert release.wait(3)
        return _desktop()

    def forbidden_close(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Discovery shutdown must not close a Desktop window")

    monkeypatch.setattr(imported_editing, "discover_desktop", discover)
    monkeypatch.setattr(imported_editing, "MISSING_TTL", 0)
    monkeypatch.setattr(NoDesktop, "close", forbidden_close)
    service = _service(tmp_path)
    try:
        assert service.readiness() is None
        assert entered.wait(2)
        assert [service.readiness() for _ in range(20)] == [None] * 20
        assert calls == ["probe"]
        release.set()
        service.close(timeout=2)
        for _ in range(20):
            answer = service.readiness()
            assert answer is not None and answer.can_edit_model is False
        assert calls == ["probe"]
    finally:
        release.set()
        service.close(timeout=2)


def test_close_bounds_a_slow_probe_and_can_finish_cleanup_on_a_second_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release, finished = (threading.Event() for _ in range(3))
    calls: list[str] = []

    def discover(_root: Path, _tools: DesktopTools) -> Any:
        calls.append("probe")
        entered.set()
        assert release.wait(3)
        finished.set()
        return _desktop()

    monkeypatch.setattr(imported_editing, "discover_desktop", discover)
    service = _service(tmp_path)
    try:
        assert service.readiness() is None
        assert entered.wait(2)
        start = time.monotonic()
        service.close(timeout=0.03)
        assert time.monotonic() - start < 0.5
        assert not finished.is_set()
        assert service.readiness() is None
        service.close(timeout=0)
        assert calls == ["probe"]
        release.set()
        service.close(timeout=2)
        assert finished.is_set()
        assert service.readiness() is not None
        assert calls == ["probe"]
    finally:
        release.set()
        service.close(timeout=2)


def test_close_before_the_first_lookup_does_not_start_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_discovery(_root: Path, _tools: DesktopTools) -> Any:
        raise AssertionError("A closed service must not start a Desktop probe")

    monkeypatch.setattr(imported_editing, "discover_desktop", forbidden_discovery)
    service = _service(tmp_path)
    service.close()
    assert service.readiness() is None
    service.close()
    assert service.readiness() is None
