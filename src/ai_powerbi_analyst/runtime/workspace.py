"""Assemble the real local analytics runtime and safe Power BI composition boundary."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from ai_powerbi_analyst.analytics.service import AnalyticsService
from ai_powerbi_analyst.contracts import (
    CompatibilityPins,
    ProjectBundle,
    ProjectCapabilities,
)
from ai_powerbi_analyst.data.catalog import catalog_sha256, load_semantic_catalog
from ai_powerbi_analyst.jobs.journal import JobJournal
from ai_powerbi_analyst.orchestration.analysis_planner import RuleFirstAnalysisPlanner
from ai_powerbi_analyst.orchestration.conversation import ConversationState
from ai_powerbi_analyst.orchestration.explainer import GroundedPowerBIExplainer
from ai_powerbi_analyst.orchestration.integrations import (
    AnalyticsServiceAdapter,
    SafeComposerAdapter,
    ThreePageReportPlanner,
)
from ai_powerbi_analyst.orchestration.router import RuleFirstRouter
from ai_powerbi_analyst.orchestration.service import ApplicationOrchestrator
from ai_powerbi_analyst.powerbi.composer import SafePowerBIComposer
from ai_powerbi_analyst.powerbi.mock_backend import PAGES_SCHEMA, MockPowerBIBackend
from ai_powerbi_analyst.powerbi.transaction import hash_tree
from ai_powerbi_analyst.powerbi.validation import validate_report
from ai_powerbi_analyst.ui.models import AppWorkspace, WorkspaceConfig

PowerBIMode = Literal["mock", "real"]


class RuntimeAssemblyError(RuntimeError):
    """The frozen project or requested execution mode cannot be assembled safely."""


class RealPowerBIModePending(RuntimeAssemblyError):
    """Real report writes remain closed until the target Windows gate is proven."""


@dataclass(frozen=True, slots=True)
class LocalRuntimePaths:
    """Stable app-owned paths underneath one runtime root."""

    root: Path
    project_path: Path
    pbip_path: Path
    report_path: Path
    semantic_model_path: Path
    transaction_state_path: Path
    journal_path: Path


def local_runtime_paths(runtime_root: str | Path) -> LocalRuntimePaths:
    root = Path(runtime_root).expanduser().resolve(strict=False)
    project = root / "powerbi" / "SalesMarginDemo"
    return LocalRuntimePaths(
        root=root,
        project_path=project,
        pbip_path=project / "SalesMarginDemo.pbip",
        report_path=project / "SalesMargin.Report",
        semantic_model_path=project / "SalesMargin.SemanticModel",
        transaction_state_path=root / "transactions",
        journal_path=root / "jobs" / "jobs.sqlite3",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_verified_manifest(data_root: Path) -> dict[str, object]:
    manifest_path = data_root / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeAssemblyError(f"prepared analytical manifest is missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeAssemblyError("prepared analytical manifest is invalid") from error
    if not isinstance(manifest, dict):
        raise RuntimeAssemblyError("prepared analytical manifest must be a JSON object")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise RuntimeAssemblyError("prepared analytical manifest has no file inventory")
    for name in ("catalog", "duckdb", "sales_parquet"):
        record = files.get(name)
        if not isinstance(record, dict):
            raise RuntimeAssemblyError(f"prepared analytical manifest is missing {name}")
        relative_path = record.get("path")
        expected_hash = record.get("sha256")
        if not isinstance(relative_path, str) or not isinstance(expected_hash, str):
            raise RuntimeAssemblyError(f"prepared analytical manifest has an invalid {name}")
        candidate = (data_root / relative_path).resolve(strict=False)
        if not candidate.is_relative_to(data_root.resolve()):
            raise RuntimeAssemblyError(f"prepared analytical path escapes its root: {name}")
        if not candidate.is_file() or _sha256(candidate) != expected_hash:
            raise RuntimeAssemblyError(f"prepared analytical file failed its hash check: {name}")
    return manifest


def _write_if_missing(path: Path, text: str) -> None:
    if path.exists():
        if not path.is_file():
            raise RuntimeAssemblyError(f"expected an app-owned file: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ensure_mock_powerbi_project(
    paths: LocalRuntimePaths,
    *,
    semantic_model_template: Path,
) -> None:
    """Create only missing app-owned baseline files; never reset a live report."""

    paths.project_path.mkdir(parents=True, exist_ok=True)
    if not paths.semantic_model_path.exists():
        shutil.copytree(semantic_model_template, paths.semantic_model_path)
    elif not paths.semantic_model_path.is_dir():
        raise RuntimeAssemblyError("mock semantic-model path is not a directory")
    pages_root = paths.report_path / "definition" / "pages"
    pages_root.mkdir(parents=True, exist_ok=True)

    _write_if_missing(
        paths.pbip_path,
        json.dumps(
            {
                "version": "1.0",
                "artifacts": [
                    {"report": {"path": "SalesMargin.Report"}},
                    {"semanticModel": {"path": "SalesMargin.SemanticModel"}},
                ],
                "developmentMode": "mock-pbir-structural-validation-only",
            },
            indent=2,
        )
        + "\n",
    )
    _write_if_missing(
        pages_root / "pages.json",
        json.dumps(
            {"$schema": PAGES_SCHEMA, "pageOrder": [], "activePageName": None},
            indent=2,
        )
        + "\n",
    )


def _project_bundle(
    *,
    data_root: Path,
    paths: LocalRuntimePaths,
    manifest: dict[str, object],
) -> ProjectBundle:
    files = manifest["files"]
    assert isinstance(files, dict)
    duckdb_record = files["duckdb"]
    catalog_record = files["catalog"]
    source_record = files["sales_parquet"]
    assert isinstance(duckdb_record, dict)
    assert isinstance(catalog_record, dict)
    assert isinstance(source_record, dict)
    catalog_path = data_root / str(catalog_record["path"])
    database_path = data_root / str(duckdb_record["path"])
    data_through_raw = manifest.get("data_through")
    if not isinstance(data_through_raw, str):
        raise RuntimeAssemblyError("prepared analytical manifest has no data cutoff")
    return ProjectBundle(
        id="sales-margin-demo",
        name="Sales & Margin",
        pbip_path=paths.pbip_path,
        report_path=paths.report_path,
        semantic_model_path=paths.semantic_model_path,
        duckdb_path=database_path,
        catalog_path=catalog_path,
        data_hash=str(manifest["data_hash"]),
        source_data_hash=str(source_record["sha256"]),
        duckdb_hash=str(duckdb_record["sha256"]),
        catalog_hash=str(catalog_record["sha256"]),
        report_hash=hash_tree(paths.report_path),
        semantic_model_hash=hash_tree(paths.semantic_model_path),
        catalog_version="1.0.0",
        data_through=date.fromisoformat(data_through_raw),
        forecast_version=str(manifest.get("forecast_version", "1.0.0")),
        scenario_version=str(manifest.get("scenario_version", "1.0.0")),
        baseline_validation_status="passed",
        compatibility=CompatibilityPins(),
        capabilities=ProjectCapabilities(
            can_analyze=True,
            can_explain=True,
            can_build=True,
            can_modify=True,
            can_open_desktop=False,
            blockers=[
                "Mock mode proves deterministic structure and bindings only; Power BI "
                "Desktop render verification remains a Windows release gate."
            ],
        ),
    )


def build_local_workspace(
    project_root: Path,
    runtime_root: Path,
    powerbi_mode: PowerBIMode = "mock",
) -> AppWorkspace:
    """Build the production app graph over the frozen local fixture.

    Mock mode exercises the complete transaction, validation, journal, analytics,
    and undo flow without making a Power BI Desktop compatibility claim. Real mode
    intentionally remains closed until its pinned Windows kill test is recorded.
    """

    repository = Path(project_root).expanduser().resolve(strict=True)
    if not repository.is_dir():
        raise RuntimeAssemblyError(f"project root is not a directory: {repository}")
    if powerbi_mode not in {"mock", "real"}:
        raise ValueError(f"unsupported Power BI mode: {powerbi_mode}")
    if powerbi_mode == "real":
        raise RealPowerBIModePending(
            "Real Power BI composition remains fail-closed until the pinned Windows, "
            "pbi-cli, Microsoft validator, and Desktop reopen gate is proven."
        )

    paths = local_runtime_paths(runtime_root)
    if paths.root.exists() and paths.root.is_symlink():
        raise RuntimeAssemblyError("runtime root cannot be a symbolic link")
    paths.root.mkdir(parents=True, exist_ok=True)
    data_root = repository / "fixtures" / "data"
    manifest = _load_verified_manifest(data_root)
    catalog_path = data_root / "semantic_catalog.yaml"
    catalog = load_semantic_catalog(catalog_path)
    if catalog_sha256(catalog_path) != manifest.get("catalog_hash"):
        raise RuntimeAssemblyError("semantic catalog hash disagrees with the manifest")

    _ensure_mock_powerbi_project(
        paths,
        semantic_model_template=(
            repository / "fixtures/powerbi/client-wow-template/ClientWow.SemanticModel"
        ),
    )
    baseline = validate_report(paths.report_path, catalog=catalog)
    if not baseline.passed:
        codes = ", ".join(issue.code for issue in (*baseline.errors, *baseline.warnings))
        raise RuntimeAssemblyError(f"mock report baseline failed closed: {codes}")

    bundle = _project_bundle(data_root=data_root, paths=paths, manifest=manifest)
    analytics = AnalyticsService.from_project_bundle(bundle)
    analysis_planner = RuleFirstAnalysisPlanner(catalog)
    analyst = AnalyticsServiceAdapter(analysis_planner, analytics)
    safe_composer = SafePowerBIComposer(
        project_root=paths.project_path,
        report_relative_path=paths.report_path.relative_to(paths.project_path),
        state_root=paths.transaction_state_path,
        backend=MockPowerBIBackend(),
    )
    composer = SafeComposerAdapter(
        safe_composer,
        catalog,
        catalog_hash=bundle.catalog_hash or "",
        data_hash=bundle.data_hash,
        state_root=paths.transaction_state_path,
    )
    report_planner = ThreePageReportPlanner(
        catalog,
        report_hash=lambda: hash_tree(paths.report_path),
    )
    journal = JobJournal(paths.journal_path)
    journal.recover_abandoned(older_than_seconds=300)
    orchestrator = ApplicationOrchestrator(
        router=RuleFirstRouter(),
        planner=report_planner,
        analyst=analyst,
        explainer=GroundedPowerBIExplainer(
            catalog,
            report_path=paths.report_path,
            semantic_model_path=paths.semantic_model_path,
        ),
        composer=composer,
        journal=journal,
    )
    initial_state = ConversationState(project_id=bundle.id)
    initial_prompt = (
        "Build a three-page executive dashboard for sales and margin. Show what changed, "
        "what drove it, and next-quarter outlook. Include our approved 3% price scenario."
    )
    initial_plan = report_planner.plan(initial_prompt, initial_state)
    initial_active_plan = initial_plan if composer.report_plan_installed(initial_plan) else None
    restored_state = orchestrator.restore_state(bundle.id, initial_state)
    return AppWorkspace(
        config=WorkspaceConfig(
            project_id=bundle.id,
            project_name=bundle.name,
            data_through=bundle.data_through,
            approved_metrics=tuple(metric.name for metric in catalog.metrics),
            compatibility_label="Locally validated",
            compatibility_detail=(
                "Frozen data, semantic bindings, and PBIR structure verified; "
                "Windows Desktop render proof pending"
            ),
            can_open_power_bi=False,
        ),
        orchestrator=orchestrator,
        initial_prompt=initial_prompt,
        initial_plan=initial_plan,
        initial_active_plan=initial_active_plan,
        initial_evidence=None,
        initial_analysis_plan=None,
        initial_conversation=restored_state,
        initial_validated=initial_active_plan is not None or orchestrator.undo_available,
        initial_undo_available=orchestrator.undo_available,
    )
