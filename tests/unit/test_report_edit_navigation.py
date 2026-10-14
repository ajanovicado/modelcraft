"""Navigation and interactivity: bookmarks, buttons, navigators, drillthrough and tooltip pages,
edit interactions, synced slicers and drill state (report_edit_navigation).

Every test runs on the structure twin (the Agents dashboard's one page "1") or on multipage.pbix
(bookmarks, a bookmark button, a page-navigation button, a drillthrough and a tooltip page,
interactions, nested groups). Documents are edited in memory and read back through ``commit``;
the fixtures are never written. New names are a counter (``sequential_names``) so the JSON a
test expects is exact. After every step ``navigation_problems`` and the structural validator
must find nothing new.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from ai_powerbi_analyst.studio import edit_tools, report_edits
from ai_powerbi_analyst.studio import report_edit_navigation as nav
from ai_powerbi_analyst.studio.pbir_document import BOOKMARKS_META, DocumentError, PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage
from ai_powerbi_analyst.studio.report_edit_context import EditOutcome, ModelInfo, NeedsConfirmation
from ai_powerbi_analyst.studio.report_edit_validation import (
    capture_baseline,
    validate_document,
    validate_findings,
)
from ai_powerbi_analyst.studio.report_refs import ReferenceIndex
from ai_powerbi_analyst.studio.report_tokens import family_of

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "imported"
REAL = Path.home() / "Downloads" / "Agents Performance - Dashboard.pbix"
PAGE1 = "ReportSection8454c812b60b8bb83b99"
DETAILS = "ReportSection3f1c0d5e7a9b2c4d6e8f"
DRILL = "ReportSectiond2a4c6e8f0b1a3c5e7d9"
TOOLTIP = "ReportSectionb7e9f1a3c5d7e9f1a3b5"
CHANNEL_FOCUS = "Bookmarkc7a5beae9c2a6a44bb3c"  # multipage: "Channel focus", used by a button
DETAILS_VIEW = "Bookmark213a31c763c98e893e32"  # multipage: "Details view"
BOOKMARK_BUTTON = "d6c05c8319906941fe02"  # multipage Details page: opens "Channel focus"
NAV_BUTTON = "eff2d7b04706cc92f1db"  # multipage page 1: goes to Details
# The 'Country' slicer on page 1 (multipage also has a 'Country' slicer on Details, same field).
COUNTRY = "54be5cf0783e66471540"
OUTER_GROUP = "40c896eda14bfa8f6a97"  # multipage Details page: a group holding INNER_GROUP
INNER_GROUP = "7799291338ca3548d7d5"
VC_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
    "visualContainer/2.12.0/schema.json"
)
MODEL = ModelInfo(
    columns={
        "DimEmployee": {"EmployeeKey": "int64"},
        "DimStore": {"StoreName": "object"},
        "DimChannel": {"ChannelName": "object"},
        "Clustered Employees": {"Clustered Employees": "object"},
    },
    measures={"Total Sales": "# Measures"},
)
NAV_OPS = frozenset(nav.NAVIGATION_HANDLERS)


def n(number: int) -> str:
    """The ``number``-th name the patched ``new_name`` hands out."""
    return f"{number:020x}"


@pytest.fixture(autouse=True)
def sequential_names() -> Iterator[None]:
    counter = iter(range(1, 10_000))

    def new_name(self: PbirDocument, used: set[str]) -> str:
        while True:
            candidate = n(next(counter))
            if candidate not in used:
                return candidate

    with mock.patch.object(PbirDocument, "new_name", new_name):
        yield


def open_document(name: str = "multipage") -> PbirDocument:
    return PbirDocument(PbixPackage(FIXTURES / f"{name}.pbix"))


def _extras_ops() -> frozenset[str]:
    """The extras family's operations when it is there (a probe uses group_visuals)."""
    try:
        from ai_powerbi_analyst.studio import report_edit_extras as extras
    except ImportError:  # pragma: no cover - the family has not landed
        return frozenset()
    known = {cls.model_fields["op"].default for cls in report_edits.REPORT_OPERATIONS}
    return frozenset(extras.EXTRAS_HANDLERS) - known


def run(document: PbirDocument, *steps: Mapping[str, Any]) -> EditOutcome:
    """Apply steps in order (navigation ones through the family's dispatcher, the rest through
    report_edits) and check that nothing dangles and nothing new is structurally wrong."""
    baseline = capture_baseline(document)
    outcome = EditOutcome()
    for step in steps:
        if step["op"] in NAV_OPS:
            one = nav.apply_navigation(document, nav.parse_navigation([step]), MODEL)
        elif step["op"] in _extras_ops():
            # A probe groups visuals with the extras family (registered by the integration step).
            from ai_powerbi_analyst.studio import report_edit_extras as extras

            one = extras.apply_extras(document, extras.parse_extras([step]), MODEL)
        else:
            one = report_edits.apply_operations(
                document, report_edits.parse_operations([step]), MODEL
            )
        outcome.summaries.extend(one.summaries)
        outcome.warnings.extend(one.warnings)
        outcome.visible_in.extend(one.visible_in)
        outcome.report_changed = outcome.report_changed or one.report_changed
        assert nav.navigation_problems(document) == []
        assert validate_document(document, baseline=baseline) == []
    return outcome


def refusal(document: PbirDocument, *steps: Mapping[str, Any]) -> str:
    with pytest.raises(DocumentError) as raised:
        run(document, *steps)
    return str(raised.value)


def reopen(document: PbirDocument) -> tuple[list[str], PbirDocument]:
    """Commit into the in-memory package and read it back as a new document."""
    written = document.commit()
    return written, PbirDocument(document.package)


def visual_named(document: PbirDocument, name: str) -> dict[str, Any]:
    return document.visual(name)[1]


def bookmark_named(document: PbirDocument, display: str) -> tuple[str, dict[str, Any]]:
    for key, value in document.bookmarks().items():
        if value.get("displayName") == display:
            return key, value
    raise AssertionError(f"no bookmark {display!r}")


def literal(value: str) -> dict[str, Any]:
    return {"expr": {"Literal": {"Value": value}}}


def page_named(document: PbirDocument, display: str) -> Any:
    return document.page(display)


# --- bookmarks ---------------------------------------------------------------------------------


def test_add_bookmark_writes_the_page_state_desktop_captures() -> None:
    document = open_document("structure-twin")
    cards = list(nav.CARDS)
    outcome = run(
        document, {"op": "add_bookmark", "name": "Charts only", "page": "1", "hide": cards}
    )
    key = "Bookmark" + n(1)
    bookmark = document.bookmark(key)
    containers = bookmark["explorationState"]["sections"][PAGE1]["visualContainers"]
    assert set(containers) == set(document.pages[PAGE1].visuals)
    hidden = {name for name, c in containers.items() if "display" in c["singleVisual"]}
    assert hidden == set(cards)
    assert containers[nav.CARD_AVG] == {
        "singleVisual": {"visualType": "cardVisual", "display": {"mode": "hidden"}}
    }
    assert containers[nav.TABLE] == {"singleVisual": {"visualType": "tableEx"}}
    assert {k: v for k, v in bookmark.items() if k != "explorationState"} == {
        "$schema": nav.BOOKMARK_SCHEMA,
        "displayName": "Charts only",
        "name": key,
        "options": {"suppressData": True},
    }
    # The version Desktop 2.157 saves Studio's bookmarks with.
    assert bookmark["explorationState"]["version"] == "1.0"
    assert bookmark["explorationState"]["activeSection"] == PAGE1
    assert document.bookmarks_metadata == {
        "$schema": nav.BOOKMARKS_METADATA_SCHEMA,
        "items": [{"name": key}],
    }
    assert outcome.summaries == [
        "Added the bookmark 'Charts only' of page '1'; it remembers which visuals are shown "
        "(4 hidden) and the page."
    ]
    # Studio's view mode lists and applies bookmarks (render_navigation).
    assert [(v.where, v.detail) for v in outcome.visible_in] == [("studio", nav._BOOKMARK_VISIBLE)]
    # The page itself is unchanged: bookmarks never touch isHidden.
    assert not any(v.get("isHidden") for v in document.pages[PAGE1].visuals.values())
    written, reopened = reopen(document)
    assert written == [f"Report/definition/bookmarks/{key}.bookmark.json", BOOKMARKS_META]
    assert reopened.bookmark(key) == bookmark
    assert reopened.bookmarks_metadata["items"] == [{"name": key}]


def test_add_bookmark_with_data_captures_filters_slicers_and_selected_visuals() -> None:
    document = open_document()
    run(
        document,
        {
            "op": "add_bookmark",
            "name": "Stores view",
            "page": "1",
            "data": True,
            "display": False,
            "current_page": False,
            "visuals": ["54be5cf0783e66471540", nav.TABLE],
        },
    )
    key, bookmark = bookmark_named(document, "Stores view")
    assert bookmark["options"] == {
        "applyOnlyToTargetVisuals": True,
        "targetVisualNames": ["54be5cf0783e66471540", nav.TABLE],
        "suppressActiveSection": True,
        "suppressDisplay": True,
    }
    state = bookmark["explorationState"]
    report_filter = document.report["filterConfig"]["filters"][0]
    assert state["filters"] == {
        "byExpr": [
            {
                "name": "Filter4713a55be3dfa9b6f20b",
                "type": "Categorical",
                "filter": report_filter["filter"],
                "expression": report_filter["field"],
                "howCreated": 1,
            }
        ]
    }
    section = state["sections"][PAGE1]
    assert [f["name"] for f in section["filters"]["byExpr"]] == ["Filter16d155b792f832600712"]
    table = section["visualContainers"][nav.TABLE]
    assert [f["name"] for f in table["filters"]["byExpr"]] == [
        f["name"] for f in visual_named(document, nav.TABLE)["filterConfig"]["filters"]
    ]
    # Sorted by a user choice (not Power BI's default): the bookmark keeps the order by.
    sort = visual_named(document, nav.TABLE)["visual"]["query"]["sortDefinition"]
    if sort.get("isDefaultSort") is not True:
        assert table["singleVisual"]["orderBy"][0]["Expression"] == sort["sort"][0]["field"]
    chart = section["visualContainers"][nav.CHART_TOTAL]["singleVisual"]
    assert chart["activeProjections"] == {
        "Category": [
            {
                "Column": {
                    "Expression": {"SourceRef": {"Entity": "Clustered Employees"}},
                    "Property": "Clustered Employees",
                }
            }
        ]
    }
    assert "orderBy" not in chart  # its sort is Power BI's default


def test_add_bookmark_refuses_duplicates_and_contradictions() -> None:
    document = open_document()
    assert refusal(document, {"op": "add_bookmark", "name": "channel FOCUS"}) == (
        "There is already a bookmark named 'channel FOCUS'"
    )
    assert refusal(
        document, {"op": "add_bookmark", "name": "X", "display": False, "hide": [nav.TABLE]}
    ) == (
        "Hiding or showing visuals in a bookmark needs Display on: the bookmark would not "
        "remember them otherwise"
    )
    message = refusal(
        document, {"op": "add_bookmark", "name": "X", "page": "1", "hide": ["dfb4645ec076d1209ebe"]}
    )
    assert message.endswith("is on page 'Details', and the bookmark is about page '1'")
    assert refusal(document, {"op": "add_bookmark", "name": "X", "group": "Nope"}) == (
        "There is no bookmark group 'Nope'. Groups: none yet."
    )


def test_add_bookmark_keeps_existing_bookmark_files_untouched() -> None:
    document = open_document()
    run(document, {"op": "add_bookmark", "name": "New view", "page": "Details"})
    written, reopened = reopen(document)
    key = "Bookmark" + n(1)
    assert written == [f"Report/definition/bookmarks/{key}.bookmark.json", BOOKMARKS_META]
    assert [i["name"] for i in reopened.bookmarks_metadata["items"]] == [
        CHANNEL_FOCUS,
        DETAILS_VIEW,
        key,
    ]
    assert set(reopened.bookmarks()) == {CHANNEL_FOCUS, DETAILS_VIEW, key}
    groups = reopened.bookmark(key)["explorationState"]["sections"][DETAILS][
        "visualContainerGroups"
    ]
    # The nested group sits under its parent, as Desktop's exploration state nests them, and
    # each says whether it is hidden (false too: a group without the flag is left as it is).
    assert groups == {
        "40c896eda14bfa8f6a97": {
            "isHidden": False,
            "children": {"7799291338ca3548d7d5": {"isHidden": False}},
        }
    }


def test_update_bookmark_recaptures_the_page_and_changes_options() -> None:
    document = open_document("structure-twin")
    run(document, {"op": "add_bookmark", "name": "View", "page": "1", "hide": [nav.TABLE]})
    run(document, {"op": "add_page", "name": "Other"})
    outcome = run(document, {"op": "update_bookmark", "bookmark": "view", "current_page": False})
    _, bookmark = bookmark_named(document, "View")
    containers = bookmark["explorationState"]["sections"][PAGE1]["visualContainers"]
    # The table hidden in the bookmark only stays hidden: the bookmark is the only record of it.
    assert {k for k, v in containers.items() if "display" in v["singleVisual"]} == {nav.TABLE}
    assert bookmark["options"] == {"suppressActiveSection": True, "suppressData": True}
    assert outcome.summaries == [
        "Updated the bookmark 'View' from page '1' as it is now (the visuals it hid or showed "
        "keep that); it remembers which visuals are shown (1 hidden)."
    ]
    assert outcome.warnings == []
    outcome = run(
        document, {"op": "update_bookmark", "bookmark": "View", "data": True, "recapture": False}
    )
    assert bookmark_named(document, "View")[1]["options"] == {"suppressActiveSection": True}
    assert outcome.summaries == [
        "Changed what the bookmark 'View' remembers; it remembers which visuals are shown "
        "(1 hidden) and filters, slicers and sorts. The page's current filters, slicers and "
        "sorts were captured, since it now remembers data."
    ]


def test_update_bookmark_keeps_what_the_bookmark_hides_and_shows() -> None:
    """Review repro e2: "make the Top agents bookmark keep the filters too" used to recapture
    with nothing hidden, so the four cards the bookmark hid came back without a word."""
    document = open_document("structure-twin")
    run(document, *nav.EXAMPLE_SETUP)
    card = next(c for c in nav.TOOL_CARDS if c.op == "update_bookmark")
    request, example = card.examples[1]
    assert request == "make the Top agents bookmark keep the filters too"
    key, bookmark = bookmark_named(document, "Top agents")
    before = json.loads(json.dumps(bookmark["explorationState"]))
    assert nav._hidden_count(bookmark) == 4
    outcome = run(document, example)
    assert nav._hidden_count(document.bookmark(key)) == 4
    assert outcome.warnings == []
    state = document.bookmark(key)["explorationState"]
    # Only the data half was added: every entry keeps its display, and slicers their selection.
    for name, entry in before["sections"][PAGE1]["visualContainers"].items():
        now = state["sections"][PAGE1]["visualContainers"][name]["singleVisual"]
        assert now.get("display") == entry["singleVisual"].get("display")
    # An explicit recapture keeps them too, and a visual shown by the bookmark while hidden on
    # the page stays shown (a pop-up panel a bookmark opens).
    run(document, {"op": "set_visual_hidden", "visual": nav.TABLE, "hidden": True})
    run(document, {"op": "update_bookmark", "bookmark": "Top agents"})
    containers = document.bookmark(key)["explorationState"]["sections"][PAGE1]["visualContainers"]
    hidden = {k for k, v in containers.items() if "display" in v["singleVisual"]}
    assert hidden == set(nav.CARDS)


def test_update_bookmark_says_when_it_stops_hiding_visuals() -> None:
    document = open_document("structure-twin")
    run(document, {"op": "add_bookmark", "name": "View", "page": "1", "hide": list(nav.CARDS)})
    outcome = run(
        document,
        {"op": "update_bookmark", "bookmark": "View", "recapture": False, "visuals": [nav.TABLE]},
    )
    assert outcome.warnings == [
        "The bookmark no longer hides the 'Agents Avg MTD Sales' card, the 'Number of Agents "
        "Sold above 100K' card, the 'Agents Avg MTD % Change' card and the 'Agents with "
        "Positive Change %' card."
    ]
    outcome = run(document, {"op": "update_bookmark", "bookmark": "View", "visuals": []})
    assert outcome.warnings == []
    outcome = run(
        document,
        {"op": "update_bookmark", "bookmark": "View", "recapture": False, "display": False},
    )
    assert len(outcome.warnings) == 1 and outcome.warnings[0].startswith(
        "The bookmark no longer hides "
    )


def test_rename_bookmark_keeps_the_id_buttons_point_at() -> None:
    document = open_document()
    outcome = run(
        document, {"op": "rename_bookmark", "bookmark": "Channel focus", "name": "By channel"}
    )
    assert outcome.summaries == ["Renamed the bookmark 'Channel focus' to 'By channel'."]
    assert document.bookmark(CHANNEL_FOCUS)["displayName"] == "By channel"
    link = visual_named(document, BOOKMARK_BUTTON)["visual"]["visualContainerObjects"]["visualLink"]
    assert link[0]["properties"]["bookmark"] == literal(f"'{CHANNEL_FOCUS}'")
    assert (
        refusal(
            document, {"op": "rename_bookmark", "bookmark": "By channel", "name": "details VIEW"}
        )
        == "There is already a bookmark named 'details VIEW'"
    )
    assert refusal(document, {"op": "rename_bookmark", "bookmark": "Nope", "name": "X"}) == (
        "There is no bookmark 'Nope'. Bookmarks: 'By channel' and 'Details view'."
    )


def test_group_move_and_ungroup_rewrite_the_bookmark_list() -> None:
    document = open_document()
    run(
        document,
        {"op": "add_bookmark", "name": "Third", "page": "1"},
        {"op": "group_bookmarks", "bookmarks": ["Details view", "Third"], "name": "Views"},
    )
    third, group = "Bookmark" + n(1), "Bookmark" + n(2)
    items = document.bookmarks_metadata["items"]
    assert items == [
        {"name": CHANNEL_FOCUS},
        {"name": group, "displayName": "Views", "children": [DETAILS_VIEW, third]},
    ]
    outcome = run(document, {"op": "move_bookmark", "bookmark": "Third", "position": 1})
    assert outcome.summaries == ["Moved the bookmark 'Third' to position 1."]
    assert document.bookmarks_metadata["items"][1]["children"] == [third, DETAILS_VIEW]
    run(document, {"op": "move_bookmark", "bookmark": "Channel focus", "group": "views"})
    assert document.bookmarks_metadata["items"] == [
        {"name": group, "displayName": "Views", "children": [third, DETAILS_VIEW, CHANNEL_FOCUS]}
    ]
    outcome = run(
        document, {"op": "move_bookmark", "bookmark": "Third", "top_level": True, "position": 1}
    )
    assert outcome.summaries == ["Moved the bookmark 'Third' out of its group to position 1."]
    assert document.bookmarks_metadata["items"] == [
        {"name": third},
        {"name": group, "displayName": "Views", "children": [DETAILS_VIEW, CHANNEL_FOCUS]},
    ]
    assert refusal(document, {"op": "move_bookmark", "bookmark": "Third", "top_level": True}) == (
        "The bookmark 'Third' is not in a group"
    )
    assert refusal(document, {"op": "delete_bookmark", "bookmark": "Views"}) == (
        "'Views' is a group of bookmarks, not a bookmark; name a bookmark inside it, or ungroup it"
    )
    run(
        document,
        {"op": "add_navigator", "page": "1", "kind": "bookmark", "bookmark_group": "Views"},
    )
    navigator = n(3)
    outcome = run(document, {"op": "ungroup_bookmarks", "group": "Views"})
    assert document.bookmarks_metadata["items"] == [
        {"name": third},
        {"name": DETAILS_VIEW},
        {"name": CHANNEL_FOCUS},
    ]
    objects = visual_named(document, navigator)["visual"]["objects"]
    assert objects["bookmarks"] == [{"properties": {"bookmarkGroup": literal("''")}}]
    assert outcome.warnings == [
        "The bookmark navigator at 0, 0 listed that group; it now lists every bookmark."
    ]


def test_moving_the_last_bookmark_out_of_a_group_removes_the_group() -> None:
    document = open_document()
    run(document, {"op": "group_bookmarks", "bookmarks": ["Details view"], "name": "Solo"})
    assert document.bookmarks_metadata["items"][1]["children"] == [DETAILS_VIEW]
    outcome = run(document, {"op": "move_bookmark", "bookmark": "Details view", "top_level": True})
    assert document.bookmarks_metadata["items"] == [{"name": CHANNEL_FOCUS}, {"name": DETAILS_VIEW}]
    assert outcome.summaries == [
        "Moved the bookmark 'Details view' out of its group to position 2; the bookmark group "
        "'Solo' was left empty, so it was removed."
    ]


@pytest.mark.parametrize(
    ("step", "summary"),
    [
        (
            {"op": "delete_bookmark", "bookmark": "Details view"},
            "Deleted the bookmark 'Details view'; the bookmark group 'Solo' was left empty, so "
            "it was removed.",
        ),
        (
            {"op": "move_bookmark", "bookmark": "Details view", "top_level": True},
            "Moved the bookmark 'Details view' out of its group to position 2; the bookmark "
            "group 'Solo' was left empty, so it was removed.",
        ),
        (
            {"op": "group_bookmarks", "bookmarks": ["Details view"], "name": "Other"},
            "Grouped 'Details view' as 'Other'; the bookmark group 'Solo' was left empty, so it "
            "was removed.",
        ),
    ],
    ids=["delete", "move", "regroup"],
)
def test_emptying_a_bookmark_group_points_its_navigators_at_every_bookmark(
    step: Mapping[str, Any], summary: str
) -> None:
    """Review repro b1: the group went, and a navigator kept listing it."""
    document = open_document()
    run(
        document,
        {"op": "group_bookmarks", "bookmarks": ["Details view"], "name": "Solo"},
        {"op": "add_navigator", "page": "1", "kind": "bookmark", "bookmark_group": "Solo"},
    )
    navigator = n(2)
    outcome = run(document, step)  # run() also holds navigation_problems and validation to []
    assert outcome.summaries == [summary]
    assert outcome.warnings == [
        "The bookmark navigator at 0, 0 listed that group; it now lists every bookmark."
    ]
    objects = visual_named(document, navigator)["visual"]["objects"]
    assert objects["bookmarks"] == [{"properties": {"bookmarkGroup": literal("''")}}]
    _, reopened = reopen(document)
    saved = visual_named(reopened, navigator)["visual"]["objects"]["bookmarks"]
    assert saved == [{"properties": {"bookmarkGroup": literal("''")}}]


def test_deleting_a_page_switches_off_what_opens_the_bookmarks_it_takes() -> None:
    """Review repro b2: the page delete removed 'Details view' (it covers only that page) but
    left a button opening it, and a page navigator kept its setting for the deleted page."""
    document = open_document()
    run(
        document,
        {
            "op": "add_button",
            "page": "1",
            "kind": "bookmark",
            "text": "Details",
            "action": {"type": "bookmark", "bookmark": "Details view"},
        },
        {"op": "add_navigator", "page": "1", "kind": "page"},
        {"op": "set_navigator", "visual": "page navigator", "hide_pages": ["Details"]},
    )
    button, navigator = n(1), n(2)
    with pytest.raises(NeedsConfirmation) as asked:
        run(document, {"op": "delete_page", "page": "Details"})
    assert "opens the bookmark 'Details view'" in asked.value.detail
    run(document, {"op": "delete_page", "page": "Details", "cleanup_references": True})
    assert DETAILS_VIEW not in document.bookmarks()
    link = visual_named(document, button)["visual"]["visualContainerObjects"]["visualLink"]
    assert link[0]["properties"]["show"] == literal("false")
    assert "bookmark" not in link[0]["properties"]
    assert "pages" not in visual_named(document, navigator)["visual"].get("objects", {})
    _, reopened = reopen(document)
    assert nav.navigation_problems(reopened) == []


def test_delete_bookmark_used_by_a_button_asks_first_then_switches_the_button_off() -> None:
    document = open_document()
    with pytest.raises(NeedsConfirmation) as asked:
        run(document, {"op": "delete_bookmark", "bookmark": "Channel focus"})
    assert asked.value.kind == "remove_bookmark_references"
    assert asked.value.detail == (
        "The bookmark 'Channel focus' is still used: The action button at 40, 620 on page "
        "'Details' opens it. Deleting it switches that off."
    )
    assert document.bookmark(CHANNEL_FOCUS)  # nothing was written
    outcome = run(
        document,
        {"op": "delete_bookmark", "bookmark": "Channel focus", "cleanup_references": True},
    )
    assert outcome.summaries == [
        "Deleted the bookmark 'Channel focus'. Switched off the action of the action button at "
        "40, 620 on page 'Details'."
    ]
    link = visual_named(document, BOOKMARK_BUTTON)["visual"]["visualContainerObjects"]["visualLink"]
    assert link == [{"properties": {"show": literal("false"), "type": literal("'Bookmark'")}}]
    written, reopened = reopen(document)
    assert f"Report/definition/bookmarks/{CHANNEL_FOCUS}.bookmark.json" not in (
        reopened.package.names()
    )
    assert reopened.bookmarks_metadata["items"] == [{"name": DETAILS_VIEW}]
    assert nav.bookmark_references(reopened, CHANNEL_FOCUS) == []


def test_deleting_the_only_bookmark_removes_the_bookmark_list_as_desktop_does() -> None:
    """Probe bookmark_delete_referenced in Desktop 2.157: an empty bookmarks.json is removed."""
    document = open_document("structure-twin")
    probe = next(p for p in nav.DESKTOP_PROBES if p.name == "bookmark_delete_referenced")
    run(document, *probe.operations)
    written, reopened = reopen(document)
    assert BOOKMARKS_META not in written
    assert not reopened.package.has(BOOKMARKS_META)
    assert reopened.bookmarks() == {}
    parts = {name: data for name, data, _ in reopened.iter_json_parts()}
    assert probe.check is not None and probe.check(parts) == []


def test_hide_in_one_bookmark_only_edits_that_bookmark() -> None:
    document = open_document()
    outcome = run(
        document,
        {"op": "set_bookmark_visual_hidden", "bookmark": "Channel focus", "visual": nav.TABLE},
    )
    bookmark = document.bookmark(CHANNEL_FOCUS)
    containers = bookmark["explorationState"]["sections"][PAGE1]["visualContainers"]
    assert containers[nav.TABLE] == {
        "singleVisual": {"visualType": "tableEx", "display": {"mode": "hidden"}}
    }
    # The bookmark applies to its selected visuals only, so the table joins them.
    assert bookmark["options"]["targetVisualNames"] == [
        "54be5cf0783e66471540",
        nav.CHART_TOTAL,
        nav.TABLE,
    ]
    assert outcome.warnings == [
        "The bookmark applies to selected visuals only; the table of Agent ID was added to them."
    ]
    assert "isHidden" not in visual_named(document, nav.TABLE)
    run(
        document,
        {
            "op": "set_bookmark_visual_hidden",
            "bookmark": "Channel focus",
            "visual": nav.TABLE,
            "hidden": False,
        },
    )
    assert containers[nav.TABLE] == {"singleVisual": {"visualType": "tableEx"}}
    message = refusal(
        document,
        {"op": "set_bookmark_visual_hidden", "bookmark": "Details view", "visual": nav.TABLE},
    )
    assert message == (
        "The bookmark 'Details view' is about another page than the table of Agent ID (page '1')"
    )


def test_editing_a_hand_built_bookmark_keeps_everything_else_in_it() -> None:
    document = open_document()
    original = json.loads(json.dumps(document.bookmark(CHANNEL_FOCUS)))
    run(
        document,
        {"op": "set_bookmark_visual_hidden", "bookmark": "Channel focus", "visual": nav.TABLE},
    )
    written, reopened = reopen(document)
    assert written == [f"Report/definition/bookmarks/{CHANNEL_FOCUS}.bookmark.json"]
    saved = reopened.bookmark(CHANNEL_FOCUS)
    containers = saved["explorationState"]["sections"][PAGE1]["visualContainers"]
    before = original["explorationState"]["sections"][PAGE1]["visualContainers"]
    # The slicer's saved selection (in the fixture's own shape) and the chart entry are kept.
    assert containers[COUNTRY] == before[COUNTRY]
    assert containers[nav.CHART_TOTAL] == before[nav.CHART_TOTAL]
    assert saved["displayName"] == "Channel focus" and saved["$schema"] == original["$schema"]
    untouched = reopened.package.read(f"Report/definition/bookmarks/{DETAILS_VIEW}.bookmark.json")
    assert untouched == open_document().package.read(
        f"Report/definition/bookmarks/{DETAILS_VIEW}.bookmark.json"
    )


def test_hide_a_nested_group_in_a_bookmark_writes_the_group_tree() -> None:
    document = open_document()
    run(
        document,
        {
            "op": "set_bookmark_visual_hidden",
            "bookmark": "Details view",
            "visual": "7799291338ca3548d7d5",
        },
    )
    section = document.bookmark(DETAILS_VIEW)["explorationState"]["sections"][DETAILS]
    assert section["visualContainerGroups"] == {
        "40c896eda14bfa8f6a97": {"children": {"7799291338ca3548d7d5": {"isHidden": True}}}
    }
    # Shown again: false is written, since a group without the flag keeps the page's state.
    run(
        document,
        {
            "op": "set_bookmark_visual_hidden",
            "bookmark": "Details view",
            "visual": "7799291338ca3548d7d5",
            "hidden": False,
        },
    )
    assert section["visualContainerGroups"] == {
        "40c896eda14bfa8f6a97": {"children": {"7799291338ca3548d7d5": {"isHidden": False}}}
    }


def test_a_bookmark_records_every_group_hidden_or_shown() -> None:
    document = open_document()
    outcome = run(
        document,
        {"op": "add_bookmark", "name": "No outer", "page": "Details", "hide": [OUTER_GROUP]},
    )
    _, bookmark = bookmark_named(document, "No outer")
    section = bookmark["explorationState"]["sections"][DETAILS]
    assert section["visualContainerGroups"] == {
        OUTER_GROUP: {"isHidden": True, "children": {INNER_GROUP: {"isHidden": False}}}
    }
    assert outcome.warnings == []
    # Showing a visual inside the hidden group in this bookmark is not enough to see it.
    outcome = run(
        document,
        {
            "op": "set_bookmark_visual_hidden",
            "bookmark": "No outer",
            "visual": INNER_GROUP,
            "hidden": False,
        },
    )
    assert len(outcome.warnings) == 1
    assert outcome.warnings[0].endswith(
        "which the bookmark hides, so it stays out of sight until that group is shown too."
    )


def test_new_bookmarks_are_seen_by_the_reference_index() -> None:
    document = open_document("structure-twin")
    run(document, {"op": "add_bookmark", "name": "V", "page": "1", "visuals": [nav.TABLE]})
    index = ReferenceIndex.build(document)
    uses = [u for u in index.page_uses(PAGE1) if u.kind == "bookmark"]
    assert [u.where for u in uses] == [
        "/explorationState/activeSection",
        f"/explorationState/sections/{PAGE1}",
    ]
    table_uses = {u.where for u in index.visual_uses(nav.TABLE)}
    assert table_uses == {
        f"/explorationState/sections/{PAGE1}/visualContainers/{nav.TABLE}",
        "/options/targetVisualNames/0",
    }


def test_deleting_a_visual_or_page_keeps_new_bookmarks_consistent() -> None:
    document = open_document()
    run(
        document,
        {"op": "add_bookmark", "name": "Page one", "page": "1", "visuals": [nav.TABLE]},
        {"op": "delete_visual", "visual": nav.TABLE},
    )
    _, bookmark = bookmark_named(document, "Page one")
    assert nav.TABLE not in bookmark["explorationState"]["sections"][PAGE1]["visualContainers"]
    assert bookmark["options"]["targetVisualNames"] == []
    run(document, {"op": "add_bookmark", "name": "Details only", "page": "Details"})
    with pytest.raises(NeedsConfirmation):
        run(document, {"op": "delete_page", "page": "Details"})
    run(document, {"op": "delete_page", "page": "Details", "cleanup_references": True})
    assert [b.get("displayName") for b in document.bookmarks().values()] == [
        "Channel focus",
        "Page one",
    ]
    names = [i["name"] for i in document.bookmarks_metadata["items"]]
    assert names == [CHANNEL_FOCUS, "Bookmark" + n(1)]


def test_duplicating_a_page_with_bookmarks_synced_slicers_and_a_binding_stays_consistent() -> None:
    document = open_document()
    run(
        document,
        {"op": "sync_slicer", "visual": "54be5cf0783e66471540", "pages": ["Details"]},
        {"op": "add_bookmark", "name": "Before copy", "page": "1"},
        {"op": "duplicate_page", "page": "1"},
        {"op": "duplicate_page", "page": "Store drillthrough"},
    )
    copy_page = document.page("1 (copy)")
    synced = [
        v for v in copy_page.visuals.values() if v["visual"].get("syncGroup", {}).get("groupName")
    ]
    assert [v["visual"]["syncGroup"]["groupName"] for v in synced] == ["Channel"]
    drill_copy = document.page("Store drillthrough (copy)")
    binding = drill_copy.data["pageBinding"]
    bound = [p["boundFilter"] for p in binding["parameters"]]
    assert bound == [f["name"] for f in drill_copy.data["filterConfig"]["filters"]]
    assert bound != ["Filterf77304be5b57466a8b00"]


# --- buttons and navigators ------------------------------------------------------------------


def test_add_back_button_is_the_button_desktop_inserts() -> None:
    document = open_document("structure-twin")
    outcome = run(document, {"op": "add_button", "page": "1", "kind": "back", "x": 20, "y": 20})
    assert visual_named(document, n(1)) == {
        "$schema": VC_SCHEMA,
        "name": n(1),
        "position": {"x": 20, "y": 20, "z": 15000, "height": 40, "width": 100, "tabOrder": 15000},
        "visual": {
            "visualType": "actionButton",
            "drillFilterOtherVisuals": True,
            "objects": {
                "icon": [
                    {"properties": {"shapeType": literal("'back'")}, "selector": {"id": "default"}}
                ]
            },
            "visualContainerObjects": {
                "visualLink": [{"properties": {"show": literal("true"), "type": literal("'Back'")}}]
            },
        },
    }
    assert outcome.summaries == [
        "Added a back button to page '1' at (20, 20); clicking it will go back."
    ]
    # Power BI only until Studio's renderer draws buttons (report_tokens.family_of says).
    drawn = family_of("actionButton") != "unsupported"
    assert [(v.op, v.where) for v in outcome.visible_in] == [
        ("add_button", "studio" if drawn else "powerbi_only")
    ]


def test_receipts_say_power_bi_only_while_studio_does_not_draw_buttons() -> None:
    document = open_document("structure-twin")
    with mock.patch.object(nav, "family_of", return_value="unsupported"):
        outcome = run(document, {"op": "add_button", "page": "1", "kind": "reset"})
    assert [(v.where, v.detail) for v in outcome.visible_in] == [
        ("powerbi_only", nav._BUTTON_VISIBLE)
    ]


def test_receipts_follow_what_the_view_mode_does() -> None:
    """render_navigation follows back, bookmark, page and drillthrough actions and resolves
    every interaction type; a web address, Q&A and the slicer buttons are Power BI's."""
    document = open_document()
    drawn = family_of("actionButton") != "unsupported"

    def where(*steps: Mapping[str, Any]) -> list[tuple[str, str]]:
        return [(v.where, v.detail) for v in run(document, *steps).visible_in]

    followed = ("studio", nav._ACTION_FOLLOWED) if drawn else ("powerbi_only", nav._BUTTON_VISIBLE)
    assert where({"op": "add_button", "page": "1", "kind": "back"}) == [followed]
    assert (
        where(
            {
                "op": "add_button",
                "page": "1",
                "text": "Web",
                "action": {"type": "web_url", "url": "https://contoso.com/help"},
            }
        )[0][1]
        != nav._ACTION_FOLLOWED
    )
    assert where(
        {"op": "set_button_action", "visual": "Web", "action": {"type": "page", "page": "Details"}}
    ) == [("studio", nav._ACTION_FOLLOWED)]
    assert where(
        {
            "op": "set_button_action",
            "visual": "Web",
            "action": {"type": "web_url", "url": "https://contoso.com/help"},
        }
    ) == [("powerbi_only", nav._ACTION_VISIBLE)]
    for kind in ("filter", "highlight", "none", "default"):
        assert where(
            {
                "op": "set_interaction",
                "source": nav.CHART_TOTAL,
                "targets": [nav.CHART_AVG],
                "type": kind,
            }
        ) == [("studio", "Studio's canvas applies it too.")]
    assert [w for w, _ in where({"op": "reset_interactions", "page": "1"})] == ["studio"]
    assert [
        w for w, _ in where({"op": "rename_bookmark", "bookmark": "Channel focus", "name": "C"})
    ] == ["studio"]


def test_apply_all_and_clear_all_slicer_buttons_are_the_ones_desktop_inserts() -> None:
    document = open_document("structure-twin")
    outcome = run(
        document, {"op": "add_button", "page": "1", "kind": "apply_all_slicers", "x": 20, "y": 20}
    )
    body = visual_named(document, n(1))["visual"]
    assert visual_named(document, n(1))["position"]["width"] == 200
    assert visual_named(document, n(1))["position"]["height"] == 50
    assert body["objects"] == {
        "icon": [
            {
                "properties": {"shapeType": literal("'applyAllSlicers'")},
                "selector": {"id": "default"},
            },
            {"properties": {"show": literal("false")}},
        ],
        "text": [
            {"properties": {"show": literal("true")}},
            {
                "properties": {
                    "text": literal("'Apply all slicers'"),
                    "horizontalAlignment": literal("'center'"),
                },
                "selector": {"id": "default"},
            },
        ],
    }
    assert body["visualContainerObjects"] == {
        "visualLink": [
            {
                "properties": {
                    "show": literal("true"),
                    "type": literal("'ApplyAllSlicers'"),
                    "tooltipPlaceholderText": literal(
                        "'Apply all slicers on this page. Hiding all buttons will cause slicers "
                        "to instantly apply on this page.'"
                    ),
                }
            }
        ]
    }
    assert outcome.summaries == [
        "Added an 'Apply all slicers' button to page '1' at (20, 20); clicking it will apply all "
        "slicers on the page."
    ]
    outcome = run(
        document,
        {"op": "add_button", "page": "1", "kind": "clear_all_slicers", "text": "Reset filters"},
    )
    link = visual_named(document, n(2))["visual"]["visualContainerObjects"]["visualLink"]
    assert link[0]["properties"]["type"] == literal("'ClearAllSlicers'")
    assert outcome.summaries[0].startswith("Added a 'Clear all slicers' button to page '1'")
    assert (
        refusal(
            document,
            {
                "op": "add_button",
                "page": "1",
                "kind": "apply_all_slicers",
                "action": {"type": "back"},
            },
        )
        == "An 'Apply all slicers' button only does that; add a blank button for another action"
    )


def test_deleting_a_page_a_new_button_goes_to_asks_first() -> None:
    document = open_document()
    run(
        document,
        {
            "op": "add_button",
            "page": "1",
            "text": "Stores",
            "action": {"type": "page", "page": "Store drillthrough"},
        },
    )
    with pytest.raises(NeedsConfirmation) as asked:
        run(document, {"op": "delete_page", "page": "Store drillthrough"})
    assert asked.value.kind == "remove_page_references"
    assert "button 'Stores' navigates to this page" in asked.value.detail
    run(document, {"op": "delete_page", "page": "Store drillthrough", "cleanup_references": True})
    assert "visualLink" not in visual_named(document, n(1))["visual"]["visualContainerObjects"]


def test_add_button_with_text_page_action_and_tooltip() -> None:
    document = open_document()
    outcome = run(
        document,
        {
            "op": "add_button",
            "page": "1",
            "kind": "right_arrow",
            "text": "Stores",
            "action": {"type": "page", "page": "Store drillthrough"},
            "tooltip": "See one store",
            "x": 20,
            "y": 1100,
        },
    )
    body = visual_named(document, n(1))["visual"]
    assert body["objects"] == {
        "icon": [
            {"properties": {"shapeType": literal("'rightArrow'")}, "selector": {"id": "default"}}
        ],
        "text": [
            {"properties": {"show": literal("true")}},
            {"properties": {"text": literal("'Stores'")}, "selector": {"id": "default"}},
        ],
    }
    assert body["visualContainerObjects"]["visualLink"] == [
        {
            "properties": {
                "show": literal("true"),
                "type": literal("'PageNavigation'"),
                "navigationSection": literal(f"'{DRILL}'"),
                "tooltip": literal("'See one store'"),
            }
        }
    ]
    assert outcome.summaries == [
        "Added a right arrow button to page '1' at (20, 1100) saying 'Stores'; clicking it will "
        "go to page 'Store drillthrough'."
    ]
    # The button is found by its words from now on.
    assert nav._find_visual(document, "stores")[1]["name"] == n(1)


def test_button_actions_replace_the_old_destination() -> None:
    document = open_document()
    run(
        document,
        {
            "op": "set_button_action",
            "visual": NAV_BUTTON,
            "action": {"type": "bookmark", "bookmark": "Details view"},
        },
    )
    link = visual_named(document, NAV_BUTTON)["visual"]["visualContainerObjects"]["visualLink"]
    assert link == [
        {
            "properties": {
                "show": literal("true"),
                "type": literal("'Bookmark'"),
                "bookmark": literal(f"'{DETAILS_VIEW}'"),
            }
        }
    ]
    run(
        document,
        {
            "op": "set_button_action",
            "visual": NAV_BUTTON,
            "action": {"type": "drillthrough", "page": "Store drillthrough"},
        },
    )
    assert link[0]["properties"] == {
        "show": literal("true"),
        "type": literal("'Drillthrough'"),
        "drillthroughSection": literal(f"'{DRILL}'"),
    }
    run(
        document,
        {
            "op": "set_button_action",
            "visual": NAV_BUTTON,
            "action": {"type": "web_url", "url": " https://example.com/a?b=1 "},
            "tooltip": "Help",
        },
    )
    assert link[0]["properties"] == {
        "show": literal("true"),
        "type": literal("'WebUrl'"),
        "webUrl": literal("'https://example.com/a?b=1'"),
        "tooltip": literal("'Help'"),
    }
    run(document, {"op": "set_button_action", "visual": NAV_BUTTON, "action": {"type": "none"}})
    assert link[0]["properties"] == {"show": literal("false"), "tooltip": literal("'Help'")}


def test_button_action_refusals() -> None:
    document = open_document()
    assert (
        refusal(
            document,
            {
                "op": "set_button_action",
                "visual": NAV_BUTTON,
                "action": {"type": "drillthrough", "page": "Details"},
            },
        )
        == "Page 'Details' is not a drillthrough page; make it one first "
        "(set its drillthrough fields)"
    )
    assert (
        refusal(
            document,
            {
                "op": "set_button_action",
                "visual": NAV_BUTTON,
                "action": {"type": "web_url", "url": "ftp://x"},
            },
        )
        == "'ftp://x' is not a web address; a web link starts with https:// (or http://)"
    )
    assert (
        refusal(
            document,
            {
                "op": "set_button_action",
                "visual": "81ab76274a5b4e9e62da",
                "action": {"type": "drillthrough", "page": "Store drillthrough"},
            },
        )
        == "Only a button or a shape can drill through; add a button for it instead"
    )
    assert (
        refusal(
            document, {"op": "set_button_action", "visual": nav.TABLE, "action": {"type": "back"}}
        )
        == "The table of Agent ID cannot have an action; only buttons, shapes and pictures can"
    )
    assert (
        refusal(document, {"op": "add_button", "page": "1", "kind": "blank", "tooltip": "Hi"})
        == nav._NEEDS_ACTION
    )
    assert (
        refusal(
            document,
            {"op": "set_button_action", "visual": NAV_BUTTON, "action": {"type": "bookmark"}},
        )
        == "Say which bookmark the button opens"
    )


def test_navigators_and_their_options() -> None:
    document = open_document()
    run(
        document,
        {"op": "group_bookmarks", "bookmarks": ["Channel focus"], "name": "Main"},
        {"op": "add_navigator", "page": "Details", "kind": "page", "x": 40, "y": 300},
        {
            "op": "add_navigator",
            "page": "Details",
            "kind": "bookmark",
            "bookmark_group": "Main",
            "x": 40,
            "y": 400,
            "rows": 2,
            "columns": 3,
        },
    )
    pages_nav = visual_named(document, n(2))
    assert pages_nav["position"]["width"] == 800  # 200 per page (4 pages)
    assert pages_nav["position"]["height"] == 50
    assert "objects" not in pages_nav["visual"]  # Desktop inserts a bare navigator
    bookmarks_nav = visual_named(document, n(3))["visual"]
    assert bookmarks_nav["visualType"] == "bookmarkNavigator"
    assert bookmarks_nav["objects"] == {
        "layout": [
            {
                "properties": {
                    "orientation": literal("0D"),
                    "rowCount": literal("2L"),
                    "columnCount": literal("3L"),
                }
            }
        ],
        "bookmarks": [{"properties": {"bookmarkGroup": literal(f"'Bookmark{n(1)}'")}}],
    }
    outcome = run(
        document,
        {
            "op": "set_navigator",
            "visual": "page navigator",
            "orientation": "vertical",
            "show_tooltip_pages": True,
            "hide_pages": ["Store drillthrough"],
            "show_pages": ["Sales tooltip"],
        },
    )
    assert visual_named(document, n(2))["visual"]["objects"] == {
        "layout": [{"properties": {"orientation": literal("1D")}}],
        "pages": [
            {"properties": {"showTooltipPages": literal("true")}},
            {"properties": {"showPage": literal("true")}, "selector": {"id": TOOLTIP}},
            {"properties": {"showPage": literal("false")}, "selector": {"id": DRILL}},
        ],
    }
    assert outcome.summaries == [
        "Set the page navigator at 40, 300 to: vertical layout, shows tooltip pages, lists page "
        "'Sales tooltip' and leaves out page 'Store drillthrough'."
    ]
    assert refusal(
        document, {"op": "set_navigator", "visual": "page navigator", "bookmark_group": "Main"}
    ) == ("Only a bookmark navigator lists a bookmark group")
    assert refusal(
        document,
        {
            "op": "set_navigator",
            "visual": "bookmark navigator",
            "orientation": "vertical",
            "rows": 2,
        },
    ) == ("Rows and columns only apply to a grid layout")


# --- drillthrough and tooltip pages -------------------------------------------------------------


def test_set_drillthrough_writes_binding_filter_and_back_button() -> None:
    document = open_document("structure-twin")
    outcome = run(
        document,
        {"op": "add_page", "name": "Agent"},
        {
            "op": "set_drillthrough",
            "page": "Agent",
            "fields": [{"name": "EmployeeKey"}],
            "keep_all_filters": False,
        },
    )
    page = document.page("Agent")
    field = {
        "Column": {
            "Expression": {"SourceRef": {"Entity": "DimEmployee"}},
            "Property": "EmployeeKey",
        }
    }
    filter_name = "Filter" + n(3)
    assert page.data["type"] == "Drillthrough"
    assert page.data["pageBinding"] == {
        "name": n(2),
        "type": "Drillthrough",
        "parameters": [{"name": n(4), "boundFilter": filter_name, "fieldExpr": field}],
        "acceptsFilterContext": "None",
    }
    assert page.data["filterConfig"] == {
        "filters": [
            {
                "name": filter_name,
                "field": field,
                "type": "Categorical",
                "howCreated": "Drillthrough",
            }
        ]
    }
    back = page.visuals[n(5)]
    assert back["position"] == {"x": 0, "y": 0, "z": 0, "height": 40, "width": 100, "tabOrder": 0}
    assert back["visual"]["visualContainerObjects"]["visualLink"] == [
        {"properties": {"show": literal("true"), "type": literal("'Back'")}}
    ]
    assert outcome.summaries[1] == (
        "Page 'Agent' is a drillthrough page (added DimEmployee[EmployeeKey]); right-click a "
        "data point of DimEmployee[EmployeeKey] to drill through to it; it keeps only the "
        "drillthrough filters; a Back button was added at its top left."
    )
    # A second call adds a field and no second Back button.
    run(
        document,
        {
            "op": "set_drillthrough",
            "page": "Agent",
            "fields": [{"table": "dimstore", "name": "storename"}],
            "mode": "add",
            "keep_all_filters": True,
        },
    )
    assert len(page.visuals) == 1
    params = page.data["pageBinding"]["parameters"]
    assert [p["fieldExpr"]["Column"]["Property"] for p in params] == ["EmployeeKey", "StoreName"]
    assert "acceptsFilterContext" not in page.data["pageBinding"]
    outcome = run(
        document,
        {
            "op": "set_drillthrough",
            "page": "Agent",
            "fields": [{"name": "StoreName"}],
            "mode": "replace",
        },
    )
    assert [p["boundFilter"] for p in page.data["pageBinding"]["parameters"]] == [
        f["name"] for f in page.data["filterConfig"]["filters"]
    ]
    assert outcome.summaries == [
        "Page 'Agent' is a drillthrough page (removed DimEmployee[EmployeeKey]); right-click a "
        "data point of DimStore[StoreName] to drill through to it; it keeps all filters."
    ]
    outcome = run(
        document,
        {
            "op": "set_drillthrough",
            "page": "Agent",
            "fields": [{"name": "StoreName"}],
            "mode": "remove",
        },
    )
    assert "pageBinding" not in page.data and "filterConfig" not in page.data
    assert page.data["type"] == "Drillthrough"
    assert outcome.warnings == [
        "Page 'Agent' has no drillthrough fields left, so nothing opens it; make it a normal "
        "page if it should not be a drillthrough page."
    ]


def test_set_drillthrough_refusals() -> None:
    document = open_document()
    assert refusal(
        document,
        {"op": "set_drillthrough", "page": "Sales tooltip", "fields": [{"name": "StoreName"}]},
    ) == (
        "Page 'Sales tooltip' is a tooltip page; make it a normal page first, or use another "
        "page for drillthrough"
    )
    assert (
        refusal(
            document,
            {"op": "set_drillthrough", "page": "Details", "fields": [{"name": "Total Sales"}]},
        )
        == "Total Sales is a measure; a drillthrough page needs a column"
    )
    assert (
        refusal(
            document,
            {
                "op": "set_drillthrough",
                "page": "Store drillthrough",
                "fields": [{"name": "ChannelName"}],
                "mode": "remove",
            },
        )
        == "Page 'Store drillthrough' has no field DimChannel[ChannelName]"
    )


def test_drillthrough_fields_without_a_model_are_taken_as_given() -> None:
    document = open_document("structure-twin")
    run(document, {"op": "add_page", "name": "P"})
    outcome = nav.apply_navigation(
        document,
        nav.parse_navigation(
            [{"op": "set_drillthrough", "page": "P", "fields": [{"table": "T", "name": "C"}]}]
        ),
        ModelInfo(),
    )
    assert outcome.warnings == [
        "Studio could not check the fields against the data model; check them in Power BI."
    ]
    with pytest.raises(DocumentError, match="Say which table 'C' is in"):
        nav.apply_navigation(
            document,
            nav.parse_navigation(
                [{"op": "set_drillthrough", "page": "P", "fields": [{"name": "C"}]}]
            ),
            ModelInfo(),
        )


def test_tooltip_page_and_visual_tooltip() -> None:
    document = open_document("structure-twin")
    outcome = run(
        document,
        {"op": "add_page", "name": "Tip"},
        {"op": "set_tooltip_page", "page": "Tip", "fields": [{"name": "Clustered Employees"}]},
        {"op": "set_visual_tooltip", "visual": nav.CHART_TOTAL, "page": "Tip"},
    )
    page = document.page("Tip")
    assert (page.data["width"], page.data["height"]) == (320, 240)
    assert page.data["visibility"] == "HiddenInViewMode"
    assert page.data["type"] == "Tooltip"
    assert page.data["pageBinding"]["type"] == "Tooltip"
    assert page.data["filterConfig"]["filters"][0]["howCreated"] == "Drillthrough"
    tooltip = visual_named(document, nav.CHART_TOTAL)["visual"]["visualContainerObjects"][
        "visualTooltip"
    ]
    assert tooltip == [
        {
            "properties": {
                "show": literal("true"),
                "type": literal("'Canvas'"),
                "section": literal(f"'{page.name}'"),
            }
        }
    ]
    assert outcome.summaries[1:] == [
        "Page 'Tip' is a tooltip page (320 x 240, hidden from readers and shown for "
        "Clustered Employees[Clustered Employees]); point a visual's tooltip at it to use it.",
        "Hovering the 'Total MTD Sales' stacked column chart now shows page 'Tip' as its tooltip.",
    ]
    run(document, {"op": "set_visual_tooltip", "visual": nav.CHART_TOTAL})
    assert (
        "visualTooltip"
        not in visual_named(document, nav.CHART_TOTAL)["visual"]["visualContainerObjects"]
    )
    assert refusal(document, {"op": "set_visual_tooltip", "visual": nav.TABLE, "page": "1"}) == (
        "Page '1' is not a tooltip page; make it one first"
    )
    assert refusal(document, {"op": "set_tooltip_page", "page": "1"}) == (
        "A 320 x 240 tooltip page cannot hold the table of Agent ID; the slicer of Top-Bottom-N; "
        "the 'Top/Bottom Field' button slicer; the 'Agents with Positive Change %' card; the "
        "'Agents Avg MTD Sales' card; the 'Agents Avg MTD % Change' card; and 9 more. Move or "
        "resize them first, or keep the page's size"
    )


def test_hand_built_report_page_tooltip_is_rewritten_as_desktop_writes_it() -> None:
    document = open_document()
    run(document, {"op": "set_visual_tooltip", "visual": nav.CHART_TOTAL, "page": "Sales tooltip"})
    tooltip = visual_named(document, nav.CHART_TOTAL)["visual"]["visualContainerObjects"][
        "visualTooltip"
    ]
    assert tooltip[0]["properties"]["type"] == literal("'Canvas'")


def test_clear_page_type_asks_when_used_then_switches_the_users_off() -> None:
    document = open_document()
    with pytest.raises(NeedsConfirmation) as asked:
        run(document, {"op": "clear_page_type", "page": "Sales tooltip"})
    assert asked.value.kind == "clear_page_links"
    assert asked.value.detail == (
        "Page 'Sales tooltip' is still used: The 'Total MTD Sales' stacked column chart on page "
        "'1' uses it as its tooltip. Making it a normal page switches that off."
    )
    outcome = run(
        document, {"op": "clear_page_type", "page": "Sales tooltip", "cleanup_references": True}
    )
    page = document.page("Sales tooltip")
    assert "type" not in page.data and "pageBinding" not in page.data
    assert "filterConfig" not in page.data  # its bound filter went with its field
    assert outcome.summaries == [
        "Page 'Sales tooltip' is a normal page again (it was a tooltip page); the 'Total MTD "
        "Sales' stacked column chart shows the default tooltip again."
    ]
    assert outcome.warnings == [
        "Page 'Sales tooltip' is still hidden from readers; show it if they should see it."
    ]
    containers = visual_named(document, nav.CHART_TOTAL)["visual"]["visualContainerObjects"]
    assert "visualTooltip" not in containers and "title" in containers
    run(
        document,
        {
            "op": "set_button_action",
            "visual": NAV_BUTTON,
            "action": {"type": "drillthrough", "page": "Store drillthrough"},
        },
    )
    run(
        document,
        {"op": "clear_page_type", "page": "Store drillthrough", "cleanup_references": True},
    )
    link = visual_named(document, NAV_BUTTON)["visual"]["visualContainerObjects"]["visualLink"]
    assert link == [{"properties": {"show": literal("false")}}]
    assert run(document, {"op": "clear_page_type", "page": "Details"}).summaries == [
        "Page 'Details' is already a normal page."
    ]


# --- interactions ------------------------------------------------------------------------------


def test_interactions_are_written_per_source_and_target() -> None:
    document = open_document()
    before = list(document.page("1").data["visualInteractions"])
    outcome = run(
        document,
        {
            "op": "set_interaction",
            "source": nav.CHART_TOTAL,
            "targets": [nav.CHART_AVG, nav.CHART_CHANGE],
            "type": "highlight",
        },
        {
            "op": "set_interaction",
            "source": nav.CHART_TOTAL,
            "targets": [nav.CHART_AVG],
            "type": "filter",
        },
        {
            "op": "set_interaction",
            "source": COUNTRY,
            "targets": [nav.CHART_TOTAL],
            "type": "default",
        },
    )
    items = document.page("1").data["visualInteractions"]
    assert items == [
        before[1],  # the fixture's own DataFilter entry is kept
        {"source": nav.CHART_TOTAL, "target": nav.CHART_CHANGE, "type": "HighlightFilter"},
        {"source": nav.CHART_TOTAL, "target": nav.CHART_AVG, "type": "DataFilter"},
    ]
    assert outcome.summaries[2] == (
        "The 'Total MTD Sales' stacked column chart reacts the default way to selections in the "
        "'Country' slicer."
    )
    # render_navigation.interaction resolves every type, so the canvas applies all three.
    assert [v.where for v in outcome.visible_in] == ["studio", "studio", "studio"]
    assert refusal(
        document,
        {
            "op": "set_interaction",
            "source": nav.CHART_TOTAL,
            "targets": [nav.TABLE],
            "type": "highlight",
        },
    ) == (
        "The table of Agent ID cannot highlight; Power BI filters them instead. Choose filter or "
        "none for them"
    )
    assert refusal(
        document,
        {
            "op": "set_interaction",
            "source": nav.CHART_TOTAL,
            "targets": ["Details view"],
            "type": "none",
        },
    ).startswith("There is no visual 'Details view' in this report.")
    outcome = run(document, {"op": "set_interaction", "source": nav.CHART_TOTAL, "type": "none"})
    targets = {
        i["target"]
        for i in document.page("1").data["visualInteractions"]
        if i["source"] == nav.CHART_TOTAL
    }
    # every other data visual of the page, not the page navigation button
    assert NAV_BUTTON not in targets and nav.TABLE in targets and len(targets) == 14
    outcome = run(document, {"op": "reset_interactions", "visual": nav.CHART_TOTAL})
    assert outcome.summaries == [
        "Reset 14 interaction settings: every visual reacts the default way to selections in "
        "the 'Total MTD Sales' stacked column chart."
    ]
    run(document, {"op": "reset_interactions", "page": "1"})
    assert "visualInteractions" not in document.page("1").data
    assert refusal(document, {"op": "reset_interactions"}) == (
        "Say either which visual's interactions to reset, or which page's"
    )


# --- sync slicers -------------------------------------------------------------------------------


def test_sync_slicer_copies_joins_and_hides() -> None:
    document = open_document()
    outcome = run(
        document,
        {"op": "add_page", "name": "Extra"},
        {"op": "sync_slicer", "visual": COUNTRY, "pages": ["Details"], "hidden_on": ["Extra"]},
    )
    group = {"groupName": "Channel", "fieldChanges": True, "filterChanges": True}
    source = visual_named(document, "54be5cf0783e66471540")
    assert source["visual"]["syncGroup"] == group
    joined = visual_named(document, "2b5b8e3a7ed15138a983")  # Details page, same field
    assert joined["visual"]["syncGroup"] == group
    extra = document.page("Extra")
    [(copy_name, copied)] = extra.visuals.items()
    assert copied["isHidden"] is True
    assert copied["visual"]["syncGroup"] == group
    assert copied["position"] == {
        "x": 1280 - 210.44776119402985,
        "y": 40,
        "z": 0,
        "height": 60,
        "width": 210.44776119402985,
        "tabOrder": 0,
    }
    assert {k: v for k, v in copied.items() if k not in {"name", "position", "isHidden"}} == {
        k: v for k, v in json.loads(json.dumps(source)).items() if k not in {"name", "position"}
    }
    assert outcome.summaries[1] == (
        "The 'Country' slicer syncs as 'Channel'; added a synced copy to page 'Extra' (hidden "
        "there) and joined the matching slicer on page 'Details'."
    )
    assert [v.where for v in outcome.visible_in] == ["studio"]  # the copy is a slicer Studio draws
    # A second group on the same field gets the next name, as Desktop numbers them.
    run(document, {"op": "sync_slicer", "visual": "04ba32659627f6e8c7d7", "pages": ["1"]})
    store = visual_named(document, "5b0dbccd61c6c4adb415")["visual"]["syncGroup"]
    assert store["groupName"] == "Store"
    assert refusal(document, {"op": "sync_slicer", "visual": nav.TABLE, "pages": ["Extra"]}) == (
        "The table of Agent ID is not a slicer; only slicers sync"
    )
    assert (
        refusal(
            document,
            {"op": "sync_slicer", "visual": COUNTRY, "pages": ["Extra"], "hidden_on": ["Extra"]},
        )
        == "A page cannot both show the synced slicer and hide it"
    )


def test_unsync_slicer_leaves_or_deletes_the_copies() -> None:
    document = open_document()
    run(
        document,
        {"op": "add_page", "name": "Extra"},
        {"op": "sync_slicer", "visual": COUNTRY, "pages": ["Details", "Extra"]},
    )
    copy_name = next(iter(document.page("Extra").visuals))
    outcome = run(
        document,
        {"op": "unsync_slicer", "visual": COUNTRY, "pages": ["Extra"], "delete_copies": True},
    )
    assert copy_name not in document.page("Extra").visuals
    assert outcome.summaries == ["Sync group 'Channel': deleted the synced copies on 'Extra'."]
    run(document, {"op": "unsync_slicer", "visual": COUNTRY, "pages": ["Details"]})
    # A group of one syncs nothing, so the last slicer leaves it too.
    assert "syncGroup" not in visual_named(document, "54be5cf0783e66471540")["visual"]
    assert "syncGroup" not in visual_named(document, "2b5b8e3a7ed15138a983")["visual"]
    assert run(document, {"op": "unsync_slicer", "visual": COUNTRY}).summaries == [
        "The 'Country' slicer is not synced with other pages."
    ]


# --- drill ---------------------------------------------------------------------------------------


def test_drill_filter_setting_for_a_visual_and_the_report() -> None:
    document = open_document("structure-twin")
    outcome = run(
        document,
        {"op": "set_drill_filter", "visual": nav.CHART_TOTAL, "filter_other_visuals": False},
        {"op": "set_drill_filter", "filter_other_visuals": False},
    )
    # Off is the key left out: Desktop drops a false one when it saves.
    assert "drillFilterOtherVisuals" not in visual_named(document, nav.CHART_TOTAL)["visual"]
    assert "defaultDrillFilterOtherVisuals" not in document.report.get("settings", {})
    assert document.report["settings"]  # the report's other settings stay
    assert outcome.summaries == [
        "Drilling in the 'Total MTD Sales' stacked column chart now does not filter the other "
        "visuals.",
        "By default, drilling in a visual now does not filter the other visuals of its page "
        "(visuals with their own setting keep it).",
    ]
    _, reopened = reopen(document)
    assert "drillFilterOtherVisuals" not in visual_named(reopened, nav.CHART_TOTAL)["visual"]
    assert "defaultDrillFilterOtherVisuals" not in reopened.report.get("settings", {})
    run(
        document,
        {"op": "set_drill_filter", "visual": nav.CHART_TOTAL, "filter_other_visuals": True},
        {"op": "set_drill_filter", "filter_other_visuals": True},
    )
    assert visual_named(document, nav.CHART_TOTAL)["visual"]["drillFilterOtherVisuals"] is True
    assert document.report["settings"]["defaultDrillFilterOtherVisuals"] is True


def test_drill_visual_moves_the_active_levels() -> None:
    document = open_document("structure-twin")
    run(
        document,
        {
            "op": "set_fields",
            "visual": nav.CHART_TOTAL,
            "role": "Category",
            "mode": "add",
            "fields": [
                {"table": "DimChannel", "name": "ChannelName"},
                {"table": "DimStore", "name": "StoreName"},
            ],
        },
    )
    projections = visual_named(document, nav.CHART_TOTAL)["visual"]["query"]["queryState"][
        "Category"
    ]["projections"]

    def flags() -> list[Any]:
        return [p.get("active") for p in projections]

    outcome = run(
        document, {"op": "drill_visual", "visual": nav.CHART_TOTAL, "action": "next_level"}
    )
    assert flags() == [False, True, False]
    assert outcome.summaries == [
        "The 'Total MTD Sales' stacked column chart now shows the next level: ChannelName."
    ]
    run(document, {"op": "drill_visual", "visual": nav.CHART_TOTAL, "action": "expand_all"})
    assert flags() == [True, True, True]
    run(document, {"op": "drill_visual", "visual": nav.CHART_TOTAL, "action": "up"})
    assert flags() == [True, True, False]
    run(document, {"op": "drill_visual", "visual": nav.CHART_TOTAL, "action": "up"})
    assert flags() == [True, False, False]
    assert refusal(document, {"op": "drill_visual", "visual": nav.CHART_TOTAL, "action": "up"}) == (
        "The 'Total MTD Sales' stacked column chart already shows its top level"
    )
    run(
        document,
        {"op": "drill_visual", "visual": nav.CHART_TOTAL, "action": "next_level"},
        {"op": "drill_visual", "visual": nav.CHART_TOTAL, "action": "next_level"},
    )
    assert flags() == [False, False, True]
    assert refusal(
        document, {"op": "drill_visual", "visual": nav.CHART_TOTAL, "action": "next_level"}
    ) == ("The 'Total MTD Sales' stacked column chart already shows its last level")
    run(document, {"op": "drill_visual", "visual": nav.CHART_TOTAL, "action": "top"})
    assert flags() == [True, False, False]
    assert refusal(
        document, {"op": "drill_visual", "visual": nav.CHART_AVG, "action": "next_level"}
    ) == (
        "This visual has no hierarchy to drill on: put two or more fields in its axis (or rows) "
        "first"
    )
    assert refusal(
        document, {"op": "drill_visual", "visual": nav.TABLE, "action": "next_level"}
    ) == ("The table of Agent ID cannot be drilled")


def test_drill_visual_never_drills_a_well_of_measures() -> None:
    """Review repro b3: with one axis field and two measures, drilling fell back to the
    measures' well and wrote drill flags on them."""
    document = open_document("structure-twin")
    state = visual_named(document, nav.CHART_TOTAL)["visual"]["query"]["queryState"]
    values = next(role for role in state if role != "Category")
    run(
        document,
        {
            "op": "set_fields",
            "visual": nav.CHART_TOTAL,
            "role": values,
            "mode": "add",
            "fields": [{"table": "# Measures", "name": "Total Sales"}],
        },
    )
    assert len(state[values]["projections"]) == 2
    assert refusal(
        document, {"op": "drill_visual", "visual": nav.CHART_TOTAL, "action": "next_level"}
    ) == (
        "This visual has no hierarchy to drill on: put two or more fields in its axis (or rows) "
        "first"
    )
    assert refusal(
        document,
        {"op": "drill_visual", "visual": nav.CHART_TOTAL, "action": "next_level", "role": values},
    ) == (
        f"The {values} well holds values (measures or summarized columns), not levels, so it "
        "cannot be drilled; drill on the axis or rows"
    )
    assert all("active" not in p for p in state[values]["projections"])


# --- parsing and the dispatcher -----------------------------------------------------------------


def test_parse_navigation_names_the_bad_step() -> None:
    with pytest.raises(DocumentError) as raised:
        nav.parse_navigation(
            [
                {"op": "add_bookmark", "name": "A"},
                {"op": "add_button", "page": "1", "kind": "rocket"},
            ]
        )
    assert str(raised.value).startswith("Step 2 (add_button): kind: Input should be ")


def test_a_failing_step_is_numbered_and_confirmations_keep_their_step() -> None:
    document = open_document()
    ops = nav.parse_navigation(
        [
            {"op": "add_bookmark", "name": "A"},
            {"op": "rename_bookmark", "bookmark": "Nope", "name": "B"},
        ]
    )
    with pytest.raises(DocumentError, match=r"^Step 2: There is no bookmark 'Nope'"):
        nav.apply_navigation(document, ops, MODEL)
    ops = nav.parse_navigation(
        [
            {"op": "add_bookmark", "name": "A2"},
            {"op": "delete_bookmark", "bookmark": "Channel focus"},
        ]
    )
    with pytest.raises(NeedsConfirmation) as asked:
        nav.apply_navigation(document, ops, MODEL)
    assert asked.value.step == 2


def test_the_family_tables_agree() -> None:
    ops = {cls.model_fields["op"].default for cls in nav.NAVIGATION_OPERATIONS}
    assert ops == set(nav.NAVIGATION_HANDLERS)
    assert len(ops) == 22
    existing = {cls.model_fields["op"].default for cls in report_edits.REPORT_OPERATIONS}
    assert ops <= existing  # registered in the facade (round 3 integration)
    assert {card.op for card in nav.TOOL_CARDS} == ops


# --- tool cards and probes --------------------------------------------------------------------


def test_navigation_findings_name_the_part_and_place_of_each_dangling_reference() -> None:
    document = open_document()
    assert nav.navigation_findings(document) == []
    run(document, {"op": "add_navigator", "page": "1", "kind": "page"})
    # A nested group a bookmark remembers that is no longer on the page (review repro e3).
    section = document.bookmark(DETAILS_VIEW)["explorationState"]["sections"][DETAILS]
    section["visualContainerGroups"] = {
        OUTER_GROUP: {"isHidden": False, "children": {"gone": {"isHidden": True}}}
    }
    # A page navigator's own setting for a page that is gone (review repro b2).
    navigator = visual_named(document, n(1))
    navigator["visual"]["objects"] = {
        "pages": [
            {"properties": {"showPage": literal("false")}, "selector": {"id": "ReportSectionGone"}}
        ]
    }
    found = [(f.code, f.part, f.pointer) for f in nav.navigation_findings(document)]
    assert found == [
        (
            "bookmark_group_missing",
            f"Report/definition/bookmarks/{DETAILS_VIEW}.bookmark.json",
            f"/explorationState/sections/{DETAILS}/visualContainerGroups/{OUTER_GROUP}"
            "/children/gone",
        ),
        (
            "navigator_page_missing",
            f"Report/definition/pages/{PAGE1}/visuals/{n(1)}/visual.json",
            "/visual/objects/pages/0/selector/id",
        ),
    ]
    assert nav.navigation_problems(document) == [
        "The bookmark 'Details view' remembers a group that is not on page 'Details'",
        "The page navigator at 0, 0 on page '1' has a setting for a page that does not exist",
    ]
    # Both block the save, like any other broken reference (the validator names visuals its own
    # way).
    blocking = [f for f in validate_findings(document) if f.severity == "blocking"]
    assert [(f.code, f.part, f.pointer) for f in blocking] == found


def test_filters_hidden_or_locked_in_view_mode_are_captured_by_name() -> None:
    document = open_document()
    report_filter = document.report["filterConfig"]["filters"][0]
    report_filter["isLockedInViewMode"] = True
    run(document, {"op": "add_bookmark", "name": "Locked", "page": "1", "data": True})
    _, bookmark = bookmark_named(document, "Locked")
    filters = bookmark["explorationState"]["filters"]
    assert list(filters) == ["byName"]
    assert filters["byName"][report_filter["name"]]["name"] == report_filter["name"]
    # The page's own filter (on a field, not locked) stays matched by its expression.
    section = bookmark["explorationState"]["sections"][PAGE1]
    assert list(section["filters"]) == ["byExpr"]


def test_a_bookmark_navigator_can_open_a_bookmark_on_deselection() -> None:
    document = open_document()
    run(document, {"op": "add_navigator", "page": "1", "kind": "bookmark"})
    outcome = run(
        document,
        {
            "op": "set_navigator",
            "visual": "bookmark navigator",
            "deselection_bookmark": "Details view",
        },
    )
    assert outcome.summaries == [
        "Set the bookmark navigator at 0, 0 to: clicking the selected bookmark again opens "
        "'Details view'."
    ]
    props = visual_named(document, n(1))["visual"]["objects"]["bookmarks"][0]["properties"]
    assert props == {
        "allowDeselectionBookmark": literal("true"),
        "deselectionBookmark": literal(f"'{DETAILS_VIEW}'"),
    }
    with pytest.raises(NeedsConfirmation) as asked:
        run(document, {"op": "delete_bookmark", "bookmark": "Details view"})
    assert asked.value.detail == (
        "The bookmark 'Details view' is still used: The bookmark navigator at 0, 0 on page '1' "
        "opens it on deselection. Deleting it switches that off."
    )
    outcome = run(
        document, {"op": "delete_bookmark", "bookmark": "Details view", "cleanup_references": True}
    )
    assert outcome.summaries == [
        "Deleted the bookmark 'Details view'. The bookmark navigator at 0, 0 on page '1' no "
        "longer opens a bookmark on deselection."
    ]
    assert props == {"allowDeselectionBookmark": literal("false")}
    assert refusal(
        document,
        {"op": "set_navigator", "visual": NAV_BUTTON, "deselection_bookmark": "Channel focus"},
    ).endswith("is not a page or bookmark navigator")


def test_every_card_is_complete() -> None:
    for card in nav.TOOL_CARDS:
        assert card.family == "navigation"
        assert len(card.examples) >= 3, card.op
        assert card.capability in {
            "bookmarks",
            "buttons_actions",
            "edit_interactions",
            "drillthrough_tooltip_pages",
            "sync_slicers",
        }
        for _, example in card.examples:
            assert example["op"] == card.op
    ops = {c.op for c in edit_tools.tool_cards()}
    assert {c.op for c in nav.TOOL_CARDS} <= ops


def test_a_card_builds_nested_action_fields_from_its_slots() -> None:
    card = next(c for c in nav.TOOL_CARDS if c.op == "set_button_action")
    assert card.to_operation({"visual": "Details", "action": "page", "target_page": "Stores"}) == {
        "op": "set_button_action",
        "visual": "Details",
        "action": {"type": "page", "page": "Stores"},
    }


def test_cards_turn_a_text_of_names_into_a_list() -> None:
    cards = {c.op: c for c in nav.TOOL_CARDS}
    assert cards["group_bookmarks"].to_operation(
        {"bookmarks": "Overview and 'Top agents'", "name": "Views"}
    ) == {"op": "group_bookmarks", "bookmarks": ["Overview", "Top agents"], "name": "Views"}
    assert cards["sync_slicer"].to_operation(
        {"visual": "Store", "pages": "Stores, Agent details", "hidden_on": ["Spare"]}
    ) == {
        "op": "sync_slicer",
        "visual": "Store",
        "pages": ["Stores", "Agent details"],
        "hidden_on": ["Spare"],
    }


def test_smaller_options_and_refusals() -> None:
    document = open_document()
    run(
        document,
        {
            "op": "set_drillthrough",
            "page": "Store drillthrough",
            "fields": [{"name": "StoreName"}],
            "cross_report": True,
            "back_button": False,
        },
    )
    assert (
        document.page("Store drillthrough").data["pageBinding"]["referenceScope"] == "CrossReport"
    )
    run(
        document,
        {"op": "update_bookmark", "bookmark": "Channel focus", "recapture": False, "visuals": []},
    )
    # Every option was on and the selected visuals are gone: nothing is left to say.
    assert "options" not in document.bookmark(CHANNEL_FOCUS)
    for name in ("1", "Details", "Store drillthrough"):
        run(document, {"op": "set_page_hidden", "page": name, "hidden": True})
    assert refusal(
        document, {"op": "add_page", "name": "Tip"}, {"op": "set_tooltip_page", "page": "Tip"}
    ) == ("Every other page is hidden, so this one has to stay visible")
    assert refusal(
        document, {"op": "set_visual_tooltip", "visual": NAV_BUTTON, "page": "Sales tooltip"}
    ) == ("The action button at 1668, 110 shows no data tooltip")
    assert refusal(document, {"op": "sync_slicer", "visual": COUNTRY}) == (
        "Say which pages the slicer should sync to"
    )


EXAMPLES = [
    pytest.param(card.op, request, example, id=f"{card.op}-{index}")
    for card in nav.TOOL_CARDS
    for index, (request, example) in enumerate(card.examples)
]


@pytest.mark.parametrize(("op", "request_text", "example"), EXAMPLES)
def test_every_card_example_applies_to_the_agents_twin(
    op: str, request_text: str, example: Mapping[str, Any]
) -> None:
    document = open_document("structure-twin")
    run(document, *nav.EXAMPLE_SETUP)
    outcome = run(document, example)
    assert outcome.report_changed
    assert len(outcome.summaries) == 1 and outcome.summaries[0].endswith(".")


@pytest.mark.parametrize("probe", nav.DESKTOP_PROBES, ids=[p.name for p in nav.DESKTOP_PROBES])
def test_every_probe_applies_to_the_agents_twin_and_passes_its_own_check(
    probe: edit_tools.ProbeCase,
) -> None:
    document = open_document("structure-twin")
    run(document, *probe.operations)
    parts = {name: data for name, data, _ in document.iter_json_parts()}
    assert probe.check is None or probe.check(parts) == []
    assert probe.family == "navigation" and probe.page == "1" and probe.expect


def test_probes_cover_every_operation_and_every_unverified_shape_has_a_source() -> None:
    probed = {step["op"] for probe in nav.DESKTOP_PROBES for step in probe.operations}
    assert set(nav.NAVIGATION_HANDLERS) <= probed
    assert all(text.strip() for text in nav.UNVERIFIED_SHAPES.values())
    assert set(nav.UNVERIFIED_SHAPES) >= {
        "bookmark.file",
        "drillthrough.page",
        "interactions",
        "sync.group",
    }


@pytest.mark.skipif(not REAL.exists(), reason="the user's Agents dashboard is not on this machine")
def test_every_probe_applies_to_the_real_agents_dashboard() -> None:
    for probe in nav.DESKTOP_PROBES:
        document = PbirDocument(PbixPackage(REAL))
        run(document, *probe.operations)
        parts = {name: data for name, data, _ in document.iter_json_parts()}
        assert probe.check is None or probe.check(parts) == [], probe.name
