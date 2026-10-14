"""What the importer keeps for editing: z order, hidden flags, groups, captions, colors,
page types, the head-revision re-read and the model identity used as cache keys."""

from __future__ import annotations

import json
import shutil
import zipfile
import zlib
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.integrity import sha256_file
from ai_powerbi_analyst.studio.report_import import (
    PAGES_VERSION,
    ImportedPage,
    ImportedReport,
    ImportedVisual,
    is_effectively_hidden,
    model_fingerprint,
    model_key,
    read_report,
    refresh_from_revision,
    report_archive,
    report_pages,
)

TWIN = Path(__file__).resolve().parents[2] / "fixtures" / "imported" / "structure-twin.pbix"


def lit(text: str) -> dict[str, Any]:
    return {"expr": {"Literal": {"Value": text}}}


def solid(hex_value: str) -> dict[str, Any]:
    return {"solid": {"color": lit(f"'{hex_value}'")}}


def themed(color_id: int, percent: float = 0) -> dict[str, Any]:
    return {
        "solid": {"color": {"expr": {"ThemeDataColor": {"ColorId": color_id, "Percent": percent}}}}
    }


def chart(name: str, *, z: int | None = None, **top: Any) -> dict[str, Any]:
    position: dict[str, Any] = {"x": 10, "y": 20, "width": 300, "height": 200}
    if z is not None:
        position["z"] = z
    return {"name": name, "position": position, "visual": {"visualType": "columnChart"}, **top}


def group(name: str, title: str, **top: Any) -> dict[str, Any]:
    return {
        "name": name,
        "position": {"x": 5, "y": 6, "width": 400, "height": 300, "z": 1000, "tabOrder": 1000},
        "visualGroup": {"displayName": title, "groupMode": "ScaleMode"},
        **top,
    }


def build_pbix(
    path: Path,
    pages: dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]],
    *,
    order: list[str] | None = None,
    report: dict[str, Any] | None = None,
    parts: dict[str, bytes] | None = None,
    model: bytes | None = b"MODEL",
) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Report/definition/report.json", json.dumps(report or {}))
        archive.writestr(
            "Report/definition/pages/pages.json", json.dumps({"pageOrder": order or list(pages)})
        )
        for name, (page, visuals) in pages.items():
            archive.writestr(
                f"Report/definition/pages/{name}/page.json",
                json.dumps({"name": name, "displayName": name.title(), **page}),
            )
            for visual_name, body in visuals.items():
                archive.writestr(
                    f"Report/definition/pages/{name}/visuals/{visual_name}/visual.json",
                    json.dumps(body),
                )
        for member, data in (parts or {}).items():
            archive.writestr(member, data)
        if model is not None:
            archive.writestr("DataModel", model, compress_type=zipfile.ZIP_STORED)
    return path


def one_page(tmp_path: Path, visuals: dict[str, dict[str, Any]], **options: Any) -> ImportedPage:
    file = build_pbix(tmp_path / "r.pbix", {"p": ({}, visuals)}, **options)
    return read_report(file)[0][0]


def by_id(page: ImportedPage) -> dict[str, ImportedVisual]:
    return {v.id: v for v in page.visuals}


# --- z, tab order, hidden ---------------------------------------------------------------------


def test_z_and_tab_order_come_from_the_position_not_the_listing(tmp_path: Path) -> None:
    high = chart("a", z=14000)
    high["position"]["tabOrder"] = 7
    page = one_page(tmp_path, {"a": high, "b": chart("b", z=2000), "c": chart("c")})
    visuals = by_id(page)
    assert [visuals[i].z for i in "abc"] == [14000, 2000, 0]
    assert [visuals[i].tab_order for i in "abc"] == [7, None, None]
    # Listed alphabetically, as a Desktop save writes it, the top visual comes first: only z
    # says what is on top.
    assert [v.id for v in sorted(page.visuals, key=lambda v: v.z)] == ["c", "b", "a"]


def test_hidden_flag_is_the_visuals_own_and_defaults_to_false(tmp_path: Path) -> None:
    page = one_page(tmp_path, {"a": chart("a", isHidden=True), "b": chart("b")})
    assert (by_id(page)["a"].hidden, by_id(page)["b"].hidden) == (True, False)


# --- groups ------------------------------------------------------------------------------------


def group_page(tmp_path: Path) -> ImportedPage:
    return one_page(
        tmp_path,
        {
            "g1": group("g1", "Header block", isHidden=True),
            "g2": group("g2", "Filters", parentGroupName="g1"),
            "aa": chart("aa", z=30, parentGroupName="g1"),
            "zz": chart("zz", z=10, parentGroupName="g1"),
            "in2": chart("in2", z=20, parentGroupName="g2"),
            "free": chart("free", z=5),
        },
    )


def test_a_group_container_is_imported_with_its_bounds_and_members(tmp_path: Path) -> None:
    visuals = by_id(group_page(tmp_path))
    outer, inner = visuals["g1"], visuals["g2"]
    assert (outer.container_kind, outer.visual_type, outer.title) == (
        "group",
        "visualGroup",
        "Header block",
    )
    assert (outer.x, outer.y, outer.width, outer.height, outer.z) == (5, 6, 400, 300, 1000)
    assert outer.hidden and not inner.hidden
    assert inner.parent_group == "g1" and outer.parent_group is None
    # Members in stacking order (z), not in name order.
    assert outer.group_children == ["zz", "aa", "g2"]
    assert inner.group_children == ["in2"]
    assert visuals["free"].container_kind == "visual" and visuals["free"].group_children == []
    assert visuals["aa"].colors == [] and outer.colors == [] and outer.caption is None


def test_members_are_placed_in_page_pixels_from_their_groups_origin(tmp_path: Path) -> None:
    # Measured in Desktop 2.157: a member stored at 100,100 in a group at 300,300 draws at
    # 400,400, and a group inside a group is relative to the outer one.
    visuals = by_id(group_page(tmp_path))
    assert (visuals["g1"].x, visuals["g1"].y) == (5, 6)
    assert (visuals["g2"].x, visuals["g2"].y) == (10, 12)  # 5+5, 6+6
    assert (visuals["aa"].x, visuals["aa"].y) == (15, 26)  # 10+5, 20+6
    assert (visuals["in2"].x, visuals["in2"].y) == (20, 32)  # 10+5+5, 20+6+6
    assert (visuals["free"].x, visuals["free"].y) == (10, 20)
    # The stored (relative) values stay in the definition, which is what the editor writes.
    assert visuals["in2"].definition["position"]["x"] == 10
    assert visuals["in2"].definition["position"]["y"] == 20


def test_a_hidden_group_hides_everything_inside_it(tmp_path: Path) -> None:
    page = group_page(tmp_path)
    hidden = {v.id for v in page.visuals if is_effectively_hidden(page, v.id)}
    assert hidden == {"g1", "g2", "aa", "zz", "in2"}
    assert not is_effectively_hidden(page, "free")
    assert not is_effectively_hidden(page, "missing")


def test_group_cycles_do_not_loop(tmp_path: Path) -> None:
    page = one_page(
        tmp_path,
        {"a": group("a", "A", parentGroupName="b"), "b": group("b", "B", parentGroupName="a")},
    )
    assert not is_effectively_hidden(page, "a")


# --- pages -------------------------------------------------------------------------------------


def test_page_order_hidden_and_type(tmp_path: Path) -> None:
    file = build_pbix(
        tmp_path / "r.pbix",
        {
            "main": ({}, {}),
            "tip": ({"type": "Tooltip", "visibility": "HiddenInViewMode"}, {}),
            "drill": ({"pageBinding": {"type": "Drillthrough", "name": "x"}}, {}),
            "plain": ({"type": "Report"}, {}),
        },
        order=["plain", "main", "drill", "tip"],
    )
    pages = read_report(file)[0]
    assert [(p.id, p.order, p.hidden, p.page_type) for p in pages] == [
        ("plain", 0, False, "default"),
        ("main", 1, False, "default"),
        ("drill", 2, False, "drillthrough"),
        ("tip", 3, True, "tooltip"),
    ]


def test_the_multipage_fixture_reads_its_special_pages() -> None:
    multipage = TWIN.with_name("multipage.pbix")
    if not multipage.is_file():
        pytest.skip("multipage fixture is not available")
    pages = read_report(multipage)[0]
    assert [(p.title, p.page_type, p.hidden) for p in pages] == [
        ("1", "default", False),
        ("Details", "default", False),
        ("Store drillthrough", "drillthrough", False),
        ("Sales tooltip", "tooltip", True),
    ]
    details = {v.id[:8]: v for v in pages[1].visuals}
    assert [c[:8] for c in details["40c896ed"].group_children] == ["77992913", "dfb4645e"]
    assert [c[:8] for c in details["77992913"].group_children] == ["04ba3265", "2b5b8e3a"]


# --- captions and titles ------------------------------------------------------------------------


def titled(name: str, text: str, **more: Any) -> dict[str, Any]:
    body = chart(name)
    body["visual"]["visualContainerObjects"] = {
        "title": [{"properties": {"text": lit(text), **more}}]
    }
    return body


def test_titles_are_decoded_with_their_doubled_quotes(tmp_path: Path) -> None:
    page = one_page(tmp_path, {"a": titled("a", "'Agent''s Sales'")})
    visual = by_id(page)["a"]
    assert (visual.title, visual.caption) == ("Agent's Sales", "Agent's Sales")


def test_a_title_switched_off_is_not_a_caption_but_still_names_the_visual(tmp_path: Path) -> None:
    page = one_page(tmp_path, {"a": titled("a", "'Old title'", show=lit("false"))})
    visual = by_id(page)["a"]
    assert (visual.caption, visual.title) == (None, "Old title")


def test_a_dynamic_title_is_the_caption_but_the_list_label_is_the_field(tmp_path: Path) -> None:
    bound = {"expr": {"Measure": {"Expression": {"SourceRef": {"Entity": "M"}}, "Property": "T"}}}
    body = chart("a")
    body["visual"]["visualContainerObjects"] = {"title": [{"properties": {"text": bound}}]}
    body["visual"]["query"] = {
        "queryState": {
            "Y": {
                "projections": [
                    {
                        "field": {
                            "Measure": {
                                "Expression": {"SourceRef": {"Entity": "M"}},
                                "Property": "Sales",
                            }
                        }
                    }
                ]
            }
        }
    }
    visual = by_id(one_page(tmp_path, {"a": body}))["a"]
    assert (visual.caption, visual.title) == ("(dynamic title)", "Sales")


def test_the_agents_twin_names_visuals_by_what_the_user_reads() -> None:
    page = read_report(TWIN)[0][0]
    visuals = {v.id[:8]: v for v in page.visuals}
    card, slicer = visuals["2202e311"], visuals["54be5cf0"]
    assert (card.caption, card.title) == ("Agents Avg MTD Sales", "Agents Avg MTD Sales")
    assert (slicer.caption, slicer.title) == ("Country", "Country")
    assert visuals["8a8ba163"].caption == "TopN/BottomN"
    assert visuals["8a8ba163"].z == 14000 and visuals["953a59a3"].z == 1000
    assert visuals["e82e02f9"].caption is None and visuals["e82e02f9"].title == "EmployeeKey"
    assert (page.hidden, page.page_type, page.order) == (False, "default", 0)


# --- colors ------------------------------------------------------------------------------------


THEME = {"dataColors": ["#111111", "#222222", "#333333", "#444444"] + ["#555555"] * 5}


def themed_report(theme: dict[str, Any] | None = THEME) -> tuple[dict[str, Any], dict[str, bytes]]:
    report = {
        "themeCollection": {"customTheme": {"name": "Custom.json", "type": "RegisteredResources"}},
        "resourcePackages": [
            {
                "name": "RegisteredResources",
                "type": "RegisteredResources",
                "items": [{"name": "Custom.json", "path": "Custom.json", "type": "CustomTheme"}],
            }
        ],
    }
    parts = {}
    if theme is not None:
        parts["Report/StaticResources/RegisteredResources/Custom.json"] = json.dumps(theme).encode()
    return report, parts


def colored(
    name: str, objects: dict[str, Any], container: dict[str, Any] | None = None
) -> dict[str, Any]:
    body = chart(name)
    body["visual"]["objects"] = objects
    if container:
        body["visual"]["visualContainerObjects"] = container
    return body


def test_theme_colors_resolve_through_the_measured_palette(tmp_path: Path) -> None:
    report, parts = themed_report()
    body = colored(
        "a",
        {
            "dataPoint": [
                {"properties": {"fill": themed(2)}},  # dataColors[0]
                # dataColors[2] is #333333; half of 0x33 is 25.5, rounded half up to 0x1A
                {"properties": {"fill": themed(4, -0.5)}, "selector": {"id": "x"}},
                {"properties": {"fill": themed(0)}},  # always white
                {"properties": {"fill": themed(1)}},  # always black
                {"properties": {"fill": themed(10)}},  # invalid id: skipped
            ],
        },
    )
    visual = by_id(one_page(tmp_path, {"a": body}, report=report, parts=parts))["a"]
    assert visual.colors == ["#111111", "#1A1A1A", "#FFFFFF", "#000000"]


def test_data_point_colors_come_first_and_duplicates_and_hidden_entries_are_dropped(
    tmp_path: Path,
) -> None:
    report, parts = themed_report()
    body = colored(
        "a",
        {
            "title": [{"properties": {"fontColor": solid("#abcdef")}}],
            "labels": [{"properties": {"color": solid("#FF0000"), "show": lit("false")}}],
            "dataPoint": [
                {"properties": {"fill": solid("#1e90ff")}},
                {"properties": {"fill": solid("#1E90FF")}},
                {"properties": {"fill": themed(3)}},
            ],
        },
        {"background": [{"properties": {"color": solid("#F8F8F8")}}]},
    )
    visual = by_id(one_page(tmp_path, {"a": body}, report=report, parts=parts))["a"]
    assert visual.colors == ["#1E90FF", "#222222", "#ABCDEF", "#F8F8F8"]


def test_colors_are_capped_at_eight_and_measure_colors_are_left_out(tmp_path: Path) -> None:
    entries = [{"properties": {"fill": solid(f"#0000{i:02X}")}} for i in range(12)]
    entries.append(
        {
            "properties": {
                "fill": {
                    "solid": {
                        "color": {
                            "expr": {
                                "Measure": {
                                    "Expression": {"SourceRef": {"Entity": "M"}},
                                    "Property": "C",
                                }
                            }
                        }
                    }
                }
            }
        }
    )
    visual = by_id(one_page(tmp_path, {"a": colored("a", {"dataPoint": entries})}))["a"]
    assert visual.colors == [f"#0000{i:02X}" for i in range(8)]


def test_a_theme_color_without_a_readable_theme_is_skipped(tmp_path: Path) -> None:
    report, _ = themed_report()
    body = colored(
        "a",
        {
            "dataPoint": [
                {"properties": {"fill": themed(2)}},
                {"properties": {"fill": themed(0, -0.3)}},
            ]
        },
    )
    visual = by_id(one_page(tmp_path, {"a": body}, report=report))["a"]
    # White shaded 30% darker (half-up rounding gives B3, not B2).
    assert visual.colors == ["#B3B3B3"]


# --- legacy layout -----------------------------------------------------------------------------


def test_legacy_layout_keeps_z_tab_order_and_hidden(tmp_path: Path) -> None:
    layout = {
        "sections": [
            {
                "name": "p1",
                "visualContainers": [
                    {
                        "x": 1,
                        "y": 2,
                        "width": 3,
                        "height": 4,
                        "z": 9000,
                        "tabOrder": 3,
                        "config": json.dumps(
                            {
                                "name": "v1",
                                "singleVisual": {
                                    "visualType": "card",
                                    "display": {"mode": "hidden"},
                                    "vcObjects": {"title": [text_property("Legacy")]},
                                },
                            }
                        ),
                    }
                ],
            }
        ]
    }
    path = tmp_path / "legacy.pbix"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Report/Layout", json.dumps(layout).encode("utf-16-le"))
    pages, kind, _ = read_report(path)
    visual = pages[0].visuals[0]
    assert kind == "Legacy PBIX"
    assert (visual.z, visual.tab_order, visual.hidden, visual.caption) == (9000, 3, True, "Legacy")
    assert pages[0].order == 0


def text_property(text: str) -> dict[str, Any]:
    return {"properties": {"text": lit(f"'{text}'")}}


# --- stored reports -----------------------------------------------------------------------------


def report_with(tmp_path: Path, **more: Any) -> ImportedReport:
    fields: dict[str, Any] = {
        "id": "report-1",
        "workspace_id": "ws",
        "name": "R",
        "source_path": str(tmp_path / "source.pbix"),
        "archived_path": str(tmp_path / "original.pbix"),
        "source_hash": "a" * 64,
        "base_hash": "a" * 64,
        "format": "PBIR",
        "pages": [],
    }
    fields.update(more)
    return ImportedReport(**fields)


def test_a_report_stored_before_these_fields_still_loads_with_defaults(tmp_path: Path) -> None:
    stored = {
        "id": "report-1",
        "workspace_id": "ws",
        "name": "R",
        "source_path": "s.pbix",
        "archived_path": "o.pbix",
        "source_hash": "a" * 64,
        "base_hash": "a" * 64,
        "format": "PBIR",
        "pages": [
            {
                "id": "p",
                "title": "Page",
                "width": 1280,
                "height": 720,
                "visuals": [
                    {
                        "id": "v",
                        "title": "Sales",
                        "visual_type": "card",
                        "x": 1,
                        "y": 2,
                        "width": 3,
                        "height": 4,
                        "bindings": [],
                        "definition": {},
                    }
                ],
            }
        ],
    }
    report = ImportedReport.model_validate(stored)
    page, visual = report.pages[0], report.pages[0].visuals[0]
    assert report.pages_version == 0 < PAGES_VERSION
    assert (page.hidden, page.page_type, page.order) == (False, "default", 0)
    assert (visual.z, visual.tab_order, visual.hidden, visual.parent_group) == (
        0,
        None,
        False,
        None,
    )
    assert (visual.caption, visual.colors, visual.group_children, visual.container_kind) == (
        None,
        [],
        [],
        "visual",
    )


def test_report_pages_rereads_an_old_report_from_its_head_file_once(tmp_path: Path) -> None:
    original = tmp_path / "original.pbix"
    shutil.copyfile(TWIN, original)
    digest = sha256_file(original)
    stale = ImportedPage(id="stale", title="Stale")
    old = report_with(
        tmp_path, source_hash=digest, base_hash=digest, pages=[stale], pages_version=0
    )
    pages = report_pages(old)
    assert [p.id for p in pages] == ["ReportSection8454c812b60b8bb83b99"]
    assert {v.id[:8]: v.z for v in pages[0].visuals}["8a8ba163"] == 14000
    current = old.model_copy(update={"pages_version": PAGES_VERSION})
    assert report_pages(current) == [stale]
    # An unreadable file leaves the stored pages alone.
    broken = old.model_copy(update={"archived_path": str(tmp_path / "missing.pbix")})
    assert report_pages(broken) == [stale]


# --- refresh_from_revision --------------------------------------------------------------------


def rewrite_page_title(source: Path, target: Path, title: str) -> str:
    """A Studio-style revision: one report part changed, every other part copied."""
    with zipfile.ZipFile(source) as read, zipfile.ZipFile(target, "w") as write:
        for info in read.infolist():
            data = read.read(info.filename)
            if info.filename.endswith("/page.json"):
                page = json.loads(data.decode("utf-8-sig"))
                page["displayName"] = title
                data = json.dumps(page, separators=(",", ":")).encode()
            write.writestr(info, data, compress_type=info.compress_type)
    return sha256_file(target)


def test_refresh_from_revision_rederives_pages_and_points_at_the_head(tmp_path: Path) -> None:
    original = tmp_path / "original.pbix"
    shutil.copyfile(TWIN, original)
    digest = sha256_file(original)
    report = report_with(tmp_path, source_hash=digest, base_hash=digest, pages=[], pages_version=0)
    revision = tmp_path / "revisions" / "r1.pbix"
    revision.parent.mkdir()
    blob = rewrite_page_title(original, revision, "Renamed page")

    refreshed = refresh_from_revision(report, revision, blob)

    assert refreshed.pages[0].title == "Renamed page"
    assert (refreshed.revision_path, refreshed.revision_hash, refreshed.base_hash) == (
        str(revision),
        blob,
        blob,
    )
    assert refreshed.pages_version == PAGES_VERSION
    assert report_archive(refreshed) == (revision, blob)
    # The input is untouched, and the original file is still what source_hash says.
    assert report.pages == [] and report.revision_path is None
    assert sha256_file(original) == digest


def test_refreshing_to_the_original_clears_the_revision(tmp_path: Path) -> None:
    original = tmp_path / "original.pbix"
    shutil.copyfile(TWIN, original)
    digest = sha256_file(original)
    edited = report_with(
        tmp_path,
        source_hash=digest,
        base_hash="b" * 64,
        revision_path=str(tmp_path / "r.pbix"),
        revision_hash="b" * 64,
    )
    back = refresh_from_revision(edited, original, digest)
    assert (back.revision_path, back.revision_hash, back.base_hash) == (None, None, digest)
    assert report_archive(back) == (original, digest)


def test_refresh_refuses_a_file_that_is_not_the_recorded_version(tmp_path: Path) -> None:
    original = tmp_path / "original.pbix"
    shutil.copyfile(TWIN, original)
    report = report_with(tmp_path, source_hash=sha256_file(original))
    with pytest.raises(ValueError, match="changed on disk"):
        refresh_from_revision(report, original, "c" * 64)


# --- model_key ------------------------------------------------------------------------------


def test_model_key_is_the_data_model_crc_and_size_and_survives_report_edits(tmp_path: Path) -> None:
    original = tmp_path / "original.pbix"
    shutil.copyfile(TWIN, original)
    with zipfile.ZipFile(original) as archive:
        data = archive.read("DataModel")
    expected = f"{zlib.crc32(data):08x}-{len(data)}"
    digest = sha256_file(original)
    report = report_with(tmp_path, source_hash=digest, base_hash=digest)
    assert model_key(report) == expected
    assert model_key(original) == expected and model_key(str(original)) == expected

    revision = tmp_path / "r1.pbix"
    blob = rewrite_page_title(original, revision, "Another title")
    assert blob != digest
    edited = refresh_from_revision(report, revision, blob)
    assert model_key(edited) == expected


def test_model_key_changes_with_the_model_and_is_none_without_one(tmp_path: Path) -> None:
    first = build_pbix(tmp_path / "a.pbix", {"p": ({}, {})}, model=b"model one")
    second = build_pbix(tmp_path / "b.pbix", {"p": ({}, {})}, model=b"model two")
    thin = build_pbix(tmp_path / "c.pbix", {"p": ({}, {})}, model=None)
    assert model_key(first) == f"{zlib.crc32(b'model one'):08x}-9"
    assert model_key(first) != model_key(second)
    assert model_key(thin) == "none"


# --- model_fingerprint ----------------------------------------------------------------------


MEASURES = [
    {"Name": "Sales", "TableName": "M", "Expression": "SUM(F[Amount])", "FormatString": "#,0"},
    {"Name": "Cost", "TableName": "M", "Expression": "SUM(F[Cost])"},
]
TABLES = [
    {"TableName": "F", "ColumnName": "Amount", "PandasDataType": "float64"},
    {"TableName": "F", "ColumnName": "Cost", "PandasDataType": "float64"},
]
RELATIONSHIPS = [
    {"FromTableName": "F", "FromColumnName": "K", "ToTableName": "D", "ToColumnName": "K"}
]


def fingerprinted(tmp_path: Path, **more: Any) -> ImportedReport:
    return report_with(
        tmp_path,
        measures=MEASURES,
        tables=TABLES,
        relationships=RELATIONSHIPS,
        snapshot_id="snap-1",
        relationships_version=2,
        **more,
    )


def test_the_fingerprint_is_a_stable_sha256(tmp_path: Path) -> None:
    fingerprint = model_fingerprint(fingerprinted(tmp_path))
    assert len(fingerprint) == 64 and int(fingerprint, 16) >= 0
    assert model_fingerprint(fingerprinted(tmp_path)) == fingerprint


def test_report_layer_changes_do_not_move_the_fingerprint(tmp_path: Path) -> None:
    base = fingerprinted(tmp_path)
    layered = base.model_copy(
        update={
            "pages": [ImportedPage(id="p", title="New page", order=3)],
            "revision_path": str(tmp_path / "r.pbix"),
            "revision_hash": "d" * 64,
            "base_hash": "d" * 64,
            "warnings": ["x"],
            "name": "Renamed",
        }
    )
    assert model_fingerprint(layered) == model_fingerprint(base)


def test_order_of_measures_tables_and_relationships_does_not_matter(tmp_path: Path) -> None:
    base = fingerprinted(tmp_path)
    shuffled = base.model_copy(
        update={
            "measures": [dict(reversed(list(m.items()))) for m in reversed(MEASURES)],
            "tables": list(reversed(TABLES)),
        }
    )
    assert model_fingerprint(shuffled) == model_fingerprint(base)


@pytest.mark.parametrize(
    "change",
    [
        {"measures": [{**MEASURES[0], "Expression": "SUM(F[Amount]) * 2"}, MEASURES[1]]},
        {"measures": [{**MEASURES[0], "FormatString": "0.00"}, MEASURES[1]]},
        {"measures": [{**MEASURES[0], "Name": "Total"}, MEASURES[1]]},
        {"measures": [{**MEASURES[0], "TableName": "N"}, MEASURES[1]]},
        {"measures": MEASURES[:1]},
        {"tables": TABLES[:1]},
        {"tables": [{**TABLES[0], "PandasDataType": "int64"}, TABLES[1]]},
        {"relationships": []},
        {"relationships": [{**RELATIONSHIPS[0], "CrossFilteringBehavior": "Both"}]},
        {"snapshot_id": "snap-2"},
        {"relationships_version": 1},
    ],
)
def test_anything_the_engine_answers_from_moves_the_fingerprint(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    base = fingerprinted(tmp_path)
    assert model_fingerprint(base.model_copy(update=change)) != model_fingerprint(base)


# --- relationships of an older import -------------------------------------------------------


def test_older_imports_read_their_joins_once_per_model_not_once_per_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from ai_powerbi_analyst.studio import report_import

    calls: list[str] = []
    commands: list[list[str]] = []
    monkeypatch.setattr(report_import, "_RELATIONSHIP_CACHE", {})
    # A model Power BI wrote starts with its backup signature (others are never read).
    same = report_import.STREAM_STORAGE + b"same"
    heads = [
        build_pbix(tmp_path / f"{name}.pbix", {"p": ({}, {})}, report={"tag": name}, model=same)
        for name in ("original", "r1", "r2")
    ]
    other = build_pbix(
        tmp_path / "saved.pbix",
        {"p": ({}, {})},
        model=report_import.STREAM_STORAGE + b"rewritten by Desktop",
    )
    digests = [sha256_file(p) for p in [*heads, other]]
    base = report_with(
        tmp_path,
        archived_path=str(heads[0]),
        source_hash=digests[0],
        base_hash=digests[0],
        relationships_version=1,
    )
    real_run = report_import.subprocess.run
    archives = {str(path) for path in [*heads, other]}
    extractor = str(Path(report_import.__file__).with_name("pbix_extract.py"))

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        # subprocess is shared with background tool discovery. Only this test's
        # relationship extractor calls measure the once-per-model cache contract.
        if (
            len(argv) == 4
            and argv[1] == extractor
            and argv[-1] == "--relationships"
            and argv[-2] in archives
        ):
            commands.append(list(argv))
            calls.append(argv[-2])
            return SimpleNamespace(returncode=0, stdout='[{"Cardinality": "M:1"}]')
        return real_run(argv, **kwargs)

    monkeypatch.setattr(report_import.subprocess, "run", fake_run)

    def read(index: int) -> tuple[list[dict[str, Any]], bool]:
        path = [*heads, other][index]
        head = (
            base
            if index == 0
            else base.model_copy(
                update={"revision_path": str(path), "revision_hash": digests[index]}
            )
        )
        return report_import.report_relationships(head)

    # original, edit, edit again, undo to the first edit: one extraction.
    assert [read(i) for i in (0, 1, 2, 1)] == [([{"Cardinality": "M:1"}], True)] * 4
    assert len(calls) == 1
    assert calls == [str(heads[0])]
    assert commands == [report_import.extract_argv(heads[0], "--relationships")]
    # A Desktop save rewrites the model, so its joins are read again.
    read(3)
    assert len(calls) == 2
    assert calls == [str(heads[0]), str(other)]
    assert commands == [
        report_import.extract_argv(path, "--relationships") for path in (heads[0], other)
    ]
