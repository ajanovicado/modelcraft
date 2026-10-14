from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_powerbi_analyst.powerbi.transaction import hash_tree
from ai_powerbi_analyst.preflight import ToolProbe
from ai_powerbi_analyst.studio.contracts import (
    BuildJob,
    CreateWorkspaceRequest,
    ExportBuildRequest,
    StudioBuildState,
    UndoBuildRequest,
)
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH, StateConflictError


def _committed_build(service: StudioService, workspace_id: str, project: Path) -> BuildJob:
    now = datetime.now(UTC)
    job = BuildJob(
        id="build-test",
        workspace_id=workspace_id,
        draft_id="draft-test",
        state=StudioBuildState.COMMITTED,
        message="validated",
        project_path=str(project / "test-project.pbip"),
        project_hash=hash_tree(project),
        created_at=now,
        updated_at=now,
    )
    return service.store.put(
        "build",
        job,
        workspace_id=workspace_id,
        expected_base_hash=EMPTY_HASH,
        idempotency_key="seed-build-test",
        mutation_payload={"build": "test"},
    )


def test_committed_build_can_export_and_restore_managed_backup(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    service = StudioService(repository, tmp_path / "state")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Test project",
            idempotency_key="create-workspace-test",
            expected_base_hash=EMPTY_HASH,
        )
    )
    project = service.projects.projects_root / "test-project"
    project.mkdir(parents=True)
    (project / "test-project.pbip").write_text("current", encoding="utf-8")
    build = _committed_build(service, workspace.id, project)

    destination = tmp_path / "exports"
    destination.mkdir()
    export_request = ExportBuildRequest(
        workspace_id=workspace.id,
        destination_folder=str(destination),
        idempotency_key="export-build-test",
        expected_base_hash=workspace.base_hash,
    )
    exported = service.export_build(build.id, export_request)

    assert Path(exported.exported_project_path).read_text(encoding="utf-8") == "current"
    assert service.export_build(build.id, export_request) == exported

    backup = service.projects.backup_root / "test-project-previous"
    backup.mkdir(parents=True)
    (backup / "test-project.pbip").write_text("previous", encoding="utf-8")
    service.projects.undo_root.mkdir(parents=True)
    (service.projects.undo_root / "test-project.json").write_text(
        json.dumps({"backup_path": str(backup)}),
        encoding="utf-8",
    )
    undo_request = UndoBuildRequest(
        workspace_id=workspace.id,
        idempotency_key="undo-build-test",
        expected_base_hash=workspace.base_hash,
    )
    undone = service.undo_build(build.id, undo_request)

    assert undone.restored
    assert (project / "test-project.pbip").read_text(encoding="utf-8") == "previous"
    assert service.builds(workspace.id)[0].state is StudioBuildState.ROLLED_BACK
    assert service.undo_build(build.id, undo_request) == undone


def _openable_build(tmp_path: Path) -> tuple[StudioService, str, BuildJob]:
    repository = Path(__file__).resolve().parents[2]
    service = StudioService(repository, tmp_path / "state")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Open project",
            idempotency_key="create-open-workspace",
            expected_base_hash=EMPTY_HASH,
        )
    )
    project = service.projects.projects_root / "test-project"
    project.mkdir(parents=True)
    (project / "test-project.pbip").write_text("{}", encoding="utf-8")
    return service, workspace.id, _committed_build(service, workspace.id, project)


def test_open_powerbi_rejects_project_bytes_changed_after_validation(
    tmp_path: Path,
) -> None:
    service, _workspace_id, build = _openable_build(tmp_path)
    assert build.project_path is not None
    Path(build.project_path).write_text("tampered", encoding="utf-8")

    with pytest.raises(StateConflictError, match="Stale Power BI project"):
        service.open_powerbi(build.id)


def _patch_open_probes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    desktop = tmp_path / "PBIDesktop.exe"
    pbi = tmp_path / "pbi.exe"
    bridge = tmp_path / "powerbi-desktop.cmd"
    ollama = tmp_path / "ollama.exe"
    for path in (desktop, pbi, bridge, ollama):
        path.write_bytes(b"tool")
    monkeypatch.setattr(
        "ai_powerbi_analyst.studio.service.probe_powerbi_desktop",
        lambda: ToolProbe(name="powerbi-desktop", available=True, path=str(desktop)),
    )
    monkeypatch.setattr(
        "ai_powerbi_analyst.studio.service.probe_pbi_cli",
        lambda: ToolProbe(
            name="pbi",
            available=True,
            path=str(pbi),
            version="3.10.10",
            distribution_version="3.11.1",
        ),
    )

    def probe(name: str) -> ToolProbe:
        if name == "powerbi-desktop":
            return ToolProbe(
                name=name,
                available=True,
                path=str(bridge),
                version="0.1.2",
            )
        return ToolProbe(name=name, available=True, path=str(ollama), version="1.0.0")

    monkeypatch.setattr("ai_powerbi_analyst.studio.service.probe_tool", probe)


def test_open_powerbi_snapshots_launches_hydrates_and_emits_render_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, workspace_id, build = _openable_build(tmp_path)
    _patch_open_probes(monkeypatch, tmp_path)
    calls: list[str] = []

    class FakeVerifier:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def capture_baseline(self, _path: str) -> object:
            calls.append("snapshot")
            return object()

        def open_with_bridge(self, _path: Path) -> None:
            calls.append("bridge-open")

        def hydrate_and_verify(self, _baseline: object, evidence: Path) -> object:
            calls.append("hydrate")
            assert evidence == service.projects.projects_root / ".render-evidence" / build.id

            class Result:
                @staticmethod
                def as_dict() -> dict[str, object]:
                    return {
                        "data_refreshed": True,
                        "render_verified": True,
                        "cache_saved": False,
                        "refreshed_tables": ["Orders"],
                        "screenshot_paths": ["overview.png"],
                    }

            return Result()

    class FakeLauncher:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            self.open_path = kwargs["open_path"]

        def __call__(self) -> dict[str, object]:
            calls.append("launch")
            assert callable(self.open_path)
            self.open_path(Path(str(build.project_path)))
            return {"opened": True, "path": build.project_path}

    monkeypatch.setattr(
        "ai_powerbi_analyst.studio.service.DesktopHydrationVerifier",
        FakeVerifier,
    )
    monkeypatch.setattr(
        "ai_powerbi_analyst.studio.service.PowerBIDesktopLauncher",
        FakeLauncher,
    )

    result = service.open_powerbi(build.id)

    assert calls == ["snapshot", "launch", "bridge-open", "hydrate"]
    assert result["opened"] is True
    assert result["data_refreshed"] is True
    assert result["render_verified"] is True
    assert result["cache_saved"] is False
    assert result["refreshed_tables"] == ["Orders"]
    assert result["screenshot_paths"] == ["overview.png"]
    events = [
        event for event in service.events(workspace_id) if event["type"] == "build.render_verified"
    ]
    assert len(events) == 1
    assert events[0]["payload"]["build_id"] == build.id


def test_open_powerbi_never_claims_success_when_hydration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, workspace_id, build = _openable_build(tmp_path)
    _patch_open_probes(monkeypatch, tmp_path)

    class FakeVerifier:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def capture_baseline(self, _path: str) -> object:
            return object()

        def open_with_bridge(self, _path: Path) -> None:
            pass

        def hydrate_and_verify(self, _baseline: object, _evidence: Path) -> object:
            raise RuntimeError("page capture failed")

    class FakeLauncher:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __call__(self) -> dict[str, object]:
            return {"opened": True, "path": build.project_path}

    monkeypatch.setattr(
        "ai_powerbi_analyst.studio.service.DesktopHydrationVerifier",
        FakeVerifier,
    )
    monkeypatch.setattr(
        "ai_powerbi_analyst.studio.service.PowerBIDesktopLauncher",
        FakeLauncher,
    )

    result = service.open_powerbi(build.id)

    assert result == {
        "opened": False,
        "reason": "Desktop refresh/render verification failed: page capture failed",
        "data_refreshed": False,
        "render_verified": False,
        "cache_saved": False,
        "refreshed_tables": [],
        "screenshot_paths": [],
    }
    assert not any(
        event["type"] == "build.render_verified" for event in service.events(workspace_id)
    )
