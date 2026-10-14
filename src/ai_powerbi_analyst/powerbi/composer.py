"""High-level safe composition boundary used by orchestration and the UI."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from typing import Literal

from ai_powerbi_analyst.contracts import ReportChangePlan, SemanticCatalog
from ai_powerbi_analyst.powerbi.backend import BackendReceipt, PowerBIBackend
from ai_powerbi_analyst.powerbi.recipes import DashboardBlueprint, RecipeCompiler
from ai_powerbi_analyst.powerbi.transaction import (
    ProjectTransactionManager,
    TransactionReceipt,
    UndoReceipt,
    hash_tree,
)
from ai_powerbi_analyst.powerbi.validation import (
    ValidationReport,
    ValidationSeverity,
    aggregate_validation,
    validate_blueprint_postconditions,
    validate_report,
    validate_semantic_catalog_tmdl,
)


class BaseReportChanged(RuntimeError):
    """The plan was created for a different report state."""


class CatalogVersionMismatch(RuntimeError):
    """The plan and active semantic catalog do not match."""


class BaselineReportInvalid(RuntimeError):
    """The live report is already unsafe to use as a transaction baseline."""


ProgressStage = Literal[
    "preflight",
    "staging",
    "building",
    "validating",
    "promoting",
    "done",
    "undo",
]


@dataclass(frozen=True, slots=True)
class ComposerProgress:
    stage: ProgressStage
    message: str
    percent: int


@dataclass(frozen=True, slots=True)
class ComposerResult:
    blueprint: DashboardBlueprint
    backend: BackendReceipt
    validation: ValidationReport
    transaction: TransactionReceipt


ProgressCallback = Callable[[ComposerProgress], None]
ExternalValidator = Callable[[Path, SemanticCatalog], ValidationReport]
WriteGuard = Callable[[], None]


class SafePowerBIComposer:
    """Compile, stage, validate, and atomically promote one closed report plan."""

    def __init__(
        self,
        project_root: Path,
        report_relative_path: Path,
        state_root: Path,
        backend: PowerBIBackend,
        external_validator: ExternalValidator | None = None,
        semantic_model_relative_path: Path | None = None,
        write_guard: WriteGuard | None = None,
        attested_baseline_report_hash: str | None = None,
        operation_lock_path: Path | None = None,
        progress_heartbeat_interval_seconds: float = 4.0,
    ) -> None:
        if (
            not math.isfinite(progress_heartbeat_interval_seconds)
            or progress_heartbeat_interval_seconds <= 0
        ):
            raise ValueError("progress heartbeat interval must be a finite positive number")
        self.write_guard = write_guard
        self.transactions = ProjectTransactionManager(
            project_root,
            report_relative_path,
            state_root,
            attested_baseline_hash=attested_baseline_report_hash,
            operation_lock_path=operation_lock_path,
            write_guard=write_guard,
        )
        self.backend = backend
        self.external_validator = external_validator
        if semantic_model_relative_path is not None and semantic_model_relative_path.is_absolute():
            raise ValueError("semantic model path must be relative to the project")
        self.semantic_model_relative_path = semantic_model_relative_path
        self.progress_heartbeat_interval_seconds = progress_heartbeat_interval_seconds
        self.compiler = RecipeCompiler()
        if backend.requires_external_validation and external_validator is None:
            raise ValueError("production Power BI backend requires an external validator")

    def apply(
        self,
        plan: ReportChangePlan,
        catalog: SemanticCatalog,
        *,
        progress: ProgressCallback | None = None,
    ) -> ComposerResult:
        self._preflight(plan.base_report_hash, plan.catalog_version, catalog, progress)
        blueprint = self.compiler.compile(plan, catalog)
        return self._apply_blueprint(
            blueprint,
            base_report_hash=plan.base_report_hash,
            catalog=catalog,
            progress=progress,
            evidence_only=False,
        )

    def apply_evidence(
        self,
        plan: ReportChangePlan,
        catalog: SemanticCatalog,
        *,
        metric_id: str,
        dimension_id: str,
        chart_kind: Literal["bar", "line", "table"],
        progress: ProgressCallback | None = None,
    ) -> ComposerResult:
        """Replace the fixed evidence slot, then run the standard safe transaction."""

        self._preflight(plan.base_report_hash, plan.catalog_version, catalog, progress)
        blueprint = self.compiler.replace_evidence_slot(
            self.compiler.compile(plan, catalog),
            catalog=catalog,
            metric_id=metric_id,
            dimension_id=dimension_id,
            chart_kind=chart_kind,
        )
        return self._apply_blueprint(
            blueprint,
            base_report_hash=plan.base_report_hash,
            catalog=catalog,
            progress=progress,
            evidence_only=True,
        )

    def _preflight(
        self,
        base_report_hash: str,
        catalog_version: str,
        catalog: SemanticCatalog,
        progress: ProgressCallback | None,
    ) -> None:
        _emit(progress, "preflight", "Checking report and catalog versions", 5)
        self._assert_write_allowed()
        live_hash = hash_tree(self.transactions.report_path)
        if base_report_hash != live_hash:
            raise BaseReportChanged(
                f"report hash changed: plan={base_report_hash}, current={live_hash}"
            )
        if catalog_version != catalog.version:
            raise CatalogVersionMismatch(
                f"catalog version changed: plan={catalog_version}, current={catalog.version}"
            )
        baseline_reports = [validate_report(self.transactions.report_path, catalog=catalog)]
        if self.semantic_model_relative_path is not None:
            baseline_reports.append(
                validate_semantic_catalog_tmdl(
                    self.transactions.project_root / self.semantic_model_relative_path,
                    catalog,
                )
            )
        baseline = aggregate_validation(*baseline_reports)
        if not baseline.passed:
            blocking = (*baseline.errors, *baseline.warnings)
            codes = ", ".join(issue.code for issue in blocking)
            raise BaselineReportInvalid(f"live report failed baseline validation: {codes}")

    def _apply_blueprint(
        self,
        blueprint: DashboardBlueprint,
        *,
        base_report_hash: str,
        catalog: SemanticCatalog,
        progress: ProgressCallback | None,
        evidence_only: bool,
    ) -> ComposerResult:
        _emit(progress, "staging", "Copying the complete PBIP project into a safe stage", 15)
        stage = self.transactions.begin()
        if stage.baseline_report_hash != base_report_hash:
            self.transactions.discard(stage)
            raise BaseReportChanged("report changed between preflight and staging")
        try:
            _emit(progress, "building", "Applying approved report recipes to the stage", 40)
            self._assert_write_allowed()

            def apply_backend() -> BackendReceipt:
                return (
                    self.backend.apply_evidence(stage.report_path, blueprint)
                    if evidence_only
                    else self.backend.apply(stage.report_path, blueprint)
                )

            backend_receipt = _run_with_progress_heartbeat(
                progress,
                stage="building",
                message="Still applying approved report recipes to the safe stage",
                percent=50,
                interval_seconds=self.progress_heartbeat_interval_seconds,
                operation=apply_backend,
            )
            if backend_receipt.blueprint_hash != blueprint.fingerprint:
                raise RuntimeError("backend receipt does not match the compiled blueprint")
            if backend_receipt.report_hash != hash_tree(stage.report_path):
                raise RuntimeError("backend receipt does not match the staged report")

            _emit(progress, "validating", "Running structural, binding, and layout checks", 70)

            def run_local_validation() -> ValidationReport:
                local_reports = [
                    validate_report(stage.report_path, catalog=catalog),
                    validate_blueprint_postconditions(stage.report_path, blueprint),
                ]
                if self.semantic_model_relative_path is not None:
                    local_reports.append(
                        validate_semantic_catalog_tmdl(
                            stage.stage_root / self.semantic_model_relative_path,
                            catalog,
                        )
                    )
                return aggregate_validation(*local_reports)

            def mutation_issue(boundary: str) -> ValidationReport:
                return ValidationReport.single(
                    source="stage-integrity",
                    severity=ValidationSeverity.ERROR,
                    code="STAGED_PROJECT_MUTATED_DURING_VALIDATION",
                    message=f"The staged project changed while {boundary} was running",
                )

            def run_validation() -> tuple[ValidationReport, str]:
                reports: list[ValidationReport] = []

                before_local_hash = hash_tree(stage.stage_root)
                reports.append(run_local_validation())
                after_local_hash = hash_tree(stage.stage_root)
                if after_local_hash != before_local_hash:
                    reports.append(mutation_issue("local validation"))

                if self.external_validator is not None:
                    before_external_hash = after_local_hash
                    reports.append(self.external_validator(stage.report_path, catalog))
                    after_external_hash = hash_tree(stage.stage_root)
                    if after_external_hash != before_external_hash:
                        reports.append(mutation_issue("external validation"))

                # External tools and their output are untrusted. Re-run every
                # local structural, semantic, and blueprint postcondition after
                # they return, then bind the result to the exact full-stage hash.
                before_final_local_hash = hash_tree(stage.stage_root)
                reports.append(run_local_validation())
                after_final_local_hash = hash_tree(stage.stage_root)
                if after_final_local_hash != before_final_local_hash:
                    reports.append(mutation_issue("final local validation"))
                return aggregate_validation(*reports), after_final_local_hash

            validation, validated_stage_hash = _run_with_progress_heartbeat(
                progress,
                stage="validating",
                message="Still validating report structure, bindings, and tool receipts",
                percent=80,
                interval_seconds=self.progress_heartbeat_interval_seconds,
                operation=run_validation,
            )
            stage_seal = self.transactions.seal(
                stage,
                expected_project_hash=validated_stage_hash,
            )
            _emit(
                progress,
                "promoting",
                "Promoting the validated report with an undo snapshot",
                90,
            )

            def promotion_validator(_report_path: Path) -> ValidationReport:
                self._assert_write_allowed()
                return validation

            transaction = self.transactions.commit(
                stage,
                validator=promotion_validator,
                seal=stage_seal,
            )
        except Exception:
            self.transactions.discard(stage)
            raise
        _emit(progress, "done", "Report changes are validated and reversible", 100)
        return ComposerResult(
            blueprint=blueprint,
            backend=backend_receipt,
            validation=validation,
            transaction=transaction,
        )

    def undo(
        self,
        transaction: TransactionReceipt,
        *,
        progress: ProgressCallback | None = None,
    ) -> UndoReceipt:
        self._assert_write_allowed()
        _emit(progress, "undo", "Restoring the exact pre-change report snapshot", 25)
        receipt = self.transactions.undo(transaction)
        _emit(progress, "done", "The prior report snapshot has been restored", 100)
        return receipt

    def _assert_write_allowed(self) -> None:
        if self.write_guard is not None:
            self.write_guard()


def _run_with_progress_heartbeat[HeartbeatResult](
    callback: ProgressCallback | None,
    *,
    stage: ProgressStage,
    message: str,
    percent: int,
    interval_seconds: float,
    operation: Callable[[], HeartbeatResult],
) -> HeartbeatResult:
    """Run blocking work off-thread while progress remains on the calling UI thread."""

    if callback is None:
        return operation()

    completed = Event()
    results: list[HeartbeatResult] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            results.append(operation())
        except BaseException as error:  # noqa: BLE001 - worker failure is re-raised on the caller thread
            failures.append(error)
        finally:
            completed.set()

    thread = Thread(
        target=run,
        name=f"ai-powerbi-operation-{stage}",
        daemon=True,
    )
    thread.start()
    while not completed.wait(interval_seconds):
        _emit(callback, stage, message, percent)
    thread.join()
    if failures:
        raise failures[0]
    if not results:
        raise RuntimeError(f"{stage} operation completed without a result")
    return results[0]


def _emit(
    callback: ProgressCallback | None,
    stage: ProgressStage,
    message: str,
    percent: int,
) -> None:
    if callback is None:
        return
    try:
        callback(ComposerProgress(stage=stage, message=message, percent=percent))
    except Exception:  # noqa: BLE001 - UI refresh or logging failures must not control filesystem safety
        return
