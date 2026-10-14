"""Phase-0 repeatability harness with explicit Desktop confirmation semantics."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ai_powerbi_analyst.contracts import ReportChangePlan, SemanticCatalog
from ai_powerbi_analyst.powerbi.composer import ProgressCallback, SafePowerBIComposer
from ai_powerbi_analyst.powerbi.transaction import hash_tree


@dataclass(frozen=True, slots=True)
class DesktopConfirmation:
    """Evidence returned by an external Windows/Desktop verification hook."""

    reopened: bool
    evidence: str
    receipt_hash: str | None = None


@dataclass(frozen=True, slots=True)
class KillTestIteration:
    iteration: int
    before_hash: str
    promoted_hash: str | None
    rollback_hash: str | None
    validation_passed: bool
    desktop_confirmation: DesktopConfirmation | None
    mutation_recipe_hash: str | None = None
    mutation_output_hash: str | None = None
    validation_receipt_hashes: tuple[str, ...] = ()
    error: str | None = None

    @property
    def structurally_passed(self) -> bool:
        return (
            self.error is None
            and self.validation_passed
            and self.promoted_hash is not None
            and self.rollback_hash == self.before_hash
        )


@dataclass(frozen=True, slots=True)
class Phase0KillTestReport:
    requested_iterations: int
    iterations: tuple[KillTestIteration, ...]

    @property
    def structurally_passed(self) -> bool:
        return len(self.iterations) == self.requested_iterations and all(
            iteration.structurally_passed for iteration in self.iterations
        )

    @property
    def desktop_reopen_verified(self) -> bool:
        return len(self.iterations) == self.requested_iterations and all(
            iteration.desktop_confirmation is not None
            and iteration.desktop_confirmation.reopened
            and bool(iteration.desktop_confirmation.evidence.strip())
            for iteration in self.iterations
        )

    @property
    def phase_gate_passed(self) -> bool:
        return self.structurally_passed and self.desktop_reopen_verified

    @property
    def blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        if not self.structurally_passed:
            blockers.append("one or more mutation/validation/rollback iterations failed")
        if all(item.desktop_confirmation is None for item in self.iterations):
            blockers.append("explicit Desktop confirmation hook was not supplied")
        elif not self.desktop_reopen_verified:
            blockers.append("one or more Desktop reopen confirmations failed or lacked evidence")
        return tuple(blockers)


PlanFactory = Callable[[str, int], ReportChangePlan]
DesktopConfirmationHook = Callable[[Path, int], DesktopConfirmation]
RecipeRecorder = Callable[[ReportChangePlan, int], str]


class Phase0KillTestRunner:
    """Repeat build/validate/undo without inferring that Desktop rendered it."""

    def __init__(self, composer: SafePowerBIComposer, report_path: Path) -> None:
        self.composer = composer
        self.report_path = report_path.resolve(strict=True)
        if self.report_path != composer.transactions.report_path:
            raise ValueError("kill-test report path must match the composer's report")

    def run(
        self,
        *,
        iterations: int,
        plan_factory: PlanFactory,
        catalog: SemanticCatalog,
        desktop_confirmation: DesktopConfirmationHook | None = None,
        recipe_recorder: RecipeRecorder | None = None,
        progress: ProgressCallback | None = None,
    ) -> Phase0KillTestReport:
        if iterations <= 0:
            raise ValueError("iterations must be positive")
        records: list[KillTestIteration] = []
        for iteration in range(1, iterations + 1):
            before_hash = hash_tree(self.report_path)
            promoted_hash: str | None = None
            rollback_hash: str | None = None
            validation_passed = False
            confirmation: DesktopConfirmation | None = None
            recipe_hash: str | None = None
            output_hash: str | None = None
            validation_receipt_hashes: tuple[str, ...] = ()
            error: str | None = None
            result = None
            try:
                plan = plan_factory(before_hash, iteration)
                if recipe_recorder is not None:
                    recipe_hash = recipe_recorder(plan, iteration)
                else:
                    recipe_payload = json.dumps(
                        plan.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    recipe_hash = hashlib.sha256(recipe_payload).hexdigest()
                result = self.composer.apply(plan, catalog, progress=progress)
                promoted_hash = hash_tree(self.report_path)
                output_hash = promoted_hash
                validation_passed = result.validation.passed
                validation_receipt_hashes = result.validation.receipt_hashes
                if desktop_confirmation is not None:
                    confirmation = desktop_confirmation(self.report_path, iteration)
                    if confirmation.reopened and not confirmation.evidence.strip():
                        raise ValueError("successful Desktop confirmation requires evidence")
            except Exception as exc:  # noqa: BLE001 - kill-test records every failure as evidence
                error = f"{type(exc).__name__}: {exc}"
            finally:
                if result is not None:
                    try:
                        self.composer.undo(result.transaction, progress=progress)
                        rollback_hash = hash_tree(self.report_path)
                    except Exception as exc:  # noqa: BLE001 - undo failure is appended to evidence
                        undo_error = f"{type(exc).__name__}: {exc}"
                        error = f"{error}; undo {undo_error}" if error else f"undo {undo_error}"
            records.append(
                KillTestIteration(
                    iteration=iteration,
                    before_hash=before_hash,
                    promoted_hash=promoted_hash,
                    rollback_hash=rollback_hash,
                    validation_passed=validation_passed,
                    desktop_confirmation=confirmation,
                    mutation_recipe_hash=recipe_hash,
                    mutation_output_hash=output_hash,
                    validation_receipt_hashes=validation_receipt_hashes,
                    error=error,
                )
            )
        return Phase0KillTestReport(
            requested_iterations=iterations,
            iterations=tuple(records),
        )
