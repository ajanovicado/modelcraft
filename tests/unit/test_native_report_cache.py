"""Desktop previews are keyed by the report's head: an edit, an undo or a page reorder never
shows another version's image, and Desktop is only photographed while it holds the head."""

from __future__ import annotations

import base64
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.integrity import sha256_file
from ai_powerbi_analyst.studio import native_report as native
from ai_powerbi_analyst.studio.report_import import ImportedPage, ImportedReport
from ai_powerbi_analyst.studio.report_revisions import RevisionStore

PNG = b"\x89PNG\r\n\x1a\n"


def tiny(path: Path, tag: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Report/definition/report.json", f'{{"tag":"{tag}"}}')
        archive.writestr("DataModel", f"model-{tag}")
    return sha256_file(path)


class Case:
    """A report on disk with a revision history, and a fake Desktop bridge."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = tmp_path
        self.original = tmp_path / "original.pbix"
        digest = tiny(self.original, "original")
        self.report = ImportedReport(
            id="report-test",
            workspace_id="workspace-test",
            name="Test",
            source_path=str(tmp_path / "source.pbix"),
            archived_path=str(self.original),
            source_hash=digest,
            base_hash=digest,
            format="PBIR",
            pages=[ImportedPage(id="page-one", title="Overview"), ImportedPage(id="p2", title="B")],
        )
        self.store = RevisionStore(tmp_path, self.original, digest)
        self.engine = native.NativeReportPreview(tmp_path, "bridge", "pbi")
        self.captures: list[str] = []
        self.during_capture: Any = None
        monkeypatch.setattr(self.engine, "instance", lambda _: 123)
        monkeypatch.setattr(native, "command", self.bridge)

    def bridge(self, args: list[str], timeout: int) -> dict[str, Any]:
        page = args[2]
        self.captures.append(page)
        image = PNG + f"capture-{len(self.captures)}".encode()
        Path(args[args.index("--output") + 1]).write_bytes(image)
        if self.during_capture:
            self.during_capture()
        return {"status": "ok", "pid": 123, "pageId": page}

    def edit(self, tag: str, *, place: bool = True) -> ImportedReport:
        """Commit a revision, as the service does, and return the report at that head."""
        staged = self.store.staging_path()
        tiny(staged, tag)
        row = self.store.commit(
            staged,
            author="studio",
            summary=tag,
            operations=[],
            expected_head=self.store.head().id,
        )
        if place:
            self.store.mark_placed(row.id, row.blob)
        return self.report_at(row.blob)

    def report_at(self, blob: str) -> ImportedReport:
        if blob == self.report.source_hash:
            return self.report
        path = self.store.revisions_dir / f"{blob}.pbix"
        return self.report.model_copy(update={"revision_path": str(path), "revision_hash": blob})

    def previews(self) -> list[str]:
        folder = self.root / "previews"
        return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


@pytest.fixture
def case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Case:
    return Case(tmp_path, monkeypatch)


def image_bytes(payload: dict[str, Any]) -> bytes:
    return base64.b64decode(payload["image"].split(",", 1)[1])


# --- names -----------------------------------------------------------------------------------


def test_preview_names_are_safe_and_never_collide() -> None:
    assert (
        native.preview_name("ReportSection8454c812b60b8bb83b99")
        == "ReportSection8454c812b60b8bb83b99"
    )
    odd = native.preview_name("../..\\Windows/System32:x")
    assert re.fullmatch(r"[A-Za-z0-9_-]+", odd) and odd.startswith("_")
    assert native.preview_name("a.b") != native.preview_name("a_b")
    assert native.preview_name("a_b") == "a_b"
    assert native.preview_name("") == "page-e3b0c442"  # sha256 of the empty string
    assert len(native.preview_name("x" * 500)) <= 80 + 9


def test_a_preview_lives_under_the_heads_hash(case: Case) -> None:
    head = case.report.source_hash
    assert case.engine.preview_path(case.report, "page-one") == (
        case.root / "previews" / head[:16] / "page-one.png"
    )
    edited = case.edit("one")
    blob = edited.revision_hash
    assert blob is not None and blob != head
    assert case.engine.preview_path(edited, "page-one") == (
        case.root / "previews" / blob[:16] / "page-one.png"
    )


def test_the_page_id_is_sanitized_before_it_becomes_a_path(case: Case) -> None:
    report = case.report.model_copy(
        update={"pages": [ImportedPage(id="../../evil name", title="x")]}
    )
    payload = case.engine.page(report, "../../evil name")
    (stored,) = case.previews()
    folder, name = stored.split("/")
    assert folder == report.source_hash[:16]
    assert name == native.preview_name("../../evil name") + ".png"
    assert re.fullmatch(r"[A-Za-z0-9_-]+\.png", name) and payload["cached"] is False
    with pytest.raises(ValueError, match="does not belong"):
        case.engine.page(report, "page-one")


# --- caching -----------------------------------------------------------------------------------


def test_a_second_request_for_the_same_head_is_served_from_disk(case: Case) -> None:
    first = case.engine.page(case.report, "page-one")
    again = case.engine.page(case.report, "page-one")
    assert (first["cached"], again["cached"]) == (False, True)
    assert image_bytes(first) == image_bytes(again) == PNG + b"capture-1"
    assert case.captures == ["page-one"]
    assert first["head_hash"] == case.report.source_hash
    assert case.previews() == [f"{case.report.source_hash[:16]}/page-one.png"]
    refreshed = case.engine.page(case.report, "page-one", refresh=True)
    assert refreshed["cached"] is False and image_bytes(refreshed) == PNG + b"capture-2"


def test_pages_have_their_own_files_so_a_reorder_cannot_swap_them(case: Case) -> None:
    one = case.engine.page(case.report, "page-one")
    two = case.engine.page(case.report, "p2")
    assert image_bytes(one) != image_bytes(two)
    reordered = case.report.model_copy(update={"pages": list(reversed(case.report.pages))})
    assert image_bytes(case.engine.page(reordered, "page-one")) == image_bytes(one)
    assert image_bytes(case.engine.page(reordered, "p2")) == image_bytes(two)
    assert case.captures == ["page-one", "p2"]


def test_an_edit_never_shows_the_previous_versions_image(case: Case) -> None:
    before = case.engine.page(case.report, "page-one")
    edited = case.edit("recolored")
    after = case.engine.page(edited, "page-one")
    assert after["cached"] is False
    assert image_bytes(after) != image_bytes(before)
    assert case.captures == ["page-one", "page-one"]
    # Only the head's folder is kept.
    assert len(case.previews()) == 1
    assert case.previews()[0].startswith((edited.revision_hash or "")[:16])


def test_undo_shows_the_restored_versions_own_image_and_prunes_the_rest(case: Case) -> None:
    original_image = case.engine.page(case.report, "page-one")
    edited = case.edit("recolored")
    case.engine.page(edited, "page-one")
    undone = case.store.undo(case.store.head().id)
    assert undone.blob == case.report.source_hash
    case.store.mark_placed(undone.id, undone.blob)
    back = case.engine.page(case.report_at(undone.blob), "page-one")
    assert back["cached"] is False  # the edit's folder replaced the original's
    assert image_bytes(back) == PNG + b"capture-3"
    assert image_bytes(back) != image_bytes(original_image)
    assert len(case.previews()) == 1


def test_old_per_index_previews_are_removed(case: Case) -> None:
    legacy = case.root / "previews"
    legacy.mkdir()
    (legacy / "page-0.png").write_bytes(PNG + b"stale")
    (legacy / "notes.txt").write_text("kept")
    case.engine.page(case.report, "page-one")
    assert not (legacy / "page-0.png").exists()
    assert (legacy / "notes.txt").exists()


def test_a_damaged_cached_image_is_captured_again(case: Case) -> None:
    case.engine.page(case.report, "page-one")
    target = case.engine.preview_path(case.report, "page-one")
    target.write_bytes(b"not a png")
    again = case.engine.page(case.report, "page-one")
    assert again["cached"] is False and image_bytes(again).startswith(PNG)


# --- staleness -----------------------------------------------------------------------------------


def test_a_caller_with_an_older_head_is_refused(case: Case) -> None:
    edited = case.edit("one")
    with pytest.raises(ValueError, match="out of date"):
        case.engine.page(edited, "page-one", head_hash=case.report.source_hash)
    assert case.captures == [] and case.previews() == []
    ok = case.engine.page(edited, "page-one", head_hash=edited.revision_hash)
    assert ok["head_hash"] == edited.revision_hash


def test_desktop_is_not_photographed_while_it_holds_an_older_version(case: Case) -> None:
    edited = case.edit("one", place=False)
    with pytest.raises(ValueError, match="not showing the current version"):
        case.engine.page(edited, "page-one")
    assert case.captures == [] and case.previews() == []
    assert edited.revision_hash is not None
    case.store.mark_placed("r0001", edited.revision_hash)
    assert case.engine.page(edited, "page-one")["cached"] is False


def test_a_working_copy_of_an_earlier_revision_is_not_the_head(case: Case) -> None:
    edited = case.edit("one")
    later = case.edit("two", place=False)
    assert case.engine.head_placed(edited) is False  # the store's head moved on
    assert case.engine.head_placed(later) is False  # placed is still r0001
    assert later.revision_hash is not None
    case.store.mark_placed("r0002", later.revision_hash)
    assert case.engine.head_placed(later) is True


def test_a_report_never_edited_is_placed_without_a_history_file(case: Case) -> None:
    assert not case.store.index_path.exists()
    assert case.engine.head_placed(case.report) is True
    stale = case.report.model_copy(update={"revision_path": "x.pbix", "revision_hash": "e" * 64})
    assert case.engine.head_placed(stale) is False


def test_a_cached_image_of_the_head_is_served_without_asking_desktop(case: Case) -> None:
    case.engine.page(case.report, "page-one")
    case.captures.clear()
    # Desktop moved on to another version, but the file for this head is already on disk.
    edited = case.edit("one", place=False)
    assert edited.revision_hash is not None
    case.engine.preview_path(edited, "page-one").parent.mkdir(parents=True)
    case.engine.preview_path(edited, "page-one").write_bytes(PNG + b"from-when-it-was-placed")
    served = case.engine.page(edited, "page-one")
    assert served["cached"] is True and case.captures == []


def test_a_head_that_moves_during_the_capture_is_not_filed_under_the_new_hash(
    case: Case,
) -> None:
    edited = case.edit("one")
    case.during_capture = lambda: case.edit("two", place=False)
    with pytest.raises(ValueError, match="not showing the current version"):
        case.engine.page(edited, "page-one")
    assert case.previews() == []
    assert not list((case.root / "previews").rglob("capture-*"))


def test_native_query_refuses_an_unplaced_head_before_connecting(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    edited = case.edit("one", place=False)

    def unexpected_connection(report: ImportedReport) -> str:
        pytest.fail("An older Desktop revision must not be queried")

    monkeypatch.setattr(case.engine, "connection", unexpected_connection)
    with pytest.raises(ValueError, match="not showing the current version"):
        case.engine.query(edited, 'EVALUATE ROW("Value", 1)')
    assert case.captures == []
    assert not list(case.root.glob("native-query-*"))


@pytest.mark.parametrize("moves_head", [False, True])
def test_native_query_returns_only_the_current_placed_heads_rows(
    case: Case, monkeypatch: pytest.MonkeyPatch, moves_head: bool
) -> None:
    edited = case.edit("one")
    calls: list[list[str]] = []
    monkeypatch.setattr(case.engine, "connection", lambda _: "specific-report")

    def execute(args: list[str], timeout: int) -> dict[str, Any]:
        calls.append(args)
        assert args[:5] == ["pbi", "--json", "--connection", "specific-report", "dax"]
        query_path = Path(args[args.index("--file") + 1])
        assert query_path.read_text(encoding="utf-8") == 'EVALUATE ROW("Value", 1)'
        if moves_head:
            case.edit("two", place=False)
        return {"columns": ["Value"], "rows": [{"Value": 1}]}

    monkeypatch.setattr(native, "command", execute)
    if moves_head:
        with pytest.raises(ValueError, match="not showing the current version"):
            case.engine.query(edited, 'EVALUATE ROW("Value", 1)')
    else:
        assert case.engine.query(edited, 'EVALUATE ROW("Value", 1)')["rows"] == [{"Value": 1}]
    assert len(calls) == 1
    assert not list(case.root.glob("native-query-*"))


def test_the_working_copy_is_beside_the_original(case: Case) -> None:
    assert native.working_copy(case.report) == case.root / "desktop" / "Report.pbix"
