from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from ai_powerbi_analyst.contracts import (
    ReportChangePlan,
    ReportPagePlan,
    SemanticCatalog,
    VisualRecipe,
)
from ai_powerbi_analyst.powerbi.backend import BackendReceipt
from ai_powerbi_analyst.powerbi.composer import ComposerProgress, SafePowerBIComposer
from ai_powerbi_analyst.powerbi.mock_backend import MockPowerBIBackend
from ai_powerbi_analyst.powerbi.recipes import DashboardBlueprint
from ai_powerbi_analyst.powerbi.transaction import hash_tree
from ai_powerbi_analyst.powerbi.validation import ValidationReport


class _StaticCompiler:
    def __init__(self, blueprint: DashboardBlueprint) -> None:
        self.blueprint = blueprint

    def compile(
        self,
        _plan: ReportChangePlan,
        _catalog: SemanticCatalog,
    ) -> DashboardBlueprint:
        return self.blueprint


class _BlockingBackend:
    requires_external_validation = False

    def __init__(self, release: threading.Event, *, fail: bool = False) -> None:
        self.release = release
        self.fail = fail
        self.delegate = MockPowerBIBackend()

    def apply(
        self,
        report_path: Path,
        blueprint: DashboardBlueprint,
    ) -> BackendReceipt:
        if not self.release.wait(timeout=1):
            raise TimeoutError("test did not receive a building heartbeat")
        if self.fail:
            raise RuntimeError("backend failed")
        return self.delegate.apply(report_path, blueprint)

    def apply_evidence(
        self,
        report_path: Path,
        blueprint: DashboardBlueprint,
    ) -> BackendReceipt:
        return self.apply(report_path, blueprint)


def _make_project(root: Path) -> tuple[Path, Path]:
    project = root / "Demo"
    report = project / "Sales.Report"
    pages = report / "definition" / "pages"
    pages.mkdir(parents=True)
    (pages / "pages.json").write_text(
        json.dumps({"pageOrder": [], "activePageName": None}) + "\n",
        encoding="utf-8",
    )
    (project / "Sales.pbip").write_text("{}\n", encoding="utf-8")
    return project, report


def _catalog() -> SemanticCatalog:
    return SemanticCatalog(version="1.0.0", metrics=[], dimensions=[])


def _plan(report: Path) -> ReportChangePlan:
    return ReportChangePlan(
        base_report_hash=hash_tree(report),
        catalog_version="1.0.0",
        pages=[
            ReportPagePlan(
                key="executive_pulse",
                title="Executive Pulse",
                visual_recipes=[VisualRecipe.KPI_ROW],
            )
        ],
    )


def _blueprint() -> DashboardBlueprint:
    return DashboardBlueprint(
        pages=(),
        style_preset="executive_light",
        catalog_version="1.0.0",
    )


def test_composer_heartbeats_during_blocking_backend_and_validator(tmp_path: Path) -> None:
    project, report = _make_project(tmp_path)
    backend_release = threading.Event()
    validator_release = threading.Event()
    heartbeat_counts = {"building": 0, "validating": 0}
    events: list[ComposerProgress] = []
    callback_thread_ids: list[int] = []
    calling_thread_id = threading.get_ident()

    def progress(event: ComposerProgress) -> None:
        events.append(event)
        callback_thread_ids.append(threading.get_ident())
        if not event.message.startswith("Still"):
            return
        heartbeat_counts[event.stage] += 1
        if event.stage == "building" and heartbeat_counts[event.stage] == 2:
            backend_release.set()
        if event.stage == "validating" and heartbeat_counts[event.stage] == 2:
            validator_release.set()

    def validator(_report_path: Path, _catalog_value: SemanticCatalog) -> ValidationReport:
        if not validator_release.wait(timeout=1):
            raise TimeoutError("test did not receive a validating heartbeat")
        return ValidationReport(source="test-validator", issues=())

    composer = SafePowerBIComposer(
        project,
        Path("Sales.Report"),
        tmp_path / "state",
        _BlockingBackend(backend_release),
        external_validator=validator,
        progress_heartbeat_interval_seconds=0.01,
    )
    composer.compiler = _StaticCompiler(_blueprint())  # type: ignore[assignment]

    result = composer.apply(_plan(report), _catalog(), progress=progress)

    assert result.validation.passed
    assert heartbeat_counts["building"] >= 2
    assert heartbeat_counts["validating"] >= 2
    assert events[-1].stage == "done"
    assert set(callback_thread_ids) == {calling_thread_id}
    assert not any(
        thread.name.startswith("ai-powerbi-operation-") for thread in threading.enumerate()
    )


def test_composer_stops_heartbeat_when_backend_raises(tmp_path: Path) -> None:
    project, report = _make_project(tmp_path)
    backend_release = threading.Event()
    heartbeats: list[ComposerProgress] = []

    def progress(event: ComposerProgress) -> None:
        if event.message.startswith("Still"):
            heartbeats.append(event)
            if len(heartbeats) == 2:
                backend_release.set()

    composer = SafePowerBIComposer(
        project,
        Path("Sales.Report"),
        tmp_path / "state",
        _BlockingBackend(backend_release, fail=True),
        progress_heartbeat_interval_seconds=0.01,
    )
    composer.compiler = _StaticCompiler(_blueprint())  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="backend failed"):
        composer.apply(_plan(report), _catalog(), progress=progress)

    count_after_failure = len(heartbeats)
    time.sleep(0.04)
    assert len(heartbeats) == count_after_failure
    assert not any(
        thread.name.startswith("ai-powerbi-operation-") for thread in threading.enumerate()
    )


def test_composer_stops_heartbeat_when_validator_raises(tmp_path: Path) -> None:
    project, report = _make_project(tmp_path)
    backend_release = threading.Event()
    backend_release.set()
    validator_release = threading.Event()
    heartbeats: list[ComposerProgress] = []

    def progress(event: ComposerProgress) -> None:
        if event.stage == "validating" and event.message.startswith("Still"):
            heartbeats.append(event)
            if len(heartbeats) == 2:
                validator_release.set()

    def validator(_report_path: Path, _catalog_value: SemanticCatalog) -> ValidationReport:
        if not validator_release.wait(timeout=1):
            raise TimeoutError("test did not receive a validating heartbeat")
        raise RuntimeError("validator failed")

    composer = SafePowerBIComposer(
        project,
        Path("Sales.Report"),
        tmp_path / "state",
        _BlockingBackend(backend_release),
        external_validator=validator,
        progress_heartbeat_interval_seconds=0.01,
    )
    composer.compiler = _StaticCompiler(_blueprint())  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="validator failed"):
        composer.apply(_plan(report), _catalog(), progress=progress)

    count_after_failure = len(heartbeats)
    time.sleep(0.04)
    assert len(heartbeats) == count_after_failure
    assert not any(
        thread.name.startswith("ai-powerbi-operation-") for thread in threading.enumerate()
    )


@pytest.mark.parametrize("interval", [0.0, -1.0, float("inf"), float("nan")])
def test_composer_rejects_invalid_heartbeat_interval(
    tmp_path: Path,
    interval: float,
) -> None:
    project, _report = _make_project(tmp_path)

    with pytest.raises(ValueError, match="finite positive"):
        SafePowerBIComposer(
            project,
            Path("Sales.Report"),
            tmp_path / "state",
            MockPowerBIBackend(),
            progress_heartbeat_interval_seconds=interval,
        )
