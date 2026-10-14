"""The write-ahead journal and recovery of batches a crash left in flight."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from editing_support import sha, tiny_pbix

from ai_powerbi_analyst.studio import edit_journal, report_revisions
from ai_powerbi_analyst.studio.desktop_session import DesktopInstance, DesktopSessionError
from ai_powerbi_analyst.studio.edit_journal import EditJournal, recover
from ai_powerbi_analyst.studio.report_revisions import RevisionStore


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report_revisions, "RETRY_DELAY", 0.01)


@pytest.fixture
def store(tmp_path: Path) -> RevisionStore:
    root = tmp_path / "reports" / "report-1"
    root.mkdir(parents=True)
    original = root / "original.pbix"
    return RevisionStore(root, original, tiny_pbix(original, "original"))


@pytest.fixture
def journal(store: RevisionStore) -> EditJournal:
    return EditJournal(store.journal_path)


class Port:
    """Only ``find``: recovery asks which Desktop instance holds the working copy."""

    def __init__(self, instance: DesktopInstance | None = None, fail: bool = False) -> None:
        self.instance, self.fail = instance, fail
        self.asked: list[Path] = []

    def find(self, path: Path) -> DesktopInstance | None:
        self.asked.append(path)
        if self.fail:
            raise DesktopSessionError("bridge is down")
        return self.instance


def commit(store: RevisionStore, tag: str, author: str = "studio") -> report_revisions.Revision:
    staged = store.staging_path()
    digest = tiny_pbix(staged, tag)
    row = store.commit(
        staged,
        author="studio",
        summary=tag,
        operations=[],
        expected_head=store.head().id,
    )
    assert row.blob == digest
    return row


def plan(
    journal: EditJournal, batch: str, parent: str, *, kind: str = "edit", key: str = "k"
) -> None:
    journal.append(batch, "planned", key=key, kind=kind, parent=parent, ops=["set_position"])


# --- the journal file --------------------------------------------------------------------------


def test_lines_are_appended_one_json_object_each_and_fold_into_batches(
    journal: EditJournal,
) -> None:
    plan(journal, "b1", "r0000")
    journal.append("b1", "desktop_saved")
    journal.append("b1", "staged", path="staging/a.pbix", hash="a" * 64)
    journal.append("b1", "committed", revision="r0002")
    lines = journal.path.read_text("utf-8").splitlines()
    assert len(lines) == 4
    first = json.loads(lines[0])
    assert (first["v"], first["batch"], first["state"], first["key"], first["ops"]) == (
        1,
        "b1",
        "planned",
        "k",
        ["set_position"],
    )
    batch = journal.batches()["b1"]
    assert (batch.state, batch.kind, batch.parent, batch.key, batch.revision) == (
        "committed",
        "edit",
        "r0000",
        "k",
        "r0002",
    )
    assert batch.states == ["planned", "desktop_saved", "staged", "committed"]
    assert batch.staged == [{"path": "staging/a.pbix", "hash": "a" * 64}]
    assert batch.staged_hash == "a" * 64
    assert not batch.terminal and not batch.model_phase


def test_an_unknown_state_is_refused(journal: EditJournal) -> None:
    with pytest.raises(ValueError, match="Unknown batch state"):
        journal.append("b1", "exploded")
    assert not journal.path.exists()


def test_a_torn_last_line_is_ignored_and_the_next_append_starts_on_a_new_line(
    journal: EditJournal,
) -> None:
    plan(journal, "b1", "r0000")
    with journal.path.open("ab") as handle:
        handle.write(b'{"v":1,"batch":"b1","state":"stag')  # a crash in the middle of an append
    assert [r["state"] for r in journal.records()] == ["planned"]
    assert journal.batches()["b1"].state == "planned"
    journal.append("b1", "desktop_closed")
    assert [r["state"] for r in journal.records()] == ["planned", "desktop_closed"]
    assert journal.batches()["b1"].state == "desktop_closed"


def test_garbage_lines_and_foreign_records_are_skipped(journal: EditJournal) -> None:
    plan(journal, "b1", "r0000")
    with journal.path.open("ab") as handle:
        handle.write(b"\n\xff\xfe not utf8\n[1,2]\n")
        handle.write(json.dumps({"batch": "b2", "state": "nonsense"}).encode() + b"\n")
        handle.write(json.dumps({"state": "done"}).encode() + b"\n")
    assert list(journal.batches()) == ["b1"]


def test_find_key_returns_the_newest_batch_that_used_it(journal: EditJournal) -> None:
    plan(journal, "b1", "r0000", key="same")
    journal.append("b1", "failed", reason="x")
    plan(journal, "b2", "r0000", key="same")
    plan(journal, "b3", "r0000", key="other")
    found = journal.find_key("same")
    assert found is not None and found.id == "b2"
    assert journal.find_key("missing") is None


def test_open_batches_and_the_revisions_they_pin(journal: EditJournal) -> None:
    plan(journal, "done-one", "r0001")
    journal.append("done-one", "done", receipt={"revision": "r0002"})
    plan(journal, "open-one", "r0003")
    plan(journal, "failed-one", "r0004")
    journal.append("failed-one", "failed", reason="x")
    plan(journal, "rolled", "r0005")
    journal.append("rolled", "rolled_back", reason="x")
    assert [b.id for b in journal.open_batches()] == ["open-one"]
    assert journal.pinned_revisions() == {"r0003"}


def test_an_adoption_moves_the_batchs_parent_and_the_receipt_is_kept(journal: EditJournal) -> None:
    plan(journal, "b1", "r0001")
    journal.append("b1", "adopted", parent="r0002", revision="r0002")
    journal.append("b1", "done", receipt={"revision": "r0003", "summaries": ["x"]})
    batch = journal.batches()["b1"]
    assert (batch.parent, batch.terminal, batch.receipt) == (
        "r0002",
        True,
        {"revision": "r0003", "summaries": ["x"]},
    )


def test_the_receipt_keeps_what_the_edit_layer_reported_through_a_torn_line(
    journal: EditJournal,
) -> None:
    """The editor records protected, replaced, visible_in and pending model operations in the
    "done" line, so a replay after a restart can give them back (report_editor)."""
    plan(journal, "b1", "r0000")
    receipt = {
        "revision": "r0001",
        "protected": [{"visual": "v1", "what": "conditional font color on Agent ID, kept"}],
        "replaced": ["TopN blue, BottomN red"],
        "visible_in": [
            {"op": "set_conditional_format", "where": "powerbi_only", "detail": "values.fontColor"}
        ],
        "pending_model_operations": [{"op": "update_measure", "name": "Rank", "expression": "1"}],
    }
    journal.append("b1", "done", receipt=receipt)
    with journal.path.open("ab") as handle:
        handle.write(b'{"v":1,"batch":"b2","state":"pla')  # a crash in the middle of a line
    found = journal.find_key("k")
    assert found is not None and found.receipt == receipt
    assert journal.batches()["b1"].receipt == receipt


def test_model_phase_is_visible_from_the_states(journal: EditJournal) -> None:
    plan(journal, "b1", "r0000")
    journal.append("b1", "model_applied")
    assert journal.batches()["b1"].model_phase


def test_trim_drops_the_oldest_finished_batches_and_keeps_open_ones(
    journal: EditJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number in range(6):
        plan(journal, f"b{number}", "r0000", key=f"k{number}")
        journal.append(f"b{number}", "done", receipt={"revision": "r0001"})
    plan(journal, "open", "r0000", key="open")
    monkeypatch.setattr(edit_journal, "MAX_JOURNAL_BYTES", 10)
    journal.trim(keep=2)
    assert list(journal.batches()) == ["b4", "b5", "open"]
    assert [b.id for b in journal.open_batches()] == ["open"]
    journal.trim(keep=2)  # nothing more to drop
    assert list(journal.batches()) == ["b4", "b5", "open"]


def test_trim_leaves_a_small_journal_alone(journal: EditJournal) -> None:
    plan(journal, "b1", "r0000")
    before = journal.path.read_bytes()
    journal.trim(keep=0)
    assert journal.path.read_bytes() == before


# --- recovery ----------------------------------------------------------------------------------


def states(journal: EditJournal, batch: str) -> list[str]:
    return journal.batches()[batch].states


def test_nothing_in_flight_means_nothing_to_do_but_the_staging_sweep(
    store: RevisionStore, journal: EditJournal
) -> None:
    orphan = store.staging_path()
    orphan.write_bytes(b"left by a crash")
    report = recover(store.root, store, Port())
    assert report.batches == []
    assert report.removed_staging == 1
    assert not orphan.exists()


def test_a_batch_that_never_committed_is_rolled_back_and_its_staging_deleted(
    store: RevisionStore, journal: EditJournal
) -> None:
    store.materialize()
    staged = store.staging_path()
    digest = tiny_pbix(staged, "never committed")
    plan(journal, "b1", "r0000")
    journal.append("b1", "staged", path=str(staged), hash=digest)
    report = recover(store.root, store, Port())
    assert [(b.batch, b.from_state, b.action) for b in report.batches] == [
        ("b1", "staged", "rolled_back")
    ]
    assert states(journal, "b1")[-1] == "rolled_back"
    assert not staged.exists()
    assert store.head().id == "r0000"
    assert store.working_copy_state() == "placed"
    assert journal.open_batches() == []


def test_a_batch_whose_revision_was_recorded_rolls_forward(
    store: RevisionStore, journal: EditJournal
) -> None:
    plan(journal, "b1", "r0000")
    staged = store.staging_path()
    digest = tiny_pbix(staged, "committed")
    journal.append("b1", "staged", path=str(staged), hash=digest)
    row = store.commit(
        staged, author="studio", summary="Moved", operations=[], expected_head="r0000"
    )  # the crash came after this and before the journal line "committed"
    report = recover(store.root, store, Port())
    assert [(b.action, b.detail) for b in report.batches] == [
        ("rolled_forward", "recorded as r0001")
    ]
    assert sha(store.working_copy) == row.blob
    assert store.placed() == ("r0001", row.blob)
    batch = journal.batches()["b1"]
    assert batch.state == "done"
    assert batch.receipt == {"revision": "r0001", "recovered": True, "summaries": ["set_position"]}
    assert states(journal, "b1")[-2:] == ["placed", "done"]


def test_rolling_forward_while_desktop_holds_the_working_copy_leaves_the_file_alone(
    store: RevisionStore, journal: EditJournal
) -> None:
    store.materialize()
    plan(journal, "b1", "r0000")
    staged = store.staging_path()
    digest = tiny_pbix(staged, "committed")
    journal.append("b1", "staged", path=str(staged), hash=digest)
    store.commit(staged, author="studio", summary="Moved", operations=[], expected_head="r0000")
    instance = DesktopInstance(4242, 1.0, store.working_copy)
    before = sha(store.working_copy)
    report = recover(store.root, store, Port(instance))
    assert report.desktop == instance
    assert report.batches[0].action == "rolled_forward"
    assert "left as it is" in report.batches[0].detail
    assert sha(store.working_copy) == before
    assert store.working_copy_state() == "placed_stale"
    assert journal.batches()["b1"].state == "done"


def test_a_committed_batch_is_recognized_by_its_staged_hash_even_after_later_edits(
    store: RevisionStore, journal: EditJournal
) -> None:
    plan(journal, "b1", "r0000")
    staged = store.staging_path()
    digest = tiny_pbix(staged, "mine")
    journal.append("b1", "staged", path=str(staged), hash=digest)
    store.commit(staged, author="studio", summary="mine", operations=[], expected_head="r0000")
    commit(store, "someone else's later edit")
    report = recover(store.root, store, Port())
    assert report.batches[0].action == "rolled_forward"
    assert report.batches[0].detail.startswith("recorded as r0001")


def test_a_desktop_save_the_user_asked_for_is_adopted_before_the_rollback(
    store: RevisionStore, journal: EditJournal
) -> None:
    store.materialize()
    plan(journal, "b1", "r0000")
    journal.append("b1", "desktop_saved")
    journal.append("b1", "desktop_closed")
    saved = tiny_pbix(store.working_copy, "the user's work")  # saved on consent, then the crash
    report = recover(store.root, store, Port())
    assert report.batches[0].action == "rolled_back"
    head = store.head()
    assert (head.id, head.author, head.blob) == ("r0001", "desktop", saved)
    assert store.working_copy_state() == "placed"
    assert sha(store.working_copy) == saved


def test_a_model_phase_by_product_is_discarded_not_adopted(
    store: RevisionStore, journal: EditJournal
) -> None:
    store.materialize()
    plan(journal, "b1", "r0000")
    journal.append("b1", "model_applied")
    journal.append("b1", "model_saved")
    tiny_pbix(store.working_copy, "half-done model change")
    report = recover(store.root, store, Port())
    assert report.batches[0].action == "rolled_back"
    assert store.head().id == "r0000"
    assert sha(store.working_copy) == store.original_hash
    assert store.working_copy_state() == "placed"


def test_desktop_still_holding_the_report_after_a_crash_is_reported_never_touched(
    store: RevisionStore, journal: EditJournal
) -> None:
    store.materialize()
    plan(journal, "b1", "r0000")
    journal.append("b1", "model_applied")
    instance = DesktopInstance(4242, 1.0, store.working_copy)
    port = Port(instance)
    report = recover(store.root, store, port)
    assert report.desktop == instance
    assert [(b.action) for b in report.batches] == ["left_open"]
    assert port.asked == [store.working_copy]
    assert store.head().id == "r0000"
    assert journal.batches()["b1"].state == "rolled_back"
    assert journal.batches()["b1"].reason == "Power BI Desktop still has the report open"


def test_a_bridge_that_cannot_answer_counts_as_no_instance(
    store: RevisionStore, journal: EditJournal
) -> None:
    plan(journal, "b1", "r0000")
    report = recover(store.root, store, Port(fail=True))
    assert report.desktop is None
    assert report.batches[0].action == "rolled_back"


def test_recovery_without_a_desktop_port_still_works(
    store: RevisionStore, journal: EditJournal
) -> None:
    plan(journal, "b1", "r0000")
    assert recover(store.root, store, None).batches[0].action == "rolled_back"


def test_an_undo_that_was_recorded_rolls_forward(
    store: RevisionStore, journal: EditJournal
) -> None:
    commit(store, "one")
    plan(journal, "u1", "r0001", kind="undo")
    row = store.undo("r0001")
    report = recover(store.root, store, Port())
    assert report.batches[0].action == "rolled_forward"
    assert report.batches[0].detail == f"recorded as {row.id}"
    assert sha(store.working_copy) == store.original_hash


def test_an_undo_that_was_not_recorded_rolls_back(
    store: RevisionStore, journal: EditJournal
) -> None:
    commit(store, "one")
    plan(journal, "u1", "r0001", kind="undo")
    assert recover(store.root, store, Port()).batches[0].action == "rolled_back"
    assert store.head().id == "r0001"


def test_a_restore_is_matched_by_kind_and_parent(
    store: RevisionStore, journal: EditJournal
) -> None:
    commit(store, "one")
    commit(store, "two")
    plan(journal, "r1", "r0002", kind="restore")
    store.restore("r0001", expected_head="r0002")
    assert recover(store.root, store, Port()).batches[0].action == "rolled_forward"


def test_finished_batches_are_not_touched_and_several_open_ones_are_all_recovered(
    store: RevisionStore, journal: EditJournal
) -> None:
    plan(journal, "old", "r0000")
    journal.append("old", "done", receipt={"revision": "r0000"})
    plan(journal, "a", "r0000", key="a")
    plan(journal, "b", "r0000", key="b")
    report = recover(store.root, store, Port())
    assert [b.batch for b in report.batches] == ["a", "b"]
    assert journal.batches()["old"].state == "done"
    assert recover(store.root, store, Port()).batches == []


def test_recovery_survives_a_working_copy_it_cannot_reset(
    store: RevisionStore, journal: EditJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit(store, "one")
    store.materialize()
    commit(store, "two")
    plan(journal, "b1", "r0002")
    real_replace = os.replace

    def locked(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        if Path(target).name == "Report.pbix":
            error = PermissionError(13, "Access is denied")
            error.winerror = 5  # type: ignore[attr-defined,unused-ignore]
            raise error
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", locked)
    monkeypatch.setattr(report_revisions, "RETRY_ATTEMPTS", 2)
    report = recover(store.root, store, Port())
    assert report.batches[0].action == "rolled_back"
    assert "could not be reset" in report.batches[0].detail
    assert journal.batches()["b1"].state == "rolled_back"


def test_staging_orphans_of_other_batches_are_swept_after_recovery(
    store: RevisionStore, journal: EditJournal
) -> None:
    plan(journal, "b1", "r0000")
    first, second = store.staging_path(), store.staging_path()
    now = time.time()
    os.utime(first, (now, now))
    report = recover(store.root, store, Port())
    assert report.removed_staging == 2
    assert not first.exists() and not second.exists()
