"""Whole-project staging with report-only promotion and conflict-safe undo."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from filelock import FileLock

from ai_powerbi_analyst.powerbi.validation import ValidationReport


class TransactionError(RuntimeError):
    """Base class for a rejected report transaction."""


class PromotionPolicyViolation(TransactionError):
    """A staged change escaped the explicitly approved report file set."""


class ConcurrentProjectChange(TransactionError):
    """The live project changed after staging began."""


class UndoConflict(TransactionError):
    """Undo would overwrite changes made after the transaction committed."""


@dataclass(frozen=True, slots=True)
class StageHandle:
    transaction_id: str
    stage_root: Path
    report_path: Path
    baseline_project_hash: str
    baseline_report_hash: str
    baseline_files: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class StageSeal:
    """Content-addressed, allowlisted snapshot of a fully validated stage."""

    transaction_id: str
    staged_project_hash: str
    staged_report_hash: str
    staged_files: tuple[tuple[str, str], ...]
    changed_project_paths: tuple[str, ...]
    report_changes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TransactionReceipt:
    transaction_id: str
    project_root: Path
    report_relative_path: Path
    before_hash: str
    after_hash: str
    changed_paths: tuple[str, ...]
    backup_path: Path | None
    validation_sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UndoReceipt:
    transaction_id: str
    replaced_hash: str
    restored_hash: str


Validator = Callable[[Path], ValidationReport]
WriteGuard = Callable[[], None]
_HASH_CHUNK_BYTES = 1024 * 1024


def _stream_file_hash(
    path: Path,
    *,
    content_sink: Callable[[bytes], object] | None = None,
) -> tuple[int, str]:
    """Hash one regular file without materializing it in process memory."""

    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if content_sink is not None:
                content_sink(before.st_size.to_bytes(8, byteorder="big"))
            while chunk := handle.read(_HASH_CHUNK_BYTES):
                total += len(chunk)
                digest.update(chunk)
                if content_sink is not None:
                    content_sink(chunk)
            after = os.fstat(handle.fileno())
    except OSError as error:
        raise PromotionPolicyViolation(f"could not hash project file: {path}") from error
    if (
        total != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise PromotionPolicyViolation(f"project file changed while hashing: {path}")
    return total, digest.hexdigest()


def hash_tree(root: Path) -> str:
    """Hash file bytes, relative paths, and empty directories deterministically."""

    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise PromotionPolicyViolation(f"tree root is not a directory: {resolved}")
    _reject_symlinks(resolved)
    digest = hashlib.sha256()
    paths = sorted(
        resolved.rglob("*"),
        key=lambda value: value.relative_to(resolved).as_posix(),
    )
    for path in paths:
        relative = path.relative_to(resolved).as_posix()
        if path.is_dir():
            digest.update(b"D\0")
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            continue
        if path.is_file():
            digest.update(b"F\0")
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            _stream_file_hash(path, content_sink=digest.update)
            continue
        raise PromotionPolicyViolation(f"unsupported filesystem object: {path}")
    return digest.hexdigest()


def _entry_manifest(root: Path) -> dict[str, str]:
    resolved = root.resolve(strict=True)
    _reject_symlinks(resolved)
    manifest: dict[str, str] = {}
    for path in sorted(resolved.rglob("*")):
        relative = path.relative_to(resolved).as_posix()
        if path.is_dir():
            manifest[relative] = "D"
        elif path.is_file():
            _, digest = _stream_file_hash(path)
            manifest[relative] = "F:" + digest
        else:
            raise PromotionPolicyViolation(f"unsupported filesystem object: {path}")
    return manifest


def _reject_symlinks(root: Path) -> None:
    if _is_link_like(root):
        raise PromotionPolicyViolation(f"symbolic link or junction is forbidden: {root}")
    for path in root.rglob("*"):
        if _is_link_like(path):
            raise PromotionPolicyViolation(f"symbolic link or junction is forbidden: {path}")


def _is_link_like(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


class ProjectTransactionManager:
    """Stage the whole PBIP project, but promote only allowlisted report files."""

    def __init__(
        self,
        project_root: Path,
        report_relative_path: Path,
        state_root: Path,
        *,
        attested_baseline_hash: str | None = None,
        operation_lock_path: Path | None = None,
        write_guard: WriteGuard | None = None,
    ) -> None:
        self.project_root = project_root.resolve(strict=True)
        if not self.project_root.is_dir():
            raise PromotionPolicyViolation("project root must be a directory")
        if report_relative_path.is_absolute():
            raise PromotionPolicyViolation("report path must be relative and inside the project")
        candidate = (self.project_root / report_relative_path).resolve(strict=False)
        if not candidate.is_relative_to(self.project_root):
            raise PromotionPolicyViolation("report path must be inside the project")
        if candidate.suffix != ".Report":
            raise PromotionPolicyViolation("report path must identify a .Report directory")
        self.report_relative_path = candidate.relative_to(self.project_root)
        self.report_path = candidate
        self.state_root = state_root.resolve(strict=False)
        if self.state_root == self.project_root or self.state_root.is_relative_to(
            self.project_root
        ):
            raise PromotionPolicyViolation("transaction state must be outside the project")
        self.attested_baseline_hash = attested_baseline_hash
        self._write_guard = write_guard
        lock_path = operation_lock_path or (self.state_root / "project.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(lock_path), timeout=5)
        if self.state_root.exists():
            with self._lock:
                self._recover_incomplete_swaps()
        if not self.report_path.is_dir():
            raise PromotionPolicyViolation(f"report directory does not exist: {self.report_path}")
        if self.attested_baseline_hash is not None:
            verify_validated_report_head(
                self.report_path,
                self.state_root,
                self.attested_baseline_hash,
            )

    def begin(self) -> StageHandle:
        self.state_root.mkdir(parents=True, exist_ok=True)
        if self.state_root.stat().st_dev != self.project_root.stat().st_dev:
            raise PromotionPolicyViolation(
                "transaction state and project must be on the same filesystem"
            )
        with self._lock:
            _reject_symlinks(self.project_root)
            transaction_id = uuid.uuid4().hex
            baseline_project_hash = hash_tree(self.project_root)
            baseline_report_hash = hash_tree(self.report_path)
            baseline_files = tuple(sorted(_entry_manifest(self.project_root).items()))
            stage_parent = self.state_root / "stages" / transaction_id
            stage_root = stage_parent / "project"
            stage_parent.mkdir(parents=True)
            shutil.copytree(self.project_root, stage_root)
            return StageHandle(
                transaction_id=transaction_id,
                stage_root=stage_root,
                report_path=stage_root / self.report_relative_path,
                baseline_project_hash=baseline_project_hash,
                baseline_report_hash=baseline_report_hash,
                baseline_files=baseline_files,
            )

    def seal(
        self,
        stage: StageHandle,
        *,
        expected_project_hash: str | None = None,
    ) -> StageSeal:
        """Capture a stable full-stage manifest after all validators have returned."""

        self._assert_stage(stage)
        staged_before = tuple(sorted(_entry_manifest(stage.stage_root).items()))
        staged_project_hash = hash_tree(stage.stage_root)
        staged_after = tuple(sorted(_entry_manifest(stage.stage_root).items()))
        if staged_before != staged_after:
            raise PromotionPolicyViolation("staged project changed while it was being sealed")
        if expected_project_hash is not None and staged_project_hash != expected_project_hash:
            raise PromotionPolicyViolation(
                "sealed staged project changed after final local validation"
            )

        changed_project_paths, report_changes = self._validate_stage_manifest(
            stage,
            dict(staged_after),
        )
        return StageSeal(
            transaction_id=stage.transaction_id,
            staged_project_hash=staged_project_hash,
            staged_report_hash=hash_tree(stage.report_path),
            staged_files=staged_after,
            changed_project_paths=changed_project_paths,
            report_changes=report_changes,
        )

    def commit(
        self,
        stage: StageHandle,
        *,
        validator: Validator,
        seal: StageSeal | None = None,
    ) -> TransactionReceipt:
        self._assert_stage(stage)
        with self._lock:
            if hash_tree(self.project_root) != stage.baseline_project_hash:
                raise ConcurrentProjectChange("live project changed after staging began")

            sealed_stage = seal or self.seal(stage)
            self._verify_stage_seal(stage, sealed_stage)

            validation = validator(stage.report_path)
            if not validation.passed:
                blocking = (*validation.errors, *validation.warnings)
                codes = ", ".join(issue.code for issue in blocking)
                raise PromotionPolicyViolation(f"validation failed before promotion: {codes}")
            self._verify_stage_seal(stage, sealed_stage)
            if hash_tree(self.project_root) != stage.baseline_project_hash:
                raise ConcurrentProjectChange(
                    "live project changed while the staged report was being validated"
                )

            before_hash = hash_tree(self.report_path)
            staged_report_hash = sealed_stage.staged_report_hash
            report_changes = sealed_stage.report_changes
            if not report_changes and before_hash == staged_report_hash:
                receipt = TransactionReceipt(
                    transaction_id=stage.transaction_id,
                    project_root=self.project_root,
                    report_relative_path=self.report_relative_path,
                    before_hash=before_hash,
                    after_hash=before_hash,
                    changed_paths=(),
                    backup_path=None,
                    validation_sources=(validation.source,),
                )
                self._write_journal(receipt, validation)
                shutil.rmtree(stage.stage_root.parent)
                return receipt

            undo_root = self.state_root / "undo" / stage.transaction_id
            backup_path = undo_root / "report"
            undo_root.mkdir(parents=True)
            shutil.copytree(self.report_path, backup_path)
            if hash_tree(backup_path) != before_hash:
                raise TransactionError("backup verification failed; promotion aborted")

            promotion_root = self.state_root / "promotion" / stage.transaction_id
            promotion_path = promotion_root / self.report_path.name
            promotion_root.mkdir(parents=True)
            # This is the final full-stage read under the operation lock. From
            # here, only the sealed report is copied, and that copy is checked
            # against the sealed report hash before it can reach the live path.
            self._verify_stage_seal(stage, sealed_stage)
            shutil.copytree(stage.report_path, promotion_path)
            if hash_tree(promotion_path) != staged_report_hash:
                raise TransactionError("promotion copy verification failed")
            if hash_tree(self.project_root) != stage.baseline_project_hash:
                raise ConcurrentProjectChange(
                    "live project changed immediately before report promotion"
                )

            swap_root = self.state_root / "swaps"
            swap_root.mkdir(parents=True, exist_ok=True)
            displaced = swap_root / f"{stage.transaction_id}.old"
            self._write_recovery_intent(stage.transaction_id, before_hash)
            self._assert_write_allowed()
            os.replace(self.report_path, displaced)
            try:
                os.replace(promotion_path, self.report_path)
            except BaseException:
                os.replace(displaced, self.report_path)
                raise

            try:
                after_hash = hash_tree(self.report_path)
                if after_hash != staged_report_hash:
                    raise TransactionError("post-promotion hash mismatch")
            except BaseException:
                failed = swap_root / f"{stage.transaction_id}.failed"
                os.replace(self.report_path, failed)
                os.replace(displaced, self.report_path)
                raise
            receipt = TransactionReceipt(
                transaction_id=stage.transaction_id,
                project_root=self.project_root,
                report_relative_path=self.report_relative_path,
                before_hash=before_hash,
                after_hash=after_hash,
                changed_paths=report_changes,
                backup_path=backup_path,
                validation_sources=(validation.source,),
            )
            try:
                self._write_journal(receipt, validation)
            except BaseException:
                failed = swap_root / f"{stage.transaction_id}.unjournaled"
                os.replace(self.report_path, failed)
                os.replace(displaced, self.report_path)
                raise
            self._mark_recovery_intent(stage.transaction_id, "committed")
            _best_effort_remove(displaced)
            _best_effort_remove(promotion_root)
            _best_effort_remove(stage.stage_root.parent)
            return receipt

    def discard(self, stage: StageHandle) -> None:
        """Best-effort removal of an unpromoted stage constrained to state_root."""

        expected_parent = (self.state_root / "stages" / stage.transaction_id).resolve(strict=False)
        if stage.stage_root.parent.resolve(strict=False) != expected_parent:
            raise PromotionPolicyViolation("stage handle is outside transaction state")
        with self._lock:
            _best_effort_remove(expected_parent)

    def undo(self, receipt: TransactionReceipt) -> UndoReceipt:
        if not _is_transaction_id(receipt.transaction_id):
            raise UndoConflict("transaction receipt identifier is invalid")
        if (
            receipt.project_root.resolve() != self.project_root
            or receipt.report_relative_path != self.report_relative_path
        ):
            raise UndoConflict("transaction receipt targets a different project")
        with self._lock:
            current_hash = hash_tree(self.report_path)
            if current_hash != receipt.after_hash:
                raise UndoConflict("report changed since promotion; undo refused")
            if receipt.backup_path is None:
                return UndoReceipt(
                    transaction_id=receipt.transaction_id,
                    replaced_hash=current_hash,
                    restored_hash=current_hash,
                )
            backup_path = receipt.backup_path.resolve(strict=True)
            if not backup_path.is_relative_to(self.state_root):
                raise UndoConflict("backup path is outside transaction state")
            if hash_tree(backup_path) != receipt.before_hash:
                raise UndoConflict("backup hash does not match the transaction receipt")

            restore_parent = self.state_root / "restore" / receipt.transaction_id
            restore_path = restore_parent / self.report_path.name
            if restore_parent.exists():
                shutil.rmtree(restore_parent)
            restore_parent.mkdir(parents=True)
            shutil.copytree(backup_path, restore_path)
            if hash_tree(restore_path) != receipt.before_hash:
                raise UndoConflict("restore copy does not match the transaction receipt")
            undo_recovery_id = uuid.uuid4().hex
            displaced = self.state_root / "swaps" / f"{undo_recovery_id}.old"
            self._write_recovery_intent(undo_recovery_id, current_hash)
            self._assert_write_allowed()
            os.replace(self.report_path, displaced)
            try:
                os.replace(restore_path, self.report_path)
            except BaseException:
                os.replace(displaced, self.report_path)
                raise
            restored_hash = hash_tree(self.report_path)
            if restored_hash != receipt.before_hash:
                failed = self.state_root / "swaps" / f"{undo_recovery_id}.undo-failed"
                os.replace(self.report_path, failed)
                os.replace(displaced, self.report_path)
                raise UndoConflict("restored report did not match its original hash")
            try:
                self._write_undo_journal(
                    receipt,
                    current_hash,
                    restored_hash,
                    entry_id=undo_recovery_id,
                )
            except BaseException:
                failed = self.state_root / "swaps" / f"{undo_recovery_id}.undo-unjournaled"
                os.replace(self.report_path, failed)
                os.replace(displaced, self.report_path)
                raise
            self._mark_recovery_intent(undo_recovery_id, "committed")
            _best_effort_remove(displaced)
            _best_effort_remove(restore_parent)
            return UndoReceipt(
                transaction_id=receipt.transaction_id,
                replaced_hash=current_hash,
                restored_hash=restored_hash,
            )

    def _assert_stage(self, stage: StageHandle) -> None:
        expected_parent = (self.state_root / "stages" / stage.transaction_id).resolve(strict=False)
        if stage.stage_root.parent.resolve(strict=False) != expected_parent:
            raise PromotionPolicyViolation("stage handle is outside transaction state")
        if stage.report_path != stage.stage_root / self.report_relative_path:
            raise PromotionPolicyViolation("stage handle report path is invalid")
        if not stage.stage_root.is_dir() or not stage.report_path.is_dir():
            raise PromotionPolicyViolation("staged project no longer exists")

    def _validate_stage_manifest(
        self,
        stage: StageHandle,
        staged: Mapping[str, str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        baseline = dict(stage.baseline_files)
        changed_project_paths = tuple(
            sorted(
                path
                for path in set(baseline).union(staged)
                if baseline.get(path) != staged.get(path)
            )
        )
        report_prefix = self.report_relative_path.as_posix() + "/"
        outside = [path for path in changed_project_paths if not path.startswith(report_prefix)]
        if outside:
            raise PromotionPolicyViolation(
                "staged changes outside the report are forbidden: " + ", ".join(outside)
            )
        report_changes = tuple(path[len(report_prefix) :] for path in changed_project_paths)
        disallowed = [
            path
            for path in report_changes
            if not _report_path_is_allowlisted(
                path,
                is_directory=(
                    staged.get(report_prefix + path) or baseline.get(report_prefix + path)
                )
                == "D",
            )
        ]
        if disallowed:
            raise PromotionPolicyViolation(
                "report changes are outside the promotion allowlist: " + ", ".join(disallowed)
            )
        return changed_project_paths, report_changes

    def _verify_stage_seal(self, stage: StageHandle, seal: StageSeal) -> None:
        if seal.transaction_id != stage.transaction_id:
            raise PromotionPolicyViolation("stage seal targets a different transaction")
        current_files = tuple(sorted(_entry_manifest(stage.stage_root).items()))
        if current_files != seal.staged_files:
            raise PromotionPolicyViolation("sealed staged project changed after validation")
        current_changed_paths, current_report_changes = self._validate_stage_manifest(
            stage,
            dict(current_files),
        )
        if (
            current_changed_paths != seal.changed_project_paths
            or current_report_changes != seal.report_changes
            or hash_tree(stage.stage_root) != seal.staged_project_hash
            or hash_tree(stage.report_path) != seal.staged_report_hash
        ):
            raise PromotionPolicyViolation("sealed staged project changed after validation")

    def _assert_write_allowed(self) -> None:
        if self._write_guard is not None:
            self._write_guard()

    def _write_journal(
        self,
        receipt: TransactionReceipt,
        validation: ValidationReport,
    ) -> None:
        journal = self.state_root / "journal" / f"{receipt.transaction_id}.json"
        payload: dict[str, object] = {
            "schema_version": "1.0.0",
            "kind": "promotion",
            "entry_id": receipt.transaction_id,
            "transaction_id": receipt.transaction_id,
            "project_root": str(receipt.project_root),
            "report_relative_path": receipt.report_relative_path.as_posix(),
            "before_hash": receipt.before_hash,
            "after_hash": receipt.after_hash,
            "changed_paths": list(receipt.changed_paths),
            "backup_path": str(receipt.backup_path) if receipt.backup_path else None,
            "validation_sources": list(receipt.validation_sources),
            "validation_receipt_hashes": list(validation.receipt_hashes),
            "validation_report_hash": _validation_report_hash(validation),
            "status": "committed",
        }
        payload.update(
            self._chain_parent(
                before_hash=receipt.before_hash,
            )
        )
        payload["entry_hash"] = _payload_hash(payload)
        _atomic_write_json(journal, payload)
        self._write_validated_head(journal.name, payload)

    def _write_recovery_intent(self, transaction_id: str, before_hash: str) -> None:
        _atomic_write_json(
            self.state_root / "recovery" / f"{transaction_id}.json",
            {
                "transaction_id": transaction_id,
                "status": "pending",
                "before_hash": before_hash,
            },
        )

    def _mark_recovery_intent(self, transaction_id: str, status: str) -> None:
        intent = self.state_root / "recovery" / f"{transaction_id}.json"
        if not intent.is_file():
            raise TransactionError("report swap recovery intent is missing")
        payload = json.loads(intent.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("transaction_id") != transaction_id:
            raise TransactionError("report swap recovery intent is invalid")
        payload["status"] = status
        _atomic_write_json(intent, payload)

    def _recover_incomplete_swaps(self) -> None:
        recovery_root = self.state_root / "recovery"
        if not recovery_root.is_dir():
            return
        for intent in sorted(recovery_root.glob("*.json")):
            try:
                payload = json.loads(intent.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise TransactionError("report swap recovery intent is unreadable") from error
            if not isinstance(payload, dict) or payload.get("status") != "pending":
                continue
            transaction_id = payload.get("transaction_id")
            before_hash = payload.get("before_hash")
            if (
                not isinstance(transaction_id, str)
                or len(transaction_id) != 32
                or any(character not in "0123456789abcdef" for character in transaction_id)
                or not isinstance(before_hash, str)
                or len(before_hash) != 64
            ):
                raise TransactionError("report swap recovery intent is invalid")
            displaced = self.state_root / "swaps" / f"{transaction_id}.old"
            if self._pending_swap_is_authorized(transaction_id):
                payload["status"] = "committed"
                _atomic_write_json(intent, payload)
                _best_effort_remove(displaced)
                continue
            if displaced.exists():
                if not displaced.is_dir() or hash_tree(displaced) != before_hash:
                    raise TransactionError("interrupted report backup failed hash verification")
                self._assert_write_allowed()
                if self.report_path.exists():
                    recovered_root = self.state_root / "recovered" / transaction_id
                    recovered_root.mkdir(parents=True, exist_ok=True)
                    failed_new = recovered_root / "uncommitted-report"
                    if failed_new.exists():
                        raise TransactionError("recovery destination already exists")
                    os.replace(self.report_path, failed_new)
                os.replace(displaced, self.report_path)
            if not self.report_path.is_dir() or hash_tree(self.report_path) != before_hash:
                raise TransactionError("could not restore the pre-transaction report")
            payload["status"] = "rolled_back"
            _atomic_write_json(intent, payload)

    def _write_undo_journal(
        self,
        receipt: TransactionReceipt,
        replaced_hash: str,
        restored_hash: str,
        *,
        entry_id: str,
    ) -> None:
        journal = self.state_root / "journal" / f"{entry_id}.undo.json"
        payload: dict[str, object] = {
            "schema_version": "1.0.0",
            "kind": "undo",
            "entry_id": entry_id,
            "transaction_id": receipt.transaction_id,
            "before_hash": replaced_hash,
            "after_hash": restored_hash,
            "replaced_hash": replaced_hash,
            "restored_hash": restored_hash,
            "changed_paths": list(receipt.changed_paths),
            "validation_sources": ["exact-content-addressed-undo"],
            "validation_receipt_hashes": [],
            "validation_report_hash": hashlib.sha256(
                f"exact-undo:{receipt.transaction_id}:{replaced_hash}:{restored_hash}".encode()
            ).hexdigest(),
            "status": "undone",
        }
        payload.update(self._chain_parent(before_hash=replaced_hash))
        payload["entry_hash"] = _payload_hash(payload)
        _atomic_write_json(journal, payload)
        self._write_validated_head(journal.name, payload)

    def _chain_parent(self, *, before_hash: str) -> dict[str, object]:
        if self.attested_baseline_hash is None:
            return {
                "baseline_report_hash": before_hash,
                "previous_entry_file": None,
                "previous_entry_hash": None,
            }
        if before_hash == self.attested_baseline_hash:
            return {
                "baseline_report_hash": self.attested_baseline_hash,
                "previous_entry_file": None,
                "previous_entry_hash": None,
            }
        # The live directory has already been atomically replaced when a
        # promotion journal is written.  Validate the parent chain against the
        # captured pre-swap hash instead of accidentally comparing the new live
        # report with its predecessor.
        _verify_validated_chain_for_hash(
            self.state_root,
            self.attested_baseline_hash,
            before_hash,
        )
        head = _load_json(self.state_root / "validated-head.json", "validated head")
        return {
            "baseline_report_hash": self.attested_baseline_hash,
            "previous_entry_file": head["entry_file"],
            "previous_entry_hash": head["entry_hash"],
        }

    def _write_validated_head(
        self,
        entry_file: str,
        entry: dict[str, object],
    ) -> None:
        if self.attested_baseline_hash is None:
            return
        _atomic_write_json(
            self.state_root / "validated-head.json",
            {
                "schema_version": "1.0.0",
                "baseline_report_hash": self.attested_baseline_hash,
                "entry_id": entry["entry_id"],
                "entry_file": entry_file,
                "entry_hash": entry["entry_hash"],
                "report_hash": entry["after_hash"],
            },
        )

    def _pending_swap_is_authorized(self, transaction_id: str) -> bool:
        if self.attested_baseline_hash is None or not self.report_path.is_dir():
            return False
        head_path = self.state_root / "validated-head.json"
        if not head_path.is_file():
            return False
        try:
            head = _load_json(head_path, "validated head")
            if head.get("entry_id") != transaction_id:
                return False
            verify_validated_report_head(
                self.report_path,
                self.state_root,
                self.attested_baseline_hash,
            )
        except (OSError, ValueError, TransactionError):
            return False
        return True


def _report_path_is_allowlisted(relative_path: str, *, is_directory: bool) -> bool:
    path = PurePosixPath(relative_path)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return False
    parts = path.parts
    if is_directory:
        if parts[0] == "StaticResources":
            return True
        return (
            parts == ("definition", "pages")
            or (len(parts) == 3 and parts[:2] == ("definition", "pages"))
            or (len(parts) == 4 and parts[:2] == ("definition", "pages") and parts[3] == "visuals")
            or (len(parts) == 5 and parts[:2] == ("definition", "pages") and parts[3] == "visuals")
        )
    exact = {
        PurePosixPath("definition/report.json"),
        PurePosixPath("definition/version.json"),
        PurePosixPath("definition/pages/pages.json"),
    }
    if path in exact:
        return True
    if len(parts) == 4 and parts[:2] == ("definition", "pages") and parts[3] == "page.json":
        return path.suffix == ".json"
    if (
        len(parts) == 6
        and parts[:2] == ("definition", "pages")
        and parts[3] == "visuals"
        and parts[5] == "visual.json"
    ):
        return path.suffix == ".json"
    return parts[0] == "StaticResources" and path.suffix.casefold() == ".json"


def verify_validated_report_head(
    report_path: Path,
    state_root: Path,
    baseline_report_hash: str,
    *,
    expected_live_hash: str | None = None,
) -> str:
    """Accept only the attested baseline or a continuous validated journal head."""

    if not _is_sha256(baseline_report_hash):
        raise TransactionError("validated-head baseline hash is invalid")
    live_hash = hash_tree(report_path)
    if expected_live_hash is not None and live_hash != expected_live_hash:
        raise TransactionError("live report changed while validating its journal chain")
    _verify_validated_chain_for_hash(state_root, baseline_report_hash, live_hash)
    return live_hash


def _verify_validated_chain_for_hash(
    state_root: Path,
    baseline_report_hash: str,
    report_hash: str,
) -> None:
    """Verify a content-addressed journal chain for a known report hash."""

    if not _is_sha256(baseline_report_hash) or not _is_sha256(report_hash):
        raise TransactionError("validated report hash is invalid")
    if report_hash == baseline_report_hash:
        return

    root = state_root.resolve(strict=False)
    head = _load_json(root / "validated-head.json", "validated head")
    if (
        head.get("schema_version") != "1.0.0"
        or head.get("baseline_report_hash") != baseline_report_hash
        or head.get("report_hash") != report_hash
        or not _is_safe_journal_name(head.get("entry_file"))
        or not _is_sha256(head.get("entry_hash"))
    ):
        raise TransactionError("validated report head does not match the live report")

    expected_after = report_hash
    expected_entry_hash = str(head["entry_hash"])
    entry_file = str(head["entry_file"])
    seen: set[str] = set()
    for _ in range(1000):
        if entry_file in seen:
            raise TransactionError("validated report journal chain contains a cycle")
        seen.add(entry_file)
        entry = _load_json(root / "journal" / entry_file, "validated journal entry")
        recorded_hash = entry.get("entry_hash")
        unsigned = dict(entry)
        unsigned.pop("entry_hash", None)
        if (
            entry.get("schema_version") != "1.0.0"
            or recorded_hash != expected_entry_hash
            or _payload_hash(unsigned) != recorded_hash
            or entry.get("after_hash") != expected_after
            or entry.get("baseline_report_hash") != baseline_report_hash
            or not _is_sha256(entry.get("before_hash"))
            or not _is_sha256(entry.get("validation_report_hash"))
            or entry.get("kind") not in {"promotion", "undo"}
            or entry.get("status") not in {"committed", "undone"}
        ):
            raise TransactionError("validated report journal entry is invalid")
        changed_paths = entry.get("changed_paths")
        if not isinstance(changed_paths, list) or not all(
            isinstance(path, str)
            and (
                _report_path_is_allowlisted(path, is_directory=False)
                or _report_path_is_allowlisted(path, is_directory=True)
            )
            for path in changed_paths
        ):
            raise TransactionError("validated report journal contains disallowed paths")
        sources = entry.get("validation_sources")
        if (
            not isinstance(sources, list)
            or not sources
            or not all(isinstance(source, str) and source for source in sources)
        ):
            raise TransactionError("validated report journal lacks validation provenance")
        receipt_hashes = entry.get("validation_receipt_hashes")
        if not isinstance(receipt_hashes, list) or not all(
            _is_sha256(receipt_hash) for receipt_hash in receipt_hashes
        ):
            raise TransactionError("validated report journal receipt hashes are invalid")
        if entry.get("kind") == "promotion" and len(receipt_hashes) < 2:
            raise TransactionError("validated report promotion lacks both raw CLI receipts")

        previous_file = entry.get("previous_entry_file")
        previous_hash = entry.get("previous_entry_hash")
        before_hash = str(entry["before_hash"])
        if previous_file is None and previous_hash is None:
            if before_hash != baseline_report_hash:
                raise TransactionError("validated report journal is not baseline-anchored")
            return
        if not _is_safe_journal_name(previous_file) or not _is_sha256(previous_hash):
            raise TransactionError("validated report journal parent link is invalid")
        expected_after = before_hash
        expected_entry_hash = str(previous_hash)
        entry_file = str(previous_file)
    raise TransactionError("validated report journal chain exceeds its safety limit")


def _validation_report_hash(validation: ValidationReport) -> str:
    payload = {
        "source": validation.source,
        "issues": [
            {
                "source": issue.source,
                "severity": issue.severity.value,
                "code": issue.code,
                "message": issue.message,
                "path": issue.path,
            }
            for issue in validation.issues
        ],
        "receipt_hashes": list(validation.receipt_hashes),
    }
    return _payload_hash(payload)


def _payload_hash(payload: Mapping[str, object]) -> str:
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _load_json(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TransactionError(f"{label} is missing or unreadable") from error
    if not isinstance(payload, dict):
        raise TransactionError(f"{label} must contain a JSON object")
    return payload


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_transaction_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_safe_journal_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and Path(value).name == value
        and value.endswith(".json")
        and ".." not in value
    )


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _best_effort_remove(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except OSError:
        # Cleanup residue is outside the live project and is safe to collect later.
        return
