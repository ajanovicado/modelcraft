"""The revision store: history, placement, adoption, pruning and Windows file semantics."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from editing_support import make_zip, patch_diff, sha, tiny_pbix

from ai_powerbi_analyst import integrity
from ai_powerbi_analyst.studio import report_revisions
from ai_powerbi_analyst.studio.edit_journal import EditJournal
from ai_powerbi_analyst.studio.report_revisions import (
    ORIGINAL_ID,
    RevisionBusy,
    RevisionConflict,
    RevisionError,
    RevisionIndexError,
    RevisionStore,
    UserChangesPending,
    WorkingCopyBusy,
    history_with_flags,
)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report_revisions, "RETRY_DELAY", 0.01)


@pytest.fixture
def store(tmp_path: Path) -> RevisionStore:
    root = tmp_path / "reports" / "report-1"
    root.mkdir(parents=True)
    original = root / "original.pbix"
    digest = tiny_pbix(original, "original")
    return RevisionStore(root, original, digest)


def make_commit(
    store: RevisionStore, tag: str, *, expected: str | None = None, **extra: Any
) -> report_revisions.Revision:
    staged = store.staging_path()
    tiny_pbix(staged, tag)
    return store.commit(
        staged,
        author="studio",
        summary=f"change {tag}",
        operations=[{"op": "set_position", "tag": tag}],
        expected_head=expected or store.head().id,
        **extra,
    )


def desktop_save(store: RevisionStore, tag: str) -> str:
    """The user saves in Desktop: the working copy gets content Studio never wrote."""
    return tiny_pbix(store.working_copy, tag)


def denied(winerror: int) -> PermissionError:
    error = PermissionError(13, "Access is denied")
    error.winerror = winerror  # type: ignore[attr-defined,unused-ignore]  # only set by the OS on Windows
    return error


# --- commit, undo, restore ---------------------------------------------------------------------


def test_a_new_store_has_only_the_original(store: RevisionStore) -> None:
    head = store.head()
    assert (head.id, head.author, head.parent, head.blob) == (
        ORIGINAL_ID,
        "import",
        None,
        store.original_hash,
    )
    assert [r.id for r in store.history()] == [ORIGINAL_ID]
    assert store.path(head) == store.original
    assert not store.index_path.exists()


def test_commit_stores_a_content_addressed_file_and_moves_the_head(store: RevisionStore) -> None:
    staged = store.staging_path()
    digest = tiny_pbix(staged, "one")
    row = store.commit(
        staged,
        author="studio",
        summary="Moved the slicer",
        operations=[{"op": "set_position", "x": 50}],
        expected_head=ORIGINAL_ID,
        desktop_version="2.157.1354.0",
    )
    assert (row.id, row.parent, row.author, row.blob) == ("r0001", ORIGINAL_ID, "studio", digest)
    assert row.operations == [{"op": "set_position", "x": 50}]
    assert row.desktop_version == "2.157.1354.0"
    assert row.size == (store.revisions_dir / f"{digest}.pbix").stat().st_size
    assert not staged.exists()
    assert store.head().id == "r0001"
    assert [r.id for r in store.history()] == ["r0001", ORIGINAL_ID]
    reloaded = RevisionStore(store.root, store.original, store.original_hash)
    assert reloaded.head() == row


def test_commit_with_a_stale_head_conflicts_and_keeps_the_staged_file(store: RevisionStore) -> None:
    make_commit(store, "one")
    staged = store.staging_path()
    tiny_pbix(staged, "two")
    with pytest.raises(RevisionConflict) as caught:
        store.commit(staged, author="studio", summary="x", operations=[], expected_head=ORIGINAL_ID)
    assert (caught.value.expected, caught.value.actual) == (ORIGINAL_ID, "r0001")
    assert staged.exists()
    assert store.head().id == "r0001"


def test_commit_only_takes_files_from_staging(store: RevisionStore, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere.pbix"
    tiny_pbix(outside, "x")
    with pytest.raises(RevisionError, match="staging folder"):
        store.commit(outside, author="studio", summary="x", operations=[], expected_head="r0000")
    not_a_zip = store.staging_path()
    not_a_zip.write_bytes(b"plain text")
    with pytest.raises(RevisionError, match="not a PBIX package"):
        store.commit(not_a_zip, author="studio", summary="x", operations=[], expected_head="r0000")
    with pytest.raises(RevisionError, match="Studio or Desktop"):
        store.commit(
            store.staging_path(),
            author="undo",  # type: ignore[arg-type]  # the point of the test
            summary="x",
            operations=[],
            expected_head="r0000",
        )


def test_operations_must_be_plain_json(store: RevisionStore) -> None:
    staged = store.staging_path()
    tiny_pbix(staged, "x")
    with pytest.raises(RevisionError, match="plain JSON"):
        store.commit(
            staged,
            author="studio",
            summary="x",
            operations=[{"v": object()}],
            expected_head="r0000",
        )


def test_committing_the_originals_bytes_adds_a_row_but_no_second_file(store: RevisionStore) -> None:
    staged = store.staging_path()
    shutil.copyfile(store.original, staged)
    row = store.commit(
        staged, author="studio", summary="same", operations=[], expected_head=ORIGINAL_ID
    )
    assert row.blob == store.original_hash
    assert store.path(row) == store.original
    assert list(store.revisions_dir.glob("*.pbix")) == []


def test_undo_reverts_the_newest_change_and_a_second_undo_goes_further_back(
    store: RevisionStore,
) -> None:
    one, two = make_commit(store, "one"), make_commit(store, "two")
    first = store.undo(two.id)
    assert (first.id, first.author, first.blob, first.restores) == (
        "r0003",
        "undo",
        one.blob,
        "r0001",
    )
    assert first.parent == "r0002"
    second = store.undo(first.id)
    assert (second.id, second.blob, second.restores) == ("r0004", store.original_hash, ORIGINAL_ID)
    with pytest.raises(RevisionError, match="nothing to undo"):
        store.undo(second.id)
    assert [r.id for r in store.history()] == ["r0004", "r0003", "r0002", "r0001", "r0000"]


def test_undo_needs_the_current_head(store: RevisionStore) -> None:
    make_commit(store, "one")
    make_commit(store, "two")
    with pytest.raises(RevisionConflict):
        store.undo("r0001")


def test_restore_makes_an_older_version_the_head_as_a_new_row(store: RevisionStore) -> None:
    one = make_commit(store, "one")
    make_commit(store, "two")
    row = store.restore("r0001", expected_head="r0002")
    assert (row.id, row.author, row.blob, row.restores) == ("r0003", "restore", one.blob, "r0001")
    assert row.summary == "Restored to r0001: change one"
    with pytest.raises(RevisionError, match="already the current version"):
        store.restore("r0003", expected_head="r0003")
    with pytest.raises(RevisionError, match="no version 'r0099'"):
        store.restore("r0099", expected_head="r0003")


def test_restore_refuses_a_version_whose_file_was_pruned(store: RevisionStore) -> None:
    one = make_commit(store, "one")
    make_commit(store, "two")
    make_commit(store, "three")
    (store.revisions_dir / f"{one.blob}.pbix").unlink()
    with pytest.raises(RevisionError, match="no longer kept on disk"):
        store.restore("r0001", expected_head="r0003")


def test_later_changes_lists_what_a_restore_leaves_out_including_desktop_saves(
    store: RevisionStore,
) -> None:
    make_commit(store, "one")
    store.materialize()
    desktop_save(store, "desktop")
    adopted = store.adopt_working_copy()
    assert adopted is not None and adopted.id == "r0002"
    make_commit(store, "three")
    later = store.later_changes("r0001")
    assert [(r.id, r.author) for r in later] == [("r0003", "studio"), ("r0002", "desktop")]
    assert store.later_changes("r0003") == []
    # An undone change is no longer in effect, so restoring past it leaves nothing out.
    undo = store.undo("r0003")
    assert [r.id for r in store.later_changes("r0002")] == []
    assert undo.restores == "r0002"
    assert [r.id for r in store.later_changes(ORIGINAL_ID)] == ["r0002", "r0001"]


def test_history_flags_show_undo_only_on_the_head(store: RevisionStore) -> None:
    one = make_commit(store, "one")
    make_commit(store, "two")
    entries = history_with_flags(store)
    assert [(e.revision.id, e.is_head, e.can_undo, e.available) for e in entries] == [
        ("r0002", True, True, True),
        ("r0001", False, False, True),
        ("r0000", False, False, True),
    ]
    (store.revisions_dir / f"{one.blob}.pbix").unlink()
    assert [e.available for e in history_with_flags(store)] == [True, False, True]
    # The head cannot be undone when the version it goes back to is gone.
    assert [e.can_undo for e in history_with_flags(store)] == [False, False, False]


def test_history_flags_mark_the_placed_revision(store: RevisionStore) -> None:
    make_commit(store, "one")
    store.materialize()
    make_commit(store, "two")
    assert [(e.revision.id, e.is_placed) for e in store.history_with_flags()] == [
        ("r0002", False),
        ("r0001", True),
        ("r0000", False),
    ]
    only_original = RevisionStore(store.root, store.original, store.original_hash)
    assert only_original.head().id == "r0002"


# --- placement, materialize, adoption ----------------------------------------------------------


def test_materialize_writes_the_head_and_records_the_placement(store: RevisionStore) -> None:
    row = make_commit(store, "one")
    assert store.working_copy_state() == "absent"
    assert store.materialize() is True
    assert sha(store.working_copy) == row.blob
    assert store.placed() == ("r0001", row.blob)
    assert store.materialize() is False
    assert store.working_copy_state() == "placed"


def test_materialize_refuses_to_overwrite_a_desktop_save(store: RevisionStore) -> None:
    make_commit(store, "one")
    store.materialize()
    saved = desktop_save(store, "user")
    with pytest.raises(UserChangesPending):
        store.materialize()
    assert sha(store.working_copy) == saved
    assert store.working_copy_state() == "user_changes"


def test_materialize_force_replaces_a_by_product_and_places_the_head(store: RevisionStore) -> None:
    row = make_commit(store, "one")
    store.materialize()
    desktop_save(store, "by-product")
    assert store.materialize(force=True) is True
    assert sha(store.working_copy) == row.blob
    assert store.working_copy_state() == "placed"


def test_materialize_leaves_desktops_normalization_of_the_head_alone(store: RevisionStore) -> None:
    make_commit(store, "one")
    store.materialize()
    normalized = desktop_save(store, "one-normalized")
    assert store.adopt_working_copy(equivalent=lambda placed, saved: True) is None
    assert store.placed() == ("r0001", normalized)
    assert store.materialize() is False
    assert sha(store.working_copy) == normalized
    assert store.working_copy_state() == "placed"


def test_a_desktop_save_becomes_a_revision_whose_parent_is_the_placed_one(
    store: RevisionStore,
) -> None:
    head = make_commit(store, "one")
    store.materialize()
    saved = desktop_save(store, "user")
    row = store.adopt_working_copy(desktop_version="2.157.1354.0")
    assert row is not None
    assert (row.id, row.author, row.parent, row.blob, row.conflict) == (
        "r0002",
        "desktop",
        head.id,
        saved,
        False,
    )
    assert (row.summary, row.desktop_version) == (
        "Changes saved in Power BI Desktop",
        "2.157.1354.0",
    )
    assert store.head() == row
    assert store.placed() == ("r0002", saved)
    assert store.adopt_working_copy() is None
    assert store.working_copy_state() == "placed"


def test_a_desktop_save_made_on_an_older_version_is_flagged_as_a_conflict(
    store: RevisionStore,
) -> None:
    make_commit(store, "one")
    store.materialize()
    make_commit(store, "two")  # the head moved while the working copy still holds r0001
    assert store.working_copy_state() == "placed_stale"
    saved = desktop_save(store, "user")
    row = store.adopt_working_copy()
    assert row is not None
    assert (row.parent, row.conflict, row.blob) == ("r0001", True, saved)
    assert store.head().id == "r0003"


def test_a_working_copy_matching_an_older_revision_is_studios_own(store: RevisionStore) -> None:
    make_commit(store, "one")
    store.materialize()
    make_commit(store, "two")
    assert store.working_copy_state() == "placed_stale"
    assert store.adopt_working_copy() is None
    assert store.materialize() is True
    assert store.working_copy_state() == "placed"


def test_adoption_of_a_normalization_of_the_placed_file_makes_no_revision(
    store: RevisionStore,
) -> None:
    make_commit(store, "one")
    store.materialize()
    saved = desktop_save(store, "same-report-other-bytes")
    seen: list[tuple[str, str]] = []

    def equivalent(base: Path, candidate: Path) -> bool:
        seen.append((sha(base), sha(candidate)))
        return True

    assert store.working_copy_state(equivalent) == "placed"
    assert store.adopt_working_copy(equivalent=equivalent) is None
    assert [r.id for r in store.history()] == ["r0001", "r0000"]
    assert store.placed() == ("r0001", saved)
    assert seen[-1] == (store.head().blob, saved)


def test_a_working_copy_from_before_placements_was_recorded_is_adopted_not_overwritten(
    store: RevisionStore,
) -> None:
    store.working_copy.parent.mkdir(parents=True)
    saved = tiny_pbix(store.working_copy, "legacy-desktop-save")
    assert store.placed() is None
    with pytest.raises(UserChangesPending):
        store.materialize()
    row = store.adopt_working_copy()
    assert row is not None
    assert (row.parent, row.author, row.blob, row.conflict) == (
        ORIGINAL_ID,
        "desktop",
        saved,
        False,
    )
    assert sha(store.working_copy) == saved


def test_a_legacy_working_copy_identical_to_the_original_is_just_placed(
    store: RevisionStore,
) -> None:
    store.working_copy.parent.mkdir(parents=True)
    shutil.copyfile(store.original, store.working_copy)
    assert store.adopt_working_copy() is None
    assert store.placed() == (ORIGINAL_ID, store.original_hash)


def test_adopting_an_incomplete_save_is_refused_and_leaves_no_staging_file(
    store: RevisionStore,
) -> None:
    store.working_copy.parent.mkdir(parents=True)
    tiny_pbix(store.working_copy, "whole")
    whole = store.working_copy.read_bytes()
    store.working_copy.write_bytes(whole[: len(whole) // 2])
    with pytest.raises(RevisionError, match="not a complete report"):
        store.adopt_working_copy()
    assert list(store.staging_dir.iterdir()) == []
    assert store.head().id == ORIGINAL_ID


def test_stage_working_copy_returns_a_verified_copy_without_recording_it(
    store: RevisionStore,
) -> None:
    store.materialize()
    saved = desktop_save(store, "saved")
    staged, digest = store.stage_working_copy()
    assert digest == saved == sha(staged)
    assert staged.parent == store.staging_dir
    assert store.head().id == ORIGINAL_ID


def test_working_copy_state_recognizes_normalization_through_the_equivalence_hook(
    store: RevisionStore,
) -> None:
    store.materialize()
    desktop_save(store, "other-bytes")
    assert store.working_copy_state() == "user_changes"
    assert store.working_copy_state(lambda a, b: True) == "placed"
    assert store.working_copy_state(lambda a, b: False) == "user_changes"


def test_pbix_diff_bridge_defaults_to_different_and_no_drops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(report_revisions, "_pbix_diff", lambda: None)
    a, b = tmp_path / "a.pbix", tmp_path / "b.pbix"
    tiny_pbix(a, "a")
    shutil.copyfile(a, b)
    assert report_revisions.semantic_equal(a, b) is False
    assert report_revisions.survival(a, b) == []


def test_pbix_diff_bridge_uses_the_module_when_it_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Diff:
        empty = True

    class Module:
        @staticmethod
        def semantic_diff(a: Path, b: Path) -> Diff:
            return Diff()

        @staticmethod
        def survival(a: Path, b: Path, baseline: Path | None = None) -> list[str]:
            return ["visual x lost its color" + ("" if baseline is None else " (edit only)")]

    monkeypatch.setattr(report_revisions, "_pbix_diff", lambda: Module)
    assert report_revisions.semantic_equal(tmp_path, tmp_path) is True
    assert report_revisions.survival(tmp_path, tmp_path) == ["visual x lost its color"]
    assert report_revisions.survival(tmp_path, tmp_path, tmp_path) == [
        "visual x lost its color (edit only)"
    ]


# --- pruning -----------------------------------------------------------------------------------


def blobs(store: RevisionStore) -> set[str]:
    return {p.stem for p in store.revisions_dir.glob("*.pbix")}


def test_prune_keeps_the_newest_distinct_contents_and_never_the_heads_or_placed_file(
    store: RevisionStore,
) -> None:
    rows = [make_commit(store, f"v{n}") for n in range(1, 6)]
    store.materialize()  # placed = r0005
    removed = store.prune(keep=2)
    assert blobs(store) == {rows[4].blob, rows[3].blob}
    assert sorted(removed) == sorted(r.blob for r in rows[:3])
    assert store.original.is_file()
    assert sha(store.working_copy) == rows[4].blob


def test_prune_never_deletes_the_head_or_placed_even_when_keep_is_one(store: RevisionStore) -> None:
    rows = [make_commit(store, f"v{n}") for n in range(1, 4)]
    store.materialize()  # placed r0003
    make_commit(store, "v4")  # head r0004 while the working copy holds r0003
    store.prune(keep=1)
    assert blobs(store) == {rows[2].blob, store.head().blob}
    assert store.path(store.head()).is_file()
    assert store.placed() is not None
    placed_revision = store.revision(store.placed()[0])  # type: ignore[index]
    assert store.path(placed_revision).is_file()


def test_undo_ping_pong_does_not_duplicate_files_or_push_real_history_out(
    store: RevisionStore,
) -> None:
    one = make_commit(store, "one")
    two = make_commit(store, "two")
    head = two
    for _ in range(6):
        undone = store.undo(store.head().id)
        assert undone.blob == one.blob
        head = store.restore("r0002", expected_head=undone.id)
        assert head.blob == two.blob
    assert len(store.history()) == 3 + 12
    assert blobs(store) == {one.blob, two.blob}
    store.prune(keep=2)
    assert blobs(store) == {one.blob, two.blob}
    assert store.available(store.revision("r0001")) and store.available(store.revision("r0002"))


def test_prune_keeps_the_parents_of_batches_still_in_flight(store: RevisionStore) -> None:
    rows = [make_commit(store, f"v{n}") for n in range(1, 5)]
    EditJournal(store.journal_path).append(
        "batch-1", "planned", key="k", kind="edit", parent="r0001", ops=["set_position"]
    )
    store.prune(keep=1)
    assert blobs(store) == {rows[0].blob, rows[3].blob}
    EditJournal(store.journal_path).append("batch-1", "rolled_back", reason="test")
    store.prune(keep=1)
    assert blobs(store) == {rows[3].blob}


def test_prune_honours_explicit_pins_by_id_or_hash_and_a_byte_budget(store: RevisionStore) -> None:
    rows = [make_commit(store, f"v{n}") for n in range(1, 5)]
    store.prune(keep=1, pinned=["r0002", rows[0].blob, "r0099", "not-a-pin"])
    assert blobs(store) == {rows[0].blob, rows[1].blob, rows[3].blob}
    # A byte budget drops what is not pinned, oldest last; pins and the head survive it.
    store.prune(keep=10, max_bytes=1, pinned=[rows[0].blob])
    assert blobs(store) == {rows[0].blob, rows[3].blob}
    store.prune(keep=10)
    assert blobs(store) == {rows[0].blob, rows[3].blob}
    store.prune(keep=1)
    assert blobs(store) == {rows[3].blob}


def test_prune_leaves_staging_files_of_a_running_batch_alone(store: RevisionStore) -> None:
    make_commit(store, "one")
    in_flight = store.staging_path()
    tiny_pbix(in_flight, "in-flight")
    store.prune(keep=1)
    assert in_flight.is_file()


def test_prune_skips_a_file_that_stays_locked_and_removes_it_next_time(
    store: RevisionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [make_commit(store, f"v{n}") for n in range(1, 4)]
    locked = store.revisions_dir / f"{rows[0].blob}.pbix"
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == locked:
            raise denied(32)
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    removed = store.prune(keep=1)
    assert rows[0].blob not in removed and rows[1].blob in removed
    assert locked.is_file()
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert store.prune(keep=1) == [rows[0].blob]


def test_prune_needs_a_positive_keep(store: RevisionStore) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        store.prune(keep=0)


def test_cleanup_removes_only_old_leftovers(store: RevisionStore) -> None:
    make_commit(store, "one")
    old = store.staging_path()
    fresh = store.staging_path()
    leftover = store.revisions_dir / "index.json.abc.tmp"
    leftover.write_bytes(b"{")
    ancient = time.time() - 3 * 24 * 3600
    for path in (old, leftover):
        os.utime(path, (ancient, ancient))
    assert store.cleanup() == 2
    assert not old.exists() and not leftover.exists() and fresh.exists()
    assert store.cleanup(max_age=-1) == 1


# --- a damaged index is reported, never reset --------------------------------------------------


@pytest.mark.parametrize(
    ("content", "detail"),
    [
        (b"{not json", "not a complete history file"),
        (b"\xff\xfe\x00", "not a complete history file"),
        (json.dumps({"version": 1}).encode(), "not a complete history file"),
    ],
)
def test_a_damaged_index_raises_and_is_left_untouched(
    store: RevisionStore, content: bytes, detail: str
) -> None:
    make_commit(store, "one")
    store.index_path.write_bytes(content)
    with pytest.raises(RevisionIndexError, match=detail):
        store.head()
    with pytest.raises(RevisionIndexError):
        make_commit(store, "two")
    assert store.index_path.read_bytes() == content


def rewrite_index(store: RevisionStore, change: Any) -> None:
    index = json.loads(store.index_path.read_text("utf-8"))
    change(index)
    store.index_path.write_text(json.dumps(index), "utf-8")


def test_an_index_for_another_original_or_out_of_order_rows_is_refused(
    store: RevisionStore,
) -> None:
    make_commit(store, "one")
    make_commit(store, "two")
    rewrite_index(store, lambda i: i.__setitem__("original", "0" * 64))
    with pytest.raises(RevisionIndexError, match="different original"):
        store.head()
    rewrite_index(store, lambda i: i.__setitem__("original", store.original_hash))
    rewrite_index(store, lambda i: i["revisions"].reverse())
    with pytest.raises(RevisionIndexError):
        store.head()
    rewrite_index(store, lambda i: i["revisions"].reverse())
    rewrite_index(store, lambda i: i.__setitem__("head", "r0009"))
    with pytest.raises(RevisionIndexError, match="not in the history"):
        store.head()


def test_an_index_from_before_delivery_and_desktop_versions_still_loads(
    store: RevisionStore,
) -> None:
    make_commit(store, "one")

    def strip(index: dict[str, Any]) -> None:
        index.pop("delivered", None)
        for row in index["revisions"]:
            row.pop("desktop_version", None)

    rewrite_index(store, strip)
    assert store.head().desktop_version is None
    assert store.delivered() is None


def test_a_history_larger_than_any_real_one_is_refused(
    store: RevisionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_commit(store, "one")
    monkeypatch.setattr(report_revisions, "MAX_INDEX_BYTES", 10)
    with pytest.raises(RevisionIndexError, match="larger than any real history"):
        store.head()


def test_the_originals_absence_is_reported(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    lost = RevisionStore(root, root / "original.pbix", "a" * 64)
    with pytest.raises(RevisionError, match="original of this report is missing"):
        lost.head()


def test_a_store_needs_a_real_hash(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="lowercase sha256"):
        RevisionStore(tmp_path, tmp_path / "o.pbix", "ABC")


# --- delivery bookkeeping ----------------------------------------------------------------------


def test_delivery_is_recorded_in_the_index(store: RevisionStore, tmp_path: Path) -> None:
    make_commit(store, "one")
    store.record_delivery(tmp_path / "Agents.pbix", "b" * 64)
    delivered = store.delivered()
    assert delivered is not None
    assert (delivered.path, delivered.hash) == (str(tmp_path / "Agents.pbix"), "b" * 64)
    with pytest.raises(RevisionError, match="sha256"):
        store.record_delivery(tmp_path / "x.pbix", "nope")


def test_mark_placed_checks_the_revision_and_the_hash(store: RevisionStore) -> None:
    with pytest.raises(RevisionError, match="no version"):
        store.mark_placed("r0007", "a" * 64)
    with pytest.raises(RevisionError, match="sha256"):
        store.mark_placed(ORIGINAL_ID, "short")
    store.mark_placed(ORIGINAL_ID, "a" * 64)
    assert store.placed() == (ORIGINAL_ID, "a" * 64)


# --- Windows file semantics --------------------------------------------------------------------


def test_history_writes_retry_transient_access_denied_errors(
    store: RevisionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace
    failures = {"left": 2}

    def flaky(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        if Path(target).name == "index.json" and failures["left"]:
            failures["left"] -= 1
            raise denied(5)
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", flaky)
    row = make_commit(store, "one")
    assert failures["left"] == 0
    assert store.head() == row


def test_a_sharing_violation_on_the_blob_move_is_retried_too(
    store: RevisionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace
    failures = {"left": 1}

    def flaky(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        if str(target).endswith(".pbix") and failures["left"]:
            failures["left"] -= 1
            raise denied(32)
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", flaky)
    row = make_commit(store, "one")
    assert (store.revisions_dir / f"{row.blob}.pbix").is_file()


def test_other_permission_errors_are_raised_at_once(
    store: RevisionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def broken(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        calls["n"] += 1
        raise denied(1314)

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(PermissionError):
        make_commit(store, "one")
    assert calls["n"] == 1


def test_a_lock_that_outlasts_the_retries_is_raised(
    store: RevisionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(report_revisions, "RETRY_ATTEMPTS", 3)
    calls = {"n": 0}

    def locked(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        calls["n"] += 1
        raise denied(5)

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(PermissionError):
        make_commit(store, "one")
    assert calls["n"] == 3
    assert store.head().id == ORIGINAL_ID


def test_a_reader_holding_the_index_open_only_delays_the_write(store: RevisionStore) -> None:
    make_commit(store, "one")
    reader = store.index_path.open("rb")
    release = threading.Timer(0.1, reader.close)
    release.start()
    try:
        store.mark_placed(ORIGINAL_ID, "c" * 64)
    finally:
        release.join()
        reader.close()
    assert store.placed() == (ORIGINAL_ID, "c" * 64)
    assert store.head().id == "r0001"


@pytest.mark.skipif(os.name != "nt", reason="Windows locks a file that is open")
def test_materialize_reports_a_working_copy_that_desktop_holds_open(
    store: RevisionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_commit(store, "one")
    store.materialize()
    make_commit(store, "two")
    monkeypatch.setattr(report_revisions, "RETRY_ATTEMPTS", 2)
    with (
        store.working_copy.open("rb"),
        pytest.raises(WorkingCopyBusy, match="Power BI Desktop has this report open"),
    ):
        store.materialize()
    assert store.materialize() is True


def test_the_lock_is_reentrant_and_times_out_for_a_second_holder(store: RevisionStore) -> None:
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with store.lock():
            held.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert held.wait(5)
        with pytest.raises(RevisionBusy, match="still being saved"):
            store.lock(timeout=0.2).__enter__()
    finally:
        release.set()
        thread.join()
    with store.lock(), store.lock():
        row = make_commit(store, "nested")
    assert row.id == "r0001"


def test_a_version_file_that_is_damaged_on_the_way_in_is_not_recorded(
    store: RevisionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = store.staging_path()
    digest = tiny_pbix(staged, "one")
    calls = {"n": 0}
    real = integrity.sha256_file

    def lying(path: Path, **kwargs: Any) -> str:
        calls["n"] += 1
        # 1st call hashes the staged file; 2nd is the re-hash after the move.
        return "f" * 64 if calls["n"] == 2 else real(path, **kwargs)

    monkeypatch.setattr(report_revisions, "sha256_file", lying)
    with pytest.raises(RevisionError, match="could not be stored intact"):
        store.commit(staged, author="studio", summary="x", operations=[], expected_head="r0000")
    assert not (store.revisions_dir / f"{digest}.pbix").exists()
    assert store.head().id == ORIGINAL_ID


def test_identical_content_committed_twice_shares_one_file(store: RevisionStore) -> None:
    first = make_commit(store, "one")
    same = store.staging_path()
    shutil.copyfile(store.revisions_dir / f"{first.blob}.pbix", same)
    second = store.commit(
        same, author="studio", summary="again", operations=[], expected_head="r0001"
    )
    assert second.blob == first.blob
    assert len(list(store.revisions_dir.glob("*.pbix"))) == 1
    assert not same.exists()


def test_undo_row_ids_stay_positions_so_lookup_by_id_is_exact(store: RevisionStore) -> None:
    make_commit(store, "one")
    assert store.revision("r0001").id == "r0001"
    for bad in ("r1", "x", "r0002", ""):
        with pytest.raises(RevisionError):
            store.revision(bad)


def test_make_zip_helper_builds_plain_zips(tmp_path: Path) -> None:
    digest = make_zip(tmp_path / "x.pbix", {"a": b"1"})
    assert digest == sha(tmp_path / "x.pbix")


def test_patch_diff_helper_replaces_the_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_diff(monkeypatch)
    assert report_revisions.semantic_equal.__module__ == "editing_support"


def test_undo_and_restore_rows_record_the_desktop_version_when_given(store: RevisionStore) -> None:
    make_commit(store, "one")
    make_commit(store, "two")
    restored = store.restore("r0001", expected_head="r0002", desktop_version="2.157.1354.0")
    undone = store.undo("r0003", desktop_version="2.158.0.0")
    assert (restored.author, restored.desktop_version) == ("restore", "2.157.1354.0")
    assert (undone.author, undone.desktop_version) == ("undo", "2.158.0.0")
    assert store.restore("r0001", expected_head="r0004").desktop_version is None


def test_check_restore_and_check_undo_refuse_what_the_action_would_refuse(
    store: RevisionStore,
) -> None:
    with pytest.raises(RevisionError, match="nothing to undo"):
        store.check_undo()
    one = make_commit(store, "one")
    make_commit(store, "two")
    store.check_undo()
    store.check_restore("r0001")
    with pytest.raises(RevisionError, match="r0002 is already the current version"):
        store.check_restore("r0002")
    with pytest.raises(RevisionError, match="no version 'r0099'"):
        store.check_restore("r0099")
    (store.revisions_dir / f"{one.blob}.pbix").unlink()
    with pytest.raises(RevisionError, match="Version r0001 is no longer kept on disk"):
        store.check_restore("r0001")
    assert store.head().id == "r0002"  # checks never write
