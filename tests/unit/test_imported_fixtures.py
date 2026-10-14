"""The committed PBIX fixtures: what they contain, that they are safe to commit, and that
the package and document layers carry them through untouched."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from ai_powerbi_analyst.studio.pbir_document import PbirDocument
from ai_powerbi_analyst.studio.pbix_diff import semantic_diff
from ai_powerbi_analyst.studio.pbix_package import PbixPackage, data_model_digest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "imported"
TWIN = FIXTURES / "structure-twin.pbix"
MULTIPAGE = FIXTURES / "multipage.pbix"
REAL = Path.home() / "Downloads" / "Agents Performance - Dashboard.pbix"
ALL = [TWIN, MULTIPAGE]


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _walk(node: Any) -> Any:
    """Every dict in a JSON document, depth first."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _literal(node: Any) -> str | None:
    value = node.get("expr", {}).get("Literal", {}).get("Value") if isinstance(node, dict) else None
    return value.strip("'") if isinstance(value, str) else None


@pytest.fixture(scope="module")
def multipage() -> PbirDocument:
    return PbirDocument(PbixPackage(MULTIPAGE))


# --- committed-size and privacy ----------------------------------------------------------------


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.name)
def test_fixtures_are_small_and_intact(path: Path) -> None:
    assert path.stat().st_size < 1024 * 1024
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        assert archive.getinfo("DataModel").compress_type == zipfile.ZIP_STORED
        assert archive.getinfo("DataModel").file_size == 64 * 1024


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.name)
def test_fixtures_hold_no_personal_data(path: Path) -> None:
    forbidden = [b"ajano", b"Users\\", b"Users/", b"@gmail", b"@outlook", b"Downloads"]
    with zipfile.ZipFile(path) as archive:
        for entry in archive.infolist():
            if entry.filename in ("DataModel", "SecurityBindings"):
                continue  # deterministic random filler, not text
            data = archive.read(entry)
            variants = [data, data.decode("utf-16-le", "ignore").encode()]
            for needle in forbidden:
                assert not any(needle in v for v in variants), (entry.filename, needle)
            if not entry.filename.endswith(".png"):
                assert "@" not in data.decode("utf-8", "ignore"), entry.filename


def test_the_twin_keeps_the_encodings_desktop_wrote() -> None:
    with zipfile.ZipFile(TWIN) as archive:
        names = archive.namelist()
        assert names[0] == "Version" and names[-1] == "DataModel"
        assert names.index("SecurityBindings") == len(names) - 2
        for utf16 in ("Version", "DiagramLayout", "Settings", "Metadata"):
            assert b"\x00" in archive.read(utf16)[:8], utf16
        assert archive.read("[Content_Types].xml").startswith(b"\xef\xbb\xbf")
        assert not archive.read("Report/definition/report.json").startswith(b"\xef\xbb\xbf")
        assert all(e.compress_type == zipfile.ZIP_DEFLATED for e in archive.infolist()[:-1])


def test_the_twin_is_regenerated_byte_for_byte_from_the_real_report(tmp_path: Path) -> None:
    if not REAL.is_file():
        pytest.skip("the real Agents Performance report is not on this machine")
    maker = _script("make_structure_twin")
    out = tmp_path / "twin.pbix"
    maker.make_twin(REAL, out)
    assert out.read_bytes() == TWIN.read_bytes()


def test_the_multipage_fixture_is_regenerated_byte_for_byte(tmp_path: Path) -> None:
    maker = _script("make_multipage_fixture")
    out = tmp_path / "multipage.pbix"
    assert maker.main(["--out", str(out)]) == 0
    assert out.read_bytes() == MULTIPAGE.read_bytes()
    assert (
        hashlib.sha256(out.read_bytes()).hexdigest()
        == hashlib.sha256(MULTIPAGE.read_bytes()).hexdigest()
    )


# --- the twin --------------------------------------------------------------------------------


def test_the_twin_loads_through_the_document_layer() -> None:
    document = PbirDocument(PbixPackage(TWIN))
    assert len(document.pages) == 1
    assert len(document.all_visuals()) == 15
    assert document.has_model and not document.is_thin
    assert document.custom_theme_location() is None
    assert document.base_theme()["name"]
    assert document.bookmarks() == {} and document.bookmarks_metadata == {}
    kinds = {v["visual"]["visualType"] for _, _, v in document.all_visuals()}
    assert {"cardVisual", "columnChart", "tableEx", "slicer", "advancedSlicerVisual"} <= kinds


# --- the multipage fixture --------------------------------------------------------------------


def test_multipage_has_four_pages_and_one_hidden(multipage: PbirDocument) -> None:
    assert [p.display_name for p in multipage.pages.values()] == [
        "1",
        "Details",
        "Store drillthrough",
        "Sales tooltip",
    ]
    hidden = [p for p in multipage.pages.values() if p.data.get("visibility") == "HiddenInViewMode"]
    assert [p.display_name for p in hidden] == ["Sales tooltip"]
    assert multipage.pages_meta["activePageName"] == next(iter(multipage.pages))
    assert multipage.pages_meta["pageOrder"] == list(multipage.pages)


def test_multipage_bookmarks_target_a_page_and_visuals(multipage: PbirDocument) -> None:
    bookmarks = multipage.bookmarks()
    assert len(bookmarks) == 2
    assert [i["name"] for i in multipage.bookmarks_metadata["items"]] == list(bookmarks)
    names = multipage.visual_names()
    for name, bookmark in bookmarks.items():
        assert bookmark["name"] == name
        state = bookmark["explorationState"]
        assert state["activeSection"] in multipage.pages
        section = state["sections"][state["activeSection"]]
        assert (
            set(section["visualContainers"])
            <= multipage.page(state["activeSection"]).visuals.keys()
        )
        targets = bookmark["options"]["targetVisualNames"]
        assert targets and set(targets) <= names
        assert set(targets) <= set(section["visualContainers"])
    assert {b["explorationState"]["activeSection"] for b in bookmarks.values()} == {
        multipage.page("1").name,
        multipage.page("Details").name,
    }


def test_multipage_buttons_link_to_pages_and_bookmarks(multipage: PbirDocument) -> None:
    links = []
    for _, name, item in multipage.all_visuals():
        for entry in item.get("visual", {}).get("visualContainerObjects", {}).get("visualLink", []):
            props = entry["properties"]
            links.append(
                (name, item.get("visual", {}).get("visualType"), _literal(props["type"]), props)
            )
    kinds = sorted((k, t) for _, k, t, _ in links)
    assert kinds == [("actionButton", "Bookmark"), ("actionButton", "PageNavigation")]
    navigation = next(p for _, _, t, p in links if t == "PageNavigation")
    assert _literal(navigation["navigationSection"]) == multipage.page("Details").name
    bookmark = next(p for _, _, t, p in links if t == "Bookmark")
    assert _literal(bookmark["bookmark"]) in multipage.bookmarks()


def test_multipage_drillthrough_and_tooltip_pages(multipage: PbirDocument) -> None:
    drill = multipage.page("Store drillthrough")
    tooltip = multipage.page("Sales tooltip")
    for page, kind in ((drill, "Drillthrough"), (tooltip, "Tooltip")):
        binding = page.data["pageBinding"]
        assert binding["type"] == kind and page.data["type"] == kind
        filters = {f["name"] for f in page.data["filterConfig"]["filters"]}
        bound = [p["boundFilter"] for p in binding["parameters"]]
        assert bound and set(bound) <= filters
    assert (tooltip.data["width"], tooltip.data["height"]) == (320, 240)
    chart = next(
        v
        for _, _, v in multipage.all_visuals()
        if v.get("visual", {}).get("visualType") == "columnChart"
    )
    props = chart["visual"]["visualContainerObjects"]["visualTooltip"][0]["properties"]
    assert _literal(props["type"]) == "ReportPage"
    assert _literal(props["section"]) == tooltip.name


def test_multipage_has_a_group_inside_a_group(multipage: PbirDocument) -> None:
    details = multipage.page("Details")
    groups = {n: v for n, v in details.visuals.items() if "visualGroup" in v}
    assert len(groups) == 2
    outer = next(n for n, v in groups.items() if "parentGroupName" not in v)
    inner = next(n for n, v in groups.items() if "parentGroupName" in v)
    assert groups[inner]["parentGroupName"] == outer
    children = {
        n: v["parentGroupName"]
        for n, v in details.visuals.items()
        if "visual" in v and "parentGroupName" in v
    }
    assert sorted(children.values()) == sorted([inner, inner, outer])
    assert set(children.values()) <= set(groups)


def test_multipage_visual_interactions(multipage: PbirDocument) -> None:
    main = multipage.page("1")
    interactions = main.data["visualInteractions"]
    assert {i["type"] for i in interactions} == {"NoFilter", "DataFilter"}
    for entry in interactions:
        assert entry["source"] in main.visuals and entry["target"] in main.visuals


def test_multipage_custom_theme_image_and_mobile_layout(multipage: PbirDocument) -> None:
    location = multipage.custom_theme_location()
    assert location == (
        "RegisteredResources",
        "Report/StaticResources/RegisteredResources/FixtureTheme.json",
    )
    theme = multipage.custom_theme()
    assert theme is not None
    assert set(theme[1]) >= {"textClasses", "visualStyles", "dataColors"}
    assert len(theme[1]["dataColors"]) == 12
    items = {
        i["name"]: i
        for p in multipage.report["resourcePackages"]
        if p["type"] == "RegisteredResources"
        for i in p["items"]
    }
    assert items["FixtureTheme.json"]["type"] == "CustomTheme"
    image = next(
        v for _, _, v in multipage.all_visuals() if v.get("visual", {}).get("visualType") == "image"
    )
    reference = image["visual"]["objects"]["general"][0]["properties"]["imageUrl"]["expr"][
        "ResourcePackageItem"
    ]
    assert items[reference["ItemName"]]["type"] == "Image"
    assert multipage.package.has(
        f"Report/StaticResources/RegisteredResources/{reference['ItemName']}"
    )
    mobile = [n for n in multipage.package.names() if n.endswith("/mobile.json")]
    assert len(mobile) == 1
    assert "visualContainerMobileState" in multipage.package.read_json(mobile[0])["$schema"]


def test_multipage_conditional_format_and_filters_follow_desktop_shapes(
    multipage: PbirDocument,
) -> None:
    table = next(
        v
        for _, _, v in multipage.all_visuals()
        if v.get("visual", {}).get("visualType") == "tableEx"
    )
    colors = [
        props["fontColor"]["solid"]["color"]["expr"]
        for entry in table["visual"]["objects"]["values"]
        for props in [entry["properties"]]
        if "fontColor" in props
    ]
    assert colors and all("Measure" in c for c in colors)

    filters = {
        "visual": table["filterConfig"]["filters"],
        "page": multipage.page("1").data["filterConfig"]["filters"],
        "report": multipage.report["filterConfig"]["filters"],
    }
    assert [f["type"] for f in filters["visual"]] == ["Advanced", "TopN"]
    assert [f["type"] for f in filters["page"]] == ["Categorical"]
    assert [f["type"] for f in filters["report"]] == ["Categorical"]
    names = [f["name"] for group in filters.values() for f in group]
    assert len(names) == len(set(names)) and all(
        re.fullmatch(r"Filter[0-9a-f]{20,}", n) for n in names
    )
    for group in filters.values():
        for spec in group:
            body = spec["filter"]
            assert body["Version"] == 2 and body["From"]
            aliases = {f["Name"] for f in body["From"]}
            sources = [
                d["Source"]
                for node in _walk(body["Where"])
                for d in ([node["SourceRef"]] if "SourceRef" in node else [])
                if "Source" in d
            ]
            entities_in_where = [
                node["SourceRef"]
                for node in _walk(body["Where"])
                if "SourceRef" in node and "Entity" in node["SourceRef"]
            ]
            assert sources and set(sources) <= aliases
            assert not entities_in_where
    top = filters["visual"][1]
    subquery = next(f for f in top["filter"]["From"] if f["Name"] == "subquery")
    assert subquery["Expression"]["Subquery"]["Query"]["Top"] == 10


def test_multipage_new_ids_are_unique_and_desktop_shaped(multipage: PbirDocument) -> None:
    names = [n for _, n, _ in multipage.all_visuals()]
    assert len(names) == len(set(names)) == 25
    assert all(re.fullmatch(r"[0-9a-f]{20}", n) for n in names)


# --- carried through the package and document layers ------------------------------------------


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.name)
def test_round_trip_is_semantically_empty_and_keeps_every_untouched_part(
    path: Path, tmp_path: Path
) -> None:
    out = tmp_path / "round.pbix"
    PbixPackage(path).write(out)
    diff = semantic_diff(path, out)
    assert diff.empty, diff.summary()
    assert data_model_digest(path) == data_model_digest(out)
    with zipfile.ZipFile(path) as before, zipfile.ZipFile(out) as after:
        kept = [e for e in before.infolist() if e.filename != "SecurityBindings"]
        assert [e.filename for e in after.infolist()] == [e.filename for e in kept]
        assert [e.compress_type for e in after.infolist()] == [e.compress_type for e in kept]
        for entry in kept:
            if entry.filename != "[Content_Types].xml":
                assert before.read(entry) == after.read(entry.filename), entry.filename
    document = PbirDocument(PbixPackage(out))
    original = PbirDocument(PbixPackage(path))
    assert list(document.pages) == list(original.pages)
    assert document.visual_names() == original.visual_names()
    assert list(document.bookmarks()) == list(original.bookmarks())


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.name)
def test_committing_an_unedited_document_writes_nothing(path: Path) -> None:
    document = PbirDocument(PbixPackage(path))
    assert document.commit() == []
    assert not document.package.changed


def test_an_in_place_edit_of_one_visual_changes_only_that_visual(tmp_path: Path) -> None:
    document = PbirDocument(PbixPackage(MULTIPAGE))
    page, name = document.page("1"), "b404a461ede7604a2774"
    page.visuals[name]["position"]["x"] = 321.5
    document.touch_visual(page, name)
    document.commit()
    out = tmp_path / "edited.pbix"
    document.package.write(out)
    diff = semantic_diff(MULTIPAGE, out)
    assert [(c.part.rsplit("/", 2)[-2], c.path, c.after) for c in diff.changed] == [
        (name, "/position/x", 321.5)
    ]
    assert diff.added == diff.removed == diff.binary_changed == ()


def test_deleting_the_tooltip_page_and_a_bookmark_on_the_fixture(tmp_path: Path) -> None:
    document = PbirDocument(PbixPackage(MULTIPAGE))
    document.remove_page("Sales tooltip")
    document.remove_bookmark("Details view")
    document.commit()
    out = tmp_path / "trimmed.pbix"
    document.package.write(out)
    reopened = PbirDocument(PbixPackage(out))
    assert [p.display_name for p in reopened.pages.values()] == [
        "1",
        "Details",
        "Store drillthrough",
    ]
    assert [b["displayName"] for b in reopened.bookmarks().values()] == ["Channel focus"]
    assert [i["name"] for i in reopened.bookmarks_metadata["items"]] == list(reopened.bookmarks())
    assert not [n for n in reopened.package.names() if "ReportSectionb7e9f1a3c5d7e9f1a3b5" in n]
    summary = semantic_diff(MULTIPAGE, out).summary()
    assert "Page 'Sales tooltip' removed (with 1 visual)" in summary
    assert "Bookmark 'Details view' removed" in summary


def test_a_gallery_theme_in_shared_resources_resolves_like_a_registered_one(tmp_path: Path) -> None:
    """The variant Desktop writes when the theme came from the gallery, built in place."""
    source = PbixPackage(MULTIPAGE)
    registered = "Report/StaticResources/RegisteredResources/FixtureTheme.json"
    shared = "Report/StaticResources/SharedResources/BuiltInThemes/FixtureTheme.json"
    theme = source.read(registered)
    report = source.read_json("Report/definition/report.json")
    report["themeCollection"]["customTheme"]["type"] = "SharedResources"
    gallery = {"name": "FixtureTheme.json", "path": "BuiltInThemes/FixtureTheme.json"}
    for package in report["resourcePackages"]:
        package["items"] = [i for i in package["items"] if i["name"] != "FixtureTheme.json"]
        if package["type"] == "SharedResources":
            package["items"].append({**gallery, "type": "CustomTheme"})
    source.write_json("Report/definition/report.json", report)
    source.remove_part(registered)
    source.write_part(shared, theme)
    out = tmp_path / "shared-theme.pbix"
    source.write(out)
    document = PbirDocument(PbixPackage(out))
    assert document.custom_theme_location() == ("SharedResources", shared)
    assert document.theme() == PbirDocument(PbixPackage(MULTIPAGE)).theme()
    assert json.loads(theme)["name"] == document.theme()["name"]
