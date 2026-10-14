"""PbirDocument: themes, bookmarks, pages, thin reports and change tracking."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest
from pbix_factory import DEFINITION, dump, make_pbix, report_parts, schema, visual

from ai_powerbi_analyst.studio.pbir_document import (
    BOOKMARKS_META,
    DocumentError,
    PbirDocument,
)
from ai_powerbi_analyst.studio.pbix_package import PbixPackage

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "imported"
TWIN = FIXTURES / "structure-twin.pbix"
MULTIPAGE = FIXTURES / "multipage.pbix"

BASE_THEME = {
    "name": "Base",
    "background": "#FFFFFF",
    "foreground": "#252423",
    "dataColors": ["#111111", "#222222", "#333333"],
    "textClasses": {
        "callout": {"fontSize": 45, "fontFace": "DIN", "color": "#252423"},
        "title": {"fontSize": 12, "fontFace": "Segoe UI Semibold"},
    },
    "visualStyles": {
        "*": {"*": {"title": [{"show": True, "fontSize": 12}], "border": [{"show": False}]}},
        "card": {"*": {"labels": [{"color": {"solid": {"color": "#000000"}}}]}},
    },
}
CUSTOM_THEME = {
    "name": "Custom",
    "foreground": "#0B1F33",
    "dataColors": ["#AA0000"],
    "textClasses": {"callout": {"fontSize": 28}, "label": {"fontSize": 9}},
    "visualStyles": {
        "*": {"*": {"legend": [{"show": False}], "title": [{"show": False}]}},
        "columnChart": {"*": {"labels": [{"show": True}]}},
    },
}


def _report(theme_collection: dict[str, Any], packages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "$schema": schema("report", "3.3.0"),
        "themeCollection": theme_collection,
        "resourcePackages": packages,
    }


def _themed(
    tmp_path: Path,
    *,
    custom_type: str = "RegisteredResources",
    item_in: str = "RegisteredResources",
    custom: dict[str, Any] | None = CUSTOM_THEME,
    item_path: str = "Custom.json",
) -> PbirDocument:
    """A report whose base theme is in SharedResources and whose custom theme is wherever asked."""
    collection: dict[str, Any] = {"baseTheme": {"name": "Base", "type": "SharedResources"}}
    packages: list[dict[str, Any]] = [
        {
            "name": "SharedResources",
            "type": "SharedResources",
            "items": [{"name": "Base", "path": "BaseThemes/Base.json", "type": "BaseTheme"}],
        },
        {"name": "RegisteredResources", "type": "RegisteredResources", "items": []},
    ]
    resources = {"Report/StaticResources/SharedResources/BaseThemes/Base.json": dump(BASE_THEME)}
    if custom is not None:
        collection["customTheme"] = {"name": "Custom.json", "type": custom_type}
        package = next(p for p in packages if p["name"] == item_in)
        package["items"].append({"name": "Custom.json", "path": item_path, "type": "CustomTheme"})
        resources[f"Report/StaticResources/{item_in}/{item_path}"] = dump(custom)
    parts = report_parts(
        [("p1", "Page 1", [])], report=_report(collection, packages), resources=resources
    )
    return PbirDocument(PbixPackage(make_pbix(tmp_path / "themed.pbix", parts)))


# --- theme ----------------------------------------------------------------------------------


def test_theme_deep_merges_the_custom_theme_over_the_base(tmp_path: Path) -> None:
    theme = _themed(tmp_path).theme()
    assert theme["name"] == "Custom"
    assert theme["background"] == "#FFFFFF"  # only in the base
    assert theme["foreground"] == "#0B1F33"  # scalar replaced
    assert theme["dataColors"] == ["#AA0000"]  # lists are replaced, not concatenated
    assert theme["textClasses"] == {
        "callout": {"fontSize": 28, "fontFace": "DIN", "color": "#252423"},
        "title": {"fontSize": 12, "fontFace": "Segoe UI Semibold"},
        "label": {"fontSize": 9},
    }
    styles = theme["visualStyles"]
    assert styles["*"]["*"] == {
        "title": [{"show": False}],  # the custom list replaces the base list
        "border": [{"show": False}],  # kept from the base
        "legend": [{"show": False}],
    }
    assert styles["columnChart"] == {"*": {"labels": [{"show": True}]}}
    assert styles["card"] == BASE_THEME["visualStyles"]["card"]


def test_theme_does_not_mutate_the_stored_themes(tmp_path: Path) -> None:
    document = _themed(tmp_path)
    document.theme()["textClasses"]["callout"]["fontSize"] = 1
    assert document.theme()["textClasses"]["callout"]["fontSize"] == 28
    assert document.base_theme()["textClasses"]["callout"]["fontSize"] == 45
    # ColorId 0 and 1 are always white and black, whatever the theme's foreground is.
    assert document.palette() == ["#FFFFFF", "#000000", "#AA0000"]


def test_theme_without_a_custom_theme_is_the_base_theme(tmp_path: Path) -> None:
    document = _themed(tmp_path, custom=None)
    assert document.theme() == BASE_THEME
    assert document.custom_theme() is None
    assert document.custom_theme_location() is None


def test_custom_theme_in_registered_resources(tmp_path: Path) -> None:
    document = _themed(tmp_path)
    part = "Report/StaticResources/RegisteredResources/Custom.json"
    assert document.custom_theme_location() == ("RegisteredResources", part)
    found = document.custom_theme()
    assert found is not None and found[0] == part and found[1]["name"] == "Custom"


def test_custom_theme_in_shared_resources_is_resolved(tmp_path: Path) -> None:
    document = _themed(
        tmp_path,
        custom_type="SharedResources",
        item_in="SharedResources",
        item_path="BuiltInThemes/Custom.json",
    )
    part = "Report/StaticResources/SharedResources/BuiltInThemes/Custom.json"
    assert document.custom_theme_location() == ("SharedResources", part)
    assert document.theme()["textClasses"]["callout"]["fontSize"] == 28


def test_a_theme_entry_whose_type_disagrees_with_its_package_still_resolves(
    tmp_path: Path,
) -> None:
    document = _themed(tmp_path, custom_type="SharedResources", item_in="RegisteredResources")
    location = document.custom_theme_location()
    assert location is not None and location[0] == "RegisteredResources"
    assert document.theme()["name"] == "Custom"


def test_malformed_resource_items_ahead_of_the_theme_are_skipped(tmp_path: Path) -> None:
    document = _themed(tmp_path)
    items = document.report["resourcePackages"][1]["items"]
    items[:0] = ["junk", None, 7, ["x"]]
    part = "Report/StaticResources/RegisteredResources/Custom.json"
    assert document.custom_theme_location() == ("RegisteredResources", part)
    assert document.theme()["name"] == "Custom"


def test_an_unresolvable_custom_theme_is_ignored(tmp_path: Path) -> None:
    document = _themed(tmp_path)
    document.package.remove_part("Report/StaticResources/RegisteredResources/Custom.json")
    assert document.custom_theme_location() is None
    assert document.theme() == BASE_THEME


def test_the_theme_of_the_multipage_fixture(tmp_path: Path) -> None:
    document = PbirDocument(PbixPackage(MULTIPAGE))
    assert document.custom_theme_location() == (
        "RegisteredResources",
        "Report/StaticResources/RegisteredResources/FixtureTheme.json",
    )
    theme = document.theme()
    assert len(theme["dataColors"]) == 12
    assert theme["textClasses"]["callout"]["fontFace"] == "DIN"
    # Merged per key from the base theme: entries the custom theme does not set survive.
    assert set(theme["textClasses"]) >= {"callout", "title", "label"}


# --- thin reports ------------------------------------------------------------------------------


def test_a_report_with_a_data_model_is_not_thin() -> None:
    document = PbirDocument(PbixPackage(TWIN))
    assert document.has_model is True
    assert document.is_thin is False


def test_a_report_without_a_data_model_is_thin(tmp_path: Path) -> None:
    parts = report_parts([("p1", "Page 1", [])])
    document = PbirDocument(PbixPackage(make_pbix(tmp_path / "thin.pbix", parts, data_model=None)))
    assert document.has_model is False
    assert document.is_thin is True


def test_report_extensions_are_read_only_measures(tmp_path: Path) -> None:
    parts = report_parts([("p1", "Page 1", [])])
    extensions = {"$schema": schema("reportExtension", "1.0.0"), "entities": [{"name": "Ext"}]}
    parts[DEFINITION + "reportExtensions.json"] = dump(extensions)
    with_ext = PbirDocument(PbixPackage(make_pbix(tmp_path / "a.pbix", parts, data_model=None)))
    assert with_ext.extensions == extensions
    assert PbirDocument(PbixPackage(TWIN)).extensions == {}


# --- pages ---------------------------------------------------------------------------------------


def _three_pages(tmp_path: Path, active: str = "p1") -> PbirDocument:
    parts = report_parts(
        [
            ("p1", "Overview", [visual("v1", "Sales")]),
            ("p2", "Details", [visual("v2", "Detail"), visual("v3", "Other")]),
            ("p3", "Notes", []),
        ],
        active=active,
    )
    return PbirDocument(PbixPackage(make_pbix(tmp_path / "three.pbix", parts)))


def test_page_index(tmp_path: Path) -> None:
    document = _three_pages(tmp_path)
    assert document.page_index() == {"p1": 0, "p2": 1, "p3": 2}
    assert document.page_index("p3") == 2
    assert document.page_index("details") == 1
    assert document.page_index(document.page("p1")) == 0
    with pytest.raises(DocumentError):
        document.page_index("missing")


def test_remove_page_fixes_the_page_order_and_the_active_page(tmp_path: Path) -> None:
    document = _three_pages(tmp_path, active="p1")
    document.remove_page("p1")  # by id
    assert list(document.pages) == ["p2", "p3"]
    assert document.pages_meta["pageOrder"] == ["p2", "p3"]
    assert document.pages_meta["activePageName"] == "p2"  # first remaining page
    document.remove_page(document.page("Notes"))  # by object
    assert document.pages_meta["pageOrder"] == ["p2"]
    assert document.pages_meta["activePageName"] == "p2"


def test_remove_page_keeps_the_active_page_when_another_page_goes(tmp_path: Path) -> None:
    document = _three_pages(tmp_path, active="p3")
    document.remove_page("Details")  # by display name
    assert document.pages_meta["activePageName"] == "p3"
    assert document.pages_meta["pageOrder"] == ["p1", "p3"]


def test_remove_page_refuses_the_last_page(tmp_path: Path) -> None:
    document = _three_pages(tmp_path)
    document.remove_page("p2")
    document.remove_page("p3")
    with pytest.raises(DocumentError, match="needs at least one page"):
        document.remove_page("p1")
    assert list(document.pages) == ["p1"]
    single = PbirDocument(PbixPackage(TWIN))
    with pytest.raises(DocumentError, match="needs at least one page"):
        single.remove_page(next(iter(single.pages.values())))


def test_remove_page_removes_its_folder_on_commit(tmp_path: Path) -> None:
    document = _three_pages(tmp_path)
    document.touch_visual(document.page("p2"), "v2")
    document.remove_page("p2")
    written = document.commit()
    assert written == [DEFINITION + "pages/pages.json"]
    names = document.package.names()
    assert not [n for n in names if "/p2/" in n]
    assert DEFINITION + "pages/p1/page.json" in names
    assert document.package.read_json(DEFINITION + "pages/pages.json")["pageOrder"] == ["p1", "p3"]


# --- bookmarks -----------------------------------------------------------------------------------


def _bookmarked(tmp_path: Path, *, group: bool = False) -> PbirDocument:
    parts = report_parts([("p1", "Page 1", [visual("v1", "A")])])
    for name, title in (("BookmarkA", "First view"), ("BookmarkB", "Second view")):
        parts[f"{DEFINITION}bookmarks/{name}.bookmark.json"] = dump(
            {
                "$schema": schema("bookmark", "2.1.0"),
                "displayName": title,
                "name": name,
                "explorationState": {"version": "1.3", "activeSection": "p1"},
            }
        )
    items: list[dict[str, Any]] = [{"name": "BookmarkA"}, {"name": "BookmarkB"}]
    if group:
        items = [{"name": "Group1", "displayName": "Views", "children": ["BookmarkA", "BookmarkB"]}]
    parts[BOOKMARKS_META] = dump({"$schema": schema("bookmarksMetadata", "1.0.0"), "items": items})
    return PbirDocument(PbixPackage(make_pbix(tmp_path / "bm.pbix", parts)))


def test_bookmarks_are_read_lazily_and_looked_up_by_id_or_name(tmp_path: Path) -> None:
    document = _bookmarked(tmp_path)
    assert document._bookmarks is None
    assert list(document.bookmarks()) == ["BookmarkA", "BookmarkB"]
    assert document.bookmark("BookmarkB")["displayName"] == "Second view"
    assert document.bookmark("first VIEW")["name"] == "BookmarkA"
    with pytest.raises(DocumentError, match="no bookmark named 'nope'"):
        document.bookmark("nope")
    assert document.bookmarks_metadata["items"] == [{"name": "BookmarkA"}, {"name": "BookmarkB"}]


def test_a_report_without_bookmarks(tmp_path: Path) -> None:
    document = PbirDocument(PbixPackage(TWIN))
    assert document.bookmarks() == {}
    assert document.bookmarks_metadata == {}
    with pytest.raises(DocumentError):
        document.remove_bookmark("anything")


def test_touch_bookmark_writes_the_edited_bookmark_on_commit(tmp_path: Path) -> None:
    document = _bookmarked(tmp_path)
    document.bookmark("BookmarkA")["displayName"] = "Renamed"
    document.touch_bookmark("BookmarkA")
    assert document.commit() == [DEFINITION + "bookmarks/BookmarkA.bookmark.json"]
    stored = document.package.read_json(DEFINITION + "bookmarks/BookmarkA.bookmark.json")
    assert stored["displayName"] == "Renamed"
    assert document.package.read(DEFINITION + "bookmarks/BookmarkB.bookmark.json") == dump(
        document.bookmark("BookmarkB")
    )


def test_remove_bookmark_removes_the_file_and_its_list_entry(tmp_path: Path) -> None:
    document = _bookmarked(tmp_path)
    document.remove_bookmark("Second view")
    assert list(document.bookmarks()) == ["BookmarkA"]
    written = document.commit()
    assert written == [BOOKMARKS_META]
    assert not document.package.has(DEFINITION + "bookmarks/BookmarkB.bookmark.json")
    assert document.package.read_json(BOOKMARKS_META)["items"] == [{"name": "BookmarkA"}]
    reopened = PbirDocument(document.package)
    assert list(reopened.bookmarks()) == ["BookmarkA"]


def test_remove_bookmark_edits_groups_and_drops_emptied_ones(tmp_path: Path) -> None:
    document = _bookmarked(tmp_path, group=True)
    document.remove_bookmark("BookmarkA")
    assert document.bookmarks_metadata["items"] == [
        {"name": "Group1", "displayName": "Views", "children": ["BookmarkB"]}
    ]
    document.remove_bookmark("BookmarkB")
    assert document.bookmarks_metadata["items"] == []
    document.commit()
    assert not [n for n in document.package.names() if n.endswith(".bookmark.json")]


def _navigator_on(group: str) -> dict[str, Any]:
    navigator = visual("nav", kind="bookmarkNavigator")
    navigator["visual"]["objects"] = {
        "bookmarks": [
            {"properties": {"bookmarkGroup": {"expr": {"Literal": {"Value": f"'{group}'"}}}}}
        ]
    }
    return navigator


def test_emptying_a_group_releases_the_navigators_that_list_it(tmp_path: Path) -> None:
    document = _bookmarked(tmp_path, group=True)
    page = document.pages["p1"]
    page.visuals["nav"] = _navigator_on("Group1")
    page.visuals["nav2"] = _navigator_on("OtherGroup")
    assert document.remove_bookmark("BookmarkA") == ((), ())
    unlisted = document.remove_bookmark("BookmarkB")
    assert unlisted == (("Views",), (("p1", "nav"),))
    assert document.bookmarks_metadata["items"] == []
    group = page.visuals["nav"]["visual"]["objects"]["bookmarks"][0]["properties"]
    assert group["bookmarkGroup"] == {"expr": {"Literal": {"Value": "''"}}}
    other = page.visuals["nav2"]["visual"]["objects"]["bookmarks"][0]["properties"]
    assert other["bookmarkGroup"] == {"expr": {"Literal": {"Value": "'OtherGroup'"}}}
    assert DEFINITION + "pages/p1/visuals/nav/visual.json" in document.commit()


def test_unlist_bookmark_keeps_the_file(tmp_path: Path) -> None:
    document = _bookmarked(tmp_path, group=True)
    assert document.unlist_bookmark("BookmarkA") == ((), ())
    assert document.unlist_bookmark("nope") == ((), ())
    assert document.bookmarks_metadata["items"] == [
        {"name": "Group1", "displayName": "Views", "children": ["BookmarkB"]}
    ]
    assert list(document.bookmarks()) == ["BookmarkA", "BookmarkB"]
    assert document.commit() == [BOOKMARKS_META]


def test_add_bookmark_makes_the_list_when_the_report_has_none() -> None:
    document = PbirDocument(PbixPackage(TWIN))
    data = {"$schema": schema("bookmark", "2.1.0"), "displayName": "New", "name": "BookmarkN"}
    part = document.add_bookmark("BookmarkN", data)
    assert part == DEFINITION + "bookmarks/BookmarkN.bookmark.json"
    assert document.bookmark("new") is data
    assert [p for p, _, kind in document.iter_json_parts() if kind == "bookmark"] == [part]
    assert document.commit() == [part, BOOKMARKS_META]
    assert document.package.read_json(BOOKMARKS_META) == {
        "$schema": schema("bookmarksMetadata", "1.0.0"),
        "items": [{"name": "BookmarkN"}],
    }
    reopened = PbirDocument(document.package)
    assert reopened.bookmark("BookmarkN") == data
    with pytest.raises(DocumentError, match="already a bookmark 'BookmarkN'"):
        reopened.add_bookmark("BookmarkN", data)
    with pytest.raises(DocumentError, match="cannot name a bookmark file"):
        reopened.add_bookmark("../evil", data)
    with pytest.raises(DocumentError, match="no bookmark group 'G'"):
        reopened.add_bookmark("BookmarkM", data, group="G")


def test_add_bookmark_into_a_group_and_remove_it_again(tmp_path: Path) -> None:
    document = _bookmarked(tmp_path, group=True)
    document.add_bookmark(
        "BookmarkC", {"displayName": "Third", "name": "BookmarkC"}, group="Group1"
    )
    assert document.bookmarks_metadata["items"][0]["children"] == [
        "BookmarkA",
        "BookmarkB",
        "BookmarkC",
    ]
    document.remove_bookmark("BookmarkC")
    written = document.commit()
    assert written == [BOOKMARKS_META]  # the new file was never written
    assert not document.package.has(DEFINITION + "bookmarks/BookmarkC.bookmark.json")


def test_deleting_the_last_bookmark_removes_the_list(tmp_path: Path) -> None:
    # Desktop 2.157 removes an empty bookmarks.json when it saves; Studio does the same.
    document = _bookmarked(tmp_path, group=True)
    document.remove_bookmark("BookmarkA")
    assert document.commit() == [BOOKMARKS_META]  # BookmarkB is left
    document.remove_bookmark("BookmarkB")
    assert document.commit() == []
    assert not document.package.has(BOOKMARKS_META)
    assert not document.package.has(DEFINITION + "bookmarks/BookmarkB.bookmark.json")
    reopened = PbirDocument(document.package)
    assert reopened.bookmarks() == {} and reopened.bookmarks_metadata == {}
    # A new first bookmark makes the list again.
    reopened.add_bookmark("BookmarkC", {"displayName": "Again", "name": "BookmarkC"})
    assert BOOKMARKS_META in reopened.commit()
    assert reopened.package.has(BOOKMARKS_META)


def test_the_bookmark_list_can_be_marked_before_the_report_has_one() -> None:
    document = PbirDocument(PbixPackage(TWIN))
    document.mark_dirty(BOOKMARKS_META)
    assert document.commit() == []  # nothing to write yet: an empty list is not a file
    document.bookmark_items().append({"name": "Bookmark1"})
    document.mark_dirty(BOOKMARKS_META)
    assert document.commit() == [BOOKMARKS_META]


def test_a_dirty_bookmark_that_is_then_removed_is_not_written(tmp_path: Path) -> None:
    document = _bookmarked(tmp_path)
    document.touch_bookmark("BookmarkA")
    document.remove_bookmark("BookmarkA")
    document.commit()
    assert not document.package.has(DEFINITION + "bookmarks/BookmarkA.bookmark.json")


def test_unrelated_edits_never_load_the_bookmarks(tmp_path: Path) -> None:
    document = _bookmarked(tmp_path)
    page, item = document.visual("v1")
    item["visual"]["visualType"] = "clusteredBarChart"
    document.touch_visual(page, "v1")
    assert document.commit() == [DEFINITION + "pages/p1/visuals/v1/visual.json"]
    assert document._bookmarks is None


# --- walking and marking parts ----------------------------------------------------------


def test_iter_json_parts_yields_every_document_with_its_kind(tmp_path: Path) -> None:
    document = _bookmarked(tmp_path)
    parts = list(document.iter_json_parts())
    assert [(name, kind) for name, _, kind in parts] == [
        (DEFINITION + "report.json", "report"),
        (DEFINITION + "pages/pages.json", "pages"),
        (DEFINITION + "pages/p1/page.json", "page"),
        (DEFINITION + "pages/p1/visuals/v1/visual.json", "visual"),
        (DEFINITION + "bookmarks/BookmarkA.bookmark.json", "bookmark"),
        (DEFINITION + "bookmarks/BookmarkB.bookmark.json", "bookmark"),
    ]
    assert parts[0][1] is document.report
    assert parts[3][1] is document.visual("v1")[1]
    assert parts[4][1] is document.bookmarks()["BookmarkA"]


def test_mark_dirty_writes_a_part_edited_in_place_and_nothing_else(tmp_path: Path) -> None:
    document = _bookmarked(tmp_path)
    for name, part, kind in document.iter_json_parts():
        if kind == "visual":
            part["visual"]["visualType"] = "tableEx"
            document.mark_dirty(name)
    written = document.commit()
    assert written == [DEFINITION + "pages/p1/visuals/v1/visual.json"]
    assert document.package.read_json(written[0])["visual"]["visualType"] == "tableEx"
    assert document.commit() == []  # the dirty set is cleared


def test_mark_dirty_rejects_a_part_that_is_not_a_document(tmp_path: Path) -> None:
    document = _bookmarked(tmp_path)
    with pytest.raises(DocumentError, match="no editable report part"):
        document.mark_dirty("Report/definition/pages/nope/page.json")
    with pytest.raises(DocumentError):
        document.mark_dirty("DataModel")


def test_commit_writes_only_dirty_parts_and_removes_deleted_visual_folders(tmp_path: Path) -> None:
    parts = report_parts([("p1", "Page 1", [visual("v1", "A"), visual("v2", "B")])])
    parts[DEFINITION + "pages/p1/visuals/v2/mobile.json"] = dump({"position": {"x": 0}})
    package = PbixPackage(make_pbix(tmp_path / "in.pbix", parts))
    document = PbirDocument(package)
    page = document.page("p1")
    page.visuals["v1"]["visual"]["visualType"] = "slicer"
    document.touch_visual(page, "v1")
    document.remove_visual(page, "v2")
    assert document.commit() == [DEFINITION + "pages/p1/visuals/v1/visual.json"]
    assert not [n for n in package.names() if "/v2/" in n]
    out = tmp_path / "out.pbix"
    package.write(out)
    with zipfile.ZipFile(tmp_path / "in.pbix") as before, zipfile.ZipFile(out) as after:
        untouched = DEFINITION + "pages/p1/page.json"
        assert before.read(untouched) == after.read(untouched)


def test_resolve_page_takes_an_id_a_name_or_a_position(tmp_path: Path) -> None:
    document = _three_pages(tmp_path)
    assert document.resolve_page("p2").name == "p2"
    assert document.resolve_page("NOTES").name == "p3"
    assert document.resolve_page("1").name == "p1"
    assert document.resolve_page(" 3 ").name == "p3"
    assert document.page_list() == "1. 'Overview', 2. 'Details', 3. 'Notes'"


def test_resolve_page_prefers_a_name_to_a_position_and_lists_the_pages_on_a_miss(
    tmp_path: Path,
) -> None:
    document = _three_pages(tmp_path)
    document.page("p2").data["displayName"] = "1"
    assert document.resolve_page("1").name == "p2"
    assert document.resolve_page("2").name == "p2"  # nothing is named "2": second page
    with pytest.raises(DocumentError) as missing:
        document.resolve_page("7")
    assert str(missing.value) == (
        "There is no page '7'. The pages are: 1. 'Overview', 2. '1', 3. 'Notes'"
    )
    document.page("p3").data["displayName"] = "1"
    with pytest.raises(DocumentError) as twice:
        document.resolve_page("1")
    assert str(twice.value) == (
        "2 pages are named '1'; say which one by its number "
        "(the pages are: 1. 'Overview', 2. '1', 3. '1')"
    )


def test_box_adds_the_origins_of_the_groups_above_a_member(tmp_path: Path) -> None:
    outer = visual("outer", x=100, y=50, width=400, height=300)
    del outer["visual"]
    outer["visualGroup"] = {"displayName": "Outer", "groupMode": "ScaleMode"}
    inner = visual("inner", x=10, y=20, width=200, height=100, parentGroupName="outer")
    del inner["visual"]
    inner["visualGroup"] = {"displayName": "Inner", "groupMode": "ScaleMode"}
    member = visual("member", x=5, y=6, width=50, height=40, parentGroupName="inner")
    loose = visual("loose", x=7, y=8, width=9, height=10)
    parts = report_parts([("p1", "One", [outer, inner, member, loose])])
    page = PbirDocument(PbixPackage(make_pbix(tmp_path / "g.pbix", parts))).page("p1")
    assert page.box("member") == (115, 76, 50, 40)
    assert page.box("inner") == (110, 70, 200, 100)
    assert page.box("outer") == (100, 50, 400, 300)
    assert page.box("loose") == (7, 8, 9, 10)
    assert page.visuals["member"]["position"]["x"] == 5  # the stored value stays relative


def test_box_survives_a_group_that_is_its_own_parent(tmp_path: Path) -> None:
    looped = visual("a", x=1, y=2, width=3, height=4, parentGroupName="a")
    parts = report_parts([("p1", "One", [looped])])
    page = PbirDocument(PbixPackage(make_pbix(tmp_path / "g.pbix", parts))).page("p1")
    assert page.box("a") == (1, 2, 3, 4)


def _with_image(tmp_path: Path) -> PbirDocument:
    report = _report(
        {},
        [
            {
                "name": "RegisteredResources",
                "type": "RegisteredResources",
                "items": [{"name": "a.png", "path": "a.png", "type": "Image"}],
            }
        ],
    )
    used = visual("v1", "A")
    used["visual"]["objects"] = {
        "general": [
            {
                "properties": {
                    "imageUrl": {
                        "expr": {
                            "ResourcePackageItem": {
                                "PackageName": "RegisteredResources",
                                "PackageType": 1,
                                "ItemName": "a.png",
                            }
                        }
                    }
                }
            }
        ]
    }
    parts = report_parts(
        [("p1", "One", [used])],
        report=report,
        resources={"Report/StaticResources/RegisteredResources/a.png": b"\x89PNG-a"},
    )
    return PbirDocument(PbixPackage(make_pbix(tmp_path / "img.pbix", parts)))


def test_resource_uses_finds_every_part_that_points_at_an_item(tmp_path: Path) -> None:
    document = _with_image(tmp_path)
    assert document.resource_uses("RegisteredResources", "a.png") == [
        DEFINITION + "pages/p1/visuals/v1/visual.json"
    ]
    assert document.resource_uses("RegisteredResources", "b.png") == []
    assert document.resource_uses("SharedResources", "a.png") == []


def test_register_and_unregister_a_resource(tmp_path: Path) -> None:
    document = _with_image(tmp_path)
    assert document.resource_items("RegisteredResources") == [
        {"name": "a.png", "path": "a.png", "type": "Image"}
    ]
    assert document.resource_items("SharedResources") == []
    part = document.write_resource_bytes("RegisteredResources", "b.png", b"\x89PNG-b")
    assert part == "Report/StaticResources/RegisteredResources/b.png"
    document.register_resource("RegisteredResources", "b.png", "b.png", "Image")
    document.register_resource("RegisteredResources", "b.png", "b.png", "Image")  # no duplicate
    assert [i["name"] for i in document.resource_items("RegisteredResources")] == ["a.png", "b.png"]
    assert document.unregister_resource("RegisteredResources", "a.png") == "a.png"
    assert document.unregister_resource("RegisteredResources", "a.png") is None
    assert document.package.has("Report/StaticResources/RegisteredResources/a.png")  # until commit
    written = document.commit()
    assert DEFINITION + "report.json" in written
    assert not document.package.has("Report/StaticResources/RegisteredResources/a.png")
    assert document.package.read(part) == b"\x89PNG-b"


def test_register_resource_creates_a_missing_package(tmp_path: Path) -> None:
    parts = report_parts([("p1", "A", [])])
    document = PbirDocument(PbixPackage(make_pbix(tmp_path / "p.pbix", parts)))
    document.register_resource("RegisteredResources", "t.json", "t.json", "CustomTheme")
    assert document.report["resourcePackages"] == [
        {
            "name": "RegisteredResources",
            "type": "RegisteredResources",
            "items": [{"name": "t.json", "path": "t.json", "type": "CustomTheme"}],
        }
    ]


def test_split_resource_part() -> None:
    split = PbirDocument.split_resource_part
    assert split("Report/StaticResources/RegisteredResources/Mine.json") == (
        "RegisteredResources",
        "Mine.json",
    )
    assert split("Report/StaticResources/SharedResources/BaseThemes/X.json") == (
        "SharedResources",
        "BaseThemes/X.json",
    )
    assert split("Report/definition/report.json") is None
    assert split("Report/StaticResources/RegisteredResources") is None
    assert (
        PbirDocument.resource_part("RegisteredResources", "a.png")
        == "Report/StaticResources/RegisteredResources/a.png"
    )


def test_the_document_reads_a_synthetic_report(tmp_path: Path) -> None:
    document = _three_pages(tmp_path)
    assert [p.display_name for p in document.pages.values()] == ["Overview", "Details", "Notes"]
    assert document.visual_names() == {"v1", "v2", "v3"}
    assert document.page("details").name == "p2"
    assert [n for _, n, _ in document.all_visuals()] == ["v1", "v2", "v3"]
    assert document.schema("visualContainer").endswith("visualContainer/2.12.0/schema.json")
    assert document.schema("page").endswith("page/2.1.0/schema.json")
