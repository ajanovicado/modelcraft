from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from ai_powerbi_analyst.powerbi.transaction import (
    ConcurrentProjectChange,
    ProjectTransactionManager,
    PromotionPolicyViolation,
    TransactionError,
    TransactionReceipt,
    UndoConflict,
    hash_tree,
    verify_validated_report_head,
)
from ai_powerbi_analyst.powerbi.validation import ValidationReport, ValidationSeverity


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise


def make_project(root: Path) -> tuple[Path, Path]:
    project = root / "Demo"
    report = project / "Sales.Report"
    pages = report / "definition" / "pages"
    semantic = project / "Sales.SemanticModel" / "definition"
    pages.mkdir(parents=True)
    semantic.mkdir(parents=True)
    (project / "Sales.pbip").write_text('{"version":"1.0"}\n', encoding="utf-8")
    (pages / "pages.json").write_text(
        json.dumps({"pageOrder": [], "activePageName": None}) + "\n", encoding="utf-8"
    )
    (semantic / "model.tmdl").write_text("model Model\n", encoding="utf-8")
    return project, report


def passing_validation(_: Path) -> ValidationReport:
    return ValidationReport(source="test", issues=())


def passing_external_validation(_: Path) -> ValidationReport:
    return ValidationReport(
        source="pbi-cli+microsoft-authoring-cli",
        issues=(),
        receipt_hashes=("a" * 64, "b" * 64),
    )


def test_tree_hash_streams_large_files_and_preserves_hash_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    nested = root / "nested"
    nested.mkdir(parents=True)
    payload = (b"power-bi-cache" * 200_000) + b"end"
    target = nested / "cache.abf"
    target.write_bytes(payload)
    expected = hashlib.sha256()
    expected.update(b"D\0nested\0")
    expected.update(b"F\0nested/cache.abf\0")
    expected.update(len(payload).to_bytes(8, byteorder="big"))
    expected.update(payload)
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _self: (_ for _ in ()).throw(AssertionError("read_bytes was used")),
    )

    assert hash_tree(root) == expected.hexdigest()


def test_transaction_stages_whole_project_and_promotes_only_allowlisted_report_files(
    tmp_path: Path,
) -> None:
    project, report = make_project(tmp_path)
    manager = ProjectTransactionManager(project, Path("Sales.Report"), tmp_path / "state")
    stage = manager.begin()

    assert (stage.stage_root / "Sales.SemanticModel/definition/model.tmdl").exists()
    (stage.stage_root / "Sales.SemanticModel/definition/model.tmdl").write_text(
        "model Hacked\n", encoding="utf-8"
    )
    (stage.report_path / "definition/pages/pages.json").write_text(
        json.dumps({"pageOrder": ["new"], "activePageName": "new"}), encoding="utf-8"
    )

    with pytest.raises(PromotionPolicyViolation, match="outside the report"):
        manager.commit(stage, validator=passing_validation)

    assert "Hacked" not in (project / "Sales.SemanticModel/definition/model.tmdl").read_text()
    assert json.loads((report / "definition/pages/pages.json").read_text())["pageOrder"] == []


def test_transaction_commit_and_undo_restore_exact_report_bytes(tmp_path: Path) -> None:
    project, report = make_project(tmp_path)
    original_hash = hash_tree(report)
    manager = ProjectTransactionManager(project, Path("Sales.Report"), tmp_path / "state")
    stage = manager.begin()
    changed_file = stage.report_path / "definition/pages/pages.json"
    changed_file.write_text(
        '{\n  "pageOrder": ["abc"],\n  "activePageName": "abc"\n}\n', encoding="utf-8"
    )

    receipt = manager.commit(stage, validator=passing_validation)

    assert receipt.before_hash == original_hash
    assert hash_tree(report) == receipt.after_hash
    assert receipt.before_hash != receipt.after_hash
    assert receipt.changed_paths == ("definition/pages/pages.json",)
    undo = manager.undo(receipt)
    assert undo.restored_hash == original_hash
    assert hash_tree(report) == original_hash


def test_transaction_rejects_validation_failure_without_touching_live_report(
    tmp_path: Path,
) -> None:
    project, report = make_project(tmp_path)
    original_hash = hash_tree(report)
    manager = ProjectTransactionManager(project, Path("Sales.Report"), tmp_path / "state")
    stage = manager.begin()
    (stage.report_path / "definition/pages/pages.json").write_text("{}", encoding="utf-8")

    def failing_validation(_: Path) -> ValidationReport:
        return ValidationReport.single(
            source="test",
            severity=ValidationSeverity.ERROR,
            code="INVALID",
            message="Rejected",
        )

    with pytest.raises(PromotionPolicyViolation, match="validation failed"):
        manager.commit(stage, validator=failing_validation)

    assert hash_tree(report) == original_hash


def test_transaction_aborts_when_live_project_changes_after_staging(tmp_path: Path) -> None:
    project, report = make_project(tmp_path)
    manager = ProjectTransactionManager(project, Path("Sales.Report"), tmp_path / "state")
    stage = manager.begin()
    (stage.report_path / "definition/pages/pages.json").write_text("{}", encoding="utf-8")
    (project / "Sales.pbip").write_text('{"version":"changed"}\n', encoding="utf-8")

    with pytest.raises(ConcurrentProjectChange):
        manager.commit(stage, validator=passing_validation)

    assert json.loads((report / "definition/pages/pages.json").read_text())["pageOrder"] == []


def test_transaction_rechecks_live_project_after_validator_runs(tmp_path: Path) -> None:
    project, report = make_project(tmp_path)
    manager = ProjectTransactionManager(project, Path("Sales.Report"), tmp_path / "state")
    stage = manager.begin()
    (stage.report_path / "definition/pages/pages.json").write_text("{}", encoding="utf-8")

    def validator_that_observes_external_change(_: Path) -> ValidationReport:
        (project / "Sales.pbip").write_text('{"version":"concurrent"}\n', encoding="utf-8")
        return ValidationReport(source="test", issues=())

    with pytest.raises(ConcurrentProjectChange):
        manager.commit(stage, validator=validator_that_observes_external_change)

    assert json.loads((report / "definition/pages/pages.json").read_text())["pageOrder"] == []


def test_transaction_detects_new_empty_directories_outside_report(tmp_path: Path) -> None:
    project, _ = make_project(tmp_path)
    manager = ProjectTransactionManager(project, Path("Sales.Report"), tmp_path / "state")
    stage = manager.begin()
    (stage.stage_root / "unapproved-empty-directory").mkdir()

    with pytest.raises(PromotionPolicyViolation, match="outside the report"):
        manager.commit(stage, validator=passing_validation)


def test_undo_fails_closed_if_report_changed_since_promotion(tmp_path: Path) -> None:
    project, report = make_project(tmp_path)
    manager = ProjectTransactionManager(project, Path("Sales.Report"), tmp_path / "state")
    stage = manager.begin()
    (stage.report_path / "definition/pages/pages.json").write_text("{}", encoding="utf-8")
    receipt = manager.commit(stage, validator=passing_validation)
    (report / "definition/pages/pages.json").write_text('{"user":"change"}', encoding="utf-8")

    with pytest.raises(UndoConflict, match="changed since promotion"):
        manager.undo(receipt)


def test_transaction_rejects_symlinks_and_report_path_escape(tmp_path: Path) -> None:
    project, _ = make_project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(project / "link", outside, directory=True)

    manager = ProjectTransactionManager(project, Path("Sales.Report"), tmp_path / "state")
    with pytest.raises(PromotionPolicyViolation, match="symbolic link"):
        manager.begin()

    with pytest.raises(PromotionPolicyViolation, match="inside the project"):
        ProjectTransactionManager(project, Path("../outside"), tmp_path / "other-state")


def test_startup_recovers_old_report_after_interrupted_directory_swap(
    tmp_path: Path,
) -> None:
    project, report = make_project(tmp_path)
    state = tmp_path / "state"
    before_hash = hash_tree(report)
    transaction_id = "a" * 32
    displaced = state / "swaps" / f"{transaction_id}.old"
    displaced.parent.mkdir(parents=True)
    os.replace(report, displaced)
    recovery = state / "recovery" / f"{transaction_id}.json"
    recovery.parent.mkdir(parents=True)
    recovery.write_text(
        json.dumps(
            {
                "transaction_id": transaction_id,
                "status": "pending",
                "before_hash": before_hash,
            }
        ),
        encoding="utf-8",
    )

    ProjectTransactionManager(project, Path("Sales.Report"), state)

    assert hash_tree(report) == before_hash
    assert json.loads(recovery.read_text(encoding="utf-8"))["status"] == "rolled_back"


def test_startup_recovery_checks_write_guard_under_lock_before_live_swap(
    tmp_path: Path,
) -> None:
    project, report = make_project(tmp_path)
    state = tmp_path / "state"
    before_hash = hash_tree(report)
    transaction_id = "b" * 32
    displaced = state / "swaps" / f"{transaction_id}.old"
    displaced.parent.mkdir(parents=True)
    shutil.copytree(report, displaced)
    (report / "definition/pages/pages.json").write_text(
        json.dumps({"pageOrder": ["uncommitted"], "activePageName": "uncommitted"}) + "\n",
        encoding="utf-8",
    )
    uncommitted_hash = hash_tree(report)
    recovery = state / "recovery" / f"{transaction_id}.json"
    recovery.parent.mkdir(parents=True)
    recovery.write_text(
        json.dumps(
            {
                "transaction_id": transaction_id,
                "status": "pending",
                "before_hash": before_hash,
            }
        ),
        encoding="utf-8",
    )

    def desktop_open_guard() -> None:
        raise RuntimeError("Power BI Desktop opened before recovery")

    with pytest.raises(RuntimeError, match="opened before recovery"):
        ProjectTransactionManager(
            project,
            Path("Sales.Report"),
            state,
            write_guard=desktop_open_guard,
        )

    assert hash_tree(report) == uncommitted_hash
    assert hash_tree(displaced) == before_hash
    assert json.loads(recovery.read_text(encoding="utf-8"))["status"] == "pending"

    ProjectTransactionManager(
        project,
        Path("Sales.Report"),
        state,
        write_guard=lambda: None,
    )
    assert hash_tree(report) == before_hash
    assert json.loads(recovery.read_text(encoding="utf-8"))["status"] == "rolled_back"


def test_attested_journal_chains_two_promotions_and_an_undo(tmp_path: Path) -> None:
    project, report = make_project(tmp_path)
    state = tmp_path / "state"
    baseline_hash = hash_tree(report)
    manager = ProjectTransactionManager(
        project,
        Path("Sales.Report"),
        state,
        attested_baseline_hash=baseline_hash,
    )

    first_stage = manager.begin()
    first_pages = first_stage.report_path / "definition/pages/pages.json"
    first_pages.write_text(
        json.dumps({"pageOrder": ["first"], "activePageName": "first"}) + "\n",
        encoding="utf-8",
    )
    first = manager.commit(first_stage, validator=passing_external_validation)
    assert verify_validated_report_head(report, state, baseline_hash) == first.after_hash

    second_stage = manager.begin()
    second_pages = second_stage.report_path / "definition/pages/pages.json"
    second_pages.write_text(
        json.dumps({"pageOrder": ["second"], "activePageName": "second"}) + "\n",
        encoding="utf-8",
    )
    second = manager.commit(second_stage, validator=passing_external_validation)
    assert verify_validated_report_head(report, state, baseline_hash) == second.after_hash

    manager.undo(second)
    assert verify_validated_report_head(report, state, baseline_hash) == first.after_hash


def test_validated_head_rejects_manual_report_edit(tmp_path: Path) -> None:
    project, report = make_project(tmp_path)
    state = tmp_path / "state"
    baseline_hash = hash_tree(report)
    manager = ProjectTransactionManager(
        project,
        Path("Sales.Report"),
        state,
        attested_baseline_hash=baseline_hash,
    )
    stage = manager.begin()
    (stage.report_path / "definition/pages/pages.json").write_text(
        json.dumps({"pageOrder": ["built"], "activePageName": "built"}) + "\n",
        encoding="utf-8",
    )
    manager.commit(stage, validator=passing_external_validation)
    (report / "definition/pages/pages.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(TransactionError, match="does not match the live report"):
        verify_validated_report_head(report, state, baseline_hash)


def test_undo_rejects_traversal_transaction_id_before_restore_cleanup(tmp_path: Path) -> None:
    project, report = make_project(tmp_path)
    state = tmp_path / "state"
    manager = ProjectTransactionManager(project, Path("Sales.Report"), state)
    victim = tmp_path / "victim"
    victim.mkdir()
    marker = victim / "keep.txt"
    marker.write_text("preserve", encoding="utf-8")
    backup = state / "undo" / "safe" / "report"
    backup.parent.mkdir(parents=True)
    shutil.copytree(report, backup)
    report_hash = hash_tree(report)
    receipt = TransactionReceipt(
        transaction_id="../../victim",
        project_root=project,
        report_relative_path=Path("Sales.Report"),
        before_hash=report_hash,
        after_hash=report_hash,
        changed_paths=(),
        backup_path=backup,
        validation_sources=("test",),
    )

    with pytest.raises(UndoConflict, match="identifier"):
        manager.undo(receipt)

    assert marker.read_text(encoding="utf-8") == "preserve"
