"""Build resource ownership, ordering, rejection and failure isolation."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_powerbi_analyst.llm.runtime import LocalModelError, OllamaModelRuntime
from ai_powerbi_analyst.settings import ModelSettings
from ai_powerbi_analyst.studio.contracts import (
    ApproveDraftRequest,
    BuildRequest,
    CreateWorkspaceRequest,
    DraftRequest,
    IngestRequest,
    StudioBuildState,
)
from ai_powerbi_analyst.studio.planner import StudioPlanningError, StudioPlanningService
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH, StateConflictError


@pytest.fixture
def ready(tmp_path_factory):
    # Keep the real Desktop path guard enabled; pytest's long test-name paths
    # otherwise exceed its native file-path budget before this fixture can build.
    tmp_path = tmp_path_factory.mktemp("br")
    source = tmp_path / "selected" / "orders.csv"
    source.parent.mkdir()
    source.write_text("Region,Sales\nEast,100\nWest,200\n", encoding="utf-8")
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    service.projects.require_external_validators = False
    service.projects._external_validation = lambda _project: ([], [])
    service.projects.desktop_closed_check = lambda: None
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Resource build",
            selected_folder=str(source.parent),
            idempotency_key="resource-workspace",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="resource-ingest",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Build sales by region",
            idempotency_key="resource-draft",
            expected_base_hash=workspace.base_hash,
        )
    )
    approved = service.approve_draft(
        draft.id,
        ApproveDraftRequest(
            workspace_id=workspace.id,
            approved_relationship_ids=[],
            idempotency_key="resource-approve",
            expected_base_hash=draft.base_hash,
        ),
    )
    workspace = service.workspace(workspace.id)
    request = BuildRequest(
        workspace_id=workspace.id,
        draft_id=approved.id,
        project_name="Resource Project",
        idempotency_key="resource-build",
        expected_base_hash=workspace.base_hash,
    )
    try:
        yield service, request
    finally:
        service.editing.close()
        service.knowledge.close()


def _track_owned_pause(service, monkeypatch, *, fail=False):
    calls = []

    @contextmanager
    def pause():
        calls.append("unload")
        if fail:
            raise LocalModelError("Owned model worker did not shut down")
        try:
            yield
        finally:
            calls.append("release")

    service.model = SimpleNamespace(pause_inference=pause)
    original = service.projects.build

    def build(*args, **kwargs):
        calls.append("build")
        assert calls[-2] == "unload"
        return original(*args, **kwargs)

    monkeypatch.setattr(service.projects, "build", build)
    return calls


def _project_files(service):
    return {
        str(path.relative_to(service.projects.projects_root)): path.read_bytes()
        for path in service.projects.projects_root.rglob("*")
        if path.is_file()
    }


def test_build_pauses_before_validator_and_replays_without_resource_side_effects(
    ready, monkeypatch
):
    service, request = ready
    calls = _track_owned_pause(service, monkeypatch)
    job = service.build(request)
    assert job.state is StudioBuildState.COMMITTED and job.project_path, job.message
    assert calls == ["unload", "build", "release"]
    files = _project_files(service)
    assert service.build(request) == job
    assert calls == ["unload", "build", "release"] and _project_files(service) == files
    with pytest.raises(StateConflictError):
        service.build(request.model_copy(update={"project_name": "Different Project"}))
    assert calls == ["unload", "build", "release"] and _project_files(service) == files


@pytest.mark.parametrize("rejection", ["hash", "foreign_draft", "stale_draft", "unapproved"])
def test_rejected_build_never_unloads_or_starts_project_work(ready, monkeypatch, rejection):
    service, request = ready
    calls = _track_owned_pause(service, monkeypatch)
    if rejection == "hash":
        request = request.model_copy(update={"expected_base_hash": EMPTY_HASH})
    elif rejection in {"foreign_draft", "unapproved"}:
        draft = service.draft(request.draft_id)
        changed = draft.model_copy(
            update={"workspace_id": "workspace-foreign"}
            if rejection == "foreign_draft"
            else {"status": "draft"}
        )
        service.store.put(
            "draft",
            changed,
            workspace_id=draft.workspace_id,
            expected_base_hash=draft.base_hash,
            idempotency_key="resource-reject-draft",
            mutation_payload={"test": rejection},
        )
    else:
        workspace = service.workspace(request.workspace_id)
        changed = service.store.put(
            "workspace",
            workspace.model_copy(update={"active_draft_id": None}),
            workspace_id=workspace.id,
            expected_base_hash=workspace.base_hash,
            idempotency_key="resource-reject-stale",
            mutation_payload={"test": rejection},
        )
        request = request.model_copy(update={"expected_base_hash": changed.base_hash})
    files = _project_files(service)
    with pytest.raises((StateConflictError, ValueError, StudioPlanningError)):
        service.build(request)
    assert (
        calls == []
        and _project_files(service) == files
        and service.builds(request.workspace_id) == []
    )


def test_owned_unload_failure_records_failed_job_without_project_mutation(ready, monkeypatch):
    service, request = ready
    marker = service.projects.projects_root / "existing-project" / "keep.pbip"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"Untouched existing project")
    calls = _track_owned_pause(service, monkeypatch, fail=True)
    before = _project_files(service)
    job = service.build(request)
    assert job.state is StudioBuildState.FAILED and "did not shut down" in job.message
    assert calls == ["unload"] and _project_files(service) == before
    assert job.project_path is None and job.project_hash is None
    assert service.build(request) == job and calls == ["unload"]


def test_validator_failure_releases_owned_pause(ready, monkeypatch):
    service, request = ready
    calls = _track_owned_pause(service, monkeypatch)

    def failed_validator(*_args, **_kwargs):
        calls.append("failed-validator")
        raise ValueError("Validator rejected the project")

    monkeypatch.setattr(service.projects, "build", failed_validator)
    job = service.build(request)
    assert job.state is StudioBuildState.FAILED and "Validator rejected" in job.message
    assert calls == ["unload", "failed-validator", "release"]


def test_shared_legacy_runtime_is_not_unloaded_by_build(ready, monkeypatch):
    service, request = ready
    calls = []
    legacy = OllamaModelRuntime(ModelSettings())
    monkeypatch.setattr(legacy, "unload", lambda: calls.append("legacy-unload"))
    service.model = legacy
    job = service.build(request)
    assert job.state is StudioBuildState.COMMITTED and not calls, job.message
