"""Build fixtures/imported/multipage.pbix: a multi-page PBIR-in-PBIX for reference-heavy tests.

    .venv/Scripts/python.exe scripts/make_multipage_fixture.py [--twin PATH] [--out PATH]

The structure twin (scripts/make_structure_twin.py) has one page and no bookmarks, groups,
drillthrough or tooltip pages. Operations that rewrite references (delete or duplicate a
page or visual, rename a measure, change a theme) need those constructs to be tested, so
this script grows the twin into a four-page report. It is deterministic: the same twin
gives the same bytes, and there is no personal data in it.

What stays as Desktop wrote it: everything in the twin that is not listed below, byte for
byte (the DataModel filler, SecurityBindings filler, DiagramLayout, the page-1 visuals).

What is added (hand-built from the PBIR schemas; see fixtures/imported/README.md):
- pages "Details", "Store drillthrough" and "Sales tooltip" (the only hidden page)
- two bookmarks, bookmarks.json, a button with a page-navigation action and one with a
  bookmark action, a visualTooltip pointing at the tooltip page
- a group inside a group with children carrying parentGroupName
- visualInteractions on page 1, a TopN filter (Subquery), a page filter and a report
  filter written with From aliases
- a custom theme in RegisteredResources (textClasses, visualStyles), an image visual with
  its image resource, a mobile layout part
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import struct
import sys
import zipfile
import zlib
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DEFAULT_TWIN = REPO / "fixtures" / "imported" / "structure-twin.pbix"
DEFAULT_OUT = REPO / "fixtures" / "imported" / "multipage.pbix"
MAX_BYTES = 1024 * 1024

DEFINITION = "Report/definition/"
REGISTERED = "Report/StaticResources/RegisteredResources/"
SCHEMA_ROOT = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
# Schema versions the twin (a Desktop 2.157 file) does not contain. Not observed in a
# Desktop-written file, so they are the versions the published schemas list; unverified.
UNOBSERVED_SCHEMAS = {
    "bookmark": "2.1.0",
    "bookmarksMetadata": "1.0.0",
    "visualContainerMobileState": "2.6.0",
}

MAIN_PAGE = "ReportSection8454c812b60b8bb83b99"
DETAILS_PAGE = "ReportSection3f1c0d5e7a9b2c4d6e8f"
DRILL_PAGE = "ReportSectiond2a4c6e8f0b1a3c5e7d9"
TOOLTIP_PAGE = "ReportSectionb7e9f1a3c5d7e9f1a3b5"
LOGO = "FixtureLogo.png"
THEME = "FixtureTheme.json"

SLICER_STORE = "5b0dbccd61c6c4adb415"
SLICER_CHANNEL = "54be5cf0783e66471540"
COLUMN_CHART = "b404a461ede7604a2774"
TABLE = "e82e02f93055d6b0b544"
CARD_SOURCE = "8a8ba163374a72762030"


class FixtureError(RuntimeError):
    """The twin is not what this script builds on."""


def hex_id(label: str) -> str:
    """A deterministic 20-hex-character name like Desktop's."""
    return hashlib.sha256(f"multipage-fixture/{label}".encode()).hexdigest()[:20]


def dumps(value: Any) -> bytes:
    """Compact UTF-8 without a BOM, as Desktop writes PBIR parts."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(red: int, green: int, blue: int) -> bytes:
    """A valid 1x1 RGB PNG."""
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixel = zlib.compress(bytes([0, red, green, blue]))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", pixel)
        + _chunk(b"IEND", b"")
    )


# --- expression helpers (shapes copied from the twin) -----------------------------------


def lit(text: str) -> dict[str, Any]:
    return {"expr": {"Literal": {"Value": "'" + text.replace("'", "''") + "'"}}}


def raw(value: str) -> dict[str, Any]:
    return {"expr": {"Literal": {"Value": value}}}


def column(entity: str, prop: str, source: bool = False) -> dict[str, Any]:
    ref = {"Source": entity} if source else {"Entity": entity}
    return {"Column": {"Expression": {"SourceRef": ref}, "Property": prop}}


def measure(entity: str, prop: str, source: bool = False) -> dict[str, Any]:
    ref = {"Source": entity} if source else {"Entity": entity}
    return {"Measure": {"Expression": {"SourceRef": ref}, "Property": prop}}


def in_filter(name: str, entity: str, prop: str, values: list[str]) -> dict[str, Any]:
    """A categorical filter as Desktop writes it: Version 2, a From alias, Source in Where."""
    return {
        "name": name,
        "field": column(entity, prop),
        "type": "Categorical",
        "filter": {
            "Version": 2,
            "From": [{"Name": "d", "Entity": entity, "Type": 0}],
            "Where": [
                {
                    "Condition": {
                        "In": {
                            "Expressions": [column("d", prop, source=True)],
                            "Values": [[{"Literal": {"Value": f"'{v}'"}}] for v in values],
                        }
                    }
                }
            ],
        },
        "howCreated": "User",
    }


def top_n_filter(
    name: str, entity: str, prop: str, by: tuple[str, str], top: int
) -> dict[str, Any]:
    """A TopN visual filter: the Subquery form Desktop writes."""
    return {
        "name": name,
        "field": column(entity, prop),
        "type": "TopN",
        "filter": {
            "Version": 2,
            "From": [
                {
                    "Name": "subquery",
                    "Expression": {
                        "Subquery": {
                            "Query": {
                                "Version": 2,
                                "From": [
                                    {"Name": "d", "Entity": entity, "Type": 0},
                                    {"Name": "m", "Entity": by[0], "Type": 0},
                                ],
                                "Select": [
                                    {**column("d", prop, source=True), "Name": "field"},
                                ],
                                "OrderBy": [
                                    {"Direction": 2, "Expression": measure("m", by[1], source=True)}
                                ],
                                "Top": top,
                            }
                        }
                    },
                    "Type": 2,
                },
                {"Name": "d", "Entity": entity, "Type": 0},
            ],
            "Where": [
                {
                    "Condition": {
                        "In": {
                            "Expressions": [column("d", prop, source=True)],
                            "Table": {"SourceRef": {"Source": "subquery"}},
                        }
                    }
                }
            ],
        },
        "howCreated": "User",
    }


def position(x: float, y: float, z: int, width: float, height: float) -> dict[str, Any]:
    return {"x": x, "y": y, "z": z, "height": height, "width": width, "tabOrder": z}


# --- the fixture -------------------------------------------------------------------------


class Builder:
    def __init__(self, twin: Path) -> None:
        with zipfile.ZipFile(twin) as archive:
            self.entries = archive.infolist()
            self.parts = {e.filename: archive.read(e) for e in self.entries}
        self.order = [e.filename for e in self.entries]
        self.date_time = next(
            e.date_time for e in self.entries if e.filename.endswith("report.json")
        )
        self.attributes = self.entries[0].external_attr
        self.visual_schema = self._schema(
            f"{DEFINITION}pages/{MAIN_PAGE}/visuals/{TABLE}/visual.json"
        )
        self.page_schema = self._schema(f"{DEFINITION}pages/{MAIN_PAGE}/page.json")
        self.pages_schema = self._schema(f"{DEFINITION}pages/pages.json")
        self.new: set[str] = set()

    def _schema(self, part: str) -> str:
        return str(self.load(part)["$schema"])

    def schema(self, family: str) -> str:
        if family == "visualContainer":
            return self.visual_schema
        return f"{SCHEMA_ROOT}{family}/{UNOBSERVED_SCHEMAS[family]}/schema.json"

    def load(self, part: str) -> Any:
        return json.loads(self.parts[part].decode("utf-8"))

    def store(self, part: str, value: Any) -> None:
        self.parts[part] = dumps(value)

    def add(self, part: str, data: bytes, after: str) -> str:
        """A new member placed right after ``after`` in the archive."""
        if part in self.parts:
            raise FixtureError(f"{part} exists already")
        self.parts[part] = data
        self.order.insert(self.order.index(after) + 1, part)
        self.new.add(part)
        return part

    def add_json(self, part: str, value: Any, after: str) -> str:
        return self.add(part, dumps(value), after)

    def copy_visual(
        self,
        source_page: str,
        source: str,
        name: str,
        box: tuple[float, float, float, float],
        z: int,
        parent: str | None = None,
    ) -> dict[str, Any]:
        """A visual copied from the twin, renamed and moved."""
        visual = copy.deepcopy(
            self.load(f"{DEFINITION}pages/{source_page}/visuals/{source}/visual.json")
        )
        visual["name"] = name
        visual["position"] = position(*box[:2], z, *box[2:])
        if parent:
            visual["parentGroupName"] = parent
        return dict(visual)


def build_parts(twin: Path) -> Builder:
    b = Builder(twin)
    report_part = f"{DEFINITION}report.json"
    main_dir = f"{DEFINITION}pages/{MAIN_PAGE}/"

    # ids
    ids = {
        label: hex_id(label)
        for label in (
            "nav-button",
            "back-button",
            "logo",
            "group-outer",
            "group-inner",
            "child-a",
            "child-b",
            "child-c",
            "drill-card",
            "tooltip-card",
            "bookmark-a",
            "bookmark-b",
            "pod-drill",
            "pod-tooltip",
            "filter-page",
            "filter-report",
            "filter-topn",
            "filter-drill",
            "filter-tooltip",
            "mobile-target",
        )
    }
    bookmark_a, bookmark_b = "Bookmark" + ids["bookmark-a"], "Bookmark" + ids["bookmark-b"]
    filter_names = {k: "Filter" + v for k, v in ids.items() if k.startswith("filter-")}

    # --- report.json: theme, resources, report filter ---
    report = b.load(report_part)
    version = report["themeCollection"]["baseTheme"]["reportVersionAtImport"]
    report["themeCollection"]["customTheme"] = {
        "name": THEME,
        "reportVersionAtImport": version,
        "type": "RegisteredResources",
    }
    registered = next(p for p in report["resourcePackages"] if p["type"] == "RegisteredResources")
    registered["items"] += [
        {"name": LOGO, "path": LOGO, "type": "Image"},
        {"name": THEME, "path": THEME, "type": "CustomTheme"},
    ]
    report["filterConfig"] = {
        "filters": [
            in_filter(
                filter_names["filter-report"], "DimStore", "StoreName", ["Store 1", "Store 2"]
            )
        ]
    }
    b.store(report_part, report)

    theme = {
        "name": "Fixture theme",
        "dataColors": [
            "#0B6E4F",
            "#F26419",
            "#33658A",
            "#86BBD8",
            "#F6AE2D",
            "#758E4F",
            "#2F4858",
            "#9A031E",
            "#5F0F40",
            "#0FA3B1",
            "#B5E2FA",
            "#EDDEA4",
        ],
        "background": "#FFFFFF",
        "foreground": "#1F2933",
        "tableAccent": "#0B6E4F",
        "textClasses": {
            "callout": {"fontSize": 28, "fontFace": "DIN", "color": "#1F2933"},
            "title": {"fontSize": 14, "fontFace": "Segoe UI Semibold", "color": "#1F2933"},
            "label": {"fontSize": 10, "fontFace": "Segoe UI", "color": "#52606D"},
        },
        "visualStyles": {
            "*": {"*": {"title": [{"show": True, "fontSize": 14}]}},
            "columnChart": {"*": {"legend": [{"show": False}]}},
        },
    }
    logo = b.add(
        REGISTERED + LOGO,
        png(0x0B, 0x6E, 0x4F),
        after=next(n for n in b.order if n.startswith(REGISTERED)),
    )
    b.add_json(REGISTERED + THEME, theme, after=logo)

    # --- page 1: interactions, page filter, tooltip, button, TopN ---
    page = b.load(main_dir + "page.json")
    page["filterConfig"] = {
        "filters": [in_filter(filter_names["filter-page"], "DimChannel", "ChannelName", ["Online"])]
    }
    page["visualInteractions"] = [
        {"source": SLICER_CHANNEL, "target": COLUMN_CHART, "type": "NoFilter"},
        {"source": SLICER_STORE, "target": TABLE, "type": "DataFilter"},
    ]
    b.store(main_dir + "page.json", page)

    chart_part = f"{main_dir}visuals/{COLUMN_CHART}/visual.json"
    chart = b.load(chart_part)
    chart["visual"].setdefault("visualContainerObjects", {})["visualTooltip"] = [
        {
            "properties": {
                "show": raw("true"),
                "type": lit("ReportPage"),
                "section": lit(TOOLTIP_PAGE),
            }
        }
    ]
    b.store(chart_part, chart)

    table_part = f"{main_dir}visuals/{TABLE}/visual.json"
    table = b.load(table_part)
    table["filterConfig"]["filters"].append(
        top_n_filter(
            filter_names["filter-topn"],
            "DimEmployee",
            "EmployeeKey",
            ("# Measures", "Table MTD Sales"),
            10,
        )
    )
    b.store(table_part, table)

    last_visual = f"{main_dir}visuals/{CARD_SOURCE}/visual.json"
    nav = b.copy_visual(MAIN_PAGE, SLICER_STORE, ids["nav-button"], (1668, 110, 210, 40), 15000)
    nav["visual"] = {
        "visualType": "actionButton",
        "objects": {
            "icon": [{"properties": {"shapeType": lit("Forward")}, "selector": {"id": "default"}}]
        },
        "visualContainerObjects": {
            "visualLink": [
                {
                    "properties": {
                        "show": raw("true"),
                        "type": lit("PageNavigation"),
                        "navigationSection": lit(DETAILS_PAGE),
                    }
                }
            ]
        },
        "drillFilterOtherVisuals": True,
    }
    nav_part = b.add_json(
        f"{main_dir}visuals/{ids['nav-button']}/visual.json", nav, after=last_visual
    )
    mobile = {
        "$schema": b.schema("visualContainerMobileState"),
        "position": {"x": 0, "y": 0, "z": 0, "height": 60, "width": 320, "tabOrder": 0},
    }
    b.add_json(f"{main_dir}visuals/{ids['nav-button']}/mobile.json", mobile, after=nav_part)

    # --- Details page: groups inside groups, image, back button ---
    inner_children = [
        b.copy_visual(
            MAIN_PAGE, SLICER_STORE, ids["child-a"], (40, 60, 200, 60), 3000, ids["group-inner"]
        ),
        b.copy_visual(
            MAIN_PAGE, SLICER_CHANNEL, ids["child-b"], (260, 60, 200, 60), 4000, ids["group-inner"]
        ),
    ]
    outer_child = b.copy_visual(
        MAIN_PAGE, CARD_SOURCE, ids["child-c"], (40, 140, 300, 120), 5000, ids["group-outer"]
    )
    inner_group = {
        "$schema": b.visual_schema,
        "name": ids["group-inner"],
        "position": position(40, 60, 2000, 420, 60),
        "visualGroup": {"displayName": "Filters", "groupMode": "ScaleMode"},
        "parentGroupName": ids["group-outer"],
    }
    outer_group = {
        "$schema": b.visual_schema,
        "name": ids["group-outer"],
        "position": position(40, 60, 1000, 420, 200),
        "visualGroup": {"displayName": "Header block", "groupMode": "ScaleMode"},
    }
    image = {
        "$schema": b.visual_schema,
        "name": ids["logo"],
        "position": position(1000, 40, 6000, 120, 120),
        "visual": {
            "visualType": "image",
            "objects": {
                "general": [
                    {
                        "properties": {
                            "imageUrl": {
                                "expr": {
                                    "ResourcePackageItem": {
                                        "PackageName": "RegisteredResources",
                                        "PackageType": 1,
                                        "ItemName": LOGO,
                                    }
                                }
                            }
                        }
                    }
                ]
            },
            "drillFilterOtherVisuals": True,
        },
    }
    back = copy.deepcopy(nav)
    back["name"] = ids["back-button"]
    back["position"] = position(40, 620, 7000, 200, 40)
    back["visual"]["visualContainerObjects"]["visualLink"][0]["properties"] = {
        "show": raw("true"),
        "type": lit("Bookmark"),
        "bookmark": lit(bookmark_a),
    }
    details_visuals = [outer_group, inner_group, *inner_children, outer_child, image, back]
    page_bg = {
        "properties": {
            "color": {"solid": {"color": {"expr": {"Literal": {"Value": "'#FFFFFF'"}}}}},
            "transparency": raw("0D"),
        }
    }
    details = {
        "$schema": b.page_schema,
        "name": DETAILS_PAGE,
        "displayName": "Details",
        "displayOption": "FitToPage",
        "height": 720,
        "width": 1280,
        "objects": {"background": [page_bg]},
    }

    # --- drillthrough and tooltip pages ---
    def binding(kind: str, pod: str, filter_key: str) -> dict[str, Any]:
        return {
            "name": ids[pod],
            "type": kind,
            "parameters": [
                {
                    "name": "Param_" + filter_names[filter_key],
                    "boundFilter": filter_names[filter_key],
                    "fieldExpr": column("DimStore", "StoreName"),
                }
            ],
        }

    def bound_filter(filter_key: str) -> dict[str, Any]:
        return {
            "name": filter_names[filter_key],
            "field": column("DimStore", "StoreName"),
            "type": "Categorical",
        }

    drill = {
        "$schema": b.page_schema,
        "name": DRILL_PAGE,
        "displayName": "Store drillthrough",
        "displayOption": "FitToPage",
        "height": 720,
        "width": 1280,
        "type": "Drillthrough",
        "pageBinding": binding("Drillthrough", "pod-drill", "filter-drill"),
        "filterConfig": {"filters": [bound_filter("filter-drill")]},
        "objects": {"background": [page_bg]},
    }
    drill_card = b.copy_visual(MAIN_PAGE, CARD_SOURCE, ids["drill-card"], (40, 40, 400, 160), 1000)
    tooltip = {
        "$schema": b.page_schema,
        "name": TOOLTIP_PAGE,
        "displayName": "Sales tooltip",
        "displayOption": "ActualSize",
        "height": 240,
        "width": 320,
        "visibility": "HiddenInViewMode",
        "type": "Tooltip",
        "pageBinding": binding("Tooltip", "pod-tooltip", "filter-tooltip"),
        "filterConfig": {"filters": [bound_filter("filter-tooltip")]},
        "objects": {"background": [page_bg]},
    }
    tooltip_card = b.copy_visual(
        MAIN_PAGE, CARD_SOURCE, ids["tooltip-card"], (10, 10, 300, 100), 1000
    )

    # Desktop lists a page's visuals after its page.json; new pages follow the last member
    # of the previous page.
    anchor = [n for n in b.order if n.startswith(main_dir)][-1]
    for page_name, doc, visuals in (
        (DETAILS_PAGE, details, details_visuals),
        (DRILL_PAGE, drill, [drill_card]),
        (TOOLTIP_PAGE, tooltip, [tooltip_card]),
    ):
        folder = f"{DEFINITION}pages/{page_name}/"
        anchor = b.add_json(folder + "page.json", doc, after=anchor)
        for visual in visuals:
            anchor = b.add_json(
                f"{folder}visuals/{visual['name']}/visual.json", visual, after=anchor
            )

    # --- pages.json ---
    pages = b.load(f"{DEFINITION}pages/pages.json")
    pages["pageOrder"] = [MAIN_PAGE, DETAILS_PAGE, DRILL_PAGE, TOOLTIP_PAGE]
    b.store(f"{DEFINITION}pages/pages.json", pages)

    # --- bookmarks ---
    channel_selection = {
        "singleVisual": {
            "visualType": "slicer",
            "objects": {
                "general": [
                    {
                        "properties": {
                            "filter": {
                                "filter": in_filter(
                                    "Filter" + hex_id("bookmark-filter"),
                                    "DimChannel",
                                    "ChannelName",
                                    ["Online"],
                                )["filter"]
                            }
                        }
                    }
                ]
            },
        }
    }
    first = {
        "$schema": b.schema("bookmark"),
        "displayName": "Channel focus",
        "name": bookmark_a,
        "options": {
            "targetVisualNames": [SLICER_CHANNEL, COLUMN_CHART],
            "applyOnlyToTargetVisuals": True,
        },
        "explorationState": {
            "version": "1.3",
            "activeSection": MAIN_PAGE,
            "sections": {
                MAIN_PAGE: {
                    "visualContainers": {
                        SLICER_CHANNEL: channel_selection,
                        COLUMN_CHART: {"singleVisual": {"visualType": "columnChart"}},
                    }
                }
            },
        },
    }
    second = {
        "$schema": b.schema("bookmark"),
        "displayName": "Details view",
        "name": bookmark_b,
        "options": {"targetVisualNames": [ids["child-a"], ids["child-c"]]},
        "explorationState": {
            "version": "1.3",
            "activeSection": DETAILS_PAGE,
            "sections": {
                DETAILS_PAGE: {
                    "visualContainers": {
                        ids["child-a"]: {"singleVisual": {"visualType": "slicer"}},
                        ids["child-c"]: {"singleVisual": {"visualType": "cardVisual"}},
                    }
                }
            },
        },
    }
    meta = {
        "$schema": b.schema("bookmarksMetadata"),
        "items": [{"name": bookmark_a}, {"name": bookmark_b}],
    }
    at = b.add_json(f"{DEFINITION}bookmarks/bookmarks.json", meta, after=report_part)
    at = b.add_json(f"{DEFINITION}bookmarks/{bookmark_a}.bookmark.json", first, after=at)
    b.add_json(f"{DEFINITION}bookmarks/{bookmark_b}.bookmark.json", second, after=at)
    return b


def _literals(node: Any, keys: frozenset[str]) -> list[str]:
    """String literals stored under any of ``keys`` anywhere in a document."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys:
                text = (
                    value.get("expr", {}).get("Literal", {}).get("Value")
                    if isinstance(value, dict)
                    else None
                )
                if isinstance(text, str):
                    found.append(text.strip("'"))
            found += _literals(value, keys)
    elif isinstance(node, list):
        for item in node:
            found += _literals(item, keys)
    return found


def check(b: Builder) -> None:
    """Every reference in the fixture resolves: a broken fixture would fail tests confusingly."""
    docs = {
        n: json.loads(d.decode("utf-8"))
        for n, d in b.parts.items()
        if n.startswith(DEFINITION) and n.endswith(".json")
    }
    pages = {n.split("/")[3] for n in docs if n.endswith("/page.json")}
    bookmarks = {
        n.rsplit("/", 1)[1].removesuffix(".bookmark.json")
        for n in docs
        if n.endswith(".bookmark.json")
    }
    for doc in docs.values():
        for page in _literals(
            doc, frozenset({"navigationSection", "drillthroughSection", "section"})
        ):
            if page not in pages:
                raise FixtureError(f"A reference points at the missing page {page}")
        for bookmark in _literals(doc, frozenset({"bookmark"})):
            if bookmark not in bookmarks:
                raise FixtureError(f"A button points at the missing bookmark {bookmark}")
    names = {d.get("name") for n, d in docs.items() if n.endswith("visual.json")}
    for name, doc in docs.items():
        parent = doc.get("parentGroupName")
        if parent and parent not in names:
            raise FixtureError(f"{name} names a missing group {parent}")
    if len(pages) < 3 or len(bookmarks) != 2:
        raise FixtureError("The fixture lost a feature it must have")


def write(b: Builder, out: Path) -> dict[str, Any]:
    temporary = out.with_name(out.name + ".tmp")
    out.parent.mkdir(parents=True, exist_ok=True)
    source_entries = {e.filename: e for e in b.entries}
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            for name in b.order:
                old = source_entries.get(name)
                info = zipfile.ZipInfo(name, date_time=old.date_time if old else b.date_time)
                info.compress_type = old.compress_type if old else zipfile.ZIP_DEFLATED
                info.external_attr = old.external_attr if old else b.attributes
                if old:
                    info.create_system = old.create_system
                archive.writestr(info, b.parts[name])
        if temporary.stat().st_size > MAX_BYTES:
            raise FixtureError(f"The fixture is {temporary.stat().st_size} bytes, above 1 MiB")
        temporary.replace(out)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "parts": len(b.order),
        "new": len(b.new),
        "bytes": out.stat().st_size,
        "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--twin", type=Path, default=DEFAULT_TWIN)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    if not args.twin.is_file():
        print(
            f"No structure twin at {args.twin}; run scripts/make_structure_twin.py first",
            file=sys.stderr,
        )
        return 2
    try:
        builder = build_parts(args.twin)
        check(builder)
        summary = write(builder, args.out)
    except FixtureError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    print(
        f"Wrote {args.out} ({summary['bytes']} bytes, sha256 {summary['sha256'][:16]}): "
        f"{summary['parts']} parts, {summary['new']} added to the twin."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
