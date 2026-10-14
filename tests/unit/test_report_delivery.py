"""Delivery: Save a copy never overwrites, Replace original only replaces what Studio imported."""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from editing_support import sha, tiny_pbix

from ai_powerbi_analyst.studio import report_delivery, report_revisions
from ai_powerbi_analyst.studio.desktop_session import DesktopInstance, DesktopSessionError
from ai_powerbi_analyst.studio.report_delivery import (
    DeliveryError,
    delivery_state,
    pending_changes,
    replace_original,
    save_copy,
)
from ai_powerbi_analyst.studio.report_revisions import RevisionStore

NOW = datetime(2026, 9, 28, 14, 5, 9, tzinfo=UTC)
STAMP = "20260928T140509Z"


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report_revisions, "RETRY_DELAY", 0.01)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        root = tmp_path / "state" / "reports" / "report-1"
        root.mkdir(parents=True)
        self.source = tmp_path / "Downloads" / "Agents.pbix"
        self.source.parent.mkdir()
        self.original_hash = tiny_pbix(self.source, "original")
        original = root / "original.pbix"
        shutil.copyfile(self.source, original)
        self.store = RevisionStore(root, original, self.original_hash)
        self.out = tmp_path / "Exports"
        self.out.mkdir()

    def commit(self, tag: str) -> report_revisions.Revision:
        staged = self.store.staging_path()
        tiny_pbix(staged, tag)
        return self.store.commit(
            staged,
            author="studio",
            summary=tag,
            operations=[],
            expected_head=self.store.head().id,
        )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


class Port:
    def __init__(self) -> None:
        self.open: dict[Path, DesktopInstance] = {}
        self.dirty = False

    def find(self, path: Path) -> DesktopInstance | None:
        return self.open.get(Path(path).resolve())

    def status(self, instance: DesktopInstance) -> dict[str, Any]:
        return {"hasUnsavedChanges": self.dirty}

    def hold(self, path: Path) -> None:
        self.open[path.resolve()] = DesktopInstance(1, 1.0, path.resolve())


def denied(winerror: int) -> PermissionError:
    error = PermissionError(13, "Access is denied")
    error.winerror = winerror  # type: ignore[attr-defined,unused-ignore]  # only set by the OS on Windows
    return error


# --- Save a copy -------------------------------------------------------------------------------


def test_save_copy_writes_the_head_next_to_a_chosen_folder(env: Env) -> None:
    head = env.commit("edited")
    written = save_copy(env.store, env.out, name="Agents")
    assert written == env.out / "Agents (edited).pbix"
    assert sha(written) == head.blob
    assert env.source.read_bytes() != written.read_bytes()


def test_save_copy_never_overwrites_it_adds_a_number(env: Env) -> None:
    env.commit("first edit")
    first = save_copy(env.store, env.out, name="Agents")
    first_bytes = first.read_bytes()
    env.commit("second edit")
    second = save_copy(env.store, env.out, name="Agents")
    third = save_copy(env.store, env.out, name="Agents")
    assert [p.name for p in (first, second, third)] == [
        "Agents (edited).pbix",
        "Agents (edited) (2).pbix",
        "Agents (edited) (3).pbix",
    ]
    assert first.read_bytes() == first_bytes
    assert sha(second) == sha(third) == env.store.head().blob


def test_save_copy_to_a_file_name_keeps_the_name_and_numbers_a_clash(env: Env) -> None:
    env.commit("edit")
    target = env.out / "Q3 Agents.pbix"
    target.write_bytes(b"the user's own file")
    written = save_copy(env.store, target)
    assert written == env.out / "Q3 Agents (2).pbix"
    assert target.read_bytes() == b"the user's own file"
    assert sha(written) == env.store.head().blob


def test_save_copy_is_pbix_only_and_needs_an_existing_folder(env: Env) -> None:
    env.commit("edit")
    with pytest.raises(DeliveryError, match=r"\.pbix file"):
        save_copy(env.store, env.out / "report.zip")
    with pytest.raises(DeliveryError, match="does not exist"):
        save_copy(env.store, env.out / "missing" / "report.pbix")
    assert list(env.out.iterdir()) == []


def test_save_copy_cleans_the_report_name(env: Env) -> None:
    written = save_copy(env.store, env.out, name='Q3: "Agents"/2026?')
    assert written.name == "Q3_ _Agents__2026_ (edited).pbix"
    assert save_copy(env.store, env.out, name="  ...  ").name == "Report (edited).pbix"


def test_save_copy_adopts_a_desktop_save_first(env: Env) -> None:
    env.commit("edit")
    env.store.materialize()
    saved = tiny_pbix(env.store.working_copy, "saved in desktop")
    written = save_copy(env.store, env.out, name="Agents", port=Port())
    assert sha(written) == saved
    assert env.store.head().author == "desktop"


def test_save_copy_refuses_while_desktop_has_unsaved_changes(env: Env) -> None:
    env.commit("edit")
    env.store.materialize()
    port = Port()
    port.hold(env.store.working_copy)
    port.dirty = True
    with pytest.raises(DeliveryError, match="unsaved changes"):
        save_copy(env.store, env.out, name="Agents", port=port)
    assert list(env.out.iterdir()) == []
    port.dirty = False
    assert sha(save_copy(env.store, env.out, name="Agents", port=port)) == env.store.head().blob


def test_save_copy_refuses_a_head_that_is_missing_or_damaged(env: Env) -> None:
    head = env.commit("edit")
    blob = env.store.revisions_dir / f"{head.blob}.pbix"
    content = blob.read_bytes()
    blob.write_bytes(content[: len(content) // 2])
    with pytest.raises(DeliveryError, match="damaged"):
        save_copy(env.store, env.out, name="Agents")
    blob.unlink()
    with pytest.raises(DeliveryError, match="missing from Studio's history"):
        save_copy(env.store, env.out, name="Agents")
    assert list(env.out.iterdir()) == []


def test_save_copy_refuses_a_head_whose_bytes_no_longer_match_its_fingerprint(env: Env) -> None:
    head = env.commit("edit")
    blob = env.store.revisions_dir / f"{head.blob}.pbix"
    tiny_pbix(blob, "different but valid")  # a complete ZIP, not the recorded content
    with pytest.raises(DeliveryError, match="changed while it was being written"):
        save_copy(env.store, env.out, name="Agents")
    assert list(env.out.iterdir()) == []


def test_a_copy_that_fails_while_writing_leaves_no_partial_file(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.commit("edit")

    def broken(source: Path, target: Any) -> str:
        target.write(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(report_delivery, "_copy_to", broken)
    with pytest.raises(OSError, match="disk full"):
        save_copy(env.store, env.out, name="Agents")
    assert list(env.out.iterdir()) == []


def test_a_copy_that_reads_back_wrong_is_removed(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.commit("edit")
    real = report_delivery._try_hash
    monkeypatch.setattr(report_delivery, "_try_hash", lambda path: "0" * 64)
    with pytest.raises(DeliveryError, match="does not match"):
        save_copy(env.store, env.out, name="Agents")
    monkeypatch.setattr(report_delivery, "_try_hash", real)
    assert list(env.out.iterdir()) == []


def test_save_copy_gives_up_after_a_thousand_copies(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(report_delivery, "_MAX_COPIES", 3)
    for _ in range(3):
        save_copy(env.store, env.out, name="Agents")
    with pytest.raises(DeliveryError, match="already 3 copies"):
        save_copy(env.store, env.out, name="Agents")


# --- Replace original --------------------------------------------------------------------------


def test_replace_original_backs_up_writes_verifies_and_records_the_delivery(env: Env) -> None:
    head = env.commit("edited in studio")
    result = replace_original(env.store, env.source, env.original_hash, now=lambda: NOW)
    assert result.path == env.source
    assert result.backup == env.source.with_name(f"Agents.pbix.studio-backup-{STAMP}")
    assert sha(result.backup) == env.original_hash
    assert sha(env.source) == head.blob == result.hash
    assert "no Power BI Desktop signature" in result.warnings[0]
    delivered = env.store.delivered()
    assert delivered is not None and (delivered.path, delivered.hash) == (
        str(env.source),
        head.blob,
    )
    assert sorted(p.name for p in env.source.parent.iterdir()) == [
        "Agents.pbix",
        f"Agents.pbix.studio-backup-{STAMP}",
    ]


def test_replace_original_again_is_allowed_while_the_file_is_what_studio_last_wrote(
    env: Env,
) -> None:
    env.commit("first")
    replace_original(env.store, env.source, env.original_hash, now=lambda: NOW)
    second = env.commit("second")
    again = replace_original(env.store, env.source, env.original_hash, now=lambda: NOW)
    assert sha(env.source) == second.blob
    # Two backups in the same second do not collide.
    assert again.backup.name == f"Agents.pbix.studio-backup-{STAMP}-1"
    assert sha(again.backup) != env.original_hash  # it holds the first delivery


def test_replace_original_refuses_a_file_changed_after_the_import(env: Env) -> None:
    env.commit("edited in studio")
    before = env.source.read_bytes()
    users_own_work = tiny_pbix(env.source, "the user kept working in Power BI Desktop")
    with pytest.raises(DeliveryError, match="changed outside Studio"):
        replace_original(env.store, env.source, env.original_hash, now=lambda: NOW)
    assert sha(env.source) == users_own_work != sha_of(before)
    assert [p.name for p in env.source.parent.iterdir()] == ["Agents.pbix"]
    assert env.store.delivered() is None


def sha_of(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def test_a_delivery_to_another_path_does_not_authorize_this_one(env: Env, tmp_path: Path) -> None:
    head = env.commit("edited")
    env.store.record_delivery(tmp_path / "Somewhere else.pbix", head.blob)
    changed = tiny_pbix(env.source, "changed after import")
    assert changed == sha(env.source)
    # The file equals nothing Studio wrote to *this* path.
    with pytest.raises(DeliveryError, match="changed outside Studio"):
        replace_original(env.store, env.source, env.original_hash, now=lambda: NOW)


def test_replace_original_refuses_while_desktop_has_the_source_open(env: Env) -> None:
    env.commit("edited")
    port = Port()
    port.hold(env.source)
    with pytest.raises(DeliveryError, match="has Agents.pbix open"):
        replace_original(env.store, env.source, env.original_hash, port=port, now=lambda: NOW)
    assert sha(env.source) == env.original_hash
    assert [p.name for p in env.source.parent.iterdir()] == ["Agents.pbix"]


def test_replace_original_refuses_a_missing_source(env: Env) -> None:
    env.commit("edited")
    env.source.unlink()
    with pytest.raises(DeliveryError, match="no longer at"):
        replace_original(env.store, env.source, env.original_hash, now=lambda: NOW)


def test_replace_original_reports_a_source_another_program_holds_and_removes_its_temp_file(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.commit("edited")

    def locked(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        raise denied(5)

    monkeypatch.setattr(os, "replace", locked)
    monkeypatch.setattr(report_revisions, "RETRY_ATTEMPTS", 2)
    with pytest.raises(DeliveryError, match="in use by another program"):
        replace_original(env.store, env.source, env.original_hash, now=lambda: NOW)
    assert sha(env.source) == env.original_hash
    assert sorted(p.name for p in env.source.parent.iterdir()) == [
        "Agents.pbix",
        f"Agents.pbix.studio-backup-{STAMP}",
    ]
    assert env.store.delivered() is None


def test_replace_original_retries_a_brief_sharing_violation(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = env.commit("edited")
    real_replace = os.replace
    failures = {"left": 2}

    def flaky(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        if Path(target) == env.source and failures["left"]:
            failures["left"] -= 1
            raise denied(32)
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", flaky)
    replace_original(env.store, env.source, env.original_hash, now=lambda: NOW)
    assert failures["left"] == 0
    assert sha(env.source) == head.blob


def test_the_written_file_is_verified_and_the_backup_is_named_when_it_is_wrong(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.commit("edited")
    real_replace = os.replace

    def corrupting(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        real_replace(source, target)
        Path(target).write_bytes(b"truncated by something else")

    monkeypatch.setattr(os, "replace", corrupting)
    with pytest.raises(DeliveryError, match=rf"studio-backup-{STAMP}"):
        replace_original(env.store, env.source, env.original_hash, now=lambda: NOW)
    assert env.store.delivered() is None


def test_a_source_that_changes_while_the_backup_is_made_aborts_the_replace(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.commit("edited")
    real = report_delivery._copy_to

    def racing(source: Path, target: Any) -> str:
        if source == env.source:
            tiny_pbix(env.source, "changed during the backup")
        return real(source, target)

    monkeypatch.setattr(report_delivery, "_copy_to", racing)
    with pytest.raises(DeliveryError, match="changed while Studio was preparing the backup"):
        replace_original(env.store, env.source, env.original_hash, now=lambda: NOW)
    assert [p.name for p in env.source.parent.iterdir()] == ["Agents.pbix"]


def test_replace_original_adopts_a_desktop_save_and_refuses_when_desktop_is_dirty(env: Env) -> None:
    env.commit("edited")
    env.store.materialize()
    saved = tiny_pbix(env.store.working_copy, "saved in desktop")
    port = Port()
    port.hold(env.store.working_copy)
    port.dirty = True
    with pytest.raises(DeliveryError, match="unsaved changes"):
        replace_original(env.store, env.source, env.original_hash, port=port, now=lambda: NOW)
    port.dirty = False
    replace_original(env.store, env.source, env.original_hash, port=port, now=lambda: NOW)
    assert sha(env.source) == saved


# --- the toolbar's state -----------------------------------------------------------------------


def test_delivery_state_and_pending_changes_follow_the_edits(env: Env) -> None:
    assert delivery_state(env.store, env.source) == "in_sync"
    assert pending_changes(env.store, env.source) == 0
    env.commit("one")
    env.commit("two")
    assert delivery_state(env.store, env.source) == "edited"
    assert pending_changes(env.store, env.source) == 2
    replace_original(env.store, env.source, env.original_hash, now=lambda: NOW)
    assert delivery_state(env.store, env.source) == "in_sync"
    assert pending_changes(env.store, env.source) == 0
    env.commit("three")
    assert delivery_state(env.store, env.source) == "edited"
    assert pending_changes(env.store, env.source) == 1


def test_undoing_back_to_the_files_content_is_in_sync(env: Env) -> None:
    env.commit("one")
    env.store.undo("r0001")
    assert delivery_state(env.store, env.source) == "in_sync"
    assert pending_changes(env.store, env.source) == 0


def test_a_file_changed_elsewhere_or_gone_is_source_changed(env: Env) -> None:
    env.commit("one")
    env.commit("two")
    tiny_pbix(env.source, "elsewhere")
    assert delivery_state(env.store, env.source) == "source_changed"
    assert pending_changes(env.store, env.source) == 2
    env.source.unlink()
    assert delivery_state(env.store, env.source) == "source_changed"
    assert pending_changes(env.store, env.source) == 2


class FailingPort(Port):
    """A bridge that cannot answer (timeout) or two windows that hold the same file."""

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def find(self, path: Path) -> DesktopInstance | None:
        raise DesktopSessionError(self.message)


def test_a_desktop_that_cannot_be_asked_refuses_save_a_copy_in_plain_words(env: Env) -> None:
    env.commit("edit")
    port = FailingPort("Power BI Desktop did not answer in time.")
    with pytest.raises(DeliveryError, match="did not answer in time"):
        save_copy(env.store, env.out, name="Agents", port=port)
    assert list(env.out.iterdir()) == []


def test_a_desktop_that_cannot_be_asked_refuses_replace_original_and_keeps_the_file(
    env: Env,
) -> None:
    env.commit("edit")
    before = sha(env.source)
    port = FailingPort("This report is open in more than one Power BI Desktop window.")
    with pytest.raises(DeliveryError, match="more than one Power BI Desktop window"):
        replace_original(env.store, env.source, env.original_hash, port=port, now=lambda: NOW)
    assert sha(env.source) == before
    assert sorted(p.name for p in env.source.parent.iterdir()) == ["Agents.pbix"]


def test_a_status_call_that_fails_is_a_refusal_not_a_raw_error(env: Env) -> None:
    env.commit("edit")
    env.store.materialize()

    class BrokenStatus(Port):
        def status(self, instance: DesktopInstance) -> dict[str, Any]:
            raise DesktopSessionError("Power BI Desktop stopped responding.")

    port = BrokenStatus()
    port.hold(env.store.working_copy)
    with pytest.raises(DeliveryError, match="stopped responding"):
        save_copy(env.store, env.out, name="Agents", port=port)
