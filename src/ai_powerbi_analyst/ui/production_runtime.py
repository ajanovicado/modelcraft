"""Assemble the real local runtime from one validated ProjectBundle."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ai_powerbi_analyst.analytics.service import AnalyticsService
from ai_powerbi_analyst.contracts import ProjectBundle, SemanticCatalog
from ai_powerbi_analyst.data.catalog import load_semantic_catalog
from ai_powerbi_analyst.integrity import sha256_file
from ai_powerbi_analyst.jobs.journal import JobJournal
from ai_powerbi_analyst.llm.ollama import OllamaStructuredClient
from ai_powerbi_analyst.orchestration.analysis_planner import RuleFirstAnalysisPlanner
from ai_powerbi_analyst.orchestration.conversation import ConversationState
from ai_powerbi_analyst.orchestration.desktop import (
    CommandResult,
    PowerBIDesktopLauncher,
    assert_powerbi_write_allowed,
)
from ai_powerbi_analyst.orchestration.explainer import GroundedPowerBIExplainer
from ai_powerbi_analyst.orchestration.integrations import (
    AnalyticsServiceAdapter,
    SafeComposerAdapter,
    ThreePageReportPlanner,
)
from ai_powerbi_analyst.orchestration.interpretation import OllamaRequestInterpreter
from ai_powerbi_analyst.orchestration.router import RuleFirstRouter
from ai_powerbi_analyst.orchestration.service import ApplicationOrchestrator
from ai_powerbi_analyst.powerbi import (
    AuthoringCliAdapter,
    PbiCliAdapter,
    PbiCliBackend,
    SafePowerBIComposer,
    make_external_validator,
)
from ai_powerbi_analyst.powerbi.cli import CommandReceipt, SubprocessRunner
from ai_powerbi_analyst.powerbi.project import (
    validate_pbip_entrypoint,
    validate_report_semantic_reference,
)
from ai_powerbi_analyst.powerbi.theme import EXECUTIVE_LIGHT_THEME_FILE
from ai_powerbi_analyst.powerbi.transaction import (
    hash_tree,
    verify_validated_report_head,
)
from ai_powerbi_analyst.powerbi.validation import aggregate_validation, validate_report
from ai_powerbi_analyst.preflight import (
    EnvironmentReport,
    collect_environment,
    evaluate_readiness,
)
from ai_powerbi_analyst.runtime.preparation import (
    recover_interrupted_report_swap,
    verify_windows_demo,
)
from ai_powerbi_analyst.settings import AppSettings, load_settings

from .models import AppWorkspace, WorkspaceConfig


@dataclass(frozen=True, slots=True)
class _ExecutableIdentity:
    path: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class _AttestedRuntimeToolchain:
    pbi_cli: _ExecutableIdentity
    microsoft_validator: _ExecutableIdentity
    node: _ExecutableIdentity
    ollama: _ExecutableIdentity
    powerbi_desktop: _ExecutableIdentity


class _AttestedCommandRunner:
    """Execute one exact attested tool with its exact Node runtime first on PATH."""

    def __init__(
        self,
        primary: _ExecutableIdentity,
        *,
        dependencies: tuple[_ExecutableIdentity, ...] = (),
    ) -> None:
        self.primary = primary
        self.identities = (primary, *dependencies)
        environment = os.environ.copy()
        ordered_directories = list(
            dict.fromkeys(str(identity.path.parent) for identity in self.identities)
        )
        existing_path = environment.get("PATH")
        if existing_path:
            ordered_directories.append(existing_path)
        environment["PATH"] = os.pathsep.join(ordered_directories)
        self.delegate = SubprocessRunner(environment=environment)

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout: float = 30.0,
    ) -> CommandReceipt:
        if not argv or os.path.normcase(argv[0]) != os.path.normcase(str(self.primary.path)):
            raise RuntimeError("tool invocation differs from its attested executable path")
        for identity in self.identities:
            _assert_executable_identity(identity)
        return self.delegate.run(argv, cwd=cwd, timeout=timeout)


def _build_request_interpreter(
    settings: AppSettings,
    catalog: SemanticCatalog,
    *,
    scripted: bool,
    client_factory: Callable[..., OllamaStructuredClient] = OllamaStructuredClient,
) -> OllamaRequestInterpreter | None:
    """Build the optional presentation-only model boundary.

    Scripted mode deliberately returns before the client factory is touched.  This
    matters because constructing ``OllamaStructuredClient`` constructs the SDK
    client, even though the first network request would happen later.
    """

    if scripted:
        return None
    client = client_factory(settings=settings.model.model_copy(update={"max_retries": 0}))
    return OllamaRequestInterpreter(client, catalog)


def _scripted_environment_flag(raw: str | None) -> bool:
    if raw in (None, "", "0"):
        return False
    if raw == "1":
        return True
    raise RuntimeError("AI_POWERBI_SCRIPTED must be either 0 or 1")


@dataclass(slots=True)
class _NoResidentModelsResult:
    returncode: int = 0
    stdout: str = "No models running"


def _scripted_ollama_post(_url: str, _payload: dict[str, object]) -> None:
    """Deliberately perform no Ollama HTTP request in scripted mode."""


def _scripted_ollama_command(argv: tuple[str, ...]) -> CommandResult:
    """Represent the model-already-unloaded invariant without spawning Ollama."""

    if len(argv) != 2 or argv[1] != "ps":
        raise RuntimeError("scripted reveal cannot execute Ollama commands")
    return _NoResidentModelsResult()


def _build_desktop_launcher(
    bundle: ProjectBundle,
    settings: AppSettings,
    toolchain: _AttestedRuntimeToolchain,
    *,
    transaction_state: Path,
    operation_lock_path: Path,
    scripted: bool,
) -> PowerBIDesktopLauncher:
    launcher = PowerBIDesktopLauncher(
        bundle.pbip_path,
        model=settings.model.name,
        ollama_endpoint=settings.model.endpoint,
        ollama_executable=toolchain.ollama.path,
        ollama_executable_hash=toolchain.ollama.sha256,
        desktop_executable=toolchain.powerbi_desktop.path,
        desktop_executable_hash=toolchain.powerbi_desktop.sha256,
        pre_open_check=lambda: _validate_bundle_paths_and_hashes(
            bundle,
            transaction_state_path=transaction_state,
        ),
        operation_lock_path=operation_lock_path,
    )
    if scripted:
        # The scripted graph has no model client and never loads a model. Keep
        # the exact same Desktop launch, lock, hash, marker, and authority path,
        # while replacing only the otherwise unnecessary Ollama unload calls.
        launcher.http_post = _scripted_ollama_post
        launcher.run_command = _scripted_ollama_command
    return launcher


def _contains_version(raw: str | None, expected: str | None) -> bool:
    if expected is None:
        return False
    versions = re.findall(r"(?<!\d)(\d+\.\d+\.\d+(?:\.\d+)?)(?!\d)", raw or "")
    return expected in versions


def _require_real_runtime_gate(
    bundle: ProjectBundle,
    settings: AppSettings,
    environment: EnvironmentReport,
) -> None:
    readiness = evaluate_readiness(environment)
    blockers = list(readiness.blockers) if not readiness.can_write_real_powerbi else []
    capabilities = bundle.capabilities
    if bundle.baseline_validation_status != "passed":
        blockers.append("ProjectBundle baseline validation is not passed")
    if not capabilities.phase0_kill_test_passed:
        blockers.append("Phase 0 mutation/reopen kill test is not attested")
    if bundle.phase0_attestation_hash is None or len(bundle.phase0_receipt_hashes) != 7:
        blockers.append("Phase 0 raw evidence is not content-addressed in the ProjectBundle")
    if not capabilities.offline_schema_verified:
        blockers.append("Offline Microsoft schema validation is not attested")
    if not capabilities.desktop_render_verified:
        blockers.append("Power BI Desktop render verification is not attested")
    if not all(
        (
            capabilities.can_build,
            capabilities.can_modify,
            capabilities.can_open_desktop,
        )
    ):
        blockers.append("ProjectBundle real-write capabilities are not enabled")
    if capabilities.blockers:
        blockers.extend(capabilities.blockers)

    pins = bundle.compatibility
    if pins.pbi_cli_version != settings.powerbi.writer_version:
        blockers.append("ProjectBundle pbi-cli pin differs from settings")
    if pins.microsoft_validator_version != settings.powerbi.microsoft_validator_version:
        blockers.append("ProjectBundle validator pin differs from settings")
    if not pins.pbir_schema_versions:
        blockers.append("PBIR schema versions are not frozen")
    if not pins.desktop_version:
        blockers.append("Power BI Desktop version is not frozen")
    if not pins.windows_build or pins.windows_build != environment.operating_system_version:
        blockers.append("Windows build does not match the frozen ProjectBundle")
    if not pins.python_version or pins.python_version != environment.python_version:
        blockers.append("Python version does not match the frozen ProjectBundle")
    node_probe = environment.tools.get("node")
    if not _contains_version(node_probe.version if node_probe else None, pins.node_version):
        blockers.append("Node.js version does not match the frozen ProjectBundle")
    ollama_probe = environment.tools.get("ollama")
    if not _contains_version(
        ollama_probe.version if ollama_probe else None,
        pins.ollama_version,
    ):
        blockers.append("Ollama version does not match the frozen ProjectBundle")
    desktop_probe = environment.tools.get("powerbi-desktop")
    if not _contains_version(
        desktop_probe.version if desktop_probe else None,
        pins.desktop_version,
    ):
        blockers.append("Power BI Desktop version does not match the frozen ProjectBundle")
    if settings.model.name != pins.model_tag:
        blockers.append("Configured model tag differs from the ProjectBundle")
    if (
        settings.model.selected_digest is None
        or pins.model_digest is None
        or settings.model.selected_digest != pins.model_digest
    ):
        blockers.append("Local model digest is not frozen or does not match")
    if blockers:
        raise RuntimeError("Real Power BI mode is blocked: " + "; ".join(dict.fromkeys(blockers)))


def _validate_bundle_paths_and_hashes(
    bundle: ProjectBundle,
    *,
    transaction_state_path: Path,
) -> None:
    project_root = bundle.pbip_path.parent.resolve(strict=True)
    for path in (
        bundle.pbip_path,
        bundle.report_path,
        bundle.semantic_model_path,
        bundle.duckdb_path,
        bundle.catalog_path,
    ):
        resolved = path.resolve(strict=True)
        if _is_link_like(path) or any(_is_link_like(parent) for parent in path.parents):
            raise RuntimeError(f"ProjectBundle path cannot use symbolic links: {path}")
        if path in {bundle.report_path, bundle.semantic_model_path} and not resolved.is_dir():
            raise RuntimeError(f"ProjectBundle directory is missing: {path}")
    if not bundle.report_path.resolve().is_relative_to(project_root):
        raise RuntimeError("ProjectBundle report must be inside the PBIP project")
    if not bundle.semantic_model_path.resolve().is_relative_to(project_root):
        raise RuntimeError("ProjectBundle semantic model must be inside the PBIP project")
    validate_pbip_entrypoint(bundle.pbip_path, bundle.report_path)
    validate_report_semantic_reference(bundle.report_path, bundle.semantic_model_path)
    if bundle.pbip_hash is None or sha256_file(bundle.pbip_path) != bundle.pbip_hash:
        raise RuntimeError("prepared PBIP hash does not match the ProjectBundle")
    if bundle.duckdb_hash is None or sha256_file(bundle.duckdb_path) != bundle.duckdb_hash:
        raise RuntimeError("prepared DuckDB hash does not match the ProjectBundle")
    if bundle.catalog_hash is None or sha256_file(bundle.catalog_path) != bundle.catalog_hash:
        raise RuntimeError("prepared semantic catalog hash does not match the ProjectBundle")
    _validate_hydrated_source_inventory(bundle, project_root)
    baseline_hash = bundle.baseline_report_hash or bundle.report_hash
    if baseline_hash is None or bundle.report_hash != baseline_hash:
        raise RuntimeError("ProjectBundle baseline report hash is invalid")
    verify_validated_report_head(
        bundle.report_path,
        transaction_state_path,
        baseline_hash,
    )
    if (
        bundle.semantic_model_hash is None
        or hash_tree(bundle.semantic_model_path) != bundle.semantic_model_hash
    ):
        raise RuntimeError("prepared semantic model hash does not match the ProjectBundle")
    _validate_phase0_content_addresses(bundle, project_root.parent)


def _assert_write_authority(
    bundle: ProjectBundle,
    *,
    transaction_state_path: Path,
    reveal_marker_path: Path,
) -> None:
    """Re-prove bundle identity and Desktop exclusion at every write boundary."""

    _validate_bundle_paths_and_hashes(
        bundle,
        transaction_state_path=transaction_state_path,
    )
    assert_powerbi_write_allowed(reveal_marker_path)


def _is_link_like(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{label} is missing or invalid") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return value


def _assert_executable_identity(identity: _ExecutableIdentity) -> None:
    if (
        not identity.path.is_absolute()
        or not identity.path.is_file()
        or sha256_file(identity.path) != identity.sha256
    ):
        raise RuntimeError(f"attested executable identity changed: {identity.path}")


def _executable_identity_from_record(
    record: object,
    *,
    label: str,
) -> _ExecutableIdentity:
    if not isinstance(record, dict):
        raise RuntimeError(f"attested {label} executable record is invalid")
    raw_path = record.get("path")
    digest = record.get("sha256")
    if (
        not isinstance(raw_path, str)
        or not Path(raw_path).is_absolute()
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        raise RuntimeError(f"attested {label} executable identity is invalid")
    identity = _ExecutableIdentity(Path(raw_path), digest)
    _assert_executable_identity(identity)
    return identity


def _load_attested_runtime_toolchain(
    bundle: ProjectBundle,
) -> _AttestedRuntimeToolchain:
    """Load exact executable paths from the already content-addressed environment receipt."""

    demo_root = bundle.pbip_path.parent.resolve(strict=True).parent
    attestation = _read_json_object(
        demo_root / "phase0-attestation.json",
        "Phase-0 attestation",
    )
    references = attestation.get("receipts")
    reference = references.get("environment") if isinstance(references, dict) else None
    expected_hash = bundle.phase0_receipt_hashes.get("environment")
    if not isinstance(reference, dict) or expected_hash is None:
        raise RuntimeError("Phase-0 environment receipt reference is missing")
    relative = reference.get("path")
    if not isinstance(relative, str):
        raise RuntimeError("Phase-0 environment receipt path is invalid")
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise RuntimeError("Phase-0 environment receipt path escapes the demo")
    receipt_path = (demo_root / relative_path).resolve(strict=True)
    if (
        not receipt_path.is_relative_to(demo_root)
        or sha256_file(receipt_path) != expected_hash
        or reference.get("sha256") != expected_hash
    ):
        raise RuntimeError("Phase-0 environment receipt identity changed")
    environment = _read_json_object(receipt_path, "Phase-0 environment receipt")
    tools = environment.get("tool_executables")
    if not isinstance(tools, dict):
        raise RuntimeError("Phase-0 environment tool inventory is invalid")
    desktop = environment.get("desktop_identity")
    if not isinstance(desktop, dict):
        raise RuntimeError("Phase-0 Desktop executable identity is invalid")
    desktop_record = {
        "path": desktop.get("executable"),
        "sha256": desktop.get("executable_sha256"),
    }
    return _AttestedRuntimeToolchain(
        pbi_cli=_executable_identity_from_record(tools.get("pbi_cli"), label="pbi-cli"),
        microsoft_validator=_executable_identity_from_record(
            tools.get("microsoft_validator"), label="Microsoft validator"
        ),
        node=_executable_identity_from_record(tools.get("node"), label="Node.js"),
        ollama=_executable_identity_from_record(tools.get("ollama"), label="Ollama"),
        powerbi_desktop=_executable_identity_from_record(
            desktop_record,
            label="Power BI Desktop",
        ),
    )


def _validate_hydrated_source_inventory(
    bundle: ProjectBundle,
    project_root: Path,
) -> None:
    hydrated_keys = {
        "sales_parquet",
        "driver_parquet",
        "forecast_parquet",
        "scenario_parquet",
    }
    expected_keys = hydrated_keys | {
        "forecast_backtest_parquet",
        "forecast_selection_parquet",
    }
    if set(bundle.source_hashes) != expected_keys:
        raise RuntimeError("ProjectBundle does not bind every prepared Parquet source")
    manifest = _read_json_object(project_root / "fixture-manifest.json", "fixture manifest")
    if (
        manifest.get("data_hash") != bundle.data_hash
        or manifest.get("catalog_hash") != bundle.catalog_hash
        or manifest.get("catalog_version") != bundle.catalog_version
        or manifest.get("data_through") != bundle.data_through.isoformat()
    ):
        raise RuntimeError("fixture manifest identity does not match the ProjectBundle")
    inventory = manifest.get("hydrated_sources")
    if not isinstance(inventory, dict) or set(inventory) != hydrated_keys:
        raise RuntimeError("fixture manifest does not inventory every hydrated source")
    for key in sorted(hydrated_keys):
        record = inventory.get(key)
        expected_hash = bundle.source_hashes[key]
        if not isinstance(record, dict) or record.get("sha256") != expected_hash:
            raise RuntimeError(f"hydrated source inventory is invalid: {key}")
        raw_path = record.get("path")
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            raise RuntimeError(f"hydrated source path is invalid: {key}")
        source = Path(raw_path)
        if _is_link_like(source) or any(_is_link_like(parent) for parent in source.parents):
            raise RuntimeError(f"hydrated source path cannot use links: {key}")
        if not source.is_file() or sha256_file(source) != expected_hash:
            raise RuntimeError(f"hydrated source hash does not match: {key}")
    analytical_manifest = _read_json_object(
        bundle.duckdb_path.with_name("manifest.json"),
        "analytical manifest",
    )
    analytical_files = analytical_manifest.get("files")
    if not isinstance(analytical_files, dict):
        raise RuntimeError("analytical manifest has no source inventory")
    for key in sorted(expected_keys):
        record = analytical_files.get(key)
        expected_hash = bundle.source_hashes[key]
        if not isinstance(record, dict) or record.get("sha256") != expected_hash:
            raise RuntimeError(f"analytical source inventory is invalid: {key}")
        relative = record.get("path")
        if not isinstance(relative, str):
            raise RuntimeError(f"analytical source path is invalid: {key}")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise RuntimeError(f"analytical source path escapes its root: {key}")
        source = bundle.duckdb_path.parent / relative_path
        if _is_link_like(source) or not source.is_file() or sha256_file(source) != expected_hash:
            raise RuntimeError(f"analytical source hash does not match: {key}")
    if bundle.source_data_hash != bundle.source_hashes["sales_parquet"]:
        raise RuntimeError("ProjectBundle primary source-data hash is inconsistent")


def _validate_phase0_content_addresses(bundle: ProjectBundle, demo_root: Path) -> None:
    attestation_path = demo_root / "phase0-attestation.json"
    if (
        bundle.phase0_attestation_hash is None
        or not attestation_path.is_file()
        or sha256_file(attestation_path) != bundle.phase0_attestation_hash
    ):
        raise RuntimeError("Phase-0 attestation content address does not match")
    attestation = _read_json_object(attestation_path, "Phase-0 attestation")
    references = attestation.get("receipts")
    if (
        not isinstance(references, dict)
        or set(references) != set(bundle.phase0_receipt_hashes)
        or len(references) != 7
    ):
        raise RuntimeError("Phase-0 receipt inventory does not match the ProjectBundle")
    for name, expected_hash in bundle.phase0_receipt_hashes.items():
        reference = references.get(name)
        if not isinstance(reference, dict) or reference.get("sha256") != expected_hash:
            raise RuntimeError(f"Phase-0 receipt reference does not match: {name}")
        relative = reference.get("path")
        if not isinstance(relative, str):
            raise RuntimeError(f"Phase-0 receipt path is invalid: {name}")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise RuntimeError(f"Phase-0 receipt path escapes the demo: {name}")
        receipt = (demo_root / relative_path).resolve(strict=True)
        if not receipt.is_relative_to(demo_root.resolve(strict=True)):
            raise RuntimeError(f"Phase-0 receipt path escapes the demo: {name}")
        if _is_link_like(receipt) or sha256_file(receipt) != expected_hash:
            raise RuntimeError(f"Phase-0 receipt hash does not match: {name}")


def build_real_workspace(
    bundle: ProjectBundle,
    settings: AppSettings,
    *,
    state_root: str | Path,
    environment: EnvironmentReport | None = None,
    repository_root: str | Path | None = None,
    scripted: bool = False,
) -> AppWorkspace:
    configured_source_root: str | Path | None = repository_root
    if configured_source_root is None:
        configured_source_root = os.environ.get("AI_POWERBI_PROJECT_ROOT")
    if configured_source_root is None:
        configured_source_root = Path(__file__).resolve().parents[3]
    source_root = Path(configured_source_root).resolve(strict=True)
    requested_state = Path(state_root).absolute()
    state = requested_state.resolve(strict=False)
    expected_state = (source_root / ".runtime").resolve(strict=False)
    if state != expected_state:
        raise RuntimeError("real mode state_root must be the repository-owned .runtime directory")
    if _is_link_like(requested_state) or any(
        _is_link_like(parent) for parent in requested_state.parents
    ):
        raise RuntimeError("real mode state_root cannot traverse symbolic links")
    demo_root = bundle.pbip_path.parent.resolve(strict=False).parent
    if state == demo_root or state.is_relative_to(demo_root) or demo_root.is_relative_to(state):
        raise RuntimeError("real mode state_root cannot overlap the attested demo")
    transaction_state = state / "powerbi"
    try:
        recover_interrupted_report_swap(
            source_root,
            demo_root,
            transaction_state_path=transaction_state,
            expected_bundle=bundle,
        )
    except Exception as error:
        raise RuntimeError(f"trusted startup report recovery failed: {error}") from error
    _verify_phase0_release_authority(
        bundle,
        repository_root=source_root,
        transaction_state=transaction_state,
    )
    _require_real_runtime_gate(bundle, settings, environment or collect_environment())
    _validate_bundle_paths_and_hashes(
        bundle,
        transaction_state_path=transaction_state,
    )
    catalog = load_semantic_catalog(bundle.catalog_path)
    analytics_service = AnalyticsService.from_project_bundle(
        bundle,
        timeout_seconds=settings.analytics.timeout_seconds,
    )
    analytical_planner = RuleFirstAnalysisPlanner(catalog)
    analyst = AnalyticsServiceAdapter(analytical_planner, analytics_service)

    toolchain = _load_attested_runtime_toolchain(bundle)
    pbi_cli = PbiCliAdapter(
        toolchain.pbi_cli.path,
        runner=_AttestedCommandRunner(toolchain.pbi_cli),
    )
    authoring_cli = AuthoringCliAdapter(
        toolchain.microsoft_validator.path,
        runner=_AttestedCommandRunner(
            toolchain.microsoft_validator,
            dependencies=(toolchain.node,),
        ),
    )
    validator = make_external_validator(
        pbi_cli,
        authoring_cli,
        receipt_root=state / "validator-receipts",
    )
    baseline_validation = aggregate_validation(
        validate_report(bundle.report_path, catalog=catalog),
        validator(bundle.report_path, catalog),
    )
    if not baseline_validation.passed:
        codes = ", ".join(
            issue.code for issue in (*baseline_validation.errors, *baseline_validation.warnings)
        )
        raise RuntimeError(f"prepared report baseline validation failed: {codes}")
    backend = PbiCliBackend(
        pbi_cli,
        theme_path=Path(__file__).resolve().parents[3] / "config" / EXECUTIVE_LIGHT_THEME_FILE,
    )

    project_root = bundle.pbip_path.parent.resolve()
    report_relative_path = bundle.report_path.resolve().relative_to(project_root)
    operation_lock_path = bundle.pbip_path.parent.parent / ".operation.lock"
    reveal_marker_path = bundle.pbip_path.parent.parent / ".reveal-in-progress.json"
    safe_composer = SafePowerBIComposer(
        project_root,
        report_relative_path,
        state / "powerbi",
        backend,
        external_validator=validator,
        semantic_model_relative_path=bundle.semantic_model_path.resolve().relative_to(project_root),
        write_guard=lambda: _assert_write_authority(
            bundle,
            transaction_state_path=transaction_state,
            reveal_marker_path=reveal_marker_path,
        ),
        attested_baseline_report_hash=bundle.baseline_report_hash or bundle.report_hash,
        operation_lock_path=operation_lock_path,
    )
    desktop = _build_desktop_launcher(
        bundle,
        settings,
        toolchain,
        transaction_state=transaction_state,
        operation_lock_path=operation_lock_path,
        scripted=scripted,
    )
    composer = SafeComposerAdapter(
        safe_composer,
        catalog,
        catalog_hash=bundle.catalog_hash or "",
        data_hash=bundle.data_hash,
        state_root=transaction_state,
        desktop_opener=desktop,
    )
    request_interpreter = _build_request_interpreter(
        settings,
        catalog,
        scripted=scripted,
    )
    router = RuleFirstRouter()
    report_planner = ThreePageReportPlanner(
        catalog,
        report_hash=lambda: hash_tree(bundle.report_path),
    )
    journal = JobJournal(state / "jobs.sqlite3")
    journal.recover_abandoned(older_than_seconds=300)
    orchestrator = ApplicationOrchestrator(
        router=router,
        planner=report_planner,
        analyst=analyst,
        explainer=GroundedPowerBIExplainer(
            catalog,
            report_path=bundle.report_path,
            semantic_model_path=bundle.semantic_model_path,
        ),
        composer=composer,
        journal=journal,
        interpreter=request_interpreter,
    )
    initial_prompt = "Build a three-page executive dashboard for sales and margin."
    initial_plan = report_planner.plan(initial_prompt, ConversationState(project_id=bundle.id))
    initial_active_plan = initial_plan if composer.report_plan_installed(initial_plan) else None
    restored_state = orchestrator.restore_state(
        bundle.id,
        ConversationState(project_id=bundle.id),
    )
    return AppWorkspace(
        config=WorkspaceConfig(
            project_id=bundle.id,
            project_name=bundle.name,
            data_through=bundle.data_through,
            approved_metrics=tuple(metric.name for metric in catalog.metrics),
            compatibility_label="Windows-attested",
            compatibility_detail="Approved fields mapped for the frozen Power BI baseline",
            can_open_power_bi=bundle.capabilities.can_open_desktop,
        ),
        orchestrator=orchestrator,
        initial_prompt=initial_prompt,
        initial_plan=initial_plan,
        initial_active_plan=initial_active_plan,
        initial_evidence=restored_state.last_evidence,
        initial_analysis_plan=restored_state.last_analysis_plan,
        initial_conversation=restored_state,
        initial_validated=initial_active_plan is not None or orchestrator.undo_available,
        initial_undo_available=orchestrator.undo_available,
    )


def _verify_phase0_release_authority(
    bundle: ProjectBundle,
    *,
    repository_root: Path,
    transaction_state: Path,
) -> None:
    """Run the complete shared attestation verifier for every real-mode entry."""

    workspace = bundle.pbip_path.parent.resolve(strict=False)
    if workspace.name != "workspace":
        raise RuntimeError("prepared PBIP must be inside the attested workspace directory")
    demo_root = workspace.parent
    try:
        prepared = verify_windows_demo(
            repository_root,
            demo_root,
            require_attested=True,
            transaction_state_path=transaction_state,
        )
        authoritative = ProjectBundle.model_validate_json(
            prepared.bundle_path.read_text(encoding="utf-8")
        )
        authoritative = _resolve_bundle_paths(authoritative, prepared.bundle_path.parent)
    except Exception as error:
        raise RuntimeError(f"full Phase-0 attestation verification failed: {error}") from error
    if authoritative != bundle:
        raise RuntimeError(
            "requested ProjectBundle differs from the fully verified release descriptor"
        )


def build_real_workspace_from_environment() -> AppWorkspace:
    descriptor_value = os.environ.get("AI_POWERBI_BUNDLE_PATH")
    if not descriptor_value:
        raise RuntimeError("AI_POWERBI_BUNDLE_PATH must identify the prepared project bundle")
    descriptor = Path(descriptor_value).resolve()
    bundle = ProjectBundle.model_validate_json(descriptor.read_text(encoding="utf-8"))
    bundle = _resolve_bundle_paths(bundle, descriptor.parent)
    settings_path = Path(
        os.environ.get(
            "AI_POWERBI_SETTINGS_PATH",
            str(Path(__file__).parents[3] / "config" / "defaults.yaml"),
        )
    ).resolve()
    settings = load_settings(settings_path)
    # The content-addressed, operator-accepted ProjectBundle is the release
    # authority for the selected local model. The generic source-controlled
    # defaults intentionally carry no machine-specific digest.
    settings = settings.model_copy(
        update={
            "model": settings.model.model_copy(
                update={
                    "name": bundle.compatibility.model_tag,
                    "selected_digest": bundle.compatibility.model_digest,
                }
            )
        }
    )
    repository_root_value = os.environ.get("AI_POWERBI_PROJECT_ROOT")
    repository_root = Path(repository_root_value or Path(__file__).resolve().parents[3]).resolve(
        strict=True
    )
    state_root = Path(os.environ.get("AI_POWERBI_STATE_DIR", str(repository_root / ".runtime")))
    return build_real_workspace(
        bundle,
        settings,
        state_root=state_root,
        repository_root=repository_root,
        scripted=_scripted_environment_flag(os.environ.get("AI_POWERBI_SCRIPTED")),
    )


def _resolve_bundle_paths(bundle: ProjectBundle, base: Path) -> ProjectBundle:
    updates = {}
    for field in (
        "pbip_path",
        "report_path",
        "semantic_model_path",
        "duckdb_path",
        "catalog_path",
    ):
        value = Path(getattr(bundle, field))
        updates[field] = (value if value.is_absolute() else base / value).resolve()
    return bundle.model_copy(update=updates)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the complete real local workspace before browser launch."
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="fail closed unless the complete real workspace can be assembled",
    )
    arguments = parser.parse_args(argv)
    if not arguments.preflight:
        parser.error("--preflight is required")
    workspace = build_real_workspace_from_environment()
    print(
        json.dumps(
            {
                "status": "ready",
                "project_id": workspace.config.project_id,
                "data_through": workspace.config.data_through.isoformat(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
