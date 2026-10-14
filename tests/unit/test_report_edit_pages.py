"""Page operations: references on duplicate and delete, size scaling, backgrounds and images.

Every test runs on the structure twin (one 1920 x 1200 page, 15 visuals, a background image) or
on multipage.pbix (four pages: bookmarks, a button that opens a page, a tooltip, a drillthrough
page, nested groups). Documents are edited in memory and read back through ``commit``; the
fixture files are never written.
"""

from __future__ import annotations

import re
import struct
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from ai_powerbi_analyst.studio import report_edit_pages
from ai_powerbi_analyst.studio.pbir_document import DocumentError, PageDoc, PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage
from ai_powerbi_analyst.studio.report_edit_context import NeedsConfirmation
from ai_powerbi_analyst.studio.report_edits import (
    EditOutcome,
    ModelInfo,
    apply_operations,
    parse_operations,
)
from ai_powerbi_analyst.studio.report_refs import ReferenceIndex, remove_page_references

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "imported"
PAGE1 = "ReportSection8454c812b60b8bb83b99"
DETAILS = "ReportSection3f1c0d5e7a9b2c4d6e8f"
DRILL = "ReportSectiond2a4c6e8f0b1a3c5e7d9"
TOOLTIP = "ReportSectionb7e9f1a3c5d7e9f1a3b5"
BG_ITEM = "Background-1-016077705666144604.png"
RESOURCES = "Report/StaticResources/RegisteredResources/"


@pytest.fixture(autouse=True)
def sequential_names() -> Iterator[None]:
    """New 20-hex names as a counter, so a test can name what a duplicate created."""
    counter = iter(range(1, 10_000))

    def new_name(self: PbirDocument, used: set[str]) -> str:
        while True:
            candidate = f"{next(counter):020x}"
            if candidate not in used:
                return candidate

    with mock.patch.object(PbirDocument, "new_name", new_name):
        yield


def open_document(name: str = "multipage") -> PbirDocument:
    return PbirDocument(PbixPackage(FIXTURES / f"{name}.pbix"))


def run(document: PbirDocument, *steps: dict[str, Any]) -> EditOutcome:
    return apply_operations(document, list(parse_operations(list(steps))), ModelInfo())


def refusal(document: PbirDocument, *steps: dict[str, Any]) -> str:
    with pytest.raises(DocumentError) as raised:
        run(document, *steps)
    return str(raised.value)


def filter_names(document: PbirDocument) -> list[str]:
    found: list[str] = []
    for _, data, _ in document.iter_json_parts():
        config = data.get("filterConfig") if isinstance(data, dict) else None
        found.extend(f["name"] for f in (config or {}).get("filters", []))
    return found


def png(width: int = 2, height: int = 2, *, seed: int = 0) -> bytes:
    """A real, tiny PNG (different ``seed`` gives different bytes)."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body)
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    row = b"\x00" + bytes([seed, 0, 0]) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


def page_objects(document: PbirDocument, page: str, name: str = "background") -> dict[str, Any]:
    props: dict[str, Any] = document.page(page).data["objects"][name][0]["properties"]
    return props


# --- finding pages -------------------------------------------------------------------------------


def test_a_page_is_found_by_id_name_or_position() -> None:
    document = open_document()
    assert document.resolve_page(DETAILS).name == DETAILS
    assert document.resolve_page("store DRILLTHROUGH").name == DRILL
    assert document.resolve_page("3").name == DRILL
    assert document.resolve_page(" 4 ").name == TOOLTIP


def test_a_page_named_with_a_number_wins_over_that_position() -> None:
    document = open_document()  # page 1 is named "1"
    run(document, {"op": "rename_page", "page": "2", "name": "1 again"})
    assert document.page(DETAILS).display_name == "1 again"
    assert document.resolve_page("1").name == PAGE1


def test_an_unknown_page_lists_the_pages() -> None:
    message = refusal(open_document(), {"op": "move_page", "page": "Nowhere", "index": 0})
    assert message == (
        "There is no page 'Nowhere'. The pages are: 1. '1', 2. 'Details', "
        "3. 'Store drillthrough', 4. 'Sales tooltip'"
    )


def test_a_position_past_the_last_page_is_not_a_page() -> None:
    assert "There is no page '9'" in refusal(open_document(), {"op": "delete_page", "page": "9"})


def test_two_pages_with_one_name_ask_for_a_number() -> None:
    document = open_document()
    document.page(DRILL).data["displayName"] = "Details"
    message = refusal(document, {"op": "rename_page", "page": "details", "name": "X"})
    assert message.startswith("2 pages are named 'details'; say which one by its number")
    run(document, {"op": "rename_page", "page": "2", "name": "Overview"})
    assert document.page(DETAILS).display_name == "Overview"


# --- rename, add, move ------------------------------------------------------------------------


def test_rename_changes_the_display_name_and_nothing_that_refers_to_the_page() -> None:
    document = open_document()
    run(document, {"op": "rename_page", "page": "Details", "name": "Overview"})
    assert list(document.pages) == [PAGE1, DETAILS, DRILL, TOOLTIP]
    assert document.pages[DETAILS].data["name"] == DETAILS
    assert document.pages[DETAILS].display_name == "Overview"
    # The button and the bookmark still resolve, because they name the folder.
    assert remove_page_references(document, DETAILS) == [
        "On page '1', button eff2d7b0 navigates to this page",
        "bookmark 'Details view' opens it",
    ]


def test_rename_refuses_a_taken_or_blank_name() -> None:
    document = open_document()
    assert refusal(document, {"op": "rename_page", "page": "1", "name": "sales TOOLTIP"}) == (
        "There is already a page named 'sales TOOLTIP'"
    )
    assert refusal(document, {"op": "rename_page", "page": "1", "name": "   "}) == (
        "A page needs a name"
    )


def test_add_page_after_a_page_found_by_position() -> None:
    document = open_document()
    run(document, {"op": "add_page", "name": "Summary", "after": "2"})
    assert [p.display_name for p in document.pages.values()] == [
        "1",
        "Details",
        "Summary",
        "Store drillthrough",
        "Sales tooltip",
    ]
    assert document.pages_meta["pageOrder"] == list(document.pages)


def test_move_page_changes_only_the_page_order() -> None:
    document = open_document()
    outcome = run(document, {"op": "move_page", "page": "Sales tooltip", "index": 1})
    assert outcome.summaries == ["Moved page 'Sales tooltip' to position 2."]
    assert document.pages_meta["pageOrder"] == [PAGE1, TOOLTIP, DETAILS, DRILL]
    assert list(document.pages) == [PAGE1, TOOLTIP, DETAILS, DRILL]
    assert document.pages_meta["activePageName"] == PAGE1
    written = document.commit()
    assert [w for w in written if "page.json" in w] == []


# --- duplicate --------------------------------------------------------------------------------


def test_a_duplicate_is_placed_after_its_source_with_new_visual_names() -> None:
    document = open_document()
    source_visuals = list(document.pages[PAGE1].visuals)
    outcome = run(document, {"op": "duplicate_page", "page": "1"})
    assert outcome.summaries == ["Duplicated page '1' as '1 (copy)'."]
    assert [p.display_name for p in document.pages.values()][:3] == ["1", "1 (copy)", "Details"]
    copy_ = next(p for p in document.pages.values() if p.display_name == "1 (copy)")
    assert document.pages_meta["pageOrder"][1] == copy_.name
    assert len(copy_.visuals) == len(source_visuals)
    assert set(copy_.visuals).isdisjoint(source_visuals)
    assert all(v["name"] == n for n, v in copy_.visuals.items())
    assert list(document.pages[PAGE1].visuals) == source_visuals  # the source is untouched
    every = [n for p in document.pages.values() for n in p.visuals]
    assert len(every) == len(set(every))


def test_a_duplicate_shares_no_filter_name_with_its_source() -> None:
    document = open_document()
    before = filter_names(document)
    run(document, {"op": "duplicate_page", "page": "1"})
    after = filter_names(document)
    assert len(after) == len(set(after)), "a filter name is used twice"
    added = [n for n in after if n not in before]
    # The page filter and every visual filter of the source got a name of their own.
    assert len(added) == len(after) - len(before) > 0
    assert all(re.fullmatch(r"Filter[0-9a-f]{20}", n) for n in added)


def test_a_duplicate_keeps_its_interactions_between_its_own_visuals() -> None:
    document = open_document()
    source = document.pages[PAGE1]
    run(document, {"op": "duplicate_page", "page": "1"})
    copy_ = next(p for p in document.pages.values() if p.display_name == "1 (copy)")
    interactions = copy_.data["visualInteractions"]
    assert [i["type"] for i in interactions] == ["NoFilter", "DataFilter"]
    for interaction in interactions:
        assert interaction["source"] in copy_.visuals
        assert interaction["target"] in copy_.visuals
    assert source.data["visualInteractions"][0]["source"] == "54be5cf0783e66471540"
    index = ReferenceIndex.build(document)
    assert all("54be5cf0783e66471540" not in u.part for u in index.page_uses(copy_.name))


def test_a_duplicated_group_page_points_its_members_at_the_copied_groups() -> None:
    document = open_document()
    run(document, {"op": "duplicate_page", "page": "Details"})
    copy_ = next(p for p in document.pages.values() if p.display_name == "Details (copy)")
    groups = {n for n, v in copy_.visuals.items() if "visualGroup" in v}
    assert len(groups) == 2
    parents = {v["parentGroupName"] for v in copy_.visuals.values() if "parentGroupName" in v}
    assert parents <= groups
    assert not parents & set(document.pages[DETAILS].visuals)
    # Two nested groups: one of them has the other as its parent.
    assert any(copy_.visuals[g].get("parentGroupName") in groups for g in groups)


def test_a_duplicated_drillthrough_page_gets_its_own_binding_and_filter() -> None:
    document = open_document()
    source = document.pages[DRILL].data
    old_binding, old_filter = source["pageBinding"], source["filterConfig"]["filters"][0]["name"]
    run(document, {"op": "duplicate_page", "page": "Store drillthrough", "name": "Drill again"})
    copy_ = document.page("Drill again").data
    assert copy_["type"] == "Drillthrough"
    binding = copy_["pageBinding"]
    assert binding["type"] == "Drillthrough"
    assert binding["name"] not in (old_binding["name"], "")
    new_filter = copy_["filterConfig"]["filters"][0]["name"]
    assert new_filter != old_filter
    assert [p["boundFilter"] for p in binding["parameters"]] == [new_filter]
    assert binding["parameters"][0]["name"] == f"Param_{new_filter}"
    assert source["pageBinding"] == old_binding  # the source keeps its own
    assert source["filterConfig"]["filters"][0]["name"] == old_filter


def test_a_duplicated_tooltip_page_stays_hidden_and_consistent() -> None:
    document = open_document()
    run(document, {"op": "duplicate_page", "page": "Sales tooltip"})
    copy_ = document.page("Sales tooltip (copy)")
    assert copy_.data["visibility"] == "HiddenInViewMode"
    assert copy_.data["pageBinding"]["type"] == "Tooltip"
    bound = copy_.data["pageBinding"]["parameters"][0]["boundFilter"]
    assert bound in [f["name"] for f in copy_.data["filterConfig"]["filters"]]
    assert (copy_.data["width"], copy_.data["height"]) == (320, 240)
    # Buttons that pointed at the tooltip page still point at the original only.
    assert remove_page_references(document, copy_.name) == []


def test_a_second_duplicate_gets_a_numbered_name() -> None:
    document = open_document()
    run(document, {"op": "duplicate_page", "page": "Details"})
    run(document, {"op": "duplicate_page", "page": "Details"})
    assert [p.display_name for p in document.pages.values()][1:4] == [
        "Details",
        "Details (copy 2)",
        "Details (copy)",
    ]


def test_a_duplicate_refuses_a_name_that_is_taken() -> None:
    assert refusal(
        open_document(), {"op": "duplicate_page", "page": "Details", "name": "details"}
    ) == ("There is already a page named 'details'")


def test_a_duplicate_is_written_with_every_part(tmp_path: Path) -> None:
    document = open_document()
    run(document, {"op": "duplicate_page", "page": "Details"})
    written = document.commit()
    copy_ = document.page("Details (copy)")
    assert f"{copy_.folder}page.json" in written
    assert sum(1 for w in written if w.startswith(copy_.folder + "visuals/")) == len(copy_.visuals)
    out = tmp_path / "copy.pbix"
    document.package.write(out)
    again = PbirDocument(PbixPackage(out))
    assert list(again.pages) == list(document.pages)
    assert {n: sorted(p.visuals) for n, p in again.pages.items()} == {
        n: sorted(p.visuals) for n, p in document.pages.items()
    }


# --- delete -----------------------------------------------------------------------------------


def confirmation(document: PbirDocument, page: str) -> NeedsConfirmation:
    with pytest.raises(NeedsConfirmation) as raised:
        run(document, {"op": "delete_page", "page": page})
    return raised.value


def test_deleting_a_page_that_others_use_asks_and_names_each_referrer() -> None:
    document = open_document()
    asked = confirmation(document, "Details")
    assert asked.kind == "remove_page_references"
    assert asked.step == 1
    assert asked.detail == (
        "Page 'Details' is still used: On page '1', button eff2d7b0 navigates to this page; "
        "bookmark 'Details view' opens it. Deleting it switches those off, and a bookmark "
        "that only covers this page is deleted too."
    )
    assert DETAILS in document.pages  # nothing was deleted
    assert document.commit() == []


def test_the_cleanup_switches_off_buttons_and_removes_the_page_from_bookmarks() -> None:
    document = open_document()
    outcome = run(document, {"op": "delete_page", "page": "Details", "cleanup_references": True})
    assert outcome.summaries == [
        "Deleted page 'Details'. Removed the action of button eff2d7b0 that used the deleted page. "
        "Deleted the bookmark 'Details view' because it only covered the deleted page."
    ]
    assert list(document.pages) == [PAGE1, DRILL, TOOLTIP]
    assert document.pages_meta["pageOrder"] == [PAGE1, DRILL, TOOLTIP]
    button = document.pages[PAGE1].visuals["eff2d7b04706cc92f1db"]
    assert "visualLink" not in button["visual"].get("visualContainerObjects", {})
    assert list(document.bookmarks()) == ["Bookmarkc7a5beae9c2a6a44bb3c"]
    assert [i["name"] for i in document.bookmarks_metadata["items"]] == [
        "Bookmarkc7a5beae9c2a6a44bb3c"
    ]
    # No page name is left dangling anywhere in the report.
    index = ReferenceIndex.build(document)
    assert index.page_uses(DETAILS) == []
    assert document.commit()
    assert not any(
        n.startswith(f"Report/definition/pages/{DETAILS}/") for n in document.package.names()
    )


def test_deleting_the_tooltip_page_clears_the_tooltip_of_the_chart_that_used_it() -> None:
    document = open_document()
    assert "uses this page as its tooltip" in confirmation(document, "Sales tooltip").detail
    run(document, {"op": "delete_page", "page": "Sales tooltip", "cleanup_references": True})
    chart = document.pages[PAGE1].visuals["b404a461ede7604a2774"]
    assert "visualTooltip" not in chart["visual"].get("visualContainerObjects", {})
    assert ReferenceIndex.build(document).page_uses(TOOLTIP) == []


def test_a_bookmark_that_covers_other_pages_keeps_them() -> None:
    document = open_document()
    bookmark = document.bookmarks()["Bookmarkc7a5beae9c2a6a44bb3c"]
    state = bookmark["explorationState"]
    state["sections"][DRILL] = {"visualContainers": {}}
    document.touch_bookmark("Bookmarkc7a5beae9c2a6a44bb3c")
    run(document, {"op": "delete_page", "page": "1", "cleanup_references": True})
    kept = document.bookmarks()["Bookmarkc7a5beae9c2a6a44bb3c"]["explorationState"]
    assert list(kept["sections"]) == [DRILL]
    assert kept["activeSection"] == DRILL
    assert document.pages_meta["activePageName"] == DETAILS


def test_a_page_nothing_uses_is_deleted_without_asking() -> None:
    document = open_document()
    outcome = run(document, {"op": "delete_page", "page": "Store drillthrough"})
    assert outcome.summaries == ["Deleted page 'Store drillthrough'."]
    assert list(document.pages) == [PAGE1, DETAILS, TOOLTIP]


def test_cleanup_is_harmless_when_nothing_refers_to_the_page() -> None:
    document = open_document()
    outcome = run(
        document, {"op": "delete_page", "page": "Store drillthrough", "cleanup_references": True}
    )
    assert outcome.summaries == ["Deleted page 'Store drillthrough'."]


def test_the_last_page_cannot_be_deleted() -> None:
    document = open_document("structure-twin")
    assert refusal(document, {"op": "delete_page", "page": "1"}) == (
        "A report needs at least one page"
    )


def test_delete_lists_at_most_eight_referrers() -> None:
    referrers = [f"referrer {i}" for i in range(11)]
    text = report_edit_pages._referrer_text(PageDoc("p", {"displayName": "Big"}), referrers)
    assert "referrer 7; and 3 more" in text
    assert "referrer 8" not in text


# --- size -------------------------------------------------------------------------------------


def stored(document: PbirDocument, page: str, visual: str) -> dict[str, float]:
    position: dict[str, float] = document.page(page).visuals[visual]["position"]
    return position


def test_resizing_a_page_scales_every_visual_and_says_so() -> None:
    document = open_document("structure-twin")
    outcome = run(document, {"op": "set_page_size", "page": "1", "width": 960, "height": 600})
    assert outcome.summaries == ["Resized page '1' to 960 x 600 and scaled its 15 visuals with it."]
    assert outcome.warnings == []
    page = document.pages[PAGE1]
    assert (page.data["width"], page.data["height"]) == (960, 600)
    assert stored(document, PAGE1, "e82e02f93055d6b0b544") == {
        "x": 483.0709,
        "y": 257.4803,
        "z": 0,
        "height": 311.8110,
        "width": 447,
        "tabOrder": 0,
    }
    assert stored(document, PAGE1, "fb4c5aa34e0b084b62b9")["x"] == 86.5
    assert stored(document, PAGE1, "fb4c5aa34e0b084b62b9")["y"] == 77


def test_a_change_of_shape_stretches_and_warns() -> None:
    document = open_document("structure-twin")
    outcome = run(document, {"op": "set_page_size", "page": "1", "width": 1280, "height": 720})
    assert outcome.warnings == [
        "The new page has a different shape (width x0.67, height x0.60), so the visuals were "
        "stretched to match. Check the layout in Power BI."
    ]
    for _, _, visual in document.all_visuals():
        box = visual["position"]
        assert box["x"] + box["width"] <= 1280
        assert box["y"] + box["height"] <= 720


def test_scaling_keeps_group_members_inside_their_group() -> None:
    document = open_document()
    page = document.pages[DETAILS]
    before = {n: page.box(n) for n in page.visuals}
    run(document, {"op": "set_page_size", "page": "Details", "width": 640, "height": 360})
    for name in page.visuals:
        x, y, width, height = before[name]
        assert page.box(name) == pytest.approx((x / 2, y / 2, width / 2, height / 2), abs=0.001)
    # Members' stored boxes are relative, so they scale by the same factor as their group.
    assert stored(document, DETAILS, "04ba32659627f6e8c7d7")["x"] == 20
    assert stored(document, DETAILS, "40c896eda14bfa8f6a97")["width"] == 210
    assert (
        document.page(DETAILS).visuals["04ba32659627f6e8c7d7"]["parentGroupName"]
        == "7799291338ca3548d7d5"
    )


def test_a_flush_edge_stays_on_the_page_after_rounding() -> None:
    document = open_document("structure-twin")
    page = document.pages[PAGE1]
    page.visuals["fb4c5aa34e0b084b62b9"]["position"].update({"x": 1000, "width": 920})
    run(document, {"op": "set_page_size", "page": "1", "width": 1280, "height": 800})
    box = page.visuals["fb4c5aa34e0b084b62b9"]["position"]
    assert box["x"] + box["width"] <= 1280


def test_the_same_size_changes_nothing_else() -> None:
    document = open_document("structure-twin")
    outcome = run(document, {"op": "set_page_size", "page": "1", "width": 1920, "height": 1200})
    assert outcome.summaries == ["Resized page '1' to 1920 x 1200."]
    assert [w for w in document.commit() if "visual.json" in w] == []


def test_without_scaling_a_page_that_is_too_small_is_refused_with_the_list() -> None:
    document = open_document("structure-twin")
    message = refusal(
        document, {"op": "set_page_size", "page": "1", "width": 1280, "height": 720, "scale": False}
    )
    assert message == (
        "A 1280 x 720 page is too small to hold 10 visuals on page '1': the table of Agent ID "
        "reaches 1860 across a page 1280 wide and reaches 1139 down a page 720 high; "
        "'Agents Avg MTD % Change' reaches 1405 across a page 1280 wide; "
        "'Number of Agents Sold above 100K' reaches 1844 across a page 1280 wide; "
        "'Total MTD Sales' reaches 870 down a page 720 high; "
        "'Avg MTD Sales by Agents' reaches 872 down a page 720 high; "
        "'Avg MTD % change by Agents' reaches 1130 down a page 720 high; "
        "'Avg Discount per Transaction' reaches 1130 down a page 720 high; "
        "the slicer of Store reaches 1878 across a page 1280 wide; and 2 more. "
        "Scale the visuals with the page, or pick a larger size."
    )
    assert document.pages[PAGE1].data["width"] == 1920  # nothing changed


def test_without_scaling_a_bigger_page_leaves_the_visuals_alone() -> None:
    document = open_document("structure-twin")
    outcome = run(
        document,
        {"op": "set_page_size", "page": "1", "width": 2400, "height": 1500, "scale": False},
    )
    assert outcome.summaries == ["Resized page '1' to 2400 x 1500."]
    assert stored(document, PAGE1, "fb4c5aa34e0b084b62b9")["x"] == 173


def test_the_bounds_check_uses_page_positions_of_group_members() -> None:
    document = open_document()
    # The 'Header block' group is 420 wide at x=40 (ends at 460); its inner group adds 40 and the
    # second slicer inside it another 260 with a width of 200, so that slicer ends at 540 on the
    # page although its stored x is only 260.
    message = refusal(
        document,
        {"op": "set_page_size", "page": "Details", "width": 450, "height": 720, "scale": False},
    )
    assert message == (
        "A 450 x 720 page is too small to hold 4 visuals on page 'Details': "
        "the group 'Header block' reaches 460 across a page 450 wide; "
        "the group 'Filters' reaches 500 across a page 450 wide; "
        "'Country' reaches 540 across a page 450 wide; "
        "the image at 1000, 40 reaches 1120 across a page 450 wide. "
        "Scale the visuals with the page, or pick a larger size."
    )
    ok = run(
        document,
        {"op": "set_page_size", "page": "Details", "width": 1280, "height": 720, "scale": False},
    )
    assert ok.summaries == ["Resized page 'Details' to 1280 x 720."]


def test_the_display_option_is_set_with_the_size() -> None:
    document = open_document()
    run(
        document,
        {
            "op": "set_page_size",
            "page": "Sales tooltip",
            "width": 320,
            "height": 240,
            "display": "FitToWidth",
        },
    )
    assert document.pages[TOOLTIP].data["displayOption"] == "FitToWidth"
