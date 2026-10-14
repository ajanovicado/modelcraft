"""The visible caption of each visual type, pinned on the real shapes of the Agents report."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.studio.report_captions import (
    DYNAMIC_TITLE,
    CaptionTarget,
    caption_target,
    column_captions,
    find_caption_overlays,
    visible_caption,
)
from ai_powerbi_analyst.studio.report_import import ImportedVisual

TWIN = Path(__file__).resolve().parents[2] / "fixtures" / "imported" / "structure-twin.pbix"
CARD = "2202e311b1493069e4eb"  # shows "Agents Avg MTD Sales", container has no title
CHANNEL_SLICER = "54be5cf0783e66471540"  # field ChannelName, header "Country"
OVERLAY = "8a8ba163374a72762030"
HOST = "953a59a3cad98b0b5a39"


@pytest.fixture(scope="module")
def twin() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    with zipfile.ZipFile(TWIN) as archive:
        for name in archive.namelist():
            if name.endswith("/visual.json"):
                data = json.loads(archive.read(name).decode("utf-8-sig"))
                found[data["name"]] = data
    return found


def literal(text: str) -> dict[str, Any]:
    return {"expr": {"Literal": {"Value": text}}}


def text_entry(text: str, **more: Any) -> dict[str, Any]:
    return {"properties": {"text": literal(f"'{text}'"), **more}}


def visual(kind: str, objects: dict[str, Any] | None = None, **body: Any) -> dict[str, Any]:
    return {"name": "v", "visual": {"visualType": kind, "objects": objects or {}, **body}}


def container(title: list[dict[str, Any]]) -> dict[str, Any]:
    return {"visualContainerObjects": {"title": title}}


# --- the real shapes ------------------------------------------------------------------------


def test_new_card_shows_its_label_not_its_field_or_container(twin: dict[str, Any]) -> None:
    card = twin[CARD]
    assert "title" not in card["visual"].get("visualContainerObjects", {}) or not any(
        "text" in entry.get("properties", {})
        for entry in card["visual"]["visualContainerObjects"]["title"]
    )
    assert visible_caption(card) == "Agents Avg MTD Sales"
    # The stored spelling keeps its trailing space.
    assert visible_caption(card, strip=False) == "Agents Avg MTD Sales "
    assert caption_target(card) == CaptionTarget(
        "label",
        ("visual", "objects", "label", 0, "properties", "text"),
        exists=True,
        shown=True,
        selector={"metadata": "# Measures.Employees Avg MTD Sales"},
    )


def test_channel_slicer_shows_country_although_its_field_is_channel_name(
    twin: dict[str, Any],
) -> None:
    slicer = twin[CHANNEL_SLICER]
    assert (
        slicer["visual"]["query"]["queryState"]["Values"]["projections"][0]["field"]["Column"][
            "Property"
        ]
        == "ChannelName"
    )
    assert visible_caption(slicer) == "Country"
    target = caption_target(slicer)
    assert (target.where, target.path, target.exists) == (
        "header",
        ("visual", "objects", "header", 0, "properties", "text"),
        True,
    )


def test_other_slicers_and_the_table_show_no_caption_of_their_own(twin: dict[str, Any]) -> None:
    # A switched-off header is not a caption, and a table has no title here.
    assert visible_caption(twin[HOST]) is None
    assert caption_target(twin[HOST]).shown is False
    assert visible_caption(twin["5b0dbccd61c6c4adb415"]) is None
    assert visible_caption(twin["e82e02f93055d6b0b544"]) is None


def test_charts_show_their_container_title(twin: dict[str, Any]) -> None:
    chart = twin["b404a461ede7604a2774"]
    assert visible_caption(chart) == "Total MTD Sales"
    assert caption_target(chart).path == (
        "visual",
        "visualContainerObjects",
        "title",
        0,
        "properties",
        "text",
    )


def test_an_emptied_card_shows_its_container_title(twin: dict[str, Any]) -> None:
    overlay = twin[OVERLAY]
    assert visible_caption(overlay) == "TopN/BottomN"
    assert caption_target(overlay).where == "title"


def test_the_twin_has_one_caption_overlay_over_the_top_bottom_slicer(
    twin: dict[str, Any],
) -> None:
    assert find_caption_overlays(list(twin.values())) == [(OVERLAY, HOST)]


def test_table_columns_are_named_by_their_display_names(twin: dict[str, Any]) -> None:
    assert column_captions(twin["e82e02f93055d6b0b544"]) == [
        ("DimEmployee.EmployeeKey", "Agent ID"),
        ("# Measures.Employee Name", "Agent Name"),
        ("# Measures.Rank", "Rank"),
        ("# Measures.Table MTD Sales", "MTD Sales"),
        ("# Measures.vs PM MOM%", "vs PM %"),
    ]


# --- decoding --------------------------------------------------------------------------------


def test_doubled_quotes_are_undoubled() -> None:
    chart = visual(
        "columnChart", **container([{"properties": {"text": literal("'Agent''s Sales'")}}])
    )
    assert visible_caption(chart) == "Agent's Sales"


def test_a_title_bound_to_a_measure_is_dynamic() -> None:
    bound = {
        "expr": {"Measure": {"Expression": {"SourceRef": {"Entity": "M"}}, "Property": "Title"}}
    }
    chart = visual("columnChart")
    chart["visual"]["visualContainerObjects"] = {"title": [{"properties": {"text": bound}}]}
    assert visible_caption(chart) == DYNAMIC_TITLE == "(dynamic title)"


def test_a_title_switched_off_shows_nothing_but_keeps_its_slot() -> None:
    chart = visual("columnChart")
    chart["visual"]["visualContainerObjects"] = {
        "title": [text_entry("Sales", show=literal("false"))]
    }
    assert visible_caption(chart) is None
    target = caption_target(chart)
    assert (target.shown, target.exists) == (False, True)


def test_a_chart_without_a_title_has_a_slot_to_create() -> None:
    target = caption_target(visual("columnChart"))
    assert target == CaptionTarget(
        "title", ("visual", "visualContainerObjects", "title", 0, "properties", "text")
    )


# --- other types ------------------------------------------------------------------------------


def test_text_box_caption_joins_runs_within_a_paragraph_and_paragraphs_with_spaces() -> None:
    box = visual(
        "textbox",
        {
            "general": [
                {
                    "properties": {
                        "paragraphs": [
                            {"textRuns": [{"value": "Sales "}, {"value": "by  region"}]},
                            {"textRuns": [{"value": "  MTD"}]},
                        ]
                    }
                }
            ]
        },
    )
    assert visible_caption(box) == "Sales by region MTD"
    assert caption_target(box).where == "paragraphs"


def test_slicer_header_beats_container_title_unless_switched_off() -> None:
    slicer = visual("slicer", {"header": [text_entry("Country")]})
    slicer["visual"]["visualContainerObjects"] = {"title": [text_entry("Filters")]}
    assert visible_caption(slicer) == "Country"
    slicer["visual"]["objects"]["header"][0]["properties"]["show"] = literal("false")
    assert visible_caption(slicer) == "Filters"
    assert caption_target(slicer).where == "title"


def test_card_label_hidden_by_the_default_entry_falls_back_to_the_container_title() -> None:
    card = visual(
        "cardVisual",
        {
            "label": [
                {"properties": {"show": literal("false")}, "selector": {"id": "default"}},
                {**text_entry("Old label"), "selector": {"metadata": "T.Sales"}},
            ]
        },
        query={"queryState": {"Data": {"projections": [{"queryRef": "T.Sales"}]}}},
    )
    card["visual"]["visualContainerObjects"] = {"title": [text_entry("Card title")]}
    assert visible_caption(card) == "Card title"


def test_card_label_is_read_for_the_data_field_first() -> None:
    card = visual(
        "cardVisual",
        {
            "label": [
                {**text_entry("Other"), "selector": {"metadata": "T.Other"}},
                {**text_entry("Sales label"), "selector": {"metadata": "T.Sales"}},
            ]
        },
        query={"queryState": {"Data": {"projections": [{"queryRef": "T.Sales"}]}}},
    )
    assert visible_caption(card) == "Sales label"
    assert caption_target(card).path[3] == 1


def test_card_without_a_label_targets_a_new_scoped_entry() -> None:
    card = visual(
        "cardVisual",
        {"label": [{"properties": {"show": literal("true")}, "selector": {"id": "default"}}]},
        query={"queryState": {"Data": {"projections": [{"queryRef": "T.Sales"}]}}},
    )
    assert visible_caption(card) is None
    assert caption_target(card) == CaptionTarget(
        "label",
        ("visual", "objects", "label", 1, "properties", "text"),
        exists=False,
        shown=True,
        selector={"metadata": "T.Sales"},
    )


def test_legacy_card_is_named_by_its_field_unless_the_category_label_is_off() -> None:
    card = visual(
        "card",
        query={
            "queryState": {
                "Values": {"projections": [{"queryRef": "T.Sales", "nativeQueryRef": "Sales"}]}
            }
        },
    )
    assert visible_caption(card) == "Sales"
    assert caption_target(card).where == "projection"
    renamed = json.loads(json.dumps(card))
    renamed["visual"]["query"]["queryState"]["Values"]["projections"][0]["displayName"] = "Revenue"
    assert visible_caption(renamed) == "Revenue"
    assert caption_target(renamed).path[-1] == "displayName"
    assert caption_target(renamed).exists
    renamed["visual"]["objects"]["categoryLabels"] = [{"properties": {"show": literal("false")}}]
    assert visible_caption(renamed) is None


def test_a_button_is_named_by_its_words_in_the_default_state() -> None:
    # Desktop's shape: the switch without a selector, the words per state.
    button = visual(
        "actionButton",
        {
            "text": [
                {"properties": {"show": literal("true")}},
                text_entry("Details"),
                text_entry("Go to details"),
            ]
        },
    )
    button["visual"]["objects"]["text"][1]["selector"] = {"id": "default"}
    button["visual"]["objects"]["text"][2]["selector"] = {"id": "hover"}
    assert visible_caption(button) == "Details"
    assert caption_target(button) == CaptionTarget(
        "button_text",
        ("visual", "objects", "text", 1, "properties", "text"),
        exists=True,
        shown=True,
        selector={"id": "default"},
    )
    # Unset, the switch is on when there are words (as the canvas draws the button).
    del button["visual"]["objects"]["text"][0]
    assert visible_caption(button) == "Details"
    button["visual"]["objects"]["text"].insert(0, {"properties": {"show": literal("false")}})
    assert visible_caption(button) is None
    # Words switched off, or none: the container title, when it shows one.
    button["visual"].update(container([text_entry("Navigation")]))
    assert visible_caption(button) == "Navigation"
    bare = visual("actionButton", {"icon": [{"properties": {}, "selector": {"id": "default"}}]})
    assert visible_caption(bare) is None
    assert caption_target(bare) == CaptionTarget(
        "title", ("visual", "visualContainerObjects", "title", 0, "properties", "text")
    )


def test_legacy_container_objects_are_read() -> None:
    legacy = {
        "name": "v",
        "visual": {"visualType": "tableEx", "vcObjects": {"title": [text_entry("Legacy")]}},
    }
    assert visible_caption(legacy) == "Legacy"
    assert caption_target(legacy).path[1] == "vcObjects"


def test_malformed_shapes_do_not_raise() -> None:
    assert visible_caption({}) is None
    assert (
        visible_caption({"visual": {"visualType": "cardVisual", "objects": {"label": "x"}}}) is None
    )
    assert (
        visible_caption({"visual": {"visualType": "textbox", "objects": {"general": [1]}}}) is None
    )
    assert column_captions({"visual": {"query": {"queryState": {"Values": []}}}}) == []


# --- overlays ---------------------------------------------------------------------------------


def box(
    name: str,
    x: float,
    y: float,
    width: float,
    height: float,
    z: int,
    body: dict[str, Any],
    **top: Any,
) -> dict[str, Any]:
    return {
        "name": name,
        "position": {"x": x, "y": y, "width": width, "height": height, "z": z},
        "visual": body,
        **top,
    }


def label_box(name: str, x: float, y: float, w: float, h: float, z: int) -> dict[str, Any]:
    return box(
        name,
        x,
        y,
        w,
        h,
        z,
        {
            "visualType": "textbox",
            "objects": {
                "general": [{"properties": {"paragraphs": [{"textRuns": [{"value": "Region"}]}]}}]
            },
        },
    )


def slicer_box(name: str, x: float, y: float, w: float, h: float, z: int) -> dict[str, Any]:
    return box(
        name,
        x,
        y,
        w,
        h,
        z,
        {
            "visualType": "slicer",
            "query": {"queryState": {"Values": {"projections": [{"queryRef": "T.Region"}]}}},
        },
    )


def test_a_text_box_over_a_slicer_is_a_caption_overlay() -> None:
    page = [slicer_box("s", 100, 100, 200, 80, 1), label_box("t", 110, 95, 60, 20, 2)]
    # The text box sticks out above the slicer, as the measured overlay does.
    assert find_caption_overlays(page) == [("t", "s")]


@pytest.mark.parametrize(
    "overlay",
    [
        label_box("t", 110, 110, 60, 20, 0),  # below the slicer, not over it
        label_box("t", 400, 400, 60, 20, 2),  # elsewhere on the page
        label_box("t", 100, 100, 190, 75, 2),  # as large as the slicer: not a caption
    ],
)
def test_text_that_is_not_lying_over_the_visual_is_not_an_overlay(overlay: dict[str, Any]) -> None:
    assert find_caption_overlays([slicer_box("s", 100, 100, 200, 80, 1), overlay]) == []


def test_hidden_overlays_and_groups_never_take_part() -> None:
    hidden = label_box("t", 110, 110, 60, 20, 2)
    hidden["isHidden"] = True
    group = {
        "name": "g",
        "position": {"x": 0, "y": 0, "width": 900, "height": 900, "z": 0},
        "visualGroup": {},
    }
    assert find_caption_overlays([slicer_box("s", 100, 100, 200, 80, 1), hidden, group]) == []


def test_every_host_under_an_overlay_is_offered_smallest_first() -> None:
    page = [
        slicer_box("big", 0, 0, 500, 300, 1),
        slicer_box("small", 100, 100, 200, 80, 2),
        label_box("t", 110, 110, 60, 20, 3),
    ]
    assert find_caption_overlays(page) == [("t", "small"), ("t", "big")]


def test_imported_visuals_are_accepted_as_well_as_raw_json() -> None:
    raw_host, raw_text = slicer_box("s", 100, 100, 200, 80, 1), label_box("t", 110, 110, 60, 20, 2)
    imported = [
        ImportedVisual(
            id=raw["name"],
            title=raw["name"],
            visual_type=raw["visual"]["visualType"],
            x=raw["position"]["x"],
            y=raw["position"]["y"],
            width=raw["position"]["width"],
            height=raw["position"]["height"],
            z=raw["position"]["z"],
            definition=raw,
        )
        for raw in (raw_host, raw_text)
    ]
    assert find_caption_overlays(imported) == [("t", "s")]
    # Listed in the other order (a Desktop save sorts by folder name) the answer is the same.
    assert find_caption_overlays(imported[::-1]) == [("t", "s")]


def test_members_of_a_hidden_group_never_take_part() -> None:
    # Desktop does not draw a hidden group's members, so neither the text box nor a host
    # inside the group may be offered for a rename.
    group = {
        "name": "g",
        "position": {"x": 0, "y": 0, "width": 900, "height": 900, "z": 5},
        "isHidden": True,
        "visualGroup": {"displayName": "G"},
    }
    text = {**label_box("t", 110, 110, 60, 20, 3), "parentGroupName": "g"}
    host = {**slicer_box("h", 100, 100, 200, 80, 1), "parentGroupName": "g"}
    plain_host = slicer_box("s", 100, 100, 200, 80, 1)
    plain_text = label_box("u", 110, 110, 60, 20, 3)
    assert find_caption_overlays([plain_host, group, text]) == []
    assert find_caption_overlays([host, group, plain_text]) == []
    # A visible group hides nothing, and a hidden flag reaches nested groups.
    shown = {**group, "isHidden": False}
    assert find_caption_overlays([plain_host, shown, text]) == [("t", "s")]
    inner = {**shown, "name": "inner", "parentGroupName": "g"}
    nested = {**text, "parentGroupName": "inner"}
    assert find_caption_overlays([plain_host, group, inner, nested]) == []


def test_members_of_a_hidden_imported_group_never_take_part() -> None:
    def item(raw: dict[str, Any], **more: Any) -> ImportedVisual:
        return ImportedVisual(
            id=raw["name"],
            title=raw["name"],
            visual_type=raw.get("visual", {}).get("visualType", "group"),
            x=raw["position"]["x"],
            y=raw["position"]["y"],
            width=raw["position"]["width"],
            height=raw["position"]["height"],
            z=raw["position"]["z"],
            definition=raw,
            **more,
        )

    host = item(slicer_box("s", 100, 100, 200, 80, 1))
    group = item(
        {"name": "g", "position": {"x": 0, "y": 0, "width": 900, "height": 900, "z": 5}},
        container_kind="group",
        hidden=True,
        group_children=["t"],
    )
    text = item(label_box("t", 110, 110, 60, 20, 3), parent_group="g")
    assert find_caption_overlays([host, group, text]) == []
    shown = group.model_copy(update={"hidden": False})
    assert find_caption_overlays([host, shown, text]) == [("t", "s")]
